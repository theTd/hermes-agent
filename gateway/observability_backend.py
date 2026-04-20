"""Generic gateway observability backend loader."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class GatewayObservabilityBackend(Protocol):
    """Stable observability backend surface used by the generic event bridge."""

    @property
    def event_type_cls(self) -> Any: ...

    @property
    def severity_cls(self) -> Any: ...

    def get_current_trace_context(self) -> Optional[Any]: ...
    def bind_trace_context(self, trace_ctx: Optional[Any]) -> Any: ...
    def emit_event(
        self,
        event_type: Optional[Any],
        payload: dict[str, Any],
        *,
        trace_ctx: Optional[Any] = None,
        severity: Optional[Any] = None,
    ) -> bool: ...


@dataclass(frozen=True)
class NullGatewayObservabilityBackend:
    """No-op fallback used when no branch-local backend is available."""

    event_type_cls: Any = None
    severity_cls: Any = None

    def get_current_trace_context(self) -> Optional[Any]:
        return None

    def bind_trace_context(self, trace_ctx: Optional[Any]) -> Any:
        del trace_ctx
        return nullcontext()

    def emit_event(
        self,
        event_type: Optional[Any],
        payload: dict[str, Any],
        *,
        trace_ctx: Optional[Any] = None,
        severity: Optional[Any] = None,
    ) -> bool:
        del event_type, payload, trace_ctx, severity
        return False


def build_gateway_observability_backend() -> GatewayObservabilityBackend:
    """Instantiate the active observability backend without leaking branch names."""
    try:
        from gateway.observability_backend_loader import (
            build_active_gateway_observability_backend,
        )

        return build_active_gateway_observability_backend()
    except Exception:
        return NullGatewayObservabilityBackend()


__all__ = [
    "GatewayObservabilityBackend",
    "NullGatewayObservabilityBackend",
    "build_gateway_observability_backend",
]
