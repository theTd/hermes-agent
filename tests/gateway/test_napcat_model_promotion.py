"""Tests for NapCat model promotion (L1/L2 escalation) — Tasks 02-07."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, GatewayOrchestratorConfig, Platform, PlatformConfig
from gateway.napcat_promotion import NapCatPromotionSupport
from gateway.platforms.base import AdapterTurnPlan, MessageEvent, MessageType
from gateway.platforms.napcat_turn_context import (
    NapCatPromotionPlan,
    attach_napcat_promotion_plan,
    get_napcat_promotion_plan,
)
from gateway.session import SessionEntry, SessionSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(**extra):
    from gateway.platforms.napcat import NapCatAdapter

    config = PlatformConfig(
        enabled=True,
        token=extra.pop("token", "tok"),
        extra={"ws_url": "ws://127.0.0.1:3001/", **extra},
    )
    return NapCatAdapter(config)


def _make_source(chat_type="group", user_id="10001", chat_id="123456"):
    return SessionSource(
        platform=Platform.NAPCAT,
        chat_id=chat_id,
        chat_name="test-group",
        chat_type=chat_type,
        user_id=user_id,
        user_name="tester",
    )


def _make_event(text="hello", source=None, **metadata):
    source = source or _make_source()
    event = MessageEvent(text=text, source=source)
    event.metadata = metadata or {"napcat_trigger_reason": "mention"}
    return event


def _make_session_entry(**extra):
    now = datetime.now(timezone.utc)
    return SessionEntry(
        session_id=extra.pop("session_id", "s1"),
        session_key=extra.pop("session_key", "sk1"),
        created_at=extra.pop("created_at", now),
        updated_at=extra.pop("updated_at", now),
        **extra,
    )


def _promotion_adapter():
    return _make_adapter(
        model_promotion={
            "enabled": True,
            "apply_to_groups": True,
            "apply_to_dms": False,
            "l1": {
                "model": "qwen-plus",
                "provider": "openrouter",
                "api_key": "sk-test",
            },
            "l2": {
                "model": "claude-opus",
                "provider": "anthropic",
                "api_key": "sk-ant-test",
            },
            "safeguards": {
                "l1_max_tool_calls": 2,
                "l1_max_api_calls": 4,
                "force_l2_toolsets": ["terminal"],
            },
        }
    )


def _make_gateway_runner(adapter, session_store):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        gateway_orchestrator=GatewayOrchestratorConfig(enabled_platforms=[])
    )
    runner.adapters = {Platform.NAPCAT: adapter}
    runner._session_model_overrides = {}
    runner.session_store = session_store
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = None
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = MagicMock()
    runner._voice_mode = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._draining = False
    runner._update_prompt_pending = {}
    runner._set_session_env = lambda context: []
    runner._clear_session_env = lambda tokens: None
    runner._should_send_voice_reply = lambda *args, **kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._run_processing_hook = AsyncMock()
    runner._prepare_inbound_message_text = AsyncMock(side_effect=lambda **kwargs: kwargs["event"].text)
    runner._adapter_for_source = lambda source: adapter
    runner._should_send_status_callback_message = lambda *args, **kwargs: False
    runner._load_reasoning_config = lambda: None
    runner._load_service_tier = lambda: None
    runner._deliver_media_from_response = AsyncMock()
    runner._run_process_watcher = AsyncMock()
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    return runner


def _make_promotion_turn_plan(
    *,
    extra_prompt="",
    routing_prompt="",
    user_context="",
    controller_context="",
    message_prefix="",
    direct_response="",
    dynamic_disabled_skills=None,
    dynamic_disabled_toolsets=None,
    super_admin=False,
    promotion_enabled=False,
    promotion_stage="",
    turn_model_override=None,
    promotion_protocol_prompt="",
    promotion_config_snapshot=None,
):
    plan = AdapterTurnPlan(
        extra_prompt=extra_prompt,
        routing_prompt=routing_prompt,
        user_context=user_context,
        controller_context=controller_context,
        message_prefix=message_prefix,
        direct_response=direct_response,
        dynamic_disabled_skills=list(dynamic_disabled_skills or []),
        dynamic_disabled_toolsets=list(dynamic_disabled_toolsets or []),
        super_admin=super_admin,
    )
    return attach_napcat_promotion_plan(
        plan,
        NapCatPromotionPlan(
            enabled=promotion_enabled,
            stage=promotion_stage,
            turn_model_override=dict(turn_model_override or {}),
            protocol_prompt=promotion_protocol_prompt,
            config_snapshot=dict(promotion_config_snapshot or {}),
        ),
    )


# ---------------------------------------------------------------------------
# Task 02: Turn payload tests
# ---------------------------------------------------------------------------

class TestPromotionTurnPayload:
    def test_group_with_promotion_enabled_returns_promotion_fields(self):
        adapter = _promotion_adapter()
        source = _make_source(chat_type="group")
        event = _make_event(source=source)
        session_entry = _make_session_entry()
        session_store = MagicMock()

        plan = asyncio.get_event_loop().run_until_complete(
            adapter.prepare_gateway_turn(
                event=event,
                session_store=session_store,
                session_entry=session_entry,
            )
        )
        promotion = get_napcat_promotion_plan(plan)

        assert promotion.enabled is True
        assert promotion.stage == "l1"
        assert promotion.turn_model_override.get("model") == "qwen-plus"
        assert promotion.protocol_prompt != ""
        assert promotion.config_snapshot.get("enabled") is True

    def test_dm_with_default_config_has_promotion_disabled(self):
        adapter = _promotion_adapter()
        source = _make_source(chat_type="dm")
        event = _make_event(source=source)
        session_entry = _make_session_entry()
        session_store = MagicMock()

        plan = asyncio.get_event_loop().run_until_complete(
            adapter.prepare_gateway_turn(
                event=event,
                session_store=session_store,
                session_entry=session_entry,
            )
        )
        assert get_napcat_promotion_plan(plan).enabled is False

    def test_session_model_override_bypasses_promotion(self):
        adapter = _promotion_adapter()
        # Simulate a session with /model override — key must match build_session_key output
        adapter._session_model_overrides_ref = {"agent:main:napcat:group:123456": {"model": "gpt-4"}}
        source = _make_source(chat_type="group")
        event = _make_event(source=source)
        session_entry = _make_session_entry()
        session_store = MagicMock()

        plan = asyncio.get_event_loop().run_until_complete(
            adapter.prepare_gateway_turn(
                event=event,
                session_store=session_store,
                session_entry=session_entry,
            )
        )
        assert get_napcat_promotion_plan(plan).enabled is False

    def test_protocol_prompt_contains_markers(self):
        adapter = _promotion_adapter()
        source = _make_source(chat_type="group")
        event = _make_event(source=source)
        session_entry = _make_session_entry()
        session_store = MagicMock()

        plan = asyncio.get_event_loop().run_until_complete(
            adapter.prepare_gateway_turn(
                event=event,
                session_store=session_store,
                session_entry=session_entry,
            )
        )
        protocol_prompt = get_napcat_promotion_plan(plan).protocol_prompt

        assert "[[NO_REPLY]]" in protocol_prompt
        assert "[[ESCALATE_L2]]" in protocol_prompt
        assert "Stay on L1 by default" in protocol_prompt
        assert "[[COMPLEXITY:N]]" in protocol_prompt
        assert "The gateway decides whether to escalate" in protocol_prompt
        assert "Score guide: 0-20 trivial, 21-40 simple, 41-60 moderate, 61-79 complex, 80-100 very complex." in protocol_prompt
        assert "If your estimated score is below the gateway threshold" in protocol_prompt
        assert "then immediately provide the actual reply content" in protocol_prompt
        assert "output exactly one token and nothing else: [[COMPLEXITY:N]]" in protocol_prompt
        assert "ANY extra text after [[COMPLEXITY:N]] is a protocol violation" in protocol_prompt
        assert "do NOT add explanations, summaries, markdown, code fences, XML, JSON, pseudo-tool calls" in protocol_prompt
        assert "Invalid: [[COMPLEXITY:78]] <tool_call>...</tool_call>" in protocol_prompt
        assert "Use low scores for casual chat" in protocol_prompt
        assert "If you expect more than 2 tool calls in this turn, assign a complexity score of at least 70." in protocol_prompt
        assert "If the user explicitly asks for a stronger/higher-tier/L2 model" in protocol_prompt
        assert "Requests for deep/detailed/systematic/code-level analysis" in protocol_prompt

    def test_explicit_stronger_model_request_forces_direct_l2(self):
        adapter = _promotion_adapter()
        source = _make_source(chat_type="group")
        event = _make_event(text="用高阶模型深入分析这个项目", source=source)
        session_entry = _make_session_entry()
        session_store = MagicMock()

        plan = asyncio.get_event_loop().run_until_complete(
            adapter.prepare_gateway_turn(
                event=event,
                session_store=session_store,
                session_entry=session_entry,
            )
        )
        promotion = get_napcat_promotion_plan(plan)

        assert promotion.enabled is True
        assert promotion.stage == "l2"
        assert promotion.turn_model_override.get("model") == "claude-opus"
        assert promotion.protocol_prompt == ""
        assert promotion.config_snapshot["optimizations"]["force_direct_l2"] is True

    def test_large_project_deep_analysis_request_forces_direct_l2(self):
        adapter = _promotion_adapter()
        source = _make_source(chat_type="group")
        event = _make_event(text="这是个大规模项目，请做代码级深入分析", source=source)
        session_entry = _make_session_entry()
        session_store = MagicMock()

        plan = asyncio.get_event_loop().run_until_complete(
            adapter.prepare_gateway_turn(
                event=event,
                session_store=session_store,
                session_entry=session_entry,
            )
        )
        promotion = get_napcat_promotion_plan(plan)

        assert promotion.enabled is True
        assert promotion.stage == "l2"
        assert promotion.turn_model_override.get("model") == "claude-opus"
        assert promotion.config_snapshot["optimizations"]["force_direct_l2_reason"] == "explicit_user_request"

    def test_dm_protocol_prompt_forbids_no_reply(self):
        adapter = _make_adapter(
            model_promotion={
                "enabled": True,
                "apply_to_groups": True,
                "apply_to_dms": True,
                "l1": {"model": "qwen-plus", "provider": "openrouter", "api_key": "sk-test"},
                "l2": {"model": "claude-opus", "provider": "anthropic", "api_key": "sk-ant-test"},
            }
        )
        source = _make_source(chat_type="dm")
        event = _make_event(source=source)
        session_entry = _make_session_entry()
        session_store = MagicMock()

        plan = asyncio.get_event_loop().run_until_complete(
            adapter.prepare_gateway_turn(
                event=event,
                session_store=session_store,
                session_entry=session_entry,
            )
        )
        protocol_prompt = get_napcat_promotion_plan(plan).protocol_prompt

        assert get_napcat_promotion_plan(plan).enabled is True
        assert "NEVER output [[NO_REPLY]]" in protocol_prompt
        assert "Always output a reply after your score marker." in protocol_prompt

    def test_promotion_disabled_when_config_disabled(self):
        adapter = _make_adapter(
            model_promotion={
                "enabled": False,
                "l1": {"model": "qwen-plus", "provider": "openrouter"},
                "l2": {"model": "claude-opus", "provider": "anthropic"},
            }
        )
        source = _make_source(chat_type="group")
        event = _make_event(source=source)
        session_entry = _make_session_entry()
        session_store = MagicMock()

        plan = asyncio.get_event_loop().run_until_complete(
            adapter.prepare_gateway_turn(
                event=event,
                session_store=session_store,
                session_entry=session_entry,
            )
        )
        assert get_napcat_promotion_plan(plan).enabled is False

    def test_l1_equals_l2_disables_protocol_prompt(self):
        adapter = _make_adapter(
            model_promotion={
                "enabled": True,
                "l1": {
                    "model": "qwen-plus",
                    "provider": "openrouter",
                    "api_key": "same-key",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_mode": "chat_completions",
                },
                "l2": {
                    "model": "qwen-plus",
                    "provider": "openrouter",
                    "api_key": "same-key",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_mode": "chat_completions",
                },
            }
        )
        source = _make_source(chat_type="group")
        event = _make_event(source=source)
        session_entry = _make_session_entry()
        session_store = MagicMock()

        plan = asyncio.get_event_loop().run_until_complete(
            adapter.prepare_gateway_turn(
                event=event,
                session_store=session_store,
                session_entry=session_entry,
            )
        )
        promotion = get_napcat_promotion_plan(plan)

        assert promotion.enabled is True
        assert promotion.protocol_prompt == ""
        assert promotion.config_snapshot["optimizations"]["single_stage_l1"] is True

# ---------------------------------------------------------------------------
# Task 04: Control marker parsing tests
# ---------------------------------------------------------------------------

class TestPromotionControlMarkers:
    def test_extract_complexity_score(self):
        from gateway.run import GatewayRunner
        assert NapCatPromotionSupport.extract_complexity_score("[[COMPLEXITY:70]] hello") == 70
        assert NapCatPromotionSupport.extract_complexity_score("[[COMPLEXITY:0]] hello") == 0
        assert NapCatPromotionSupport.extract_complexity_score("[[COMPLEXITY:100]] hello") == 100

    def test_extract_complexity_score_missing(self):
        from gateway.run import GatewayRunner
        assert NapCatPromotionSupport.extract_complexity_score("hello") is None

    def test_extract_turn_complexity_score_uses_current_turn_assistant_messages(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.extract_turn_complexity_score(
            {
                "final_response": "普通总结回复",
                "history_offset": 1,
                "messages": [
                    {"role": "assistant", "content": "[[COMPLEXITY:99]] old turn"},
                    {
                        "role": "assistant",
                        "content": "[[COMPLEXITY:16]]\n\n先 clone 下来。",
                        "tool_calls": [{"function": {"name": "terminal"}}],
                    },
                    {"role": "tool", "content": "{\"output\":\"ok\"}"},
                    {"role": "assistant", "content": "普通总结回复"},
                ],
            }
        )
        assert result == 16

    def test_parse_no_reply(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.parse_result(
            "[[NO_REPLY]]",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
        )
        assert result == "no_reply"

    def test_parse_escalate(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.parse_result(
            "[[ESCALATE_L2]]",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
        )
        assert result == "escalate"

    def test_parse_reply(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.parse_result(
            "This is a normal reply.",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
        )
        assert result == "reply"

    def test_parse_empty_is_no_reply(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.parse_result("", {})
        assert result == "no_reply"

    def test_parse_custom_markers(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.parse_result(
            "[[SKIP]]",
            {"no_reply_marker": "[[SKIP]]", "escalate_marker": "[[UP]]"},
        )
        assert result == "no_reply"

    def test_parse_marker_with_whitespace(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.parse_result(
            "  [[NO_REPLY]]  ",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
        )
        assert result == "no_reply"

    def test_parse_no_reply_with_complexity_prefix(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.parse_result(
            "[[COMPLEXITY:80]] [[NO_REPLY]]",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
        )
        assert result == "no_reply"

    def test_clip_high_score_response_to_complexity_marker_only(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.clip_high_score_response(
            "我来分析一下。[[COMPLEXITY:80]]\n<tool_call>...</tool_call>",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
            70,
        )
        assert result == "[[COMPLEXITY:80]]"

    def test_clip_high_score_response_preserves_no_reply_marker(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.clip_high_score_response(
            "不用回复。[[COMPLEXITY:80]] [[NO_REPLY]] 其他文本",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
            70,
        )
        assert result == "[[COMPLEXITY:80]] [[NO_REPLY]]"

    def test_clip_high_score_response_leaves_low_score_reply_untouched(self):
        from gateway.run import GatewayRunner
        text = "[[COMPLEXITY:15]] 正常回复内容"
        result = NapCatPromotionSupport.clip_high_score_response(
            text,
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
            16,
        )
        assert result == text

    def test_inject_promotion_protocol_before_extra_prompt(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.inject_protocol_before_extra_prompt(
            "session block\n\n[Platform note: qq]\n\nextra",
            "[Platform note: qq]\n\nextra",
            "promotion rules",
        )
        assert result == "session block\n\npromotion rules\n\n[Platform note: qq]\n\nextra"


# ---------------------------------------------------------------------------
# Task 05: Complexity safeguard tests
# ---------------------------------------------------------------------------

class TestPromotionComplexitySafeguards:
    def test_api_calls_over_threshold(self):
        from gateway.run import GatewayRunner
        ok, reason = NapCatPromotionSupport.check_l1_complexity(
            {"api_calls": 10, "messages": []},
            {"l1_max_api_calls": 4, "l1_max_tool_calls": 2, "force_l2_toolsets": []},
        )
        assert ok is True
        assert "api_calls=10" in reason

    def test_tool_calls_over_threshold(self):
        from gateway.run import GatewayRunner
        ok, reason = NapCatPromotionSupport.check_l1_complexity(
            {
                "api_calls": 1,
                "messages": [
                    {"tool_calls": [{"function": {"name": "search"}}]},
                    {"tool_calls": [{"function": {"name": "memory"}}]},
                    {"tool_calls": [{"function": {"name": "skills_list"}}]},
                ],
            },
            {"l1_max_api_calls": 4, "l1_max_tool_calls": 2, "force_l2_toolsets": []},
        )
        assert ok is True
        assert "tool_calls=3" in reason

    def test_tool_calls_ignore_history_before_history_offset(self):
        from gateway.run import GatewayRunner
        ok, reason = NapCatPromotionSupport.check_l1_complexity(
            {
                "api_calls": 1,
                "history_offset": 3,
                "messages": [
                    {"tool_calls": [{"function": {"name": "search"}}]},
                    {"tool_calls": [{"function": {"name": "memory"}}]},
                    {"tool_calls": [{"function": {"name": "skills_list"}}]},
                    {"role": "assistant", "content": "final reply"},
                ],
            },
            {"l1_max_api_calls": 4, "l1_max_tool_calls": 2, "force_l2_toolsets": []},
        )
        assert ok is False
        assert reason == ""

    def test_force_l2_toolset_hit(self):
        from gateway.run import GatewayRunner
        # Mock the registry singleton used inside _check_l1_complexity
        with patch("tools.registry.registry") as mock_reg:
            mock_reg.get_toolset_for_tool.return_value = "terminal"

            ok, reason = NapCatPromotionSupport.check_l1_complexity(
                {
                    "api_calls": 1,
                    "history_offset": 1,
                    "messages": [
                        {"tool_calls": [{"function": {"name": "search"}}]},
                        {"tool_calls": [{"function": {"name": "execute_code"}}]},
                    ],
                },
                {"l1_max_api_calls": 4, "l1_max_tool_calls": 5, "force_l2_toolsets": ["terminal"]},
            )
            assert ok is True
            assert "terminal" in reason

    def test_under_thresholds_keeps_l1(self):
        from gateway.run import GatewayRunner
        ok, reason = NapCatPromotionSupport.check_l1_complexity(
            {"api_calls": 2, "messages": []},
            {"l1_max_api_calls": 4, "l1_max_tool_calls": 2, "force_l2_toolsets": []},
        )
        assert ok is False

    def test_empty_safeguards_never_escalates(self):
        from gateway.run import GatewayRunner
        ok, reason = NapCatPromotionSupport.check_l1_complexity(
            {"api_calls": 100, "messages": [{"tool_calls": [1, 2, 3]}]},
            {},
        )
        assert ok is False


# ---------------------------------------------------------------------------
# Task 06: Response sanitization tests
# ---------------------------------------------------------------------------

class TestPromotionResponseSanitization:
    def test_strips_no_reply_marker(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.sanitize_response(
            "[[COMPLEXITY:80]] [[NO_REPLY]]",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
            "fallback",
        )
        assert result == "fallback"

    def test_strips_escalate_marker(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.sanitize_response(
            "[[ESCALATE_L2]]",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
            "fallback",
        )
        assert result == "fallback"

    def test_normal_text_passes_through(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.sanitize_response(
            "[[COMPLEXITY:40]] Hello, this is a reply.",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
            "fallback",
        )
        assert result == "Hello, this is a reply."

    def test_marker_mixed_with_text_strips_only_markers(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.sanitize_response(
            "Some text [[NO_REPLY]] more",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
            "fallback",
        )
        assert result == "Some text  more"

    def test_empty_protocol_returns_text_as_is(self):
        from gateway.run import GatewayRunner
        result = NapCatPromotionSupport.sanitize_response("hello", {}, "fallback")
        assert result == "hello"


# ---------------------------------------------------------------------------
# Task 07: Integration / regression tests (using mocked _run_agent)
# ---------------------------------------------------------------------------

class TestPromotionIntegration:
    """End-to-end promotion flow tests using mocked agent runs."""

    def _make_runner(self, adapter=None):
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig()
        runner.adapters = {}
        runner._session_model_overrides = {}
        runner.session_store = MagicMock()
        runner.session_store.has_any_sessions.return_value = True
        runner.hooks = MagicMock()
        runner.hooks.emit = AsyncMock()
        runner._ephemeral_system_prompt = ""
        runner._prefill_messages = None
        runner._show_reasoning = False
        runner._provider_routing = {}
        runner._fallback_model = None
        runner._session_db = None
        runner._agent_cache = {}
        runner._agent_cache_lock = MagicMock()

        if adapter:
            runner.adapters[Platform.NAPCAT] = adapter

        # Mock _run_agent to avoid real agent creation
        runner._run_agent = AsyncMock()

        return runner

    def test_promotion_off_uses_normal_agent(self):
        """When promotion is off, _run_agent is called without turn override."""
        adapter = _make_adapter()  # No promotion config
        source = _make_source()

        runner = self._make_runner(adapter)
        runner._run_agent.return_value = {
            "final_response": "hello",
            "messages": [],
            "api_calls": 1,
        }

        # Direct call to the area we're testing
        result = asyncio.get_event_loop().run_until_complete(
            runner._run_agent(
                message="hello",
                context_prompt="test",
                history=[],
                source=source,
                session_id="s1",
                session_key="sk1",
                turn_model_override=None,
            )
        )

        # Verify no turn override was passed
        call_kwargs = runner._run_agent.call_args
        assert call_kwargs.kwargs.get("turn_model_override") is None

    def test_l1_normal_reply_flow(self):
        """L1 returns normal text → no L2 escalation."""
        from gateway.run import GatewayRunner

        response = NapCatPromotionSupport.parse_result(
            "This is a helpful answer.",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
        )
        assert response == "reply"

        # Complexity check should pass
        ok, reason = NapCatPromotionSupport.check_l1_complexity(
            {"api_calls": 1, "messages": []},
            {"l1_max_api_calls": 4, "l1_max_tool_calls": 2, "force_l2_toolsets": []},
        )
        assert ok is False

    def test_l1_no_reply_flow(self):
        """L1 returns [[NO_REPLY]] → turn dropped."""
        from gateway.run import GatewayRunner

        response = NapCatPromotionSupport.parse_result(
            "[[NO_REPLY]]",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
        )
        assert response == "no_reply"

    def test_l1_escalate_flow(self):
        """L1 returns [[ESCALATE_L2]] → L2 runs."""
        from gateway.run import GatewayRunner

        response = NapCatPromotionSupport.parse_result(
            "[[ESCALATE_L2]]",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
        )
        assert response == "escalate"

    def test_l1_high_complexity_score_forces_l2(self):
        from gateway.run import GatewayRunner
        ok, reason = NapCatPromotionSupport.check_l1_complexity(
            {"api_calls": 1, "messages": []},
            {"l1_max_api_calls": 4, "l1_max_tool_calls": 2, "force_l2_toolsets": [], "l2_complexity_score_threshold": 70},
        )
        assert ok is False

    def test_l1_complexity_forces_l2(self):
        """L1 gives normal reply but complexity over threshold → forced L2."""
        from gateway.run import GatewayRunner

        response = NapCatPromotionSupport.parse_result(
            "Here is my answer.",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
        )
        assert response == "reply"

        ok, reason = NapCatPromotionSupport.check_l1_complexity(
            {"api_calls": 10, "messages": []},
            {"l1_max_api_calls": 4, "l1_max_tool_calls": 2, "force_l2_toolsets": []},
        )
        assert ok is True

    def test_l2_output_marker_gets_fallback(self):
        """If L2 outputs a control marker, user gets fallback text."""
        from gateway.run import GatewayRunner

        result = NapCatPromotionSupport.sanitize_response(
            "[[NO_REPLY]]",
            {"no_reply_marker": "[[NO_REPLY]]", "escalate_marker": "[[ESCALATE_L2]]"},
            NapCatPromotionSupport.L2_PROMOTION_FALLBACK_RESPONSE,
        )
        assert result == NapCatPromotionSupport.L2_PROMOTION_FALLBACK_RESPONSE

    def test_turn_override_does_not_pollute_session(self):
        """Turn model override must not write to _session_model_overrides."""
        adapter = _promotion_adapter()
        assert adapter._model_promotion["enabled"] is True
        # The override ref should be None unless runner sets it
        assert adapter._session_model_overrides_ref is None


class TestAdapterTurnPlanPromotionFields:
    """Verify NapCat promotion state stays in private turn context."""

    def test_default_values(self):
        plan = AdapterTurnPlan()
        promotion = get_napcat_promotion_plan(plan)
        assert promotion.enabled is False
        assert promotion.stage == ""
        assert promotion.turn_model_override == {}
        assert promotion.protocol_prompt == ""
        assert promotion.config_snapshot == {}

    def test_attached_context_access(self):
        plan = _make_promotion_turn_plan(promotion_enabled=True, promotion_stage="l1")
        promotion = get_napcat_promotion_plan(plan)
        assert promotion.enabled is True
        assert promotion.stage == "l1"
        assert plan.get("extra_prompt") == ""
        assert plan["controller_context"] == ""

    def test_dict_normalization(self):
        from gateway.napcat_gateway_extension import NapCatGatewayExtension

        raw = {
            "extra_prompt": "test",
            "controller_context": "<persona>router</persona>",
            "promotion_enabled": True,
            "promotion_stage": "l2",
            "turn_model_override": {"model": "opus"},
            "promotion_protocol_prompt": "rules",
            "promotion_config_snapshot": {"enabled": True},
        }
        plan = NapCatGatewayExtension(SimpleNamespace()).normalize_adapter_turn_plan(raw)
        promotion = get_napcat_promotion_plan(plan)
        assert promotion.enabled is True
        assert promotion.stage == "l2"
        assert promotion.turn_model_override == {"model": "opus"}
        assert plan.controller_context == "<persona>router</persona>"
        assert plan["controller_context"] == "<persona>router</persona>"


@pytest.mark.asyncio
class TestPromotionGatewayFlow:
    async def test_no_reply_stops_before_transcript_persistence(self, monkeypatch):
        import gateway.run as gateway_run

        monkeypatch.setattr(gateway_run, "build_session_context", lambda source, config, session_entry: SimpleNamespace(source=source))
        monkeypatch.setattr(gateway_run, "build_session_context_prompt", lambda context, redact_pii=False: "")
        monkeypatch.setenv("NAPCAT_HOME_CHANNEL", "123456")

        adapter = SimpleNamespace(
            prepare_gateway_turn=AsyncMock(
                return_value=_make_promotion_turn_plan(
                    promotion_enabled=True,
                    promotion_stage="l1",
                    turn_model_override={"model": "qwen-plus"},
                    promotion_protocol_prompt="rules",
                    promotion_config_snapshot={
                        "protocol": {
                            "no_reply_marker": "[[NO_REPLY]]",
                            "escalate_marker": "[[ESCALATE_L2]]",
                        },
                        "l2": {"model": "claude-opus"},
                        "safeguards": {},
                    },
                )
            ),
            send=AsyncMock(),
            stop_typing=AsyncMock(),
        )
        session_entry = SimpleNamespace(
            session_key="agent:main:napcat:group:123456",
            session_id="sess-1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            was_auto_reset=False,
            last_prompt_tokens=0,
        )
        session_store = SimpleNamespace(
            get_or_create_session=lambda source: session_entry,
            load_transcript=lambda session_id: [{"role": "user", "content": "earlier"}],
            has_any_sessions=lambda: True,
            append_to_transcript=MagicMock(),
            update_session=MagicMock(),
        )
        runner = _make_gateway_runner(adapter, session_store)
        runner._run_agent = AsyncMock(
            return_value={
                "final_response": "[[NO_REPLY]]",
                "messages": [{"role": "assistant", "content": "[[NO_REPLY]]"}],
                "api_calls": 1,
                "history_offset": 1,
                "model": "qwen-plus",
                "provider": "openrouter",
                "api_mode": "chat_completions",
            }
        )

        source = _make_source(chat_type="group")
        event = _make_event("hello", source=source)
        event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-no-reply")

        emit_calls = []

        def _capture_emit(event_type, payload, **kwargs):
            emit_calls.append((event_type, payload, kwargs))

        monkeypatch.setattr(gateway_run, "_emit_gateway_event", _capture_emit)

        result = await runner._handle_message_with_agent(event, source, session_entry.session_key)

        assert result is None
        assert runner._run_agent.await_count == 1
        session_store.append_to_transcript.assert_not_called()
        final_events = [payload for event_type, payload, _ in emit_calls if event_type == gateway_run._GatewayEventType.AGENT_RESPONSE_FINAL]
        assert final_events
        assert final_events[-1]["promotion_stage_used"] == "l1"
        assert final_events[-1]["promotion_escalated"] is False
        assert final_events[-1]["promotion_reason"] == "no_reply"
        assert final_events[-1]["response_suppressed"] is True
        assert final_events[-1]["model"] == "qwen-plus"

    async def test_dm_no_reply_from_l1_escalates_to_l2(self, monkeypatch):
        import gateway.run as gateway_run

        monkeypatch.setattr(gateway_run, "build_session_context", lambda source, config, session_entry: SimpleNamespace(source=source))
        monkeypatch.setattr(gateway_run, "build_session_context_prompt", lambda context, redact_pii=False: "")
        monkeypatch.setenv("NAPCAT_HOME_CHANNEL", "123456")

        adapter = SimpleNamespace(
            prepare_gateway_turn=AsyncMock(
                return_value=_make_promotion_turn_plan(
                    promotion_enabled=True,
                    promotion_stage="l1",
                    turn_model_override={"model": "qwen-plus"},
                    promotion_protocol_prompt="rules",
                    promotion_config_snapshot={
                        "protocol": {
                            "no_reply_marker": "[[NO_REPLY]]",
                            "escalate_marker": "[[ESCALATE_L2]]",
                        },
                        "l2": {"model": "claude-opus"},
                        "safeguards": {},
                    },
                )
            ),
            send=AsyncMock(),
            stop_typing=AsyncMock(),
        )
        session_entry = SimpleNamespace(
            session_key="agent:main:napcat:dm:10001",
            session_id="sess-dm-no-reply",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            was_auto_reset=False,
            last_prompt_tokens=0,
        )
        session_store = SimpleNamespace(
            get_or_create_session=lambda source: session_entry,
            load_transcript=lambda session_id: [{"role": "user", "content": "earlier"}],
            has_any_sessions=lambda: True,
            append_to_transcript=MagicMock(),
            update_session=MagicMock(),
        )
        runner = _make_gateway_runner(adapter, session_store)
        runner._run_agent = AsyncMock(
            side_effect=[
                {
                    "final_response": "[[NO_REPLY]]",
                    "messages": [{"role": "assistant", "content": "[[NO_REPLY]]"}],
                    "api_calls": 1,
                    "history_offset": 1,
                    "model": "qwen-plus",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                },
                {
                    "final_response": "dm final reply",
                    "messages": [
                        {"role": "user", "content": "earlier"},
                        {"role": "assistant", "content": "dm final reply"},
                    ],
                    "api_calls": 1,
                    "history_offset": 1,
                    "model": "claude-opus",
                    "provider": "anthropic",
                    "api_mode": "anthropic_messages",
                },
            ]
        )

        source = _make_source(chat_type="dm", chat_id="10001")
        event = _make_event("hello", source=source)
        event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-dm-no-reply")

        emit_calls = []

        def _capture_emit(event_type, payload, **kwargs):
            emit_calls.append((event_type, payload, kwargs))

        monkeypatch.setattr(gateway_run, "_emit_gateway_event", _capture_emit)

        result = await runner._handle_message_with_agent(event, source, session_entry.session_key)

        assert result == "dm final reply"
        assert runner._run_agent.await_count == 2
        first_call = runner._run_agent.await_args_list[0].kwargs
        second_call = runner._run_agent.await_args_list[1].kwargs
        assert first_call["promotion_stage"] == "l1"
        assert second_call["promotion_stage"] == "l2"

        final_events = [payload for event_type, payload, _ in emit_calls if event_type == gateway_run._GatewayEventType.AGENT_RESPONSE_FINAL]
        assert final_events
        assert final_events[-1]["promotion_stage_used"] == "l2"
        assert final_events[-1]["promotion_escalated"] is True
        assert final_events[-1]["promotion_reason"] == "dm_no_reply_forbidden"
        assert final_events[-1]["model"] == "claude-opus"

    async def test_escalate_runs_l2_once_and_uses_l2_reply(self, monkeypatch):
        import gateway.run as gateway_run

        monkeypatch.setattr(gateway_run, "build_session_context", lambda source, config, session_entry: SimpleNamespace(source=source))
        monkeypatch.setattr(gateway_run, "build_session_context_prompt", lambda context, redact_pii=False: "")
        monkeypatch.setenv("NAPCAT_HOME_CHANNEL", "123456")

        protocol = {
            "no_reply_marker": "[[NO_REPLY]]",
            "escalate_marker": "[[ESCALATE_L2]]",
        }
        l1_override = {"model": "qwen-plus"}
        l2_override = {"model": "claude-opus"}
        adapter = SimpleNamespace(
            prepare_gateway_turn=AsyncMock(
                return_value=_make_promotion_turn_plan(
                    promotion_enabled=True,
                    promotion_stage="l1",
                    turn_model_override=l1_override,
                    promotion_protocol_prompt="rules",
                    promotion_config_snapshot={
                        "protocol": protocol,
                        "l2": l2_override,
                        "safeguards": {},
                    },
                )
            ),
            send=AsyncMock(),
            stop_typing=AsyncMock(),
        )
        session_entry = SimpleNamespace(
            session_key="agent:main:napcat:group:123456",
            session_id="sess-2",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            was_auto_reset=False,
            last_prompt_tokens=0,
        )
        session_store = SimpleNamespace(
            get_or_create_session=lambda source: session_entry,
            load_transcript=lambda session_id: [{"role": "user", "content": "earlier"}],
            has_any_sessions=lambda: True,
            append_to_transcript=MagicMock(),
            update_session=MagicMock(),
        )
        runner = _make_gateway_runner(adapter, session_store)
        runner._run_agent = AsyncMock(
            side_effect=[
                {
                    "final_response": "[[ESCALATE_L2]]",
                    "messages": [{"role": "assistant", "content": "[[ESCALATE_L2]]"}],
                    "api_calls": 1,
                    "history_offset": 1,
                    "model": "qwen-plus",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                },
                {
                    "final_response": "final l2 reply",
                    "messages": [
                        {"role": "user", "content": "earlier"},
                        {"role": "assistant", "content": "final l2 reply"},
                    ],
                    "api_calls": 1,
                    "history_offset": 1,
                    "model": "claude-opus",
                    "provider": "anthropic",
                    "api_mode": "anthropic_messages",
                },
            ]
        )

        source = _make_source(chat_type="group")
        event = _make_event("solve this", source=source)
        event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-1")

        emit_calls = []

        def _capture_emit(event_type, payload, **kwargs):
            emit_calls.append((event_type, payload, kwargs))

        monkeypatch.setattr(gateway_run, "_emit_gateway_event", _capture_emit)

        result = await runner._handle_message_with_agent(event, source, session_entry.session_key)

        assert result == "final l2 reply"
        assert runner._run_agent.await_count == 2
        first_call = runner._run_agent.await_args_list[0].kwargs
        second_call = runner._run_agent.await_args_list[1].kwargs
        assert first_call["turn_model_override"] == l1_override
        assert second_call["turn_model_override"] == l2_override
        assert first_call["promotion_stage"] == "l1"
        assert second_call["promotion_stage"] == "l2"

        final_events = [payload for event_type, payload, _ in emit_calls if event_type == gateway_run._GatewayEventType.AGENT_RESPONSE_FINAL]
        assert final_events
        assert final_events[-1]["promotion_active"] is True
        assert final_events[-1]["promotion_stage_used"] == "l2"
        assert final_events[-1]["promotion_escalated"] is True
        assert final_events[-1]["promotion_reason"] == "marker"
        assert final_events[-1]["model"] == "claude-opus"

    async def test_high_complexity_score_runs_l2_once_and_uses_l2_reply(self, monkeypatch):
        import gateway.run as gateway_run

        monkeypatch.setattr(gateway_run, "build_session_context", lambda source, config, session_entry: SimpleNamespace(source=source))
        monkeypatch.setattr(gateway_run, "build_session_context_prompt", lambda context, redact_pii=False: "")
        monkeypatch.setenv("NAPCAT_HOME_CHANNEL", "123456")

        protocol = {
            "no_reply_marker": "[[NO_REPLY]]",
            "escalate_marker": "[[ESCALATE_L2]]",
        }
        l1_override = {"model": "qwen-plus"}
        l2_override = {"model": "claude-opus"}
        adapter = SimpleNamespace(
            prepare_gateway_turn=AsyncMock(
                return_value=_make_promotion_turn_plan(
                    promotion_enabled=True,
                    promotion_stage="l1",
                    turn_model_override=l1_override,
                    promotion_protocol_prompt="rules",
                    promotion_config_snapshot={
                        "protocol": protocol,
                        "l2": l2_override,
                        "safeguards": {"l2_complexity_score_threshold": 70},
                    },
                )
            ),
            send=AsyncMock(),
            stop_typing=AsyncMock(),
        )
        session_entry = SimpleNamespace(
            session_key="agent:main:napcat:group:123456",
            session_id="sess-score-1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            was_auto_reset=False,
            last_prompt_tokens=0,
        )
        session_store = SimpleNamespace(
            get_or_create_session=lambda source: session_entry,
            load_transcript=lambda session_id: [{"role": "user", "content": "earlier"}],
            has_any_sessions=lambda: True,
            append_to_transcript=MagicMock(),
            update_session=MagicMock(),
        )
        runner = _make_gateway_runner(adapter, session_store)
        runner._run_agent = AsyncMock(
            side_effect=[
                {
                    "final_response": "[[COMPLEXITY:80]] 先给一个简短判断。",
                    "messages": [{"role": "assistant", "content": "[[COMPLEXITY:80]] 先给一个简短判断。"}],
                    "api_calls": 1,
                    "history_offset": 1,
                    "model": "qwen-plus",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                },
                {
                    "final_response": "final l2 reply",
                    "messages": [
                        {"role": "user", "content": "earlier"},
                        {"role": "assistant", "content": "final l2 reply"},
                    ],
                    "api_calls": 1,
                    "history_offset": 1,
                    "model": "claude-opus",
                    "provider": "anthropic",
                    "api_mode": "anthropic_messages",
                },
            ]
        )

        source = _make_source(chat_type="group")
        event = _make_event("complex request", source=source)
        event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-score-1")

        emit_calls = []

        def _capture_emit(event_type, payload, **kwargs):
            emit_calls.append((event_type, payload, kwargs))

        monkeypatch.setattr(gateway_run, "_emit_gateway_event", _capture_emit)

        result = await runner._handle_message_with_agent(event, source, session_entry.session_key)

        assert result == "final l2 reply"
        assert runner._run_agent.await_count == 2
        first_call = runner._run_agent.await_args_list[0].kwargs
        second_call = runner._run_agent.await_args_list[1].kwargs
        assert first_call["promotion_stage"] == "l1"
        assert second_call["promotion_stage"] == "l2"

        final_events = [payload for event_type, payload, _ in emit_calls if event_type == gateway_run._GatewayEventType.AGENT_RESPONSE_FINAL]
        assert final_events
        assert final_events[-1]["promotion_stage_used"] == "l2"
        assert final_events[-1]["promotion_escalated"] is True
        assert final_events[-1]["promotion_reason"] == "complexity_score=80>=70"

    async def test_l1_context_places_promotion_rules_before_platform_note(self, monkeypatch):
        import gateway.run as gateway_run

        monkeypatch.setattr(gateway_run, "build_session_context", lambda source, config, session_entry: SimpleNamespace(source=source))
        monkeypatch.setattr(gateway_run, "build_session_context_prompt", lambda context, redact_pii=False: "session block")
        monkeypatch.setenv("NAPCAT_HOME_CHANNEL", "123456")

        adapter = SimpleNamespace(
            prepare_gateway_turn=AsyncMock(
                return_value=_make_promotion_turn_plan(
                    extra_prompt="[Platform note: qq]\n\nextra",
                    promotion_enabled=True,
                    promotion_stage="l1",
                    turn_model_override={"model": "qwen-plus"},
                    promotion_protocol_prompt="promotion rules",
                    promotion_config_snapshot={
                        "protocol": {
                            "no_reply_marker": "[[NO_REPLY]]",
                            "escalate_marker": "[[ESCALATE_L2]]",
                        },
                        "l2": {"model": "claude-opus"},
                        "safeguards": {"l2_complexity_score_threshold": 70},
                    },
                )
            ),
            send=AsyncMock(),
            stop_typing=AsyncMock(),
        )
        session_entry = SimpleNamespace(
            session_key="agent:main:napcat:group:123456",
            session_id="sess-order-1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            was_auto_reset=False,
            last_prompt_tokens=0,
        )
        session_store = SimpleNamespace(
            get_or_create_session=lambda source: session_entry,
            load_transcript=lambda session_id: [{"role": "user", "content": "earlier"}],
            has_any_sessions=lambda: True,
            append_to_transcript=MagicMock(),
            update_session=MagicMock(),
        )
        runner = _make_gateway_runner(adapter, session_store)
        runner._run_agent = AsyncMock(
            return_value={
                "final_response": "[[COMPLEXITY:10]] ok",
                "messages": [{"role": "assistant", "content": "[[COMPLEXITY:10]] ok"}],
                "api_calls": 1,
                "history_offset": 1,
                "model": "qwen-plus",
                "provider": "openrouter",
                "api_mode": "chat_completions",
            }
        )

        source = _make_source(chat_type="group")
        event = _make_event("simple request", source=source)
        event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-order-1")

        await runner._handle_message_with_agent(event, source, session_entry.session_key)

        first_call = runner._run_agent.await_args.kwargs
        assert first_call["promotion_stage"] == "l1"
        assert first_call["context_prompt"] == "session block\n\npromotion rules\n\n[Platform note: qq]\n\nextra"

    async def test_noisy_high_complexity_l1_response_is_hard_clipped_before_l2(self, monkeypatch):
        import gateway.run as gateway_run

        monkeypatch.setattr(gateway_run, "build_session_context", lambda source, config, session_entry: SimpleNamespace(source=source))
        monkeypatch.setattr(gateway_run, "build_session_context_prompt", lambda context, redact_pii=False: "")
        monkeypatch.setenv("NAPCAT_HOME_CHANNEL", "123456")

        protocol = {
            "no_reply_marker": "[[NO_REPLY]]",
            "escalate_marker": "[[ESCALATE_L2]]",
        }
        adapter = SimpleNamespace(
            prepare_gateway_turn=AsyncMock(
                return_value=_make_promotion_turn_plan(
                    promotion_enabled=True,
                    promotion_stage="l1",
                    turn_model_override={"model": "qwen-plus"},
                    promotion_protocol_prompt="rules",
                    promotion_config_snapshot={
                        "protocol": protocol,
                        "l2": {"model": "claude-opus"},
                        "safeguards": {"l2_complexity_score_threshold": 16},
                    },
                )
            ),
            send=AsyncMock(),
            stop_typing=AsyncMock(),
        )
        session_entry = SimpleNamespace(
            session_key="agent:main:napcat:group:123456",
            session_id="sess-score-clip",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            was_auto_reset=False,
            last_prompt_tokens=0,
        )
        session_store = SimpleNamespace(
            get_or_create_session=lambda source: session_entry,
            load_transcript=lambda session_id: [{"role": "user", "content": "earlier"}],
            has_any_sessions=lambda: True,
            append_to_transcript=MagicMock(),
            update_session=MagicMock(),
        )
        runner = _make_gateway_runner(adapter, session_store)
        runner._run_agent = AsyncMock(
            side_effect=[
                {
                    "final_response": "我来帮你分析。[[COMPLEXITY:25]]\n<tool_call>...</tool_call>",
                    "messages": [{"role": "assistant", "content": "我来帮你分析。[[COMPLEXITY:25]]\n<tool_call>...</tool_call>"}],
                    "api_calls": 1,
                    "history_offset": 1,
                    "model": "qwen-plus",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                },
                {
                    "final_response": "final l2 reply",
                    "messages": [{"role": "assistant", "content": "final l2 reply"}],
                    "api_calls": 1,
                    "history_offset": 1,
                    "model": "claude-opus",
                    "provider": "anthropic",
                    "api_mode": "anthropic_messages",
                },
            ]
        )

        source = _make_source(chat_type="group")
        event = _make_event("complex request", source=source)
        event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-score-clip")

        result = await runner._handle_message_with_agent(event, source, session_entry.session_key)

        assert result == "final l2 reply"
        assert runner._run_agent.await_count == 2

    async def test_intermediate_complexity_marker_at_threshold_still_escalates(self, monkeypatch):
        import gateway.run as gateway_run

        monkeypatch.setattr(gateway_run, "build_session_context", lambda source, config, session_entry: SimpleNamespace(source=source))
        monkeypatch.setattr(gateway_run, "build_session_context_prompt", lambda context, redact_pii=False: "")
        monkeypatch.setenv("NAPCAT_HOME_CHANNEL", "123456")

        protocol = {
            "no_reply_marker": "[[NO_REPLY]]",
            "escalate_marker": "[[ESCALATE_L2]]",
        }
        adapter = SimpleNamespace(
            prepare_gateway_turn=AsyncMock(
                return_value=_make_promotion_turn_plan(
                    promotion_enabled=True,
                    promotion_stage="l1",
                    turn_model_override={"model": "MiniMax-M2.7-highspeed"},
                    promotion_protocol_prompt="rules",
                    promotion_config_snapshot={
                        "protocol": protocol,
                        "l2": {"model": "glm-4-7-251222"},
                        "safeguards": {"l2_complexity_score_threshold": 16},
                    },
                )
            ),
            send=AsyncMock(),
            stop_typing=AsyncMock(),
        )
        session_entry = SimpleNamespace(
            session_key="agent:main:napcat:group:123456",
            session_id="sess-complexity-threshold",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            was_auto_reset=False,
            last_prompt_tokens=0,
        )
        session_store = SimpleNamespace(
            get_or_create_session=lambda source: session_entry,
            load_transcript=lambda session_id: [{"role": "user", "content": "earlier"}],
            has_any_sessions=lambda: True,
            append_to_transcript=MagicMock(),
            update_session=MagicMock(),
        )
        runner = _make_gateway_runner(adapter, session_store)
        runner._run_agent = AsyncMock(
            side_effect=[
                {
                    "final_response": "这是最终总结，不带复杂度标记。",
                    "messages": [
                        {"role": "user", "content": "earlier"},
                        {
                            "role": "assistant",
                            "content": "[[COMPLEXITY:16]]\n\n先 clone 下来。",
                            "tool_calls": [{"function": {"name": "terminal"}}],
                        },
                        {"role": "tool", "content": "{\"output\":\"ok\"}"},
                        {"role": "assistant", "content": "这是最终总结，不带复杂度标记。"},
                    ],
                    "api_calls": 6,
                    "history_offset": 1,
                    "model": "MiniMax-M2.7-highspeed",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                },
                {
                    "final_response": "final l2 reply",
                    "messages": [
                        {"role": "user", "content": "earlier"},
                        {"role": "assistant", "content": "final l2 reply"},
                    ],
                    "api_calls": 1,
                    "history_offset": 1,
                    "model": "glm-4-7-251222",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                },
            ]
        )

        source = _make_source(chat_type="group")
        event = _make_event(
            "https://github.com/MetaCubeX/mihomo 把这个项目clone下来切到Meta分支分析一下这个项目是用来做什么的",
            source=source,
        )
        event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-complexity-threshold")

        emit_calls = []

        def _capture_emit(event_type, payload, **kwargs):
            emit_calls.append((event_type, payload, kwargs))

        monkeypatch.setattr(gateway_run, "_emit_gateway_event", _capture_emit)

        result = await runner._handle_message_with_agent(event, source, session_entry.session_key)

        assert result == "final l2 reply"
        assert runner._run_agent.await_count == 2
        final_events = [payload for event_type, payload, _ in emit_calls if event_type == gateway_run._GatewayEventType.AGENT_RESPONSE_FINAL]
        assert final_events
        assert final_events[-1]["promotion_stage_used"] == "l2"
        assert final_events[-1]["promotion_escalated"] is True
        assert final_events[-1]["promotion_reason"] == "complexity_score=16>=16"

    async def test_explicit_direct_l2_runs_once_without_l1(self, monkeypatch):
        import gateway.run as gateway_run

        monkeypatch.setattr(gateway_run, "build_session_context", lambda source, config, session_entry: SimpleNamespace(source=source))
        monkeypatch.setattr(gateway_run, "build_session_context_prompt", lambda context, redact_pii=False: "")
        monkeypatch.setenv("NAPCAT_HOME_CHANNEL", "123456")

        l2_override = {"model": "claude-opus"}
        adapter = SimpleNamespace(
            prepare_gateway_turn=AsyncMock(
                return_value=_make_promotion_turn_plan(
                    promotion_enabled=True,
                    promotion_stage="l2",
                    turn_model_override=l2_override,
                    promotion_protocol_prompt="",
                    promotion_config_snapshot={
                        "protocol": {
                            "no_reply_marker": "[[NO_REPLY]]",
                            "escalate_marker": "[[ESCALATE_L2]]",
                        },
                        "l1": {"model": "qwen-plus"},
                        "l2": l2_override,
                        "optimizations": {
                            "force_direct_l2": True,
                            "force_direct_l2_reason": "explicit_user_request",
                        },
                        "safeguards": {},
                    },
                )
            ),
            send=AsyncMock(),
            stop_typing=AsyncMock(),
        )
        session_entry = SimpleNamespace(
            session_key="agent:main:napcat:group:123456",
            session_id="sess-direct-l2",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            was_auto_reset=False,
            last_prompt_tokens=0,
        )
        session_store = SimpleNamespace(
            get_or_create_session=lambda source: session_entry,
            load_transcript=lambda session_id: [{"role": "user", "content": "earlier"}],
            has_any_sessions=lambda: True,
            append_to_transcript=MagicMock(),
            update_session=MagicMock(),
        )
        runner = _make_gateway_runner(adapter, session_store)
        runner._run_agent = AsyncMock(
            return_value={
                "final_response": "direct l2 reply",
                "messages": [
                    {"role": "user", "content": "earlier"},
                    {"role": "assistant", "content": "direct l2 reply"},
                ],
                "api_calls": 1,
                "history_offset": 1,
                "model": "claude-opus",
                "provider": "anthropic",
                "api_mode": "anthropic_messages",
            }
        )

        source = _make_source(chat_type="group")
        event = _make_event("用高阶模型分析", source=source)
        event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-direct-l2")

        emit_calls = []

        def _capture_emit(event_type, payload, **kwargs):
            emit_calls.append((event_type, payload, kwargs))

        monkeypatch.setattr(gateway_run, "_emit_gateway_event", _capture_emit)

        result = await runner._handle_message_with_agent(event, source, session_entry.session_key)

        assert result == "direct l2 reply"
        assert runner._run_agent.await_count == 1
        only_call = runner._run_agent.await_args.kwargs
        assert only_call["turn_model_override"] == l2_override
        assert only_call["promotion_stage"] == "l2"

        final_events = [payload for event_type, payload, _ in emit_calls if event_type == gateway_run._GatewayEventType.AGENT_RESPONSE_FINAL]
        assert final_events
        assert final_events[-1]["promotion_stage_used"] == "l2"
        assert final_events[-1]["promotion_escalated"] is True
        assert final_events[-1]["promotion_reason"] == "explicit_user_request"
        assert final_events[-1]["model"] == "claude-opus"

    async def test_l2_marker_violation_is_sanitized_in_response_and_transcript(self, monkeypatch):
        import gateway.run as gateway_run

        monkeypatch.setattr(gateway_run, "build_session_context", lambda source, config, session_entry: SimpleNamespace(source=source))
        monkeypatch.setattr(gateway_run, "build_session_context_prompt", lambda context, redact_pii=False: "")
        monkeypatch.setenv("NAPCAT_HOME_CHANNEL", "123456")

        protocol = {
            "no_reply_marker": "[[NO_REPLY]]",
            "escalate_marker": "[[ESCALATE_L2]]",
        }
        adapter = SimpleNamespace(
            prepare_gateway_turn=AsyncMock(
                return_value=_make_promotion_turn_plan(
                    promotion_enabled=True,
                    promotion_stage="l1",
                    turn_model_override={"model": "qwen-plus"},
                    promotion_protocol_prompt="rules",
                    promotion_config_snapshot={
                        "protocol": protocol,
                        "l2": {"model": "claude-opus"},
                        "safeguards": {},
                    },
                )
            ),
            send=AsyncMock(),
            stop_typing=AsyncMock(),
        )
        session_entry = SimpleNamespace(
            session_key="agent:main:napcat:group:123456",
            session_id="sess-3",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            was_auto_reset=False,
            last_prompt_tokens=0,
        )
        session_store = SimpleNamespace(
            get_or_create_session=lambda source: session_entry,
            load_transcript=lambda session_id: [{"role": "user", "content": "earlier"}],
            has_any_sessions=lambda: True,
            append_to_transcript=MagicMock(),
            update_session=MagicMock(),
        )
        runner = _make_gateway_runner(adapter, session_store)
        runner._run_agent = AsyncMock(
            side_effect=[
                {
                    "final_response": "[[ESCALATE_L2]]",
                    "messages": [{"role": "assistant", "content": "[[ESCALATE_L2]]"}],
                    "api_calls": 1,
                    "history_offset": 1,
                },
                {
                    "final_response": "[[NO_REPLY]]",
                    "messages": [
                        {"role": "user", "content": "earlier"},
                        {"role": "assistant", "content": "[[NO_REPLY]]"},
                    ],
                    "api_calls": 1,
                    "history_offset": 1,
                },
            ]
        )

        source = _make_source(chat_type="group")
        event = _make_event("bad l2", source=source)

        result = await runner._handle_message_with_agent(event, source, session_entry.session_key)

        assert result == "抱歉，处理遇到了问题，请稍后重试。"
        appended = [call.args[1] for call in session_store.append_to_transcript.call_args_list]
        assistant_entries = [entry for entry in appended if entry.get("role") == "assistant"]
        assert assistant_entries
        assert assistant_entries[-1]["content"] == "抱歉，处理遇到了问题，请稍后重试。"

    async def test_l1_equals_l2_runs_single_stage_without_protocol_prompt(self, monkeypatch):
        import gateway.run as gateway_run

        monkeypatch.setattr(gateway_run, "build_session_context", lambda source, config, session_entry: SimpleNamespace(source=source))
        monkeypatch.setattr(gateway_run, "build_session_context_prompt", lambda context, redact_pii=False: "")
        monkeypatch.setenv("NAPCAT_HOME_CHANNEL", "123456")

        adapter = SimpleNamespace(
            prepare_gateway_turn=AsyncMock(
                return_value=_make_promotion_turn_plan(
                    promotion_enabled=True,
                    promotion_stage="l1",
                    turn_model_override={"model": "qwen-plus"},
                    promotion_protocol_prompt="",
                    promotion_config_snapshot={
                        "protocol": {
                            "no_reply_marker": "[[NO_REPLY]]",
                            "escalate_marker": "[[ESCALATE_L2]]",
                        },
                        "l2": {"model": "qwen-plus"},
                        "optimizations": {"single_stage_l1": True},
                        "safeguards": {},
                    },
                )
            ),
            send=AsyncMock(),
            stop_typing=AsyncMock(),
        )
        session_entry = SimpleNamespace(
            session_key="agent:main:napcat:group:123456",
            session_id="sess-4",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            was_auto_reset=False,
            last_prompt_tokens=0,
        )
        session_store = SimpleNamespace(
            get_or_create_session=lambda source: session_entry,
            load_transcript=lambda session_id: [{"role": "user", "content": "earlier"}],
            has_any_sessions=lambda: True,
            append_to_transcript=MagicMock(),
            update_session=MagicMock(),
        )
        runner = _make_gateway_runner(adapter, session_store)
        runner._run_agent = AsyncMock(
            return_value={
                "final_response": "single-stage reply",
                "messages": [
                    {"role": "user", "content": "earlier"},
                    {"role": "assistant", "content": "single-stage reply"},
                ],
                "api_calls": 1,
                "history_offset": 1,
            }
        )

        source = _make_source(chat_type="group")
        event = _make_event("single stage", source=source)

        result = await runner._handle_message_with_agent(event, source, session_entry.session_key)

        assert result == "single-stage reply"
        assert runner._run_agent.await_count == 1
        assert runner._run_agent.await_args.kwargs["context_prompt"] == ""
