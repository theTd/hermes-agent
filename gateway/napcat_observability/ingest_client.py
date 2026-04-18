"""
NapCat observability ingest client.

Runs in the Hermes main process. Consumes events from the publisher
queue and either:
  - Writes directly to the local SQLite store (in-process mode), or
  - Sends batches to the remote observability service via HTTP.

In-process mode is the default for simplicity; remote mode is available
when the observability service runs as a separate process.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Callable, Optional

from gateway.napcat_observability.publisher import drain_queue, register_consumer
from gateway.napcat_observability.schema import (
    EventType,
    NapcatEvent,
    Severity,
)
from gateway.napcat_observability.store import ObservabilityStore

_log = logging.getLogger(__name__)

_TRACE_TERMINAL_EVENTS = {
    EventType.AGENT_RESPONSE_FINAL,
    EventType.AGENT_RESPONSE_SUPPRESSED,
    EventType.GATEWAY_TURN_SHORT_CIRCUITED,
    EventType.ORCHESTRATOR_STATUS_REPLY,
    EventType.ORCHESTRATOR_TURN_COMPLETED,
    EventType.ORCHESTRATOR_TURN_FAILED,
    EventType.ORCHESTRATOR_TURN_IGNORED,
    EventType.ORCHESTRATOR_CHILD_COMPLETED,
    EventType.ORCHESTRATOR_CHILD_CANCELLED,
    EventType.ORCHESTRATOR_CHILD_FAILED,
    EventType.ERROR_RAISED,
    EventType.GROUP_TRIGGER_REJECTED,
}

_TRACE_MAIN_COMPLETED_EVENTS = {
    EventType.AGENT_RESPONSE_FINAL,
    EventType.AGENT_RESPONSE_SUPPRESSED,
    EventType.GATEWAY_TURN_SHORT_CIRCUITED,
    EventType.ORCHESTRATOR_STATUS_REPLY,
    EventType.ORCHESTRATOR_TURN_COMPLETED,
    EventType.ORCHESTRATOR_TURN_IGNORED,
}

_TRACE_MAIN_ERROR_EVENTS = {
    EventType.ERROR_RAISED,
    EventType.ORCHESTRATOR_TURN_FAILED,
}


class IngestClient:
    """Consumes events from the publisher and persists them."""

    def __init__(
        self,
        store: ObservabilityStore,
        *,
        batch_size: int = 50,
        flush_interval: float = 1.0,
    ):
        self.store = store
        self.batch_size = batch_size
        self.flush_interval = flush_interval

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._trace_update_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._alert_listeners: list[Callable[[dict[str, Any]], None]] = []

    def start(self) -> None:
        """Start the background ingest thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="napcat-obs-ingest",
            daemon=True,
        )
        self._thread.start()
        _log.info("NapCat observability ingest client started")

    def stop(self) -> None:
        """Stop the ingest thread, flushing remaining events."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

        # Final drain
        self._flush()
        _log.info("NapCat observability ingest client stopped")

    def add_trace_update_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._trace_update_listeners.append(listener)

    def add_alert_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._alert_listeners.append(listener)

    def _run_loop(self) -> None:
        """Background loop that periodically drains the queue."""
        while self._running:
            try:
                self._flush()
            except Exception:
                _log.debug("Ingest flush error", exc_info=True)

            time.sleep(self.flush_interval)

    def _flush(self) -> None:
        """Drain events from the queue and write to store."""
        events = drain_queue(max_items=self.batch_size)
        if not events:
            return

        event_dicts = [e.to_dict() for e in events]

        try:
            count = self.store.insert_events_batch(event_dicts)
            if count:
                _log.debug("Ingested %d events", count)
        except Exception:
            _log.warning("Failed to write events batch", exc_info=True)

        # Update trace records
        self._update_traces(events)

        # Check for alerts
        self._check_alerts(events)

    def _update_traces(self, events: list[NapcatEvent]) -> None:
        """Update trace summary records based on new events."""
        traces_seen: dict[str, list[NapcatEvent]] = {}
        for event in events:
            if event.trace_id:
                traces_seen.setdefault(event.trace_id, []).append(event)

        for trace_id, trace_events in traces_seen.items():
            try:
                trace_events = self._load_trace_events(trace_id)
                if not trace_events:
                    continue

                first = trace_events[0]
                trace_context = self._resolve_trace_context(trace_events)
                trace_analysis = self._analyze_trace_status(trace_events)
                status = str(trace_analysis["status"])
                ended_at = trace_analysis["ended_at"]

                trace_dict = {
                    "trace_id": trace_id,
                    "session_key": trace_context["session_key"],
                    "session_id": trace_context["session_id"],
                    "chat_type": trace_context["chat_type"],
                    "chat_id": trace_context["chat_id"],
                    "user_id": trace_context["user_id"],
                    "message_id": trace_context["message_id"],
                    "started_at": first.ts,
                    "ended_at": ended_at,
                    "event_count": len(trace_events),
                    "status": status,
                }
                trace_dict.update(self._build_trace_summary(trace_events, trace_analysis=trace_analysis))
                self.store.upsert_trace(trace_dict)
                for listener in self._trace_update_listeners:
                    try:
                        listener(trace_dict)
                    except Exception:
                        _log.debug("Trace update listener failed", exc_info=True)
            except Exception:
                _log.debug("Failed to update trace %s", trace_id, exc_info=True)

    def _resolve_trace_context(self, events: list[NapcatEvent]) -> dict[str, str]:
        """Pick the first non-empty routing identifiers across a trace's events."""
        resolved = {
            "session_key": "",
            "session_id": "",
            "chat_type": "",
            "chat_id": "",
            "user_id": "",
            "message_id": "",
        }
        if not events:
            return resolved

        for event in events:
            for field in resolved:
                if resolved[field]:
                    continue
                value = str(getattr(event, field, "") or "").strip()
                if value:
                    resolved[field] = value

            if all(resolved.values()):
                break

        return resolved

    def _load_trace_events(self, trace_id: str) -> list[NapcatEvent]:
        rows = self.store.get_events_for_trace(trace_id)
        events: list[NapcatEvent] = []
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            events.append(NapcatEvent.from_dict({**dict(row), "payload": payload}))
        return events

    def _analyze_trace_status(self, events: list[NapcatEvent]) -> dict[str, Any]:
        if not events:
            return {
                "status": "in_progress",
                "ended_at": None,
                "main_outcome": "",
                "child_counts": {
                    "started": 0,
                    "reused": 0,
                    "completed": 0,
                    "cancelled": 0,
                    "failed": 0,
                    "active": 0,
                },
                "last_terminal_event": "",
            }

        child_statuses: dict[str, str] = {}
        child_reused = 0
        main_outcome = ""
        main_outcome_ts: float | None = None
        rejected_ts: float | None = None
        terminal_events: list[NapcatEvent] = []

        for event in events:
            payload = event.payload if isinstance(event.payload, dict) else {}
            if event.event_type in {
                EventType.ORCHESTRATOR_CHILD_QUEUED,
                EventType.ORCHESTRATOR_CHILD_SPAWNED,
            }:
                child_id = str(payload.get("child_id") or "").strip()
                if child_id and child_id not in child_statuses:
                    child_statuses[child_id] = "active"
            elif event.event_type == EventType.ORCHESTRATOR_CHILD_REUSED:
                child_reused += 1
            elif event.event_type == EventType.ORCHESTRATOR_CHILD_COMPLETED:
                child_id = str(payload.get("child_id") or "").strip()
                if child_id:
                    child_statuses[child_id] = "completed"
            elif event.event_type == EventType.ORCHESTRATOR_CHILD_CANCELLED:
                child_id = str(payload.get("child_id") or "").strip()
                if child_id:
                    child_statuses[child_id] = "cancelled"
            elif event.event_type == EventType.ORCHESTRATOR_CHILD_FAILED:
                child_id = str(payload.get("child_id") or "").strip()
                if child_id:
                    child_statuses[child_id] = "failed"

            if event.event_type == EventType.GROUP_TRIGGER_REJECTED and rejected_ts is None:
                rejected_ts = event.ts

            if event.event_type in _TRACE_MAIN_COMPLETED_EVENTS:
                main_outcome = "completed"
                main_outcome_ts = event.ts
            elif event.event_type in _TRACE_MAIN_ERROR_EVENTS:
                main_outcome = "error"
                main_outcome_ts = event.ts

            if event.event_type in _TRACE_TERMINAL_EVENTS:
                terminal_events.append(event)

        child_counts = {
            "started": len(child_statuses),
            "reused": child_reused,
            "completed": sum(1 for state in child_statuses.values() if state == "completed"),
            "cancelled": sum(1 for state in child_statuses.values() if state == "cancelled"),
            "failed": sum(1 for state in child_statuses.values() if state == "failed"),
            "active": sum(1 for state in child_statuses.values() if state == "active"),
        }

        status = "in_progress"
        if child_counts["active"] > 0:
            status = "in_progress"
        elif main_outcome == "error":
            status = "error"
        elif child_counts["failed"] > 0:
            status = "error"
        elif main_outcome == "completed":
            status = "completed"
        elif child_counts["started"] > 0:
            status = "completed"
        elif rejected_ts is not None:
            status = "rejected"

        ended_at = None
        last_terminal_event = ""
        if status != "in_progress" and terminal_events:
            last_terminal = max(terminal_events, key=lambda item: (item.ts, item.event_id))
            ended_at = last_terminal.ts
            last_terminal_event = last_terminal.event_type.value

        return {
            "status": status,
            "ended_at": ended_at,
            "main_outcome": main_outcome,
            "child_counts": child_counts,
            "last_terminal_event": last_terminal_event,
        }

    def _build_trace_summary(self, events: list[NapcatEvent], *, trace_analysis: dict[str, Any] | None = None) -> dict:
        """Build a summary dict for a trace from its events."""
        event_types = [e.event_type.value for e in events]
        has_error = any(e.severity == Severity.ERROR for e in events)
        tools_used = []
        tool_call_ids: set[str] = set()
        model = ""
        error_count = 0
        latest_stage = "raw"
        active_stream = False
        response_preview = ""
        error_preview = ""
        child_result_preview = ""
        child_error_preview = ""
        child_cancel_preview = ""
        status_reply_preview = ""
        inbound_preview = ""
        chat_name = ""
        main_outcome = str((trace_analysis or {}).get("main_outcome") or "")
        child_counts = dict((trace_analysis or {}).get("child_counts") or {})
        last_terminal_event = str((trace_analysis or {}).get("last_terminal_event") or "")
        stage_priority = {
            "raw": 0,
            "inbound": 1,
            "trigger": 2,
            "context": 3,
            "model": 4,
            "tools": 5,
            "memory_skill_routing": 6,
            "final": 7,
            "error": 8,
        }

        for e in events:
            if e.event_type == EventType.AGENT_TOOL_CALLED:
                tool_name = e.payload.get("tool_name", "")
                if tool_name and tool_name not in tools_used:
                    tools_used.append(tool_name)
                tool_call_id = str(e.payload.get("tool_call_id", "")).strip()
                tool_call_ids.add(tool_call_id or f"{tool_name}:{e.ts}:{e.trace_id}")
            if e.event_type in (
                EventType.ERROR_RAISED,
                EventType.ORCHESTRATOR_TURN_FAILED,
                EventType.ORCHESTRATOR_CHILD_FAILED,
            ):
                error_count += 1
            if not model and e.payload.get("model"):
                model = str(e.payload.get("model") or "")
            stage = str(e.payload.get("stage") or "")
            if stage_priority.get(stage, 0) >= stage_priority.get(latest_stage, 0):
                latest_stage = stage or latest_stage
            if e.event_type in (EventType.AGENT_REASONING_DELTA, EventType.AGENT_RESPONSE_DELTA):
                active_stream = True
            if e.event_type in (
                EventType.AGENT_RESPONSE_FINAL,
                EventType.AGENT_RESPONSE_SUPPRESSED,
                EventType.GATEWAY_TURN_SHORT_CIRCUITED,
                EventType.ORCHESTRATOR_STATUS_REPLY,
                EventType.ORCHESTRATOR_TURN_COMPLETED,
                EventType.ORCHESTRATOR_TURN_IGNORED,
                EventType.ERROR_RAISED,
                EventType.ORCHESTRATOR_CHILD_COMPLETED,
                EventType.ORCHESTRATOR_CHILD_CANCELLED,
                EventType.ORCHESTRATOR_CHILD_FAILED,
            ):
                active_stream = False
            if not inbound_preview and e.event_type == EventType.MESSAGE_RECEIVED:
                inbound_preview = str(
                    e.payload.get("text_preview")
                    or e.payload.get("text")
                    or e.payload.get("raw_message")
                    or ""
                )[:240]
            if not chat_name:
                chat_name = str(
                    e.payload.get("chat_name")
                    or e.payload.get("group_name")
                    or ""
                ).strip()[:240]
            if e.event_type == EventType.AGENT_RESPONSE_FINAL:
                response_preview = str(
                    e.payload.get("response_preview")
                    or e.payload.get("response")
                    or response_preview
                )[:240]
            if e.event_type == EventType.ORCHESTRATOR_TURN_COMPLETED:
                response_preview = str(
                    e.payload.get("response_preview")
                    or e.payload.get("reply_text")
                    or response_preview
                )[:240]
            if e.event_type == EventType.ORCHESTRATOR_CHILD_COMPLETED:
                child_result_preview = str(
                    e.payload.get("result_preview")
                    or e.payload.get("summary")
                    or child_result_preview
                )[:240]
            if e.event_type == EventType.ORCHESTRATOR_STATUS_REPLY:
                status_reply_preview = str(
                    e.payload.get("reply_text")
                    or e.payload.get("response_preview")
                    or status_reply_preview
                )[:240]
            if e.event_type == EventType.ORCHESTRATOR_CHILD_FAILED:
                child_error_preview = str(
                    e.payload.get("error")
                    or e.payload.get("result_preview")
                    or child_error_preview
                )[:240]
            if e.event_type == EventType.ORCHESTRATOR_CHILD_CANCELLED:
                child_cancel_preview = str(
                    e.payload.get("reason")
                    or e.payload.get("summary")
                    or child_cancel_preview
                )[:240]
            if e.event_type == EventType.ERROR_RAISED:
                error_preview = str(
                    e.payload.get("error_message")
                    or e.payload.get("error")
                    or error_preview
                )[:240]

        return {
            "latest_stage": latest_stage,
            "active_stream": active_stream,
            "tool_call_count": len(tool_call_ids),
            "error_count": error_count,
            "model": model,
            "summary": {
                "event_types": event_types,
                "has_error": has_error,
                "tools_used": tools_used,
                "controller_outcome": main_outcome,
                "child_counts": {
                    "started": int(child_counts.get("started", 0) or 0),
                    "reused": int(child_counts.get("reused", 0) or 0),
                    "completed": int(child_counts.get("completed", 0) or 0),
                    "cancelled": int(child_counts.get("cancelled", 0) or 0),
                    "failed": int(child_counts.get("failed", 0) or 0),
                    "active": int(child_counts.get("active", 0) or 0),
                },
                "chat_name": chat_name,
                "last_terminal_event": last_terminal_event,
                "headline": (
                    child_error_preview
                    or child_result_preview
                    or response_preview
                    or error_preview
                    or status_reply_preview
                    or child_cancel_preview
                    or inbound_preview
                ),
            },
        }

    def _check_alerts(self, events: list[NapcatEvent]) -> None:
        """Generate alerts from high-severity events."""
        for event in events:
            if event.severity == Severity.ERROR:
                try:
                    alert = {
                        "alert_id": uuid.uuid4().hex,
                        "trace_id": event.trace_id,
                        "event_id": event.event_id,
                        "ts": event.ts,
                        "severity": event.severity.value,
                        "title": f"{event.event_type.value}: {event.payload.get('error', 'Unknown error')[:200]}",
                        "detail": str(event.payload)[:2000],
                    }
                    self.store.insert_alert(alert)
                    for listener in self._alert_listeners:
                        try:
                            listener(alert)
                        except Exception:
                            _log.debug("Alert listener failed", exc_info=True)
                except Exception:
                    _log.debug("Failed to create alert", exc_info=True)
