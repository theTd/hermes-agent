"""NapCat-specific cache for auto-generated image vision descriptions.

NapCat group chats often repeat the same meme/sticker image under different
temporary filenames. Cache by image-content hash so repeated images can reuse
the previous description instead of consuming more vision tokens.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home


_CACHE_PATH = get_hermes_home() / "gateway" / "napcat_vision_cache.json"
_CACHE_LOCK = threading.Lock()
_MAX_CACHE_ENTRIES = 2048


def _load_cache() -> dict[str, dict]:
    if not _CACHE_PATH.exists():
        return {}
    try:
        payload = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_cache(cache: dict[str, dict]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _prune_cache(cache: dict[str, dict]) -> dict[str, dict]:
    if len(cache) <= _MAX_CACHE_ENTRIES:
        return cache

    ranked = sorted(
        cache.items(),
        key=lambda item: float((item[1] or {}).get("cached_at") or 0.0),
        reverse=True,
    )
    return dict(ranked[:_MAX_CACHE_ENTRIES])


def _hash_image_file(image_path: str) -> Optional[str]:
    path = Path(image_path).expanduser()
    if not path.is_file():
        return None

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_cached_analysis(image_path: str) -> Optional[str]:
    cache_key = _hash_image_file(image_path)
    if not cache_key:
        return None

    with _CACHE_LOCK:
        entry = _load_cache().get(cache_key)
    if not isinstance(entry, dict):
        return None

    analysis = str(entry.get("analysis") or "").strip()
    return analysis or None


def cache_image_analysis(image_path: str, analysis: str) -> bool:
    cache_key = _hash_image_file(image_path)
    normalized = str(analysis or "").strip()
    if not cache_key or not normalized:
        return False

    with _CACHE_LOCK:
        cache = _load_cache()
        cache[cache_key] = {
            "analysis": normalized,
            "cached_at": time.time(),
        }
        _save_cache(_prune_cache(cache))
    return True
