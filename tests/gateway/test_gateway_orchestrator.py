import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, GatewayOrchestratorConfig, Platform, PlatformConfig
from gateway.napcat_metadata import PREFETCHED_MEMORY_CONTEXT_KEY
from gateway.napcat_orchestrator_support import NapCatOrchestratorSupport, _ProviderRuntimeHandle
from gateway.napcat_observability.ingest_client import IngestClient
from gateway.napcat_observability.publisher import (
    drain_queue,
    emit_napcat_event,
    init_publisher,
    shutdown_publisher,
)
from gateway.napcat_observability.repository import ObservabilityRepository
from gateway.napcat_observability.schema import EventType
from gateway.napcat_observability.store import ObservabilityStore
from gateway.napcat_observability.trace import NapcatTraceContext
from gateway.orchestrator import GatewayChildState, parse_orchestrator_decision
from gateway.platforms.base import AdapterTurnPlan, MessageEvent, MessageType
from gateway.platforms.napcat_turn_context import attach_napcat_promotion_plan
from gateway.session import SessionEntry, SessionSource


def _make_source(chat_type: str = "dm") -> SessionSource:
    return SessionSource(
        platform=Platform.NAPCAT,
        chat_id="chat-1",
        chat_type=chat_type,
        user_id="user-1",
        user_name="tester",
    )


def _make_event(text: str = "hello", *, chat_type: str = "dm") -> MessageEvent:
    return MessageEvent(
        text=text,
        source=_make_source(chat_type=chat_type),
        message_type=MessageType.TEXT,
        message_id="msg-1",
    )


def _make_session_entry(chat_type: str = "dm") -> SessionEntry:
    now = datetime.now(timezone.utc)
    return SessionEntry(
        session_key=f"agent:main:napcat:{chat_type}:chat-1",
        session_id="session-1",
        created_at=now,
        updated_at=now,
        platform=Platform.NAPCAT,
        chat_type=chat_type,
        origin=_make_source(chat_type=chat_type),
    )


def _make_trace_ctx(
    *,
    trace_id: str = "trace-1",
    span_id: str = "span-1",
    parent_span_id: str = "",
    session_key: str = "agent:main:napcat:dm:chat-1",
    session_id: str = "session-1",
    agent_scope: str = "",
    child_id: str = "",
) -> NapcatTraceContext:
    return NapcatTraceContext(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        session_key=session_key,
        session_id=session_id,
        chat_type="dm",
        chat_id="chat-1",
        user_id="user-1",
        message_id="msg-1",
        agent_scope=agent_scope,
        child_id=child_id,
    )


def _make_runner() -> "GatewayRunner":
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.NAPCAT: PlatformConfig(
                enabled=True,
                extra={
                    "orchestrator": {
                        "enabled_platforms": ["napcat"],
                        "child_max_concurrency": 1,
                        "child_default_toolsets": ["terminal", "file_tools"],
                    }
                },
            )
        }
    )
    runner.adapters = {Platform.NAPCAT: MagicMock()}
    runner.adapters[Platform.NAPCAT].send = AsyncMock()
    runner.adapters[Platform.NAPCAT]._send_with_retry = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="busy-ack-1")
    )
    runner.session_store = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.load_transcript = MagicMock(return_value=[])
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner._session_db = None
    runner._background_tasks = set()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._busy_ack_ts = {}
    runner._voice_mode = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = MagicMock()
    runner._cleanup_agent_resources = MagicMock()
    runner._get_gateway_extension().replace_orchestrator_sessions({})
    return runner


def _support(runner):
    return runner._get_gateway_extension().orchestrator_support()


def _runtime(runner):
    return runner._get_gateway_extension().orchestrator_runtime()


def _extension(runner):
    return runner._get_gateway_extension()


def _set_effective_orchestrator_config(runner, **overrides):
    cfg = runner.config.gateway_orchestrator.to_dict()
    cfg.update(overrides)
    runner.config.gateway_orchestrator = GatewayOrchestratorConfig.from_dict(cfg)
    runner.config.platforms[Platform.NAPCAT].extra["orchestrator"] = (
        runner.config.gateway_orchestrator.to_dict()
    )


def test_parse_orchestrator_decision_accepts_action_payload_json_fence():
    decision = parse_orchestrator_decision(
        """```json
        {"action": "reply", "payload": {"text": "ok"}}
        ```"""
    )

    assert decision.complexity_score == 1
    assert decision.respond_now is True
    assert decision.immediate_reply == "ok"
    assert decision.reasoning_summary == ""


def test_parse_orchestrator_decision_accepts_top_level_complexity_zero():
    decision = parse_orchestrator_decision(
        '{"action":"ignore","complexity_score":0,"payload":{}}'
    )

    assert decision.complexity_score == 0
    assert decision.ignore_message is True
    assert decision.respond_now is False


def test_parse_orchestrator_decision_keeps_backward_compatibility():
    decision = parse_orchestrator_decision(
        '{"complexity_score": 1, "respond_now": true, "immediate_reply": "ok", '
        '"spawn_children": [], "cancel_children": [], "ignore_message": false, '
        '"reasoning_summary": "done"}'
    )

    assert decision.complexity_score == 1
    assert decision.respond_now is True
    assert decision.immediate_reply == "ok"
    assert decision.reasoning_summary == "done"


def test_parse_orchestrator_decision_accepts_spawn_handoff_reply():
    decision = parse_orchestrator_decision(
        '{"action":"spawn","complexity_score":44,'
        '"payload":{"goal":"different task","handoff_reply":"先把旧任务做完，等会继续处理这个。"}}'
    )

    assert decision.respond_now is False
    assert len(decision.spawn_children) == 1
    assert decision.spawn_children[0].goal == "different task"
    assert decision.spawn_handoff_reply == "先把旧任务做完，等会继续处理这个。"


def test_parse_orchestrator_decision_repairs_mismatched_payload_closer():
    decision = parse_orchestrator_decision(
        '{"action":"reply","payload":{"text":"kst~ 来了来了~ 马上给你找图去的说！"}],"complexity_score":8}'
    )

    assert decision.complexity_score == 8
    assert decision.respond_now is True
    assert decision.immediate_reply == "kst~ 来了来了~ 马上给你找图去的说！"


def test_parse_orchestrator_decision_repairs_missing_trailing_brace():
    decision = parse_orchestrator_decision(
        '{"action":"ignore","payload":{},"complexity_score":0'
    )

    assert decision.complexity_score == 0
    assert decision.ignore_message is True
    assert decision.respond_now is False


def test_orchestrator_prompt_hides_model_details():
    runner = _make_runner()
    system_prompt = _support(runner).build_system_prompt("dm")
    group_prompt = _support(runner).build_system_prompt("group")

    assert "Allowed output shapes:" in system_prompt
    assert "payload must always be a JSON object" in system_prompt
    assert "valid JSON that parses with Python json.loads without repair" in system_prompt
    assert "payload closes with } not ]" in system_prompt
    assert "current, live, or externally verified information" in system_prompt
    assert "Do not recommend, select, or mention toolsets." in system_prompt
    assert "Requests to analyze a project, repository, repo structure, codebase, files, directories, architecture, implementation, bug, trace, or investigation are NOT trivial." in system_prompt
    assert "Treat conversation_history and the injected routing context as authoritative context for routing." in system_prompt
    assert 'history_source="orchestrator"' in system_prompt
    assert 'history_source="full_agent"' in system_prompt
    assert "substantive follow-up work for the full agent" in system_prompt
    assert "an old full-agent summary is not fresh truth" in system_prompt
    assert "current device state, home device status, smart-home facts, sensor readings" in system_prompt
    assert '{"action":"reply","payload":{"text":"short direct reply"},"complexity_score":3}' in system_prompt
    assert '{"action":"reply","payload":{"text":"ok"}],"complexity_score":3}' in system_prompt
    assert "The system routes spawned work to L1 for scores 0-16 and to L2 for scores 17-100" in system_prompt
    assert "do not score it below that prior high score" in system_prompt
    assert "image generation, TTS, or STT are not automatically high complexity" in system_prompt
    assert "If the user corrects you" in system_prompt
    assert "DM routing rules:" in system_prompt
    assert "In DMs, prefer replying instead of ignoring." in system_prompt
    assert "device facts, device status, smart-home readings" in system_prompt
    assert "Group routing rules:" in group_prompt
    assert "In groups, default to action=ignore. Only avoid ignore when the message is clearly related to the agent in context." in group_prompt
    assert "Treat banter as an ultra-rare edge case" in group_prompt
    assert "prefetched group memory explicitly defines that exact short message as a fixed trigger/keyword/command for Hermes" in group_prompt
    assert "Do not interject just because a line is funny, answerable, sympathetic, or easy to react to." in group_prompt
    assert "use action=ignore instead of inserting Hermes into the conversation" in group_prompt
    assert "If a human in the room could naturally reply, laugh, or continue the thread without Hermes, prefer action=ignore." in group_prompt
    assert '{"action":"reply","payload":{"text":"我还在看刚才那个仓库问题，当前先盯着 gateway 这块。"},"complexity_score":8}' in group_prompt
    assert 'group message is a short reaction like "这也行"' in group_prompt
    assert "In DMs, prefer replying instead of ignoring." not in group_prompt
    assert "Group routing rules:" not in system_prompt
    assert "allowed_toolsets" not in system_prompt
    assert "model_tier" not in system_prompt
    assert "turn_model_override" not in system_prompt
    assert '"reason":' not in system_prompt
    assert "openrouter" not in system_prompt.lower()
    assert "openrouter" not in group_prompt.lower()


def test_orchestrator_detects_user_correction_messages():
    runner = _make_runner()

    assert _support(runner).is_user_correction("不是，我是后端，不是前端")
    assert _support(runner).is_user_correction("纠正一下，你记错了，我在上海")
    assert _support(runner).is_user_correction("you got that wrong, it's 2025 not 2024")
    assert not _support(runner).is_user_correction("hi")
    assert not _support(runner).is_user_correction("what's the progress")


def test_orchestrator_detects_device_state_handoff_messages():
    runner = _make_runner()

    assert _support(runner).requires_device_state_handoff("请你帮我看一下我家的设备状态")
    assert _support(runner).requires_device_state_handoff("看看除湿机细节信息")
    assert _support(runner).requires_device_state_handoff("台灯现在开着吗？")
    assert not _support(runner).requires_device_state_handoff("你还在吗")
    assert not _support(runner).requires_device_state_handoff("看一下这个设备状态机实现")


def test_orchestrator_group_message_gate_accepts_recent_agent_followup():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:group:chat-1")

    related = _support(runner).group_message_clearly_agent_related(
        user_message="继续展开说一下",
        source=_make_source(chat_type="group"),
        session=session,
        history=[{"role": "assistant", "content": "我刚说了问题在锁竞争。"}],
        event_metadata={"napcat_trigger_reason": "group_message"},
    )

    assert related is True


def test_orchestrator_group_banter_reply_accepts_isolated_one_liner():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:group:chat-1")

    reply = _support(runner).group_banter_reply(
        user_message="这也行",
        source=_make_source(chat_type="group"),
        session=session,
        history=[],
        event_metadata={"napcat_trigger_reason": "group_message"},
    )

    assert reply == "好家伙，这也行。"


def test_orchestrator_group_banter_reply_rejects_active_discussion_line():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:group:chat-1")

    reply = _support(runner).group_banter_reply(
        user_message="这也行",
        source=_make_source(chat_type="group"),
        session=session,
        history=[],
        event_metadata={
            "napcat_trigger_reason": "group_message",
            "napcat_recent_group_context": "Alice: 这方案肯定会炸\nBob: 我也觉得得重构",
        },
    )

    assert reply == ""


def test_orchestrator_group_message_gate_rejects_ambiguous_repo_chat():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:group:chat-1")

    related = _support(runner).group_message_clearly_agent_related(
        user_message="这个仓库的并发模型是不是有问题",
        source=_make_source(chat_type="group"),
        session=session,
        history=[],
        event_metadata={"napcat_trigger_reason": "group_message"},
    )

    assert related is False


def test_orchestrator_group_message_gate_accepts_prefetched_group_trigger_memory():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:group:chat-1")

    related = _support(runner).group_message_clearly_agent_related(
        user_message="kst",
        source=_make_source(chat_type="group"),
        session=session,
        history=[],
        event_metadata={
            "napcat_trigger_reason": "group_message",
            PREFETCHED_MEMORY_CONTEXT_KEY: (
                "<memory-context>\n"
                "## Shared Context [napcat_group_547996548]\n"
                "- NapCat群里固定指令：任何人单独说「kst」时，自动调用 pixiv-soft-r15 skill 找图发出来\n"
                "</memory-context>"
            ),
        },
    )

    assert related is True


def test_orchestrator_turn_message_keeps_only_raw_user_message():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:dm:chat-1")
    session_entry = _make_session_entry()
    message = _support(runner).build_turn_message(
        user_message="please analyze this repo deeply",
        source=_make_source(),
        session_entry=session_entry,
        platform_turn=attach_napcat_promotion_plan(
            AdapterTurnPlan(),
            {
                "promotion_enabled": True,
                "promotion_stage": "l2",
                "turn_model_override": {"model": "claude-opus", "provider": "openrouter"},
                "promotion_config_snapshot": {"l2": {"model": "claude-opus"}},
            },
        ),
        session=session,
        event_metadata={"napcat_trigger_reason": "dm"},
    )

    assert message == "please analyze this repo deeply"


def test_hydrate_event_napcat_trace_context_backfills_event_and_adapter_trace():
    runner = _make_runner()
    session_entry = _make_session_entry()
    source = _make_source()
    event = _make_event()

    event.napcat_trace_ctx = _make_trace_ctx(
        trace_id="trace-shared",
        session_key="",
        session_id="",
    )
    runner.adapters[Platform.NAPCAT]._current_trace_ctx = _make_trace_ctx(
        trace_id="trace-shared",
        span_id="span-adapter",
        session_key="stale-session-key",
        session_id="stale-session-id",
    )

    runner._get_gateway_extension().hydrate_event_trace_context(source, event, session_entry)

    assert event.napcat_trace_ctx.session_key == session_entry.session_key
    assert event.napcat_trace_ctx.session_id == session_entry.session_id
    assert runner.adapters[Platform.NAPCAT]._current_trace_ctx.session_key == session_entry.session_key
    assert runner.adapters[Platform.NAPCAT]._current_trace_ctx.session_id == session_entry.session_id


def test_orchestrator_turn_message_excludes_platform_reply_style_context():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:dm:chat-1")
    session_entry = _make_session_entry()

    message = _support(runner).build_turn_message(
        user_message="今天怎么说话",
        source=_make_source(chat_type="group"),
        session_entry=session_entry,
        platform_turn=AdapterTurnPlan(
            extra_prompt=(
                "[System note: NapCat platform conversation rules.]\n"
                "语气指令：务实元气少女\n"
                "说话非常直白。"
            ),
        ),
        session=session,
        event_metadata={"napcat_trigger_reason": "keyword"},
    )

    assert message == "今天怎么说话"


def test_orchestrator_turn_user_context_uses_compact_child_board():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:dm:chat-1")
    session_entry = _make_session_entry()
    child = GatewayChildState(
        child_id="child-1",
        session_key=session.session_key,
        goal="analyze repo deeply",
        originating_user_message="analyze repo deeply",
        status="running",
        current_step="iteration 2",
        current_tool="terminal",
        last_progress_message="Inspecting gateway flow",
        progress_seq=4,
        last_activity_ts="2026-04-17T10:01:00Z",
    )
    child.tool_history = [
        {"tool": "terminal", "message": "rg orchestrator", "timestamp": "2026-04-17T10:00:00Z"},
        {"tool": "file_read", "message": "gateway/run.py", "timestamp": "2026-04-17T10:00:10Z"},
    ]
    session.active_children[child.child_id] = child

    message = _support(runner).build_turn_user_context(
        user_message="跑到哪了",
        source=_make_source(),
        session_entry=session_entry,
        platform_turn=AdapterTurnPlan(),
        session=session,
        event_metadata={"napcat_trigger_reason": "dm"},
    )

    assert '"active_children": [{"child_id": "child-1", "goal": "analyze repo deeply", "status": "running"}]' in message
    assert '"last_decision_summary": ""' in message
    assert '"recent_children"' not in message
    assert '"current_step"' not in message
    assert '"current_tool"' not in message
    assert "progress_seq" not in message
    assert "last_activity_ts" not in message
    assert "last_progress_message" not in message


def test_orchestrator_turn_system_context_excludes_platform_reply_style():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:dm:chat-1")
    session_entry = _make_session_entry()

    message = _support(runner).build_turn_system_context(
        user_message="hi",
        source=_make_source(),
        session_entry=session_entry,
        platform_turn=AdapterTurnPlan(
            extra_prompt=(
                "[Platform note: NapCat platform conversation rules.]\n"
                "语气指令：务实元气少女"
            ),
        ),
        session=session,
        event_metadata={"napcat_trigger_reason": "dm"},
    )

    assert "[Platform note: NapCat platform conversation rules.]" not in message
    assert '"session_id": "session-1"' in message
    assert '"session_key": "agent:main:napcat:dm:chat-1"' in message
    assert '"trigger_reason": "dm"' in message
    assert '"platform": "napcat"' in message
    assert '"user_message"' not in message
    assert "[Context note: Current gateway turn context." in message


def test_orchestrator_turn_system_context_puts_structured_context_before_routing_prompt():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:dm:chat-1")
    session_entry = _make_session_entry()

    system_message = _support(runner).build_turn_system_context(
        user_message="hi",
        source=_make_source(),
        session_entry=session_entry,
        platform_turn=AdapterTurnPlan(
            extra_prompt=(
                "[Platform note: NapCat platform conversation rules.]\n"
                "语气指令：务实元气少女"
            ),
            routing_prompt=(
                "[Routing note: reuse existing child status when available.]\n"
                "Prefer status replies over duplicate spawns."
            ),
        ),
        session=session,
        event_metadata={"napcat_trigger_reason": "dm"},
        memory_context_block="mock memory",
    )
    structured_idx = system_message.index("[Context note: Current gateway turn context.")
    memory_idx = system_message.index("[Context note: Prefetched memory context")
    routing_idx = system_message.index("[Routing note: reuse existing child status when available.]")
    assert "[Platform note: NapCat platform conversation rules.]" not in system_message
    assert structured_idx < memory_idx < routing_idx


def test_orchestrator_turn_user_context_includes_running_full_agent_status():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:dm:chat-1")
    session_entry = _make_session_entry()
    running_agent = MagicMock()
    running_agent.get_activity_summary.return_value = {
        "api_call_count": 7,
        "max_iterations": 90,
        "current_tool": "terminal",
        "last_activity_desc": "Inspecting gateway flow",
    }
    runner._running_agents[session_entry.session_key] = running_agent
    runner._running_agents_ts[session_entry.session_key] = 0

    message = _support(runner).build_turn_user_context(
        user_message="跑到哪了",
        source=_make_source(),
        session_entry=session_entry,
        platform_turn=AdapterTurnPlan(),
        session=session,
        event_metadata={"napcat_trigger_reason": "dm"},
    )

    assert '"full_agent_status": {' in message
    assert '"current_step": "iteration 7/90"' in message
    assert '"current_tool": "terminal"' in message
    assert '"current_activity"' not in message


def test_orchestrator_turn_user_context_includes_memory_context_block():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:dm:chat-1")
    session_entry = _make_session_entry()

    message = _support(runner).build_turn_user_context(
        user_message="hi",
        source=_make_source(),
        session_entry=session_entry,
        platform_turn=AdapterTurnPlan(),
        session=session,
        event_metadata={"napcat_trigger_reason": "dm"},
        memory_context_block="<memory-context>\nKnown user preference\n</memory-context>",
    )

    assert "Prefetched memory context for routing only" in message
    assert "<memory-context>" in message
    assert "Known user preference" in message


def test_build_orchestrator_routing_history_seeds_only_user_messages():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:dm:chat-1")

    history = [
        {"role": "user", "content": "查一下最近的地震"},
        {"role": "assistant", "content": "还在查哦，刚启动的说。"},
        {"role": "user", "content": "查得如何了"},
    ]

    routing_history = _support(runner).build_routing_history(
        session=session,
        history=history,
    )

    # Seeding now includes assistant messages so the orchestrator can see
    # what was previously replied, producing an alternating user/assistant
    # structure instead of a flat user-only list. Assistant content is
    # canonicalised to the compact routing-JSON form by compact_history.
    assert routing_history == [
        {"role": "user", "content": "查一下最近的地震"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"还在查哦，刚启动的说。"},"complexity_score":1}',
        },
        {"role": "user", "content": "查得如何了"},
    ]


def test_orchestrator_memory_context_uses_primary_session_identity(monkeypatch):
    runner = _make_runner()
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = "DM with tester"
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {"memory": {"provider": "fake-memory"}})

    captured = {}

    class _FakeProvider:
        name = "fake-memory"

        def is_available(self):
            return True

        def get_tool_schemas(self):
            return []

        def get_last_prefetch_debug_info(self):
            return {}

    class _FakeMemoryManager:
        def __init__(self):
            self.providers = []

        def add_provider(self, provider):
            self.providers.append(provider)

        def initialize_all(self, **kwargs):
            captured["initialize_all"] = dict(kwargs)

        def on_turn_start(self, turn_number, query, platform=""):
            captured["on_turn_start"] = {
                "turn_number": turn_number,
                "query": query,
                "platform": platform,
            }

        def prefetch_all(self, query, *, session_id=""):
            captured["prefetch_all"] = {
                "query": query,
                "session_id": session_id,
            }
            return "Known user preference"

        def shutdown_all(self):
            captured["shutdown_all"] = True

    monkeypatch.setattr("gateway.run.MemoryManager", _FakeMemoryManager, raising=False)
    monkeypatch.setattr("agent.memory_manager.MemoryManager", _FakeMemoryManager)
    monkeypatch.setattr("plugins.memory.load_memory_provider", lambda name: _FakeProvider())
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: "/tmp/hermes-home")

    block = _support(runner).build_memory_context_block(
        user_message="testtest",
        source=_make_source(),
        session_key="agent:main:napcat:dm:chat-1",
        session_id="session-1",
        turn_number=3,
    )

    assert "<memory-context>" in block
    assert "Known user preference" in block
    assert captured["initialize_all"]["session_id"] == "session-1"
    assert captured["initialize_all"]["agent_context"] == "primary"
    assert captured["initialize_all"]["session_title"] == "DM with tester"
    assert captured["initialize_all"]["gateway_session_key"] == "agent:main:napcat:dm:chat-1"
    assert captured["prefetch_all"]["session_id"] == "session-1"


def test_orchestrator_memory_context_snapshot_includes_provider_debug_details(monkeypatch):
    runner = _make_runner()
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {"memory": {"provider": "fake-memory"}})

    captured = {"initialize_calls": 0, "prefetch_calls": 0}

    class _FakeProvider:
        name = "fake-memory"

        def is_available(self):
            return True

        def get_tool_schemas(self):
            return []

    class _FakeMemoryManager:
        def add_provider(self, provider):
            captured["provider_name"] = getattr(provider, "name", "")

        def initialize_all(self, **kwargs):
            captured["initialize_calls"] += 1
            captured["initialize_all"] = dict(kwargs)

        def on_turn_start(self, turn_number, query, platform=""):
            captured["on_turn_start"] = {
                "turn_number": turn_number,
                "query": query,
                "platform": platform,
            }

        def prefetch_all_with_details(self, query, *, session_id=""):
            captured["prefetch_calls"] += 1
            captured["prefetch_all_with_details"] = {
                "query": query,
                "session_id": session_id,
            }
            return [
                {
                    "provider": "fake-memory",
                    "content": "Known user preference",
                    "prefetch_debug": {
                        "mode": "hybrid",
                        "top_k": 8,
                    },
                }
            ]

        def shutdown_all(self):
            captured["shutdown_all"] = True

    monkeypatch.setattr("gateway.run.MemoryManager", _FakeMemoryManager, raising=False)
    monkeypatch.setattr("agent.memory_manager.MemoryManager", _FakeMemoryManager)
    monkeypatch.setattr("plugins.memory.load_memory_provider", lambda name: _FakeProvider())
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: "/tmp/hermes-home")

    snapshot = _support(runner).build_memory_context_snapshot(
        user_message="testtest",
        source=_make_source(),
        session_key="agent:main:napcat:dm:chat-1",
        session_id="session-1",
        turn_number=3,
    )

    assert snapshot["combined_block"].startswith("<memory-context>")
    assert snapshot["memory_prefetch_content"] == "Known user preference"
    assert snapshot["memory_prefetch_fenced"].startswith("<memory-context>")
    assert snapshot["provider_names"] == ["fake-memory"]
    assert snapshot["provider_count"] == 1
    assert '"mode": "hybrid"' in snapshot["memory_prefetch_params_by_provider"]
    assert '"top_k": 8' in snapshot["memory_prefetch_params_by_provider"]
    assert snapshot["configured_provider"] == "fake-memory"
    assert snapshot["provider_available"] is True
    assert snapshot["prefetch_attempted"] is True
    assert captured["prefetch_all_with_details"]["session_id"] == "session-1"
    assert captured["initialize_calls"] == 1
    assert captured["prefetch_calls"] == 1

    cached_snapshot = _support(runner).build_memory_context_snapshot(
        user_message="testtest",
        source=_make_source(),
        session_key="agent:main:napcat:dm:chat-1",
        session_id="session-1",
        turn_number=4,
    )

    assert cached_snapshot["provider_prefetch_cache_hit"] is True
    assert captured["initialize_calls"] == 1
    assert captured["prefetch_calls"] == 1


def test_orchestrator_router_memory_snapshot_reuses_cached_provider_context_for_ttl(monkeypatch):
    runner = _make_runner()
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {"memory": {"provider": "fake-memory"}})

    captured = {"prefetch_calls": 0}
    fake_now = {"value": 1_000.0}

    class _FakeProvider:
        name = "fake-memory"

        def is_available(self):
            return True

        def get_tool_schemas(self):
            return []

    class _FakeMemoryManager:
        def add_provider(self, provider):
            captured["provider_name"] = getattr(provider, "name", "")

        def initialize_all(self, **kwargs):
            captured["initialize_all"] = dict(kwargs)

        def on_turn_start(self, turn_number, query, platform=""):
            captured["on_turn_start"] = {
                "turn_number": turn_number,
                "query": query,
                "platform": platform,
            }

        def prefetch_all_with_details(self, query, *, session_id=""):
            captured["prefetch_calls"] += 1
            captured["last_query"] = query
            captured["last_session_id"] = session_id
            return [
                {
                    "provider": "fake-memory",
                    "content": "Known user preference",
                    "prefetch_debug": {"query": query},
                }
            ]

        def shutdown_all(self):
            captured["shutdown_all"] = True

    monkeypatch.setattr("gateway.run.MemoryManager", _FakeMemoryManager, raising=False)
    monkeypatch.setattr("agent.memory_manager.MemoryManager", _FakeMemoryManager)
    monkeypatch.setattr("plugins.memory.load_memory_provider", lambda name: _FakeProvider())
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: "/tmp/hermes-home")
    monkeypatch.setattr("gateway.napcat_orchestrator_support.time.time", lambda: fake_now["value"])
    monkeypatch.setattr(
        _support(runner),
        "_build_builtin_memory_context_block",
        lambda *, source: "",
    )

    first = _support(runner).build_router_memory_context_snapshot(
        user_message="我是后端",
        source=_make_source(),
        session_key="agent:main:napcat:dm:chat-1",
        session_id="session-1",
        turn_number=1,
    )
    fake_now["value"] += 120
    second = _support(runner).build_router_memory_context_snapshot(
        user_message="我是后端",
        source=_make_source(),
        session_key="agent:main:napcat:dm:chat-1",
        session_id="session-1",
        turn_number=2,
    )
    fake_now["value"] += 120
    fake_now["value"] += 601
    third = _support(runner).build_router_memory_context_snapshot(
        user_message="我是猫家长",
        source=_make_source(),
        session_key="agent:main:napcat:dm:chat-1",
        session_id="session-1",
        turn_number=3,
    )
    fake_now["value"] += 120
    fourth = _support(runner).build_router_memory_context_snapshot(
        user_message="我住在上海",
        source=_make_source(),
        session_key="agent:main:napcat:dm:chat-1",
        session_id="session-1",
        turn_number=4,
    )

    assert first["provider_prefetch_cache_hit"] is False
    assert second["provider_prefetch_cache_hit"] is True
    assert third["provider_prefetch_cache_hit"] is False
    assert fourth["provider_prefetch_cache_hit"] is False
    assert captured["prefetch_calls"] == 3
    assert captured["last_query"] == "我住在上海"
    assert second["memory_prefetch_content"] == "Known user preference"
    assert fourth["provider_runtime_reused"] is True


def test_invalidate_provider_memory_snapshot_keeps_clean_prefetch_cache():
    runner = _make_runner()
    support = _support(runner)
    handle = _ProviderRuntimeHandle(
        cache_key="cache-1",
        session_key="agent:main:napcat:dm:chat-1",
        session_id="session-1",
        provider_name="fake-memory",
        profile_name="",
        provider=SimpleNamespace(should_invalidate_cached_prefetch=lambda: False),
        last_message_signature="sig-1",
        last_snapshot={"provider_block": "<memory-context>cached</memory-context>"},
        last_snapshot_ts=123.0,
    )
    support._provider_runtime_cache[handle.cache_key] = handle

    support.invalidate_provider_memory_snapshot("agent:main:napcat:dm:chat-1")

    assert handle.last_message_signature == "sig-1"
    assert handle.last_snapshot["provider_block"] == "<memory-context>cached</memory-context>"
    assert handle.last_snapshot_ts == 123.0


def test_invalidate_provider_memory_snapshot_drops_dirty_prefetch_cache():
    runner = _make_runner()
    support = _support(runner)
    handle = _ProviderRuntimeHandle(
        cache_key="cache-1",
        session_key="agent:main:napcat:dm:chat-1",
        session_id="session-1",
        provider_name="fake-memory",
        profile_name="",
        provider=SimpleNamespace(should_invalidate_cached_prefetch=lambda: True),
        last_message_signature="sig-1",
        last_snapshot={"provider_block": "<memory-context>cached</memory-context>"},
        last_snapshot_ts=123.0,
    )
    support._provider_runtime_cache[handle.cache_key] = handle

    support.invalidate_provider_memory_snapshot("agent:main:napcat:dm:chat-1")

    assert handle.last_message_signature == ""
    assert handle.last_snapshot == {}
    assert handle.last_snapshot_ts == 0.0


def test_orchestrator_memory_context_falls_back_to_builtin_memory_store(monkeypatch):
    runner = _make_runner()
    runner.config.memory = SimpleNamespace(
        provider="",
        memory_enabled=True,
        user_profile_enabled=True,
        memory_char_limit=2200,
        user_char_limit=1375,
        chat_char_limit=2200,
    )

    captured = {}

    class _FakeMemoryStore:
        def __init__(self, **kwargs):
            captured["init"] = dict(kwargs)

        def load_from_disk(self):
            captured["loaded"] = True

        def format_for_system_prompt(self, target):
            blocks = {
                "memory": "MEMORY BLOCK",
                "chat": "",
                "user": "USER BLOCK",
            }
            return blocks.get(target)

    monkeypatch.setattr("tools.memory_tool.MemoryStore", _FakeMemoryStore)

    block = _support(runner).build_memory_context_block(
        user_message="记一下我喜欢简洁回复",
        source=_make_source(),
        session_key="agent:main:napcat:dm:chat-1",
        session_id="session-1",
        turn_number=3,
    )

    assert "MEMORY BLOCK" in block
    assert "USER BLOCK" in block
    assert captured["loaded"] is True
    assert captured["init"]["platform"] == "napcat"
    assert captured["init"]["user_id"] == "user-1"
    assert captured["init"]["chat_id"] == "chat-1"
    assert captured["init"]["chat_type"] == "dm"


def test_orchestrator_memory_context_swallows_provider_registration_failure(monkeypatch):
    runner = _make_runner()
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {"memory": {"provider": "fake-memory"}})

    class _FakeProvider:
        name = "fake-memory"

        def is_available(self):
            return True

    class _FakeMemoryManager:
        def add_provider(self, provider):
            del provider
            raise AttributeError("provider missing _agent_context")

        def shutdown_all(self):
            return None

    monkeypatch.setattr("gateway.run.MemoryManager", _FakeMemoryManager, raising=False)
    monkeypatch.setattr("agent.memory_manager.MemoryManager", _FakeMemoryManager)
    monkeypatch.setattr("plugins.memory.load_memory_provider", lambda name: _FakeProvider())

    block = _support(runner).build_memory_context_block(
        user_message="testtest",
        source=_make_source(),
        session_key="agent:main:napcat:dm:chat-1",
        session_id="session-1",
        turn_number=3,
    )

    assert block == ""


@pytest.mark.asyncio
async def test_run_orchestrator_agent_compacts_history_tool_noise(monkeypatch):
    captured = {}

    class _FakeAgent:
        def __init__(self, **kwargs):
            captured["init"] = dict(kwargs)
            captured["agent"] = self
            captured["agent"] = self
            captured["agent"] = self

        def run_conversation(self, user_message, conversation_history, turn_system_context=None, turn_user_context=None):
            captured["run"] = {
                "user_message": user_message,
                "conversation_history": list(conversation_history or []),
                "turn_system_context": turn_system_context,
                "turn_user_context": turn_user_context,
            }
            return {"final_response": '{"complexity_score":1,"respond_now":true,"immediate_reply":"ok"}'}

    runner = _make_runner()
    runner._resolve_session_agent_runtime = lambda source, session_key: ("fallback-model", {"provider": "openrouter"})

    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    history = [
        {"role": "session_meta", "tools": [{"name": "terminal"}]},
        {"role": "user", "content": "please inspect the repo"},
        {
            "role": "assistant",
            "content": "I'll inspect it.",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "terminal", "arguments": '{"command":"rg orchestrator"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "tool_name": "terminal",
            "content": "very long raw rg output that should not reach the orchestrator",
        },
        {"role": "assistant", "content": "I found the relevant files."},
    ]

    await _runtime(runner).run_orchestrator_agent(
        session_key="agent:main:napcat:dm:chat-1",
        source=_make_source(),
        history=history,
        user_message="ping",
    )

    compact_history = captured["run"]["conversation_history"]
    # Tool summaries and visible assistant replies stay split so the
    # orchestrator can distinguish separate assistant-side events.
    assert compact_history == [
        {"role": "user", "content": "please inspect the repo"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"Tool activity summary: used terminal."},"complexity_score":1}',
        },
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"I found the relevant files."},"complexity_score":1}',
        },
    ]
    assert captured["run"]["turn_system_context"] is None
    assert captured["run"]["turn_user_context"] is None
    assert all(msg.get("role") != "tool" for msg in compact_history)
    assert all("tool_calls" not in msg for msg in compact_history)
    assert captured["init"]["ephemeral_system_prompt"] is None
    assert captured["init"]["skip_context_files"] is True
    assert captured["init"]["skip_memory"] is True


def test_compact_orchestrator_history_preserves_existing_json_decisions():
    from gateway.run import GatewayRunner

    history = [
        {"role": "user", "content": "ping"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"ok"}}',
        },
    ]

    compact = NapCatOrchestratorSupport.compact_history(history)

    assert compact == [
        {"role": "user", "content": "ping"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"ok"},"complexity_score":1}',
        },
    ]


def test_compact_orchestrator_history_keeps_spawn_summary_as_canonical_decision():
    compact = NapCatOrchestratorSupport.compact_history(
        [
            {
                "role": "assistant",
                "content": (
                    "[[ORCH_COMPLEXITY:72]] Routing decision; action=spawn; "
                    "goals=analyze the repo deeply"
                ),
            }
        ]
    )

    assert compact == [
        {
            "role": "assistant",
            "content": '{"action":"spawn","payload":{"goal":"analyze the repo deeply"},"complexity_score":72,"history_source":"orchestrator"}',
        },
    ]


def test_compact_orchestrator_history_recovers_reply_from_internal_summary():
    from gateway.run import GatewayRunner

    compact = NapCatOrchestratorSupport.compact_history(
        [
            {
                "role": "assistant",
                "content": (
                    "[[ORCH_COMPLEXITY:2]] Routing decision; action=reply; complexity_score=2; "
                    "reply_kind=final; reply_summary=在，我在。"
                ),
            }
        ]
    )

    assert compact == [
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"在，我在。"},"complexity_score":2,"history_source":"orchestrator"}',
        },
    ]


def test_compact_orchestrator_history_strips_legacy_duplicate_reply_wrapper():
    compact = NapCatOrchestratorSupport.compact_history(
        [
            {
                "role": "assistant",
                "content": (
                    "知道啦！以后回答你的问题都会说明白依据的哈？这样你也能判断我说的是不是靠谱哒哟。说吧，这次想问啥？ "
                    "Routing decision; action=reply; complexity_score=8; reply_kind=final; "
                    "reply_summary=知道啦！以后回答你的问题都会说明白依据的哈？这样你也能判断我说的是不是靠谱哒哟。说吧，这次想问啥？"
                ),
            }
        ]
    )

    assert compact == [
        {
            "role": "assistant",
            "content": (
                '{"action":"reply","payload":{"text":"知道啦！以后回答你的问题都会说明白依据的哈？这样你也能判断我说的是不是靠谱哒哟。说吧，这次想问啥？"},'
                '"complexity_score":8,"history_source":"orchestrator"}'
            ),
        },
    ]


def test_compact_orchestrator_history_splits_embedded_spawn_and_full_agent_reply():
    compact = NapCatOrchestratorSupport.compact_history(
        [
            {
                "role": "assistant",
                "content": (
                    '{"action":"reply","payload":{"text":"{\\"action\\":\\"spawn\\",\\"payload\\":{\\"goal\\":\\"Try to turn on the desk lamp of the Wuma family\\"},'
                    '\\"complexity_score\\":34} {\\"action\\":\\"reply\\",\\"payload\\":{\\"text\\":\\"搞定啦～五马家的台灯已经亮起来了的说！✨ 还有什么要调整的吗，比如亮度什么的？\\"},'
                    '\\"complexity_score\\":34}"},"complexity_score":1}'
                ),
            }
        ]
    )

    assert compact == [
        {
            "role": "assistant",
            "content": (
                '{"action":"spawn","payload":{"goal":"Try to turn on the desk lamp of the Wuma family"},'
                '"complexity_score":34,"history_source":"orchestrator"}'
            ),
        },
        {
            "role": "assistant",
            "content": (
                '{"action":"reply","payload":{"text":"搞定啦～五马家的台灯已经亮起来了的说！✨ 还有什么要调整的吗，比如亮度什么的？"},'
                '"complexity_score":34,"history_source":"full_agent"}'
            ),
        },
    ]


def test_compact_orchestrator_history_keeps_stable_prefix_when_truncated():
    history = []
    for i in range(13):
        history.append({"role": "user", "content": f"user-{i}"})
        history.append(
            {
                "role": "assistant",
                "content": f'{{"action":"reply","payload":{{"text":"assistant-{i}"}}}}',
            }
        )

    compact = NapCatOrchestratorSupport.compact_history(
        history,
        max_messages=24,
    )

    assert compact[:8] == [
        {"role": "user", "content": "user-0"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-0"},"complexity_score":1}',
        },
        {"role": "user", "content": "user-1"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-1"},"complexity_score":1}',
        },
        {"role": "user", "content": "user-2"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-2"},"complexity_score":1}',
        },
        {"role": "user", "content": "user-3"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-3"},"complexity_score":1}',
        },
    ]
    assert compact[8:12] == [
        {"role": "user", "content": "user-5"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-5"},"complexity_score":1}',
        },
        {"role": "user", "content": "user-6"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-6"},"complexity_score":1}',
        },
    ]
    assert compact[-2:] == [
        {"role": "user", "content": "user-12"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-12"},"complexity_score":1}',
        },
    ]


def test_build_routing_history_keeps_frozen_stable_prefix_after_window_shifts():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:group:chat-1")
    support = _support(runner)

    for i in range(24):
        session.append_routing_history_entry(role="user", content=f"user-{i}")
        session.append_routing_history_entry(
            role="assistant",
            content=f'{{"action":"reply","payload":{{"text":"assistant-{i}"}}}}',
        )

    baseline = support.build_routing_history(
        session=session,
        history=[],
    )
    assert baseline[:8] == [
        {"role": "user", "content": "user-0"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-0"},"complexity_score":1}',
        },
        {"role": "user", "content": "user-1"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-1"},"complexity_score":1}',
        },
        {"role": "user", "content": "user-2"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-2"},"complexity_score":1}',
        },
        {"role": "user", "content": "user-3"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-3"},"complexity_score":1}',
        },
    ]
    assert session.routing_stable_prefix == baseline[:8]

    for i in range(24, 26):
        session.append_routing_history_entry(role="user", content=f"user-{i}")
        session.append_routing_history_entry(
            role="assistant",
            content=f'{{"action":"reply","payload":{{"text":"assistant-{i}"}}}}',
        )

    shifted_compact = NapCatOrchestratorSupport.compact_history(
        list(session.routing_history),
        max_messages=24,
    )
    assert shifted_compact[:8] == [
        {"role": "user", "content": "user-2"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-2"},"complexity_score":1}',
        },
        {"role": "user", "content": "user-3"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-3"},"complexity_score":1}',
        },
        {"role": "user", "content": "user-4"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-4"},"complexity_score":1}',
        },
        {"role": "user", "content": "user-5"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-5"},"complexity_score":1}',
        },
    ]

    rebuilt = support.build_routing_history(
        session=session,
        history=[],
    )
    assert rebuilt[:8] == baseline[:8]
    assert rebuilt[-2:] == [
        {"role": "user", "content": "user-25"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"assistant-25"},"complexity_score":1}',
        },
    ]


def test_build_routing_history_for_model_keeps_full_history_within_context_budget():
    runner = _make_runner()
    runner.config = GatewayConfig(
        platforms={
            Platform.NAPCAT: PlatformConfig(
                enabled=True,
                extra={
                    "orchestrator": {
                        "enabled_platforms": ["napcat"],
                        "orchestrator_context_length": 32000,
                    }
                },
            )
        }
    )
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:dm:chat-1")
    support = _support(runner)

    for i in range(30):
        session.append_routing_history_entry(
            role="user",
            content=f"user-{i}",
            max_entries=0,
        )
        session.append_routing_history_entry(
            role="assistant",
            content=f'{{"action":"reply","payload":{{"text":"assistant-{i}"}}}}',
            max_entries=0,
        )

    full_compact = NapCatOrchestratorSupport.compact_history(
        list(session.routing_history),
        max_messages=0,
    )
    result = support.build_routing_history_for_model(
        session=session,
        history=[],
        model="doubao-seed-2-0-mini-260215",
        runtime_kwargs={"provider": "custom", "base_url": "https://example.invalid"},
        system_prompt="router prompt",
        user_message="latest turn",
    )

    assert result == full_compact
    assert len(result) == 60


def test_build_routing_history_for_model_trims_only_when_context_exceeded():
    runner = _make_runner()
    runner.config = GatewayConfig(
        platforms={
            Platform.NAPCAT: PlatformConfig(
                enabled=True,
                extra={
                    "orchestrator": {
                        "enabled_platforms": ["napcat"],
                        "orchestrator_context_length": 360,
                    }
                },
            )
        }
    )
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:dm:chat-1")
    support = _support(runner)

    for i in range(10):
        session.append_routing_history_entry(
            role="user",
            content=f"user-{i} " + ("x" * 80),
            max_entries=0,
        )
        session.append_routing_history_entry(
            role="assistant",
            content=json.dumps(
                {
                    "action": "reply",
                    "payload": {"text": f"assistant-{i} " + ("y" * 80)},
                },
                ensure_ascii=False,
            ),
            max_entries=0,
        )

    full_compact = NapCatOrchestratorSupport.compact_history(
        list(session.routing_history),
        max_messages=0,
    )
    result = support.build_routing_history_for_model(
        session=session,
        history=[],
        model="doubao-seed-2-0-mini-260215",
        runtime_kwargs={"provider": "custom", "base_url": "https://example.invalid"},
        system_prompt="router prompt",
        user_message="latest turn " + ("z" * 80),
    )

    assert len(result) < len(full_compact)
    halved_sizes = []
    size = len(full_compact)
    while size > 1:
        size = max(1, size // 2)
        halved_sizes.append(size)
        if size == 1:
            break
    assert len(result) in halved_sizes
    preserved = min(8, len(result))
    assert result[:preserved] == full_compact[:preserved]


def test_seed_routing_history_recovers_ignore_decision_from_session_meta():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:group:chat-1")

    _support(runner).seed_routing_history(
        session,
        [
            {"role": "user", "content": "收到"},
            {
                "role": "session_meta",
                "content": '{"action":"ignore","payload":{},"complexity_score":1}',
            },
            {"role": "user", "content": "继续说一下刚才那个问题"},
        ],
    )

    assert session.routing_history == [
        {"role": "user", "content": "收到"},
        {"role": "assistant", "content": '{"action":"ignore","payload":{},"complexity_score":1,"history_source":"orchestrator"}'},
        {"role": "user", "content": "继续说一下刚才那个问题"},
    ]


@pytest.mark.asyncio
async def test_run_orchestrator_agent_reuses_local_run_conversation_path(monkeypatch):
    captured = {}

    class _FakeAgent:
        def __init__(self, **kwargs):
            captured["init"] = dict(kwargs)
            captured["agent"] = self

        def run_conversation(self, user_message, conversation_history, turn_system_context=None, turn_user_context=None):
            captured["run"] = {
                "user_message": user_message,
                "conversation_history": list(conversation_history or []),
                "turn_system_context": turn_system_context,
                "turn_user_context": turn_user_context,
            }
            return {
                "final_response": '{"complexity_score":1,"respond_now":true,"immediate_reply":"ok"}',
                "last_reasoning": "brief reasoning",
            }

    runner = _make_runner()
    runner.config = GatewayConfig(
        gateway_orchestrator=GatewayOrchestratorConfig(
            enabled_platforms=["napcat"],
            orchestrator_model="doubao-seed-2-0-mini-260215",
            orchestrator_provider="custom",
            orchestrator_base_url="https://ark.cn-beijing.volces.com/api/v3",
            orchestrator_api_key="volcengine-test-key",
            orchestrator_reasoning_effort="low",
        )
    )
    runner._resolve_session_agent_runtime = lambda source, session_key: ("fallback-model", {"provider": "openrouter"})

    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    result = await _runtime(runner).run_orchestrator_agent(
        session_key="agent:main:napcat:dm:chat-1",
        source=_make_source(),
        history=[{"role": "user", "content": "hi"}],
        user_message="ping",
    )

    assert result["final_response"] == '{"complexity_score":1,"respond_now":true,"immediate_reply":"ok"}'
    assert result["last_reasoning"] == "brief reasoning"
    assert captured["run"]["user_message"] == "ping"
    assert captured["run"]["conversation_history"] == [
        {"role": "user", "content": "hi"},
    ]
    assert captured["run"]["turn_system_context"] is None
    assert captured["run"]["turn_user_context"] is None
    assert captured["init"]["ephemeral_system_prompt"] is None
    assert captured["init"]["api_mode"] == "chat_completions"
    assert captured["init"]["provider"] == "custom"
    assert captured["init"]["skip_context_files"] is True
    assert captured["init"]["skip_memory"] is True
    assert captured["init"]["runtime_context"].agent_context == "orchestrator"
    assert captured["agent"]._cached_system_prompt == _support(runner).build_system_prompt("dm")
    assert captured["agent"]._build_system_prompt() == _support(runner).build_system_prompt("dm")


@pytest.mark.asyncio
async def test_run_orchestrator_agent_passes_routing_system_context(monkeypatch):
    captured = {}

    class _FakeAgent:
        def __init__(self, **kwargs):
            captured["init"] = dict(kwargs)
            captured["agent"] = self

        def run_conversation(self, user_message, conversation_history, turn_system_context=None, turn_user_context=None):
            captured["run"] = {
                "user_message": user_message,
                "conversation_history": list(conversation_history or []),
                "turn_system_context": turn_system_context,
                "turn_user_context": turn_user_context,
            }
            return {"final_response": '{"complexity_score":1,"respond_now":true,"immediate_reply":"ok"}'}

    runner = _make_runner()
    runner._resolve_session_agent_runtime = lambda source, session_key: ("fallback-model", {"provider": "openrouter"})

    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    await _runtime(runner).run_orchestrator_agent(
        session_key="agent:main:napcat:dm:chat-1",
        source=_make_source(chat_type="group"),
        history=[{"role": "user", "content": "hi"}],
        user_message="ping",
        turn_system_context=(
            "[Routing note: reuse existing child status when available.]\n"
            "Prefer status replies over duplicate spawns."
        ),
    )

    assert captured["run"]["turn_system_context"] == (
        "[Routing note: reuse existing child status when available.]\n"
        "Prefer status replies over duplicate spawns."
    )
    assert captured["run"]["turn_user_context"] is None
    assert captured["init"]["ephemeral_system_prompt"] is None
    assert captured["agent"]._cached_system_prompt == _support(runner).build_system_prompt("group")


@pytest.mark.asyncio
async def test_run_orchestrator_agent_passes_persona_as_turn_user_context(monkeypatch):
    captured = {}

    class _FakeAgent:
        def __init__(self, **kwargs):
            captured["init"] = dict(kwargs)

        def run_conversation(self, user_message, conversation_history, turn_system_context=None, turn_user_context=None):
            captured["run"] = {
                "user_message": user_message,
                "conversation_history": list(conversation_history or []),
                "turn_system_context": turn_system_context,
                "turn_user_context": turn_user_context,
            }
            return {"final_response": '{"complexity_score":1,"respond_now":true,"immediate_reply":"ok"}'}

    runner = _make_runner()
    runner._resolve_session_agent_runtime = lambda source, session_key: ("fallback-model", {"provider": "openrouter"})

    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    await _runtime(runner).run_orchestrator_agent(
        session_key="agent:main:napcat:dm:chat-1",
        source=_make_source(),
        history=[],
        user_message="ping",
        turn_user_context="<persona>\nNapCat persona\n</persona>",
    )

    assert captured["run"]["turn_system_context"] is None
    assert captured["run"]["turn_user_context"] == "<persona>\nNapCat persona\n</persona>"


def test_gateway_config_enables_napcat_orchestrator():
    config = GatewayConfig(
        gateway_orchestrator=GatewayOrchestratorConfig(enabled_platforms=["napcat"])
    )

    assert config.gateway_orchestrator.is_enabled_for_platform(Platform.NAPCAT) is True
    assert config.gateway_orchestrator.is_enabled_for_platform(Platform.TELEGRAM) is False


def test_gateway_orchestrator_defaults_are_platform_neutral():
    config = GatewayConfig()

    assert config.gateway_orchestrator.is_enabled_for_platform(Platform.NAPCAT) is False
    assert config.gateway_orchestrator.is_enabled_for_platform(Platform.TELEGRAM) is False
    assert config.gateway_orchestrator.orchestrator_model == ""
    assert config.gateway_orchestrator.orchestrator_provider == ""
    assert config.gateway_orchestrator.orchestrator_base_url == ""
    assert config.gateway_orchestrator.orchestrator_reasoning_effort == ""
    assert config.gateway_orchestrator.orchestrator_context_length == 0
    assert config.gateway_orchestrator.routing_history_max_entries == 0
    assert config.gateway_orchestrator.routing_stable_prefix_messages == 8


def test_gateway_orchestrator_from_dict_defaults_are_platform_neutral():
    cfg = GatewayOrchestratorConfig.from_dict({})

    assert cfg.enabled_platforms == []
    assert cfg.is_enabled_for_platform(Platform.NAPCAT) is False
    assert cfg.orchestrator_model == ""
    assert cfg.orchestrator_provider == ""
    assert cfg.orchestrator_base_url == ""
    assert cfg.orchestrator_reasoning_effort == ""
    assert cfg.orchestrator_context_length == 0
    assert cfg.routing_history_max_entries == 0
    assert cfg.routing_stable_prefix_messages == 8


def test_gateway_orchestrator_from_dict_allows_explicit_disable():
    cfg = GatewayOrchestratorConfig.from_dict({"enabled_platforms": []})

    assert cfg.enabled_platforms == []
    assert cfg.is_enabled_for_platform(Platform.NAPCAT) is False


@pytest.mark.asyncio
async def test_run_orchestrator_agent_uses_configured_runtime_override_and_low_reasoning(monkeypatch):
    captured = {}

    class _FakeAgent:
        def __init__(self, **kwargs):
            captured["init"] = dict(kwargs)

        def run_conversation(self, user_message, conversation_history, turn_system_context=None, turn_user_context=None):
            captured["run"] = {
                "user_message": user_message,
                "conversation_history": list(conversation_history or []),
                "turn_system_context": turn_system_context,
                "turn_user_context": turn_user_context,
            }
            return {"final_response": '{"complexity_score":1,"respond_now":true,"immediate_reply":"ok"}'}

    runner = _make_runner()
    runner.config = GatewayConfig(
        gateway_orchestrator=GatewayOrchestratorConfig(
            enabled_platforms=["napcat"],
            orchestrator_model="doubao-seed-2-0-mini-260215",
            orchestrator_provider="custom",
            orchestrator_base_url="https://ark.cn-beijing.volces.com/api/v3",
            orchestrator_api_key="volcengine-test-key",
            orchestrator_reasoning_effort="low",
        )
    )
    runner._resolve_session_agent_runtime = lambda source, session_key: ("fallback-model", {"provider": "openrouter"})

    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    result = await _runtime(runner).run_orchestrator_agent(
        session_key="agent:main:napcat:dm:chat-1",
        source=_make_source(),
        history=[{"role": "user", "content": "hi"}],
        user_message="ping",
    )

    assert result["final_response"]
    assert captured["init"]["model"] == "doubao-seed-2-0-mini-260215"
    assert captured["init"]["provider"] == "custom"
    assert captured["init"]["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"
    assert captured["init"]["api_key"] == "volcengine-test-key"
    assert captured["init"]["api_mode"] == "chat_completions"
    assert captured["init"]["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert captured["init"]["session_id"].endswith("::orchestrator")
    assert captured["init"]["session_db"] is None
    assert captured["init"]["persist_session"] is False
    assert captured["init"]["ephemeral_system_prompt"] is None
    assert captured["init"]["skip_context_files"] is True
    assert captured["init"]["skip_memory"] is True


@pytest.mark.asyncio
async def test_run_orchestrator_agent_streams_reasoning_to_observability(monkeypatch):
    emitted = []

    class _FakeAgent:
        def __init__(self, **kwargs):
            self.reasoning_callback = kwargs.get("reasoning_callback")
            self.stream_delta_callback = kwargs.get("stream_delta_callback")

        def run_conversation(self, user_message, conversation_history, turn_system_context=None, turn_user_context=None):
            if self.reasoning_callback:
                self.reasoning_callback("step 1")
                self.reasoning_callback("\nstep 2")
            return {
                "final_response": '{"complexity_score":1,"respond_now":true,"immediate_reply":"ok"}',
                "last_reasoning": "step 1\nstep 2",
            }

    runner = _make_runner()
    runner._resolve_session_agent_runtime = lambda source, session_key: ("fallback-model", {"provider": "openrouter"})

    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_emit_gateway_event",
        lambda event_type, payload, **kwargs: emitted.append((event_type, payload, kwargs)),
    )

    trace_ctx = NapcatTraceContext(
        trace_id="trace-1",
        span_id="span-root",
        session_key="agent:main:napcat:dm:chat-1",
        session_id="session-1",
        chat_type="dm",
        chat_id="chat-1",
        user_id="user-1",
        message_id="msg-1",
    )

    result = await _runtime(runner).run_orchestrator_agent(
        session_key="agent:main:napcat:dm:chat-1",
        source=_make_source(),
        history=[],
        user_message="ping",
        trace_ctx=trace_ctx,
    )

    assert result["last_reasoning"] == "step 1\nstep 2"
    reasoning = [
        payload for event_type, payload, _ in emitted
        if event_type == gateway_run._GatewayEventType.AGENT_REASONING_DELTA
    ]
    assert [payload["delta"] for payload in reasoning] == ["step 1", "\nstep 2"]
    assert all("content" not in payload for payload in reasoning)


def test_apply_runtime_override_payload_recomputes_api_mode_for_custom_endpoint():
    from gateway.run import GatewayRunner

    model, runtime = GatewayRunner._apply_runtime_override_payload(
        "MiniMax-M2.7-highspeed",
        {
            "provider": "minimax-cn",
            "api_key": "minimax-key",
            "base_url": "https://api.minimaxi.com/anthropic",
            "api_mode": "anthropic_messages",
        },
        {
            "model": "doubao-seed-2-0-mini-260215",
            "provider": "custom",
            "api_key": "ark-key",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        },
    )

    assert model == "doubao-seed-2-0-mini-260215"
    assert runtime["provider"] == "custom"
    assert runtime["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"
    assert runtime["api_mode"] == "chat_completions"


@pytest.mark.asyncio
async def test_run_orchestrator_turn_returns_main_reply():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 1, "respond_now": true, "immediate_reply": "quick answer", '
                '"spawn_children": [], "cancel_children": [], '
                '"ignore_message": false, "reasoning_summary": "simple"}'
            )
        }
    )
    runner._run_agent = AsyncMock()

    session_entry = _make_session_entry()
    event = _make_event("ping")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text="ping",
    )

    assert response == "quick answer"
    assert runner._run_agent.await_count == 0
    orchestrator_kwargs = _runtime(runner).run_orchestrator_agent.await_args.kwargs
    assert '"session_id": "session-1"' in orchestrator_kwargs["turn_system_context"]
    assert orchestrator_kwargs["turn_user_context"] is None
    transcript_roles = [call.args[1]["role"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_roles == ["user", "assistant"]


@pytest.mark.asyncio
async def test_run_orchestrator_turn_skips_provider_memory_enrich_for_direct_reply(monkeypatch):
    import gateway.run as gateway_run

    emitted: list[tuple[str, dict]] = []
    provider_calls = 0

    def _fake_emit(event_type, payload, trace_ctx=None, severity=None):
        del trace_ctx, severity
        emitted.append((event_type.value if hasattr(event_type, "value") else str(event_type), dict(payload)))

    monkeypatch.setattr(gateway_run, "_emit_gateway_event", _fake_emit)

    runner = _make_runner()
    support = _support(runner)
    original_router_builder = support.build_router_memory_context_snapshot
    original_provider_builder = support.build_provider_memory_context_snapshot_if_needed

    def _fake_router_snapshot(**kwargs):
        del kwargs
        return {
            "builtin_block": "",
            "provider_block": "",
            "combined_block": "<memory-context>\nKnown user preference\n</memory-context>",
            "provider_details": [],
            "provider_names": ["fake-memory"],
            "provider_count": 1,
            "configured_provider": "fake-memory",
            "provider_available": True,
            "prefetch_attempted": False,
            "provider_error": "",
            "memory_prefetch_by_provider": "",
            "memory_prefetch_content": "",
            "memory_prefetch_fenced": "",
            "memory_prefetch_params_by_provider": "",
            "provider_prefetch_cache_hit": True,
            "provider_runtime_reused": True,
            "provider_context_reused": True,
        }

    def _fake_provider_snapshot(**kwargs):
        nonlocal provider_calls
        provider_calls += 1
        del kwargs
        return {
            "configured_provider": "fake-memory",
            "provider_available": True,
            "prefetch_attempted": True,
            "provider_error": "",
            "provider_details": [
                {
                    "provider": "fake-memory",
                    "content": "Known user preference",
                    "prefetch_debug": {"mode": "hybrid"},
                }
            ],
            "provider_merged_content": "Known user preference",
            "provider_block": "<memory-context>\nKnown user preference\n</memory-context>",
            "memory_prefetch_fenced": "<memory-context>\nKnown user preference\n</memory-context>",
            "provider_prefetch_cache_hit": False,
            "provider_runtime_reused": True,
            "provider_context_reused": False,
        }

    support.build_router_memory_context_snapshot = _fake_router_snapshot
    support.build_provider_memory_context_snapshot_if_needed = _fake_provider_snapshot
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 1, "respond_now": true, "immediate_reply": "quick answer", '
                '"spawn_children": [], "cancel_children": [], '
                '"ignore_message": false, "reasoning_summary": "simple"}'
            )
        }
    )

    session_entry = _make_session_entry()
    event = _make_event("ping")
    event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-1")

    try:
        response = await _extension(runner).run_orchestrator_turn(
            event=event,
            source=event.source,
            session_entry=session_entry,
            session_key=session_entry.session_key,
            history=[],
            context_prompt="ctx",
            platform_turn=AdapterTurnPlan(),
            dynamic_disabled_skills=[],
            dynamic_disabled_toolsets=[],
            message_text="ping",
        )
    finally:
        support.build_router_memory_context_snapshot = original_router_builder
        support.build_provider_memory_context_snapshot_if_needed = original_provider_builder

    assert response == "quick answer"
    memory_events = [
        payload
        for event_type, payload in emitted
        if event_type == gateway_run._GatewayEventType.AGENT_MEMORY_USED.value
    ]
    assert provider_calls == 0
    assert len(memory_events) == 1
    assert memory_events[0]["source"] == "gateway_router_memory"
    assert memory_events[0]["agent_scope"] == "main_orchestrator"
    assert memory_events[0]["configured_provider"] == "fake-memory"
    assert memory_events[0]["provider_prefetch_cache_hit"] is True
    assert memory_events[0]["gateway_memory_context"].startswith("<memory-context>")
    assert event.metadata[PREFETCHED_MEMORY_CONTEXT_KEY].startswith("<memory-context>")
    orchestrator_kwargs = _runtime(runner).run_orchestrator_agent.await_args.kwargs
    assert "<memory-context>" in orchestrator_kwargs["turn_system_context"]
    assert orchestrator_kwargs["turn_user_context"] is None


@pytest.mark.asyncio
async def test_run_orchestrator_turn_enriches_provider_memory_for_child_handoff(monkeypatch):
    import gateway.run as gateway_run

    emitted: list[tuple[str, dict]] = []
    provider_calls = 0

    def _fake_emit(event_type, payload, trace_ctx=None, severity=None):
        del trace_ctx, severity
        emitted.append((event_type.value if hasattr(event_type, "value") else str(event_type), dict(payload)))

    monkeypatch.setattr(gateway_run, "_emit_gateway_event", _fake_emit)

    runner = _make_runner()
    support = _support(runner)
    original_router_builder = support.build_router_memory_context_snapshot
    original_provider_builder = support.build_provider_memory_context_snapshot_if_needed

    def _fake_router_snapshot(**kwargs):
        del kwargs
        return {
            "builtin_block": "",
            "provider_block": "",
            "combined_block": "<memory-context>\nKnown user preference\n</memory-context>",
            "provider_details": [],
            "provider_names": ["fake-memory"],
            "provider_count": 1,
            "configured_provider": "fake-memory",
            "provider_available": True,
            "prefetch_attempted": False,
            "provider_error": "",
            "memory_prefetch_by_provider": "",
            "memory_prefetch_content": "",
            "memory_prefetch_fenced": "",
            "memory_prefetch_params_by_provider": "",
            "provider_prefetch_cache_hit": True,
            "provider_runtime_reused": True,
            "provider_context_reused": True,
        }

    def _fake_provider_snapshot(**kwargs):
        nonlocal provider_calls
        provider_calls += 1
        del kwargs
        return {
            "configured_provider": "fake-memory",
            "provider_available": True,
            "prefetch_attempted": True,
            "provider_error": "",
            "provider_details": [
                {
                    "provider": "fake-memory",
                    "content": "Known user preference",
                    "prefetch_debug": {"mode": "hybrid"},
                }
            ],
            "provider_merged_content": "Known user preference",
            "provider_block": "<memory-context>\nKnown user preference\n</memory-context>",
            "memory_prefetch_fenced": "<memory-context>\nKnown user preference\n</memory-context>",
            "provider_prefetch_cache_hit": False,
            "provider_runtime_reused": True,
            "provider_context_reused": False,
        }

    support.build_router_memory_context_snapshot = _fake_router_snapshot
    support.build_provider_memory_context_snapshot_if_needed = _fake_provider_snapshot
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 18, "respond_now": false, "immediate_reply": "", '
                '"spawn_children": [{"goal": "deep work"}], "cancel_children": [], '
                '"ignore_message": false, "reasoning_summary": "needs deeper work"}'
            )
        }
    )
    _runtime(runner).spawn_orchestrator_children = AsyncMock(
        return_value={"started": 1, "queued": 0, "reused": 0, "skipped": 0, "started_child_ids": []}
    )

    session_entry = _make_session_entry()
    event = _make_event("ping")
    event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-1")

    try:
        response = await _extension(runner).run_orchestrator_turn(
            event=event,
            source=event.source,
            session_entry=session_entry,
            session_key=session_entry.session_key,
            history=[],
            context_prompt="ctx",
            platform_turn=AdapterTurnPlan(),
            dynamic_disabled_skills=[],
            dynamic_disabled_toolsets=[],
            message_text="ping",
        )
    finally:
        support.build_router_memory_context_snapshot = original_router_builder
        support.build_provider_memory_context_snapshot_if_needed = original_provider_builder

    assert response is None
    assert provider_calls == 1
    _runtime(runner).spawn_orchestrator_children.assert_awaited_once()
    child_context_prompt = _runtime(runner).spawn_orchestrator_children.await_args.kwargs["context_prompt"]
    assert child_context_prompt.startswith("ctx")
    assert "<memory-context>" in child_context_prompt
    memory_events = [
        payload
        for event_type, payload in emitted
        if event_type == gateway_run._GatewayEventType.AGENT_MEMORY_USED.value
    ]
    assert len(memory_events) == 2
    assert memory_events[0]["source"] == "gateway_router_memory"
    assert memory_events[1]["source"] == "gateway_provider_enrich"
    assert memory_events[1]["configured_provider"] == "fake-memory"
    assert memory_events[1]["provider_count"] == 1
    assert memory_events[1]["memory_prefetch_content"] == "Known user preference"
    assert memory_events[1]["memory_prefetch_fenced"].startswith("<memory-context>")
    assert memory_events[1]["gateway_memory_context"].startswith("<memory-context>")
    assert memory_events[1]["injection_sources"] == ["memory_provider_prefetch"]
    assert event.metadata[PREFETCHED_MEMORY_CONTEXT_KEY].startswith("<memory-context>")


@pytest.mark.asyncio
async def test_orchestrator_user_correction_keeps_llm_reply():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"action":"reply","payload":{"text":"收到，我记住了。"},"complexity_score":3}'
            )
        }
    )
    _runtime(runner).spawn_orchestrator_children = AsyncMock(
        return_value={"started": 1, "queued": 0, "reused": 0, "skipped": 0, "started_child_ids": []}
    )
    runner._run_agent = AsyncMock()

    session_entry = _make_session_entry()
    event = _make_event("不是，我是后端，不是前端")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == "收到，我记住了。"
    _runtime(runner).spawn_orchestrator_children.assert_not_awaited()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    assert session.last_decision.respond_now is True
    assert not session.last_decision.spawn_children
    transcript_replies = [call.args[1]["content"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert "收到，我记住了。" in transcript_replies


@pytest.mark.asyncio
async def test_run_orchestrator_turn_spawns_child_without_user_visible_handoff_messages():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 88, "respond_now": false, "immediate_reply": "working...", '
                '"spawn_children": [{"goal": "analyze repo", "allowed_toolsets": ["terminal"], "may_reply_directly": true}], '
                '"cancel_children": [], "ignore_message": false, "reasoning_summary": "needs deeper work"}'
            )
        }
    )
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "child result",
            "messages": [],
            "failed": False,
            "already_sent": False,
        }
    )

    session_entry = _make_session_entry()
    event = _make_event("please analyze this repo deeply")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=attach_napcat_promotion_plan(
            AdapterTurnPlan(),
            {
                "promotion_enabled": True,
                "promotion_stage": "l2",
                "turn_model_override": {"model": "router-picked"},
                "promotion_config_snapshot": {
                    "l1": {"model": "haiku"},
                    "l2": {"model": "opus"},
                },
            },
        ),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    runner._run_agent.assert_awaited_once()
    assert runner._run_agent.await_args.kwargs["enabled_toolsets_override"] == ["terminal"]
    assert runner._run_agent.await_args.kwargs["turn_model_override"] == {"model": "opus"}
    orchestrator_session = runner._get_gateway_extension().get_orchestrator_session(
        session_entry.session_key
    )
    assert orchestrator_session.active_child_count() == 0
    assert orchestrator_session.recent_children[0].status == "completed"
    assert orchestrator_session.recent_children[0].complexity_score == 88
    assert runner.adapters[Platform.NAPCAT].send.await_count == 1

    replies = [call.args[1]["content"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert "child result" in replies
    assert replies.count("please analyze this repo deeply") == 1
    assert replies.count("child result") == 1
    assert not any("working..." in reply for reply in replies)
    assert not any("Started background task:" in reply for reply in replies)


@pytest.mark.asyncio
async def test_run_orchestrator_turn_routes_low_complexity_spawn_to_l1():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"action":"spawn","complexity_score":16,'
                '"payload":{"goal":"answer who Hermes Gateway is and what it can do"}}'
            )
        }
    )
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "child result",
            "messages": [],
            "failed": False,
            "already_sent": False,
        }
    )

    session_entry = _make_session_entry()
    event = _make_event("who are you")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=attach_napcat_promotion_plan(
            AdapterTurnPlan(),
            {
                "promotion_enabled": True,
                "promotion_stage": "l1",
                "turn_model_override": {"model": "router-picked"},
                "promotion_config_snapshot": {
                    "l1": {"model": "haiku"},
                    "l2": {"model": "opus"},
                },
            },
        ),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    runner._run_agent.assert_awaited_once()
    assert runner._run_agent.await_args.kwargs["turn_model_override"] == {"model": "haiku"}


def test_record_orchestrator_routing_turn_persists_canonical_decision_json():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:dm:chat-1")

    _support(runner).record_routing_turn(
        session=session,
        history=[],
        user_message="analyze the repo deeply",
        decision=parse_orchestrator_decision(
            '{"action":"spawn","complexity_score":82,"payload":{"goal":"analyze the repo deeply"}}'
        ),
        spawn_result={"started": 1, "reused": 0, "skipped": 0},
    )

    assert session.routing_history[-1]["role"] == "assistant"
    assert session.routing_history[-1]["content"] == (
        '{"action":"spawn","payload":{"goal":"analyze the repo deeply"},"complexity_score":82,"history_source":"orchestrator"}'
    )


def test_orchestrator_related_complexity_floor_uses_same_topic_followup():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:dm:chat-1")
    session.append_routing_history_entry(role="user", content="请深入分析这个仓库的 orchestrator")
    session.append_routing_history_entry(
        role="assistant",
        content='{"action":"spawn","payload":{"goal":"请深入分析这个仓库的 orchestrator"},"complexity_score":78}',
    )

    assert _support(runner).related_complexity_floor(session, "继续展开说一下") == 78
    assert _support(runner).related_complexity_floor(session, "另外问个天气") is None


def test_full_agent_followup_requires_handoff_when_message_targets_recent_result():
    runner = _make_runner()
    session = _extension(runner).get_or_create_orchestrator_session("agent:main:napcat:dm:chat-1")
    session.append_routing_history_entry(role="user", content="看看五马这个家庭里面的所有设备的状态")
    session.append_routing_history_entry(
        role="assistant",
        content=_support(runner).canonicalize_history_decision(
            parse_orchestrator_decision(
                '{"action":"spawn","payload":{"goal":"Check the status of all devices in the Wuma family"},"complexity_score":20}'
            ),
            history_source="orchestrator",
        ),
    )
    session.append_routing_history_entry(
        role="assistant",
        content=_support(runner).canonicalize_reply_history_json(
            "五马家庭设备状态汇总：除湿机运行中，书房空调关闭。",
            complexity_score=20,
            history_source="full_agent",
            reply_kind="final",
            child_id="child-1",
        ),
    )

    assert _support(runner).full_agent_followup_requires_handoff(session, "看看除湿机细节信息")
    assert not _support(runner).full_agent_followup_requires_handoff(session, "你还在吗")


@pytest.mark.asyncio
async def test_orchestrator_fallback_answers_progress_question_from_child_board():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(side_effect=ValueError("bad json"))
    runner._run_agent = AsyncMock()

    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    child = GatewayChildState(
        child_id="child-1",
        session_key=session_entry.session_key,
        goal="analyze repo deeply",
        originating_user_message="analyze repo deeply",
        status="running",
        started_at="2026-04-17T10:00:00Z",
        current_step="iteration 2",
        current_tool="terminal",
        last_progress_message="Inspecting gateway orchestrator tests.",
        last_activity_ts="2026-04-17T10:01:00Z",
        progress_seq=3,
        dedupe_key="analyze repo deeply",
    )
    session.active_children[child.child_id] = child

    event = _make_event("跑到哪了")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert "Still working on:" in response
    assert "Current step: iteration 2." in response
    assert "Current tool: terminal." in response
    assert runner._run_agent.await_count == 0
    transcript_roles = [call.args[1]["role"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_roles == ["user"]


@pytest.mark.asyncio
async def test_orchestrator_turn_passes_formatted_routing_history_and_keeps_transcript_natural():
    runner = _make_runner()
    captured = {}
    runner._run_agent = AsyncMock()

    async def _fake_run_orchestrator_agent(**kwargs):
        captured["history"] = kwargs["history"]
        captured["turn_system_context"] = kwargs["turn_system_context"]
        captured["turn_user_context"] = kwargs["turn_user_context"]
        captured["user_message"] = kwargs["user_message"]
        return {
            "final_response": '{"action":"reply","payload":{"text":"在，我在。"}}'
        }

    _runtime(runner).run_orchestrator_agent = AsyncMock(side_effect=_fake_run_orchestrator_agent)

    session_entry = _make_session_entry()
    history = [
        {"role": "user", "content": "查一下最近的地震"},
        {"role": "assistant", "content": "还在查哦，刚启动的说。"},
        {"role": "user", "content": "查得如何了"},
        {"role": "assistant", "content": "还在跑呢的说~ 现在在第4轮。"},
    ]
    event = _make_event("好了吗!")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=history,
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(
            extra_prompt=(
                "[Platform note: NapCat platform conversation rules.]\n"
                "语气指令：务实元气少女"
            )
        ),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == "在，我在。"
    # History now includes assistant turns (seeded from the transcript) so the
    # orchestrator can see the alternating user/assistant conversation.
    # Assistant content is canonicalised to compact routing-JSON form.
    assert captured["history"] == [
        {"role": "user", "content": "查一下最近的地震"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"还在查哦，刚启动的说。"},"complexity_score":1}',
        },
        {"role": "user", "content": "查得如何了"},
        {
            "role": "assistant",
            "content": '{"action":"reply","payload":{"text":"还在跑呢的说~ 现在在第4轮。"},"complexity_score":1}',
        },
    ]
    assert captured["user_message"] == "好了吗!"
    assert captured["turn_user_context"] is None
    assert "[Platform note: NapCat platform conversation rules.]" not in captured["turn_system_context"]
    assert '"session_id": "session-1"' in captured["turn_system_context"]
    assert "[Context note: Current gateway turn context." in captured["turn_system_context"]

    session = runner._get_gateway_extension().get_orchestrator_session(
        session_entry.session_key
    )
    assert session.routing_history[-2] == {"role": "user", "content": "好了吗!"}
    assert session.routing_history[-1]["role"] == "assistant"
    assert session.routing_history[-1]["content"] == (
        '{"action":"reply","payload":{"text":"在，我在。"},"complexity_score":1,"history_source":"orchestrator","reply_kind":"final"}'
    )

    transcript_payloads = [call.args[1] for call in runner.session_store.append_to_transcript.call_args_list]
    transcript_texts = [payload["content"] for payload in transcript_payloads]
    assert transcript_texts == ["好了吗!", "在，我在。"]
    assert not any("Routing decision" in text for text in transcript_texts)


@pytest.mark.asyncio
async def test_orchestrator_turn_recovers_reply_summary_from_internal_protocol_text():
    runner = _make_runner()
    runner._run_agent = AsyncMock()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"action":"reply","complexity_score":2,"payload":{"text":"[[ORCH_COMPLEXITY:2]] '
                'Routing decision; action=reply; complexity_score=2; '
                'reply_kind=final; reply_summary=哎呀抱歉，是我记串了。"}}'
            )
        }
    )

    session_entry = _make_session_entry(chat_type="group")
    event = _make_event("不对啊，我没在这个群里和你说过这事", chat_type="group")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == "哎呀抱歉，是我记串了。"


@pytest.mark.asyncio
async def test_orchestrator_progress_reply_is_suppressed_when_adapter_disables_non_llm_status_messages():
    runner = _make_runner()
    runner.adapters[Platform.NAPCAT].EMIT_NON_LLM_STATUS_MESSAGES = False
    _runtime(runner).run_orchestrator_agent = AsyncMock(side_effect=ValueError("bad json"))
    runner._run_agent = AsyncMock()

    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    child = GatewayChildState(
        child_id="child-1",
        session_key=session_entry.session_key,
        goal="analyze repo deeply",
        originating_user_message="analyze repo deeply",
        status="running",
        started_at="2026-04-17T10:00:00Z",
        current_step="iteration 2",
        current_tool="terminal",
        last_progress_message="Inspecting gateway orchestrator tests.",
        last_activity_ts="2026-04-17T10:01:00Z",
        progress_seq=3,
        dedupe_key="analyze repo deeply",
    )
    session.active_children[child.child_id] = child

    event = _make_event("跑到哪了")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    transcript_roles = [call.args[1]["role"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_roles == ["user"]


@pytest.mark.asyncio
async def test_orchestrator_spawn_handoff_reply_is_not_suppressed_when_adapter_disables_status_messages():
    runner = _make_runner()
    runner.adapters[Platform.NAPCAT].EMIT_NON_LLM_STATUS_MESSAGES = False
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 88, "respond_now": false, "immediate_reply": "", '
                '"spawn_children": [{"goal": "different task", "may_reply_directly": true}], '
                '"spawn_handoff_reply": "我先继续处理手头这个，再接着帮你看 different task。", '
                '"cancel_children": [], "ignore_message": false, "reasoning_summary": "needs deeper work"}'
            )
        }
    )
    runner._run_agent = AsyncMock()

    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    session.active_children["child-1"] = GatewayChildState(
        child_id="child-1",
        session_key=session_entry.session_key,
        goal="existing task",
        originating_user_message="existing task",
        status="running",
        current_step="iteration 3",
        current_tool="terminal",
        last_progress_message="Still digging through the repo.",
        dedupe_key="existing task",
    )

    event = _make_event("different task")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == "我先继续处理手头这个，再接着帮你看 different task。"
    transcript_roles = [call.args[1]["role"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_roles == ["user", "assistant"]


@pytest.mark.asyncio
async def test_orchestrator_fallback_answers_progress_question_from_running_full_agent():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(side_effect=ValueError("bad json"))
    runner._run_agent = AsyncMock()

    session_entry = _make_session_entry()
    running_agent = MagicMock()
    running_agent.get_activity_summary.return_value = {
        "api_call_count": 3,
        "max_iterations": 12,
        "current_tool": "web_search",
        "last_activity_desc": "Checking live documentation",
    }
    runner._running_agents[session_entry.session_key] = running_agent
    runner._running_agents_ts[session_entry.session_key] = 0

    event = _make_event("现在在干嘛")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == (
        "Still working on the current request. Current step: iteration 3/12. "
        "Current tool: web_search. Latest progress: Checking live documentation."
    )
    assert runner._run_agent.await_count == 0


@pytest.mark.asyncio
async def test_orchestrator_fallback_handoffs_identity_question():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(side_effect=ValueError("bad json"))
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "我是 Hermes gateway 的主调度 agent。",
            "messages": [],
            "failed": False,
            "already_sent": False,
        }
    )

    session_entry = _make_session_entry()
    event = _make_event("你是谁")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    runner._run_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_fallback_routes_external_info_query_to_child_with_web_access():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(side_effect=ValueError("bad json"))
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "child result",
            "messages": [],
            "failed": False,
            "already_sent": False,
        }
    )

    session_entry = _make_session_entry()
    event = _make_event("天气如何")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    runner._run_agent.assert_awaited_once()
    assert runner._run_agent.await_args.kwargs["enabled_toolsets_override"] == ["terminal", "file_tools"]


@pytest.mark.asyncio
async def test_orchestrator_fallback_child_completion_closes_observability_trace(tmp_path):
    shutdown_publisher()
    init_publisher(enabled=True, queue_size=100)
    store = ObservabilityStore(db_path=tmp_path / "test_obs.db")
    client = IngestClient(store)

    try:
        runner = _make_runner()
        _runtime(runner).run_orchestrator_agent = AsyncMock(side_effect=ValueError("bad json"))
        runner._run_agent = AsyncMock(
            return_value={
                "final_response": "repo analysis finished",
                "messages": [],
                "failed": False,
                "already_sent": False,
            }
        )

        session_entry = _make_session_entry()
        event = _make_event("please analyze this repo deeply")
        trace_ctx = NapcatTraceContext(
            trace_id="trace-fallback-child-complete",
            chat_type="private",
            chat_id=event.source.chat_id,
            user_id=event.source.user_id,
            message_id=event.message_id,
            session_key=session_entry.session_key,
            session_id=session_entry.session_id,
        )
        event.napcat_trace_ctx = trace_ctx

        emit_napcat_event(
            EventType.MESSAGE_RECEIVED,
            {"text_preview": event.text},
            trace_ctx=trace_ctx,
        )

        response = await _extension(runner).run_orchestrator_turn(
            event=event,
            source=event.source,
            session_entry=session_entry,
            session_key=session_entry.session_key,
            history=[],
            context_prompt="ctx",
            platform_turn=AdapterTurnPlan(),
            dynamic_disabled_skills=[],
            dynamic_disabled_toolsets=[],
            message_text=event.text,
        )

        assert response is None

        child_tasks = list(runner._background_tasks)
        assert len(child_tasks) == 1
        await asyncio.gather(*child_tasks)

        client._flush()

        trace = ObservabilityRepository(store).get_trace(trace_ctx.trace_id)
        assert trace is not None
        assert trace["status"] == "completed"
        assert trace["summary"]["headline"] == "repo analysis finished"
        assert trace["summary"]["child_counts"]["completed"] == 1
        assert trace["summary"]["last_terminal_event"] == EventType.ORCHESTRATOR_CHILD_COMPLETED.value

        event_types = [item["event_type"] for item in trace["events"]]
        assert EventType.ORCHESTRATOR_TURN_COMPLETED.value not in event_types
        assert EventType.ORCHESTRATOR_CHILD_COMPLETED.value in event_types
        assert EventType.ORCHESTRATOR_CHILD_SPAWNED.value in event_types
    finally:
        store.close()
        shutdown_publisher()


def test_emit_orchestrator_child_terminal_event_includes_runtime_session_ids():
    import gateway.run as gateway_run

    shutdown_publisher()
    init_publisher(enabled=True, queue_size=100)

    try:
        runner = _make_runner()
        child_state = GatewayChildState(
            child_id="child-1",
            session_key="agent:main:napcat:dm:chat-1",
            goal="analyze repo",
            originating_user_message="analyze repo",
            status="completed",
            request_kind="analysis",
            summary="repo analysis finished",
            runtime_session_id="runtime-child-1",
            runtime_session_key="agent:main:napcat:dm:chat-1::child-1",
            trace_ctx=_make_trace_ctx(
                trace_id="trace-child",
                span_id="span-child",
                agent_scope="orchestrator_child",
                child_id="child-1",
            ),
        )

        _runtime(runner).emit_orchestrator_child_terminal_event(
            child_state=child_state,
                event_type=gateway_run._GatewayEventType.ORCHESTRATOR_CHILD_COMPLETED,
            payload={"result_preview": "repo analysis finished"},
        )

        events = drain_queue()
        assert len(events) == 1
        payload = events[0].payload
        assert payload["child_id"] == "child-1"
        assert payload["runtime_session_id"] == "runtime-child-1"
        assert payload["runtime_session_key"] == "agent:main:napcat:dm:chat-1::child-1"
        assert payload["result_preview"] == "repo analysis finished"
    finally:
        shutdown_publisher()


@pytest.mark.asyncio
async def test_orchestrator_post_parse_keeps_llm_reply_for_external_info_query():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 1, "respond_now": true, "immediate_reply": "晴天", '
                '"spawn_children": [], "cancel_children": [], '
                '"ignore_message": false, "reasoning_summary": "simple"}'
            )
        }
    )
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "child result",
            "messages": [],
            "failed": False,
            "already_sent": False,
        }
    )

    session_entry = _make_session_entry()
    event = _make_event("天气如何")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == "晴天"
    runner._run_agent.assert_not_awaited()
    session = runner._get_gateway_extension().get_orchestrator_session(
        session_entry.session_key
    )
    assert session.last_decision.respond_now is True
    assert session.last_decision.immediate_reply == "晴天"
    assert not session.last_decision.spawn_children
    transcript_replies = [call.args[1]["content"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert "晴天" in transcript_replies
    assert "child result" not in transcript_replies
    assert transcript_replies.count("天气如何") == 1
    assert transcript_replies.count("晴天") == 1


@pytest.mark.asyncio
async def test_orchestrator_complexity_score_no_longer_auto_spawns_without_spawn_action():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 42, "respond_now": true, "immediate_reply": "handoff", '
                '"spawn_children": [], "cancel_children": [], '
                '"ignore_message": false, "reasoning_summary": "needs handoff"}'
            )
        }
    )
    runner._run_agent = AsyncMock()

    session_entry = _make_session_entry()
    event = _make_event("please fix this repo")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=attach_napcat_promotion_plan(
            AdapterTurnPlan(),
            {
                "promotion_enabled": True,
                "promotion_stage": "l2",
                "turn_model_override": {"model": "router-l2"},
            },
        ),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == "handoff"
    runner._run_agent.assert_not_awaited()
    session = runner._get_gateway_extension().get_orchestrator_session(
        session_entry.session_key
    )
    assert session.last_decision.complexity_score == 42
    assert session.last_decision.respond_now is True
    transcript_replies = [call.args[1]["content"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_replies.count("please fix this repo") == 1
    assert transcript_replies.count("handoff") == 1


@pytest.mark.asyncio
async def test_orchestrator_fallback_reuses_similar_active_child():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(side_effect=ValueError("bad json"))
    runner._run_agent = AsyncMock()

    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    child = GatewayChildState(
        child_id="child-1",
        session_key=session_entry.session_key,
        goal="fully implement orchestration plan",
        originating_user_message="fully implement orchestration plan",
        status="running",
        started_at="2026-04-17T10:00:00Z",
        current_step="iteration 1",
        current_tool="terminal",
        last_progress_message="Reading the repository plan and affected modules.",
        dedupe_key="fully implement orchestration plan",
    )
    session.active_children[child.child_id] = child

    event = _make_event("please fully implement the orchestration plan")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert "Still working on:" in response
    assert "Reading the repository plan" in response
    assert runner._run_agent.await_count == 0
    transcript_roles = [call.args[1]["role"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_roles == ["user"]


@pytest.mark.asyncio
async def test_orchestrator_active_child_status_checkin_keeps_llm_spawn_decision():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"action":"spawn","payload":{"goal":"你在吗","handoff_reply":"我先继续处理地震查询，处理完接着看这个。"}}'
            )
        }
    )
    runner._run_agent = AsyncMock()
    _runtime(runner).spawn_orchestrator_children = AsyncMock(
        return_value={"started": 1, "queued": 0, "reused": 0, "skipped": 0, "started_child_ids": []}
    )

    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    child = GatewayChildState(
        child_id="child-1",
        session_key=session_entry.session_key,
        goal="query latest earthquake",
        originating_user_message="查一下最近的地震是在什么时候",
        status="running",
        current_step="checking live data",
        current_tool="terminal",
        last_progress_message="Checking live USGS data.",
        dedupe_key="query latest earthquake",
    )
    session.active_children[child.child_id] = child

    event = _make_event("你在吗")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == "我先继续处理地震查询，处理完接着看这个。"
    _runtime(runner).spawn_orchestrator_children.assert_awaited_once()
    assert runner._run_agent.await_count == 0
    transcript_roles = [call.args[1]["role"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_roles == ["user", "assistant"]


@pytest.mark.asyncio
async def test_orchestrator_fallback_ignores_low_value_group_message():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(side_effect=ValueError("bad json"))
    runner._run_agent = AsyncMock()

    session_entry = _make_session_entry()
    event = MessageEvent(
        text="收到",
        source=_make_source(chat_type="group"),
        message_type=MessageType.TEXT,
        message_id="msg-1",
    )
    event.metadata = {"napcat_trigger_reason": "mention"}
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    transcript_roles = [call.args[1]["role"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_roles == ["user"]
    assert runner._run_agent.await_count == 0


@pytest.mark.asyncio
async def test_orchestrator_fallback_allows_isolated_group_banter_reply():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(side_effect=ValueError("bad json"))
    runner._run_agent = AsyncMock()

    session_entry = _make_session_entry(chat_type="group")
    event = _make_event("这也行", chat_type="group")
    event.metadata = {"napcat_trigger_reason": "group_message"}
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == "好家伙，这也行。"
    assert runner._run_agent.await_count == 0


@pytest.mark.asyncio
async def test_orchestrator_fallback_routes_user_correction_to_child():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(side_effect=ValueError("bad json"))
    _runtime(runner).spawn_orchestrator_children = AsyncMock(
        return_value={"started": 1, "queued": 0, "reused": 0, "skipped": 0, "started_child_ids": []}
    )
    runner._run_agent = AsyncMock()

    session_entry = _make_session_entry()
    event = _make_event("纠正一下，不是杭州，是苏州")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    _runtime(runner).spawn_orchestrator_children.assert_awaited_once()
    request = _runtime(runner).spawn_orchestrator_children.await_args.kwargs["requests"][0]
    assert request.goal == event.text
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    assert session.last_decision.used_fallback is True
    assert session.last_decision.respond_now is False
    assert session.last_decision.spawn_children
    transcript_roles = [call.args[1]["role"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_roles == ["user"]


@pytest.mark.asyncio
async def test_orchestrator_fallback_routes_device_state_query_to_child():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(side_effect=ValueError("bad json"))
    _runtime(runner).spawn_orchestrator_children = AsyncMock(
        return_value={"started": 1, "queued": 0, "reused": 0, "skipped": 0, "started_child_ids": []}
    )
    runner._run_agent = AsyncMock()

    session_entry = _make_session_entry()
    event = _make_event("请你帮我看一下我家的设备状态")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    _runtime(runner).spawn_orchestrator_children.assert_awaited_once()
    request = _runtime(runner).spawn_orchestrator_children.await_args.kwargs["requests"][0]
    assert request.goal == event.text
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    assert session.last_decision.used_fallback is True
    assert session.last_decision.respond_now is False
    assert session.last_decision.spawn_children


@pytest.mark.asyncio
async def test_orchestrator_group_message_keeps_llm_spawn_decision():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"action":"spawn","payload":{"goal":"Analyze the current workspace repository concurrency model"}}'
            )
        }
    )
    _runtime(runner).spawn_orchestrator_children = AsyncMock(
        return_value={"started": 1, "reused": 0, "skipped": 0, "started_child_ids": []}
    )

    session_entry = _make_session_entry(chat_type="group")
    event = _make_event("这个仓库的并发模型是不是有问题", chat_type="group")
    event.metadata = {"napcat_trigger_reason": "group_message"}
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    _runtime(runner).spawn_orchestrator_children.assert_awaited_once()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    assert session.last_decision.ignore_message is False
    assert len(session.last_decision.spawn_children) == 1
    assert session.last_decision.spawn_children[0].goal == (
        "Analyze the current workspace repository concurrency model"
    )


@pytest.mark.asyncio
async def test_orchestrator_group_message_followup_to_recent_agent_reply_can_spawn():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"action":"spawn","payload":{"goal":"Explain the previous locking analysis in more detail"}}'
            )
        }
    )
    _runtime(runner).spawn_orchestrator_children = AsyncMock(
        return_value={"started": 1, "reused": 0, "skipped": 0, "started_child_ids": []}
    )

    session_entry = _make_session_entry(chat_type="group")
    event = _make_event("继续展开说一下", chat_type="group")
    event.metadata = {"napcat_trigger_reason": "group_message"}
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[{"role": "assistant", "content": "我刚说了问题在锁竞争。"}],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    _runtime(runner).spawn_orchestrator_children.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_full_agent_followup_keeps_llm_reply():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"action":"reply","payload":{"text":"除湿机的细节信息来咯：运行中。"},"complexity_score":1}'
            )
        }
    )
    _runtime(runner).spawn_orchestrator_children = AsyncMock(
        return_value={"started": 1, "queued": 0, "reused": 0, "skipped": 0}
    )

    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    session.append_routing_history_entry(role="user", content="看看五马这个家庭里面的所有设备的状态")
    session.append_routing_history_entry(
        role="assistant",
        content=_support(runner).canonicalize_history_decision(
            parse_orchestrator_decision(
                '{"action":"spawn","payload":{"goal":"Check the status of all devices in the Wuma family"},"complexity_score":20}'
            ),
            history_source="orchestrator",
        ),
    )
    session.append_routing_history_entry(
        role="assistant",
        content=_support(runner).canonicalize_reply_history_json(
            "五马家庭设备状态汇总：除湿机运行中，书房空调关闭。",
            complexity_score=20,
            history_source="full_agent",
            reply_kind="final",
            child_id="child-1",
        ),
    )

    event = _make_event("看看除湿机细节信息")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == "除湿机的细节信息来咯：运行中。"
    _runtime(runner).spawn_orchestrator_children.assert_not_awaited()
    assert session.last_decision is not None
    assert session.last_decision.respond_now is True
    assert not session.last_decision.spawn_children


@pytest.mark.asyncio
async def test_orchestrator_device_state_query_keeps_llm_reply():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"action":"reply","payload":{"text":"好哒，你家设备状态如下：台灯已关闭。"},"complexity_score":2}'
            )
        }
    )
    _runtime(runner).spawn_orchestrator_children = AsyncMock(
        return_value={"started": 1, "queued": 0, "reused": 0, "skipped": 0}
    )

    session_entry = _make_session_entry()
    event = _make_event("请你帮我看一下我家的设备状态")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == "好哒，你家设备状态如下：台灯已关闭。"
    _runtime(runner).spawn_orchestrator_children.assert_not_awaited()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    assert session.last_decision is not None
    assert session.last_decision.respond_now is True
    assert not session.last_decision.spawn_children


@pytest.mark.asyncio
async def test_orchestrator_group_message_keeps_llm_spawn_instead_of_banter_override():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"action":"spawn","payload":{"goal":"Explain why this is funny"}}'
            )
        }
    )
    _runtime(runner).spawn_orchestrator_children = AsyncMock(
        return_value={"started": 1, "queued": 0, "reused": 0, "skipped": 0, "started_child_ids": []}
    )

    session_entry = _make_session_entry(chat_type="group")
    event = _make_event("这也行", chat_type="group")
    event.metadata = {"napcat_trigger_reason": "group_message"}
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    _runtime(runner).spawn_orchestrator_children.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_active_child_status_query_keeps_llm_reply():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"action":"reply","payload":{"text":"先别看状态，直接把下一个任务发我。"}}'
            )
        }
    )

    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    session.active_children["child-1"] = GatewayChildState(
        child_id="child-1",
        session_key=session_entry.session_key,
        goal="analyze repo",
        originating_user_message="analyze repo",
        status="running",
    )

    event = _make_event("跑到哪了")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == "先别看状态，直接把下一个任务发我。"


@pytest.mark.asyncio
async def test_child_progress_updates_state_without_transcript_noise():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 88, "respond_now": false, "immediate_reply": "working...", '
                '"spawn_children": [{"goal": "analyze repo", "allowed_toolsets": ["terminal"], "may_reply_directly": true}], '
                '"cancel_children": [], "ignore_message": false, "reasoning_summary": "needs deeper work"}'
            )
        }
    )

    async def _fake_run_agent(**kwargs):
        callback = kwargs.get("orchestrator_progress_callback")
        assert callback is not None
        callback("tool", {"tool_name": "terminal", "preview": "rg orchestrator"})
        callback("step", {"iteration": 2, "tool_names": ["terminal"]})
        callback("status", {"status_type": "lifecycle", "message": "Inspecting gateway flow"})
        callback("interim_assistant", {"text": "I'll inspect the gateway orchestrator first."})
        await asyncio.sleep(0.02)
        return {
            "final_response": "child result",
            "messages": [],
            "failed": False,
            "already_sent": False,
        }

    runner._run_agent = AsyncMock(side_effect=_fake_run_agent)

    session_entry = _make_session_entry()
    event = _make_event("please analyze this repo deeply")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    await asyncio.sleep(0.01)
    session = runner._get_gateway_extension().get_orchestrator_session(
        session_entry.session_key
    )
    board = session.child_board()
    assert board["active_children"][0]["current_tool"] == "terminal"
    assert board["active_children"][0]["tools_seen"] == ["terminal"]
    assert board["active_children"][0]["current_activity"] == "Inspecting gateway flow"
    assert "last_progress_message" not in board["active_children"][0]

    await asyncio.sleep(0.05)
    recent_child = session.recent_children[0]
    assert recent_child.tool_history[0]["tool"] == "terminal"
    assert recent_child.status == "completed"

    transcript_replies = [call.args[1]["content"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert "child result" in transcript_replies
    assert transcript_replies.count("please analyze this repo deeply") == 1
    assert transcript_replies.count("child result") == 1
    assert not any("working..." in reply for reply in transcript_replies)
    assert not any("Started background task:" in reply for reply in transcript_replies)
    assert not any("Inspecting gateway flow" in reply for reply in transcript_replies)
    assert not any("gateway orchestrator first" in reply for reply in transcript_replies)


@pytest.mark.asyncio
async def test_orchestrator_child_uses_isolated_runtime_session_key():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 88, "respond_now": false, "immediate_reply": "", '
                '"spawn_children": [{"goal": "analyze repo", "may_reply_directly": true}], '
                '"cancel_children": [], "ignore_message": false, "reasoning_summary": "needs deeper work"}'
            )
        }
    )

    release_child = asyncio.Event()

    async def _fake_run_agent(**kwargs):
        await release_child.wait()
        return {
            "final_response": "child result",
            "messages": [],
            "failed": False,
            "already_sent": False,
        }

    runner._run_agent = AsyncMock(side_effect=_fake_run_agent)

    session_entry = _make_session_entry()
    event = _make_event("please analyze this repo deeply")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    await asyncio.sleep(0)

    session = runner._get_gateway_extension().get_orchestrator_session(
        session_entry.session_key
    )
    assert len(session.active_children) == 1
    child_id = next(iter(session.active_children))
    child_state = session.active_children[child_id]
    kwargs = runner._run_agent.await_args.kwargs
    assert kwargs["session_key"] == session_entry.session_key
    assert kwargs["session_id"] != session_entry.session_id
    assert kwargs["parent_session_id"] == session_entry.session_id
    assert kwargs["session_id"] == child_state.runtime_session_id
    assert kwargs["session_id"] == f"{session_entry.session_id}__orchestrator_fullagent"
    assert kwargs["session_runtime_key"] != session_entry.session_key
    assert kwargs["session_runtime_key"] == child_state.runtime_session_key
    assert kwargs["session_runtime_key"] == f"{session_entry.session_key}::fullagent"
    assert session.fullagent_runtime_session_id == child_state.runtime_session_id
    assert session.fullagent_runtime_session_key == child_state.runtime_session_key

    release_child.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_orchestrator_child_appends_final_reply_to_main_transcript_even_when_child_has_visible_assistant_message():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 88, "respond_now": false, "immediate_reply": "", '
                '"spawn_children": [{"goal": "analyze repo", "may_reply_directly": true}], '
                '"cancel_children": [], "ignore_message": false, "reasoning_summary": "needs deeper work"}'
            )
        }
    )

    async def _fake_run_agent(**kwargs):
        return {
            "final_response": "child result",
            "messages": [
                {"role": "assistant", "content": "child result"},
            ],
            "history_offset": 0,
            "failed": False,
            "already_sent": True,
        }

    runner._run_agent = AsyncMock(side_effect=_fake_run_agent)

    session_entry = _make_session_entry()
    event = _make_event("please analyze this repo deeply")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    transcript_replies = [call.args[1]["content"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_replies.count("please analyze this repo deeply") == 1
    assert transcript_replies.count("child result") == 1


@pytest.mark.asyncio
async def test_orchestrator_child_syncs_visible_assistant_messages_to_transcript_and_routing_history():
    runner = _make_runner()
    runner.session_store.load_transcript.return_value = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 88, "respond_now": false, "immediate_reply": "", '
                '"spawn_children": [{"goal": "analyze repo", "may_reply_directly": true}], '
                '"cancel_children": [], "ignore_message": false, "reasoning_summary": "needs deeper work"}'
            )
        }
    )

    async def _fake_run_agent(**kwargs):
        return {
            "final_response": "final child result",
            "messages": [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "assistant", "content": "first visible update"},
                {"role": "assistant", "content": "final child result"},
            ],
            "history_offset": 2,
            "failed": False,
            "already_sent": False,
        }

    runner._run_agent = AsyncMock(side_effect=_fake_run_agent)

    session_entry = _make_session_entry()
    event = _make_event("please analyze this repo deeply")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    transcript_replies = [call.args[1]["content"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_replies == [
        "please analyze this repo deeply",
        "first visible update",
        "final child result",
    ]

    session = runner._get_gateway_extension().get_orchestrator_session(
        session_entry.session_key
    )
    first_payload = json.loads(session.routing_history[-2]["content"])
    final_payload = json.loads(session.routing_history[-1]["content"])
    assert session.routing_history[-2]["role"] == "assistant"
    assert session.routing_history[-1]["role"] == "assistant"
    assert first_payload["payload"]["text"] == "first visible update"
    assert first_payload["complexity_score"] == 88
    assert first_payload["history_source"] == "full_agent"
    assert first_payload["reply_kind"] == "interim"
    assert first_payload["child_id"]
    assert final_payload["payload"]["text"] == "final child result"
    assert final_payload["complexity_score"] == 88
    assert final_payload["history_source"] == "full_agent"
    assert final_payload["reply_kind"] == "final"
    assert final_payload["child_id"] == first_payload["child_id"]


@pytest.mark.asyncio
async def test_orchestrator_child_sanitizes_promotion_markers_before_transcript_sync():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 88, "respond_now": false, "immediate_reply": "", '
                '"spawn_children": [{"goal": "analyze repo", "may_reply_directly": true}], '
                '"cancel_children": [], "ignore_message": false, "reasoning_summary": "needs deeper work"}'
            )
        }
    )

    async def _fake_run_agent(**kwargs):
        return {
            "final_response": "[[COMPLEXITY:80]] final answer [[ESCALATE_L2]]",
            "messages": [
                {"role": "assistant", "content": "[[NO_REPLY]]"},
                {"role": "assistant", "content": "Visible answer [[ESCALATE_L2]]"},
            ],
            "history_offset": 0,
            "failed": False,
            "already_sent": False,
        }

    runner._run_agent = AsyncMock(side_effect=_fake_run_agent)

    session_entry = _make_session_entry()
    event = _make_event("please analyze this repo deeply")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=attach_napcat_promotion_plan(
            AdapterTurnPlan(),
            {
                "promotion_enabled": True,
                "promotion_stage": "l2",
                "turn_model_override": {"model": "router-picked"},
                "promotion_config_snapshot": {
                    "protocol": {
                        "no_reply_marker": "[[NO_REPLY]]",
                        "escalate_marker": "[[ESCALATE_L2]]",
                    }
                },
            },
        ),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    transcript_replies = [call.args[1]["content"] for call in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_replies == [
        "please analyze this repo deeply",
        "Visible answer",
        "final answer",
    ]
    assert not any("[[" in reply for reply in transcript_replies)

    session = runner._get_gateway_extension().get_orchestrator_session(
        session_entry.session_key
    )
    assert session.routing_history[-2]["content"] == _support(runner).canonicalize_reply_history_json(
        "Visible answer",
        complexity_score=88,
        history_source="full_agent",
        reply_kind="interim",
        child_id=json.loads(session.routing_history[-2]["content"])["child_id"],
    )
    assert session.routing_history[-1]["content"] == _support(runner).canonicalize_reply_history_json(
        "final answer",
        complexity_score=88,
        history_source="full_agent",
        reply_kind="final",
        child_id=json.loads(session.routing_history[-1]["content"])["child_id"],
    )


@pytest.mark.asyncio
async def test_orchestrator_spawn_records_user_turn_before_child_finishes():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 88, "respond_now": false, "immediate_reply": "", '
                '"spawn_children": [{"goal": "analyze repo", "may_reply_directly": true}], '
                '"cancel_children": [], "ignore_message": false, "reasoning_summary": "needs deeper work"}'
            )
        }
    )
    release_child = asyncio.Event()

    async def _fake_run_agent(**kwargs):
        await release_child.wait()
        return {
            "final_response": "child result",
            "messages": [],
            "failed": False,
            "already_sent": False,
        }

    runner._run_agent = AsyncMock(side_effect=_fake_run_agent)

    session_entry = _make_session_entry()
    event = _make_event("please analyze this repo deeply")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    await asyncio.sleep(0)

    transcript_entries = [call.args[1] for call in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_entries[0]["role"] == "user"
    assert transcript_entries[0]["content"] == "please analyze this repo deeply"
    assert not any(entry.get("role") == "assistant" for entry in transcript_entries)

    release_child.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_orchestrator_reused_child_returns_existing_status_when_no_new_child_starts():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 88, "respond_now": false, "immediate_reply": "", '
                '"spawn_children": [{"goal": "analyze repo", "may_reply_directly": true}], '
                '"cancel_children": [], "ignore_message": false, "reasoning_summary": "needs deeper work"}'
            )
        }
    )
    runner._run_agent = AsyncMock()

    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    session.active_children["child-1"] = GatewayChildState(
        child_id="child-1",
        session_key=session_entry.session_key,
        goal="analyze repo",
        originating_user_message="analyze repo",
        status="running",
        current_step="iteration 2",
        current_tool="terminal",
        last_progress_message="Tracing the gateway flow.",
        dedupe_key="analyze repo",
    )

    event = _make_event("analyze repo")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert "Still working on: analyze repo." in response
    assert "Current tool: terminal." in response
    runner._run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_orchestrator_concurrency_queue_returns_handoff_reply_and_drains():
    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 88, "respond_now": false, "immediate_reply": "", '
                '"spawn_children": [{"goal": "different task", "may_reply_directly": true}], '
                '"spawn_handoff_reply": "等我先把 existing task 处理完，接着就看 different task。", '
                '"cancel_children": [], "ignore_message": false, "reasoning_summary": "needs deeper work"}'
            )
        }
    )
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "queued child result",
            "messages": [],
            "failed": False,
            "already_sent": False,
        }
    )

    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    runtime_session_id, runtime_session_key = session.bind_fullagent_runtime(
        runtime_session_id=_extension(runner).orchestrator_fullagent_session_id(
            session_entry.session_id
        ),
        runtime_session_key=_extension(runner).orchestrator_fullagent_runtime_session_key(
            session_entry.session_key
        ),
    )
    session.active_children["child-1"] = GatewayChildState(
        child_id="child-1",
        session_key=session_entry.session_key,
        goal="existing task",
        originating_user_message="existing task",
        runtime_session_id=runtime_session_id,
        runtime_session_key=runtime_session_key,
        status="running",
        current_step="iteration 3",
        current_tool="terminal",
        last_progress_message="Still digging through the repo.",
        dedupe_key="existing task",
    )

    event = _make_event("different task")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == "等我先把 existing task 处理完，接着就看 different task。"
    runner._run_agent.assert_not_awaited()
    queued_children = [child for child in session.active_children.values() if child.child_id != "child-1"]
    assert len(queued_children) == 1
    assert queued_children[0].status == "queued"
    assert queued_children[0].goal == "different task"
    assert queued_children[0].runtime_session_id == runtime_session_id
    assert queued_children[0].runtime_session_key == runtime_session_key

    existing_child = session.active_children["child-1"]
    existing_child.status = "completed"
    existing_child.summary = "existing task done"
    _runtime(runner).finalize_orchestrator_child(
        session=session,
        child_state=existing_child,
    )

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    runner._run_agent.assert_awaited_once()
    recent_goals = [child.goal for child in session.recent_children]
    assert "different task" in recent_goals
    queued_recent = next(child for child in session.recent_children if child.goal == "different task")
    assert queued_recent.status == "completed"
    assert queued_recent.user_turn_recorded is True


@pytest.mark.asyncio
async def test_orchestrator_started_spawn_with_existing_child_returns_handoff_reply():
    runner = _make_runner()
    _set_effective_orchestrator_config(runner, child_max_concurrency=2)
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 88, "respond_now": false, "immediate_reply": "", '
                '"spawn_children": [{"goal": "different task", "may_reply_directly": true}], '
                '"spawn_handoff_reply": "我先收尾 existing task，完了马上接 different task。", '
                '"cancel_children": [], "ignore_message": false, "reasoning_summary": "needs deeper work"}'
            )
        }
    )
    release_child = asyncio.Event()

    async def _fake_run_agent(**kwargs):
        await release_child.wait()
        return {
            "final_response": "child result",
            "messages": [],
            "failed": False,
            "already_sent": False,
        }

    runner._run_agent = AsyncMock(side_effect=_fake_run_agent)

    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    session.active_children["child-1"] = GatewayChildState(
        child_id="child-1",
        session_key=session_entry.session_key,
        goal="existing task",
        originating_user_message="existing task",
        status="running",
        current_step="iteration 3",
        current_tool="terminal",
        last_progress_message="Still digging through the repo.",
        dedupe_key="existing task",
    )

    event = _make_event("different task")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == "我先收尾 existing task，完了马上接 different task。"
    transcript_entries = [call.args[1] for call in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_entries[0]["role"] == "user"
    assert transcript_entries[0]["content"] == "different task"
    assert transcript_entries[1]["role"] == "assistant"
    assert transcript_entries[1]["content"] == "我先收尾 existing task，完了马上接 different task。"

    release_child.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_orchestrator_started_spawn_with_existing_child_repairs_missing_handoff_reply():
    runner = _make_runner()
    _set_effective_orchestrator_config(runner, child_max_concurrency=2)
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        side_effect=[
            {
                "final_response": (
                    '{"complexity_score": 88, "respond_now": false, "immediate_reply": "", '
                    '"spawn_children": [{"goal": "different task", "may_reply_directly": true}], '
                    '"cancel_children": [], "ignore_message": false, "reasoning_summary": "needs deeper work"}'
                )
            },
            {
                "final_response": (
                    '{"action":"spawn","complexity_score":88,'
                    '"payload":{"goal":"different task","handoff_reply":"我先继续处理 existing task，处理完马上接着看 different task。"}}'
                )
            },
        ]
    )
    release_child = asyncio.Event()

    async def _fake_run_agent(**kwargs):
        await release_child.wait()
        return {
            "final_response": "child result",
            "messages": [],
            "failed": False,
            "already_sent": False,
        }

    runner._run_agent = AsyncMock(side_effect=_fake_run_agent)

    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    session.active_children["child-1"] = GatewayChildState(
        child_id="child-1",
        session_key=session_entry.session_key,
        goal="existing task",
        originating_user_message="existing task",
        status="running",
        current_step="iteration 3",
        current_tool="terminal",
        last_progress_message="Still digging through the repo.",
        dedupe_key="existing task",
    )

    event = _make_event("different task")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == "我先继续处理 existing task，处理完马上接着看 different task。"
    assert _runtime(runner).run_orchestrator_agent.await_count == 2
    repair_call = _runtime(runner).run_orchestrator_agent.await_args_list[1]
    assert "Contract repair for this same turn:" in repair_call.kwargs["turn_system_context"]
    assert 'payload.handoff_reply is required' in repair_call.kwargs["turn_system_context"]

    transcript_entries = [call.args[1] for call in runner.session_store.append_to_transcript.call_args_list]
    assert transcript_entries[0]["role"] == "user"
    assert transcript_entries[0]["content"] == "different task"
    assert transcript_entries[1]["role"] == "assistant"
    assert transcript_entries[1]["content"] == "我先继续处理 existing task，处理完马上接着看 different task。"

    release_child.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_busy_handler_bypasses_adapter_guard_when_only_child_is_running():
    runner = _make_runner()
    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    session.active_children["child-1"] = GatewayChildState(
        child_id="child-1",
        session_key=session_entry.session_key,
        goal="analyze repo",
        originating_user_message="analyze repo",
        status="running",
    )

    runner._handle_message = AsyncMock(return_value="child-status reply")
    event = _make_event("跑到哪了")

    handled = await runner._handle_active_session_busy_message(
        event,
        session_entry.session_key,
    )

    assert handled is True
    runner._handle_message.assert_awaited_once_with(event)
    runner.adapters[Platform.NAPCAT].send.assert_not_awaited()
    runner.adapters[Platform.NAPCAT]._send_with_retry.assert_awaited_once()
    assert runner.adapters[Platform.NAPCAT]._send_with_retry.await_args.kwargs["content"] == "child-status reply"


@pytest.mark.asyncio
async def test_failed_child_emits_failed_event_not_completed(monkeypatch):
    import gateway.run as gateway_run

    emitted: list[str] = []

    def _fake_emit(event_type, payload, trace_ctx=None, severity=None):
        del payload, trace_ctx, severity
        emitted.append(event_type.value if hasattr(event_type, "value") else str(event_type))

    monkeypatch.setattr(gateway_run, "_emit_gateway_event", _fake_emit)

    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 88, "respond_now": false, "immediate_reply": "working...", '
                '"spawn_children": [{"goal": "analyze repo", "allowed_toolsets": ["terminal"], "may_reply_directly": true}], '
                '"cancel_children": [], "ignore_message": false, "reasoning_summary": "needs deeper work"}'
            )
        }
    )
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "task failed hard",
            "messages": [],
            "failed": True,
            "already_sent": False,
        }
    )

    session_entry = _make_session_entry()
    event = _make_event("please analyze this repo deeply")
    event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-1")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response is None
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert gateway_run._GatewayEventType.ORCHESTRATOR_CHILD_FAILED.value in emitted
    assert gateway_run._GatewayEventType.ORCHESTRATOR_CHILD_COMPLETED.value not in emitted


@pytest.mark.asyncio
async def test_orchestrator_main_reply_emits_completed_event(monkeypatch):
    import gateway.run as gateway_run

    emitted: list[str] = []

    def _fake_emit(event_type, payload, trace_ctx=None, severity=None):
        del payload, trace_ctx, severity
        emitted.append(event_type.value if hasattr(event_type, "value") else str(event_type))

    monkeypatch.setattr(gateway_run, "_emit_gateway_event", _fake_emit)

    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(
        return_value={
            "final_response": (
                '{"complexity_score": 1, "respond_now": true, "immediate_reply": "quick answer", '
                '"spawn_children": [], "cancel_children": [], '
                '"ignore_message": false, "reasoning_summary": "simple"}'
            )
        }
    )

    session_entry = _make_session_entry()
    event = _make_event("ping")
    event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-1")

    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert response == "quick answer"
    assert gateway_run._GatewayEventType.ORCHESTRATOR_TURN_COMPLETED.value in emitted


@pytest.mark.asyncio
async def test_orchestrator_status_reply_emits_status_event_but_not_completed(monkeypatch):
    import gateway.run as gateway_run

    emitted: list[str] = []

    def _fake_emit(event_type, payload, trace_ctx=None, severity=None):
        del payload, trace_ctx, severity
        emitted.append(event_type.value if hasattr(event_type, "value") else str(event_type))

    monkeypatch.setattr(gateway_run, "_emit_gateway_event", _fake_emit)

    runner = _make_runner()
    _runtime(runner).run_orchestrator_agent = AsyncMock(side_effect=ValueError("bad json"))

    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    session.active_children["child-1"] = GatewayChildState(
        child_id="child-1",
        session_key=session_entry.session_key,
        goal="analyze repo",
        originating_user_message="analyze repo",
        status="running",
        current_step="iteration 2",
        current_tool="terminal",
        last_progress_message="Tracing the gateway flow.",
        dedupe_key="analyze repo",
    )

    event = _make_event("跑到哪了")
    event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-1")
    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text=event.text,
    )

    assert "Still working on:" in response
    assert gateway_run._GatewayEventType.ORCHESTRATOR_STATUS_REPLY.value in emitted
    assert gateway_run._GatewayEventType.ORCHESTRATOR_TURN_COMPLETED.value not in emitted


@pytest.mark.asyncio
async def test_cancel_orchestrator_children_emits_cancel_event(monkeypatch):
    import gateway.run as gateway_run

    emitted: list[str] = []

    def _fake_emit(event_type, payload, trace_ctx=None, severity=None):
        del payload, trace_ctx, severity
        emitted.append(event_type.value if hasattr(event_type, "value") else str(event_type))

    monkeypatch.setattr(gateway_run, "_emit_gateway_event", _fake_emit)

    runner = _make_runner()
    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    child = GatewayChildState(
        child_id="child-1",
        session_key=session_entry.session_key,
        goal="analyze repo",
        originating_user_message="analyze repo",
        status="running",
        trace_ctx=SimpleNamespace(trace_id="trace-1"),
    )
    child.task = asyncio.create_task(asyncio.sleep(10))
    session.active_children[child.child_id] = child

    try:
        await _extension(runner).cancel_orchestrator_children(session_entry.session_key, reason="stop requested")
    finally:
        if child.task is not None:
            child.task.cancel()

    assert gateway_run._GatewayEventType.ORCHESTRATOR_CHILD_CANCELLED.value in emitted


@pytest.mark.asyncio
async def test_cancel_orchestrator_children_interrupts_runtime_control():
    runner = _make_runner()
    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    interrupt = MagicMock()
    child = GatewayChildState(
        child_id="child-1",
        session_key=session_entry.session_key,
        goal="analyze repo",
        originating_user_message="analyze repo",
        status="running",
        runtime_control=SimpleNamespace(
            cancel_requested=False,
            finished=False,
            interrupt=interrupt,
        ),
    )
    child.task = asyncio.create_task(asyncio.sleep(10))
    session.active_children[child.child_id] = child

    try:
        await _extension(runner).cancel_orchestrator_children(session_entry.session_key, reason="stop requested")
    finally:
        if child.task is not None:
            child.task.cancel()

    interrupt.assert_called_once_with("stop requested")
    assert child.status == "cancelled"
    assert session.active_child_count() == 0


@pytest.mark.asyncio
async def test_cancel_orchestrator_children_removes_queued_child_without_running_it():
    runner = _make_runner()
    runner._run_agent = AsyncMock()
    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    queued_child = GatewayChildState(
        child_id="child-queued",
        session_key=session_entry.session_key,
        goal="different task",
        originating_user_message="different task",
        status="queued",
    )
    session.active_children[queued_child.child_id] = queued_child

    await _extension(runner).cancel_orchestrator_children(
        session_entry.session_key,
        child_ids=[queued_child.child_id],
        reason="stop requested",
    )

    runner._run_agent.assert_not_awaited()
    assert session.active_child_count() == 0
    assert session.recent_children[0].child_id == queued_child.child_id
    assert session.recent_children[0].status == "cancelled"


@pytest.mark.asyncio
async def test_orchestrator_decision_event_includes_raw_response(monkeypatch):
    import gateway.run as gateway_run

    emitted: list[tuple[str, dict]] = []

    def _fake_emit(event_type, payload, trace_ctx=None, severity=None):
        del trace_ctx, severity
        emitted.append((event_type.value if hasattr(event_type, "value") else str(event_type), dict(payload)))

    monkeypatch.setattr(gateway_run, "_emit_gateway_event", _fake_emit)

    runner = _make_runner()
    raw_response = (
        '{"respond_now": true, "immediate_reply": "quick answer", '
        '"spawn_children": [], "cancel_children": [], '
        '"ignore_message": false, "reasoning_summary": "simple"}'
    )
    _runtime(runner).run_orchestrator_agent = AsyncMock(return_value={"final_response": raw_response})
    runner._run_agent = AsyncMock()

    session_entry = _make_session_entry()
    event = _make_event("ping")
    event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-1")

    response = await _extension(runner).run_orchestrator_turn(
        event=event,
        source=event.source,
        session_entry=session_entry,
        session_key=session_entry.session_key,
        history=[],
        context_prompt="ctx",
        platform_turn=AdapterTurnPlan(),
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        message_text="ping",
    )

    assert response == "quick answer"
    decision_events = [
        payload for event_type, payload in emitted
        if event_type == gateway_run._GatewayEventType.ORCHESTRATOR_DECISION_PARSED.value
    ]
    assert decision_events
    assert decision_events[-1]["raw_response"] == raw_response


@pytest.mark.asyncio
async def test_stop_command_cancels_active_orchestrator_children():
    runner = _make_runner()
    session_entry = _make_session_entry()
    event = _make_event("/stop")
    runner.session_store.get_or_create_session = MagicMock(return_value=session_entry)
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    child = GatewayChildState(
        child_id="child-1",
        session_key=session_entry.session_key,
        goal="analyze repo",
        originating_user_message="analyze repo",
        status="running",
    )
    child.lifecycle_finalized = False
    child.cancel_requested = False
    child.task = asyncio.create_task(asyncio.sleep(10))
    session.active_children[child.child_id] = child

    try:
        response = await runner._handle_stop_command(event)
    finally:
        if child.task is not None:
            child.task.cancel()

    assert "Stopped background tasks" in response
    assert child.status == "cancelled"


@pytest.mark.asyncio
async def test_send_orchestrator_public_reply_uses_gateway_post_processing():
    runner = _make_runner()
    runner._deliver_media_from_response = AsyncMock()
    event = _make_event("hello")
    adapter = runner.adapters[Platform.NAPCAT]
    adapter.extract_media.return_value = [("/tmp/result.mp3", True)], "hello [[audio_as_voice]]"
    adapter.extract_images.return_value = [], "hello [[audio_as_voice]]"
    adapter.extract_local_files.return_value = [], "hello"

    delivered = await _runtime(runner).send_orchestrator_public_reply(
        event=event,
        content="hello\nMEDIA:/tmp/result.mp3",
        reply_to=event.message_id,
        agent_messages=[],
        already_sent=False,
    )

    assert delivered is True
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[1] == "hello"
    runner._deliver_media_from_response.assert_awaited_once()


def test_resolve_orchestrator_child_model_override_honors_policy_and_request_override():
    from gateway.orchestrator import OrchestratorChildRequest

    runner = _make_runner()
    _set_effective_orchestrator_config(runner, child_model_policy="none")

    decision = _support(runner).resolve_child_model_override(
        OrchestratorChildRequest(goal="analyze repo"),
        attach_napcat_promotion_plan(
            AdapterTurnPlan(),
            {"turn_model_override": {"model": "router-picked"}},
        ),
    )
    assert decision == {}

    request_override = _support(runner).resolve_child_model_override(
        OrchestratorChildRequest(
            goal="analyze repo",
            turn_model_override={"model": "request-picked"},
        ),
        attach_napcat_promotion_plan(
            AdapterTurnPlan(),
            {"turn_model_override": {"model": "router-picked"}},
        ),
    )
    assert request_override == {"model": "request-picked"}


def test_resolve_child_transcript_replies_strips_unusable_media_tags(tmp_path):
    runner = _make_runner()
    existing = tmp_path / "result.png"
    existing.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    content = (
        "来源：Pixiv\n"
        f"MEDIA:{existing}\n"
        "MEDIA:/absolute/path/to/image.jpg\n"
        "MEDIA:<image_path>"
    )

    replies = _runtime(runner).resolve_child_transcript_replies(
        result={
            "messages": [{"role": "assistant", "content": content}],
            "history_offset": 0,
        },
        platform_turn=AdapterTurnPlan(),
        history=[],
        terminal_content=content,
    )

    assert replies == [f"来源：Pixiv\nMEDIA:{existing}"]


def test_resolve_orchestrator_child_model_override_uses_promotion_safeguard_threshold():
    from gateway.orchestrator import OrchestratorChildRequest

    runner = _make_runner()
    high_threshold_turn = attach_napcat_promotion_plan(
        AdapterTurnPlan(),
        {
            "promotion_config_snapshot": {
                "l1": {"model": "l1-picked"},
                "l2": {"model": "l2-picked"},
                "safeguards": {"l2_complexity_score_threshold": 100},
            }
        },
    )
    low_threshold_turn = attach_napcat_promotion_plan(
        AdapterTurnPlan(),
        {
            "promotion_config_snapshot": {
                "l1": {"model": "l1-picked"},
                "l2": {"model": "l2-picked"},
                "safeguards": {"l2_complexity_score_threshold": 70},
            }
        },
    )

    low_complexity = _support(runner).resolve_child_model_override(
        OrchestratorChildRequest(goal="analyze repo", complexity_score=28),
        high_threshold_turn,
    )
    high_complexity = _support(runner).resolve_child_model_override(
        OrchestratorChildRequest(goal="analyze repo", complexity_score=80),
        low_threshold_turn,
    )

    assert low_complexity == {"model": "l1-picked"}
    assert high_complexity == {"model": "l2-picked"}


def test_orchestrator_child_runtime_identity_stays_stable_for_same_dedupe_key():
    from gateway.orchestrator import OrchestratorChildRequest

    runner = _make_runner()
    runtime = _runtime(runner)
    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    event = _make_event("please analyze this repo deeply")
    platform_turn = AdapterTurnPlan()

    request_a = OrchestratorChildRequest(goal="analyze repo", dedupe_key="analyze repo deeply")
    request_b = OrchestratorChildRequest(goal="analyze repo again", dedupe_key="analyze repo deeply")

    launch_a = runtime._make_child_launch_spec(
        event=event,
        session_entry=session_entry,
        source=event.source,
        request=request_a,
        context_prompt="ctx",
        channel_prompt=None,
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        event_message_id=event.message_id,
        platform_turn=platform_turn,
        trace_ctx=None,
        originating_user_message=event.text,
    )
    launch_b = runtime._make_child_launch_spec(
        event=event,
        session_entry=session_entry,
        source=event.source,
        request=request_b,
        context_prompt="ctx",
        channel_prompt=None,
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        event_message_id=event.message_id,
        platform_turn=platform_turn,
        trace_ctx=None,
        originating_user_message=event.text,
    )

    child_a = runtime._build_child_state(
        session=session,
        session_entry=session_entry,
        request=request_a,
        launch_spec=launch_a,
        trace_ctx=None,
    )
    child_b = runtime._build_child_state(
        session=session,
        session_entry=session_entry,
        request=request_b,
        launch_spec=launch_b,
        trace_ctx=None,
    )

    assert child_a.child_id != child_b.child_id
    assert child_a.runtime_session_id == child_b.runtime_session_id
    assert child_a.runtime_session_key == child_b.runtime_session_key
    assert child_a.runtime_session_id == f"{session_entry.session_id}__orchestrator_fullagent"
    assert child_a.runtime_session_key == f"{session_entry.session_key}::fullagent"
    assert child_a.dedupe_key == "analyze repo deeply"
    assert child_b.dedupe_key == "analyze repo deeply"


def test_orchestrator_child_runtime_identity_stays_session_singleton_for_different_dedupe_keys():
    from gateway.orchestrator import OrchestratorChildRequest

    runner = _make_runner()
    runtime = _runtime(runner)
    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    event = _make_event("please analyze this repo deeply")
    platform_turn = AdapterTurnPlan()

    request_a = OrchestratorChildRequest(goal="analyze repo", dedupe_key="analyze repo deeply")
    request_b = OrchestratorChildRequest(goal="trace gateway cache bug", dedupe_key="trace gateway cache bug")

    launch_a = runtime._make_child_launch_spec(
        event=event,
        session_entry=session_entry,
        source=event.source,
        request=request_a,
        context_prompt="ctx",
        channel_prompt=None,
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        event_message_id=event.message_id,
        platform_turn=platform_turn,
        trace_ctx=None,
        originating_user_message=event.text,
    )
    launch_b = runtime._make_child_launch_spec(
        event=event,
        session_entry=session_entry,
        source=event.source,
        request=request_b,
        context_prompt="ctx",
        channel_prompt=None,
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        event_message_id=event.message_id,
        platform_turn=platform_turn,
        trace_ctx=None,
        originating_user_message=event.text,
    )

    child_a = runtime._build_child_state(
        session=session,
        session_entry=session_entry,
        request=request_a,
        launch_spec=launch_a,
        trace_ctx=None,
    )
    child_b = runtime._build_child_state(
        session=session,
        session_entry=session_entry,
        request=request_b,
        launch_spec=launch_b,
        trace_ctx=None,
    )

    assert child_a.runtime_session_id == child_b.runtime_session_id
    assert child_a.runtime_session_key == child_b.runtime_session_key
    assert child_a.dedupe_key == "analyze repo deeply"
    assert child_b.dedupe_key == "trace gateway cache bug"


def test_orchestrator_child_runtime_identity_falls_back_to_normalized_goal():
    from gateway.orchestrator import OrchestratorChildRequest

    runner = _make_runner()
    runtime = _runtime(runner)
    session_entry = _make_session_entry()
    session = _extension(runner).get_or_create_orchestrator_session(session_entry.session_key)
    event = _make_event("Please analyze this repo deeply!!!")
    platform_turn = AdapterTurnPlan()

    request_a = OrchestratorChildRequest(goal="Please analyze this repo deeply!!!")
    request_b = OrchestratorChildRequest(goal="please analyze this repo deeply")

    launch_a = runtime._make_child_launch_spec(
        event=event,
        session_entry=session_entry,
        source=event.source,
        request=request_a,
        context_prompt="ctx",
        channel_prompt=None,
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        event_message_id=event.message_id,
        platform_turn=platform_turn,
        trace_ctx=None,
        originating_user_message=event.text,
    )
    launch_b = runtime._make_child_launch_spec(
        event=event,
        session_entry=session_entry,
        source=event.source,
        request=request_b,
        context_prompt="ctx",
        channel_prompt=None,
        dynamic_disabled_skills=[],
        dynamic_disabled_toolsets=[],
        event_message_id=event.message_id,
        platform_turn=platform_turn,
        trace_ctx=None,
        originating_user_message=event.text,
    )

    child_a = runtime._build_child_state(
        session=session,
        session_entry=session_entry,
        request=request_a,
        launch_spec=launch_a,
        trace_ctx=None,
    )
    child_b = runtime._build_child_state(
        session=session,
        session_entry=session_entry,
        request=request_b,
        launch_spec=launch_b,
        trace_ctx=None,
    )

    assert child_a.runtime_session_id == child_b.runtime_session_id
    assert child_a.runtime_session_key == child_b.runtime_session_key
    assert child_a.dedupe_key == "please analyze this repo deeply"
