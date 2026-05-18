"""Generic agent/runtime event bridge.

Core modules should depend on this bridge instead of importing platform-
specific observability modules directly. The current branch-local
implementation forwards to the active gateway observability backend when it is
available.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional
import uuid

from gateway.observability_backend import build_gateway_observability_backend


_OBSERVABILITY_BACKEND = build_gateway_observability_backend()
AgentEventType = _OBSERVABILITY_BACKEND.event_type_cls
AgentEventSeverity = _OBSERVABILITY_BACKEND.severity_cls
OBSERVABILITY_AVAILABLE = bool(AgentEventType and AgentEventSeverity)


def get_current_trace_context() -> Optional[Any]:
    """Return the currently bound trace context, if any."""
    return _OBSERVABILITY_BACKEND.get_current_trace_context()


@contextmanager
def bind_trace_context(trace_ctx: Optional[Any]) -> Iterator[None]:
    """Bind *trace_ctx* for the current thread when observability is enabled."""
    with _OBSERVABILITY_BACKEND.bind_trace_context(trace_ctx):
        yield


def emit_agent_event(
    event_type: Optional[Any],
    payload: Dict[str, Any],
    *,
    trace_ctx: Optional[Any] = None,
    severity: Optional[Any] = None,
) -> bool:
    """Emit a generic agent/runtime event through the active observability sink."""
    return _OBSERVABILITY_BACKEND.emit_event(
        event_type,
        payload,
        trace_ctx=trace_ctx,
        severity=severity,
    )


def emit_current_agent_event(
    event_type: Optional[Any],
    payload: Dict[str, Any],
    *,
    severity: Optional[Any] = None,
) -> bool:
    """Emit an event against the currently bound trace context."""
    trace_ctx = get_current_trace_context()
    if trace_ctx is None:
        return False
    return emit_agent_event(event_type, payload, trace_ctx=trace_ctx, severity=severity)


def begin_llm_request(trace_ctx: Optional[Any]) -> tuple[str, str]:
    """Set a new per-request id on the trace context and return the old value."""
    if trace_ctx is None:
        return "", ""
    request_id = uuid.uuid4().hex
    previous_request_id = str(getattr(trace_ctx, "llm_request_id", "") or "")
    trace_ctx.llm_request_id = request_id
    return request_id, previous_request_id


def end_llm_request(trace_ctx: Optional[Any], previous_request_id: str) -> None:
    """Restore the previous LLM request id on the trace context."""
    if trace_ctx is None:
        return
    trace_ctx.llm_request_id = previous_request_id
