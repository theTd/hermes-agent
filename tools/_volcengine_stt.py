"""Volcengine STT credential resolution and helpers.

Extracted from transcription_tools.py to reduce upstream merge conflicts in
the provider-credential section.
"""

import os
import re
from typing import Any, Optional

_ENV_TEMPLATE_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

DEFAULT_VOLCENGINE_STT_MODEL = os.getenv("STT_VOLCENGINE_MODEL", "bigmodel")
DEFAULT_VOLCENGINE_STT_RESOURCE_ID = os.getenv(
    "VOLCENGINE_STT_RESOURCE_ID", "volc.bigasr.auc_turbo"
)
VOLCENGINE_STT_BASE_URL = os.getenv(
    "VOLCENGINE_STT_BASE_URL",
    "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
)


def _resolve_secret_value(value: Any) -> str:
    """Resolve a config secret, including ${VAR} placeholders backed by ~/.hermes/.env."""
    resolved = str(value or "").strip()
    if not resolved:
        return ""

    expanded = os.path.expandvars(resolved).strip()
    if expanded != resolved and "$" not in expanded:
        return expanded

    match = _ENV_TEMPLATE_RE.fullmatch(resolved)
    if not match:
        return expanded

    try:
        from hermes_cli.config import get_env_value

        env_value = str(get_env_value(match.group(1)) or "").strip()
        if env_value:
            return env_value
    except Exception:
        pass

    return expanded


def _resolve_volcengine_stt_credentials(stt_config: Optional[dict] = None) -> dict[str, str]:
    if stt_config is None:
        from tools.transcription_tools import _load_stt_config

        stt_config = _load_stt_config()

    volc_cfg = stt_config.get("volcengine", {})
    api_key = _resolve_secret_value(volc_cfg.get("api_key"))
    app_key = _resolve_secret_value(volc_cfg.get("app_key"))
    access_key = _resolve_secret_value(volc_cfg.get("access_key"))

    if not api_key:
        api_key = str(os.getenv("VOLCENGINE_STT_API_KEY") or "").strip()
    if not app_key:
        app_key = str(os.getenv("VOLCENGINE_STT_APP_KEY") or "").strip()
    if not access_key:
        access_key = str(os.getenv("VOLCENGINE_STT_ACCESS_KEY") or "").strip()

    if api_key:
        return {"auth_mode": "api_key", "api_key": api_key}
    if app_key and access_key:
        return {"auth_mode": "legacy", "app_key": app_key, "access_key": access_key}
    raise ValueError(
        "Neither stt.volcengine.api_key / VOLCENGINE_STT_API_KEY nor "
        "stt.volcengine.app_key + access_key / VOLCENGINE_STT_APP_KEY + VOLCENGINE_STT_ACCESS_KEY is set"
    )


def _has_volcengine_stt_credentials(stt_config: Optional[dict] = None) -> bool:
    try:
        _resolve_volcengine_stt_credentials(stt_config)
        return True
    except ValueError:
        return False
