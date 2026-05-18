"""
NapCat observability event publisher.

Provides `emit_napcat_event()` — the public API for instrumenting the
NapCat processing chain.  Events are placed on a bounded in-memory queue
for asynchronous consumption by the ingest layer.  The publisher is
best-effort: it never raises exceptions to callers and silently drops
events when the system is disabled or the queue is full.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Dict, List, Optional

from gateway.napcat_observability.schema import (
    EventPriority,
    EventType,
    NapcatEvent,
    Severity,
    default_severity,
    event_stage,
    event_priority,
    truncate_payload,
)
from gateway.napcat_observability.trace import NapcatTraceContext

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton state
# ---------------------------------------------------------------------------

_enabled: bool = False
_queue: Optional[queue.Queue] = None
_queue_size: int = 10000
_max_stdout_chars: int = 8000
_max_stderr_chars: int = 4000

# Counters for observability of the observability system
_drop_count: int = 0
_emit_count: int = 0
_lock = threading.Lock()

# Optional callback for consumers (ingest client or direct store)
_consumers: List[Callable[[NapcatEvent], None]] = []


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def init_publisher(
    *,
    enabled: bool = False,
    queue_size: int = 10000,
    max_stdout_chars: int = 8000,
    max_stderr_chars: int = 4000,
) -> None:
    """Initialize the publisher from config.

    Call once at startup. Safe to call multiple times (idempotent).
    If configuration has not changed and a queue already exists, it is preserved.
    """
    global _enabled, _queue, _queue_size, _max_stdout_chars, _max_stderr_chars

    with _lock:
        _enabled = enabled
        _queue_size = queue_size
        _max_stdout_chars = max_stdout_chars
        _max_stderr_chars = max_stderr_chars

        if enabled:
            if _queue is None:
                _queue = queue.Queue(maxsize=queue_size)
                _log.info("NapCat observability publisher initialized (queue_size=%d)", queue_size)
            else:
                _log.debug("NapCat observability publisher already initialized; preserving existing queue")
        else:
            _queue = None
            _log.debug("NapCat observability publisher disabled")


def shutdown_publisher() -> None:
    """Shutdown the publisher, draining remaining events."""
    global _enabled, _queue
    with _lock:
        _enabled = False
        _queue = None
    _log.debug("NapCat observability publisher shut down")


def register_consumer(callback: Callable[[NapcatEvent], None]) -> None:
    """Register a consumer callback for events.

    Consumers are called synchronously when an event is emitted.
    They should be lightweight — heavy processing should be offloaded
    to a background thread.
    """
    _consumers.append(callback)


def unregister_consumer(callback: Callable[[NapcatEvent], None]) -> None:
    """Remove a consumer callback."""
    try:
        _consumers.remove(callback)
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def get_stats() -> Dict[str, Any]:
    """Return publisher statistics."""
    with _lock:
        return {
            "enabled": _enabled,
            "emit_count": _emit_count,
            "drop_count": _drop_count,
            "queue_size": _queue.qsize() if _queue else 0,
            "queue_capacity": _queue_size,
        }


# ---------------------------------------------------------------------------
# Public emit API
# ---------------------------------------------------------------------------

def emit_napcat_event(
    event_type: str | EventType,
    payload: Dict[str, Any],
    *,
    trace_ctx: Optional[NapcatTraceContext] = None,
    severity: Optional[Severity] = None,
    span_id: str = "",
    parent_span_id: str = "",
) -> Optional[NapcatEvent]:
    """Publish a NapCat observability event.

    This is the primary instrumentation API.  It is best-effort:
    - Returns None silently when disabled.
    - Drops low-priority events when the queue is full.
    - Never raises exceptions to the caller.

    Parameters
    ----------
    event_type : str or EventType
        The event type identifier.
    payload : dict
        Event-specific data.  Large fields are auto-truncated.
    trace_ctx : NapcatTraceContext, optional
        Trace correlation context.  If None, a detached event is created.
    severity : Severity, optional
        Override the default severity for this event type.
    span_id : str, optional
        Override the auto-generated span ID.
    parent_span_id : str, optional
        Explicit parent span (usually inherited from trace_ctx).

    Returns
    -------
    NapcatEvent or None
        The created event, or None if disabled/dropped.
    """
    global _emit_count, _drop_count

    if not _enabled:
        return None

    try:
        if isinstance(event_type, str):
            event_type = EventType(event_type)

        # Add stable stage metadata before truncation so clients can rely on it.
        payload = dict(payload)
        payload.setdefault("stage", event_stage(event_type))

        # Truncate large payload fields
        payload = truncate_payload(
            payload,
            max_stdout_chars=_max_stdout_chars,
            max_stderr_chars=_max_stderr_chars,
        )

        # Build event
        event = NapcatEvent(
            event_type=event_type,
            payload=payload,
            severity=severity or default_severity(event_type),
            span_id=span_id if span_id else "",
            parent_span_id=parent_span_id,
        )

        timing = event.payload.get("timing")
        if not isinstance(timing, dict):
            timing = {}
        timing.setdefault("observability_emitted_at", event.ts)
        event.payload["timing"] = timing

        # Apply trace context
        if trace_ctx:
            event.trace_id = trace_ctx.trace_id
            if not span_id:
                event.span_id = trace_ctx.span_id
            if not parent_span_id:
                event.parent_span_id = trace_ctx.parent_span_id
            event.session_key = trace_ctx.session_key
            event.session_id = trace_ctx.session_id
            event.chat_type = trace_ctx.chat_type
            event.chat_id = trace_ctx.chat_id
            event.user_id = trace_ctx.user_id
            event.message_id = trace_ctx.message_id
            if getattr(trace_ctx, "agent_scope", ""):
                event.payload.setdefault("agent_scope", trace_ctx.agent_scope)
            if getattr(trace_ctx, "child_id", ""):
                event.payload.setdefault("child_id", trace_ctx.child_id)
            if getattr(trace_ctx, "llm_request_id", ""):
                event.payload.setdefault("llm_request_id", trace_ctx.llm_request_id)

        # Try to enqueue
        if _queue is not None:
            try:
                _queue.put_nowait(event)
                with _lock:
                    _emit_count += 1
            except queue.Full:
                # Drop policy: try to make room by removing low-priority events
                if event.priority >= EventPriority.HIGH:
                    if _try_evict_low_priority(event):
                        with _lock:
                            _emit_count += 1
                    else:
                        with _lock:
                            _drop_count += 1
                        return None
                else:
                    with _lock:
                        _drop_count += 1
                    _log.debug(
                        "Dropped %s event (queue full, priority=%s)",
                        event.event_type.value,
                        event.priority.name,
                    )
                    return None

        # Notify consumers
        for consumer in _consumers:
            try:
                consumer(event)
            except Exception:
                _log.debug("Consumer callback failed", exc_info=True)

        return event

    except ValueError as exc:
        _log.warning("Failed to emit NapCat event (invalid value): %s", exc, exc_info=True)
        return None
    except Exception:
        _log.debug("Failed to emit NapCat event", exc_info=True)
        return None


def _try_evict_low_priority(high_priority_event: NapcatEvent) -> bool:
    """Attempt to evict a low-priority event to make room for a high-priority one.

    This is a best-effort operation — under high contention it may fail.
    The entire drain-filter-re-enqueue sequence is protected by the module lock.
    """
    if _queue is None:
        return False

    try:
        with _lock:
            # Drain and re-enqueue, skipping one low-priority event
            temp: List[NapcatEvent] = []
            evicted = False

            while not _queue.empty():
                try:
                    evt = _queue.get_nowait()
                    if not evicted and evt.priority <= EventPriority.LOW:
                        evicted = True  # drop this one
                        global _drop_count
                        _drop_count += 1
                    else:
                        temp.append(evt)
                except queue.Empty:
                    break

            # Re-enqueue
            for evt in temp:
                try:
                    _queue.put_nowait(evt)
                except queue.Full:
                    break

            # Now try to enqueue the high-priority event
            if evicted:
                try:
                    _queue.put_nowait(high_priority_event)
                    return True
                except queue.Full:
                    pass

            return False

    except Exception:
        return False


def drain_queue(max_items: int = 0) -> List[NapcatEvent]:
    """Drain up to max_items events from the queue.

    If max_items is 0, drain all available events.
    Used by the ingest client for batch sending.
    """
    if _queue is None:
        return []

    events: List[NapcatEvent] = []
    limit = max_items or _queue_size

    with _lock:
        for _ in range(limit):
            try:
                events.append(_queue.get_nowait())
            except queue.Empty:
                break

    return events
