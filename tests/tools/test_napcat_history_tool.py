import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import Platform
from gateway.session_context import clear_session_vars, set_session_vars
from model_tools import get_tool_definitions
from tools.napcat_history_tool import _napcat_read_history


def _run_async(coro):
    import asyncio

    return asyncio.run(coro)


def _make_config():
    napcat_cfg = SimpleNamespace(enabled=True, token="tok", extra={"ws_url": "ws://127.0.0.1:3001/"})
    return SimpleNamespace(platforms={Platform.NAPCAT: napcat_cfg})


class TestNapCatHistoryTool:
    def test_reads_group_history_from_current_session(self):
        config = _make_config()
        tokens = set_session_vars(
            platform="napcat",
            chat_id="100000001",
            chat_type="group",
            user_id="100000002",
        )
        try:
            with patch("tools.napcat_history_tool.load_gateway_config", return_value=config), patch(
                "tools.napcat_history_tool.call_napcat_action_once",
                new=AsyncMock(
                    return_value={
                        "status": "ok",
                        "retcode": 0,
                        "data": [
                            {
                                "message_id": "901",
                                "message_seq": 321,
                                "time": 1710000000,
                                "message": [
                                    {"type": "text", "data": {"text": "First msg"}},
                                    {"type": "image", "data": {"file": "a.png"}},
                                ],
                                "raw_message": "First msg[CQ:image,file=a.png]",
                                "sender": {"user_id": "10001", "nickname": "Alice"},
                            }
                        ],
                    }
                ),
            ) as call_mock:
                result = json.loads(_run_async(_napcat_read_history({"count": 5, "message_seq": 0})))
        finally:
            clear_session_vars(tokens)

        assert result["success"] is True
        assert result["chat_type"] == "group"
        assert result["target_ref"] == "group:100000001"
        assert result["returned_count"] == 1
        assert result["messages"][0]["sender_name"] == "Alice"
        assert result["messages"][0]["text"] == "First msg[image]"
        call_mock.assert_awaited_once_with(
            "ws://127.0.0.1:3001/",
            "tok",
            "get_group_msg_history",
            {
                "group_id": "100000001",
                "message_seq": 0,
                "count": 5,
                "reverseOrder": False,
            },
        )

    def test_reads_private_history_from_current_session(self):
        config = _make_config()
        tokens = set_session_vars(
            platform="napcat",
            chat_id="10001",
            chat_type="dm",
            user_id="10001",
        )
        try:
            with patch("tools.napcat_history_tool.load_gateway_config", return_value=config), patch(
                "tools.napcat_history_tool.call_napcat_action_once",
                new=AsyncMock(
                    return_value={
                        "status": "ok",
                        "retcode": 0,
                        "data": [
                            {
                                "message_id": "902",
                                "message_seq": 322,
                                "time": 1710000001,
                                "message": [{"type": "text", "data": {"text": "Hello"}}],
                                "raw_message": "Hello",
                                "sender": {"user_id": "10001", "nickname": "Bob"},
                            }
                        ],
                    }
                ),
            ) as call_mock:
                result = json.loads(
                    _run_async(_napcat_read_history({"count": 3, "message_seq": 123, "reverse_order": True}))
                )
        finally:
            clear_session_vars(tokens)

        assert result["success"] is True
        assert result["chat_type"] == "private"
        assert result["target_ref"] == "private:10001"
        assert result["messages"][0]["text"] == "Hello"
        call_mock.assert_awaited_once_with(
            "ws://127.0.0.1:3001/",
            "tok",
            "get_friend_msg_history",
            {
                "user_id": "10001",
                "message_seq": 123,
                "count": 3,
                "reverseOrder": True,
            },
        )

    def test_tool_definition_only_appears_in_napcat_session(self):
        config = _make_config()

        with patch("tools.napcat_history_tool.load_gateway_config", return_value=config):
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

        assert "napcat_read_history" in napcat_names
        assert "napcat_read_history" not in telegram_names
