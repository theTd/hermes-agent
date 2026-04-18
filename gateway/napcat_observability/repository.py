"""
NapCat observability repository — query layer over the SQLite store.

Provides filtered, paginated access to events, traces, sessions,
and alerts for the REST API and WebSocket stream.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from gateway.napcat_observability.store import ObservabilityStore

_log = logging.getLogger(__name__)


class ObservabilityRepository:
    """High-level query interface for NapCat observability data."""

    def __init__(self, store: ObservabilityStore):
        self.store = store

    # -------------------------------------------------------------------
    # Unified filter builder
    # -------------------------------------------------------------------

    @staticmethod
    def _build_where(
        *,
        trace_id: Optional[str] = None,
        time_start: Optional[float] = None,
        time_end: Optional[float] = None,
        session_key: Optional[str] = None,
        chat_id: Optional[str] = None,
        user_id: Optional[str] = None,
        message_id: Optional[str] = None,
        event_type: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        table_prefix: str = "",
    ) -> Tuple[str, List[Any]]:
        """Build WHERE clause and params from filter arguments."""
        clauses: List[str] = []
        params: List[Any] = []
        p = f"{table_prefix}." if table_prefix else ""

        if trace_id:
            clauses.append(f"{p}trace_id = ?")
            params.append(trace_id)
        if time_start is not None:
            clauses.append(f"{p}ts >= ?")
            params.append(time_start)
        if time_end is not None:
            clauses.append(f"{p}ts <= ?")
            params.append(time_end)
        if session_key:
            clauses.append(f"{p}session_key = ?")
            params.append(session_key)
        if chat_id:
            clauses.append(f"{p}chat_id = ?")
            params.append(chat_id)
        if user_id:
            clauses.append(f"{p}user_id = ?")
            params.append(user_id)
        if message_id:
            clauses.append(f"{p}message_id = ?")
            params.append(message_id)
        if event_type:
            clauses.append(f"{p}event_type = ?")
            params.append(event_type)
        if severity:
            clauses.append(f"{p}severity = ?")
            params.append(severity)
        if status:
            clauses.append(f"{p}status = ?")
            params.append(status)

        where = " AND ".join(clauses) if clauses else "1=1"
        return where, params

    # -------------------------------------------------------------------
    # Events
    # -------------------------------------------------------------------

    def query_events(
        self,
        *,
        trace_id: Optional[str] = None,
        time_start: Optional[float] = None,
        time_end: Optional[float] = None,
        session_key: Optional[str] = None,
        chat_id: Optional[str] = None,
        user_id: Optional[str] = None,
        message_id: Optional[str] = None,
        event_type: Optional[str] = None,
        severity: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Query events with filters. Returns (events, total_count)."""
        where, params = self._build_where(
            trace_id=trace_id,
            time_start=time_start, time_end=time_end,
            session_key=session_key, chat_id=chat_id,
            user_id=user_id, message_id=message_id,
            event_type=event_type, severity=severity,
            table_prefix="events",
        )

        if search:
            search_like = f"%{search}%"
            where += " AND (events.event_type LIKE ? OR events.payload LIKE ?)"
            params.extend([search_like, search_like])

        conn = self.store._get_conn()
        cursor = conn.cursor()

        try:
            # Count
            cursor.execute(
                f"SELECT COUNT(*) FROM events WHERE {where}",
                params,
            )
            total = cursor.fetchone()[0]

            # Fetch
            cursor.execute(
                f"SELECT * FROM events WHERE {where} ORDER BY ts DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            )
            rows = [self._row_to_event_dict(row) for row in cursor.fetchall()]

            return rows, total
        finally:
            cursor.close()

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Get a single event by ID."""
        conn = self.store._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT * FROM events WHERE event_id = ?", (event_id,))
            row = cursor.fetchone()
            return self._row_to_event_dict(row) if row else None
        finally:
            cursor.close()

    # -------------------------------------------------------------------
    # Traces
    # -------------------------------------------------------------------

    def query_traces(
        self,
        *,
        time_start: Optional[float] = None,
        time_end: Optional[float] = None,
        session_key: Optional[str] = None,
        chat_id: Optional[str] = None,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        event_type: Optional[str] = None,
        search: Optional[str] = None,
        view: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Query traces with filters."""
        clauses: List[str] = []
        params: List[Any] = []

        if time_start is not None:
            clauses.append("started_at >= ?")
            params.append(time_start)
        if time_end is not None:
            clauses.append("started_at <= ?")
            params.append(time_end)
        if session_key:
            clauses.append("session_key = ?")
            params.append(session_key)
        if chat_id:
            clauses.append("chat_id = ?")
            params.append(chat_id)
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if event_type:
            clauses.append(
                "EXISTS (SELECT 1 FROM events e WHERE e.trace_id = traces.trace_id AND e.event_type = ?)"
            )
            params.append(event_type)
        if view == "tools":
            clauses.append(
                "(tool_call_count > 0 OR EXISTS (SELECT 1 FROM events e WHERE e.trace_id = traces.trace_id AND e.event_type LIKE 'agent.tool.%'))"
            )
        elif view == "streaming":
            clauses.append("active_stream = 1")
        elif view == "alerts":
            clauses.append("(error_count > 0 OR status = 'error')")
        if search:
            search_like = f"%{search}%"
            clauses.append(
                """(
                    summary LIKE ?
                    OR trace_id LIKE ?
                    OR session_key LIKE ?
                    OR session_id LIKE ?
                    OR chat_id LIKE ?
                    OR user_id LIKE ?
                    OR message_id LIKE ?
                    OR model LIKE ?
                    OR EXISTS (
                        SELECT 1 FROM events e
                        WHERE e.trace_id = traces.trace_id
                          AND (e.event_type LIKE ? OR e.payload LIKE ?)
                    )
                )"""
            )
            params.extend([
                search_like,
                search_like,
                search_like,
                search_like,
                search_like,
                search_like,
                search_like,
                search_like,
                search_like,
                search_like,
            ])

        where = " AND ".join(clauses) if clauses else "1=1"

        conn = self.store._get_conn()
        cursor = conn.cursor()

        try:
            cursor.execute(f"SELECT COUNT(*) FROM traces WHERE {where}", params)
            total = cursor.fetchone()[0]

            cursor.execute(
                f"SELECT * FROM traces WHERE {where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            )
            rows = [self._row_to_trace_dict(row) for row in cursor.fetchall()]

            return rows, total
        finally:
            cursor.close()

    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """Get a trace with its associated events."""
        conn = self.store._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT * FROM traces WHERE trace_id = ?", (trace_id,))
            row = cursor.fetchone()
            if not row:
                return None

            trace = self._row_to_trace_dict(row)

            # Fetch all events for this trace
            cursor.execute(
                "SELECT * FROM events WHERE trace_id = ? ORDER BY ts ASC",
                (trace_id,),
            )
            trace["events"] = [self._row_to_event_dict(r) for r in cursor.fetchall()]

            return trace
        finally:
            cursor.close()

    # -------------------------------------------------------------------
    # Sessions (aggregated from traces)
    # -------------------------------------------------------------------

    def query_sessions(
        self,
        *,
        time_start: Optional[float] = None,
        time_end: Optional[float] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Query distinct sessions from traces."""
        clauses: List[str] = []
        params: List[Any] = []

        if time_start is not None:
            clauses.append("started_at >= ?")
            params.append(time_start)
        if time_end is not None:
            clauses.append("started_at <= ?")
            params.append(time_end)

        where = " AND ".join(clauses) if clauses else "1=1"

        conn = self.store._get_conn()
        cursor = conn.cursor()

        try:
            cursor.execute(
                f"""SELECT session_key, session_id,
                           COUNT(*) as trace_count,
                           MIN(started_at) as first_seen,
                           MAX(started_at) as last_seen,
                           SUM(event_count) as total_events
                    FROM traces
                    WHERE {where} AND session_key != ''
                    GROUP BY session_key
                    ORDER BY last_seen DESC
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            )
            rows = [dict(row) for row in cursor.fetchall()]

            cursor.execute(
                f"""SELECT COUNT(DISTINCT session_key)
                    FROM traces WHERE {where} AND session_key != ''""",
                params,
            )
            total = cursor.fetchone()[0]

            return rows, total
        finally:
            cursor.close()

    # -------------------------------------------------------------------
    # Messages (events grouped by message_id)
    # -------------------------------------------------------------------

    def get_message_events(self, message_id: str) -> Dict[str, Any]:
        """Get all events and traces related to a message."""
        conn = self.store._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT * FROM events WHERE message_id = ? ORDER BY ts ASC",
                (message_id,),
            )
            events = [self._row_to_event_dict(r) for r in cursor.fetchall()]

            # Find related traces
            trace_ids = list({e["trace_id"] for e in events if e.get("trace_id")})
            traces = []
            for tid in trace_ids:
                cursor.execute("SELECT * FROM traces WHERE trace_id = ?", (tid,))
                row = cursor.fetchone()
                if row:
                    traces.append(self._row_to_trace_dict(row))

            return {
                "message_id": message_id,
                "events": events,
                "traces": traces,
            }
        finally:
            cursor.close()

    # -------------------------------------------------------------------
    # Alerts
    # -------------------------------------------------------------------

    def query_alerts(
        self,
        *,
        acknowledged: Optional[bool] = None,
        severity: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Query alerts with filters."""
        clauses: List[str] = []
        params: List[Any] = []

        if acknowledged is not None:
            clauses.append("acknowledged = ?")
            params.append(1 if acknowledged else 0)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)

        where = " AND ".join(clauses) if clauses else "1=1"

        conn = self.store._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute(f"SELECT COUNT(*) FROM alerts WHERE {where}", params)
            total = cursor.fetchone()[0]

            cursor.execute(
                f"SELECT * FROM alerts WHERE {where} ORDER BY ts DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            )
            rows = [dict(row) for row in cursor.fetchall()]

            return rows, total
        finally:
            cursor.close()

    # -------------------------------------------------------------------
    # Cursor support (for WebSocket backfill)
    # -------------------------------------------------------------------

    def get_events_after_cursor(
        self,
        cursor_ts: float,
        *,
        trace_id: Optional[str] = None,
        session_key: Optional[str] = None,
        chat_id: Optional[str] = None,
        user_id: Optional[str] = None,
        event_type: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Get events after a timestamp cursor for WebSocket backfill."""
        where, params = self._build_where(
            trace_id=trace_id,
            session_key=session_key,
            chat_id=chat_id,
            user_id=user_id,
            event_type=event_type,
            table_prefix="events",
        )
        clauses = [where, "events.ts > ?"]
        params.append(cursor_ts)

        if status:
            clauses.append("traces.status = ?")
            params.append(status)
        if search:
            search_like = f"%{search}%"
            clauses.append("(events.event_type LIKE ? OR events.payload LIKE ?)")
            params.extend([search_like, search_like])

        conn = self.store._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""SELECT events.* FROM events
                    INNER JOIN traces ON traces.trace_id = events.trace_id
                    WHERE {' AND '.join(clauses)}
                    ORDER BY events.ts ASC, events.event_id ASC
                    LIMIT ?""",
                params + [limit],
            )
            return [self._row_to_event_dict(r) for r in cursor.fetchall()]
        finally:
            cursor.close()

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _row_to_event_dict(row) -> Dict[str, Any]:
        """Convert a sqlite3.Row to event dict with parsed payload."""
        d = dict(row)
        if "payload" in d and isinstance(d["payload"], str):
            try:
                d["payload"] = json.loads(d["payload"])
            except (json.JSONDecodeError, TypeError):
                d["payload"] = {}
        return d

    @staticmethod
    def _row_to_trace_dict(row) -> Dict[str, Any]:
        """Convert a sqlite3.Row to trace dict with parsed summary."""
        d = dict(row)
        if "active_stream" in d:
            d["active_stream"] = bool(d["active_stream"])
        if "summary" in d and isinstance(d["summary"], str):
            try:
                d["summary"] = json.loads(d["summary"])
            except (json.JSONDecodeError, TypeError):
                d["summary"] = {}
        return d
