"""Branch-local gateway config-extension loader."""

from __future__ import annotations

from typing import Any


def build_active_gateway_config_extension(*args: Any, **kwargs: Any) -> Any:
    """Instantiate the active branch config extension."""
    from gateway.napcat_config_extension import NapCatGatewayConfigExtension

    return NapCatGatewayConfigExtension(*args, **kwargs)
