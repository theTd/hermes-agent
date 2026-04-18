import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from gateway.config import Platform
from gateway.session_context import clear_session_vars, set_session_vars
from hermes_cli.config import get_config_path
from model_tools import get_tool_definitions
from tools.napcat_admin_tool import _handle_napcat_manage_settings


def _run_async(coro):
    return asyncio.run(coro)


def _make_gateway_config():
    napcat_cfg = SimpleNamespace(enabled=True, token="tok", extra={"ws_url": "ws://127.0.0.1:3001/"})
    return SimpleNamespace(platforms={Platform.NAPCAT: napcat_cfg})


def _write_config(data: dict):
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _read_config() -> dict:
    return yaml.safe_load(Path(get_config_path()).read_text(encoding="utf-8")) or {}


def _base_user_config() -> dict:
    return {
        "platforms": {
            "napcat": {
                "enabled": True,
                "token": "tok",
                "extra": {
                    "ws_url": "ws://127.0.0.1:3001/",
                    "super_admins": ["100000002"],
                },
            }
        }
    }


class TestNapCatAdminTool:
    def test_tool_definition_only_appears_in_napcat_session(self):
        config = _make_gateway_config()

        with patch("tools.napcat_admin_tool.load_gateway_config", return_value=config), patch(
            "tools.napcat_history_tool.load_gateway_config",
            return_value=config,
        ):
            napcat_tokens = set_session_vars(platform="napcat")
            try:
                napcat_defs = get_tool_definitions(enabled_toolsets=["hermes-napcat"], quiet_mode=True)
            finally:
                clear_session_vars(napcat_tokens)

            telegram_tokens = set_session_vars(platform="telegram")
            try:
                telegram_defs = get_tool_definitions(enabled_toolsets=["hermes-napcat"], quiet_mode=True)
            finally:
                clear_session_vars(telegram_tokens)

        napcat_names = {item["function"]["name"] for item in napcat_defs}
        telegram_names = {item["function"]["name"] for item in telegram_defs}

        assert "napcat_manage_settings" in napcat_names
        assert "napcat_manage_settings" not in telegram_names

    def test_inspect_sanitizes_group_policy_for_non_admin(self):
        config = _make_gateway_config()
        user_config = _base_user_config()
        user_config["platforms"]["napcat"]["extra"]["group_policies"] = {
            "100000001": {
                "default_system_prompt": "Group prompt",
                "extra_system_prompt": "Extra style",
                "allow_skills": ["imessage"],
                "model_override": {
                    "model": "openai/gpt-5-mini",
                    "provider": "openrouter",
                    "api_key": "secret-key",
                    "base_url": "https://secret.example",
                },
            }
        }
        _write_config(user_config)

        tokens = set_session_vars(
            platform="napcat",
            chat_id="100000001",
            chat_type="group",
            user_id="10001",
            user_name="visitor",
        )
        try:
            with patch("tools.napcat_admin_tool.load_gateway_config", return_value=config):
                result = json.loads(_run_async(_handle_napcat_manage_settings({"action": "inspect"})))
        finally:
            clear_session_vars(tokens)

        assert result["success"] is True
        assert result["current_user_is_super_admin"] is False
        assert "super_admins" not in result
        assert result["current_group_policy"]["default_system_prompt"] == "Group prompt"
        assert result["current_group_policy"]["model_override"] == {
            "model": "openai/gpt-5-mini",
            "provider": "openrouter",
        }
        serialized = json.dumps(result["current_group_policy"], ensure_ascii=False)
        assert "secret-key" not in serialized
        assert "secret.example" not in serialized

    def test_add_super_admin_writes_config(self):
        config = _make_gateway_config()
        _write_config(_base_user_config())

        tokens = set_session_vars(platform="napcat", user_id="100000002", user_name="root")
        try:
            with patch("tools.napcat_admin_tool.load_gateway_config", return_value=config):
                result = json.loads(
                    _run_async(
                        _handle_napcat_manage_settings(
                            {"action": "add_super_admin", "target_user_id": "12345678"}
                        )
                    )
                )
        finally:
            clear_session_vars(tokens)

        saved = _read_config()
        admins = saved["platforms"]["napcat"]["extra"]["super_admins"]

        assert result["success"] is True
        assert result["changed"] is True
        assert result["restart_required"] is True
        assert admins == ["100000002", "12345678"]

    def test_remove_last_super_admin_is_rejected(self):
        config = _make_gateway_config()
        _write_config(_base_user_config())

        tokens = set_session_vars(platform="napcat", user_id="100000002")
        try:
            with patch("tools.napcat_admin_tool.load_gateway_config", return_value=config):
                result = json.loads(
                    _run_async(
                        _handle_napcat_manage_settings(
                            {"action": "remove_super_admin", "target_user_id": "100000002"}
                        )
                    )
                )
        finally:
            clear_session_vars(tokens)

        assert result["success"] is False
        assert "last NapCat super admin" in result["error"]

    def test_non_admin_mutation_is_denied(self):
        config = _make_gateway_config()
        _write_config(_base_user_config())

        tokens = set_session_vars(
            platform="napcat",
            chat_id="100000001",
            chat_type="group",
            user_id="10001",
        )
        try:
            with patch("tools.napcat_admin_tool.load_gateway_config", return_value=config):
                result = json.loads(
                    _run_async(
                        _handle_napcat_manage_settings(
                            {"action": "set_group_trigger_value", "setting": "require_at", "value": False}
                        )
                    )
                )
        finally:
            clear_session_vars(tokens)

        assert result["success"] is False
        assert result["code"] == "permission_denied"

    def test_set_group_personality_writes_group_policy(self):
        config = _make_gateway_config()
        _write_config(_base_user_config())

        tokens = set_session_vars(
            platform="napcat",
            chat_id="100000001",
            chat_type="group",
            user_id="100000002",
        )
        try:
            with patch("tools.napcat_admin_tool.load_gateway_config", return_value=config):
                result = json.loads(
                    _run_async(
                        _handle_napcat_manage_settings(
                            {
                                "action": "set_group_personality",
                                "personality_kind": "default",
                                "text": "Be more direct in this group.",
                            }
                        )
                    )
                )
        finally:
            clear_session_vars(tokens)

        saved = _read_config()
        policy = saved["platforms"]["napcat"]["extra"]["group_policies"]["100000001"]

        assert result["success"] is True
        assert policy["default_system_prompt"] == "Be more direct in this group."

    def test_group_trigger_and_list_updates_write_expected_paths(self):
        config = _make_gateway_config()
        _write_config(_base_user_config())

        tokens = set_session_vars(
            platform="napcat",
            chat_id="100000001",
            chat_type="group",
            user_id="100000002",
        )
        try:
            with patch("tools.napcat_admin_tool.load_gateway_config", return_value=config):
                trigger_result = json.loads(
                    _run_async(
                        _handle_napcat_manage_settings(
                            {"action": "set_group_trigger_value", "setting": "require_at", "value": False}
                        )
                    )
                )
                list_result = json.loads(
                    _run_async(
                        _handle_napcat_manage_settings(
                            {
                                "action": "replace_group_list_values",
                                "setting": "allow_skills",
                                "values": ["imessage", "google-workspace"],
                            }
                        )
                    )
                )
        finally:
            clear_session_vars(tokens)

        saved = _read_config()
        extra = saved["platforms"]["napcat"]["extra"]

        assert trigger_result["success"] is True
        assert extra["group_trigger_rules"]["100000001"]["require_at"] is False
        assert list_result["success"] is True
        assert extra["group_policies"]["100000001"]["allow_skills"] == ["imessage", "google-workspace"]

    def test_set_group_model_override_writes_nested_override(self):
        config = _make_gateway_config()
        _write_config(_base_user_config())

        tokens = set_session_vars(
            platform="napcat",
            chat_id="100000001",
            chat_type="group",
            user_id="100000002",
        )
        try:
            with patch("tools.napcat_admin_tool.load_gateway_config", return_value=config):
                result = json.loads(
                    _run_async(
                        _handle_napcat_manage_settings(
                            {
                                "action": "set_group_model_override",
                                "model": "openai/gpt-5-mini",
                                "provider": "openrouter",
                                "api_mode": "responses",
                            }
                        )
                    )
                )
        finally:
            clear_session_vars(tokens)

        saved = _read_config()
        override = saved["platforms"]["napcat"]["extra"]["group_policies"]["100000001"]["model_override"]

        assert result["success"] is True
        assert override == {
            "model": "openai/gpt-5-mini",
            "provider": "openrouter",
            "api_mode": "responses",
        }
