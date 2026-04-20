"""
Integration tests for NapCat observability.

Tests the full chain: schema → publisher → store → repository → API,
using in-memory SQLite and mocked events.
"""

import json
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.tool_event_instrumentation import suppress_tool_event_instrumentation
from model_tools import handle_function_call
from gateway.napcat_observability.schema import (
    EventPriority,
    EventType,
    NapcatEvent,
    Severity,
    default_severity,
    event_priority,
    truncate_field,
    truncate_payload,
)
from gateway.napcat_observability.trace import NapcatTraceContext, current_napcat_trace
from gateway.napcat_observability.publisher import (
    drain_queue,
    emit_napcat_event,
    get_stats,
    init_publisher,
    shutdown_publisher,
)
from gateway.napcat_observability.store import ObservabilityStore
from gateway.napcat_observability.repository import ObservabilityRepository
from gateway.napcat_observability.ingest_client import IngestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def trace_ctx():
    """A sample NapCat trace context for a group message."""
    return NapcatTraceContext(
        trace_id=uuid.uuid4().hex,
        chat_type="group",
        chat_id="12345",
        user_id="67890",
        message_id="msg_001",
        session_key="napcat:group:12345",
        session_id="sess_abc",
    )


@pytest.fixture
def dm_trace_ctx():
    """A sample trace context for a DM."""
    return NapcatTraceContext(
        trace_id=uuid.uuid4().hex,
        chat_type="private",
        chat_id="67890",
        user_id="67890",
        message_id="msg_002",
        session_key="napcat:private:67890",
        session_id="sess_def",
    )


@pytest.fixture
def tmp_store(tmp_path):
    """Create a temporary SQLite store."""
    store = ObservabilityStore(db_path=tmp_path / "test_obs.db")
    yield store
    store.close()


@pytest.fixture
def repo(tmp_store):
    """Repository backed by temp store."""
    return ObservabilityRepository(tmp_store)


@pytest.fixture(autouse=True)
def _reset_publisher():
    """Reset publisher state before each test."""
    shutdown_publisher()
    yield
    shutdown_publisher()


# ---------------------------------------------------------------------------
# Schema Tests
# ---------------------------------------------------------------------------

class TestSchema:
    """Event schema and type tests."""

    def test_event_types_are_strings(self):
        for et in EventType:
            assert isinstance(et.value, str)
            assert "." in et.value

    def test_severity_levels(self):
        assert Severity.DEBUG.value == "debug"
        assert Severity.ERROR.value == "error"

    def test_event_creation_minimal(self, trace_ctx):
        event = NapcatEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"text": "hello"},
        )
        assert event.event_id
        assert event.trace_id
        assert event.span_id
        assert event.severity == Severity.INFO


class TestObservabilityStorePaths:
    def test_default_store_path_uses_active_hermes_home(self, tmp_path):
        with patch("gateway.napcat_observability.store.get_hermes_home", return_value=tmp_path):
            store = ObservabilityStore()
        try:
            assert store.db_path == tmp_path / "napcat_observability.db"
        finally:
            store.close()

    def test_event_creation_with_context(self, trace_ctx):
        event = NapcatEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"text": "hello"},
            trace_id=trace_ctx.trace_id,
            session_key=trace_ctx.session_key,
            chat_type=trace_ctx.chat_type,
            chat_id=trace_ctx.chat_id,
            user_id=trace_ctx.user_id,
            message_id=trace_ctx.message_id,
        )
        assert event.chat_type == "group"
        assert event.chat_id == "12345"

    def test_event_round_trip(self):
        event = NapcatEvent(
            event_type=EventType.AGENT_TOOL_CALLED,
            payload={"tool_name": "read_file", "args": {"path": "/tmp/test"}},
        )
        d = event.to_dict()
        restored = NapcatEvent.from_dict(d)
        assert restored.event_type == event.event_type
        assert restored.payload == event.payload
        assert restored.event_id == event.event_id

    def test_error_event_default_severity(self):
        event = NapcatEvent(
            event_type=EventType.ERROR_RAISED,
            payload={"error": "test"},
        )
        assert event.severity == Severity.ERROR

    def test_event_priority(self):
        assert event_priority(EventType.TRACE_CREATED) == EventPriority.CRITICAL
        assert event_priority(EventType.ADAPTER_LIFECYCLE) == EventPriority.CRITICAL
        assert event_priority(EventType.ERROR_RAISED) == EventPriority.HIGH
        assert event_priority(EventType.MESSAGE_MEDIA_EXTRACTED) == EventPriority.LOW
        assert event_priority(EventType.MESSAGE_RECEIVED) == EventPriority.NORMAL

    def test_truncate_field(self):
        short = "hello"
        assert truncate_field(short, 100) == short

        long = "x" * 1000
        truncated = truncate_field(long, 100)
        assert len(truncated) <= 100
        assert "truncated" in truncated

    def test_truncate_payload(self):
        payload = {
            "stdout": "x" * 10000,
            "stderr": "y" * 10000,
            "other": "preserved",
        }
        result = truncate_payload(payload, max_stdout_chars=100, max_stderr_chars=50)
        assert len(result["stdout"]) <= 100
        assert len(result["stderr"]) <= 50
        assert result["other"] == "preserved"

    def test_truncate_payload_preserves_full_prompt_and_context(self):
        long_text = "p" * 20000
        result = truncate_payload({
            "prompt": long_text,
            "context": long_text,
            "stdout": long_text,
        })

        assert result["prompt"] == long_text
        assert result["context"] == long_text
        assert result["stdout"] != long_text
        assert "[truncated]" in result["stdout"]


# ---------------------------------------------------------------------------
# Trace Context Tests
# ---------------------------------------------------------------------------

class TestTraceContext:
    """Trace context management tests."""

    def test_child_span(self, trace_ctx):
        child = trace_ctx.child_span()
        assert child.trace_id == trace_ctx.trace_id
        assert child.parent_span_id == trace_ctx.span_id
        assert child.span_id != trace_ctx.span_id
        assert child.session_key == trace_ctx.session_key

    def test_trace_round_trip(self, trace_ctx):
        d = trace_ctx.to_dict()
        restored = NapcatTraceContext.from_dict(d)
        assert restored.trace_id == trace_ctx.trace_id
        assert restored.chat_type == trace_ctx.chat_type

    def test_child_span_preserves_scope_metadata(self, trace_ctx):
        trace_ctx.agent_scope = "main_orchestrator"
        trace_ctx.child_id = "child-1"

        child = trace_ctx.child_span()

        assert child.agent_scope == "main_orchestrator"
        assert child.child_id == "child-1"

    def test_child_span_resets_llm_request_id(self, trace_ctx):
        trace_ctx.llm_request_id = "req-main"

        child = trace_ctx.child_span()

        assert child.llm_request_id == ""


# ---------------------------------------------------------------------------
# Publisher Tests
# ---------------------------------------------------------------------------

class TestPublisher:
    """Event publisher and queue tests."""

    def test_disabled_returns_none(self, trace_ctx):
        init_publisher(enabled=False)
        result = emit_napcat_event(
            EventType.MESSAGE_RECEIVED,
            {"text": "test"},
            trace_ctx=trace_ctx,
        )
        assert result is None

    def test_enabled_returns_event(self, trace_ctx):
        init_publisher(enabled=True, queue_size=100)
        result = emit_napcat_event(
            EventType.MESSAGE_RECEIVED,
            {"text": "test"},
            trace_ctx=trace_ctx,
        )
        assert result is not None
        assert result.event_type == EventType.MESSAGE_RECEIVED

    def test_drain_queue(self, trace_ctx):
        init_publisher(enabled=True, queue_size=100)
        for i in range(5):
            emit_napcat_event(
                EventType.MESSAGE_RECEIVED,
                {"index": i},
                trace_ctx=trace_ctx,
            )
        events = drain_queue()
        assert len(events) == 5

    def test_emit_inherits_agent_scope_and_child_id(self, trace_ctx):
        init_publisher(enabled=True, queue_size=100)
        trace_ctx.agent_scope = "orchestrator_child"
        trace_ctx.child_id = "child-42"

        event = emit_napcat_event(
            EventType.AGENT_MODEL_REQUESTED,
            {"model": "test-model"},
            trace_ctx=trace_ctx,
        )

        assert event is not None
        assert event.payload["agent_scope"] == "orchestrator_child"
        assert event.payload["child_id"] == "child-42"

    def test_emit_inherits_llm_request_id(self, trace_ctx):
        init_publisher(enabled=True, queue_size=100)
        trace_ctx.llm_request_id = "req-123"

        event = emit_napcat_event(
            EventType.AGENT_TOOL_CALLED,
            {"tool_name": "read_file"},
            trace_ctx=trace_ctx,
        )

        assert event is not None
        assert event.payload["llm_request_id"] == "req-123"

    def test_queue_full_drops_low_priority(self, trace_ctx):
        init_publisher(enabled=True, queue_size=3)
        # Fill with low priority events
        for i in range(5):
            emit_napcat_event(
                EventType.MESSAGE_MEDIA_EXTRACTED,
                {"index": i},
                trace_ctx=trace_ctx,
            )
        stats = get_stats()
        assert stats["drop_count"] >= 2  # At least 2 dropped


class TestStoreActions:
    def test_clear_all_removes_persisted_observability_data(self, tmp_store, trace_ctx):
        tmp_store.insert_event(
            NapcatEvent(
                event_type=EventType.MESSAGE_RECEIVED,
                payload={"text": "hello"},
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
            ).to_dict()
        )
        tmp_store.upsert_trace(
            {
                "trace_id": trace_ctx.trace_id,
                "session_key": trace_ctx.session_key,
                "session_id": trace_ctx.session_id,
                "chat_type": trace_ctx.chat_type,
                "chat_id": trace_ctx.chat_id,
                "user_id": trace_ctx.user_id,
                "message_id": trace_ctx.message_id,
                "started_at": time.time(),
                "ended_at": None,
                "event_count": 1,
                "status": "in_progress",
            }
        )
        tmp_store.insert_alert(
            {
                "alert_id": "alert-1",
                "trace_id": trace_ctx.trace_id,
                "event_id": "evt-1",
                "ts": time.time(),
                "severity": "error",
                "title": "boom",
                "detail": "detail",
            }
        )
        tmp_store.set_runtime_state("diagnostic_level", {"level": "debug"})

        result = tmp_store.clear_all()

        assert result["events"] == 1
        assert result["traces"] == 1
        assert result["alerts"] == 1
        assert result["runtime_state"] == 1
        assert tmp_store.get_events_for_trace(trace_ctx.trace_id) == []
        assert tmp_store.get_trace(trace_ctx.trace_id) is None
        assert tmp_store.get_all_runtime_state() == {}
        conn = tmp_store._get_conn()
        remaining_alerts = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        assert remaining_alerts == 0

    def test_stats(self, trace_ctx):
        init_publisher(enabled=True, queue_size=100)
        emit_napcat_event(
            EventType.MESSAGE_RECEIVED,
            {"text": "test"},
            trace_ctx=trace_ctx,
        )
        stats = get_stats()
        assert stats["enabled"] is True
        assert stats["emit_count"] >= 1

    def test_registry_tools_emit_tool_events_with_bound_trace(self, trace_ctx, tmp_path):
        init_publisher(enabled=True, queue_size=100)
        file_path = tmp_path / "sample.txt"
        file_path.write_text("hello observability", encoding="utf-8")

        token = current_napcat_trace.set(trace_ctx)
        try:
            handle_function_call(
                "read_file",
                {"path": str(file_path)},
                task_id="task_obs",
                tool_call_id="tool_123",
            )
        finally:
            current_napcat_trace.reset(token)

        events = drain_queue()
        assert [e.event_type for e in events] == [
            EventType.AGENT_TOOL_CALLED,
            EventType.AGENT_TOOL_COMPLETED,
        ]
        assert events[0].payload["tool_name"] == "read_file"
        assert events[0].payload["tool_call_id"] == "tool_123"
        assert events[1].payload["tool_name"] == "read_file"
        assert events[1].payload["tool_call_id"] == "tool_123"

    def test_registry_tool_observability_can_be_suppressed(self, trace_ctx, tmp_path):
        init_publisher(enabled=True, queue_size=100)
        file_path = tmp_path / "sample.txt"
        file_path.write_text("hello observability", encoding="utf-8")

        token = current_napcat_trace.set(trace_ctx)
        try:
            with suppress_tool_event_instrumentation():
                handle_function_call(
                    "read_file",
                    {"path": str(file_path)},
                    task_id="task_obs",
                    tool_call_id="tool_123",
                )
        finally:
            current_napcat_trace.reset(token)

        assert drain_queue() == []


# ---------------------------------------------------------------------------
# Store Tests
# ---------------------------------------------------------------------------

class TestStore:
    """SQLite store tests."""

    def test_tables_created(self, tmp_store):
        conn = tmp_store._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "events" in tables
        assert "traces" in tables
        assert "runtime_state" in tables
        assert "alerts" in tables

    def test_insert_and_query_event(self, tmp_store, trace_ctx):
        event = NapcatEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"text": "hello"},
            trace_id=trace_ctx.trace_id,
            chat_type=trace_ctx.chat_type,
            chat_id=trace_ctx.chat_id,
            user_id=trace_ctx.user_id,
        )
        tmp_store.insert_event(event.to_dict())

        conn = tmp_store._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM events")
        assert cursor.fetchone()[0] == 1

    def test_batch_insert(self, tmp_store, trace_ctx):
        events = []
        for i in range(10):
            e = NapcatEvent(
                event_type=EventType.MESSAGE_RECEIVED,
                payload={"index": i},
                trace_id=trace_ctx.trace_id,
            )
            events.append(e.to_dict())
        count = tmp_store.insert_events_batch(events)
        assert count == 10

    def test_upsert_trace(self, tmp_store, trace_ctx):
        tmp_store.upsert_trace({
            "trace_id": trace_ctx.trace_id,
            "session_key": trace_ctx.session_key,
            "started_at": time.time(),
            "status": "in_progress",
            "event_count": 1,
        })
        # Update
        tmp_store.upsert_trace({
            "trace_id": trace_ctx.trace_id,
            "session_key": trace_ctx.session_key,
            "started_at": time.time(),
            "ended_at": time.time(),
            "status": "completed",
            "event_count": 5,
        })
        conn = tmp_store._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT status, event_count FROM traces WHERE trace_id = ?", (trace_ctx.trace_id,))
        row = cursor.fetchone()
        assert row[0] == "completed"
        assert row[1] == 5

    def test_runtime_state(self, tmp_store):
        tmp_store.set_runtime_state("ws_status", {"connected": True})
        result = tmp_store.get_runtime_state("ws_status")
        assert result["connected"] is True

    def test_alert_lifecycle(self, tmp_store):
        alert_id = uuid.uuid4().hex
        tmp_store.insert_alert({
            "alert_id": alert_id,
            "ts": time.time(),
            "severity": "error",
            "title": "Test alert",
        })
        assert tmp_store.acknowledge_alert(alert_id) is True
        assert tmp_store.acknowledge_alert("nonexistent") is False

    def test_cleanup_expired(self, tmp_store, trace_ctx):
        old_time = time.time() - (31 * 86400)  # 31 days ago
        event = NapcatEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"text": "old"},
            trace_id=trace_ctx.trace_id,
            ts=old_time,
        )
        tmp_store.insert_event(event.to_dict())

        new_event = NapcatEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"text": "new"},
            trace_id="new_trace",
        )
        tmp_store.insert_event(new_event.to_dict())

        result = tmp_store.cleanup_expired(retention_days=30)
        assert result["events"] == 1  # Only old event deleted

        conn = tmp_store._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM events")
        assert cursor.fetchone()[0] == 1  # New event remains


# ---------------------------------------------------------------------------
# Repository Tests
# ---------------------------------------------------------------------------

class TestRepository:
    """Query repository tests."""

    def _insert_sample_trace(self, store, trace_ctx, event_types=None):
        """Insert a complete sample trace."""
        if event_types is None:
            event_types = [
                EventType.MESSAGE_RECEIVED,
                EventType.GROUP_TRIGGER_EVALUATED,
                EventType.GATEWAY_TURN_PREPARED,
                EventType.AGENT_RUN_STARTED,
                EventType.AGENT_MODEL_REQUESTED,
                EventType.AGENT_MODEL_COMPLETED,
                EventType.AGENT_TOOL_CALLED,
                EventType.AGENT_TOOL_COMPLETED,
                EventType.AGENT_RESPONSE_FINAL,
            ]

        for et in event_types:
            event = NapcatEvent(
                event_type=et,
                payload={"test": True},
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
            )
            store.insert_event(event.to_dict())

        store.upsert_trace({
            "trace_id": trace_ctx.trace_id,
            "session_key": trace_ctx.session_key,
            "session_id": trace_ctx.session_id,
            "chat_type": trace_ctx.chat_type,
            "chat_id": trace_ctx.chat_id,
            "user_id": trace_ctx.user_id,
            "message_id": trace_ctx.message_id,
            "started_at": time.time(),
            "ended_at": time.time(),
            "event_count": len(event_types),
            "status": "completed",
            "summary": {"event_types": [et.value for et in event_types]},
        })

    def test_query_events_with_filters(self, repo, trace_ctx):
        self._insert_sample_trace(repo.store, trace_ctx)
        events, total = repo.query_events(chat_id="12345")
        assert total == 9
        assert len(events) == 9

    def test_query_events_by_type(self, repo, trace_ctx):
        self._insert_sample_trace(repo.store, trace_ctx)
        events, total = repo.query_events(event_type="message.received")
        assert total == 1

    def test_query_traces(self, repo, trace_ctx):
        self._insert_sample_trace(repo.store, trace_ctx)
        traces, total = repo.query_traces(chat_id="12345")
        assert total == 1
        assert traces[0]["status"] == "completed"

    def test_query_traces_by_event_type(self, repo, trace_ctx, dm_trace_ctx):
        self._insert_sample_trace(
            repo.store,
            trace_ctx,
            event_types=[EventType.MESSAGE_RECEIVED, EventType.AGENT_TOOL_CALLED, EventType.AGENT_RESPONSE_FINAL],
        )
        self._insert_sample_trace(
            repo.store,
            dm_trace_ctx,
            event_types=[EventType.MESSAGE_RECEIVED, EventType.AGENT_RESPONSE_FINAL],
        )

        traces, total = repo.query_traces(event_type="agent.tool.called")
        assert total == 1
        assert traces[0]["trace_id"] == trace_ctx.trace_id

    def test_query_traces_by_search_and_view(self, repo, trace_ctx, dm_trace_ctx):
        self._insert_sample_trace(
            repo.store,
            trace_ctx,
            event_types=[EventType.MESSAGE_RECEIVED, EventType.AGENT_TOOL_CALLED, EventType.AGENT_RESPONSE_FINAL],
        )
        self._insert_sample_trace(
            repo.store,
            dm_trace_ctx,
            event_types=[EventType.MESSAGE_RECEIVED, EventType.ERROR_RAISED],
        )

        repo.store.upsert_trace({
            "trace_id": trace_ctx.trace_id,
            "session_key": trace_ctx.session_key,
            "session_id": trace_ctx.session_id,
            "chat_type": trace_ctx.chat_type,
            "chat_id": trace_ctx.chat_id,
            "user_id": trace_ctx.user_id,
            "message_id": trace_ctx.message_id,
            "started_at": time.time(),
            "ended_at": time.time(),
            "event_count": 3,
            "status": "completed",
            "latest_stage": "tools",
            "active_stream": False,
            "tool_call_count": 1,
            "error_count": 0,
            "model": "claude",
            "summary": {"headline": "read_file done", "event_types": ["message.received", "agent.tool.called", "agent.response.final"]},
        })
        repo.store.upsert_trace({
            "trace_id": dm_trace_ctx.trace_id,
            "session_key": dm_trace_ctx.session_key,
            "session_id": dm_trace_ctx.session_id,
            "chat_type": dm_trace_ctx.chat_type,
            "chat_id": dm_trace_ctx.chat_id,
            "user_id": dm_trace_ctx.user_id,
            "message_id": dm_trace_ctx.message_id,
            "started_at": time.time(),
            "ended_at": time.time(),
            "event_count": 2,
            "status": "error",
            "latest_stage": "error",
            "active_stream": False,
            "tool_call_count": 0,
            "error_count": 1,
            "model": "claude",
            "summary": {"headline": "boom", "event_types": ["message.received", "error.raised"]},
        })

        tool_traces, tool_total = repo.query_traces(view="tools")
        assert tool_total == 1
        assert tool_traces[0]["trace_id"] == trace_ctx.trace_id

        alert_traces, alert_total = repo.query_traces(view="alerts")
        assert alert_total == 1
        assert alert_traces[0]["trace_id"] == dm_trace_ctx.trace_id

        search_traces, search_total = repo.query_traces(search="read_file")
        assert search_total == 1
        assert search_traces[0]["trace_id"] == trace_ctx.trace_id

    def test_get_trace_with_events(self, repo, trace_ctx):
        self._insert_sample_trace(repo.store, trace_ctx)
        trace = repo.get_trace(trace_ctx.trace_id)
        assert trace is not None
        assert len(trace["events"]) == 9

    def test_query_sessions(self, repo, trace_ctx):
        self._insert_sample_trace(repo.store, trace_ctx)
        sessions, total = repo.query_sessions()
        assert total == 1
        assert sessions[0]["session_key"] == trace_ctx.session_key

    def test_get_message_events(self, repo, trace_ctx):
        self._insert_sample_trace(repo.store, trace_ctx)
        result = repo.get_message_events(trace_ctx.message_id)
        assert len(result["events"]) == 9

    def test_empty_query(self, repo):
        events, total = repo.query_events(chat_id="nonexistent")
        assert total == 0
        assert len(events) == 0

    def test_trace_not_found(self, repo):
        result = repo.get_trace("nonexistent")
        assert result is None

    def test_cursor_backfill(self, repo, trace_ctx):
        self._insert_sample_trace(repo.store, trace_ctx)
        old_ts = time.time() - 3600
        events = repo.get_events_after_cursor(old_ts)
        assert len(events) == 9

    def test_cursor_backfill_with_status_filter(self, repo, trace_ctx):
        self._insert_sample_trace(repo.store, trace_ctx)
        old_ts = time.time() - 3600
        events = repo.get_events_after_cursor(old_ts, status="completed")
        assert len(events) == 9
        assert repo.get_events_after_cursor(old_ts, status="error") == []


# ---------------------------------------------------------------------------
# Ingest Client Tests
# ---------------------------------------------------------------------------

class TestIngestClient:
    """Ingest client integration tests."""

    def test_flush_events(self, tmp_store, trace_ctx):
        init_publisher(enabled=True, queue_size=100)

        for et in [EventType.MESSAGE_RECEIVED, EventType.AGENT_RESPONSE_FINAL]:
            emit_napcat_event(et, {"test": True}, trace_ctx=trace_ctx)

        client = IngestClient(tmp_store)
        client._flush()

        conn = tmp_store._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM events")
        assert cursor.fetchone()[0] == 2

    def test_trace_created_from_events(self, tmp_store, trace_ctx):
        init_publisher(enabled=True, queue_size=100)

        emit_napcat_event(EventType.MESSAGE_RECEIVED, {"text": "hi"}, trace_ctx=trace_ctx)
        emit_napcat_event(EventType.AGENT_RESPONSE_FINAL, {"response": "hello"}, trace_ctx=trace_ctx)

        client = IngestClient(tmp_store)
        client._flush()

        conn = tmp_store._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM traces WHERE trace_id = ?", (trace_ctx.trace_id,))
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "completed"

    def test_trace_created_event_keeps_trace_in_progress(self, tmp_store, trace_ctx):
        init_publisher(enabled=True, queue_size=100)

        emit_napcat_event(
            EventType.TRACE_CREATED,
            {"message_type": "private", "segment_count": 1},
            trace_ctx=trace_ctx,
        )

        client = IngestClient(tmp_store)
        client._flush()

        trace = ObservabilityRepository(tmp_store).get_trace(trace_ctx.trace_id)
        assert trace is not None
        assert trace["status"] == "in_progress"
        assert trace["latest_stage"] == "inbound"

    def test_orchestrator_child_completed_creates_completed_trace_summary(self, tmp_store, trace_ctx):
        init_publisher(enabled=True, queue_size=100)

        emit_napcat_event(EventType.MESSAGE_RECEIVED, {"text_preview": "please analyze"}, trace_ctx=trace_ctx)
        emit_napcat_event(
            EventType.ORCHESTRATOR_CHILD_COMPLETED,
            {
                "child_id": "child-1",
                "goal": "analyze repo",
                "result_preview": "repo analysis finished",
                "summary": "repo analysis finished",
            },
            trace_ctx=trace_ctx,
        )

        client = IngestClient(tmp_store)
        client._flush()

        trace = ObservabilityRepository(tmp_store).get_trace(trace_ctx.trace_id)
        assert trace is not None
        assert trace["status"] == "completed"
        assert trace["latest_stage"] == "final"
        assert trace["summary"]["headline"] == "repo analysis finished"
        assert trace["summary"]["child_counts"]["completed"] == 1
        assert trace["summary"]["last_terminal_event"] == EventType.ORCHESTRATOR_CHILD_COMPLETED.value

    def test_orchestrator_child_failed_creates_error_trace_and_alert(self, tmp_store, trace_ctx):
        init_publisher(enabled=True, queue_size=100)

        emit_napcat_event(EventType.MESSAGE_RECEIVED, {"text_preview": "please analyze"}, trace_ctx=trace_ctx)
        emit_napcat_event(
            EventType.ORCHESTRATOR_CHILD_FAILED,
            {
                "child_id": "child-1",
                "goal": "analyze repo",
                "error": "sandbox blew up",
                "result_preview": "sandbox blew up",
            },
            trace_ctx=trace_ctx,
        )

        client = IngestClient(tmp_store)
        client._flush()

        trace = ObservabilityRepository(tmp_store).get_trace(trace_ctx.trace_id)
        assert trace is not None
        assert trace["status"] == "error"
        assert trace["latest_stage"] == "error"
        assert trace["summary"]["headline"] == "sandbox blew up"
        assert trace["summary"]["child_counts"]["failed"] == 1

        alerts, total = ObservabilityRepository(tmp_store).query_alerts()
        assert total == 1
        assert alerts[0]["trace_id"] == trace_ctx.trace_id

    def test_trace_summary_enrichment_is_cumulative(self, tmp_store, trace_ctx):
        init_publisher(enabled=True, queue_size=100)
        client = IngestClient(tmp_store)

        emit_napcat_event(
            EventType.MESSAGE_RECEIVED,
            {"text_preview": "hi"},
            trace_ctx=trace_ctx,
        )
        client._flush()

        emit_napcat_event(
            EventType.AGENT_REASONING_DELTA,
            {"content": "thinking", "sequence": 1},
            trace_ctx=trace_ctx,
        )
        emit_napcat_event(
            EventType.AGENT_TOOL_CALLED,
            {"tool_name": "read_file", "tool_call_id": "call_1"},
            trace_ctx=trace_ctx,
        )
        client._flush()

        emit_napcat_event(
            EventType.AGENT_RESPONSE_FINAL,
            {"response_preview": "done"},
            trace_ctx=trace_ctx,
        )
        client._flush()

        trace = ObservabilityRepository(tmp_store).get_trace(trace_ctx.trace_id)
        assert trace is not None
        assert trace["event_count"] == 4
        assert trace["latest_stage"] == "final"
        assert trace["active_stream"] is False
        assert trace["tool_call_count"] == 1
        assert trace["summary"]["headline"] == "done"

    def test_trace_summary_preserves_chat_name_from_inbound_event(self, tmp_store, trace_ctx):
        init_publisher(enabled=True, queue_size=100)

        emit_napcat_event(
            EventType.MESSAGE_RECEIVED,
            {"text_preview": "hi", "chat_name": "测试群"},
            trace_ctx=trace_ctx,
        )

        client = IngestClient(tmp_store)
        client._flush()

        trace = ObservabilityRepository(tmp_store).get_trace(trace_ctx.trace_id)
        assert trace is not None
        assert trace["summary"]["chat_name"] == "测试群"

    def test_trace_uses_first_non_empty_session_context_across_events(self, tmp_store):
        init_publisher(enabled=True, queue_size=100)

        trace_ctx = NapcatTraceContext(
            trace_id=uuid.uuid4().hex,
            chat_type="group",
            chat_id="12345",
            user_id="67890",
            message_id="msg_003",
            session_key="",
            session_id="",
        )
        emit_napcat_event(EventType.MESSAGE_RECEIVED, {"text_preview": "hi"}, trace_ctx=trace_ctx)

        trace_ctx.session_key = "agent:main:napcat:group:12345"
        trace_ctx.session_id = "sess_xyz"
        emit_napcat_event(
            EventType.AGENT_RESPONSE_FINAL,
            {"response_preview": "done"},
            trace_ctx=trace_ctx,
        )

        client = IngestClient(tmp_store)
        client._flush()

        trace = ObservabilityRepository(tmp_store).get_trace(trace_ctx.trace_id)
        assert trace is not None
        assert trace["session_key"] == "agent:main:napcat:group:12345"
        assert trace["session_id"] == "sess_xyz"
        assert trace["chat_id"] == "12345"

    def test_orchestrator_status_reply_completes_trace(self, tmp_store, trace_ctx):
        init_publisher(enabled=True, queue_size=100)

        emit_napcat_event(EventType.MESSAGE_RECEIVED, {"text_preview": "跑到哪了"}, trace_ctx=trace_ctx)
        emit_napcat_event(
            EventType.ORCHESTRATOR_STATUS_REPLY,
            {"reply_text": "Still working on: analyze repo."},
            trace_ctx=trace_ctx,
        )

        client = IngestClient(tmp_store)
        client._flush()

        trace = ObservabilityRepository(tmp_store).get_trace(trace_ctx.trace_id)
        assert trace is not None
        assert trace["status"] == "completed"
        assert trace["ended_at"] is not None
        assert trace["active_stream"] is False
        assert trace["summary"]["headline"] == "Still working on: analyze repo."

    def test_orchestrator_turn_completed_creates_completed_trace_summary(self, tmp_store, trace_ctx):
        init_publisher(enabled=True, queue_size=100)

        emit_napcat_event(EventType.MESSAGE_RECEIVED, {"text_preview": "ping"}, trace_ctx=trace_ctx)
        emit_napcat_event(
            EventType.ORCHESTRATOR_TURN_COMPLETED,
            {"reply_text": "quick answer"},
            trace_ctx=trace_ctx,
        )

        client = IngestClient(tmp_store)
        client._flush()

        trace = ObservabilityRepository(tmp_store).get_trace(trace_ctx.trace_id)
        assert trace is not None
        assert trace["status"] == "completed"
        assert trace["summary"]["controller_outcome"] == "completed"
        assert trace["summary"]["headline"] == "quick answer"

    def test_trace_summary_builds_timing_breakdown_for_orchestrator_only_turn(self, tmp_store, trace_ctx):
        client = IngestClient(tmp_store)
        events = [
            NapcatEvent(
                event_type=EventType.MESSAGE_RECEIVED,
                payload={
                    "text_preview": "ping",
                    "timing": {"adapter_received_at": 10.0},
                },
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=10.05,
            ),
            NapcatEvent(
                event_type=EventType.GATEWAY_TURN_PREPARED,
                payload={
                    "timing": {
                        "node": "gateway.turn_prepare",
                        "started_at": 10.0,
                        "ended_at": 11.2,
                        "duration_ms": 1200,
                    },
                },
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=11.2,
            ),
            NapcatEvent(
                event_type=EventType.ORCHESTRATOR_TURN_STARTED,
                payload={
                    "timing": {
                        "node": "gateway.inbound_preprocess",
                        "started_at": 11.2,
                        "ended_at": 12.0,
                        "duration_ms": 800,
                    },
                },
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=12.0,
            ),
            NapcatEvent(
                event_type=EventType.AGENT_MEMORY_USED,
                payload={
                    "agent_scope": "main_orchestrator",
                    "timing": {
                        "node": "orchestrator.memory_prefetch",
                        "started_at": 12.0,
                        "ended_at": 12.7,
                        "duration_ms": 700,
                    },
                },
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=12.7,
            ),
            NapcatEvent(
                event_type=EventType.AGENT_MODEL_REQUESTED,
                payload={
                    "agent_scope": "main_orchestrator",
                    "llm_request_id": "req-main",
                    "model": "router-model",
                },
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=12.8,
            ),
            NapcatEvent(
                event_type=EventType.AGENT_MODEL_COMPLETED,
                payload={
                    "agent_scope": "main_orchestrator",
                    "llm_request_id": "req-main",
                    "model": "router-model",
                    "duration_ms": 3100,
                },
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=15.9,
            ),
            NapcatEvent(
                event_type=EventType.ORCHESTRATOR_DECISION_PARSED,
                payload={"respond_now": True},
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=16.5,
            ),
            NapcatEvent(
                event_type=EventType.ORCHESTRATOR_TURN_COMPLETED,
                payload={"reply_text": "quick answer"},
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=17.0,
            ),
            NapcatEvent(
                event_type=EventType.ADAPTER_WS_ACTION,
                payload={
                    "action": "send_private_msg",
                    "success": True,
                    "duration_ms": 400,
                    "timing": {
                        "node": "response.send",
                        "started_at": 17.2,
                        "ended_at": 17.6,
                        "duration_ms": 400,
                        "note": "send_private_msg",
                    },
                },
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=17.6,
            ),
        ]

        summary = client._build_trace_summary(
            events,
            trace_analysis={
                "status": "completed",
                "ended_at": 17.0,
                "main_outcome": "completed",
                "child_counts": {},
                "last_terminal_event": EventType.ORCHESTRATOR_TURN_COMPLETED.value,
            },
        )

        timing_breakdown = summary["summary"]["timing_breakdown"]
        timing_totals = summary["summary"]["timing_totals"]

        assert [item["key"] for item in timing_breakdown] == [
            "gateway.turn_prepare",
            "gateway.inbound_preprocess",
            "orchestrator.memory_prefetch",
            "orchestrator.model",
            "orchestrator.decision_parse",
            "orchestrator.reply_finalize",
            "response.send",
        ]
        assert timing_totals["end_to_end_ms"] == 7600.0
        assert timing_totals["gateway_turn_prepare_ms"] == 1200.0
        assert timing_totals["gateway_inbound_preprocess_ms"] == 800.0
        assert timing_totals["orchestrator_memory_prefetch_ms"] == 700.0
        assert timing_totals["orchestrator_model_ms"] == 3100.0
        assert timing_totals["orchestrator_decision_parse_ms"] == 600.0
        assert timing_totals["orchestrator_reply_finalize_ms"] == 700.0
        assert timing_totals["response_send_ms"] == 400.0
        assert timing_totals["measured_ms"] == 7500.0
        assert timing_totals["unaccounted_ms"] == 100.0
        assert timing_breakdown[-1]["note"] == "send_private_msg"

    def test_trace_summary_splits_reply_finalize_when_child_work_fills_the_gap(self, tmp_store, trace_ctx):
        client = IngestClient(tmp_store)
        events = [
            NapcatEvent(
                event_type=EventType.MESSAGE_RECEIVED,
                payload={
                    "text_preview": "please inspect this",
                    "timing": {"adapter_received_at": 10.0},
                },
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=10.05,
            ),
            NapcatEvent(
                event_type=EventType.GATEWAY_TURN_PREPARED,
                payload={},
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=10.5,
            ),
            NapcatEvent(
                event_type=EventType.ORCHESTRATOR_DECISION_PARSED,
                payload={"spawn_count": 1},
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=12.0,
            ),
            NapcatEvent(
                event_type=EventType.ORCHESTRATOR_CHILD_SPAWNED,
                payload={"child_id": "child-1", "goal": "inspect"},
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=12.2,
            ),
            NapcatEvent(
                event_type=EventType.ORCHESTRATOR_CHILD_COMPLETED,
                payload={"child_id": "child-1", "result_preview": "done"},
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=40.0,
            ),
            NapcatEvent(
                event_type=EventType.ADAPTER_WS_ACTION,
                payload={
                    "action": "send_private_msg",
                    "success": True,
                    "duration_ms": 300,
                    "timing": {
                        "node": "response.send",
                        "started_at": 40.2,
                        "ended_at": 40.5,
                        "duration_ms": 300,
                        "note": "send_private_msg",
                    },
                },
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                session_id=trace_ctx.session_id,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=40.5,
            ),
        ]

        summary = client._build_trace_summary(
            events,
            trace_analysis={
                "status": "completed",
                "ended_at": 40.0,
                "main_outcome": "completed",
                "child_counts": {"started": 1, "completed": 1},
                "last_terminal_event": EventType.ORCHESTRATOR_CHILD_COMPLETED.value,
            },
        )

        timing_breakdown = summary["summary"]["timing_breakdown"]
        timing_totals = summary["summary"]["timing_totals"]

        assert [item["key"] for item in timing_breakdown] == [
            "gateway.turn_prepare",
            "orchestrator.child_dispatch",
            "orchestrator.child_wait",
            "orchestrator.reply_finalize",
            "response.send",
        ]
        assert timing_breakdown[1]["label"] == "Child Dispatch"
        assert timing_breakdown[1]["note"] == "child-1"
        assert timing_breakdown[2]["label"] == "Child Wait"
        assert timing_breakdown[2]["note"] == "child-1"
        assert timing_breakdown[3]["source_event_type"] == "adapter.ws_action"
        assert timing_totals["orchestrator_child_dispatch_ms"] == 200.0
        assert timing_totals["orchestrator_child_wait_ms"] == 27800.0
        assert timing_totals["orchestrator_reply_finalize_ms"] == 200.0
        assert timing_totals["response_send_ms"] == 300.0
        assert timing_totals["end_to_end_ms"] == 30500.0
        assert timing_totals["measured_ms"] == 29000.0
        assert timing_totals["unaccounted_ms"] == 1500.0

    def test_orchestrator_waits_for_all_children_before_completing_trace(self, tmp_store, trace_ctx):
        init_publisher(enabled=True, queue_size=100)

        emit_napcat_event(EventType.MESSAGE_RECEIVED, {"text_preview": "please analyze"}, trace_ctx=trace_ctx)
        emit_napcat_event(
            EventType.ORCHESTRATOR_CHILD_SPAWNED,
            {"child_id": "child-1", "goal": "analyze repo"},
            trace_ctx=trace_ctx,
        )
        emit_napcat_event(
            EventType.ORCHESTRATOR_CHILD_SPAWNED,
            {"child_id": "child-2", "goal": "inspect tests"},
            trace_ctx=trace_ctx,
        )
        emit_napcat_event(
            EventType.ORCHESTRATOR_CHILD_COMPLETED,
            {"child_id": "child-1", "goal": "analyze repo", "result_preview": "repo done"},
            trace_ctx=trace_ctx,
        )

        client = IngestClient(tmp_store)
        client._flush()

        trace = ObservabilityRepository(tmp_store).get_trace(trace_ctx.trace_id)
        assert trace is not None
        assert trace["status"] == "in_progress"
        assert trace["summary"]["child_counts"]["active"] == 1

        emit_napcat_event(
            EventType.ORCHESTRATOR_CHILD_COMPLETED,
            {"child_id": "child-2", "goal": "inspect tests", "result_preview": "tests done"},
            trace_ctx=trace_ctx,
        )
        client._flush()

        trace = ObservabilityRepository(tmp_store).get_trace(trace_ctx.trace_id)
        assert trace is not None
        assert trace["status"] == "completed"
        assert trace["summary"]["child_counts"]["active"] == 0
        assert trace["summary"]["child_counts"]["completed"] == 2

    def test_queued_child_keeps_trace_in_progress(self, tmp_store, trace_ctx):
        init_publisher(enabled=True, queue_size=100)

        emit_napcat_event(EventType.MESSAGE_RECEIVED, {"text_preview": "please analyze"}, trace_ctx=trace_ctx)
        emit_napcat_event(
            EventType.ORCHESTRATOR_CHILD_QUEUED,
            {"child_id": "child-1", "goal": "different task"},
            trace_ctx=trace_ctx,
        )

        client = IngestClient(tmp_store)
        client._flush()

        trace = ObservabilityRepository(tmp_store).get_trace(trace_ctx.trace_id)
        assert trace is not None
        assert trace["status"] == "in_progress"
        assert trace["summary"]["child_counts"]["active"] == 1

    def test_child_failure_beats_suppressed_reply_when_trace_finishes(self, tmp_store, trace_ctx):
        init_publisher(enabled=True, queue_size=100)

        emit_napcat_event(EventType.MESSAGE_RECEIVED, {"text_preview": "please analyze"}, trace_ctx=trace_ctx)
        emit_napcat_event(
            EventType.AGENT_RESPONSE_SUPPRESSED,
            {"reason": "no_response"},
            trace_ctx=trace_ctx,
        )
        emit_napcat_event(
            EventType.ORCHESTRATOR_CHILD_SPAWNED,
            {"child_id": "child-1", "goal": "analyze repo"},
            trace_ctx=trace_ctx,
        )
        emit_napcat_event(
            EventType.ORCHESTRATOR_CHILD_FAILED,
            {"child_id": "child-1", "error": "sandbox blew up", "result_preview": "sandbox blew up"},
            trace_ctx=trace_ctx,
        )

        client = IngestClient(tmp_store)
        client._flush()

        trace = ObservabilityRepository(tmp_store).get_trace(trace_ctx.trace_id)
        assert trace is not None
        assert trace["status"] == "error"
        assert trace["summary"]["headline"] == "sandbox blew up"

    def test_alert_created_for_error(self, tmp_store, trace_ctx):
        init_publisher(enabled=True, queue_size=100)

        emit_napcat_event(
            EventType.ERROR_RAISED,
            {"error": "something broke"},
            trace_ctx=trace_ctx,
            severity=Severity.ERROR,
        )

        client = IngestClient(tmp_store)
        client._flush()

        conn = tmp_store._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM alerts")
        assert cursor.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Sample Trace Fixtures
# ---------------------------------------------------------------------------

class TestSampleTraces:
    """Verify sample traces cover all key paths."""

    def _build_full_trace(self, trace_ctx) -> list:
        """Build a complete trace from adapter to response."""
        events = []
        t = time.time()

        event_sequence = [
            (EventType.ADAPTER_LIFECYCLE, {"action": "connected"}),
            (EventType.MESSAGE_RECEIVED, {"text_preview": "Hello bot", "segment_count": 1}),
            (EventType.MESSAGE_MEDIA_EXTRACTED, {"media_urls_count": 0, "detected_type": "text"}),
            (EventType.GROUP_TRIGGER_EVALUATED, {"trigger_reason": "mention", "group_id": "12345"}),
            (EventType.GATEWAY_TURN_PREPARED, {
                "user_message": "Hello bot",
                "platform_prompt": "NapCat prompt",
                "default_prompt": "System prompt",
                "super_admin": False,
            }),
            (EventType.AGENT_RUN_STARTED, {
                "model": "claude-sonnet-4.6",
                "session_key": "test",
                "prompt": "System prompt\n\nContext",
            }),
            (EventType.AGENT_MODEL_REQUESTED, {"model": "claude-sonnet-4.6", "streaming": True}),
            (EventType.AGENT_MODEL_COMPLETED, {"model": "claude-sonnet-4.6", "duration_ms": 1500}),
            (EventType.AGENT_TOOL_CALLED, {"tool_name": "read_file", "args_preview": '{"path": "/tmp"}'}),
            (EventType.AGENT_TOOL_COMPLETED, {"tool_name": "read_file", "duration_ms": 50, "success": True}),
            (EventType.AGENT_RESPONSE_FINAL, {
                "response_preview": "Here is the content...",
                "tools_used": ["read_file"],
                "api_call_count": 2,
            }),
        ]

        for i, (et, payload) in enumerate(event_sequence):
            event = NapcatEvent(
                event_type=et,
                payload=payload,
                trace_id=trace_ctx.trace_id,
                session_key=trace_ctx.session_key,
                chat_type=trace_ctx.chat_type,
                chat_id=trace_ctx.chat_id,
                user_id=trace_ctx.user_id,
                message_id=trace_ctx.message_id,
                ts=t + i * 0.1,
            )
            events.append(event)
        return events

    def test_full_group_accept_trace(self, tmp_store, trace_ctx):
        """Full group message: accept → agent → tool → response."""
        events = self._build_full_trace(trace_ctx)
        dicts = [e.to_dict() for e in events]
        tmp_store.insert_events_batch(dicts)

        conn = tmp_store._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM events WHERE trace_id = ?", (trace_ctx.trace_id,))
        assert cursor.fetchone()[0] == 11

    def test_group_reject_trace(self, tmp_store, trace_ctx):
        """Group message: rejected by trigger → context only."""
        events = [
            NapcatEvent(
                event_type=EventType.MESSAGE_RECEIVED,
                payload={"text_preview": "random chat"},
                trace_id=trace_ctx.trace_id,
                chat_type="group",
                chat_id="12345",
                user_id="67890",
            ),
            NapcatEvent(
                event_type=EventType.GROUP_TRIGGER_REJECTED,
                payload={
                    "rejection_layer": "trigger",
                    "matched_policy": "no_trigger",
                    "final_reason": "No mention, keyword, or LLM gate match",
                },
                trace_id=trace_ctx.trace_id,
            ),
            NapcatEvent(
                event_type=EventType.GROUP_CONTEXT_ONLY_EMITTED,
                payload={"trigger_reason": "no_trigger"},
                trace_id=trace_ctx.trace_id,
            ),
        ]
        tmp_store.insert_events_batch([e.to_dict() for e in events])

        repo = ObservabilityRepository(tmp_store)
        trace_events, _ = repo.query_events(event_type="group.trigger.rejected")
        assert len(trace_events) == 1
        assert trace_events[0]["payload"]["rejection_layer"] == "trigger"

    def test_dm_direct_response_trace(self, dm_trace_ctx, tmp_store):
        """DM with short-circuit direct response (no agent)."""
        events = [
            NapcatEvent(
                event_type=EventType.MESSAGE_RECEIVED,
                payload={"text_preview": "auth code"},
                trace_id=dm_trace_ctx.trace_id,
                chat_type="private",
            ),
            NapcatEvent(
                event_type=EventType.GATEWAY_TURN_SHORT_CIRCUITED,
                payload={"reason": "direct_response", "response_preview": "Auth accepted"},
                trace_id=dm_trace_ctx.trace_id,
            ),
        ]
        tmp_store.insert_events_batch([e.to_dict() for e in events])

        repo = ObservabilityRepository(tmp_store)
        sc, _ = repo.query_events(event_type="gateway.turn.short_circuited")
        assert len(sc) == 1

    def test_tool_failure_trace(self, tmp_store, trace_ctx):
        """Trace with a failed tool call."""
        events = [
            NapcatEvent(
                event_type=EventType.AGENT_TOOL_CALLED,
                payload={"tool_name": "execute_command", "args_preview": '{"command": "rm -rf /"}'},
                trace_id=trace_ctx.trace_id,
            ),
            NapcatEvent(
                event_type=EventType.AGENT_TOOL_FAILED,
                payload={
                    "tool_name": "execute_command",
                    "error": "Permission denied",
                    "duration_ms": 15,
                    "exit_code": 1,
                },
                trace_id=trace_ctx.trace_id,
                severity=Severity.WARN,
            ),
        ]
        tmp_store.insert_events_batch([e.to_dict() for e in events])

        repo = ObservabilityRepository(tmp_store)
        failed, _ = repo.query_events(event_type="agent.tool.failed")
        assert len(failed) == 1
        assert failed[0]["payload"]["tool_name"] == "execute_command"
