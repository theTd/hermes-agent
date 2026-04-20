from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_audio_transcribe_tool_uses_local_file_directly(tmp_path):
    from tools.audio_transcribe_tool import audio_transcribe_tool

    audio_path = tmp_path / "note.ogg"
    audio_path.write_bytes(b"OggS")

    with patch(
        "tools.audio_transcribe_tool.transcribe_audio",
        return_value={"success": True, "transcript": "hello world", "provider": "local"},
    ) as transcribe_mock:
        result_json = await audio_transcribe_tool(str(audio_path))

    assert '"success": true' in result_json.lower()
    assert "hello world" in result_json
    transcribe_mock.assert_called_once_with(str(audio_path), None)


@pytest.mark.asyncio
async def test_audio_transcribe_tool_downloads_remote_audio_on_demand():
    from tools.audio_transcribe_tool import audio_transcribe_tool

    with patch(
        "tools.audio_transcribe_tool.cache_audio_from_url",
        new=AsyncMock(return_value="/tmp/cached-voice.ogg"),
    ) as cache_mock, patch(
        "tools.audio_transcribe_tool.transcribe_audio",
        return_value={"success": True, "transcript": "remote speech", "provider": "openai"},
    ) as transcribe_mock:
        result_json = await audio_transcribe_tool("https://example.com/audio.ogg")

    cache_mock.assert_awaited_once_with("https://example.com/audio.ogg")
    transcribe_mock.assert_called_once_with("/tmp/cached-voice.ogg", None)
    assert "remote speech" in result_json
