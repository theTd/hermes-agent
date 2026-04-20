"""Stable gateway runtime-extension interfaces and helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Protocol, runtime_checkable

from gateway.agent_events import AgentEventSeverity, AgentEventType, bind_trace_context, emit_agent_event


class _AgentEventProxy:
    """Expose event enums through a tolerant facade."""

    def __init__(self, target: Any):
        self._target = target

    def __bool__(self) -> bool:
        return self._target is not None

    def __getattr__(self, name: str) -> Any:
        target = self._target
        if target is None:
            return None
        return getattr(target, name, None)


GatewayEventType = _AgentEventProxy(AgentEventType)
GatewayEventSeverity = _AgentEventProxy(AgentEventSeverity)


def get_gateway_trace_context(owner: Any) -> Optional[Any]:
    """Return the runtime trace context attached to an event-like object."""
    if owner is None:
        return None
    return getattr(owner, "gateway_trace_ctx", None)


def emit_gateway_event(
    event_type: Optional[Any],
    payload: Dict[str, Any],
    *,
    trace_ctx: Optional[Any] = None,
    severity: Optional[Any] = None,
) -> bool:
    """Emit a gateway runtime event through the active event publisher."""
    return emit_agent_event(
        event_type,
        payload,
        trace_ctx=trace_ctx,
        severity=severity,
    )


def emit_gateway_trace_event(
    trace_ctx: Optional[Any],
    event_type: Optional[Any],
    payload: Dict[str, Any],
    *,
    severity: Optional[Any] = None,
) -> bool:
    """Emit a trace-bound runtime event when a trace context is present."""
    if not trace_ctx or not event_type:
        return False
    return emit_gateway_event(
        event_type,
        payload,
        trace_ctx=trace_ctx,
        severity=severity,
    )


@dataclass(frozen=True)
class GatewayRuntimeContext:
    """Explicit runtime dependencies for gateway orchestration helpers."""

    emit_event: Any = emit_gateway_event
    emit_trace_event: Optional[Any] = emit_gateway_trace_event
    event_type_cls: Any = GatewayEventType
    severity_cls: Any = GatewayEventSeverity
    bind_trace_context_fn: Any = bind_trace_context
    complexity_threshold: int = 16


def build_default_gateway_runtime_context(
    *,
    emit_event: Any = emit_gateway_event,
    emit_trace_event: Optional[Any] = emit_gateway_trace_event,
    event_type_cls: Any = GatewayEventType,
    severity_cls: Any = GatewayEventSeverity,
    bind_trace_context_fn: Any = bind_trace_context,
    complexity_threshold: int = 16,
) -> GatewayRuntimeContext:
    """Build the default runtime context for gateway extensions and helpers."""
    return GatewayRuntimeContext(
        emit_event=emit_event,
        emit_trace_event=emit_trace_event,
        event_type_cls=event_type_cls,
        severity_cls=severity_cls,
        bind_trace_context_fn=bind_trace_context_fn,
        complexity_threshold=complexity_threshold,
    )


@runtime_checkable
class GatewayRuntimeExtension(Protocol):
    """Stable runtime-extension surface consumed by ``GatewayRunner``."""

    def apply_session_defaults(self, source: Any, session_key: str, *, adapter: Optional[Any] = None) -> None: ...
    def check_command_permission(self, adapter: Any, source: Any, command: Optional[str]) -> tuple[bool, Optional[str]]: ...
    def bind_connected_adapter(
        self,
        adapter: Any,
        *,
        session_model_overrides: Optional[dict[str, dict[str, str]]] = None,
    ) -> None: ...
    def build_platform_adapter(self, platform: Any, config: Any) -> Any: ...
    def cancel_orchestrator_children(
        self,
        session_key: str,
        *,
        child_ids: Optional[list[str]] = None,
        reason: str = "",
    ) -> Any: ...
    def clear_orchestrator_sessions(self) -> None: ...
    def decorate_transcript_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        event: Optional[Any] = None,
        source: Optional[Any] = None,
    ) -> list[dict[str, Any]]: ...
    def format_running_agent_status(self, snapshot: Dict[str, Any]) -> str: ...
    def get_orchestrator_session(self, session_key: str) -> Optional[Any]: ...
    def handle_recall_event(self, event: Any) -> Any: ...
    def hydrate_event_trace_context(self, source: Any, event: Any, session_entry: Any) -> None: ...
    def image_analysis_cache(
        self,
        *,
        platform: Optional[Any] = None,
        source: Optional[Any] = None,
    ) -> tuple[Optional[Any], Optional[Any]]: ...
    def is_progress_query(self, text: str) -> bool: ...
    def iter_orchestrator_sessions(self) -> Iterable[tuple[str, Any]]: ...
    def normalize_adapter_turn_plan(self, plan: Any) -> Any: ...
    def orchestrator_enabled_for_source(self, source: Optional[Any]) -> bool: ...
    def orchestrator_cache_key_belongs_to_session(self, session_key: str, cache_key: str) -> bool: ...
    def orchestrator_fullagent_runtime_session_key(self, session_key: str) -> str: ...
    def orchestrator_fullagent_session_id(self, parent_session_id: str) -> str: ...
    def orchestrator_child_runtime_session_key(self, session_key: str, child_id: str) -> str: ...
    def orchestrator_child_session_id(self, parent_session_id: str, child_id: str) -> str: ...
    def orchestrator_internal_session_id(self, session_key: str) -> str: ...
    def orchestrator_session_keys(self) -> list[str]: ...
    def promotion_plan_for_turn(self, turn_plan: Any) -> Any: ...
    def promotion_support(self) -> Any: ...
    def referenced_image_inputs(self, event: Any, source: Optional[Any]) -> list[tuple[str, str]]: ...
    def run_orchestrator_turn(self, **kwargs: Any) -> Any: ...
    def running_agent_snapshot(self, session_key: str) -> Dict[str, Any]: ...
    def runtime_context(self) -> GatewayRuntimeContext: ...
    def start_observability_runtime(self) -> Any: ...
    def stop_observability_runtime(self) -> Any: ...
    def trace_context_for_event(self, event: Any, *, source: Optional[Any] = None) -> Optional[Any]: ...
    def trace_metadata_key(
        self,
        *,
        source: Optional[Any] = None,
        adapter: Optional[Any] = None,
    ) -> str: ...
    def transcript_metadata_for_event(self, event: Any, source: Any) -> Optional[dict[str, Any]]: ...


def build_gateway_runtime_extension(*args: Any, **kwargs: Any) -> GatewayRuntimeExtension:
    """Instantiate the active gateway runtime extension without leaking implementation names."""
    from gateway.runtime_extension_loader import build_active_gateway_runtime_extension

    return build_active_gateway_runtime_extension(*args, **kwargs)


__all__ = [
    "GatewayEventSeverity",
    "GatewayEventType",
    "GatewayRuntimeContext",
    "GatewayRuntimeExtension",
    "build_default_gateway_runtime_context",
    "build_gateway_runtime_extension",
    "emit_gateway_event",
    "emit_gateway_trace_event",
    "get_gateway_trace_context",
]
