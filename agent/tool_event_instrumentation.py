"""Shared tool-call instrumentation helpers for agent and dispatcher paths."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import time
from typing import Any, Callable, Dict, Optional

from agent.observability_bridge import (
    DEFAULT_CURRENT_TRACE,
    DEFAULT_EVENT_SEVERITY,
    DEFAULT_EVENT_TYPE,
    emit_observability_event,
    get_current_trace_ctx as _bridge_get_current_trace_ctx,
)

_SUPPRESS_TOOL_EVENT_INSTRUMENTATION: ContextVar[bool] = ContextVar(
    "suppress_tool_event_instrumentation",
    default=False,
)


def _default_trace_context() -> Optional[Any]:
    """Return the currently bound generic observability trace context."""
    return _bridge_get_current_trace_ctx(
        obs_available=bool(DEFAULT_EVENT_TYPE),
        current_trace=DEFAULT_CURRENT_TRACE,
        fallback_getter=lambda: None,
    )


@dataclass(frozen=True)
class ToolEventContext:
    """Context captured at tool-call start for later completion/failure events."""

    function_name: str
    function_args: Dict[str, Any]
    tool_call_id: str
    trace_ctx: Any
    started_at: float


def begin_tool_event(
    function_name: str,
    function_args: Dict[str, Any],
    *,
    tool_call_id: Optional[str] = None,
    emit_observability: bool = True,
    trace_ctx: Optional[Any] = None,
    get_trace_context_fn: Callable[[], Optional[Any]] = _default_trace_context,
    emit_event_fn: Callable[..., bool] = emit_observability_event,
    event_type_cls: Any = DEFAULT_EVENT_TYPE,
) -> Optional[ToolEventContext]:
    """Emit tool-called and return context for completion/failure events."""
    if _SUPPRESS_TOOL_EVENT_INSTRUMENTATION.get():
        return None
    active_trace = trace_ctx if trace_ctx is not None else get_trace_context_fn()
    if not emit_observability or active_trace is None or not event_type_cls:
        return None
    ctx = ToolEventContext(
        function_name=function_name,
        function_args=function_args,
        tool_call_id=str(tool_call_id or ""),
        trace_ctx=active_trace,
        started_at=time.time(),
    )
    try:
        emit_event_fn(
            getattr(event_type_cls, "AGENT_TOOL_CALLED", None),
            {
                "tool_name": ctx.function_name,
                "tool_call_id": ctx.tool_call_id,
                "args_preview": str(ctx.function_args)[:300],
            },
            trace_ctx=ctx.trace_ctx,
        )
    except Exception:
        pass
    return ctx


def emit_tool_success(
    ctx: Optional[ToolEventContext],
    result: Any,
    *,
    emit_event_fn: Callable[..., bool] = emit_observability_event,
    event_type_cls: Any = DEFAULT_EVENT_TYPE,
    emit_semantic_events: bool = False,
) -> None:
    """Emit tool-completed and optional semantic tool events."""
    if ctx is None or not event_type_cls:
        return
    try:
        payload: Dict[str, Any] = {
            "tool_name": ctx.function_name,
            "tool_call_id": ctx.tool_call_id,
            "duration_ms": (time.time() - ctx.started_at) * 1000,
            "success": True,
            "result_preview": str(result)[:200],
        }
        try:
            result_data = json.loads(result) if isinstance(result, str) else result
            if isinstance(result_data, dict):
                if "command" in result_data:
                    payload["command"] = str(result_data["command"])[:500]
                if "exit_code" in result_data:
                    payload["exit_code"] = result_data["exit_code"]
                if "stdout" in result_data:
                    payload["stdout_preview"] = str(result_data["stdout"])[:500]
                if "stderr" in result_data:
                    payload["stderr_preview"] = str(result_data["stderr"])[:500]
        except (json.JSONDecodeError, TypeError):
            pass
        emit_event_fn(
            getattr(event_type_cls, "AGENT_TOOL_COMPLETED", None),
            payload,
            trace_ctx=ctx.trace_ctx,
        )
        if emit_semantic_events:
            _emit_semantic_tool_events(
                ctx,
                result,
                emit_event_fn=emit_event_fn,
                event_type_cls=event_type_cls,
            )
    except Exception:
        pass


def emit_tool_failure(
    ctx: Optional[ToolEventContext],
    error: Exception,
    *,
    emit_event_fn: Callable[..., bool] = emit_observability_event,
    event_type_cls: Any = DEFAULT_EVENT_TYPE,
    severity_cls: Any = DEFAULT_EVENT_SEVERITY,
) -> None:
    """Emit tool-failed for a previously started context."""
    if ctx is None or not event_type_cls:
        return
    try:
        emit_event_fn(
            getattr(event_type_cls, "AGENT_TOOL_FAILED", None),
            {
                "tool_name": ctx.function_name,
                "tool_call_id": ctx.tool_call_id,
                "duration_ms": (time.time() - ctx.started_at) * 1000,
                "error": str(error)[:500],
            },
            trace_ctx=ctx.trace_ctx,
            severity=getattr(severity_cls, "WARN", None),
        )
    except Exception:
        pass


@contextmanager
def suppress_tool_event_instrumentation():
    """Temporarily disable dispatcher-level tool event emission."""
    token = _SUPPRESS_TOOL_EVENT_INSTRUMENTATION.set(True)
    try:
        yield
    finally:
        _SUPPRESS_TOOL_EVENT_INSTRUMENTATION.reset(token)


@contextmanager
def _tool_observability(
    function_name: str,
    function_args: Dict[str, Any],
    *,
    tool_call_id: Optional[str] = None,
    trace_ctx: Optional[Any] = None,
    get_trace_context_fn: Callable[[], Optional[Any]] = _default_trace_context,
    emit_event_fn: Callable[..., bool] = emit_observability_event,
    event_type_cls: Any = DEFAULT_EVENT_TYPE,
    severity_cls: Any = DEFAULT_EVENT_SEVERITY,
):
    """Context manager that emits tool-called on entry and tool-failed on exception.

    Yields the ToolEventContext so callers can call emit_tool_success after
    the with-block body completes (the result is only available then).
    """
    ctx = begin_tool_event(
        function_name,
        function_args,
        tool_call_id=tool_call_id,
        trace_ctx=trace_ctx,
        get_trace_context_fn=get_trace_context_fn,
        emit_event_fn=emit_event_fn,
        event_type_cls=event_type_cls,
    )
    try:
        yield ctx
    except Exception as exc:
        emit_tool_failure(
            ctx,
            exc,
            emit_event_fn=emit_event_fn,
            event_type_cls=event_type_cls,
            severity_cls=severity_cls,
        )
        raise


def _emit_semantic_tool_events(
    ctx: ToolEventContext,
    result: Any,
    *,
    emit_event_fn: Callable[..., bool],
    event_type_cls: Any,
) -> None:
    duration_ms = (time.time() - ctx.started_at) * 1000
    if ctx.function_name in {"memory", "session_search"}:
        emit_event_fn(
            getattr(event_type_cls, "AGENT_MEMORY_USED", None),
            {
                "tool_call_id": ctx.tool_call_id,
                "source": ctx.function_name,
                "type": ctx.function_args.get("target", "memory"),
                "summary": str(ctx.function_args.get("action") or ctx.function_name)[:120],
                "preview": str(result)[:200],
                "duration_ms": duration_ms,
            },
            trace_ctx=ctx.trace_ctx,
        )
    elif ctx.function_name in {"skill_view", "skills_list", "skill_manage"}:
        emit_event_fn(
            getattr(event_type_cls, "AGENT_SKILL_USED", None),
            {
                "tool_call_id": ctx.tool_call_id,
                "skill_name": str(
                    ctx.function_args.get("name") or ctx.function_args.get("skill_name") or ""
                ),
                "resolution": ctx.function_name,
                "source": "tool",
                "preview": str(result)[:200],
                "duration_ms": duration_ms,
            },
            trace_ctx=ctx.trace_ctx,
        )


# ---------------------------------------------------------------------------
# Hook-based instrumentation (replaces inline calls in model_tools.py)
# ---------------------------------------------------------------------------

_PENDING_TOOL_CONTEXTS: dict[str, ToolEventContext] = {}


def _on_pre_tool_call(
    *, tool_name: str, args: dict, task_id: str = "",
    session_id: str = "", tool_call_id: str = "", **_kw: Any,
) -> None:
    ctx = begin_tool_event(tool_name, args, tool_call_id=tool_call_id)
    if ctx:
        _PENDING_TOOL_CONTEXTS[tool_call_id] = ctx


def _on_post_tool_call(
    *, tool_name: str, args: dict, result: Any, task_id: str = "",
    session_id: str = "", tool_call_id: str = "", **_kw: Any,
) -> None:
    ctx = _PENDING_TOOL_CONTEXTS.pop(tool_call_id, None)
    if ctx:
        emit_tool_success(ctx, result, emit_semantic_events=True)


def pop_and_emit_failure(tool_call_id: str, error: Exception) -> None:
    """Pop a pending tool event context and emit failure event."""
    ctx = _PENDING_TOOL_CONTEXTS.pop(tool_call_id, None)
    if ctx:
        emit_tool_failure(ctx, error)


def install_tool_event_hooks() -> None:
    """Register pre/post_tool_call hooks for observability instrumentation."""
    try:
        from hermes_cli.plugins import get_plugin_manager
        pm = get_plugin_manager()
        pm._hooks.setdefault("pre_tool_call", []).append(_on_pre_tool_call)
        pm._hooks.setdefault("post_tool_call", []).append(_on_post_tool_call)
    except Exception:
        pass
