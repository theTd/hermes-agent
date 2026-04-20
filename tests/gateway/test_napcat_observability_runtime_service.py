import asyncio
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.napcat_observability.publisher import (
    emit_napcat_event,
    get_stats,
    shutdown_publisher,
)
from gateway.napcat_observability.runtime_service import (
    NapcatObservabilityRuntimeService,
)
from gateway.napcat_observability.schema import EventType
from gateway.napcat_observability.store import ObservabilityStore
from gateway.napcat_observability.trace import NapcatTraceContext


@pytest.fixture(autouse=True)
def _reset_publisher():
    shutdown_publisher()
    yield
    shutdown_publisher()


@pytest.mark.asyncio
async def test_runtime_service_persists_events_in_process(tmp_path, monkeypatch):
    temp_store = ObservabilityStore(db_path=tmp_path / "napcat_obs.db")
    monkeypatch.setattr(
        "gateway.napcat_observability.runtime_service.ObservabilityStore",
        lambda: temp_store,
    )

    service = NapcatObservabilityRuntimeService(
        {
            "enabled": True,
            "queue_size": 64,
            "retention_days": 30,
        },
        serve_http=False,
    )

    started = await service.start()
    assert started is True
    assert get_stats()["enabled"] is True

    trace_ctx = NapcatTraceContext(
        trace_id=uuid.uuid4().hex,
        chat_type="private",
        chat_id="12345",
        user_id="12345",
        message_id="msg_001",
        session_key="napcat:private:12345",
        session_id="sess_123",
    )
    emit_napcat_event(EventType.MESSAGE_RECEIVED, {"text": "hello"}, trace_ctx=trace_ctx)

    service.ingest_client._flush()

    conn = temp_store._get_conn()
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 1

    await service.stop()
    assert get_stats()["enabled"] is False


@pytest.mark.asyncio
async def test_runtime_service_starts_http_server_when_enabled(tmp_path, monkeypatch):
    temp_store = ObservabilityStore(db_path=tmp_path / "napcat_obs.db")
    monkeypatch.setattr(
        "gateway.napcat_observability.runtime_service.ObservabilityStore",
        lambda: temp_store,
    )

    called = {"http": False}

    async def _fake_start_http(self):
        called["http"] = True

    monkeypatch.setattr(
        NapcatObservabilityRuntimeService,
        "_start_http_server",
        _fake_start_http,
    )

    service = NapcatObservabilityRuntimeService(
        {"enabled": True, "queue_size": 16},
        serve_http=True,
    )

    await service.start()
    assert called["http"] is True

    await service.stop()


@pytest.mark.asyncio
async def test_runtime_service_tolerates_http_app_system_exit(tmp_path, monkeypatch):
    temp_store = ObservabilityStore(db_path=tmp_path / "napcat_obs.db")
    monkeypatch.setattr(
        "gateway.napcat_observability.runtime_service.ObservabilityStore",
        lambda: temp_store,
    )

    def _boom(*args, **kwargs):
        raise SystemExit("missing fastapi")

    monkeypatch.setattr(
        "hermes_cli.napcat_observability_server.create_app",
        _boom,
    )

    service = NapcatObservabilityRuntimeService(
        {"enabled": True, "queue_size": 16},
        serve_http=True,
    )

    started = await service.start()
    assert started is True
    assert service._http_task is None

    await service.stop()


@pytest.mark.asyncio
async def test_runtime_service_tolerates_http_server_system_exit(tmp_path, monkeypatch):
    temp_store = ObservabilityStore(db_path=tmp_path / "napcat_obs.db")
    monkeypatch.setattr(
        "gateway.napcat_observability.runtime_service.ObservabilityStore",
        lambda: temp_store,
    )

    class _BoomServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False
            self.install_signal_handlers = None

        async def serve(self):
            raise SystemExit(1)

    fake_uvicorn = SimpleNamespace(
        Config=lambda *args, **kwargs: SimpleNamespace(args=args, kwargs=kwargs),
        Server=_BoomServer,
    )
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(
        "hermes_cli.napcat_observability_server.create_app",
        lambda **kwargs: object(),
    )

    service = NapcatObservabilityRuntimeService(
        {"enabled": True, "queue_size": 16},
        serve_http=True,
    )

    started = await service.start()
    assert started is True
    assert service._http_task is not None

    await service._http_task

    await service.stop()


@pytest.mark.asyncio
async def test_runtime_service_stop_closes_websocket_clients_before_waiting_for_http_task():
    service = NapcatObservabilityRuntimeService(
        {"enabled": True},
        serve_http=True,
    )
    service._started = True
    service._runtime_loop = asyncio.get_running_loop()
    service._http_loop = asyncio.get_running_loop()
    service._http_server = SimpleNamespace(should_exit=False)
    stop_gate = asyncio.Event()

    service._http_task = asyncio.create_task(stop_gate.wait())
    hub = SimpleNamespace(
        close_all_clients=AsyncMock(side_effect=lambda: stop_gate.set())
    )
    service._ws_hub = hub
    service.ingest_client = SimpleNamespace(stop=lambda: None)
    service.store = SimpleNamespace(close=lambda: None)

    await service.stop()

    hub.close_all_clients.assert_awaited_once()
    assert service._http_server is None
    assert service._http_task is None


@pytest.mark.asyncio
async def test_runtime_service_stop_times_out_stuck_websocket_shutdown(monkeypatch):
    service = NapcatObservabilityRuntimeService(
        {"enabled": True},
        serve_http=True,
    )
    service._started = True
    service._runtime_loop = asyncio.get_running_loop()
    service._http_loop = asyncio.get_running_loop()
    service._http_server = SimpleNamespace(should_exit=False)
    http_task = asyncio.get_running_loop().create_future()
    http_task.set_result(None)
    service._http_task = http_task

    stuck = asyncio.Event()

    async def _never_close():
        await stuck.wait()

    service._ws_hub = SimpleNamespace(close_all_clients=_never_close)
    service.ingest_client = SimpleNamespace(stop=lambda: None)
    service.store = SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(
        "gateway.napcat_observability.runtime_service._WS_CLIENT_SHUTDOWN_TIMEOUT_SECONDS",
        0.01,
    )

    await asyncio.wait_for(service.stop(), timeout=0.5)

    assert service._ws_hub is None
    assert service._http_server is None
    assert service._http_task is None
