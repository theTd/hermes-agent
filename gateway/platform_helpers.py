"""Central registry for platform-specific helper adapters used by tools."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Optional

from gateway.config import Platform

_TOOL_PLATFORM_MAP = {
    "telegram": Platform.TELEGRAM,
    "discord": Platform.DISCORD,
    "slack": Platform.SLACK,
    "napcat": Platform.NAPCAT,
    "whatsapp": Platform.WHATSAPP,
    "signal": Platform.SIGNAL,
    "bluebubbles": Platform.BLUEBUBBLES,
    "qqbot": Platform.QQBOT,
    "matrix": Platform.MATRIX,
    "mattermost": Platform.MATTERMOST,
    "homeassistant": Platform.HOMEASSISTANT,
    "dingtalk": Platform.DINGTALK,
    "feishu": Platform.FEISHU,
    "wecom": Platform.WECOM,
    "wecom_callback": Platform.WECOM_CALLBACK,
    "weixin": Platform.WEIXIN,
    "email": Platform.EMAIL,
    "sms": Platform.SMS,
}


@dataclass(frozen=True)
class PlatformToolHelperSpec:
    """Declarative helper-adapter wiring for shared tool entrypoints."""

    module_name: str
    attr_name: str
    helper_one_shot: bool = False


_TOOL_HELPER_REGISTRY = {
    Platform.NAPCAT: PlatformToolHelperSpec(
        module_name="gateway.platforms.napcat",
        attr_name="NapCatAdapter",
        helper_one_shot=True,
    ),
}


def resolve_tool_platform(platform_name: str) -> Optional[Platform]:
    """Resolve a tool-level platform name into the corresponding enum value."""
    return _TOOL_PLATFORM_MAP.get(str(platform_name or "").strip().lower())


def get_tool_platform_names() -> list[str]:
    """Return tool-addressable platform names in stable display order."""
    return list(_TOOL_PLATFORM_MAP.keys())


def uses_helper_one_shot(platform: Platform) -> bool:
    """Return whether sends for this platform are delegated to a helper adapter."""
    spec = _TOOL_HELPER_REGISTRY.get(platform)
    return bool(spec and spec.helper_one_shot)


def get_helper_one_shot_platform_names() -> list[str]:
    """Return platform names whose send_message delivery uses helper one-shot sends."""
    return [name for name, platform in _TOOL_PLATFORM_MAP.items() if uses_helper_one_shot(platform)]


def build_platform_tool_helper(platform: Any, platform_config: Any) -> Optional[Any]:
    """Return a short-lived helper adapter for tool-level platform operations."""
    spec = _TOOL_HELPER_REGISTRY.get(platform)
    if spec is None:
        return None
    module = importlib.import_module(spec.module_name)
    helper_cls = getattr(module, spec.attr_name)
    return helper_cls(platform_config)
