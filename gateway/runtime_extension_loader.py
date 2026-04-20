"""Branch-local gateway runtime-extension loader."""

from __future__ import annotations

from typing import Any


def build_active_gateway_runtime_extension(*args: Any, **kwargs: Any) -> Any:
    """Instantiate the active branch runtime extension."""
    from gateway.napcat_gateway_extension import NapCatGatewayExtension

    return NapCatGatewayExtension(*args, **kwargs)
