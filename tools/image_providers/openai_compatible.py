"""OpenAI-compatible image generation backend.

Supports Volcengine/Ark Seedream models and any other provider that exposes
the OpenAI ``images.generate`` endpoint.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Union
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

DEFAULT_CUSTOM_TIMEOUT = 120

CUSTOM_IMAGE_SIZE_MAP = {
    "landscape": "2304x1792",
    "square": "1920x1920",
    "portrait": "1792x2304",
}


def _load_image_generation_config() -> Dict[str, Any]:
    """Load the optional image generation backend config."""
    try:
        from hermes_cli.config import load_config

        config = load_config()
        return dict(config.get("image_generation") or {})
    except ImportError:
        logger.debug("hermes_cli.config not available, using default image generation backend")
        return {}
    except Exception as e:
        logger.warning("Failed to load image generation config: %s", e, exc_info=True)
        return {}


def _resolve_image_generation_backend() -> Dict[str, Any]:
    """Resolve whether to use the custom OpenAI-compatible backend or FAL."""
    backend_config = _load_image_generation_config()
    provider = str(backend_config.get("provider") or "").strip().lower()
    model = str(backend_config.get("model") or "").strip()
    base_url = str(backend_config.get("base_url") or "").strip().rstrip("/")
    api_key = str(backend_config.get("api_key") or os.getenv("OPENAI_API_KEY") or "").strip()

    timeout_raw = backend_config.get("timeout", DEFAULT_CUSTOM_TIMEOUT)
    try:
        timeout = int(timeout_raw)
    except (TypeError, ValueError):
        timeout = DEFAULT_CUSTOM_TIMEOUT
    timeout = max(timeout, 1)

    custom_configured = bool(base_url and model and api_key)
    use_custom_backend = custom_configured or provider in {"custom", "openai", "openai-compatible", "ark"}

    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "timeout": timeout,
        "custom_configured": custom_configured,
        "selected_backend": "custom" if use_custom_backend else "fal",
    }


def _normalize_input_image(image: str) -> str:
    """Return an input image as URL/data URL for OpenAI-compatible APIs."""
    image_value = str(image or "").strip()
    if not image_value:
        raise ValueError("Reference image must be a non-empty string")

    lowered = image_value.lower()
    if lowered.startswith(("https://", "http://", "data:image/")):
        return image_value

    image_path = Path(image_value).expanduser()
    if not image_path.exists() or not image_path.is_file():
        raise ValueError(f"Reference image not found: {image_value}")

    mime_type, _ = mimetypes.guess_type(str(image_path))
    if not mime_type:
        mime_type = "image/png"
    if not mime_type.startswith("image/"):
        raise ValueError(f"Unsupported reference image type: {mime_type}")

    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _normalize_input_images(image: Optional[Union[str, list]]) -> Optional[Union[str, list]]:
    """Normalize optional input image(s) for custom image generation backends."""
    if image is None:
        return None
    if isinstance(image, str):
        normalized = _normalize_input_image(image)
        return normalized if normalized else None
    if isinstance(image, list):
        normalized_images = [_normalize_input_image(item) for item in image if str(item or "").strip()]
        return normalized_images or None
    raise ValueError("image must be a string or a list of strings")


def _save_base64_image(b64_data: str, output_format: str) -> str:
    """Persist a base64 image response when the backend does not return a URL."""
    from hermes_constants import get_hermes_dir

    output_dir = get_hermes_dir("cache/images", "image_cache")
    output_dir.mkdir(parents=True, exist_ok=True)
    image_path = output_dir / f"generated_{uuid.uuid4()}.{output_format}"
    image_path.write_bytes(base64.b64decode(b64_data))
    return str(image_path)


def _guess_image_extension_from_url(image_url: str, fallback: str = ".png") -> str:
    path = urlsplit(str(image_url or "")).path
    suffix = Path(path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return suffix
    return fallback


def _cache_generated_remote_image(image_url: str, fallback_ext: str = ".png") -> str:
    """Download a generated remote image into the local Hermes image cache."""
    from gateway.platforms.base import cache_image_from_bytes, safe_url_for_log
    from tools.url_safety import is_safe_url

    if not is_safe_url(image_url):
        raise ValueError(f"Blocked unsafe generated image URL: {safe_url_for_log(image_url)}")

    ext = _guess_image_extension_from_url(image_url, fallback=fallback_ext)
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        response = client.get(
            image_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)",
                "Accept": "image/*,*/*;q=0.8",
            },
        )
        response.raise_for_status()
        return cache_image_from_bytes(response.content, ext)


def _generate_with_openai_compatible_backend(
    *,
    prompt: str,
    aspect_ratio: str,
    num_images: int,
    output_format: str,
    image: Optional[Union[str, list]] = None,
) -> Dict[str, Any]:
    """Generate image(s) using an OpenAI-compatible images endpoint."""
    backend = _resolve_image_generation_backend()
    if not backend["custom_configured"]:
        raise ValueError("image_generation.base_url, model, and api_key are required for the custom backend")

    from openai import OpenAI

    openai_client = OpenAI(
        api_key=backend["api_key"],
        base_url=backend["base_url"],
        timeout=backend["timeout"],
        max_retries=0,
    )
    try:
        normalized_images = _normalize_input_images(image)
        extra_body = {}
        if normalized_images is not None:
            extra_body["image"] = normalized_images

        model_name = str(backend["model"] or "").strip().lower()
        image_kwargs = {
            "model": backend["model"],
            "prompt": prompt.strip(),
            "size": CUSTOM_IMAGE_SIZE_MAP[aspect_ratio],
            "n": num_images,
            "response_format": "url",
            "extra_headers": {"x-idempotency-key": str(uuid.uuid4())},
            "extra_body": extra_body or None,
            "timeout": backend["timeout"],
        }
        # Volcengine's Seedream docs state output_format is only accepted by
        # the Seedream 5.0 lite variants; 4.5/4.0 default to jpeg and reject
        # the parameter entirely.
        if model_name in {"doubao-seedream-5-0-lite-260128", "doubao-seedream-5-0-260128"}:
            image_kwargs["output_format"] = output_format

        result = openai_client.images.generate(**image_kwargs)
    finally:
        close_fn = getattr(openai_client, "close", None)
        if callable(close_fn):
            close_fn()

    data = getattr(result, "data", None) or []
    if not data:
        raise ValueError("Invalid response from image backend - no images returned")

    first_image = data[0]
    image_url = getattr(first_image, "url", None)
    if image_url:
        cached_path = _cache_generated_remote_image(str(image_url), fallback_ext=f".{output_format}")
        return {
            "backend": "custom",
            "image": cached_path,
            "source_url": str(image_url),
        }

    image_b64 = getattr(first_image, "b64_json", None)
    if image_b64:
        return {
            "backend": "custom",
            "image": _save_base64_image(image_b64, output_format),
        }

    raise ValueError("Image backend returned neither URL nor base64 image data")


def check_custom_image_generation_backend() -> bool:
    """Return True when an OpenAI-compatible image backend is configured."""
    backend = _resolve_image_generation_backend()
    return backend["custom_configured"]
