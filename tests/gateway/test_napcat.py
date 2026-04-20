"""Tests for NapCat / QQ gateway support."""

import asyncio
import base64
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, GatewayOrchestratorConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, SessionStore, build_session_key


def _make_adapter(**extra):
    from gateway.platforms.napcat import NapCatAdapter

    reply_to_mode = extra.pop("reply_to_mode", "first")
    group_chat_enabled = extra.pop("group_chat_enabled", True)
    config = PlatformConfig(
        enabled=True,
        token=extra.pop("token", "tok"),
        reply_to_mode=reply_to_mode,
        extra={"ws_url": "ws://127.0.0.1:3001/", "group_chat_enabled": group_chat_enabled, **extra},
    )
    return NapCatAdapter(config)


class TestNapCatPlatformEnum:
    def test_napcat_enum_exists(self):
        assert Platform.NAPCAT.value == "napcat"

    def test_napcat_declares_no_message_editing(self):
        from gateway.platforms.napcat import NapCatAdapter

        assert NapCatAdapter.SUPPORTS_MESSAGE_EDITING is False

    def test_napcat_disables_non_llm_status_messages(self):
        from gateway.platforms.napcat import NapCatAdapter

        assert NapCatAdapter.EMIT_NON_LLM_STATUS_MESSAGES is False

    def test_napcat_strips_markdown_for_plain_text(self):
        from gateway.platforms.napcat import NapCatAdapter

        adapter = NapCatAdapter(PlatformConfig(enabled=True, token="tok", extra={"ws_url": "ws://127.0.0.1:3001/"}))
        formatted = adapter.format_message("## Heading\n**Bold** [Link](https://example.com)\n`Code`\n```\nBlock content\n```")

        assert formatted == "Heading\nBold Link\nCode\nBlock content"


class TestGatewayRunStructureGuards:
    def test_run_module_limits_napcat_references_to_registration_and_auth_maps(self):
        src = Path("gateway/run.py").read_text(encoding="utf-8")
        lines = [line.strip() for line in src.splitlines() if "Platform.NAPCAT" in line]
        assert lines == [
            'Platform.NAPCAT: "NAPCAT_ALLOWED_USERS",',
            'Platform.NAPCAT: "NAPCAT_ALLOW_ALL_USERS",',
        ]
        assert "NapCatAdapter" not in src
        assert "_session_model_overrides_ref" not in src
        assert "AgentEventType as _NapCatEventType" not in src
        assert "AgentEventSeverity as _NapCatSeverity" not in src
        assert "gateway.napcat_orchestrator_support" not in src
        assert "gateway.platforms.napcat_turn_context" not in src
        assert "_napcat_trace_ctx" not in src
        assert "napcat_referenced_image_urls" not in src
        assert "napcat_vision_cache" not in src

    def test_shared_config_module_keeps_napcat_defaults_and_env_parsing_out_of_core(self):
        src = Path("gateway/config.py").read_text(encoding="utf-8")

        assert 'os.getenv("NAPCAT_WS_URL")' not in src
        assert 'os.getenv("NAPCAT_TOKEN")' not in src
        assert 'os.getenv("NAPCAT_HOME_CHANNEL")' not in src
        assert '"enabled_platforms": ["napcat"]' not in src
        assert "minimax-2.7-highspeed" not in src
        assert 'plat == Platform.NAPCAT and "orchestrator" in platform_cfg' not in src
        assert 'platform == Platform.NAPCAT and config.extra.get("ws_url")' not in src

    def test_shared_loader_modules_do_not_name_branch_impl_classes(self):
        runtime_src = Path("gateway/runtime_extension.py").read_text(encoding="utf-8")
        config_src = Path("gateway/config_extension.py").read_text(encoding="utf-8")
        observability_src = Path("gateway/observability_backend.py").read_text(encoding="utf-8")

        assert "NapCatGatewayExtension" not in runtime_src
        assert "NapCatGatewayConfigExtension" not in config_src
        assert "NapCatObservabilityBackend" not in observability_src


class TestNapCatConfigLoading:
    def test_apply_platform_defaults_disable_napcat_groups_by_default(self):
        from gateway.config import GatewayConfig, PlatformConfig, _apply_platform_defaults

        config = GatewayConfig(
            platforms={
                Platform.NAPCAT: PlatformConfig(enabled=True, extra={"ws_url": "ws://127.0.0.1:3001/"})
            }
        )

        _apply_platform_defaults(config)

        assert config.platforms[Platform.NAPCAT].extra["group_chat_enabled"] is False

    def test_apply_env_overrides_napcat(self, monkeypatch):
        monkeypatch.setenv("NAPCAT_WS_URL", "ws://127.0.0.1:3001/")
        monkeypatch.setenv("NAPCAT_TOKEN", "secret")
        monkeypatch.setenv("NAPCAT_HOME_CHANNEL", "group:123456")

        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.NAPCAT in config.platforms
        pc = config.platforms[Platform.NAPCAT]
        assert pc.enabled is True
        assert pc.token == "secret"
        assert pc.extra["ws_url"] == "ws://127.0.0.1:3001/"
        assert pc.home_channel.chat_id == "group:123456"

    def test_connected_platforms_includes_napcat(self, monkeypatch):
        monkeypatch.setenv("NAPCAT_WS_URL", "ws://127.0.0.1:3001/")

        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.NAPCAT in config.get_connected_platforms()


class TestNapCatAuthorization:
    def test_napcat_allow_all_users(self):
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig()
        runner.pairing_store = MagicMock()
        runner.pairing_store.is_approved.return_value = False
        source = SessionSource(platform=Platform.NAPCAT, chat_id="private:1", chat_type="dm", user_id="10001")

        with patch.dict("os.environ", {"NAPCAT_ALLOW_ALL_USERS": "true"}, clear=True):
            assert runner._is_user_authorized(source) is True

    @pytest.mark.asyncio
    async def test_napcat_non_super_admin_commands_are_blocked(self):
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig()
        runner.adapters = {Platform.NAPCAT: _make_adapter(super_admins=["99999"])}
        runner.hooks = SimpleNamespace(emit=AsyncMock())
        runner._running_agents = {}
        runner._running_agents_ts = {}
        runner._pending_messages = {}
        runner._draining = False
        runner._update_prompt_pending = {}
        runner._session_key_for_source = lambda source: f"napcat:{source.chat_id}"
        runner._is_user_authorized = lambda source: True
        runner._get_unauthorized_dm_behavior = lambda platform: "ignore"

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="10001",
            chat_type="dm",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="/help", source=source)

        result = await runner._handle_message(event)

        assert result == "NapCat commands are restricted to super admins."

    @pytest.mark.asyncio
    async def test_napcat_friend_dm_bypasses_authorization(self):
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig()
        runner.adapters = {Platform.NAPCAT: _make_adapter(super_admins=["10001"])}
        runner.hooks = SimpleNamespace(emit=AsyncMock())
        runner._running_agents = {}
        runner._running_agents_ts = {}
        runner._pending_messages = {}
        runner._draining = False
        runner._update_prompt_pending = {}
        runner._session_key_for_source = lambda source: f"napcat:{source.chat_id}"
        runner._is_user_authorized = MagicMock(return_value=False)
        runner._get_unauthorized_dm_behavior = lambda platform: "pair"
        runner._handle_help_command = AsyncMock(return_value="help-ok")

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="10001",
            chat_type="dm",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(
            text="/help",
            source=source,
            raw_message={"message_type": "private", "sub_type": "friend"},
        )

        result = await runner._handle_message(event)

        assert result == "help-ok"
        runner._is_user_authorized.assert_not_called()
        runner._handle_help_command.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_napcat_non_friend_dm_is_blackholed(self):
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig()
        runner.adapters = {Platform.NAPCAT: _make_adapter(super_admins=["10001"])}
        runner.hooks = SimpleNamespace(emit=AsyncMock())
        runner._running_agents = {}
        runner._running_agents_ts = {}
        runner._pending_messages = {}
        runner._draining = False
        runner._update_prompt_pending = {}
        runner._session_key_for_source = lambda source: f"napcat:{source.chat_id}"
        runner._is_user_authorized = MagicMock(return_value=False)
        runner._get_unauthorized_dm_behavior = lambda platform: "pair"
        runner.pairing_store = MagicMock()

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="20002",
            chat_type="dm",
            user_id="20002",
            user_name="Stranger",
        )
        event = MessageEvent(
            text="hello",
            source=source,
            raw_message={"message_type": "private", "sub_type": "other"},
        )

        result = await runner._handle_message(event)

        assert result is None
        runner._is_user_authorized.assert_not_called()
        runner.pairing_store.generate_code.assert_not_called()

    @pytest.mark.asyncio
    async def test_napcat_super_admin_commands_still_work(self, monkeypatch):
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig()
        runner.adapters = {Platform.NAPCAT: _make_adapter(super_admins=["10001"])}
        runner.hooks = SimpleNamespace(emit=AsyncMock())
        runner._running_agents = {}
        runner._running_agents_ts = {}
        runner._pending_messages = {}
        runner._draining = False
        runner._update_prompt_pending = {}
        runner._session_key_for_source = lambda source: f"napcat:{source.chat_id}"
        runner._is_user_authorized = lambda source: True
        runner._get_unauthorized_dm_behavior = lambda platform: "ignore"
        runner._handle_help_command = AsyncMock(return_value="help-ok")

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="10001",
            chat_type="dm",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="/help", source=source)

        event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-help-1")
        captured = []
        monkeypatch.setattr(
            "gateway.run._emit_gateway_trace_event",
            lambda trace_ctx, event_type, payload, **kwargs: captured.append(
                (event_type, payload, {"trace_ctx": trace_ctx, **kwargs})
            ),
        )

        result = await runner._handle_message(event)

        assert result == "help-ok"
        runner._handle_help_command.assert_awaited_once_with(event)
        short_circuit = next(
            payload for event_type, payload, _ in captured
            if getattr(event_type, "value", event_type) == "gateway.turn.short_circuited"
        )
        assert short_circuit["reason"] == "gateway_command"
        assert short_circuit["command"] == "help"

    @pytest.mark.asyncio
    async def test_napcat_skill_command_emits_observability_event(self, monkeypatch):
        import gateway.run as gateway_run
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={
                Platform.NAPCAT: PlatformConfig(
                    enabled=True,
                    token="tok",
                    extra={"orchestrator": {"enabled_platforms": []}},
                )
            },
            gateway_orchestrator=GatewayOrchestratorConfig(enabled_platforms=[]),
        )
        runner.adapters = {Platform.NAPCAT: _make_adapter(super_admins=["10001"])}
        runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
        runner._running_agents = {}
        runner._running_agents_ts = {}
        runner._pending_messages = {}
        runner._pending_approvals = {}
        runner._draining = False
        runner._update_prompt_pending = {}
        runner._voice_mode = {}
        runner._session_db = None
        runner._reasoning_config = None
        runner._provider_routing = {}
        runner._fallback_model = None
        runner._show_reasoning = False
        runner._is_user_authorized = lambda source: True
        runner._set_session_env = lambda _context: None
        runner._should_send_voice_reply = lambda *_args, **_kwargs: False
        runner._send_voice_reply = AsyncMock()
        runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
        runner._emit_gateway_run_progress = AsyncMock()
        runner._update_runtime_status = lambda *_args, **_kwargs: None
        runner._get_unauthorized_dm_behavior = lambda platform: "ignore"
        runner._run_agent = AsyncMock(return_value={
            "final_response": "ok",
            "messages": [],
            "api_calls": 0,
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "model": "test-model",
            "provider": "test-provider",
            "api_mode": "responses",
            "base_url": "",
            "promotion_stage": "",
            "turn_usage": {},
        })

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="10001",
            chat_type="dm",
            user_id="10001",
            user_name="Alice",
        )
        session_entry = SessionEntry(
            session_key=build_session_key(source),
            session_id="sess-1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            platform=Platform.NAPCAT,
            chat_type="dm",
        )
        runner.session_store = MagicMock()
        runner.session_store.get_or_create_session.return_value = session_entry
        runner.session_store.load_transcript.return_value = []
        runner.session_store.has_any_sessions.return_value = True
        runner.session_store.append_to_transcript = MagicMock()
        runner.session_store.rewrite_transcript = MagicMock()
        runner.session_store.update_session = MagicMock()

        monkeypatch.setattr(
            gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"}
        )
        monkeypatch.setattr(
            "agent.skill_commands.get_skill_commands",
            lambda: {"/memory-audit": {"name": "memory-audit"}},
        )
        monkeypatch.setattr(
            "agent.skill_commands.resolve_skill_command_key",
            lambda command: "/memory-audit" if command == "memory-audit" else None,
        )
        monkeypatch.setattr(
            "agent.skill_commands.build_skill_invocation_message",
            lambda *args, **kwargs: "Injected skill payload",
        )

        captured = []
        monkeypatch.setattr(
            gateway_run,
            "_emit_gateway_trace_event",
            lambda trace_ctx, event_type, payload, **kwargs: captured.append(
                (event_type, payload, {"trace_ctx": trace_ctx, **kwargs})
            ),
        )

        event = MessageEvent(text="/memory-audit explain recent memory", source=source, message_id="m1")
        event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-skill-1")

        result = await runner._handle_message(event)

        assert result == "ok"
        assert event.text == "Injected skill payload"
        runner._run_agent.assert_awaited_once()
        skill_event = next(
            payload for event_type, payload, _ in captured
            if getattr(event_type, "value", event_type) == "agent.skill.used"
        )
        assert skill_event["skill_name"] == "memory-audit"
        assert skill_event["source"] == "slash_command"
        assert skill_event["command"] == "/memory-audit"


class TestNapCatGatewaySessionMessages:
    def test_auto_reset_notice_is_suppressed_in_group_chats(self):
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.adapters = {Platform.NAPCAT: _make_adapter(super_admins=["10001"])}
        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="group:123456",
            chat_type="group",
            user_id="10001",
        )

        assert runner._should_send_auto_reset_notice(source) is False
        assert runner._should_include_session_info(source) is False
        assert runner._should_send_status_callback_message(source, "lifecycle") is False
        assert runner._should_send_status_callback_message(source, "context_pressure") is False

    def test_session_messages_are_suppressed_in_friend_dms(self):
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.adapters = {Platform.NAPCAT: _make_adapter(super_admins=["10001"])}
        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="10001",
            chat_type="dm",
            user_id="10001",
        )

        assert runner._should_send_auto_reset_notice(source) is False
        assert runner._should_include_session_info(source) is False
        assert runner._should_send_status_callback_message(source, "lifecycle") is False


class TestNapCatPassiveGroupContext:
    @pytest.mark.asyncio
    async def test_group_messages_are_ignored_when_group_chat_switch_is_off(self):
        from gateway.platforms.napcat import NapCatAdapter

        adapter = NapCatAdapter(
            PlatformConfig(enabled=True, token="tok", extra={"ws_url": "ws://127.0.0.1:3001/"})
        )
        adapter.handle_message = AsyncMock()
        passive_handler = AsyncMock()
        adapter.set_passive_message_handler(passive_handler)

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "123456",
                "group_name": "Dev Group",
                "user_id": "10001",
                "message_id": "m-group-disabled",
                "message": [{"type": "text", "data": {"text": "Just casual chat"}}],
                "sender": {"nickname": "Alice"},
            }
        )

        passive_handler.assert_not_awaited()
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_group_messages_are_ignored_when_group_chat_switch_targets_other_groups(self):
        adapter = _make_adapter(group_chat_enabled=["547996548"])
        adapter.handle_message = AsyncMock()

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "123456",
                "group_name": "Dev Group",
                "user_id": "10001",
                "message_id": "m-group-not-allowed",
                "message": [{"type": "text", "data": {"text": "Just casual chat"}}],
                "sender": {"nickname": "Alice"},
            }
        )

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_explicit_group_message_is_forwarded_for_orchestrator_handling(self):
        adapter = _make_adapter(group_trigger_rules={"123456": {}})
        adapter.handle_message = AsyncMock()
        passive_handler = AsyncMock()
        adapter.set_passive_message_handler(passive_handler)

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "123456",
                "group_name": "Dev Group",
                "user_id": "10001",
                "message_id": "m1",
                "message": [{"type": "text", "data": {"text": "Just casual chat"}}],
                "sender": {"nickname": "Alice"},
            }
        )

        passive_handler.assert_not_awaited()
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "Just casual chat"
        assert event.metadata["napcat_trigger_reason"] == "group_message"
        assert event.source.chat_type == "group"

    @pytest.mark.asyncio
    async def test_non_explicit_super_admin_group_message_is_forwarded_for_orchestrator_handling(self):
        adapter = _make_adapter(group_trigger_rules={"123456": {}}, super_admins=["10001"])
        adapter.handle_message = AsyncMock()
        passive_handler = AsyncMock()
        adapter.set_passive_message_handler(passive_handler)

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "123456",
                "group_name": "Dev Group",
                "user_id": "10001",
                "message_id": "m-super-no-trigger",
                "message": [{"type": "text", "data": {"text": "Just casual chat"}}],
                "sender": {"nickname": "Alice"},
            }
        )

        passive_handler.assert_not_awaited()
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "Just casual chat"
        assert event.metadata["napcat_trigger_reason"] == "group_message"
        assert event.source.chat_type == "group"

    @pytest.mark.asyncio
    async def test_passive_group_messages_persist_into_shared_session_transcript(self, tmp_path):
        from gateway.run import GatewayRunner

        config = GatewayConfig(
            group_sessions_per_user=False,
            platforms={
                Platform.NAPCAT: PlatformConfig(
                    enabled=True,
                    token="tok",
                    extra={"ws_url": "ws://127.0.0.1:3001/"},
                )
            },
            sessions_dir=tmp_path,
        )
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = config
        runner.adapters = {Platform.NAPCAT: _make_adapter()}
        runner.session_store = SessionStore(sessions_dir=tmp_path, config=config)
        runner.session_store._db = None
        runner._is_user_authorized = lambda source: True

        source_alice = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_name="Dev Group",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )
        source_bob = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_name="Dev Group",
            chat_type="group",
            user_id="10002",
            user_name="Bob",
        )

        await runner._handle_passive_message(MessageEvent(text="Msg one", source=source_alice, message_id="m-alice"))
        await runner._handle_passive_message(MessageEvent(text="Msg two", source=source_bob, message_id="m-bob"))

        session_key = runner.session_store._generate_session_key(source_alice)
        entry = runner.session_store.get_session_entry(session_key)
        assert entry is not None

        transcript = runner.session_store.load_transcript(entry.session_id)
        user_messages = [msg["content"] for msg in transcript if msg.get("role") == "user"]
        assert user_messages == ["[Alice] Msg one", "[Bob] Msg two"]
        user_entries = [msg for msg in transcript if msg.get("role") == "user"]
        assert user_entries[0]["platform_message_id"] == "m-alice"
        assert user_entries[0]["platform_chat_id"] == "123456"
        assert user_entries[1]["platform_message_id"] == "m-bob"

    @pytest.mark.asyncio
    async def test_context_only_shared_image_message_is_not_persisted(self, tmp_path):
        from gateway.run import GatewayRunner

        config = GatewayConfig(
            group_sessions_per_user=False,
            platforms={
                Platform.NAPCAT: PlatformConfig(
                    enabled=True,
                    token="tok",
                    extra={"ws_url": "ws://127.0.0.1:3001/"},
                )
            },
            sessions_dir=tmp_path,
        )
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = config
        runner.adapters = {Platform.NAPCAT: _make_adapter()}
        runner.session_store = SessionStore(sessions_dir=tmp_path, config=config)
        runner._is_user_authorized = lambda source: True

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_name="Dev Group",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )

        event = MessageEvent(
            text="",
            source=source,
            media_urls=[str((tmp_path / "img.png").resolve())],
            media_types=["image/png"],
        )
        event.metadata = {"napcat_context_only": True}

        handled = await runner.adapters[Platform.NAPCAT].handle_passive_event(event, runner.session_store)

        assert handled is False
        session_key = runner.session_store._generate_session_key(source)
        entry = runner.session_store.get_session_entry(session_key)
        if entry is not None:
            transcript = runner.session_store.load_transcript(entry.session_id)
            assert [msg for msg in transcript if msg.get("role") == "user"] == []

    @pytest.mark.asyncio
    async def test_context_only_shared_text_plus_image_keeps_text_without_media_placeholder(self, tmp_path):
        from gateway.run import GatewayRunner

        config = GatewayConfig(
            group_sessions_per_user=False,
            platforms={
                Platform.NAPCAT: PlatformConfig(
                    enabled=True,
                    token="tok",
                    extra={"ws_url": "ws://127.0.0.1:3001/"},
                )
            },
            sessions_dir=tmp_path,
        )
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = config
        runner.adapters = {Platform.NAPCAT: _make_adapter()}
        runner.session_store = SessionStore(sessions_dir=tmp_path, config=config)
        runner._is_user_authorized = lambda source: True

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_name="Dev Group",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )

        event = MessageEvent(
            text="你为什么没有我好友",
            source=source,
            media_urls=[str((tmp_path / "img.png").resolve())],
            media_types=["image/png"],
        )
        event.metadata = {"napcat_context_only": True}

        handled = await runner.adapters[Platform.NAPCAT].handle_passive_event(event, runner.session_store)

        assert handled is True
        session_key = runner.session_store._generate_session_key(source)
        entry = runner.session_store.get_session_entry(session_key)
        assert entry is not None
        transcript = runner.session_store.load_transcript(entry.session_id)
        user_messages = [msg["content"] for msg in transcript if msg.get("role") == "user"]
        assert user_messages == ["[Alice] 你为什么没有我好友"]

    @pytest.mark.asyncio
    async def test_reset_command_omits_session_info_in_napcat_group(self, monkeypatch):
        from gateway.run import GatewayRunner
        from gateway.session import build_session_key

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={
                Platform.NAPCAT: PlatformConfig(
                    enabled=True,
                    token="tok",
                    extra={"ws_url": "ws://127.0.0.1:3001/"},
                )
            }
        )
        runner.adapters = {Platform.NAPCAT: _make_adapter(super_admins=["10001"])}
        runner._voice_mode = {}
        runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
        runner._session_model_overrides = {}
        runner._pending_model_notes = {}
        runner._background_tasks = set()
        runner._running_agents = {}
        runner._running_agents_ts = {}
        runner._pending_messages = {}
        runner._pending_approvals = {}
        runner._update_prompt_pending = {}
        runner._draining = False
        runner._session_db = None
        runner._agent_cache_lock = None
        runner._is_user_authorized = lambda _source: True
        runner._get_unauthorized_dm_behavior = lambda _platform: "ignore"
        runner._format_session_info = MagicMock(return_value="◆ Model: `MiniMax-M2.7-highspeed`")

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="/new", source=source, message_id="m1")
        session_key = build_session_key(source)
        session_entry = SessionEntry(
            session_key=session_key,
            session_id="sess-1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            platform=Platform.NAPCAT,
            chat_type="group",
        )
        runner.session_store = MagicMock()
        runner.session_store.get_or_create_session.return_value = session_entry
        runner.session_store.reset_session.return_value = session_entry
        runner.session_store._entries = {session_key: session_entry}
        runner.session_store._generate_session_key.return_value = session_key
        event.napcat_trace_ctx = SimpleNamespace(trace_id="trace-reset-1")

        captured = []
        monkeypatch.setattr(
            "gateway.run._emit_gateway_trace_event",
            lambda trace_ctx, event_type, payload, **kwargs: captured.append(
                (event_type, payload, {"trace_ctx": trace_ctx, **kwargs})
            ),
        )

        result = await runner._handle_message(event)

        assert "Session reset" in result or "New session" in result
        assert "◆ Model:" not in result
        runner._format_session_info.assert_not_called()
        short_circuit = next(
            payload for event_type, payload, _ in captured
            if getattr(event_type, "value", event_type) == "gateway.turn.short_circuited"
        )
        assert short_circuit["reason"] == "reset_command"
        assert short_circuit["command"] == "new"


class TestNapCatAuthStore:
    def test_grant_and_revoke(self, tmp_path):
        from gateway.platforms.napcat import NapCatAuthStore

        store = NapCatAuthStore(tmp_path / "napcat_auth.json")
        store.grant("sess-1", "123456")
        assert store.list_groups("sess-1") == ["123456"]
        store.revoke("sess-1", "123456")
        assert store.list_groups("sess-1") == []

    @pytest.mark.asyncio
    async def test_apply_auth_intent_resolves_group_name_via_live_group_list(self):
        adapter = _make_adapter()
        adapter._call_action = AsyncMock(
            return_value={
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": "10001", "group_name": "TestGroup"},
            }
        )

        fake_store = SimpleNamespace(
            list_sessions=lambda: [],
        )

        with patch(
            "gateway.platforms.napcat.call_napcat_action_once",
            new=AsyncMock(
                return_value={
                    "status": "ok",
                    "retcode": 0,
                    "data": [{"group_id": "123456", "group_name": "TestGroup"}],
                }
            ),
        ):
            result = await adapter._apply_auth_intent(
                text="Please authorize reading TestGroup context",
                session_id="sess-1",
                user_id="10001",
                session_store=fake_store,
            )

        assert "TestGroup" in result["note"]
        assert "TestGroup" in result["response"]
        assert adapter.auth_store.list_groups("sess-1") == ["123456"]


class TestNapCatAdapterEvents:
    @pytest.mark.asyncio
    async def test_dispatch_incoming_notice_routes_to_notice_handler(self):
        adapter = _make_adapter()
        adapter._handle_incoming_event = AsyncMock()
        adapter._handle_notice_event = AsyncMock()

        await adapter._dispatch_incoming_payload({"post_type": "notice", "notice_type": "notify", "sub_type": "poke"})

        adapter._handle_notice_event.assert_awaited_once()
        adapter._handle_incoming_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_friend_recall_notice_routes_to_recall_handler(self):
        adapter = _make_adapter()
        adapter._handle_recall_notice = AsyncMock()

        await adapter._handle_notice_event(
            {
                "post_type": "notice",
                "notice_type": "friend_recall",
                "user_id": "10001",
                "message_id": "m-recall",
            }
        )

        adapter._handle_recall_notice.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_group_recall_notice_emits_structured_recall_event(self):
        adapter = _make_adapter()
        captured = []

        async def _capture(event):
            captured.append(event)

        adapter.set_recall_event_handler(_capture)

        await adapter._handle_notice_event(
            {
                "post_type": "notice",
                "notice_type": "group_recall",
                "group_id": "123456",
                "user_id": "10001",
                "operator_id": "20002",
                "message_id": "m-recall",
            }
        )

        assert len(captured) == 1
        assert captured[0].recall_type == "group_recall"
        assert captured[0].chat_type == "group"
        assert captured[0].chat_id == "123456"
        assert captured[0].user_id == "10001"
        assert captured[0].operator_id == "20002"
        assert captured[0].message_id == "m-recall"

    @pytest.mark.asyncio
    async def test_connect_probes_login_before_starting_listener(self, monkeypatch):
        adapter = _make_adapter()

        login_probe = AsyncMock(
            return_value={
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": "42", "nickname": "NapCat"},
            }
        )
        monkeypatch.setattr("gateway.platforms.napcat.call_napcat_action_once", login_probe)

        fake_ws = AsyncMock()
        fake_ws.closed = False
        fake_session = AsyncMock()
        fake_session.ws_connect = AsyncMock(return_value=fake_ws)
        monkeypatch.setattr("gateway.platforms.napcat.aiohttp.ClientSession", MagicMock(return_value=fake_session))

        listener_started = asyncio.Event()

        async def fake_listen_loop():
            listener_started.set()
            while adapter._running:
                await asyncio.sleep(0)

        monkeypatch.setattr(adapter, "_listen_loop", fake_listen_loop)

        assert await adapter.connect() is True
        assert adapter._bot_user_id == "42"
        assert adapter._bot_nickname == "NapCat"
        login_probe.assert_awaited_once_with(
            "ws://127.0.0.1:3001/",
            "tok",
            "get_login_info",
            {},
            timeout=20.0,
        )
        fake_session.ws_connect.assert_awaited_once()
        await asyncio.wait_for(listener_started.wait(), timeout=1)

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_private_message_event_mapping(self):
        import gateway.platforms.napcat as napcat_mod

        adapter = _make_adapter()
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()
        emit_calls = []

        def _capture_emit(event_type, payload, **kwargs):
            emit_calls.append((event_type, payload, kwargs))

        with patch.object(napcat_mod, "emit_napcat_event", side_effect=_capture_emit):
            await adapter._handle_incoming_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "sub_type": "friend",
                    "user_id": "10001",
                    "message_id": "m1",
                    "message": [{"type": "text", "data": {"text": "hello"}}],
                    "sender": {"nickname": "Alice"},
                }
            )

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "hello"
        assert event.source.chat_type == "dm"
        assert event.source.chat_id == "10001"
        assert getattr(event, "napcat_trace_ctx", None) is not None
        assert event.napcat_trace_ctx.chat_type == "private"
        assert event.napcat_trace_ctx.chat_id == "10001"
        assert event.napcat_trace_ctx.message_id == "m1"
        assert event.metadata["napcat_trigger_reason"] == "dm"
        assert event.metadata["napcat_private_sub_type"] == "friend"
        media_events = [kwargs.get("trace_ctx") for event_type, _, kwargs in emit_calls if event_type == napcat_mod.EventType.MESSAGE_MEDIA_EXTRACTED]
        assert media_events
        assert media_events[-1] is not None
        assert media_events[-1].chat_id == "10001"
        assert media_events[-1].message_id == "m1"

    @pytest.mark.asyncio
    async def test_private_message_emits_trace_before_slow_media_enrichment(self, monkeypatch):
        import gateway.platforms.napcat as napcat_mod

        adapter = _make_adapter()
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()
        emit_calls = []
        enrichment_started = asyncio.Event()
        release_enrichment = asyncio.Event()

        async def _slow_collect_inbound_media(segments, **kwargs):
            enrichment_started.set()
            await release_enrichment.wait()
            return [], [], segments, MessageType.TEXT

        def _capture_emit(event_type, payload, **kwargs):
            emit_calls.append((event_type, payload, kwargs))

        monkeypatch.setattr(adapter, "_collect_inbound_media", _slow_collect_inbound_media)

        with patch.object(napcat_mod, "emit_napcat_event", side_effect=_capture_emit):
            task = asyncio.create_task(
                adapter._handle_incoming_event(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "sub_type": "friend",
                        "user_id": "10001",
                        "message_id": "m-slow-media",
                        "message": [{"type": "text", "data": {"text": "hello"}}],
                        "sender": {"nickname": "Alice"},
                    }
                )
            )

            await asyncio.wait_for(enrichment_started.wait(), timeout=1)

            trace_created = [
                payload
                for event_type, payload, _ in emit_calls
                if event_type == napcat_mod.EventType.TRACE_CREATED
            ]
            message_received = [
                payload
                for event_type, payload, _ in emit_calls
                if event_type == napcat_mod.EventType.MESSAGE_RECEIVED
            ]
            assert trace_created
            assert trace_created[0]["message_id"] == "m-slow-media"
            assert message_received
            assert message_received[0]["message_id"] == "m-slow-media"
            assert message_received[0]["text_preview"] == "hello"
            adapter.handle_message.assert_not_awaited()

            release_enrichment.set()
            await asyncio.wait_for(task, timeout=1)

        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_private_message_emits_message_received_before_slow_reply_resolution(self, monkeypatch):
        import gateway.platforms.napcat as napcat_mod

        adapter = _make_adapter()
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()
        emit_calls = []
        reply_started = asyncio.Event()
        release_reply = asyncio.Event()

        async def _slow_resolve_reply_context(message_id, **kwargs):
            reply_started.set()
            await release_reply.wait()
            return None, None, [], []

        def _capture_emit(event_type, payload, **kwargs):
            emit_calls.append((event_type, payload, kwargs))

        monkeypatch.setattr(adapter, "_resolve_reply_context", _slow_resolve_reply_context)

        with patch.object(napcat_mod, "emit_napcat_event", side_effect=_capture_emit):
            task = asyncio.create_task(
                adapter._handle_incoming_event(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "sub_type": "friend",
                        "user_id": "10001",
                        "message_id": "m-slow-reply",
                        "message": [
                            {"type": "reply", "data": {"id": "reply-1"}},
                            {"type": "text", "data": {"text": "replying now"}},
                        ],
                        "sender": {"nickname": "Alice"},
                    }
                )
            )

            await asyncio.wait_for(reply_started.wait(), timeout=1)

            message_received = [
                payload
                for event_type, payload, _ in emit_calls
                if event_type == napcat_mod.EventType.MESSAGE_RECEIVED
            ]
            assert message_received
            assert message_received[0]["message_id"] == "m-slow-reply"
            assert message_received[0]["text_preview"] == "replying now"
            adapter.handle_message.assert_not_awaited()

            release_reply.set()
            await asyncio.wait_for(task, timeout=1)

        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_private_message_emits_early_trace_with_session_key(self, tmp_path):
        import gateway.platforms.napcat as napcat_mod

        config = GatewayConfig(
            platforms={
                Platform.NAPCAT: PlatformConfig(
                    enabled=True,
                    token="tok",
                    extra={"ws_url": "ws://127.0.0.1:3001/"},
                )
            }
        )
        store = SessionStore(tmp_path / "sessions", config)
        adapter = _make_adapter()
        adapter.set_session_store(store)
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()
        emit_calls = []

        def _capture_emit(event_type, payload, **kwargs):
            emit_calls.append((event_type, payload, kwargs))

        with patch.object(napcat_mod, "emit_napcat_event", side_effect=_capture_emit):
            await adapter._handle_incoming_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "sub_type": "friend",
                    "user_id": "10001",
                    "message_id": "m-session-dm",
                    "message": [{"type": "text", "data": {"text": "hello"}}],
                    "sender": {"nickname": "Alice"},
                }
            )

        expected_key = build_session_key(
            SessionSource(
                platform=Platform.NAPCAT,
                chat_id="10001",
                chat_name="Alice",
                chat_type="dm",
                user_id="10001",
                user_name="Alice",
            ),
            group_sessions_per_user=config.group_sessions_per_user,
            thread_sessions_per_user=config.thread_sessions_per_user,
        )
        trace_ctx = next(
            kwargs["trace_ctx"]
            for event_type, _, kwargs in emit_calls
            if event_type == napcat_mod.EventType.TRACE_CREATED
        )
        assert trace_ctx.session_key == expected_key

    @pytest.mark.asyncio
    async def test_group_message_emits_early_trace_with_session_key(self, tmp_path):
        import gateway.platforms.napcat as napcat_mod

        config = GatewayConfig(
            platforms={
                Platform.NAPCAT: PlatformConfig(
                    enabled=True,
                    token="tok",
                    extra={
                        "ws_url": "ws://127.0.0.1:3001/",
                        "group_chat_enabled": True,
                        "group_sessions_per_user": True,
                    },
                )
            }
        )
        store = SessionStore(tmp_path / "sessions", config)
        adapter = _make_adapter(group_chat_enabled=True, group_sessions_per_user=True)
        adapter.set_session_store(store)
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()
        emit_calls = []

        def _capture_emit(event_type, payload, **kwargs):
            emit_calls.append((event_type, payload, kwargs))

        with patch.object(napcat_mod, "emit_napcat_event", side_effect=_capture_emit):
            await adapter._handle_incoming_event(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": "123456",
                    "group_name": "Test Group",
                    "user_id": "10001",
                    "message_id": "m-session-group",
                    "message": [{"type": "text", "data": {"text": "hello group"}}],
                    "sender": {"nickname": "Alice"},
                }
            )

        expected_key = build_session_key(
            SessionSource(
                platform=Platform.NAPCAT,
                chat_id="123456",
                chat_name="Test Group",
                chat_type="group",
                user_id="10001",
                user_name="Alice",
            ),
            group_sessions_per_user=True,
            thread_sessions_per_user=config.thread_sessions_per_user,
        )
        trace_ctx = next(
            kwargs["trace_ctx"]
            for event_type, _, kwargs in emit_calls
            if event_type == napcat_mod.EventType.TRACE_CREATED
        )
        assert trace_ctx.session_key == expected_key

    @pytest.mark.asyncio
    async def test_private_message_image_segment_populates_media(self, tmp_path):
        adapter = _make_adapter()
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()
        image_path = tmp_path / "napcat-image.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "user_id": "10001",
                "message_id": "m-img",
                "message": [
                    {"type": "text", "data": {"text": "What is in this image"}},
                    {"type": "image", "data": {"file": str(image_path)}},
                ],
                "sender": {"nickname": "Alice"},
            }
        )

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "What is in this image"
        assert event.message_type == MessageType.PHOTO
        assert event.media_urls == [str(image_path.resolve())]
        assert event.media_types == ["image/png"]

    @pytest.mark.asyncio
    async def test_private_message_record_segment_populates_audio_media(self, tmp_path):
        adapter = _make_adapter()
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()
        audio_path = tmp_path / "napcat-voice.ogg"
        audio_path.write_bytes(b"OggS" + b"\x00" * 16)

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "user_id": "10001",
                "message_id": "m-voice",
                "message": [
                    {"type": "record", "data": {"file": str(audio_path)}},
                ],
                "sender": {"nickname": "Alice"},
            }
        )

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == ""
        assert event.message_type == MessageType.VOICE
        assert event.media_urls == [str(audio_path.resolve())]
        assert event.media_types == ["audio/ogg"]

    @pytest.mark.asyncio
    async def test_private_message_image_segment_logs_raw_payload(self, tmp_path, caplog):
        adapter = _make_adapter()
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()
        image_path = tmp_path / "napcat-image.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

        with caplog.at_level("INFO"):
            await adapter._handle_incoming_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "sub_type": "friend",
                    "user_id": "10001",
                    "message_id": "m-log-img",
                    "message": [
                        {
                            "type": "image",
                            "data": {
                                "file": str(image_path),
                                "url": "http://example.com/demo.png",
                                "summary": "[Image]",
                            },
                        }
                    ],
                    "sender": {"nickname": "Alice"},
                }
            )

        assert "NapCat inbound image payload" in caplog.text
        assert '"type": "image"' in caplog.text
        assert '"summary": "[Image]"' in caplog.text

    @pytest.mark.asyncio
    async def test_private_message_html_image_tag_populates_media(self, tmp_path):
        adapter = _make_adapter()
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()
        image_path = tmp_path / "embedded.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "user_id": "10001",
                "message_id": "m-html-img",
                "message": [
                    {
                        "type": "text",
                        "data": {"text": f'<img src="file://{image_path}" />What is in this image'},
                    }
                ],
                "sender": {"nickname": "Alice"},
            }
        )

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "What is in this image"
        assert event.message_type == MessageType.PHOTO
        assert event.media_urls == [str(image_path.resolve())]
        assert event.media_types == ["image/png"]

    @pytest.mark.asyncio
    async def test_non_friend_private_message_is_ignored(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "other",
                "user_id": "20002",
                "message_id": "m2",
                "message": [{"type": "text", "data": {"text": "hello"}}],
                "sender": {"nickname": "Stranger"},
            }
        )

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_private_message_is_ignored(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "user_id": "10001",
                "message_id": "m-empty",
                "message": [],
                "sender": {"nickname": "Alice"},
            }
        )

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_self_message_is_ignored(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": "42",
                "message_id": "m1",
                "message": [{"type": "text", "data": {"text": "loop"}}],
            }
        )

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_group_message_event_mapping(self):
        adapter = _make_adapter(group_trigger_rules={"123456": {}})
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "123456",
                "group_name": "Dev Group",
                "user_id": "10001",
                "message_id": "m2",
                "message": [
                    {"type": "at", "data": {"qq": "42"}},
                    {"type": "text", "data": {"text": " hello"}},
                ],
                "sender": {"nickname": "Alice", "card": "AliceCard"},
            }
        )

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.source.chat_type == "group"
        assert event.source.chat_id == "123456"
        assert event.source.chat_name == "Dev Group"
        assert getattr(event, "napcat_trace_ctx", None) is not None
        assert event.napcat_trace_ctx.chat_type == "group"
        assert event.napcat_trace_ctx.chat_id == "123456"
        assert event.napcat_trace_ctx.message_id == "m2"
        assert event.metadata["napcat_trigger_reason"] == "mention"

    @pytest.mark.asyncio
    async def test_empty_group_message_is_ignored(self):
        adapter = _make_adapter(group_trigger_rules={"123456": {}})
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()
        passive_handler = AsyncMock()
        adapter.set_passive_message_handler(passive_handler)

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "123456",
                "group_name": "Dev Group",
                "user_id": "10001",
                "message_id": "m-empty-group",
                "message": [],
                "sender": {"nickname": "Alice"},
            }
        )

        adapter.handle_message.assert_not_awaited()
        passive_handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_friend_poke_notice_becomes_dm_event(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()

        await adapter._handle_notice_event(
            {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "user_id": "10001",
                "target_id": "42",
                "sender": {"nickname": "Alice"},
            }
        )

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "戳了戳你"
        assert event.source.chat_type == "dm"
        assert event.source.chat_id == "10001"
        assert event.source.user_name == "Alice"
        assert event.metadata["napcat_trigger_reason"] == "poke"
        assert event.metadata["napcat_private_sub_type"] == "friend"
        assert event.metadata["napcat_notice_sub_type"] == "poke"
        assert event.metadata["napcat_poke_target_id"] == "42"
        assert getattr(event, "napcat_trace_ctx", None) is not None
        assert event.napcat_trace_ctx.chat_type == "private"
        assert event.napcat_trace_ctx.chat_id == "10001"

    @pytest.mark.asyncio
    async def test_group_poke_notice_targeting_bot_becomes_group_event(self):
        adapter = _make_adapter(group_trigger_rules={"123456": {"require_at": True}})
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()

        await adapter._handle_notice_event(
            {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "group_id": "123456",
                "group_name": "Dev Group",
                "user_id": "10001",
                "target_id": "42",
                "sender": {"nickname": "Alice", "card": "AliceCard"},
            }
        )

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "戳了戳你"
        assert event.source.chat_type == "group"
        assert event.source.chat_id == "123456"
        assert event.source.chat_name == "Dev Group"
        assert event.source.user_name == "AliceCard"
        assert event.metadata["napcat_trigger_reason"] == "poke"
        assert event.metadata["napcat_notice_type"] == "notify"
        assert event.metadata["napcat_notice_sub_type"] == "poke"
        assert event.metadata["napcat_poke_target_id"] == "42"
        assert getattr(event, "napcat_trace_ctx", None) is not None
        assert event.napcat_trace_ctx.chat_type == "group"
        assert event.napcat_trace_ctx.chat_id == "123456"

    @pytest.mark.asyncio
    async def test_group_gateway_command_bypasses_require_at(self):
        adapter = _make_adapter(group_trigger_rules={"123456": {"require_at": True}})
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "123456",
                "group_name": "Dev Group",
                "user_id": "10001",
                "message_id": "m-group-help",
                "message": [{"type": "text", "data": {"text": "/help"}}],
                "sender": {"nickname": "Alice", "card": "AliceCard"},
            }
        )

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "/help"
        assert event.source.chat_type == "group"
        assert event.metadata["napcat_trigger_reason"] == "gateway_command"

    @pytest.mark.asyncio
    async def test_group_poke_notice_for_other_target_is_ignored(self):
        adapter = _make_adapter(group_trigger_rules={"123456": {}})
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()

        await adapter._handle_notice_event(
            {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "group_id": "123456",
                "user_id": "10001",
                "target_id": "77777",
                "sender": {"nickname": "Alice"},
            }
        )

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_group_single_image_only_stays_context_only(self, tmp_path):
        # No @bot, no keywords — generic group_message trigger.
        # Single-image-only messages should stay context_only to avoid
        # unconditionally analysing every image in the group.
        adapter = _make_adapter(group_trigger_rules={"123456": {}})
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()
        passive_handler = AsyncMock()
        adapter.set_passive_message_handler(passive_handler)
        image_path = tmp_path / "standalone.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "123456",
                "group_name": "Dev Group",
                "user_id": "10001",
                "message_id": "m-img-only",
                "message": [
                    {"type": "image", "data": {"file": str(image_path)}},
                ],
                "sender": {"nickname": "Alice"},
            }
        )

        adapter.handle_message.assert_not_awaited()
        passive_handler.assert_awaited_once()
        event = passive_handler.await_args.args[0]
        assert event.media_urls == [str(image_path.resolve())]
        assert getattr(event, "napcat_trace_ctx", None) is not None
        assert event.napcat_trace_ctx.chat_type == "group"
        assert event.napcat_trace_ctx.chat_id == "123456"
        assert event.napcat_trace_ctx.message_id == "m-img-only"
        assert event.metadata["napcat_context_only"] is True
        assert event.metadata["napcat_trigger_reason"] == "single_image_only"

    @pytest.mark.asyncio
    async def test_group_single_image_only_with_explicit_trigger_runs_agent(self, tmp_path):
        # @bot + image — explicit mention trigger should let the image
        # through for normal agent handling.
        adapter = _make_adapter(group_trigger_rules={"123456": {}})
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()
        passive_handler = AsyncMock()
        adapter.set_passive_message_handler(passive_handler)
        image_path = tmp_path / "mention-img.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "123456",
                "group_name": "Dev Group",
                "user_id": "10001",
                "message_id": "m-mention-img",
                "message": [
                    {"type": "at", "data": {"qq": "42"}},
                    {"type": "image", "data": {"file": str(image_path)}},
                ],
                "sender": {"nickname": "Alice"},
            }
        )

        adapter.handle_message.assert_awaited_once()
        passive_handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_group_reply_to_single_image_populates_referenced_image_metadata(self, tmp_path):
        adapter = _make_adapter(group_trigger_rules={"123456": {}})
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()
        image_path = tmp_path / "reply-target.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

        async def _fake_call_action(action, params):
            assert action == "get_msg"
            assert params == {"message_id": "orig-img"}
            return {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "message": [{"type": "image", "data": {"file": str(image_path)}}],
                    "sender": {"user_id": "10002"},
                },
            }

        adapter._call_action = AsyncMock(side_effect=_fake_call_action)

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "123456",
                "group_name": "Dev Group",
                "user_id": "10001",
                "message_id": "m-reply",
                "message": [
                    {"type": "reply", "data": {"id": "orig-img"}},
                    {"type": "at", "data": {"qq": "42"}},
                    {"type": "text", "data": {"text": " What does this pic mean"}},
                ],
                "sender": {"nickname": "Alice"},
            }
        )

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.metadata["napcat_referenced_image_reason"] == "reply_to_group_single_image"
        assert event.metadata["napcat_referenced_image_urls"] == [str(image_path.resolve())]
        assert event.metadata["napcat_referenced_image_types"] == ["image/png"]

    @pytest.mark.asyncio
    async def test_group_oral_reference_uses_latest_single_image(self, tmp_path):
        adapter = _make_adapter(group_trigger_rules={"123456": {}})
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()
        passive_handler = AsyncMock()
        adapter.set_passive_message_handler(passive_handler)
        image_path = tmp_path / "latest-standalone.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "123456",
                "group_name": "Dev Group",
                "user_id": "10002",
                "message_id": "m-standalone",
                "message": [{"type": "image", "data": {"file": str(image_path)}}],
                "sender": {"nickname": "Bob"},
            }
        )

        await adapter._handle_incoming_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "123456",
                "group_name": "Dev Group",
                "user_id": "10001",
                "message_id": "m-ref",
                "message": [
                    {"type": "at", "data": {"qq": "42"}},
                    {"type": "text", "data": {"text": "What does this pic mean"}},
                ],
                "sender": {"nickname": "Alice"},
            }
        )

        event = adapter.handle_message.await_args.args[0]
        assert event.metadata["napcat_referenced_image_reason"] == "group_single_image_reference"
        assert event.metadata["napcat_referenced_image_urls"] == [str(image_path.resolve())]
        assert event.metadata["napcat_referenced_image_types"] == ["image/png"]


class TestNapCatTranscriptMetadata:
    def test_decorate_napcat_transcript_messages_tags_first_user_message(self):
        from gateway.napcat_gateway_extension import NapCatGatewayExtension

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="10001",
            chat_type="dm",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="hello", source=source, message_id="m-user-1")

        extension = NapCatGatewayExtension(SimpleNamespace())
        decorated = extension.decorate_transcript_messages(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
            event=event,
            source=source,
        )

        assert decorated[0]["platform"] == "napcat"
        assert decorated[0]["platform_message_id"] == "m-user-1"
        assert decorated[0]["platform_chat_type"] == "dm"
        assert decorated[0]["platform_chat_id"] == "10001"
        assert decorated[0]["platform_user_id"] == "10001"
        assert decorated[0]["napcat_direction"] == "inbound"
        assert "platform_message_id" not in decorated[1]


class TestNapCatRecallHandling:
    @pytest.mark.asyncio
    async def test_recall_rewrites_matching_transcript_message(self, tmp_path, monkeypatch):
        from gateway.platforms.napcat import NapCatRecallEvent
        from gateway.run import GatewayRunner

        config = GatewayConfig(
            platforms={
                Platform.NAPCAT: PlatformConfig(
                    enabled=True,
                    token="tok",
                    extra={"ws_url": "ws://127.0.0.1:3001/"},
                )
            }
        )
        store = SessionStore(tmp_path / "sessions", config)
        store._db = None
        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )
        entry = store.get_or_create_session(source)
        store.append_to_transcript(
            entry.session_id,
            {
                "role": "user",
                "content": "[Alice] original message",
                "timestamp": datetime.now().isoformat(),
                "platform": "napcat",
                "platform_message_id": "m1",
                "platform_chat_type": "group",
                "platform_chat_id": "123456",
                "platform_user_id": "10001",
                "napcat_direction": "inbound",
            },
        )

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = config
        runner.session_store = store

        captured = []
        monkeypatch.setattr(
            "gateway.napcat_gateway_extension.emit_gateway_event",
            lambda event_type, payload, **kwargs: captured.append((event_type, payload, kwargs)),
        )

        await runner._get_gateway_extension().handle_recall_event(
            NapCatRecallEvent(
                recall_type="group_recall",
                chat_type="group",
                chat_id="123456",
                user_id="10001",
                operator_id="20002",
                message_id="m1",
                raw_event={},
                trace_ctx=SimpleNamespace(trace_id="trace-recall-1"),
            )
        )

        transcript = runner.session_store.load_transcript(entry.session_id)
        assert transcript[0]["content"] == (
            "[System note: A previously recorded QQ group message was retracted and should no longer "
            "be treated as active context.]"
        )
        assert transcript[0]["napcat_recalled"] is True
        assert transcript[0]["napcat_recall_type"] == "group_recall"
        assert transcript[0]["napcat_recall_operator_id"] == "20002"
        payload = next(
            payload for event_type, payload, _ in captured
            if getattr(event_type, "value", event_type) == "message.recalled"
        )
        assert payload["transcript_match_found"] is True

    @pytest.mark.asyncio
    async def test_recall_without_match_appends_single_system_note(self, tmp_path):
        from gateway.platforms.napcat import NapCatRecallEvent
        from gateway.run import GatewayRunner

        config = GatewayConfig(
            platforms={
                Platform.NAPCAT: PlatformConfig(
                    enabled=True,
                    token="tok",
                    extra={"ws_url": "ws://127.0.0.1:3001/"},
                )
            }
        )
        store = SessionStore(tmp_path / "sessions", config)
        store._db = None
        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="10001",
            chat_type="dm",
            user_id="10001",
            user_name="Alice",
        )
        entry = store.get_or_create_session(source)
        store.append_to_transcript(
            entry.session_id,
            {
                "role": "user",
                "content": "hello",
                "timestamp": datetime.now().isoformat(),
                "platform": "napcat",
                "platform_message_id": "m-other",
                "platform_chat_type": "dm",
                "platform_chat_id": "10001",
                "platform_user_id": "10001",
                "napcat_direction": "inbound",
            },
        )

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = config
        runner.session_store = store

        recall_event = NapCatRecallEvent(
            recall_type="friend_recall",
            chat_type="dm",
            chat_id="10001",
            user_id="10001",
            operator_id="",
            message_id="m-missing",
            raw_event={},
            trace_ctx=None,
        )

        await runner._get_gateway_extension().handle_recall_event(recall_event)
        await runner._get_gateway_extension().handle_recall_event(recall_event)

        transcript = runner.session_store.load_transcript(entry.session_id)
        recall_notes = [
            msg for msg in transcript
            if msg.get("internal_source") == "napcat_recall" and msg.get("platform_message_id") == "m-missing"
        ]
        assert len(recall_notes) == 1
        assert "no matching transcript entry was found" in recall_notes[0]["content"]


class TestNapCatSend:
    def test_map_napcat_local_path_stages_unmapped_file_into_shared_dir(self, tmp_path, monkeypatch):
        from gateway.platforms.napcat import map_napcat_local_path

        mounted_root = tmp_path / "mounted"
        mounted_root.mkdir()
        hermes_home = mounted_root / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        image_path = outside_dir / "steve.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

        mapped = map_napcat_local_path(
            str(image_path),
            path_map=[{"host_prefix": str(mounted_root), "container_prefix": "/host-share"}],
            kind="Image",
        )

        assert mapped.startswith("/host-share/.hermes/cache/napcat_media/")
        staged_file = hermes_home / "cache" / "napcat_media" / Path(mapped).name
        assert staged_file.exists()
        assert staged_file.read_bytes() == image_path.read_bytes()

    def test_map_napcat_file_path_stages_unmapped_file_into_shared_dir(self, tmp_path, monkeypatch):
        from gateway.platforms.napcat import map_napcat_file_path

        mounted_root = tmp_path / "mounted"
        mounted_root.mkdir()
        hermes_home = mounted_root / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        doc_path = outside_dir / "report.pdf"
        doc_path.write_bytes(b"%PDF-1.4")

        mapped = map_napcat_file_path(
            str(doc_path),
            path_map=[{"host_prefix": str(mounted_root), "container_prefix": "/host-share"}],
            kind="File",
        )

        assert mapped.startswith("file:///host-share/.hermes/cache/napcat_media/")
        staged_file = hermes_home / "cache" / "napcat_media" / Path(mapped).name
        assert staged_file.exists()
        assert staged_file.read_bytes() == doc_path.read_bytes()

    def test_dm_quick_reply_does_not_thread_by_default(self):
        adapter = _make_adapter()
        now = 100.0
        adapter._remember_inbound_message(chat_type="dm", chat_id="10001", message_id="42", received_at=now)

        assert adapter._build_outbound_message(
            "hello",
            "42",
            target_type="dm",
            target_id="10001",
            now=now + 5,
        ) == "hello"

    def test_dm_slow_reply_uses_reply_segment(self):
        adapter = _make_adapter()
        now = 100.0
        adapter._remember_inbound_message(chat_type="dm", chat_id="10001", message_id="42", received_at=now)

        assert adapter._build_outbound_message(
            "hello",
            "42",
            target_type="dm",
            target_id="10001",
            now=now + 31,
        ) == [
            {"type": "reply", "data": {"id": "42"}},
            {"type": "text", "data": {"text": "hello"}},
        ]

    def test_send_reply_mode_off_ignores_reply_segment(self):
        adapter = _make_adapter(reply_to_mode="off")

        assert adapter._build_outbound_message(
            "hello",
            "42",
            target_type="dm",
            target_id="10001",
            now=150.0,
        ) == "hello"

    def test_send_can_opt_in_to_reply_segment_via_legacy_flag(self):
        adapter = _make_adapter(reply_to_messages=True)
        now = 100.0
        adapter._remember_inbound_message(chat_type="dm", chat_id="10001", message_id="42", received_at=now)

        assert adapter._build_outbound_message(
            "hello",
            "42",
            target_type="dm",
            target_id="10001",
            now=now + 31,
        ) == [
            {"type": "reply", "data": {"id": "42"}},
            {"type": "text", "data": {"text": "hello"}},
        ]

    def test_group_quick_reply_to_latest_message_does_not_thread(self):
        adapter = _make_adapter()
        now = 100.0
        adapter._remember_inbound_message(chat_type="group", chat_id="123456", message_id="42", received_at=now)

        assert adapter._build_outbound_message(
            "hello",
            "42",
            target_type="group",
            target_id="123456",
            now=now + 5,
        ) == "hello"

    def test_group_reply_threads_when_newer_message_arrived(self):
        adapter = _make_adapter()
        now = 100.0
        adapter._remember_inbound_message(chat_type="group", chat_id="123456", message_id="42", received_at=now)
        adapter._remember_inbound_message(chat_type="group", chat_id="123456", message_id="43", received_at=now + 2)

        assert adapter._build_outbound_message(
            "hello",
            "42",
            target_type="group",
            target_id="123456",
            now=now + 5,
        ) == [
            {"type": "reply", "data": {"id": "42"}},
            {"type": "text", "data": {"text": "hello"}},
        ]

    def test_group_slow_reply_threads_even_when_target_is_latest(self):
        adapter = _make_adapter()
        now = 100.0
        adapter._remember_inbound_message(chat_type="group", chat_id="123456", message_id="42", received_at=now)

        assert adapter._build_outbound_message(
            "hello",
            "42",
            target_type="group",
            target_id="123456",
            now=now + 11,
        ) == [
            {"type": "reply", "data": {"id": "42"}},
            {"type": "text", "data": {"text": "hello"}},
        ]

    def test_send_reply_mode_all_threads_every_chunk_when_reply_is_active(self):
        adapter = _make_adapter(reply_to_mode="all")
        now = 100.0
        adapter._remember_inbound_message(chat_type="group", chat_id="123456", message_id="42", received_at=now)

        assert adapter._build_outbound_message(
            "chunk1",
            "42",
            0,
            target_type="group",
            target_id="123456",
            now=now + 11,
        ) == [
            {"type": "reply", "data": {"id": "42"}},
            {"type": "text", "data": {"text": "chunk1"}},
        ]
        assert adapter._build_outbound_message(
            "chunk2",
            "42",
            1,
            target_type="group",
            target_id="123456",
            now=now + 11,
        ) == [
            {"type": "reply", "data": {"id": "42"}},
            {"type": "text", "data": {"text": "chunk2"}},
        ]

    @pytest.mark.asyncio
    async def test_send_long_message_tries_single_message_first(self):
        adapter = _make_adapter()
        adapter._call_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": "m1"}})

        content = "x" * 5000
        result = await adapter.send("10001", content)

        assert result.success is True
        adapter._call_action.assert_awaited_once()
        args = adapter._call_action.await_args.args
        assert args[0] == "send_private_msg"
        assert args[1]["message"] == content
        assert args[1]["user_id"] == "10001"

    @pytest.mark.asyncio
    async def test_send_chunks_long_message_proactively(self):
        adapter = _make_adapter(max_message_length=1000)
        adapter._call_action = AsyncMock(
            side_effect=[
                {"status": "ok", "retcode": 0, "data": {"message_id": "m1"}},
                {"status": "ok", "retcode": 0, "data": {"message_id": "m2"}},
            ]
        )

        content = "x" * 1500
        result = await adapter.send("10001", content)

        assert result.success is True
        assert adapter._call_action.await_count == 2
        first_call = adapter._call_action.await_args_list[0]
        second_call = adapter._call_action.await_args_list[1]
        assert first_call.args[1]["message"] != content
        assert second_call.args[1]["message"] != content
        assert result.message_id == "m2"

    @pytest.mark.asyncio
    async def test_send_falls_back_to_chunking_when_server_rejects_long_message(self):
        adapter = _make_adapter(max_message_length=2000)
        adapter._call_action = AsyncMock(
            side_effect=[
                RuntimeError("message is too long"),
                {"status": "ok", "retcode": 0, "data": {"message_id": "m1"}},
                {"status": "ok", "retcode": 0, "data": {"message_id": "m2"}},
            ]
        )

        content = "x" * 1500
        result = await adapter.send("10001", content)

        assert result.success is True
        assert adapter._call_action.await_count == 3
        first_call = adapter._call_action.await_args_list[0]
        second_call = adapter._call_action.await_args_list[1]
        third_call = adapter._call_action.await_args_list[2]
        assert first_call.args[1]["message"] == content
        assert second_call.args[1]["message"] != content
        assert third_call.args[1]["message"] != content
        assert result.message_id == "m2"

    @pytest.mark.asyncio
    async def test_send_timeout_returns_explicit_timeout_error(self):
        adapter = _make_adapter()
        adapter._call_action = AsyncMock(side_effect=asyncio.TimeoutError())

        result = await adapter.send("10001", "hello")

        assert result.success is False
        assert result.retryable is False
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_passes_trace_ctx_from_metadata(self):
        from gateway.napcat_observability.trace import NapcatTraceContext

        adapter = _make_adapter()
        adapter._call_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": "m1"}})
        trace_ctx = NapcatTraceContext(chat_type="private", chat_id="10001", user_id="10001", message_id="m-src")

        result = await adapter.send("10001", "hello", metadata={"_napcat_trace_ctx": trace_ctx})

        assert result.success is True
        assert adapter._call_action.await_args.kwargs["trace_ctx"] is trace_ctx

    @pytest.mark.asyncio
    async def test_send_uses_bound_trace_ctx_when_metadata_missing(self):
        from gateway.napcat_observability.trace import NapcatTraceContext, current_napcat_trace

        adapter = _make_adapter()
        adapter._call_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": "m1"}})
        trace_ctx = NapcatTraceContext(chat_type="private", chat_id="10001", user_id="10001", message_id="m-src")

        token = current_napcat_trace.set(trace_ctx)
        try:
            result = await adapter.send("10001", "hello")
        finally:
            current_napcat_trace.reset(token)

        assert result.success is True
        assert adapter._call_action.await_args.kwargs["trace_ctx"] is trace_ctx

    @pytest.mark.asyncio
    async def test_send_voice_private_uses_record_segment(self, tmp_path):
        adapter = _make_adapter()
        audio_path = tmp_path / "voice.ogg"
        audio_path.write_bytes(b"voice-bytes")
        adapter._call_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": "m1"}})

        result = await adapter.send_voice("10001", str(audio_path))

        assert result.success is True
        assert result.message_id == "m1"
        adapter._call_action.assert_awaited_once()
        call = adapter._call_action.await_args
        assert call.args[0] == "send_private_msg"
        assert call.args[1]["user_id"] == "10001"
        message = call.args[1]["message"]
        assert message[0]["type"] == "record"
        encoded = message[0]["data"]["file"]
        assert encoded.startswith("base64://")
        assert base64.b64decode(encoded.removeprefix("base64://")) == b"voice-bytes"

    @pytest.mark.asyncio
    async def test_send_voice_group_uses_group_action(self, tmp_path):
        adapter = _make_adapter()
        audio_path = tmp_path / "voice.mp3"
        audio_path.write_bytes(b"group-voice")
        adapter._call_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": "g1"}})

        result = await adapter.send_voice("group:123456", str(audio_path))

        assert result.success is True
        call = adapter._call_action.await_args
        assert call.args[0] == "send_group_msg"
        assert call.args[1]["group_id"] == "123456"
        assert call.args[1]["message"][0]["type"] == "record"

    @pytest.mark.asyncio
    async def test_send_voice_sends_caption_as_follow_up_text(self, tmp_path):
        adapter = _make_adapter()
        audio_path = tmp_path / "voice.wav"
        audio_path.write_bytes(b"captioned-voice")
        adapter._call_action = AsyncMock(
            side_effect=[
                {"status": "ok", "retcode": 0, "data": {"message_id": "v1"}},
                {"status": "ok", "retcode": 0, "data": {"message_id": "t1"}},
            ]
        )

        result = await adapter.send_voice("10001", str(audio_path), caption="Voice note")

        assert result.success is True
        assert result.message_id == "v1"
        assert adapter._call_action.await_count == 2
        voice_call = adapter._call_action.await_args_list[0]
        caption_call = adapter._call_action.await_args_list[1]
        assert voice_call.args[1]["message"][0]["type"] == "record"
        assert caption_call.args[1]["message"] == "Voice note"

    @pytest.mark.asyncio
    async def test_send_voice_returns_error_when_file_missing(self, tmp_path):
        adapter = _make_adapter()
        adapter._call_action = AsyncMock()

        result = await adapter.send_voice("10001", str(tmp_path / "missing.ogg"))

        assert result.success is False
        assert "Audio file not found" in str(result.error)
        adapter._call_action.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_document_private_uses_file_segment(self, tmp_path):
        adapter = _make_adapter()
        doc_path = tmp_path / "report.pdf"
        doc_path.write_bytes(b"%PDF-1.4")
        adapter._call_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": "d1"}})

        result = await adapter.send_document("10001", str(doc_path))

        assert result.success is True
        assert result.message_id == "d1"
        call = adapter._call_action.await_args
        assert call.args[0] == "send_private_msg"
        assert call.args[1]["user_id"] == "10001"
        message = call.args[1]["message"]
        assert message[0]["type"] == "file"
        assert message[0]["data"]["name"] == "report.pdf"
        assert message[0]["data"]["file"] == doc_path.resolve().as_uri()

    @pytest.mark.asyncio
    async def test_send_document_group_uses_group_action_and_custom_name(self, tmp_path):
        adapter = _make_adapter()
        doc_path = tmp_path / "raw.bin"
        doc_path.write_bytes(b"payload")
        adapter._call_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": "gdoc"}})

        result = await adapter.send_document("group:123456", str(doc_path), file_name="artifact.bin")

        assert result.success is True
        call = adapter._call_action.await_args
        assert call.args[0] == "send_group_msg"
        assert call.args[1]["group_id"] == "123456"
        assert call.args[1]["message"][0]["data"]["name"] == "artifact.bin"

    @pytest.mark.asyncio
    async def test_send_document_applies_container_path_mapping(self, tmp_path):
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        doc_path = shared_dir / "report.pdf"
        doc_path.write_bytes(b"%PDF-1.4")
        adapter = _make_adapter(
            file_path_map=[
                {
                    "host_prefix": str(shared_dir),
                    "container_prefix": "/host-share",
                }
            ]
        )
        adapter._call_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": "mapped"}})

        result = await adapter.send_document("10001", str(doc_path))

        assert result.success is True
        call = adapter._call_action.await_args
        assert call.args[1]["message"][0]["data"]["file"] == "file:///host-share/report.pdf"

    @pytest.mark.asyncio
    async def test_send_document_sends_caption_as_follow_up_text(self, tmp_path):
        adapter = _make_adapter()
        doc_path = tmp_path / "report.txt"
        doc_path.write_text("hello", encoding="utf-8")
        adapter._call_action = AsyncMock(
            side_effect=[
                {"status": "ok", "retcode": 0, "data": {"message_id": "doc1"}},
                {"status": "ok", "retcode": 0, "data": {"message_id": "text1"}},
            ]
        )

        result = await adapter.send_document("10001", str(doc_path), caption="File note")

        assert result.success is True
        assert result.message_id == "doc1"
        assert adapter._call_action.await_count == 2
        doc_call = adapter._call_action.await_args_list[0]
        caption_call = adapter._call_action.await_args_list[1]
        assert doc_call.args[1]["message"][0]["type"] == "file"
        assert caption_call.args[1]["message"] == "File note"

    @pytest.mark.asyncio
    async def test_send_document_returns_error_when_file_missing(self, tmp_path):
        adapter = _make_adapter()
        adapter._call_action = AsyncMock()

        result = await adapter.send_document("10001", str(tmp_path / "missing.pdf"))

        assert result.success is False
        assert "File not found" in str(result.error)
        adapter._call_action.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_image_file_private_uses_image_segment(self, tmp_path):
        adapter = _make_adapter()
        image_path = tmp_path / "chart.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        adapter._call_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": "img1"}})

        result = await adapter.send_image_file("10001", str(image_path), caption="Chart")

        assert result.success is True
        assert result.message_id == "img1"
        assert adapter._call_action.await_count == 2
        image_call = adapter._call_action.await_args_list[0]
        caption_call = adapter._call_action.await_args_list[1]
        assert image_call.args[0] == "send_private_msg"
        assert image_call.args[1]["message"][0]["type"] == "image"
        assert image_call.args[1]["message"][0]["data"]["file"] == str(image_path.resolve())
        assert caption_call.args[1]["message"] == "Chart"

    @pytest.mark.asyncio
    async def test_send_image_file_applies_container_path_mapping(self, tmp_path):
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        image_path = shared_dir / "chart.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        adapter = _make_adapter(
            file_path_map=[
                {
                    "host_prefix": str(shared_dir),
                    "container_prefix": "/host-share",
                }
            ]
        )
        adapter._call_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": "mapped-img"}})

        result = await adapter.send_image_file("10001", str(image_path))

        assert result.success is True
        call = adapter._call_action.await_args
        assert call.args[1]["message"][0]["data"]["file"] == "/host-share/chart.png"

    @pytest.mark.asyncio
    async def test_send_image_downloads_then_uses_image_file_send(self):
        adapter = _make_adapter()
        adapter.send_image_file = AsyncMock(return_value=SimpleNamespace(success=True, message_id="img1"))

        with patch("gateway.platforms.napcat.cache_image_from_url", AsyncMock(return_value="/tmp/cached.png")) as cache_mock:
            result = await adapter.send_image(
                "10001",
                "https://ark-acg-cn-beijing.tos-cn-beijing.volces.com/demo/output.png?token=abc",
            )

        assert result.success is True
        assert result.message_id == "img1"
        cache_mock.assert_awaited_once()
        adapter.send_image_file.assert_awaited_once_with(
            chat_id="10001",
            image_path="/tmp/cached.png",
            caption=None,
            reply_to=None,
            metadata=None,
        )

    @pytest.mark.asyncio
    async def test_send_image_sends_caption_via_image_file_send(self):
        adapter = _make_adapter()
        adapter.send_image_file = AsyncMock(return_value=SimpleNamespace(success=True, message_id="img1"))

        with patch("gateway.platforms.napcat.cache_image_from_url", AsyncMock(return_value="/tmp/generated.png")):
            result = await adapter.send_image(
                "10001",
                "https://example.com/generated.png",
                caption="More vibrant character in a darker scene",
            )

        assert result.success is True
        assert result.message_id == "img1"
        adapter.send_image_file.assert_awaited_once_with(
            chat_id="10001",
            image_path="/tmp/generated.png",
            caption="More vibrant character in a darker scene",
            reply_to=None,
            metadata=None,
        )


class TestNapCatTriggers:
    @pytest.mark.asyncio
    async def test_group_trigger_rejected_when_group_chat_switch_is_off(self):
        from gateway.platforms.napcat import NapCatAdapter

        adapter = NapCatAdapter(
            PlatformConfig(enabled=True, token="tok", extra={"ws_url": "ws://127.0.0.1:3001/"})
        )

        result = await adapter._evaluate_group_trigger(
            group_id="123456",
            user_id="10001",
            raw_text="@42 hello",
            cleaned_text="hello",
            segments=[{"type": "at", "data": {"qq": "42"}}, {"type": "text", "data": {"text": " hello"}}],
            reply_sender_id=None,
            sender_name="Alice",
            message_id="m0",
        )

        assert result.accept is False
        assert result.reason == "group_chat_disabled"

    @pytest.mark.asyncio
    async def test_group_trigger_accepts_when_group_chat_switch_lists_target_group(self):
        adapter = _make_adapter(group_chat_enabled=["123456"], group_trigger_rules={"123456": {}})

        result = await adapter._evaluate_group_trigger(
            group_id="123456",
            user_id="10001",
            raw_text="Just casual chat",
            cleaned_text="Just casual chat",
            segments=[{"type": "text", "data": {"text": "Just casual chat"}}],
            reply_sender_id=None,
            sender_name="Alice",
            message_id="m-listed",
        )

        assert result.accept is True
        assert result.reason == "group_message"

    @pytest.mark.asyncio
    async def test_group_mention_trigger(self):
        adapter = _make_adapter(group_trigger_rules={"123456": {}})
        adapter._bot_user_id = "42"

        result = await adapter._evaluate_group_trigger(
            group_id="123456",
            user_id="10001",
            raw_text="@42 hello",
            cleaned_text="hello",
            segments=[{"type": "at", "data": {"qq": "42"}}, {"type": "text", "data": {"text": " hello"}}],
            reply_sender_id=None,
            sender_name="Alice",
            message_id="m1",
        )

        assert result.accept is True
        assert result.reason == "mention"
        assert result.cleaned_text == "hello"

    @pytest.mark.asyncio
    async def test_group_reply_to_bot_trigger(self):
        adapter = _make_adapter(group_trigger_rules={"123456": {}})
        adapter._bot_user_id = "42"

        result = await adapter._evaluate_group_trigger(
            group_id="123456",
            user_id="10001",
            raw_text="replying",
            cleaned_text="replying",
            segments=[{"type": "text", "data": {"text": "replying"}}],
            reply_sender_id="42",
            sender_name="Alice",
            message_id="m2",
        )

        assert result.accept is True
        assert result.reason == "reply_to_bot"

    @pytest.mark.asyncio
    async def test_group_gateway_command_trigger_bypasses_require_at(self):
        adapter = _make_adapter(group_trigger_rules={"123456": {"require_at": True}})
        adapter._bot_user_id = "42"

        result = await adapter._evaluate_group_trigger(
            group_id="123456",
            user_id="10001",
            raw_text="/help",
            cleaned_text="/help",
            segments=[{"type": "text", "data": {"text": "/help"}}],
            reply_sender_id=None,
            sender_name="Alice",
            message_id="m-help",
        )

        assert result.accept is True
        assert result.reason == "gateway_command"
        assert result.cleaned_text == "/help"

    @pytest.mark.asyncio
    async def test_group_non_explicit_message_passes_through_for_orchestrator(self):
        adapter = _make_adapter(group_trigger_rules={"123456": {}})

        result = await adapter._evaluate_group_trigger(
            group_id="123456",
            user_id="10001",
            raw_text="Just casual chat",
            cleaned_text="Just casual chat",
            segments=[{"type": "text", "data": {"text": "Just casual chat"}}],
            reply_sender_id=None,
            sender_name="Alice",
            message_id="m3",
        )

        assert result.accept is True
        assert result.reason == "group_message"
        assert result.cleaned_text == "Just casual chat"


class TestNapCatPrepareGatewayTurn:
    @pytest.mark.asyncio
    async def test_prepare_gateway_turn_applies_skill_policy_and_prompts(self, monkeypatch):
        adapter = _make_adapter(
            default_system_prompt="Platform default",
            extra_system_prompt="Platform extra",
            group_policies={
                "123456": {
                    "default_system_prompt": "Group default",
                    "extra_system_prompt": "Group extra",
                    "allow_skills": ["Skill A"],
                }
            },
            user_policies={"10001": {"deny_skills": ["Skill B"]}},
        )
        monkeypatch.setattr(adapter, "_all_skill_names", lambda: {"Skill A", "Skill B", "Skill C"})

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="hello", source=source)
        event.metadata = {"napcat_trigger_reason": "mention"}

        session_entry = SessionEntry(
            session_key="k1",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            display_name="Test Group",
            platform=Platform.NAPCAT,
            chat_type="group",
        )
        fake_store = SimpleNamespace()

        result = await adapter.prepare_gateway_turn(
            event=event,
            session_store=fake_store,
            session_entry=session_entry,
        )

        assert "natural participant in a QQ group" in result["extra_prompt"]
        assert "Do not mention session boundaries" in result["extra_prompt"]
        assert "Avoid checklist-style recaps and menu-like pivots" in result["extra_prompt"]
        assert "about 18 degrees" in result["extra_prompt"]
        assert "Group default" in result["extra_prompt"]
        assert "Platform extra" in result["extra_prompt"]
        assert "Group extra" in result["extra_prompt"]
        assert "Use memory(target='user') only for stable facts about the current speaker" in result["extra_prompt"]
        assert "Group-level facts belong in target='chat'" in result["extra_prompt"]
        assert "<persona>" not in result["user_context"]
        assert "<persona>" in result["controller_context"]
        assert "Group default" in result["controller_context"]
        assert "Platform extra" in result["controller_context"]
        assert "Group extra" in result["controller_context"]
        assert "user deny=Skill B" not in result["extra_prompt"]
        assert "user deny=Skill B" in result["user_context"]
        assert "This turn is from NapCat / QQ" in result["routing_prompt"]
        assert sorted(result["dynamic_disabled_skills"]) == ["Skill B", "Skill C"]
        assert result["dynamic_disabled_toolsets"] == []
        assert result["super_admin"] is False

    @pytest.mark.asyncio
    async def test_handle_passive_event_does_not_reinject_internal_reply_text(self):
        adapter = _make_adapter()
        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(
            text="那现在继续",
            source=source,
            reply_to_message_id="reply-1",
            reply_to_text='⚡ Interrupting current task (1 min elapsed, iteration 2/90). '
                         "I'll respond to your message shortly.",
        )
        session_entry = SessionEntry(
            session_key="k-passive",
            session_id="s-passive",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            display_name="Test Group",
            platform=Platform.NAPCAT,
            chat_type="group",
        )
        fake_store = SimpleNamespace(
            config=GatewayConfig(),
            get_or_create_session=MagicMock(return_value=session_entry),
            load_transcript=MagicMock(return_value=[]),
            append_to_transcript=MagicMock(),
        )

        handled = await adapter.handle_passive_event(event, fake_store)

        assert handled is True
        fake_store.append_to_transcript.assert_called_once()
        persisted = fake_store.append_to_transcript.call_args.args[1]["content"]
        assert "那现在继续" in persisted
        assert "Interrupting current task" not in persisted
        assert "[Replying to:" not in persisted

    @pytest.mark.asyncio
    async def test_resolve_reply_context_suppresses_internal_control_messages(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "bot-1"
        adapter.remember_internal_control_message("reply-42", "⚡ Interrupting current task...")
        adapter._call_action = AsyncMock()

        text, sender_id, media_urls, media_types = await adapter._resolve_reply_context("reply-42")

        assert text is None
        assert sender_id == "bot-1"
        assert media_urls == []
        assert media_types == []
        adapter._call_action.assert_not_awaited()

    def test_group_session_model_override_prefers_group_policy_over_user_policy(self):
        adapter = _make_adapter(
            group_policies={
                "123456": {
                    "model_override": {
                        "model": "group-model",
                        "provider": "openrouter",
                    },
                    "api_mode": "responses",
                }
            },
            user_policies={
                "10001": {
                    "model": "user-model",
                    "base_url": "https://example.com/v1",
                }
            },
        )

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )

        result = adapter.get_session_model_override(source)

        assert result == {
            "model": "group-model",
            "provider": "openrouter",
            "base_url": "https://example.com/v1",
            "api_mode": "responses",
        }

    def test_dm_session_model_override_uses_user_policy(self):
        adapter = _make_adapter(
            user_policies={
                "10001": {
                    "model": "dm-model",
                    "provider": "openai",
                    "api_mode": "chat_completions",
                }
            }
        )

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="10001",
            chat_type="dm",
            user_id="10001",
            user_name="Alice",
        )

        result = adapter.get_session_model_override(source)

        assert result == {
            "model": "dm-model",
            "provider": "openai",
            "api_mode": "chat_completions",
        }

    def test_platform_public_prompt_dm_uses_same_human_chat_rules(self):
        adapter = _make_adapter()
        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="10001",
            chat_type="dm",
            user_id="10001",
            user_name="Alice",
        )

        prompt = adapter._platform_public_prompt(source)

        assert "private chat" in prompt
        assert "Reply like a natural person in QQ chat" in prompt
        assert "Do not mention session boundaries" in prompt
        assert "Avoid checklist-style recaps and menu-like pivots" in prompt
        assert "about 18 degrees" in prompt
        assert "refuse briefly and do not run tools or commands to inspect them" in prompt
        assert "natural participant in a QQ group" not in prompt

    @pytest.mark.asyncio
    async def test_prepare_gateway_turn_includes_recent_group_context(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setattr(adapter, "_all_skill_names", lambda: set())

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="hello", source=source)
        event.metadata = {
            "napcat_trigger_reason": "group_message",
            "napcat_recent_group_context": "Alice: First msg\nBob: Second msg",
        }

        session_entry = SessionEntry(
            session_key="k1",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            display_name="Test Group",
            platform=Platform.NAPCAT,
            chat_type="group",
        )
        fake_store = SimpleNamespace()

        result = await adapter.prepare_gateway_turn(
            event=event,
            session_store=fake_store,
            session_entry=session_entry,
        )

        assert result["controller_context"] == "<persona></persona>"
        assert "Alice: First msg" not in result["extra_prompt"]
        assert "Bob: Second msg" not in result["extra_prompt"]
        assert "Alice: First msg" in result["user_context"]
        assert "Bob: Second msg" in result["user_context"]
        assert result["dynamic_disabled_toolsets"] == []

    @pytest.mark.asyncio
    async def test_prepare_gateway_turn_emits_empty_orchestrator_persona_block_without_persona_content(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setattr(adapter, "_all_skill_names", lambda: set())

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="hello", source=source)
        event.metadata = {"napcat_trigger_reason": "mention"}

        session_entry = SessionEntry(
            session_key="k1",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            display_name="Test Group",
            platform=Platform.NAPCAT,
            chat_type="group",
        )

        result = await adapter.prepare_gateway_turn(
            event=event,
            session_store=SimpleNamespace(),
            session_entry=session_entry,
        )

        assert result["user_context"] == ""
        assert result["controller_context"] == "<persona></persona>"

    @pytest.mark.asyncio
    async def test_prepare_gateway_turn_moves_group_user_prompt_overlays_to_user_context(self, monkeypatch):
        adapter = _make_adapter(
            default_system_prompt="Platform default",
            user_policies={
                "10001": {
                    "default_system_prompt": "User default",
                    "extra_system_prompt": "User extra",
                }
            },
        )
        monkeypatch.setattr(adapter, "_all_skill_names", lambda: set())

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="hello", source=source)
        event.metadata = {"napcat_trigger_reason": "mention"}

        session_entry = SessionEntry(
            session_key="k1",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            display_name="Test Group",
            platform=Platform.NAPCAT,
            chat_type="group",
        )
        fake_store = SimpleNamespace()

        result = await adapter.prepare_gateway_turn(
            event=event,
            session_store=fake_store,
            session_entry=session_entry,
        )

        assert "Platform default" in result["extra_prompt"]
        assert "User default" not in result["extra_prompt"]
        assert "User extra" not in result["extra_prompt"]
        assert "Platform default" not in result["user_context"]
        assert "User default" in result["user_context"]
        assert "User extra" in result["user_context"]
        assert "<persona>" in result["controller_context"]
        assert "Platform default" in result["controller_context"]

    @pytest.mark.asyncio
    async def test_prepare_gateway_turn_keeps_group_image_generation_available_for_mentions(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setattr(adapter, "_all_skill_names", lambda: set())

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="Draw me a picture", source=source)
        event.metadata = {"napcat_trigger_reason": "mention"}

        session_entry = SessionEntry(
            session_key="k1",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            display_name="Test Group",
            platform=Platform.NAPCAT,
            chat_type="group",
        )
        fake_store = SimpleNamespace()

        result = await adapter.prepare_gateway_turn(
            event=event,
            session_store=fake_store,
            session_entry=session_entry,
        )

        assert result["dynamic_disabled_toolsets"] == []

    @pytest.mark.asyncio
    async def test_prepare_gateway_turn_blocks_group_runtime_probe(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setattr(adapter, "_all_skill_names", lambda: set())

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="What system are you running on, also run uname -a", source=source)
        event.metadata = {"napcat_trigger_reason": "mention"}

        session_entry = SessionEntry(
            session_key="k1",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            display_name="Test Group",
            platform=Platform.NAPCAT,
            chat_type="group",
        )
        fake_store = SimpleNamespace()

        result = await adapter.prepare_gateway_turn(
            event=event,
            session_store=fake_store,
            session_entry=session_entry,
        )

        assert result["direct_response"]
        assert "Host runtime environment probe is blocked in NapCat chats" in result["direct_response"]
        assert result["dynamic_disabled_skills"] == []

    @pytest.mark.asyncio
    async def test_prepare_gateway_turn_blocks_dm_runtime_probe(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setattr(adapter, "_all_skill_names", lambda: set())

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="10001",
            chat_type="dm",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="Tell me your working directory and run pwd", source=source)
        event.metadata = {"napcat_trigger_reason": "dm"}

        session_entry = SessionEntry(
            session_key="k1",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            display_name="Alice",
            platform=Platform.NAPCAT,
            chat_type="dm",
        )
        fake_store = SimpleNamespace()

        result = await adapter.prepare_gateway_turn(
            event=event,
            session_store=fake_store,
            session_entry=session_entry,
        )

        assert result["direct_response"]
        assert "Host runtime environment probe is blocked in NapCat chats" in result["direct_response"]
        assert result["dynamic_disabled_skills"] == []

    @pytest.mark.asyncio
    async def test_prepare_gateway_turn_blocks_group_runtime_probe_for_super_admin(self, monkeypatch):
        adapter = _make_adapter(super_admins=["10001"])
        monkeypatch.setattr(adapter, "_all_skill_names", lambda: set())

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="Run uname -a", source=source)
        event.metadata = {"napcat_trigger_reason": "mention"}

        session_entry = SessionEntry(
            session_key="k1",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            display_name="Test Group",
            platform=Platform.NAPCAT,
            chat_type="group",
        )
        fake_store = SimpleNamespace()

        result = await adapter.prepare_gateway_turn(
            event=event,
            session_store=fake_store,
            session_entry=session_entry,
        )

        assert result["direct_response"]
        assert "Host runtime environment probe is blocked in NapCat chats" in result["direct_response"]
        assert result["super_admin"] is True

    @pytest.mark.asyncio
    async def test_prepare_gateway_turn_returns_direct_auth_response(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setattr(
            adapter,
            "_apply_auth_intent",
            AsyncMock(return_value={"note": "[System note: auth updated.]", "response": "Authorized."}),
        )
        monkeypatch.setattr(adapter, "_build_cross_session_context_block", AsyncMock(return_value=""))

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="10001",
            chat_type="dm",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="Authorize reading test group context", source=source)
        event.metadata = {}

        session_entry = SessionEntry(
            session_key="k1",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            display_name="Alice",
            platform=Platform.NAPCAT,
            chat_type="dm",
        )
        fake_store = SimpleNamespace()

        result = await adapter.prepare_gateway_turn(
            event=event,
            session_store=fake_store,
            session_entry=session_entry,
        )

        assert result["message_prefix"] == "[System note: auth updated.]"
        assert result["direct_response"] == "Authorized."


class TestNapCatSharedSpeakerInjection:
    @pytest.mark.asyncio
    async def test_shared_group_turn_prefixes_current_speaker(self):
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={
                Platform.NAPCAT: PlatformConfig(enabled=True, token="tok", extra={}),
            },
            stt_enabled=True,
        )
        runner.adapters = {}
        runner._model = "test-model"
        runner._base_url = ""
        runner._has_setup_skill = lambda: False

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="100000001",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="Hello", source=source)

        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

        assert result == "[Alice] Hello"

    @pytest.mark.asyncio
    async def test_per_user_group_opt_out_skips_speaker_prefix(self):
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={
                Platform.NAPCAT: PlatformConfig(
                    enabled=True,
                    token="tok",
                    extra={"group_sessions_per_user": True},
                ),
            },
            stt_enabled=True,
        )
        runner.adapters = {}
        runner._model = "test-model"
        runner._base_url = ""
        runner._has_setup_skill = lambda: False

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="100000001",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="Hello", source=source)

        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

        assert result == "Hello"


class TestNapCatSuperAdminDebug:
    def test_super_admin_debug_contains_route_and_tools(self):
        adapter = _make_adapter(super_admins=["10001"])
        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_type="group",
            user_id="10001",
        )
        event = MessageEvent(text="hello", source=source)
        event.metadata = {"napcat_trigger_reason": "keyword"}

        result = adapter.format_super_admin_debug(
            event=event,
            agent_result={
                "model": "openai/gpt-5",
                "messages": [
                    {"tool_calls": [{"function": {"name": "memory"}}]},
                    {"tool_calls": [{"function": {"name": "skill_view"}}]},
                ],
            },
            dynamic_disabled_skills=["Skill C"],
        )

        assert "NapCat Debug" in result
        assert "openai/gpt-5" in result
        assert "memory" in result
        assert "skill_view" in result


class TestModelPromotionConfig:
    """Tests for Task 01 — promotion config parsing and normalization."""

    def test_default_config_has_promotion_disabled(self):
        adapter = _make_adapter()
        assert adapter._model_promotion["enabled"] is False

    def test_missing_model_promotion_key_is_disabled(self):
        adapter = _make_adapter()
        # No model_promotion key in extra → disabled
        assert adapter._model_promotion["enabled"] is False
        assert adapter._model_promotion["apply_to_groups"] is True
        assert adapter._model_promotion["apply_to_dms"] is False

    def test_enabled_but_missing_l1_model_disables_promotion(self):
        adapter = _make_adapter(
            model_promotion={
                "enabled": True,
                "l1": {"provider": "openrouter"},
                "l2": {"model": "claude-opus", "provider": "anthropic"},
            }
        )
        assert adapter._model_promotion["enabled"] is False

    def test_enabled_but_missing_l2_model_disables_promotion(self):
        adapter = _make_adapter(
            model_promotion={
                "enabled": True,
                "l1": {"model": "qwen-plus", "provider": "openrouter"},
                "l2": {"provider": "anthropic"},
            }
        )
        assert adapter._model_promotion["enabled"] is False

    def test_full_config_parsed_correctly(self):
        adapter = _make_adapter(
            model_promotion={
                "enabled": True,
                "apply_to_groups": True,
                "apply_to_dms": True,
                "l1": {
                    "model": "qwen-plus",
                    "provider": "openrouter",
                    "api_key": "sk-test",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_mode": "chat_completions",
                },
                "l2": {
                    "model": "claude-opus-4.1",
                    "provider": "anthropic",
                    "api_key": "sk-ant-test",
                },
                "protocol": {
                    "no_reply_marker": "[[SKIP]]",
                    "escalate_marker": "[[UPGRADE]]",
                },
                "safeguards": {
                    "l1_max_tool_calls": 5,
                    "l1_max_api_calls": 10,
                    "force_l2_toolsets": ["terminal", "browser"],
                    "l2_complexity_score_threshold": 80,
                },
            }
        )
        cfg = adapter._model_promotion
        assert cfg["enabled"] is True
        assert cfg["apply_to_groups"] is True
        assert cfg["apply_to_dms"] is True
        assert cfg["l1"]["model"] == "qwen-plus"
        assert cfg["l2"]["model"] == "claude-opus-4.1"
        assert cfg["protocol"]["no_reply_marker"] == "[[SKIP]]"
        assert cfg["protocol"]["escalate_marker"] == "[[UPGRADE]]"
        assert cfg["safeguards"]["l1_max_tool_calls"] == 5
        assert cfg["safeguards"]["l1_max_api_calls"] == 10
        assert "terminal" in cfg["safeguards"]["force_l2_toolsets"]
        assert cfg["safeguards"]["l2_complexity_score_threshold"] == 80

    @pytest.mark.asyncio
    async def test_prepare_gateway_turn_emits_promotion_metadata(self):
        long_default = "D" * 260
        long_extra = "E" * 520
        adapter = _make_adapter(
            default_system_prompt=long_default,
            extra_system_prompt=long_extra,
            model_promotion={
                "enabled": True,
                "apply_to_groups": True,
                "l1": {"model": "qwen-plus", "provider": "openrouter", "api_key": "sk-test"},
                "l2": {"model": "claude-opus", "provider": "anthropic", "api_key": "sk-ant"},
            }
        )

        source = SessionSource(
            platform=Platform.NAPCAT,
            chat_id="123456",
            chat_type="group",
            user_id="10001",
            user_name="Alice",
        )
        event = MessageEvent(text="hello", source=source)
        event.metadata = {"napcat_trigger_reason": "mention"}
        session_entry = SessionEntry(
            session_key="k1",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            display_name="Test Group",
            platform=Platform.NAPCAT,
            chat_type="group",
        )

        with patch("gateway.platforms.napcat.emit_napcat_event") as emit_mock:
            await adapter.prepare_gateway_turn(
                event=event,
                session_store=SimpleNamespace(),
                session_entry=session_entry,
            )

        payload = emit_mock.call_args.args[1]
        assert payload["promotion_enabled"] is True
        assert payload["promotion_stage"] == "l1"
        assert payload["promotion_l1_model"] == "qwen-plus"
        assert payload["promotion_l2_model"] == "claude-opus"
        assert payload["default_prompt"] == long_default
        assert "<persona>" in payload["controller_context"]
        assert long_extra in payload["controller_context"]
        assert payload["promotion_protocol_enabled"] is True
        assert "Stay on L1 by default" in payload["promotion_protocol_prompt"]

    def test_default_protocol_and_safeguards(self):
        adapter = _make_adapter(
            model_promotion={
                "enabled": True,
                "l1": {"model": "qwen-plus", "provider": "openrouter"},
                "l2": {"model": "claude-opus", "provider": "anthropic"},
            }
        )
        cfg = adapter._model_promotion
        assert cfg["protocol"]["no_reply_marker"] == "[[NO_REPLY]]"
        assert cfg["protocol"]["escalate_marker"] == "[[ESCALATE_L2]]"
        assert cfg["safeguards"]["l1_max_tool_calls"] == 5
        assert cfg["safeguards"]["l1_max_api_calls"] == 8
        assert cfg["safeguards"]["force_l2_toolsets"] == []
        assert cfg["safeguards"]["l2_complexity_score_threshold"] == 70


class TestNapCatHandoffHeuristics:
    """Boundary tests for orchestrator handoff heuristics.

    These tests document expected positive/negative cases so that future
    keyword additions do not silently regress behaviour.
    """

    @pytest.fixture(scope="class")
    def support(self):
        from gateway.napcat_orchestrator_support import NapCatOrchestratorSupport

        return NapCatOrchestratorSupport

    # -- requires_external_info_handoff ----------------------------------

    @pytest.mark.parametrize(
        "message",
        [
            "今天天气怎么样",
            "明天会下雨吗",
            "查一下气温",
            "看看最新的新闻",
            "今天汇率多少",
            "查一下比特币价格",
            "搜一下股价",
            "今天航班",
            "look up the weather",
            "current temperature",
            "latest news headlines",
            "btc price now",
        ],
    )
    def test_requires_external_info_handoff_positive(self, support, message):
        assert support.requires_external_info_handoff(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "你好",
            "今天真开心",
            "最近好吗",
            "帮我写代码",
            "灯开着吗",
            "hello world",
            "price",  # no time/search marker
            "news",  # no time/search marker
            "股票",  # no time/search marker
            "油价",  # no time/search marker
        ],
    )
    def test_requires_external_info_handoff_negative(self, support, message):
        assert support.requires_external_info_handoff(message) is False

    # -- requires_device_state_handoff -----------------------------------

    @pytest.mark.parametrize(
        "message",
        [
            # specific device + state query
            "台灯开着吗",
            "空调温度多少",
            "灯亮着吗",
            "除湿机运行状态",
            "插座功率多少",
            "摄像头在线吗",
            "空气检测仪读数",
            "check lamp status",
            "is the ac running",
            "humidifier power reading",
            # home context + generic device + query intent
            "家里设备状态",
            "看看全屋传感器",
            "帮我查一下家庭设备",
            "客厅灯开着吗",
            "卧室设备详情",
        ],
    )
    def test_requires_device_state_handoff_positive(self, support, message):
        assert support.requires_device_state_handoff(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            # casual chat about devices (not state queries)
            "灯好看吗",
            "这个空调什么牌子",
            "我喜欢家里的灯",
            "窗帘颜色不错",
            "扫地机器人好用吗",
            "加湿器买了吗",
            # generic device without home context or state query
            "设备",
            "传感器",
            "lamp",
            # home context without device
            "家里挺好的",
            "客厅很大",
            # action commands (not state queries)
            "打开空调",
            "关灯",
            "把灯打开",
            # unrelated
            "今天天气怎么样",
            "hello",
        ],
    )
    def test_requires_device_state_handoff_negative(self, support, message):
        assert support.requires_device_state_handoff(message) is False
