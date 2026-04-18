"""Branch-local gateway observability-backend loader."""

from __future__ import annotations

from typing import Any


def build_active_gateway_observability_backend(*args: Any, **kwargs: Any) -> Any:
    """Instantiate the active branch observability backend."""
    from gateway.napcat_observability_backend import NapCatObservabilityBackend

    return NapCatObservabilityBackend(*args, **kwargs)
