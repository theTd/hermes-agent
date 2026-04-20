"""Stable gateway agent-run hook helpers."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from agent.runtime_context import AgentRuntimeContext
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewayAgentRunHooks:
    """Normalized callbacks threaded through ``GatewayRunner._run_agent``."""

    trace_ctx: Optional[Any] = None
    progress_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None
    runtime_callback: Optional[Callable[[Any, Optional[str]], None]] = None

    @classmethod
    def coerce(
        cls,
        runtime_hooks: Optional[Any] = None,
        *,
        trace_ctx: Optional[Any] = None,
        progress_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        runtime_callback: Optional[Callable[[Any, Optional[str]], None]] = None,
    ) -> "GatewayAgentRunHooks":
        """Normalize legacy run-hook arguments into a stable container."""
        if isinstance(runtime_hooks, cls):
            return cls(
                trace_ctx=runtime_hooks.trace_ctx if runtime_hooks.trace_ctx is not None else trace_ctx,
                progress_callback=runtime_hooks.progress_callback or progress_callback,
                runtime_callback=runtime_hooks.runtime_callback or runtime_callback,
            )
        if runtime_hooks is None:
            return cls(
                trace_ctx=trace_ctx,
                progress_callback=progress_callback,
                runtime_callback=runtime_callback,
            )
        if isinstance(runtime_hooks, dict):
            return cls(
                trace_ctx=runtime_hooks.get("trace_ctx", trace_ctx),
                progress_callback=runtime_hooks.get("progress_callback") or progress_callback,
                runtime_callback=runtime_hooks.get("runtime_callback") or runtime_callback,
            )
        return cls(
            trace_ctx=getattr(runtime_hooks, "trace_ctx", trace_ctx),
            progress_callback=getattr(runtime_hooks, "progress_callback", None) or progress_callback,
            runtime_callback=getattr(runtime_hooks, "runtime_callback", None) or runtime_callback,
        )

    def with_trace_ctx(self, trace_ctx: Optional[Any]) -> "GatewayAgentRunHooks":
        """Return a copy with an updated trace context."""
        return type(self)(
            trace_ctx=trace_ctx,
            progress_callback=self.progress_callback,
            runtime_callback=self.runtime_callback,
        )

    def with_trace_metadata(
        self,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        metadata_key: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Attach the active trace context to adapter metadata."""
        merged = dict(metadata or {})
        if self.trace_ctx is not None and metadata_key:
            merged.setdefault(metadata_key, self.trace_ctx)
        return merged or None

    def notify_progress(self, kind: str, payload: Dict[str, Any]) -> None:
        """Invoke the normalized progress callback when present."""
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(kind, payload)
        except Exception:
            logger.debug("gateway run hook progress callback error", exc_info=True)

    def notify_runtime(self, agent: Any, runtime_session_key: Optional[str]) -> None:
        """Invoke the normalized runtime callback when present."""
        if self.runtime_callback is None:
            return
        try:
            self.runtime_callback(agent, runtime_session_key)
        except Exception:
            logger.debug("gateway run hook runtime callback error", exc_info=True)


class GatewayLiveObservabilityBuffer:
    """Aggregate live reasoning/response deltas before emitting trace events."""

    def __init__(
        self,
        *,
        trace_ctx: Optional[Any],
        event_type_cls: Optional[Any],
        emit_event: Callable[..., Any],
        emit_interval: float = 0.25,
        min_chars_between_emits: int = 160,
    ) -> None:
        self.trace_ctx = trace_ctx
        self.event_type_cls = event_type_cls
        self.emit_event = emit_event
        self.emit_interval = float(emit_interval)
        self.min_chars_between_emits = int(min_chars_between_emits)
        self._lock = threading.Lock()
        self._state = {
            "reasoning": {"content": "", "last_emit": 0.0, "last_emit_len": 0, "sequence": 0},
            "response": {"content": "", "last_emit": 0.0, "last_emit_len": 0, "sequence": 0},
        }

    def has_trace(self) -> bool:
        return bool(self.trace_ctx and self.event_type_cls)

    def emit_delta(self, kind: str, text: Optional[str], *, force: bool = False) -> None:
        """Emit an aggregated reasoning/response delta when enough text accumulates."""
        if not self.has_trace():
            return

        if kind == "reasoning":
            event_type = getattr(self.event_type_cls, "AGENT_REASONING_DELTA", None)
        elif kind == "response":
            event_type = getattr(self.event_type_cls, "AGENT_RESPONSE_DELTA", None)
        else:
            return
        if not event_type:
            return

        delta = "" if text is None else str(text)
        if not delta and not force:
            return

        with self._lock:
            state = self._state[kind]
            if delta:
                state["content"] += delta
            content = state["content"]
            if not content:
                return

            now = time.monotonic()
            changed = len(content) != int(state["last_emit_len"])
            pending_delta = content[int(state["last_emit_len"]):]
            should_emit = force or (
                changed and (
                    now - float(state["last_emit"]) >= self.emit_interval
                    or len(content) - int(state["last_emit_len"]) >= self.min_chars_between_emits
                    or "\n" in delta
                )
            )
            if not should_emit:
                return
            if not pending_delta:
                return

            state["last_emit"] = now
            state["last_emit_len"] = len(content)
            state["sequence"] = int(state["sequence"]) + 1
            sequence = int(state["sequence"])

        try:
            self.emit_event(
                event_type,
                {
                    "delta": pending_delta,
                    "chars": len(content),
                    "delta_chars": len(pending_delta),
                    "sequence": sequence,
                },
                trace_ctx=self.trace_ctx,
            )
        except Exception:
            logger.debug("Gateway live observability emit failed", exc_info=True)

    def flush(self, final_response: Optional[str] = None) -> None:
        """Force-flush aggregated reasoning/response buffers."""
        if final_response:
            with self._lock:
                response_state = self._state["response"]
                if not response_state["content"]:
                    response_state["content"] = str(final_response)
        self.emit_delta("reasoning", None, force=True)
        self.emit_delta("response", None, force=True)


def _build_agent_runtime_context_kwargs(
    run_agent_module: Any,
    *,
    source: Optional[Any] = None,
    session_key: Optional[str] = None,
    agent_context: str = "primary",
) -> Dict[str, Any]:
    """Build ``AIAgent`` runtime_context kwargs when the runtime supports them."""
    return AgentRuntimeContext.from_source(
        source,
        gateway_session_key=session_key,
        agent_context=agent_context,
    ).to_agent_kwargs()


__all__ = [
    "GatewayAgentRunHooks",
    "GatewayLiveObservabilityBuffer",
    "_build_agent_runtime_context_kwargs",
]
