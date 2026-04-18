"""Tests for gateway /yolo session scoping."""

import os

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.runtime_extension import build_default_gateway_runtime_context
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from tools.approval import clear_session, is_session_yolo_enabled


@pytest.fixture(autouse=True)
def _clean_yolo_state(monkeypatch):
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    clear_session("agent:main:telegram:dm:chat-a")
    clear_session("agent:main:telegram:dm:chat-b")
    clear_session("agent:main:napcat:group:123456")
    clear_session("agent:main:napcat:group:654321")
    yield
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    clear_session("agent:main:telegram:dm:chat-a")
    clear_session("agent:main:telegram:dm:chat-b")
    clear_session("agent:main:napcat:group:123456")
    clear_session("agent:main:napcat:group:654321")


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.session_store = None
    runner.config = None
    return runner


def _make_event(chat_id: str) -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id=f"user-{chat_id}",
        chat_id=chat_id,
        user_name="tester",
        chat_type="dm",
    )
    return MessageEvent(text="/yolo", source=source)


@pytest.mark.asyncio
async def test_yolo_command_toggles_only_current_session(monkeypatch):
    runner = _make_runner()

    event_a = _make_event("chat-a")
    session_a = runner._session_key_for_source(event_a.source)
    session_b = runner._session_key_for_source(_make_event("chat-b").source)

    result_on = await runner._handle_yolo_command(event_a)

    assert "ON" in result_on
    assert is_session_yolo_enabled(session_a) is True
    assert is_session_yolo_enabled(session_b) is False
    assert os.environ.get("HERMES_YOLO_MODE") is None

    result_off = await runner._handle_yolo_command(event_a)

    assert "OFF" in result_off
    assert is_session_yolo_enabled(session_a) is False
    assert os.environ.get("HERMES_YOLO_MODE") is None


def _make_napcat_group_source(chat_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.NAPCAT,
        user_id="10001",
        chat_id=chat_id,
        user_name="tester",
        chat_type="group",
    )


def test_napcat_group_auto_yolo_uses_configured_group_list():
    runner = _make_runner()
    runner.config = GatewayConfig(
        platforms={
            Platform.NAPCAT: PlatformConfig(
                enabled=True,
                extra={"yolo_groups": ["group:123456"]},
            )
        }
    )

    source_enabled = _make_napcat_group_source("123456")
    source_disabled = _make_napcat_group_source("654321")
    session_enabled = runner._session_key_for_source(source_enabled)
    session_disabled = runner._session_key_for_source(source_disabled)

    extension = runner._get_gateway_extension()
    extension.apply_session_defaults(source_enabled, session_enabled)
    extension.apply_session_defaults(source_disabled, session_disabled)

    assert is_session_yolo_enabled(session_enabled) is True
    assert is_session_yolo_enabled(session_disabled) is False


def test_get_gateway_extension_reuses_instance_and_runtime_context():
    runner = _make_runner()

    extension = runner._get_gateway_extension()
    same_extension = runner._get_gateway_extension()
    runtime_context = extension.runtime_context()

    assert extension is same_extension
    assert runtime_context.complexity_threshold == build_default_gateway_runtime_context().complexity_threshold
    assert callable(runtime_context.emit_event)
    assert callable(runtime_context.emit_trace_event)
    assert runtime_context.event_type_cls
    assert runtime_context.severity_cls
    assert extension.orchestrator_runtime().runtime_context is runtime_context


@pytest.mark.asyncio
async def test_yolo_command_can_disable_auto_yolo_for_configured_napcat_group():
    runner = _make_runner()
    runner.config = GatewayConfig(
        platforms={
            Platform.NAPCAT: PlatformConfig(
                enabled=True,
                extra={"yolo_groups": ["123456"]},
            )
        }
    )

    source = _make_napcat_group_source("123456")
    event = MessageEvent(text="/yolo", source=source)
    session_key = runner._session_key_for_source(source)

    runner._get_gateway_extension().apply_session_defaults(source, session_key)
    assert is_session_yolo_enabled(session_key) is True

    result_off = await runner._handle_yolo_command(event)
    assert "OFF" in result_off
    assert is_session_yolo_enabled(session_key) is False

    result_on = await runner._handle_yolo_command(event)
    assert "ON" in result_on
    assert is_session_yolo_enabled(session_key) is True
