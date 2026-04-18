from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/usagefooter", platform=Platform.TELEGRAM, user_id="12345", chat_id="67890"):
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_key_for_source = MagicMock(return_value="agent:main:telegram:private:12345")
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    runner._get_gateway_extension().replace_reply_usage_footer_sessions(set())
    return runner


@pytest.mark.asyncio
async def test_usagefooter_command_toggles_per_chat_state():
    runner = _make_runner()

    result_on = await runner._handle_usagefooter_command(_make_event("/usagefooter on"))
    assert "ON" in result_on
    assert runner._reply_usage_footer_enabled("agent:main:telegram:private:12345") is True

    result_status = await runner._handle_usagefooter_command(_make_event("/usagefooter"))
    assert "Current state: **ON**" in result_status

    result_off = await runner._handle_usagefooter_command(_make_event("/usagefooter off"))
    assert "OFF" in result_off
    assert runner._reply_usage_footer_enabled("agent:main:telegram:private:12345") is False
