import asyncio
import json
import time
import uuid

import pytest

from gateway.napcat_observability.repository import ObservabilityRepository
from gateway.napcat_observability.schema import EventType, NapcatEvent, Severity
from gateway.napcat_observability.store import ObservabilityStore
from gateway.napcat_observability.trace import NapcatTraceContext
from gateway.napcat_observability.ws_hub import WebSocketHub


class _FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)

    async def send_text(self, payload):
        self.messages.append(json.loads(payload))


class _HangingCloseWebSocket:
    def __init__(self):
        self.close_calls = 0
        self.started = asyncio.Event()

    async def close(self, code=1000, reason=""):
        self.close_calls += 1
        self.started.set()
        await asyncio.Event().wait()


def _make_trace_ctx(status: str = "in_progress") -> NapcatTraceContext:
    suffix = uuid.uuid4().hex[:6]
    return NapcatTraceContext(
        trace_id=f"trace_{suffix}",
        chat_type="group",
        chat_id="group_1",
        user_id=f"user_{suffix}",
        message_id=f"msg_{suffix}",
        session_key=f"napcat:group:group_1:{suffix}",
        session_id=f"sess_{suffix}",
    )


def _insert_trace(store: ObservabilityStore, trace_ctx: NapcatTraceContext, status: str, events: list[NapcatEvent]):
    for event in events:
        store.insert_event(event.to_dict())

    store.upsert_trace({
        "trace_id": trace_ctx.trace_id,
        "session_key": trace_ctx.session_key,
        "session_id": trace_ctx.session_id,
        "chat_type": trace_ctx.chat_type,
        "chat_id": trace_ctx.chat_id,
        "user_id": trace_ctx.user_id,
        "message_id": trace_ctx.message_id,
        "started_at": events[0].ts,
        "ended_at": events[-1].ts if status != "in_progress" else None,
        "event_count": len(events),
        "status": status,
        "latest_stage": events[-1].payload.get("stage", "raw"),
        "active_stream": status == "in_progress",
        "tool_call_count": 0,
        "error_count": 1 if status == "error" else 0,
        "model": "",
        "summary": {"headline": "test"},
    })


@pytest.mark.asyncio
async def test_ws_subscribe_pause_resume_and_backfill_filters(tmp_path):
    store = ObservabilityStore(db_path=tmp_path / "napcat_obs.db")
    repo = ObservabilityRepository(store)
    hub = WebSocketHub(repo)

    error_ctx = _make_trace_ctx()
    ok_ctx = _make_trace_ctx()
    now = time.time()

    error_events = [
        NapcatEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"text": "boom", "stage": "inbound"},
            trace_id=error_ctx.trace_id,
            session_key=error_ctx.session_key,
            session_id=error_ctx.session_id,
            chat_type=error_ctx.chat_type,
            chat_id=error_ctx.chat_id,
            user_id=error_ctx.user_id,
            message_id=error_ctx.message_id,
            ts=now,
        ),
        NapcatEvent(
            event_type=EventType.ERROR_RAISED,
            payload={"error": "boom", "stage": "error"},
            trace_id=error_ctx.trace_id,
            session_key=error_ctx.session_key,
            session_id=error_ctx.session_id,
            chat_type=error_ctx.chat_type,
            chat_id=error_ctx.chat_id,
            user_id=error_ctx.user_id,
            message_id=error_ctx.message_id,
            severity=Severity.ERROR,
            ts=now + 1,
        ),
    ]
    ok_events = [
        NapcatEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"text": "ok", "stage": "inbound"},
            trace_id=ok_ctx.trace_id,
            session_key=ok_ctx.session_key,
            session_id=ok_ctx.session_id,
            chat_type=ok_ctx.chat_type,
            chat_id=ok_ctx.chat_id,
            user_id=ok_ctx.user_id,
            message_id=ok_ctx.message_id,
            ts=now + 2,
        ),
        NapcatEvent(
            event_type=EventType.AGENT_RESPONSE_FINAL,
            payload={"response_preview": "done", "stage": "final"},
            trace_id=ok_ctx.trace_id,
            session_key=ok_ctx.session_key,
            session_id=ok_ctx.session_id,
            chat_type=ok_ctx.chat_type,
            chat_id=ok_ctx.chat_id,
            user_id=ok_ctx.user_id,
            message_id=ok_ctx.message_id,
            ts=now + 3,
        ),
    ]

    _insert_trace(store, error_ctx, "error", error_events)
    _insert_trace(store, ok_ctx, "completed", ok_events)

    ws_error = _FakeWebSocket()
    ws_all = _FakeWebSocket()
    await hub.connect(ws_error)
    await hub.connect(ws_all)
    await hub.subscribe_client(ws_error, {"status": "error"})

    before_error = len(ws_error.messages)
    before_all = len(ws_all.messages)

    await hub.broadcast_event(ok_events[-1].to_dict())
    assert len(ws_error.messages) == before_error
    assert len(ws_all.messages) == before_all + 1

    await hub.broadcast_event(error_events[-1].to_dict())
    assert ws_error.messages[-1]["type"] == "event.append"
    assert ws_error.messages[-1]["data"]["trace_id"] == error_ctx.trace_id

    paused_count = len(ws_error.messages)
    await hub.pause_client(ws_error)
    await hub.broadcast_event(error_events[-1].to_dict())
    assert len(ws_error.messages) == paused_count

    await hub.resume_client(ws_error)
    await hub.handle_backfill(ws_error, now - 1, {"status": "error"})
    backfill_types = [message["type"] for message in ws_error.messages[-3:]]
    assert backfill_types[-1] == "backfill.complete"
    assert "event.append" in backfill_types


@pytest.mark.asyncio
async def test_ws_trace_update_and_alert_filters(tmp_path):
    store = ObservabilityStore(db_path=tmp_path / "napcat_obs.db")
    repo = ObservabilityRepository(store)
    hub = WebSocketHub(repo)

    trace_ctx = _make_trace_ctx()
    started = time.time()
    store.upsert_trace({
        "trace_id": trace_ctx.trace_id,
        "session_key": trace_ctx.session_key,
        "session_id": trace_ctx.session_id,
        "chat_type": trace_ctx.chat_type,
        "chat_id": trace_ctx.chat_id,
        "user_id": trace_ctx.user_id,
        "message_id": trace_ctx.message_id,
        "started_at": started,
        "ended_at": None,
        "event_count": 1,
        "status": "error",
        "latest_stage": "error",
        "active_stream": False,
        "tool_call_count": 0,
        "error_count": 1,
        "model": "",
        "summary": {"headline": "error trace"},
    })

    ws = _FakeWebSocket()
    await hub.connect(ws)
    await hub.subscribe_client(ws, {"status": "error"})

    await hub.broadcast_trace_update({
        "trace_id": trace_ctx.trace_id,
        "session_key": trace_ctx.session_key,
        "session_id": trace_ctx.session_id,
        "chat_type": trace_ctx.chat_type,
        "chat_id": trace_ctx.chat_id,
        "user_id": trace_ctx.user_id,
        "message_id": trace_ctx.message_id,
        "started_at": started,
        "ended_at": started + 1,
        "event_count": 2,
        "status": "error",
        "latest_stage": "error",
        "active_stream": False,
        "tool_call_count": 0,
        "error_count": 1,
        "model": "",
        "summary": {"headline": "error trace"},
    })
    assert ws.messages[-1]["type"] == "trace.update"

    await hub.broadcast_alert({
        "alert_id": uuid.uuid4().hex,
        "trace_id": trace_ctx.trace_id,
        "event_id": uuid.uuid4().hex,
        "ts": started + 2,
        "severity": "error",
        "title": "boom",
        "detail": "detail",
        "acknowledged": 0,
    })
    assert ws.messages[-1]["type"] == "alert.raised"


@pytest.mark.asyncio
async def test_ws_close_all_clients_times_out_stuck_close(tmp_path):
    store = ObservabilityStore(db_path=tmp_path / "napcat_obs.db")
    repo = ObservabilityRepository(store)
    hub = WebSocketHub(repo)

    stuck_ws = _HangingCloseWebSocket()
    hub._clients.add(stuck_ws)

    await asyncio.wait_for(hub.close_all_clients(timeout=0.01), timeout=0.5)

    assert stuck_ws.close_calls == 1
    assert hub.client_count == 0


@pytest.mark.asyncio
async def test_ws_derive_status_handles_orchestrator_events(tmp_path):
    store = ObservabilityStore(db_path=tmp_path / "napcat_obs.db")
    repo = ObservabilityRepository(store)
    hub = WebSocketHub(repo)

    trace_ctx = _make_trace_ctx()
    ws_completed = _FakeWebSocket()
    ws_error = _FakeWebSocket()
    await hub.connect(ws_completed)
    await hub.connect(ws_error)
    await hub.subscribe_client(ws_completed, {"status": "completed"})
    await hub.subscribe_client(ws_error, {"status": "error"})

    completed_event = NapcatEvent(
        event_type=EventType.ORCHESTRATOR_CHILD_COMPLETED,
        payload={"child_id": "child-1", "result_preview": "done", "stage": "final"},
        trace_id=trace_ctx.trace_id,
        session_key=trace_ctx.session_key,
        session_id=trace_ctx.session_id,
        chat_type=trace_ctx.chat_type,
        chat_id=trace_ctx.chat_id,
        user_id=trace_ctx.user_id,
        message_id=trace_ctx.message_id,
        ts=time.time(),
    )
    failed_event = NapcatEvent(
        event_type=EventType.ORCHESTRATOR_TURN_FAILED,
        payload={"reason": "bad route", "stage": "error"},
        trace_id=trace_ctx.trace_id,
        session_key=trace_ctx.session_key,
        session_id=trace_ctx.session_id,
        chat_type=trace_ctx.chat_type,
        chat_id=trace_ctx.chat_id,
        user_id=trace_ctx.user_id,
        message_id=trace_ctx.message_id,
        severity=Severity.ERROR,
        ts=time.time() + 1,
    )

    await hub.broadcast_event(completed_event.to_dict())
    assert ws_completed.messages[-1]["data"]["event_type"] == EventType.ORCHESTRATOR_CHILD_COMPLETED.value

    status_reply_event = NapcatEvent(
        event_type=EventType.ORCHESTRATOR_STATUS_REPLY,
        payload={"reply_text": "Still working on: analyze repo.", "stage": "context"},
        trace_id=trace_ctx.trace_id,
        session_key=trace_ctx.session_key,
        session_id=trace_ctx.session_id,
        chat_type=trace_ctx.chat_type,
        chat_id=trace_ctx.chat_id,
        user_id=trace_ctx.user_id,
        message_id=trace_ctx.message_id,
        ts=time.time() + 0.5,
    )

    await hub.broadcast_event(status_reply_event.to_dict())
    assert ws_completed.messages[-1]["data"]["event_type"] == EventType.ORCHESTRATOR_STATUS_REPLY.value

    await hub.broadcast_event(failed_event.to_dict())
    assert ws_error.messages[-1]["data"]["event_type"] == EventType.ORCHESTRATOR_TURN_FAILED.value


@pytest.mark.asyncio
async def test_ws_trace_created_derives_in_progress_status(tmp_path):
    store = ObservabilityStore(db_path=tmp_path / "napcat_obs.db")
    repo = ObservabilityRepository(store)
    hub = WebSocketHub(repo)

    trace_ctx = _make_trace_ctx()
    ws_active = _FakeWebSocket()
    await hub.connect(ws_active)
    await hub.subscribe_client(ws_active, {"status": "in_progress"})

    created_event = NapcatEvent(
        event_type=EventType.TRACE_CREATED,
        payload={"message_type": "private", "stage": "inbound"},
        trace_id=trace_ctx.trace_id,
        session_key=trace_ctx.session_key,
        session_id=trace_ctx.session_id,
        chat_type=trace_ctx.chat_type,
        chat_id=trace_ctx.chat_id,
        user_id=trace_ctx.user_id,
        message_id=trace_ctx.message_id,
        ts=time.time(),
    )

    await hub.broadcast_event(created_event.to_dict())

    assert ws_active.messages[-1]["type"] == "event.append"
    assert ws_active.messages[-1]["data"]["event_type"] == EventType.TRACE_CREATED.value


@pytest.mark.asyncio
async def test_ws_in_progress_subscriber_receives_terminal_child_event(tmp_path):
    store = ObservabilityStore(db_path=tmp_path / "napcat_obs.db")
    repo = ObservabilityRepository(store)
    hub = WebSocketHub(repo)

    trace_ctx = _make_trace_ctx()
    started = time.time()
    store.upsert_trace({
        "trace_id": trace_ctx.trace_id,
        "session_key": trace_ctx.session_key,
        "session_id": trace_ctx.session_id,
        "chat_type": trace_ctx.chat_type,
        "chat_id": trace_ctx.chat_id,
        "user_id": trace_ctx.user_id,
        "message_id": trace_ctx.message_id,
        "started_at": started,
        "ended_at": None,
        "event_count": 1,
        "status": "in_progress",
        "latest_stage": "tools",
        "active_stream": False,
        "tool_call_count": 0,
        "error_count": 0,
        "model": "",
        "summary": {"headline": "running child"},
    })

    ws_active = _FakeWebSocket()
    await hub.connect(ws_active)
    await hub.subscribe_client(ws_active, {"status": "in_progress"})

    terminal_event = NapcatEvent(
        event_type=EventType.ORCHESTRATOR_CHILD_COMPLETED,
        payload={"child_id": "child-1", "result_preview": "done", "stage": "final"},
        trace_id=trace_ctx.trace_id,
        session_key=trace_ctx.session_key,
        session_id=trace_ctx.session_id,
        chat_type=trace_ctx.chat_type,
        chat_id=trace_ctx.chat_id,
        user_id=trace_ctx.user_id,
        message_id=trace_ctx.message_id,
        ts=started + 1,
    )

    before = len(ws_active.messages)
    await hub.broadcast_event(terminal_event.to_dict())

    assert len(ws_active.messages) == before + 1
    assert ws_active.messages[-1]["type"] == "event.append"
    assert ws_active.messages[-1]["data"]["event_type"] == EventType.ORCHESTRATOR_CHILD_COMPLETED.value
