"""Napcat-specific tests extracted from test_run_progress_topics.py.

Import shared helpers from the original file to avoid duplication.
"""

import asyncio
import logging
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayOrchestratorConfig, Platform, PlatformConfig
from gateway.agent_run_hooks import GatewayAgentRunHooks
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource

# Import shared helpers from the original test module
from tests.gateway.test_run_progress_topics import (
    ProgressCaptureAdapter,
    NonEditingProgressCaptureAdapter,
    NoEditProgressCaptureAdapter,
    FakeAgent,
    PromptCaptureAgent,
    LiveObservabilityAgent,
    PromotionCommentaryAgent,
    FailIfConstructedAgent,
    ObservabilityOnlyCommentaryAgent,
    StreamingProbeAgent,
    _make_runner,
)

async def test_run_agent_interrupt_followup_propagates_latest_event_message_id(monkeypatch, tmp_path):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    InterruptThenReplyAgent.calls = 0
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = InterruptThenReplyAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = ProgressCaptureAdapter(platform=Platform.NAPCAT)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="100000001",
        chat_type="group",
        user_id="10001",
        user_name="Alice",
    )
    pending_event = MessageEvent(
        text="new prompt",
        source=source,
        message_id="new-msg",
    )
    adapter._pending_messages["agent:main:napcat:group:100000001"] = pending_event

    result = await runner._run_agent(
        message="old prompt",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-interrupt",
        session_key="agent:main:napcat:group:100000001",
        event_message_id="old-msg",
    )

    assert result["final_response"] == "handled: [Alice] new prompt"
    assert result["effective_event_message_id"] == "new-msg"

async def test_run_agent_emits_full_prompt_in_started_event(monkeypatch, tmp_path):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = PromptCaptureAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = ProgressCaptureAdapter(platform=Platform.NAPCAT)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "***", "provider": "openrouter", "api_mode": "chat_completions"},
    )

    emitted = []
    monkeypatch.setattr(
        gateway_run,
        "_emit_gateway_event",
        lambda event_type, payload, **kwargs: emitted.append((event_type, payload, kwargs)),
    )

    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="12345",
        chat_type="dm",
        user_id="user-1",
    )
    prompt = "Base prompt\n\nPromotion rules"

    result = await runner._run_agent(
        message="hello",
        context_prompt=prompt,
        history=[],
        source=source,
        session_id="sess-1",
        session_key="agent:main:napcat:dm:12345",
        promotion_stage="l1",
        runtime_hooks=GatewayAgentRunHooks(trace_ctx=SimpleNamespace(trace_id="trace-1")),
    )

    assert result["final_response"] == "done"
    started = [payload for event_type, payload, _ in emitted if event_type == gateway_run._GatewayEventType.AGENT_RUN_STARTED]
    assert started
    assert started[-1]["prompt"] == prompt
    assert started[-1]["promotion_stage"] == "l1"

async def test_run_agent_emits_live_reasoning_and_response_events(monkeypatch, tmp_path):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = LiveObservabilityAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = ProgressCaptureAdapter(platform=Platform.NAPCAT)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "***", "provider": "openrouter", "api_mode": "chat_completions"},
    )

    emitted = []
    monkeypatch.setattr(
        gateway_run,
        "_emit_gateway_event",
        lambda event_type, payload, **kwargs: emitted.append((event_type, payload, kwargs)),
    )

    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="12345",
        chat_type="dm",
        user_id="user-1",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-live",
        session_key="agent:main:napcat:dm:12345",
        runtime_hooks=GatewayAgentRunHooks(trace_ctx=SimpleNamespace(trace_id="trace-live")),
    )

    assert result["final_response"] == "hello world"
    reasoning = [
        payload for event_type, payload, _ in emitted
        if event_type == gateway_run._GatewayEventType.AGENT_REASONING_DELTA
    ]
    response = [
        payload for event_type, payload, _ in emitted
        if event_type == gateway_run._GatewayEventType.AGENT_RESPONSE_DELTA
    ]
    assert reasoning
    assert response
    assert "".join(payload["delta"] for payload in reasoning) == "step 1\nstep 2"
    assert "".join(payload["delta"] for payload in response) == "hello world"
    assert all("content" not in payload for payload in reasoning)
    assert all("content" not in payload for payload in response)


# ---------------------------------------------------------------------------
# Preview truncation tests (all/new mode respects tool_preview_length)
# ---------------------------------------------------------------------------

async def test_run_agent_sanitizes_promotion_interim_commentary(monkeypatch, tmp_path):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = PromotionCommentaryAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = NoEditProgressCaptureAdapter(platform=Platform.NAPCAT)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="100000001",
        chat_type="group",
    )

    result = await runner._run_agent(
        message="天气如何",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-promotion-commentary",
        session_key="agent:main:napcat:group:100000001",
        promotion_stage="l1",
        promotion_protocol={
            "no_reply_marker": "[[NO_REPLY]]",
            "escalate_marker": "[[ESCALATE_L2]]",
        },
    )

    assert result.get("already_sent") is not True
    assert any(call["content"] == "查一下上海现在的天气~" for call in adapter.sent)
    assert all("[[COMPLEXITY:" not in call["content"] for call in adapter.sent)

async def test_run_agent_disables_token_streaming_on_non_editing_platforms(monkeypatch, tmp_path):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = StreamingProbeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = NoEditProgressCaptureAdapter(platform=Platform.NAPCAT)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="100000001",
        chat_type="group",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-napcat-streaming",
        session_key="agent:main:napcat:group:100000001",
    )

    assert result["final_response"] == "partial reply"
    assert result.get("already_sent") is not True
    assert adapter.sent == []

async def test_napcat_direct_response_short_circuits_agent(monkeypatch, tmp_path):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FailIfConstructedAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = NoEditProgressCaptureAdapter(platform=Platform.NAPCAT)
    adapter.prepare_gateway_turn = AsyncMock(
        return_value={
            "extra_prompt": "",
            "message_prefix": "[System note: auth updated.]",
            "direct_response": "Authorized.",
            "dynamic_disabled_skills": [],
            "super_admin": False,
            "trigger_reason": "dm",
        }
    )
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        gateway_run,
        "build_session_context",
        lambda source, config, session_entry: SimpleNamespace(source=source),
    )
    monkeypatch.setattr(gateway_run, "build_session_context_prompt", lambda context, redact_pii=False: "")
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(
            session_key="agent:main:napcat:dm:10001",
            session_id="sess-auth",
            created_at=source and __import__("datetime").datetime.now(),
            updated_at=__import__("datetime").datetime.now(),
            was_auto_reset=False,
        ),
        has_any_sessions=lambda: True,
        load_transcript=lambda session_id: [],
    )
    runner._get_unauthorized_dm_behavior = lambda platform: "ignore"
    runner._is_user_authorized = lambda source: True
    runner._set_session_env = lambda context: []
    runner._clear_session_env = lambda tokens: None
    runner._run_processing_hook = AsyncMock()
    runner._session_key_for_source = lambda source: "agent:main:napcat:dm:10001"
    runner._update_prompt_pending = {}
    runner._should_send_voice_reply = lambda *args, **kwargs: False
    runner._send_voice_reply = AsyncMock()

    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="10001",
        chat_type="dm",
        user_id="10001",
        user_name="Alice",
    )
    event = MessageEvent(text="Authorize reading test group context", source=source)

    response = await runner._handle_message(event)

    assert response == "Authorized."

async def test_handle_message_stores_reply_override_from_latest_interrupted_event(monkeypatch, tmp_path):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    adapter = NoEditProgressCaptureAdapter(platform=Platform.NAPCAT)
    adapter.prepare_gateway_turn = AsyncMock(
        return_value={
            "extra_prompt": "",
            "message_prefix": "",
            "direct_response": "",
            "dynamic_disabled_skills": [],
            "dynamic_disabled_toolsets": [],
            "super_admin": False,
            "trigger_reason": "dm",
        }
    )
    runner = _make_runner(adapter)
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        gateway_run,
        "build_session_context",
        lambda source, config, session_entry: SimpleNamespace(source=source),
    )
    monkeypatch.setattr(gateway_run, "build_session_context_prompt", lambda context, redact_pii=False: "")
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(
            session_key="agent:main:napcat:dm:10001",
            session_id="sess-reply-override",
            created_at=source and __import__("datetime").datetime.now(),
            updated_at=__import__("datetime").datetime.now(),
            was_auto_reset=False,
        ),
        has_any_sessions=lambda: True,
        load_transcript=lambda session_id: [],
        update_session=lambda *args, **kwargs: None,
        append_to_transcript=lambda *args, **kwargs: None,
    )
    runner._get_unauthorized_dm_behavior = lambda platform: "ignore"
    runner._is_user_authorized = lambda source: True
    runner._set_session_env = lambda context: []
    runner._clear_session_env = lambda tokens: None
    runner._run_processing_hook = AsyncMock()
    runner._session_key_for_source = lambda source: "agent:main:napcat:dm:10001"
    runner._update_prompt_pending = {}
    runner._should_send_voice_reply = lambda *args, **kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "reply",
            "messages": [],
            "api_calls": 1,
            "effective_event_message_id": "new-msg",
        }
    )

    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="10001",
        chat_type="dm",
        user_id="10001",
        user_name="Alice",
    )
    event = MessageEvent(text="follow-up", source=source, message_id="old-msg")

    response = await runner._handle_message(event)

    assert response == "reply"
    assert event.metadata["response_reply_to_message_id"] == "new-msg"

async def test_deliver_media_from_response_uses_reply_override(monkeypatch, tmp_path):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    adapter = NoEditProgressCaptureAdapter(platform=Platform.NAPCAT)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="100000001",
        chat_type="group",
        user_id="10001",
        user_name="Alice",
    )
    event = MessageEvent(text="hello", source=source, message_id="old-msg")
    event.metadata = {"response_reply_to_message_id": "new-msg"}

    image_path = tmp_path / "demo.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

    await runner._deliver_media_from_response(f"MEDIA:{image_path}", event, adapter)

    assert adapter.sent == [
        {
            "chat_id": "100000001",
            "content": f"🖼️ Image: {image_path}",
            "reply_to": "new-msg",
            "metadata": None,
        }
    ]

async def test_deliver_media_from_response_logs_missing_media_path(monkeypatch, tmp_path, caplog):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    adapter = NoEditProgressCaptureAdapter(platform=Platform.NAPCAT)
    adapter.send_image_file = AsyncMock()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="100000001",
        chat_type="group",
        user_id="10001",
        user_name="Alice",
    )
    event = MessageEvent(text="hello", source=source, message_id="old-msg")

    missing_path = tmp_path / "missing.png"
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        await runner._deliver_media_from_response(f"MEDIA:{missing_path}", event, adapter)

    adapter.send_image_file.assert_not_awaited()
    assert "Post-stream media path missing or not a file" in caplog.text

async def test_deliver_media_from_response_logs_unsuccessful_send_result(monkeypatch, tmp_path, caplog):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    adapter = NoEditProgressCaptureAdapter(platform=Platform.NAPCAT)
    adapter.send_image_file = AsyncMock(return_value=SendResult(success=False, error="timeout"))
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="100000001",
        chat_type="group",
        user_id="10001",
        user_name="Alice",
    )
    event = MessageEvent(text="hello", source=source, message_id="old-msg")

    image_path = tmp_path / "demo.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        await runner._deliver_media_from_response(f"MEDIA:{image_path}", event, adapter)

    adapter.send_image_file.assert_awaited_once()
    assert "Post-stream media delivery failed for" in caplog.text
    assert "timeout" in caplog.text

async def test_napcat_observability_only_stream_does_not_hide_interim_commentary(monkeypatch, tmp_path):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = ObservabilityOnlyCommentaryAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = NoEditProgressCaptureAdapter(platform=Platform.NAPCAT)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "***", "provider": "openrouter", "api_mode": "chat_completions"},
    )
    monkeypatch.setattr(
        gateway_run,
        "_emit_gateway_event",
        lambda *args, **kwargs: None,
    )

    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="12345",
        chat_type="dm",
        user_id="user-1",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-napcat-commentary",
        session_key="agent:main:napcat:dm:12345",
        runtime_hooks=GatewayAgentRunHooks(trace_ctx=SimpleNamespace(trace_id="trace-live")),
    )

    assert result["final_response"] == "done"
    assert ObservabilityOnlyCommentaryAgent.last_track_streamed_flag is False
    assert any(call["content"] == "I'll inspect the repo first." for call in adapter.sent)

