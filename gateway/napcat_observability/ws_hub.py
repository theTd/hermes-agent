"""
NapCat observability WebSocket hub.

Manages connected clients, broadcasts events, and handles
cursor-based backfill for reconnecting clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional, Set

_log = logging.getLogger(__name__)

_CLIENT_CLOSE_TIMEOUT_SECONDS = 1.0


class WebSocketHub:
    """Manages WebSocket connections for real-time event streaming."""

    def __init__(self, repo):
        self.repo = repo
        self._clients: Set = set()
        self._paused: Set = set()
        self._subscriptions: Dict[Any, Dict[str, Any]] = {}
        self._trace_status: Dict[str, str] = {}
        self._latest_cursor: float = time.time()
        self._lock = asyncio.Lock()

    async def connect(self, websocket) -> None:
        """Register a new WebSocket client."""
        async with self._lock:
            self._clients.add(websocket)
            self._subscriptions[websocket] = {}

        # Send initial snapshot
        await self._send_snapshot(websocket)

    async def disconnect(self, websocket) -> None:
        """Unregister a WebSocket client."""
        async with self._lock:
            self._clients.discard(websocket)
            self._paused.discard(websocket)
            self._subscriptions.pop(websocket, None)

    async def _send_snapshot(self, websocket) -> None:
        """Send initial state snapshot to a new client."""
        try:
            # Recent traces with full view models
            groups, _ = await asyncio.to_thread(self.repo.query_trace_view_models, limit=50)
            self._trace_status.update({
                str(g.get("traceId")): str(g.get("status") or "in_progress")
                for g in groups
                if g.get("traceId")
            })

            # Runtime state
            runtime = await asyncio.to_thread(self.repo.store.get_all_runtime_state)

            # Recent alerts (unacknowledged)
            alerts, _ = await asyncio.to_thread(self.repo.query_alerts, acknowledged=False, limit=20)
            sessions, _ = await asyncio.to_thread(self.repo.query_sessions, limit=20)

            # Publisher stats
            try:
                from gateway.napcat_observability.publisher import get_stats
                stats = get_stats()
            except ImportError:
                stats = {}

            await websocket.send_json({
                "type": "snapshot.init",
                "data": {
                    "groups": groups,
                    "sessions": sessions,
                    "runtime": runtime,
                    "alerts": alerts,
                    "stats": stats,
                    "cursor": self._latest_cursor,
                },
            })
        except Exception:
            _log.debug("Failed to send snapshot", exc_info=True)

    async def broadcast_event(self, event_dict: dict) -> None:
        """Broadcast a new event to all connected, non-paused clients."""
        self._latest_cursor = event_dict.get("ts", time.time())
        payload = event_dict.get("payload")
        if isinstance(payload, dict):
            timing = payload.get("timing")
            if not isinstance(timing, dict):
                timing = {}
            timing.setdefault("ws_broadcast_started_at", time.time())
            payload["timing"] = timing
        trace_id = str(event_dict.get("trace_id") or "")
        previous_status = self._trace_status.get(trace_id) if trace_id else None
        derived_status = self._derive_trace_status(event_dict)
        if trace_id:
            self._trace_status[trace_id] = derived_status
            # Cap memory growth by evicting oldest entries when over limit
            if len(self._trace_status) > self._TRACE_STATUS_MAX_SIZE:
                # Remove the oldest ~10% of entries
                to_evict = int(self._TRACE_STATUS_MAX_SIZE * 0.1) or 1
                for _ in range(to_evict):
                    try:
                        self._trace_status.pop(next(iter(self._trace_status)), None)
                    except StopIteration:
                        break

        msg = json.dumps({
            "type": "event.append",
            "data": event_dict,
        })

        async with self._lock:
            active = {
                ws for ws in self._clients - self._paused
                if self._event_matches_filters(
                    event_dict,
                    self._subscriptions.get(ws, {}),
                    derived_status=derived_status,
                    previous_status=previous_status,
                )
            }

        for ws in list(active):
            try:
                await ws.send_text(msg)
            except Exception:
                async with self._lock:
                    self._clients.discard(ws)

    async def broadcast_trace_update(self, trace_dict: dict) -> None:
        """Broadcast a trace update."""
        trace_id = str(trace_dict.get("trace_id") or "")
        previous_status = self._trace_status.get(trace_id) if trace_id else None
        if trace_id:
            self._trace_status[trace_id] = str(trace_dict.get("status") or "in_progress")
        msg = json.dumps({
            "type": "trace.update",
            "data": trace_dict,
        })

        async with self._lock:
            active = {
                ws for ws in self._clients - self._paused
                if self._trace_matches_filters(
                    trace_dict,
                    self._subscriptions.get(ws, {}),
                    previous_status=previous_status,
                )
            }

        for ws in list(active):
            try:
                await ws.send_text(msg)
            except Exception:
                async with self._lock:
                    self._clients.discard(ws)

    async def broadcast_group_update(self, group_dict: dict) -> None:
        """Broadcast a full LiveTraceGroup update."""
        trace_id = str(group_dict.get("traceId") or "")
        previous_status = self._trace_status.get(trace_id) if trace_id else None
        if trace_id:
            self._trace_status[trace_id] = str(group_dict.get("status") or "in_progress")
        msg = json.dumps({
            "type": "group.update",
            "data": group_dict,
        })

        async with self._lock:
            active = {
                ws for ws in self._clients - self._paused
                if self._trace_matches_filters(
                    group_dict.get("trace", {}),
                    self._subscriptions.get(ws, {}),
                    previous_status=previous_status,
                )
            }

        for ws in list(active):
            try:
                await ws.send_text(msg)
            except Exception:
                async with self._lock:
                    self._clients.discard(ws)

    async def broadcast_runtime_update(self, state: dict) -> None:
        """Broadcast a runtime state update."""
        msg = json.dumps({
            "type": "runtime.update",
            "data": state,
        })

        async with self._lock:
            active = self._clients - self._paused

        for ws in list(active):
            try:
                await ws.send_text(msg)
            except Exception:
                async with self._lock:
                    self._clients.discard(ws)

    async def broadcast_alert(self, alert_dict: dict) -> None:
        """Broadcast a new alert."""
        msg = json.dumps({
            "type": "alert.raised",
            "data": alert_dict,
        })

        # Run the synchronous DB query off the event loop to avoid blocking
        subscriptions = dict(self._subscriptions)
        active = set()
        for ws in list(self._clients - self._paused):
            filters = subscriptions.get(ws, {})
            if await asyncio.to_thread(self._alert_matches_filters, alert_dict, filters):
                active.add(ws)

        for ws in list(active):
            try:
                await ws.send_text(msg)
            except Exception:
                async with self._lock:
                    self._clients.discard(ws)

    async def broadcast_dashboard_update(self, stats: dict) -> None:
        """Broadcast dashboard stats to all connected clients."""
        msg = json.dumps({
            "type": "dashboard.update",
            "data": stats,
        })

        async with self._lock:
            active = self._clients - self._paused

        for ws in list(active):
            try:
                await ws.send_text(msg)
            except Exception:
                async with self._lock:
                    self._clients.discard(ws)

    async def handle_backfill(self, websocket, cursor: float, filters: Optional[dict] = None) -> None:
        """Send events after a cursor timestamp (for reconnection)."""
        try:
            active_filters = dict(self._subscriptions.get(websocket, {}))
            active_filters.update(filters or {})
            events = await asyncio.to_thread(
                self.repo.get_events_after_cursor,
                cursor,
                trace_id=active_filters.get("trace_id"),
                session_key=active_filters.get("session_key"),
                chat_id=active_filters.get("chat_id"),
                user_id=active_filters.get("user_id"),
                event_type=active_filters.get("event_type"),
                status=active_filters.get("status"),
                search=active_filters.get("search"),
                limit=500,
            )

            for event in events:
                await websocket.send_json({
                    "type": "event.append",
                    "data": event,
                })

            await websocket.send_json({
                "type": "backfill.complete",
                "data": {"cursor": self._latest_cursor, "count": len(events)},
            })
        except Exception:
            _log.debug("Backfill failed", exc_info=True)

    async def pause_client(self, websocket) -> None:
        """Pause streaming for a client."""
        async with self._lock:
            self._paused.add(websocket)

    async def resume_client(self, websocket) -> None:
        """Resume streaming for a client."""
        async with self._lock:
            self._paused.discard(websocket)

    async def subscribe_client(self, websocket, filters: Optional[dict]) -> None:
        """Update per-client subscription filters."""
        async with self._lock:
            if websocket in self._clients:
                self._subscriptions[websocket] = {
                    key: value
                    for key, value in dict(filters or {}).items()
                    if value not in (None, "", [], {})
                }

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def _close_client(
        self,
        websocket,
        *,
        code: int,
        reason: str,
        timeout: float,
    ) -> None:
        try:
            try:
                close_coro = websocket.close(code=code, reason=reason)
            except TypeError:
                close_coro = websocket.close()
            await asyncio.wait_for(close_coro, timeout=max(0.0, float(timeout)))
        except asyncio.TimeoutError:
            _log.warning(
                "Timed out closing observability WebSocket client after %.1fs",
                timeout,
            )
        except Exception:
            _log.debug("Failed to close WebSocket client", exc_info=True)

    async def close_all_clients(
        self,
        *,
        code: int = 1001,
        reason: str = "Server shutting down",
        timeout: float = _CLIENT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        """Close and unregister all connected WebSocket clients."""
        async with self._lock:
            clients = list(self._clients)
            self._clients.clear()
            self._paused.clear()
            self._subscriptions.clear()

        if not clients:
            return

        await asyncio.gather(
            *(
                self._close_client(
                    websocket,
                    code=code,
                    reason=reason,
                    timeout=timeout,
                )
                for websocket in clients
            )
        )

    _TRACE_STATUS_MAX_SIZE: int = 5000

    def _derive_trace_status(self, event_dict: dict) -> str:
        event_type = str(event_dict.get("event_type") or "")
        trace_id = str(event_dict.get("trace_id") or "")
        if event_type == "trace.created":
            return "in_progress"
        if event_type == "error.raised":
            return "error"
        if event_type == "group.trigger.rejected":
            return "rejected"
        if event_type == "orchestrator.child.cancelled":
            return "cancelled"
        if event_type in {
            "agent.response.final",
            "agent.response.suppressed",
            "gateway.turn.short_circuited",
            "orchestrator.status.reply",
            "orchestrator.turn.completed",
            "orchestrator.turn.ignored",
            "orchestrator.child.completed",
        }:
            return "completed"
        if event_type in {
            "orchestrator.turn.failed",
            "orchestrator.child.failed",
        }:
            return "error"
        return self._trace_status.get(trace_id, "in_progress")

    def _status_matches_filter(
        self,
        filters: Dict[str, Any],
        current_status: str,
        *,
        previous_status: Optional[str] = None,
    ) -> bool:
        requested_status = str(filters.get("status") or "").strip()
        if not requested_status:
            return True
        if current_status == requested_status:
            return True
        return previous_status == requested_status

    def _trace_matches_filters(
        self,
        trace_dict: dict,
        filters: Dict[str, Any],
        *,
        previous_status: Optional[str] = None,
    ) -> bool:
        if not filters:
            return True
        view = str(filters.get("view") or "")
        if view == "tools" and int(trace_dict.get("tool_call_count") or 0) <= 0:
            return False
        if view == "streaming" and not bool(trace_dict.get("active_stream")):
            return False
        if view == "alerts" and int(trace_dict.get("error_count") or 0) <= 0 and trace_dict.get("status") != "error":
            return False
        if not self._status_matches_filter(
            filters,
            str(trace_dict.get("status") or "in_progress"),
            previous_status=previous_status,
        ):
            return False
        if filters.get("session_key") and trace_dict.get("session_key") != filters["session_key"]:
            return False
        if filters.get("trace_id") and trace_dict.get("trace_id") != filters["trace_id"]:
            return False
        if filters.get("chat_id") and trace_dict.get("chat_id") != filters["chat_id"]:
            return False
        if filters.get("user_id") and trace_dict.get("user_id") != filters["user_id"]:
            return False
        search = str(filters.get("search") or "").strip().lower()
        if not search:
            return True
        return search in json.dumps(trace_dict, ensure_ascii=False).lower()

    def _event_matches_filters(
        self,
        event_dict: dict,
        filters: Dict[str, Any],
        *,
        derived_status: Optional[str] = None,
        previous_status: Optional[str] = None,
    ) -> bool:
        if not filters:
            return True
        event_type = str(event_dict.get("event_type") or "")
        view = str(filters.get("view") or "")
        if view == "tools" and not event_type.startswith("agent.tool."):
            return False
        if filters.get("session_key") and event_dict.get("session_key") != filters["session_key"]:
            return False
        if filters.get("trace_id") and event_dict.get("trace_id") != filters["trace_id"]:
            return False
        if filters.get("chat_id") and event_dict.get("chat_id") != filters["chat_id"]:
            return False
        if filters.get("user_id") and event_dict.get("user_id") != filters["user_id"]:
            return False
        if filters.get("event_type") and event_type != filters["event_type"]:
            return False
        if not self._status_matches_filter(
            filters,
            derived_status or self._derive_trace_status(event_dict),
            previous_status=previous_status,
        ):
            return False
        search = str(filters.get("search") or "").strip().lower()
        if not search:
            return True
        return search in json.dumps(event_dict, ensure_ascii=False).lower()

    def _alert_matches_filters(self, alert_dict: dict, filters: Dict[str, Any]) -> bool:
        if not filters:
            return True
        trace_id = str(alert_dict.get("trace_id") or "")
        if not trace_id:
            return True
        trace = self.repo.get_trace(trace_id)
        if not trace:
            return False
        return self._trace_matches_filters(trace, filters)
