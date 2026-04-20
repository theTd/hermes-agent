"""Napcat-specific instrumentation for the agent loop.

This module centralises napcat's observability, memory-hook, and
tool-event imports so that upstream changes to ``run_agent.py`` do not
collide with napcat's import block.  Re-exported into ``run_agent.py``
via ``from agent.napcat_agent_instrumentation import *``.
"""

from types import SimpleNamespace
from typing import Any

from agent.memory_hooks import (
    execute_memory_tool as _execute_memory_tool_extracted,
    turn_used_tool as _turn_used_tool_fn,
    memory_tool_result_success as _memory_tool_result_success_fn,
    memory_tool_unavailable as _memory_tool_unavailable_fn,
    emit_memory_prefetch_usage as _emit_memory_prefetch_usage_fn,
    emit_auto_injection_context as _emit_auto_injection_context_fn,
    emit_memory_tool_usage as _emit_memory_tool_usage_fn,
    notify_external_memory_write as _notify_external_memory_write_fn,
)
from agent.observability_bridge import (
    DEFAULT_BEGIN_LLM_REQUEST,
    DEFAULT_CURRENT_TRACE,
    DEFAULT_END_LLM_REQUEST,
    DEFAULT_EVENT_SEVERITY,
    DEFAULT_EVENT_TYPE,
    DEFAULT_OBSERVABILITY_AVAILABLE,
    bind_trace_context_for_observability as _bridge_bind_observability_trace_context,
    begin_llm_request_observation as _begin_llm_request_observation,
    emit_final_response_event as _emit_final_response_event,
    finish_llm_request_observation as _finish_llm_request_observation,
    emit_current_observability_event as _bridge_emit_current_observability_event,
    emit_observability_event as _bridge_emit_observability_event,
    get_current_trace_ctx as _bridge_get_current_trace_ctx,
    format_usage_payload_for_log as _bridge_format_usage_payload_for_log,
    preview_text as _bridge_preview_text,
)
from agent.tool_event_instrumentation import (
    _tool_observability as _tool_observability_ctx,
    begin_tool_event as _begin_tool_event,
    emit_tool_failure as _emit_tool_failure_event,
    emit_tool_success as _emit_tool_success_event,
    suppress_tool_event_instrumentation as _suppress_tool_event_instrumentation,
)
from agent.runtime_context import AgentRuntimeContext

__all__ = [
    "AgentRuntimeContext",
    "_execute_memory_tool_extracted",
    "_turn_used_tool_fn",
    "_memory_tool_result_success_fn",
    "_memory_tool_unavailable_fn",
    "_emit_memory_prefetch_usage_fn",
    "_emit_auto_injection_context_fn",
    "_emit_memory_tool_usage_fn",
    "_notify_external_memory_write_fn",
    "_begin_llm_request_observation",
    "_emit_final_response_event",
    "_finish_llm_request_observation",
    "_tool_observability_ctx",
    "_begin_tool_event",
    "_emit_tool_failure_event",
    "_emit_tool_success_event",
    "_suppress_tool_event_instrumentation",
    "_OBSERVABILITY_ENABLED",
    "_ObservabilitySeverity",
    "_ObservabilityEventType",
    "_begin_observability_llm_request",
    "_end_observability_llm_request",
    "_current_observability_trace",
    "_default_trace_ctx",
    "_get_current_observability_trace_ctx",
    "_bind_observability_trace_context",
    "_emit_observability_event",
    "_emit_current_observability_event",
    "_preview_text",
    "_bridge_format_usage_payload_for_log",
]


# Shared observability facade used by the agent loop.
_OBSERVABILITY_ENABLED = DEFAULT_OBSERVABILITY_AVAILABLE
_ObservabilitySeverity = DEFAULT_EVENT_SEVERITY
_ObservabilityEventType = DEFAULT_EVENT_TYPE
_begin_observability_llm_request = DEFAULT_BEGIN_LLM_REQUEST
_end_observability_llm_request = DEFAULT_END_LLM_REQUEST
_current_observability_trace = DEFAULT_CURRENT_TRACE or SimpleNamespace(get=lambda: None)


def _default_trace_ctx():
    return None


def _get_current_observability_trace_ctx():
    return _bridge_get_current_trace_ctx(
        obs_available=_OBSERVABILITY_ENABLED,
        current_trace=_current_observability_trace,
        fallback_getter=_default_trace_ctx,
    )


def _bind_observability_trace_context(trace_ctx):
    return _bridge_bind_observability_trace_context(
        trace_ctx,
    )


def _emit_observability_event(event_type, payload, *, trace_ctx=None, severity=None):
    return _bridge_emit_observability_event(
        event_type,
        payload,
        trace_ctx=trace_ctx,
        severity=severity,
    )


def _emit_current_observability_event(event_type, payload, *, severity=None):
    return _bridge_emit_current_observability_event(
        event_type,
        payload,
        severity=severity,
        get_trace_ctx_fn=_get_current_observability_trace_ctx,
        emit_event_fn=_emit_observability_event,
    )


def _preview_text(value: Any, max_chars: int = 200) -> str:
    return _bridge_preview_text(value, max_chars)

