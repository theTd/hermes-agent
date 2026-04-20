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
    EventType.GROUP_CONTEXT_ONLY_EMITTED,
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

_TRACE_REPLY_EVENTS = {
    EventType.AGENT_RESPONSE_FINAL,
    EventType.GATEWAY_TURN_SHORT_CIRCUITED,
    EventType.ORCHESTRATOR_STATUS_REPLY,
    EventType.ORCHESTRATOR_TURN_COMPLETED,
}

_RESPONSE_SEND_ACTIONS = {
    "send_group_msg",
    "send_private_msg",
}

_TIMING_CHILD_ACTIVITY_EVENTS = {
    EventType.ORCHESTRATOR_CHILD_QUEUED,
    EventType.ORCHESTRATOR_CHILD_SPAWNED,
    EventType.ORCHESTRATOR_CHILD_REUSED,
    EventType.ORCHESTRATOR_CHILD_COMPLETED,
    EventType.ORCHESTRATOR_CHILD_CANCELLED,
    EventType.ORCHESTRATOR_CHILD_FAILED,
}

_TIMING_CHILD_TERMINAL_EVENTS = {
    EventType.ORCHESTRATOR_CHILD_COMPLETED,
    EventType.ORCHESTRATOR_CHILD_CANCELLED,
    EventType.ORCHESTRATOR_CHILD_FAILED,
}

_TIMING_LABELS = {
    "gateway.turn_prepare": "Gateway Turn Prepare",
    "gateway.inbound_preprocess": "Inbound Preprocess",
    "orchestrator.memory_prefetch": "Memory Prefetch",
    "orchestrator.router_memory": "Router Memory",
    "orchestrator.provider_memory_enrich": "Provider Memory Enrich",
    "orchestrator.model": "Orchestrator Model",
    "orchestrator.decision_parse": "Decision Parse",
    "orchestrator.child_dispatch": "Child Dispatch",
    "orchestrator.child_wait": "Child Wait",
    "orchestrator.reply_finalize": "Reply Finalize",
    "response.send": "Response Send",
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
        self._group_update_listeners: list[Callable[[dict[str, Any]], None]] = []
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
            if self._thread.is_alive():
                _log.warning(
                    "NapCat observability ingest thread still alive after timeout; "
                    "skipping final flush to avoid concurrent store access"
                )
                self._thread = None
                return
            self._thread = None

        # Final drain only when the background thread has exited
        self._flush()
        _log.info("NapCat observability ingest client stopped")

    def add_trace_update_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._trace_update_listeners.append(listener)

    def add_group_update_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._group_update_listeners.append(listener)

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

        write_ok = False
        try:
            count = self.store.insert_events_batch(event_dicts)
            if count:
                _log.debug("Ingested %d events", count)
            write_ok = True
        except Exception:
            _log.warning("Failed to write events batch", exc_info=True)

        # Only update traces and alerts when the batch was successfully persisted
        if write_ok:
            self._update_traces(events)
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

                # Rebuild full view model and notify group listeners
                if self._group_update_listeners:
                    try:
                        full_events = self._load_trace_events(trace_id)
                        from gateway.napcat_observability.view_models import build_live_trace_group
                        group = build_live_trace_group(trace_dict, [e.to_dict() for e in full_events])
                        for listener in self._group_update_listeners:
                            try:
                                listener(group)
                            except Exception:
                                _log.debug("Group update listener failed", exc_info=True)
                    except Exception:
                        _log.debug("Failed to rebuild group for %s", trace_id, exc_info=True)
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

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if parsed != parsed:
            return None
        return parsed

    @staticmethod
    def _round_ms(value: float | None) -> float | None:
        if value is None:
            return None
        return round(max(0.0, float(value)), 2)

    @classmethod
    def _timing_label(cls, node: str) -> str:
        normalized = str(node or "").strip()
        if not normalized:
            return ""
        if normalized in _TIMING_LABELS:
            return _TIMING_LABELS[normalized]
        return normalized.replace(".", " ").replace("_", " ").title()

    @classmethod
    def _make_timing_node(
        cls,
        *,
        key: str,
        label: str = "",
        started_at: float | None = None,
        ended_at: float | None = None,
        duration_ms: float | None = None,
        source_event_type: str = "",
        note: str = "",
    ) -> dict[str, Any] | None:
        normalized_key = str(key or "").strip()
        if not normalized_key:
            return None
        normalized_start = cls._as_float(started_at)
        normalized_end = cls._as_float(ended_at)
        normalized_duration = cls._round_ms(duration_ms)
        if normalized_duration is None and normalized_start is not None and normalized_end is not None:
            normalized_duration = cls._round_ms((normalized_end - normalized_start) * 1000)
        if normalized_duration is None or normalized_duration < 0:
            return None
        if normalized_start is None and normalized_end is not None:
            normalized_start = normalized_end - (normalized_duration / 1000.0)
        if normalized_end is None and normalized_start is not None:
            normalized_end = normalized_start + (normalized_duration / 1000.0)
        return {
            "key": normalized_key,
            "label": label or cls._timing_label(normalized_key),
            "started_at": normalized_start,
            "ended_at": normalized_end,
            "duration_ms": normalized_duration,
            "source_event_type": source_event_type,
            "note": str(note or "").strip(),
        }

    @classmethod
    def _append_unique_timing_node(
        cls,
        nodes: list[dict[str, Any]],
        seen: set[tuple[Any, ...]],
        node: dict[str, Any] | None,
    ) -> None:
        if not node:
            return
        identity = (
            node.get("key"),
            node.get("started_at"),
            node.get("ended_at"),
            node.get("duration_ms"),
            node.get("source_event_type"),
            node.get("note"),
        )
        if identity in seen:
            return
        seen.add(identity)
        nodes.append(node)

    def _extract_payload_timing_nodes(self, events: list[NapcatEvent]) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for event in events:
            payload = event.payload if isinstance(event.payload, dict) else {}
            timing = payload.get("timing")
            if not isinstance(timing, dict):
                continue
            node = self._make_timing_node(
                key=str(timing.get("node") or "").strip(),
                label=str(timing.get("label") or "").strip(),
                started_at=timing.get("started_at"),
                ended_at=timing.get("ended_at") if timing.get("ended_at") is not None else event.ts,
                duration_ms=timing.get("duration_ms"),
                source_event_type=event.event_type.value,
                note=str(timing.get("note") or "").strip(),
            )
            self._append_unique_timing_node(nodes, seen, node)
        return nodes

    def _find_event(
        self,
        events: list[NapcatEvent],
        event_type: EventType,
        *,
        reverse: bool = False,
        predicate: Optional[Callable[[NapcatEvent], bool]] = None,
    ) -> NapcatEvent | None:
        iterable = reversed(events) if reverse else events
        for event in iterable:
            if event.event_type != event_type:
                continue
            if predicate is not None and not predicate(event):
                continue
            return event
        return None

    def _find_events_in_window(
        self,
        events: list[NapcatEvent],
        event_types: set[EventType],
        *,
        started_at: float,
        ended_at: float | None = None,
    ) -> list[NapcatEvent]:
        return [
            event
            for event in events
            if event.event_type in event_types
            and event.ts >= started_at
            and (ended_at is None or event.ts <= ended_at)
        ]

    def _build_timing_summary(
        self,
        events: list[NapcatEvent],
        *,
        trace_analysis: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        sorted_events = sorted(events, key=lambda item: (item.ts, item.event_id))
        nodes = self._extract_payload_timing_nodes(sorted_events)
        seen = {
            (
                node.get("key"),
                node.get("started_at"),
                node.get("ended_at"),
                node.get("duration_ms"),
                node.get("source_event_type"),
                node.get("note"),
            )
            for node in nodes
        }

        ingress_started_at = None
        for event in sorted_events:
            payload = event.payload if isinstance(event.payload, dict) else {}
            timing = payload.get("timing")
            if not isinstance(timing, dict):
                continue
            candidate = self._as_float(timing.get("adapter_received_at"))
            if candidate is None:
                continue
            ingress_started_at = candidate if ingress_started_at is None else min(ingress_started_at, candidate)
        if ingress_started_at is None and sorted_events:
            ingress_started_at = self._as_float(sorted_events[0].ts)

        gateway_turn_event = self._find_event(sorted_events, EventType.GATEWAY_TURN_PREPARED)
        has_gateway_turn_prepare = any(node.get("key") == "gateway.turn_prepare" for node in nodes)
        if gateway_turn_event is not None and ingress_started_at is not None and not has_gateway_turn_prepare:
            self._append_unique_timing_node(
                nodes,
                seen,
                self._make_timing_node(
                    key="gateway.turn_prepare",
                    started_at=ingress_started_at,
                    ended_at=gateway_turn_event.ts,
                    source_event_type=gateway_turn_event.event_type.value,
                ),
            )

        orchestrator_requests = [
            event for event in sorted_events
            if event.event_type == EventType.AGENT_MODEL_REQUESTED
            and str(event.payload.get("agent_scope") or "").strip() == "main_orchestrator"
        ]
        orchestrator_completions = [
            event for event in sorted_events
            if event.event_type == EventType.AGENT_MODEL_COMPLETED
            and str(event.payload.get("agent_scope") or "").strip() == "main_orchestrator"
        ]
        completion_by_request: dict[str, list[NapcatEvent]] = {}
        completion_fallback: list[NapcatEvent] = []
        for event in orchestrator_completions:
            request_id = str(event.payload.get("llm_request_id") or "").strip()
            if request_id:
                completion_by_request.setdefault(request_id, []).append(event)
            else:
                completion_fallback.append(event)
        last_model_completed_at = None
        for request_event in orchestrator_requests:
            request_id = str(request_event.payload.get("llm_request_id") or "").strip()
            completion_event = None
            if request_id and completion_by_request.get(request_id):
                completion_event = completion_by_request[request_id].pop(0)
            elif completion_fallback:
                completion_event = completion_fallback.pop(0)
            if completion_event is None:
                continue
            last_model_completed_at = completion_event.ts
            self._append_unique_timing_node(
                nodes,
                seen,
                self._make_timing_node(
                    key="orchestrator.model",
                    started_at=request_event.ts,
                    ended_at=completion_event.ts,
                    duration_ms=completion_event.payload.get("duration_ms"),
                    source_event_type=completion_event.event_type.value,
                    note=request_id,
                ),
            )

        decision_event = self._find_event(
            sorted_events,
            EventType.ORCHESTRATOR_DECISION_PARSED,
            reverse=True,
        )
        if decision_event is not None and last_model_completed_at is not None and decision_event.ts >= last_model_completed_at:
            self._append_unique_timing_node(
                nodes,
                seen,
                self._make_timing_node(
                    key="orchestrator.decision_parse",
                    started_at=last_model_completed_at,
                    ended_at=decision_event.ts,
                    source_event_type=decision_event.event_type.value,
                ),
            )

        reply_terminal_event = None
        for event in reversed(sorted_events):
            if event.event_type in _TRACE_REPLY_EVENTS:
                reply_terminal_event = event
                break

        response_send_nodes = [node for node in nodes if node.get("key") == "response.send"]
        if not response_send_nodes:
            for event in sorted_events:
                if event.event_type != EventType.ADAPTER_WS_ACTION:
                    continue
                action = str(event.payload.get("action") or "").strip()
                if action not in _RESPONSE_SEND_ACTIONS:
                    continue
                if event.payload.get("success") is False:
                    continue
                self._append_unique_timing_node(
                    nodes,
                    seen,
                    self._make_timing_node(
                        key="response.send",
                        started_at=(
                            event.ts - ((self._as_float(event.payload.get("duration_ms")) or 0.0) / 1000.0)
                        ),
                        ended_at=event.ts,
                        duration_ms=event.payload.get("duration_ms"),
                        source_event_type=event.event_type.value,
                        note=action,
                    ),
                )
            response_send_nodes = [node for node in nodes if node.get("key") == "response.send"]

        response_send_start_at = None
        response_send_end_at = None
        response_send_source_event_type = ""
        if response_send_nodes:
            response_send_starts = [
                self._as_float(node.get("started_at"))
                for node in response_send_nodes
                if self._as_float(node.get("started_at")) is not None
            ]
            response_send_ends = [
                self._as_float(node.get("ended_at"))
                for node in response_send_nodes
                if self._as_float(node.get("ended_at")) is not None
            ]
            if response_send_starts:
                response_send_start_at = min(response_send_starts)
            if response_send_ends:
                response_send_end_at = max(response_send_ends)
            first_response_send_node = min(
                response_send_nodes,
                key=lambda node: self._as_float(node.get("started_at")) if self._as_float(node.get("started_at")) is not None else float("inf"),
            )
            response_send_source_event_type = str(first_response_send_node.get("source_event_type") or "")

        if decision_event is not None:
            reply_finalize_end_at = None
            reply_finalize_source_event_type = ""
            if response_send_start_at is not None and response_send_start_at >= decision_event.ts:
                reply_finalize_end_at = response_send_start_at
                reply_finalize_source_event_type = response_send_source_event_type
            elif reply_terminal_event is not None and reply_terminal_event.ts >= decision_event.ts:
                reply_finalize_end_at = reply_terminal_event.ts
                reply_finalize_source_event_type = reply_terminal_event.event_type.value
            if reply_finalize_end_at is not None and reply_finalize_end_at >= decision_event.ts:
                child_activity_events = self._find_events_in_window(
                    sorted_events,
                    _TIMING_CHILD_ACTIVITY_EVENTS,
                    started_at=decision_event.ts,
                    ended_at=reply_finalize_end_at,
                )
                child_terminal_events = [
                    event for event in child_activity_events
                    if event.event_type in _TIMING_CHILD_TERMINAL_EVENTS
                ]
                reply_finalize_started_at = decision_event.ts
                first_child_activity = child_activity_events[0] if child_activity_events else None
                last_child_terminal = child_terminal_events[-1] if child_terminal_events else None

                if first_child_activity is not None and first_child_activity.ts > decision_event.ts:
                    self._append_unique_timing_node(
                        nodes,
                        seen,
                        self._make_timing_node(
                            key="orchestrator.child_dispatch",
                            started_at=decision_event.ts,
                            ended_at=first_child_activity.ts,
                            source_event_type=first_child_activity.event_type.value,
                            note=str(first_child_activity.payload.get("child_id") or "").strip(),
                        ),
                    )
                    reply_finalize_started_at = first_child_activity.ts

                if last_child_terminal is not None:
                    child_wait_started_at = (
                        first_child_activity.ts
                        if first_child_activity is not None
                        else decision_event.ts
                    )
                    if last_child_terminal.ts > child_wait_started_at:
                        self._append_unique_timing_node(
                            nodes,
                            seen,
                            self._make_timing_node(
                                key="orchestrator.child_wait",
                                started_at=child_wait_started_at,
                                ended_at=last_child_terminal.ts,
                                source_event_type=last_child_terminal.event_type.value,
                                note=str(last_child_terminal.payload.get("child_id") or "").strip(),
                            ),
                        )
                    reply_finalize_started_at = max(reply_finalize_started_at, last_child_terminal.ts)

                if reply_finalize_end_at > reply_finalize_started_at:
                    self._append_unique_timing_node(
                        nodes,
                        seen,
                        self._make_timing_node(
                            key="orchestrator.reply_finalize",
                            started_at=reply_finalize_started_at,
                            ended_at=reply_finalize_end_at,
                            source_event_type=(
                                reply_finalize_source_event_type
                                or (reply_terminal_event.event_type.value if reply_terminal_event is not None else decision_event.event_type.value)
                            ),
                        ),
                    )
                elif not child_activity_events:
                    self._append_unique_timing_node(
                        nodes,
                        seen,
                        self._make_timing_node(
                            key="orchestrator.reply_finalize",
                            started_at=decision_event.ts,
                            ended_at=reply_finalize_end_at,
                            source_event_type=(
                                reply_finalize_source_event_type
                                or (reply_terminal_event.event_type.value if reply_terminal_event is not None else decision_event.event_type.value)
                            ),
                        ),
                    )

        nodes.sort(
            key=lambda item: (
                self._as_float(item.get("started_at")) if self._as_float(item.get("started_at")) is not None else float("inf"),
                self._as_float(item.get("ended_at")) if self._as_float(item.get("ended_at")) is not None else float("inf"),
                str(item.get("source_event_type") or ""),
                str(item.get("key") or ""),
            )
        )

        ended_at = self._as_float((trace_analysis or {}).get("ended_at"))
        if response_send_end_at is not None:
            end_to_end_end_at = response_send_end_at
        elif ended_at is not None:
            end_to_end_end_at = ended_at
        elif sorted_events:
            end_to_end_end_at = self._as_float(sorted_events[-1].ts)
        else:
            end_to_end_end_at = None
        end_to_end_ms = None
        if ingress_started_at is not None and end_to_end_end_at is not None and end_to_end_end_at >= ingress_started_at:
            end_to_end_ms = self._round_ms((end_to_end_end_at - ingress_started_at) * 1000)

        component_keys = [
            "gateway.turn_prepare",
            "gateway.inbound_preprocess",
            "orchestrator.memory_prefetch",
            "orchestrator.router_memory",
            "orchestrator.provider_memory_enrich",
            "orchestrator.model",
            "orchestrator.decision_parse",
            "orchestrator.child_dispatch",
            "orchestrator.child_wait",
            "orchestrator.reply_finalize",
            "response.send",
        ]
        totals: dict[str, Any] = {
            "end_to_end_ms": end_to_end_ms,
        }
        measured_ms = 0.0
        for component_key in component_keys:
            component_total = self._round_ms(
                sum(
                    self._as_float(node.get("duration_ms")) or 0.0
                    for node in nodes
                    if node.get("key") == component_key
                )
            )
            totals[f"{component_key.replace('.', '_')}_ms"] = component_total
            measured_ms += component_total or 0.0
        totals["orchestrator_memory_prefetch_ms"] = self._round_ms(
            sum(
                self._as_float(node.get("duration_ms")) or 0.0
                for node in nodes
                if node.get("key") in {
                    "orchestrator.memory_prefetch",
                    "orchestrator.router_memory",
                    "orchestrator.provider_memory_enrich",
                }
            )
        )
        totals["measured_ms"] = self._round_ms(measured_ms)
        totals["unaccounted_ms"] = (
            self._round_ms(end_to_end_ms - measured_ms)
            if end_to_end_ms is not None and end_to_end_ms >= measured_ms
            else None
        )

        return nodes, totals

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
        context_only_ts: float | None = None
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

            if event.event_type == EventType.GROUP_CONTEXT_ONLY_EMITTED and context_only_ts is None:
                context_only_ts = event.ts

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
        elif context_only_ts is not None:
            status = "completed"

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
        timing_breakdown, timing_totals = self._build_timing_summary(
            events,
            trace_analysis=trace_analysis,
        )
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
                "timing_breakdown": timing_breakdown,
                "timing_totals": timing_totals,
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
