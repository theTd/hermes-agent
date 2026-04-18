"""Compatibility helpers for agent/runtime observability integrations."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional

from agent.usage_pricing import estimate_usage_cost, normalize_usage


class _NoOpEnumProxy:
    """Falsy attribute proxy used when no observability backend is installed."""

    def __bool__(self) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        return None


def _noop_begin_llm_request(trace_ctx: Optional[Any]) -> tuple[str, str]:
    return "", ""


def _noop_bind_trace_context(trace_ctx: Optional[Any]):
    class _NoOpBinding:
        def __enter__(self):
            return trace_ctx

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    return _NoOpBinding()


def _noop_emit_agent_event(
    event_type: Optional[Any],
    payload: Dict[str, Any],
    *,
    trace_ctx: Optional[Any] = None,
    severity: Optional[Any] = None,
) -> bool:
    return False


def _noop_end_llm_request(trace_ctx: Optional[Any], previous_request_id: str) -> None:
    return None


def _noop_get_current_trace_context() -> Optional[Any]:
    return None


try:
    from gateway.agent_events import (
        AgentEventSeverity,
        AgentEventType,
        begin_llm_request,
        bind_trace_context,
        emit_agent_event,
        end_llm_request,
        get_current_trace_context,
    )

    DEFAULT_OBSERVABILITY_AVAILABLE = True
except Exception:  # pragma: no cover - exercised in non-gateway environments
    AgentEventSeverity = _NoOpEnumProxy()
    AgentEventType = _NoOpEnumProxy()
    begin_llm_request = _noop_begin_llm_request
    bind_trace_context = _noop_bind_trace_context
    emit_agent_event = _noop_emit_agent_event
    end_llm_request = _noop_end_llm_request
    get_current_trace_context = _noop_get_current_trace_context
    DEFAULT_OBSERVABILITY_AVAILABLE = False

DEFAULT_EVENT_SEVERITY = AgentEventSeverity
DEFAULT_EVENT_TYPE = AgentEventType
DEFAULT_BEGIN_LLM_REQUEST = begin_llm_request
DEFAULT_END_LLM_REQUEST = end_llm_request
DEFAULT_CURRENT_TRACE = SimpleNamespace(get=get_current_trace_context)


@dataclass(frozen=True)
class LlmRequestObservation:
    """Captured lifecycle data for a single LLM request."""

    trace_ctx: Optional[Any]
    request_id: str
    previous_request_id: str
    started_at: float
    streaming: bool


def get_current_trace_ctx(
    *,
    obs_available: bool,
    current_trace: Any,
    fallback_getter: Callable[[], Optional[Any]],
) -> Optional[Any]:
    """Return the currently bound trace context for backward-compat callers."""
    if not obs_available:
        return None
    try:
        getter = getattr(current_trace, "get", None)
        if callable(getter):
            return getter()
    except Exception:
        return None
    return fallback_getter()


def bind_trace_context_for_observability(
    trace_ctx: Optional[Any],
    *,
    bind_trace_context_fn: Callable[[Optional[Any]], Any] = bind_trace_context,
):
    """Bind the provided trace context using the active observability bridge."""
    return bind_trace_context_fn(trace_ctx)


def emit_observability_event(
    event_type: Optional[Any],
    payload: Dict[str, Any],
    *,
    trace_ctx: Optional[Any] = None,
    severity: Optional[Any] = None,
    emit_agent_event_fn: Callable[..., bool] = emit_agent_event,
) -> bool:
    """Emit an event using the active observability bridge."""
    return emit_agent_event_fn(
        event_type,
        payload,
        trace_ctx=trace_ctx,
        severity=severity,
    )


def emit_current_observability_event(
    event_type: Optional[Any],
    payload: Dict[str, Any],
    *,
    severity: Optional[Any] = None,
    get_trace_ctx_fn: Callable[[], Optional[Any]],
    emit_event_fn: Callable[..., bool],
) -> bool:
    """Emit an event against the currently bound trace context."""
    active_trace_ctx = get_trace_ctx_fn()
    if active_trace_ctx is None:
        return False
    return emit_event_fn(
        event_type,
        payload,
        trace_ctx=active_trace_ctx,
        severity=severity,
    )


def preview_text(value: Any, max_chars: int = 200) -> str:
    """Return a short JSON-friendly preview of *value*."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    return text[:max_chars]


def build_request_body_payload(api_kwargs: Dict[str, Any], *, streaming: bool) -> str:
    """Serialize the effective model request body for observability."""
    try:
        request_body = copy.deepcopy(api_kwargs)
    except Exception:
        request_body = dict(api_kwargs)
    if isinstance(request_body, dict):
        request_body.pop("timeout", None)
        if streaming and "stream" not in request_body:
            request_body["stream"] = True
            if "stream_options" not in request_body:
                request_body["stream_options"] = {"include_usage": True}
    return json.dumps(request_body, ensure_ascii=False, indent=2, default=str)


def build_usage_cost_payload(
    usage: Any,
    *,
    model: str,
    provider: str | None,
    base_url: str | None,
    api_mode: str | None,
    api_key: str | None = None,
) -> Dict[str, Any]:
    """Normalize usage and estimate cost using the same pricing path as usagefooter."""
    if not usage:
        return {}
    canonical = normalize_usage(usage, provider=provider, api_mode=api_mode)
    cost_result = estimate_usage_cost(
        model,
        canonical,
        provider=provider,
        base_url=base_url,
        api_key=api_key or "",
    )
    payload: Dict[str, Any] = {
        "input_tokens": canonical.input_tokens,
        "output_tokens": canonical.output_tokens,
        "cache_read_tokens": canonical.cache_read_tokens,
        "cache_write_tokens": canonical.cache_write_tokens,
        "reasoning_tokens": canonical.reasoning_tokens,
        "prompt_tokens": canonical.prompt_tokens,
        "total_tokens": canonical.total_tokens,
        "cost_status": cost_result.status,
        "cost_source": cost_result.source,
        "cost_label": cost_result.label,
    }
    if cost_result.amount_usd is not None:
        payload["estimated_cost_usd"] = float(cost_result.amount_usd)
    return payload


def begin_llm_request_observation(
    api_kwargs: Dict[str, Any],
    *,
    model: str,
    provider: str | None,
    api_mode: str | None,
    streaming: bool,
    obs_available: bool,
    current_trace: Any,
    fallback_getter: Callable[[], Optional[Any]],
    begin_request_fn: Callable[[Optional[Any]], tuple[str, str]] = begin_llm_request,
    emit_event_fn: Callable[..., bool] = emit_agent_event,
    event_type_cls: Any = AgentEventType,
) -> LlmRequestObservation:
    """Start an LLM request observation and emit the request event."""
    trace_ctx = get_current_trace_ctx(
        obs_available=obs_available,
        current_trace=current_trace,
        fallback_getter=fallback_getter,
    )
    request_id = ""
    previous_request_id = ""
    if trace_ctx is not None:
        try:
            request_id, previous_request_id = begin_request_fn(trace_ctx)
        except Exception:
            request_id, previous_request_id = "", ""
        try:
            emit_event_fn(
                getattr(event_type_cls, "AGENT_MODEL_REQUESTED", None),
                {
                    "llm_request_id": request_id,
                    "model": model,
                    "provider": provider,
                    "api_mode": api_mode,
                    "streaming": streaming,
                    "request_body": build_request_body_payload(api_kwargs, streaming=streaming),
                },
                trace_ctx=trace_ctx,
            )
        except Exception:
            pass
    return LlmRequestObservation(
        trace_ctx=trace_ctx,
        request_id=request_id,
        previous_request_id=previous_request_id,
        started_at=time.time(),
        streaming=streaming,
    )


def finish_llm_request_observation(
    observation: LlmRequestObservation,
    *,
    model: str,
    provider: str | None,
    base_url: str | None,
    api_mode: str | None,
    api_key: str | None,
    success: bool,
    usage: Any = None,
    error: Any = None,
    end_request_fn: Callable[[Optional[Any], str], None] = end_llm_request,
    emit_event_fn: Callable[..., bool] = emit_agent_event,
    event_type_cls: Any = AgentEventType,
    severity_cls: Any = AgentEventSeverity,
) -> None:
    """Finish an LLM request observation and emit the completion event."""
    trace_ctx = observation.trace_ctx
    if trace_ctx is not None:
        payload: Dict[str, Any] = {
            "llm_request_id": observation.request_id,
            "model": model,
            "provider": provider,
            "duration_ms": (time.time() - observation.started_at) * 1000,
            "success": bool(success),
        }
        if success:
            payload.update(
                build_usage_cost_payload(
                    usage,
                    model=model,
                    provider=provider,
                    base_url=base_url,
                    api_mode=api_mode,
                    api_key=api_key,
                )
            )
        elif error is not None:
            payload["error"] = str(error)[:300]
        try:
            emit_event_fn(
                getattr(event_type_cls, "AGENT_MODEL_COMPLETED", None),
                payload,
                trace_ctx=trace_ctx,
                severity=(getattr(severity_cls, "WARN", None) if not success else None),
            )
        except Exception:
            pass
    try:
        end_request_fn(trace_ctx, observation.previous_request_id)
    except Exception:
        pass


def emit_final_response_event(
    *,
    final_response: str,
    interrupted: bool,
    completed: bool,
    api_call_count: int,
    input_tokens: int,
    output_tokens: int,
    model: str,
    emit_enabled: bool,
    obs_available: bool,
    current_trace: Any,
    fallback_getter: Callable[[], Optional[Any]],
    emit_event_fn: Callable[..., bool] = emit_agent_event,
    event_type_cls: Any = AgentEventType,
) -> None:
    """Emit the final/suppressed response event for an agent turn."""
    if not emit_enabled:
        return
    trace_ctx = get_current_trace_ctx(
        obs_available=obs_available,
        current_trace=current_trace,
        fallback_getter=fallback_getter,
    )
    if trace_ctx is None:
        return
    try:
        if final_response:
            emit_event_fn(
                getattr(event_type_cls, "AGENT_RESPONSE_FINAL", None),
                {
                    "response_preview": str(final_response or "")[:500],
                    "model": model,
                    "api_call_count": api_call_count,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "interrupted": interrupted,
                },
                trace_ctx=trace_ctx,
            )
        elif not interrupted:
            emit_event_fn(
                getattr(event_type_cls, "AGENT_RESPONSE_SUPPRESSED", None),
                {
                    "reason": "no_final_response",
                    "model": model,
                    "api_call_count": api_call_count,
                    "completed": completed,
                },
                trace_ctx=trace_ctx,
            )
    except Exception:
        pass


def get_current_napcat_trace_ctx(
    *,
    obs_available: bool,
    current_trace: Any,
    fallback_getter: Callable[[], Optional[Any]],
) -> Optional[Any]:
    """Backward-compatible alias for older branch-local callers."""
    return get_current_trace_ctx(
        obs_available=obs_available,
        current_trace=current_trace,
        fallback_getter=fallback_getter,
    )


def bind_napcat_trace_context(
    trace_ctx: Optional[Any],
    *,
    bind_trace_context_fn: Callable[[Optional[Any]], Any] = bind_trace_context,
):
    """Backward-compatible alias for older branch-local callers."""
    return bind_trace_context_for_observability(
        trace_ctx,
        bind_trace_context_fn=bind_trace_context_fn,
    )


def emit_napcat_event(
    event_type: Optional[Any],
    payload: Dict[str, Any],
    *,
    trace_ctx: Optional[Any] = None,
    severity: Optional[Any] = None,
    emit_agent_event_fn: Callable[..., bool] = emit_agent_event,
) -> bool:
    """Backward-compatible alias for older branch-local callers."""
    return emit_observability_event(
        event_type,
        payload,
        trace_ctx=trace_ctx,
        severity=severity,
        emit_agent_event_fn=emit_agent_event_fn,
    )


def emit_current_napcat_event(
    event_type: Optional[Any],
    payload: Dict[str, Any],
    *,
    severity: Optional[Any] = None,
    get_trace_ctx_fn: Callable[[], Optional[Any]],
    emit_event_fn: Callable[..., bool],
) -> bool:
    """Backward-compatible alias for older branch-local callers."""
    return emit_current_observability_event(
        event_type,
        payload,
        severity=severity,
        get_trace_ctx_fn=get_trace_ctx_fn,
        emit_event_fn=emit_event_fn,
    )
