"""Generic gateway config-extension loader."""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class GatewayConfigExtension(Protocol):
    """Branch-local gateway config hooks consumed by the shared config loader."""

    def apply_platform_defaults(self, config: Any) -> None: ...
    def apply_env_overrides(self, config: Any) -> None: ...
    def bridge_platform_config(self, platform: Any, platform_cfg: Any, bridged: dict[str, Any]) -> None: ...
    def platform_connection_ready(self, platform: Any, platform_config: Any) -> Optional[bool]: ...


class NullGatewayConfigExtension:
    """No-op fallback when no branch-local config extension exists."""

    def apply_platform_defaults(self, config: Any) -> None:
        del config

    def apply_env_overrides(self, config: Any) -> None:
        del config

    def bridge_platform_config(self, platform: Any, platform_cfg: Any, bridged: dict[str, Any]) -> None:
        del platform, platform_cfg, bridged

    def platform_connection_ready(self, platform: Any, platform_config: Any) -> Optional[bool]:
        del platform, platform_config
        return None


def build_gateway_config_extension() -> GatewayConfigExtension:
    """Instantiate the active config extension without leaking branch names."""
    try:
        from gateway.config_extension_loader import build_active_gateway_config_extension

        return build_active_gateway_config_extension()
    except Exception:
        return NullGatewayConfigExtension()


__all__ = [
    "GatewayConfigExtension",
    "NullGatewayConfigExtension",
    "build_gateway_config_extension",
]
