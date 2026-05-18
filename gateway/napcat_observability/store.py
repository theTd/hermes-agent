"""
NapCat observability SQLite store.

Manages an independent SQLite database for monitoring events,
separate from the existing session transcript database.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

_log = logging.getLogger(__name__)

# Default database location
_DEFAULT_DB_NAME = "napcat_observability.db"


def _get_default_db_path() -> Path:
    """Return the default database path under the active HERMES_HOME."""
    return get_hermes_home() / _DEFAULT_DB_NAME


class ObservabilityStore:
    """SQLite-backed storage for NapCat observability data."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or _get_default_db_path()
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._ensure_tables()

    def _get_conn(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                self._conn = sqlite3.connect(
                    str(self.db_path),
                    check_same_thread=False,
                    timeout=10.0,
                )
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute("PRAGMA foreign_keys=ON")
            return self._conn

    @contextmanager
    def _cursor(self):
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            try:
                yield cursor
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cursor.close()

    def _ensure_tables(self) -> None:
        """Create tables and indexes if they don't exist (idempotent)."""
        with self._lock:
            conn = self._get_conn()

            conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id       TEXT PRIMARY KEY,
                trace_id       TEXT NOT NULL,
                span_id        TEXT NOT NULL,
                parent_span_id TEXT DEFAULT '',
                ts             REAL NOT NULL,
                session_key    TEXT DEFAULT '',
                session_id     TEXT DEFAULT '',
                platform       TEXT DEFAULT 'napcat',
                chat_type      TEXT DEFAULT '',
                chat_id        TEXT DEFAULT '',
                user_id        TEXT DEFAULT '',
                message_id     TEXT DEFAULT '',
                event_type     TEXT NOT NULL,
                severity       TEXT DEFAULT 'info',
                payload        TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_events_trace_id ON events(trace_id);
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
            CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type);
            CREATE INDEX IF NOT EXISTS idx_events_session_key ON events(session_key);
            CREATE INDEX IF NOT EXISTS idx_events_chat_id ON events(chat_id);
            CREATE INDEX IF NOT EXISTS idx_events_user_id ON events(user_id);
            CREATE INDEX IF NOT EXISTS idx_events_message_id ON events(message_id);
            CREATE INDEX IF NOT EXISTS idx_events_severity ON events(severity);

            CREATE TABLE IF NOT EXISTS traces (
                trace_id       TEXT PRIMARY KEY,
                session_key    TEXT DEFAULT '',
                session_id     TEXT DEFAULT '',
                chat_type      TEXT DEFAULT '',
                chat_id        TEXT DEFAULT '',
                user_id        TEXT DEFAULT '',
                message_id     TEXT DEFAULT '',
                started_at     REAL NOT NULL,
                ended_at       REAL,
                event_count    INTEGER DEFAULT 0,
                status         TEXT DEFAULT 'in_progress',
                latest_stage   TEXT DEFAULT 'raw',
                active_stream  INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                error_count    INTEGER DEFAULT 0,
                model          TEXT DEFAULT '',
                summary        TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_traces_session_key ON traces(session_key);
            CREATE INDEX IF NOT EXISTS idx_traces_chat_id ON traces(chat_id);
            CREATE INDEX IF NOT EXISTS idx_traces_user_id ON traces(user_id);
            CREATE INDEX IF NOT EXISTS idx_traces_started_at ON traces(started_at);
            CREATE INDEX IF NOT EXISTS idx_traces_status ON traces(status);

            CREATE TABLE IF NOT EXISTS runtime_state (
                key            TEXT PRIMARY KEY,
                value          TEXT NOT NULL,
                updated_at     REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS alerts (
                alert_id       TEXT PRIMARY KEY,
                trace_id       TEXT DEFAULT '',
                event_id       TEXT DEFAULT '',
                ts             REAL NOT NULL,
                severity       TEXT DEFAULT 'error',
                title          TEXT NOT NULL,
                detail         TEXT DEFAULT '',
                acknowledged   INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
            CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity);
            CREATE INDEX IF NOT EXISTS idx_alerts_acknowledged ON alerts(acknowledged);
        """)

        # FTS5 virtual table for full-text search on event payloads
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
                    event_id,
                    payload,
                    content='events',
                    content_rowid='rowid'
                )
            """)
        except sqlite3.OperationalError:
            _log.warning("FTS5 not available; full-text search will be disabled")

        conn.commit()
        self._ensure_trace_columns(conn)
        _log.debug("Observability store tables ensured at %s", self.db_path)

    def _ensure_trace_columns(self, conn: sqlite3.Connection) -> None:
        cursor = conn.cursor()
        try:
            existing = {
                row[1]
                for row in cursor.execute("PRAGMA table_info(traces)").fetchall()
            }
            wanted = {
                "latest_stage": "TEXT DEFAULT 'raw'",
                "active_stream": "INTEGER DEFAULT 0",
                "tool_call_count": "INTEGER DEFAULT 0",
                "error_count": "INTEGER DEFAULT 0",
                "model": "TEXT DEFAULT ''",
            }
            for name, definition in wanted.items():
                if name not in existing:
                    cursor.execute(f"ALTER TABLE traces ADD COLUMN {name} {definition}")
            conn.commit()
        finally:
            cursor.close()

    # -------------------------------------------------------------------
    # Event CRUD
    # -------------------------------------------------------------------

    def insert_event(self, event_dict: dict) -> None:
        """Insert a single event dict (from NapcatEvent.to_dict())."""
        payload_str = json.dumps(event_dict.get("payload", {}), ensure_ascii=False)

        with self._cursor() as cur:
            cur.execute(
                """INSERT OR IGNORE INTO events
                   (event_id, trace_id, span_id, parent_span_id, ts,
                    session_key, session_id, platform, chat_type, chat_id,
                    user_id, message_id, event_type, severity, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_dict["event_id"],
                    event_dict["trace_id"],
                    event_dict["span_id"],
                    event_dict.get("parent_span_id", ""),
                    event_dict["ts"],
                    event_dict.get("session_key", ""),
                    event_dict.get("session_id", ""),
                    event_dict.get("platform", "napcat"),
                    event_dict.get("chat_type", ""),
                    event_dict.get("chat_id", ""),
                    event_dict.get("user_id", ""),
                    event_dict.get("message_id", ""),
                    event_dict["event_type"],
                    event_dict.get("severity", "info"),
                    payload_str,
                ),
            )

            # Update FTS index
            try:
                cur.execute(
                    "INSERT INTO events_fts(event_id, payload) VALUES (?, ?)",
                    (event_dict["event_id"], payload_str),
                )
            except sqlite3.OperationalError:
                pass  # FTS not available

    def insert_events_batch(self, event_dicts: list[dict]) -> int:
        """Insert multiple events in a single transaction. Returns count inserted."""
        if not event_dicts:
            return 0

        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            count = 0
            try:
                for ed in event_dicts:
                    payload_str = json.dumps(ed.get("payload", {}), ensure_ascii=False)
                    cursor.execute(
                        """INSERT OR IGNORE INTO events
                           (event_id, trace_id, span_id, parent_span_id, ts,
                            session_key, session_id, platform, chat_type, chat_id,
                            user_id, message_id, event_type, severity, payload)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            ed["event_id"], ed["trace_id"], ed["span_id"],
                            ed.get("parent_span_id", ""), ed["ts"],
                            ed.get("session_key", ""), ed.get("session_id", ""),
                            ed.get("platform", "napcat"), ed.get("chat_type", ""),
                            ed.get("chat_id", ""), ed.get("user_id", ""),
                            ed.get("message_id", ""), ed["event_type"],
                            ed.get("severity", "info"), payload_str,
                        ),
                    )
                    count += cursor.rowcount

                    try:
                        cursor.execute(
                            "INSERT INTO events_fts(event_id, payload) VALUES (?, ?)",
                            (ed["event_id"], payload_str),
                        )
                    except sqlite3.OperationalError:
                        pass

                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cursor.close()

        return count

    # -------------------------------------------------------------------
    # Trace CRUD
    # -------------------------------------------------------------------

    def upsert_trace(self, trace_dict: dict) -> None:
        """Insert or update a trace summary record."""
        summary_str = json.dumps(trace_dict.get("summary", {}), ensure_ascii=False)

        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO traces
                   (trace_id, session_key, session_id, chat_type, chat_id,
                    user_id, message_id, started_at, ended_at, event_count,
                    status, latest_stage, active_stream, tool_call_count,
                    error_count, model, summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(trace_id) DO UPDATE SET
                    session_key = excluded.session_key,
                    session_id = excluded.session_id,
                    chat_type = excluded.chat_type,
                    chat_id = excluded.chat_id,
                    user_id = excluded.user_id,
                    message_id = excluded.message_id,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    event_count = excluded.event_count,
                    status = excluded.status,
                    latest_stage = excluded.latest_stage,
                    active_stream = excluded.active_stream,
                    tool_call_count = excluded.tool_call_count,
                    error_count = excluded.error_count,
                    model = excluded.model,
                    summary = excluded.summary""",
                (
                    trace_dict["trace_id"],
                    trace_dict.get("session_key", ""),
                    trace_dict.get("session_id", ""),
                    trace_dict.get("chat_type", ""),
                    trace_dict.get("chat_id", ""),
                    trace_dict.get("user_id", ""),
                    trace_dict.get("message_id", ""),
                    trace_dict["started_at"],
                    trace_dict.get("ended_at"),
                    trace_dict.get("event_count", 0),
                    trace_dict.get("status", "in_progress"),
                    trace_dict.get("latest_stage", "raw"),
                    1 if trace_dict.get("active_stream") else 0,
                    trace_dict.get("tool_call_count", 0),
                    trace_dict.get("error_count", 0),
                    trace_dict.get("model", ""),
                    summary_str,
                ),
            )

    def get_trace(self, trace_id: str) -> Optional[sqlite3.Row]:
        """Return a single trace row."""
        with self._cursor() as cur:
            cur.execute("SELECT * FROM traces WHERE trace_id = ?", (trace_id,))
            return cur.fetchone()

    def get_events_for_trace(self, trace_id: str) -> list[sqlite3.Row]:
        """Return all events for a trace ordered by time."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM events WHERE trace_id = ? ORDER BY ts ASC, event_id ASC",
                (trace_id,),
            )
            return cur.fetchall()

    # -------------------------------------------------------------------
    # Runtime state
    # -------------------------------------------------------------------

    def set_runtime_state(self, key: str, value: dict) -> None:
        """Set a runtime state entry."""
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO runtime_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at""",
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )

    def get_runtime_state(self, key: str) -> Optional[dict]:
        """Get a runtime state entry."""
        with self._cursor() as cur:
            cur.execute("SELECT value FROM runtime_state WHERE key = ?", (key,))
            row = cur.fetchone()
            return json.loads(row["value"]) if row else None

    def get_all_runtime_state(self) -> dict:
        """Get all runtime state entries."""
        with self._cursor() as cur:
            cur.execute("SELECT key, value, updated_at FROM runtime_state")
            return {
                row["key"]: {
                    "value": json.loads(row["value"]),
                    "updated_at": row["updated_at"],
                }
                for row in cur.fetchall()
            }

    # -------------------------------------------------------------------
    # Alerts
    # -------------------------------------------------------------------

    def insert_alert(self, alert_dict: dict) -> None:
        """Insert an alert."""
        with self._cursor() as cur:
            cur.execute(
                """INSERT OR IGNORE INTO alerts
                   (alert_id, trace_id, event_id, ts, severity, title, detail, acknowledged)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    alert_dict["alert_id"],
                    alert_dict.get("trace_id", ""),
                    alert_dict.get("event_id", ""),
                    alert_dict["ts"],
                    alert_dict.get("severity", "error"),
                    alert_dict["title"],
                    alert_dict.get("detail", ""),
                    0,
                ),
            )

    def acknowledge_alert(self, alert_id: str) -> bool:
        """Mark an alert as acknowledged. Returns True if found."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE alerts SET acknowledged = 1 WHERE alert_id = ?",
                (alert_id,),
            )
            return cur.rowcount > 0

    # -------------------------------------------------------------------
    # Retention cleanup
    # -------------------------------------------------------------------

    def cleanup_expired(self, retention_days: int = 30) -> dict:
        """Delete monitoring data older than retention_days.

        Returns a dict with counts of deleted records per table.
        Only touches observability tables — never the session transcript.
        """
        cutoff = time.time() - (retention_days * 86400)
        result = {}

        with self._cursor() as cur:
            # Delete old events
            cur.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            result["events"] = cur.rowcount

            # Delete old traces
            cur.execute("DELETE FROM traces WHERE started_at < ?", (cutoff,))
            result["traces"] = cur.rowcount

            # Delete old alerts
            cur.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))
            result["alerts"] = cur.rowcount

            # Rebuild FTS after deleting events
            try:
                cur.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
            except sqlite3.OperationalError:
                pass

        _log.info(
            "Observability cleanup: deleted %d events, %d traces, %d alerts (cutoff=%d days)",
            result["events"], result["traces"], result["alerts"], retention_days,
        )
        return result

    def clear_all(self) -> dict:
        """Delete all observability data.

        Only touches observability tables — never the session transcript.
        """
        result = {}

        with self._cursor() as cur:
            cur.execute("DELETE FROM events")
            result["events"] = cur.rowcount

            cur.execute("DELETE FROM traces")
            result["traces"] = cur.rowcount

            cur.execute("DELETE FROM alerts")
            result["alerts"] = cur.rowcount

            cur.execute("DELETE FROM runtime_state")
            result["runtime_state"] = cur.rowcount

            try:
                cur.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
            except sqlite3.OperationalError:
                pass

        _log.info(
            "Observability clear-all: deleted %d events, %d traces, %d alerts, %d runtime entries",
            result["events"],
            result["traces"],
            result["alerts"],
            result["runtime_state"],
        )
        return result

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
