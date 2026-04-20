"""Regression tests for session-scoped model/provider overrides in gateway agents.

These cover the bug where `/model ...` stored a session override, but fresh
agent constructions still resolved model/provider from global config/runtime.
That let helper agents (and cache-miss main agents) route GPT-5.4 to the wrong
provider, e.g. Nous instead of OpenAI Codex.
"""

import asyncio
import sys
import threading
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource


class _CapturingAgent:
    """Fake agent that records init kwargs for assertions."""

    last_init = None
    last_run_kwargs = None
    instances = 0

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        type(self).instances += 1
        self.tools = []

    def run_conversation(self, user_message: str, conversation_history=None, task_id=None, **kwargs):
        type(self).last_run_kwargs = dict(kwargs)
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
        }


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner.session_store = None
    runner.config = None
    runner._voice_mode = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._service_tier = None
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._background_tasks = set()
    runner._session_db = None
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._pending_approvals = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    return runner


def _codex_override():
    return {
        "model": "gpt-5.4",
        "provider": "openai-codex",
        "api_key": "***",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
    }


def _napcat_policy_override():
    return {
        "model": "qwen-max-latest",
        "provider": "openrouter",
        "api_key": "or-key-999",
        "base_url": "https://openrouter.ai/api/v1",
        "api_mode": "chat_completions",
    }


def _explode_runtime_resolution():
    raise AssertionError(
        "global runtime resolution should not run when a complete session override exists"
    )


def test_run_agent_prefers_session_override_over_global_runtime(monkeypatch):
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", _explode_runtime_resolution)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    _CapturingAgent.last_init = None
    runner = _make_runner()

    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id="cli",
        chat_name="CLI",
        chat_type="dm",
        user_id="user-1",
    )
    session_key = "agent:main:local:dm"
    runner._session_model_overrides[session_key] = _codex_override()
    runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "high"}

    result = asyncio.run(
        runner._run_agent(
            message="ping",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key=session_key,
        )
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init is not None
    assert _CapturingAgent.last_init["model"] == "gpt-5.4"
    assert _CapturingAgent.last_init["provider"] == "openai-codex"
    assert _CapturingAgent.last_init["api_mode"] == "codex_responses"
    assert _CapturingAgent.last_init["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert _CapturingAgent.last_init["api_key"] == "***"
    assert _CapturingAgent.last_init["reasoning_config"] == {"enabled": True, "effort": "high"}


def test_run_agent_moves_gateway_context_to_turn_system_context(monkeypatch):
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_key": "***",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        },
    )

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    _CapturingAgent.last_init = None
    _CapturingAgent.last_run_kwargs = None
    runner = _make_runner()
    runner._ephemeral_system_prompt = "stable system hint"

    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id="cli",
        chat_name="CLI",
        chat_type="dm",
        user_id="user-1",
    )

    result = asyncio.run(
        runner._run_agent(
            message="ping",
            context_prompt="session context",
            history=[],
            source=source,
            session_id="session-1",
            session_key="agent:main:local:dm",
            channel_prompt="channel note",
        )
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init["ephemeral_system_prompt"] == "stable system hint"
    assert _CapturingAgent.last_init["split_session_on_compress"] is False
    assert _CapturingAgent.last_run_kwargs["turn_system_context"] == "session context\n\nchannel note"
    assert "turn_user_context" not in _CapturingAgent.last_run_kwargs


def test_run_agent_passes_adapter_user_context_as_turn_user_context(monkeypatch):
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_key": "***",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        },
    )

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    _CapturingAgent.last_init = None
    _CapturingAgent.last_run_kwargs = None
    runner = _make_runner()
    runner._ephemeral_system_prompt = "stable system hint"

    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="group-1",
        chat_name="Test Group",
        chat_type="group",
        user_id="user-1",
    )

    result = asyncio.run(
        runner._run_agent(
            message="ping",
            context_prompt="session context",
            history=[{"role": "assistant", "content": "earlier"}],
            source=source,
            session_id="session-1",
            session_key="agent:main:napcat:group:group-1",
            turn_user_context="[System note: current speaker is Alice.]",
        )
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_run_kwargs["turn_system_context"] == "session context"
    assert _CapturingAgent.last_run_kwargs["turn_user_context"] == "[System note: current speaker is Alice.]"


def test_turn_override_reuses_single_cache_slot(monkeypatch):
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_key": "***",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        },
    )

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    _CapturingAgent.instances = 0
    runner = _make_runner()

    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id="cli",
        chat_name="CLI",
        chat_type="dm",
        user_id="user-1",
    )
    session_key = "agent:main:local:dm"

    asyncio.run(
        runner._run_agent(
            message="first",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key=session_key,
        )
    )
    asyncio.run(
        runner._run_agent(
            message="second",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key=session_key,
            turn_model_override={
                "model": "gpt-5.4",
                "provider": "openai-codex",
                "api_key": "***",
                "base_url": "https://chatgpt.com/backend-api/codex",
                "api_mode": "codex_responses",
            },
        )
    )

    assert _CapturingAgent.instances == 2
    assert list(runner._agent_cache.keys()) == [session_key]


def test_child_runtime_session_is_isolated_and_cache_is_cleaned_up(monkeypatch):
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_key": "***",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        },
    )

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    _CapturingAgent.last_init = None
    _CapturingAgent.last_run_kwargs = None
    runner = _make_runner()

    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id="cli",
        chat_name="CLI",
        chat_type="dm",
        user_id="user-1",
    )
    session_key = "agent:main:local:dm"
    runtime_key = f"{session_key}::child:child_123"

    result = asyncio.run(
        runner._run_agent(
            message="child work",
            context_prompt="ctx",
            history=[],
            source=source,
            session_id="session-1__orchestrator_child__child_123",
            session_key=session_key,
            session_runtime_key=runtime_key,
            parent_session_id="session-1",
        )
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init["session_id"] == "session-1__orchestrator_child__child_123"
    assert _CapturingAgent.last_init["parent_session_id"] == "session-1"
    assert runtime_key not in runner._agent_cache


@pytest.mark.asyncio
async def test_background_task_prefers_session_override_over_global_runtime(monkeypatch):
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", _explode_runtime_resolution)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    _CapturingAgent.last_init = None
    runner = _make_runner()

    adapter = AsyncMock()
    adapter.send = AsyncMock()
    adapter.extract_media = MagicMock(return_value=([], "ok"))
    adapter.extract_images = MagicMock(return_value=([], "ok"))
    runner.adapters[Platform.TELEGRAM] = adapter

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="12345",
        chat_id="67890",
        user_name="testuser",
    )
    session_key = runner._session_key_for_source(source)
    runner._session_model_overrides[session_key] = _codex_override()
    runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "high"}

    await runner._run_background_task("say hello", source, "bg_test")

    assert _CapturingAgent.last_init is not None
    assert _CapturingAgent.last_init["model"] == "gpt-5.4"
    assert _CapturingAgent.last_init["provider"] == "openai-codex"
    assert _CapturingAgent.last_init["api_mode"] == "codex_responses"
    assert _CapturingAgent.last_init["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert _CapturingAgent.last_init["api_key"] == "***"
    assert _CapturingAgent.last_init["reasoning_config"] == {"enabled": True, "effort": "high"}


def test_resolve_session_runtime_applies_napcat_policy_override(monkeypatch):
    runner = _make_runner()
    runner.adapters[Platform.NAPCAT] = MagicMock(
        get_session_model_override=MagicMock(return_value=_napcat_policy_override())
    )

    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda user_config=None: "global-model")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "anthropic",
            "api_key": "ant-key",
            "base_url": "https://api.anthropic.com",
            "api_mode": "anthropic_messages",
        },
    )

    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="123456",
        chat_type="group",
        user_id="10001",
    )

    model, runtime = runner._resolve_session_agent_runtime(
        source=source,
        session_key="agent:main:napcat:group:123456",
    )

    assert model == "qwen-max-latest"
    assert runtime["provider"] == "openrouter"
    assert runtime["api_key"] == "or-key-999"
    assert runtime["base_url"] == "https://openrouter.ai/api/v1"
    assert runtime["api_mode"] == "chat_completions"


def test_session_model_override_beats_napcat_policy_override(monkeypatch):
    runner = _make_runner()
    runner.adapters[Platform.NAPCAT] = MagicMock(
        get_session_model_override=MagicMock(return_value=_napcat_policy_override())
    )

    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda user_config=None: "global-model")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "anthropic",
            "api_key": "ant-key",
            "base_url": "https://api.anthropic.com",
            "api_mode": "anthropic_messages",
        },
    )

    session_key = "agent:main:napcat:group:123456"
    runner._session_model_overrides[session_key] = _codex_override()
    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="123456",
        chat_type="group",
        user_id="10001",
    )

    model, runtime = runner._resolve_session_agent_runtime(
        source=source,
        session_key=session_key,
    )

    assert model == "gpt-5.4"
    assert runtime["provider"] == "openai-codex"
    assert runtime["api_key"] == "***"
    assert runtime["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert runtime["api_mode"] == "codex_responses"


def test_run_agent_session_override_beats_turn_override(monkeypatch):
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", _explode_runtime_resolution)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    _CapturingAgent.last_init = None
    runner = _make_runner()

    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="123456",
        chat_type="group",
        user_id="10001",
    )
    session_key = "agent:main:napcat:group:123456"
    runner._session_model_overrides[session_key] = _codex_override()

    result = asyncio.run(
        runner._run_agent(
            message="ping",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key=session_key,
            turn_model_override={
                "model": "qwen-plus",
                "provider": "openrouter",
                "api_key": "or-key",
                "base_url": "https://openrouter.ai/api/v1",
                "api_mode": "chat_completions",
            },
        )
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init is not None
    assert _CapturingAgent.last_init["model"] == "gpt-5.4"
    assert _CapturingAgent.last_init["provider"] == "openai-codex"
    assert _CapturingAgent.last_init["api_mode"] == "codex_responses"
    assert _CapturingAgent.last_init["base_url"] == "https://chatgpt.com/backend-api/codex"


def test_run_agent_turn_override_beats_napcat_policy_override(monkeypatch):
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    runner = _make_runner()
    runner.adapters[Platform.NAPCAT] = types.SimpleNamespace(
        get_session_model_override=MagicMock(return_value=_napcat_policy_override()),
        get_pending_message=lambda session_key: None,
        send=AsyncMock(),
        SUPPORTS_MESSAGE_EDITING=False,
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda user_config=None: "global-model")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "anthropic",
            "api_key": "ant-key",
            "base_url": "https://api.anthropic.com",
            "api_mode": "anthropic_messages",
        },
    )

    _CapturingAgent.last_init = None
    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="123456",
        chat_type="group",
        user_id="10001",
    )

    result = asyncio.run(
        runner._run_agent(
            message="ping",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-2",
            session_key="agent:main:napcat:group:123456",
            turn_model_override={
                "model": "qwen-plus",
                "provider": "openrouter",
                "api_key": "turn-key",
                "base_url": "https://openrouter.ai/api/v1",
                "api_mode": "chat_completions",
            },
        )
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init is not None
    assert _CapturingAgent.last_init["model"] == "qwen-plus"
    assert _CapturingAgent.last_init["provider"] == "openrouter"
    assert _CapturingAgent.last_init["api_key"] == "turn-key"
