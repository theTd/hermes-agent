import asyncio

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


@pytest.mark.asyncio
async def test_enrich_message_with_vision_times_out_gracefully(monkeypatch):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    monkeypatch.setenv("HERMES_GATEWAY_IMAGE_ENRICH_TIMEOUT", "1")

    async def _slow_vision(*, image_url, user_prompt):
        await asyncio.sleep(1.1)
        return '{"success": true, "analysis": "too late"}'

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", _slow_vision)

    result = await runner._enrich_message_with_vision(
        "What is in this image",
        ["/tmp/demo.png"],
    )

    assert "automatic image analysis timed out" in result
    assert "/tmp/demo.png" in result
    assert "What is in this image" in result


@pytest.mark.asyncio
async def test_enrich_message_with_vision_uses_napcat_cache(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    image_path = tmp_path / "meme.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"cached-meme")

    monkeypatch.setattr(
        "gateway.platforms.napcat_vision_cache.get_cached_analysis",
        lambda path: "A tired panda meme with folded arms." if path == str(image_path) else None,
    )

    async def _unexpected_vision(*, image_url, user_prompt):
        raise AssertionError("vision_analyze_tool should not be called on NapCat cache hit")

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", _unexpected_vision)

    result = await runner._enrich_message_with_vision(
        "What does this pic mean",
        [str(image_path)],
        platform=Platform.NAPCAT,
    )

    assert "A tired panda meme with folded arms." in result
    assert str(image_path) in result
    assert "What does this pic mean" in result


@pytest.mark.asyncio
async def test_prepare_inbound_message_text_exposes_referenced_napcat_group_image_for_lazy_vision(tmp_path):
    from gateway.run import GatewayRunner
    from gateway.config import GatewayConfig

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    image_path = tmp_path / "quoted-group-image.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"referenced")

    source = SessionSource(
        platform=Platform.NAPCAT,
        chat_id="123456",
        chat_type="group",
        user_id="10001",
        user_name="Alice",
    )
    event = MessageEvent(
        text="What does this pic mean",
        message_type=MessageType.TEXT,
        source=source,
    )
    event.metadata = {
        "napcat_referenced_image_urls": [str(image_path)],
        "napcat_referenced_image_types": ["image/png"],
    }

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert "The user referred to an earlier image from the group" in result
    assert "vision_analyze" in result
    assert str(image_path) in result
    assert "What does this pic mean" in result
