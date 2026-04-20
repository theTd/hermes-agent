#!/usr/bin/env python3
"""On-demand audio transcription tool for local paths or remote URLs."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict

from gateway.platforms.base import cache_audio_from_url
from tools.registry import registry
from tools.transcription_tools import (
    _get_provider,
    _load_stt_config,
    is_stt_enabled,
    transcribe_audio,
)


def _check_audio_transcribe_requirements() -> bool:
    stt_config = _load_stt_config()
    if not is_stt_enabled(stt_config):
        return False
    return _get_provider(stt_config) != "none"


async def audio_transcribe_tool(audio_url: str, model: str | None = None) -> str:
    resolved = str(audio_url or "").strip()
    if not resolved:
        return json.dumps({
            "success": False,
            "error": "audio_url is required",
            "transcript": "",
        }, ensure_ascii=False)

    local_path = None
    if resolved.startswith("file://"):
        resolved = resolved[len("file://"):]

    expanded_path = Path(os.path.expanduser(resolved))
    if expanded_path.is_file():
        local_path = str(expanded_path)
    elif resolved.startswith(("http://", "https://")):
        local_path = await cache_audio_from_url(resolved)
    else:
        return json.dumps({
            "success": False,
            "error": (
                "Invalid audio source. Provide an HTTP/HTTPS URL, file:// URL, "
                "or a valid local file path."
            ),
            "transcript": "",
        }, ensure_ascii=False)

    result = await asyncio.to_thread(transcribe_audio, local_path, model)
    payload: Dict[str, Any] = {
        "success": bool(result.get("success")),
        "transcript": str(result.get("transcript") or ""),
        "provider": str(result.get("provider") or ""),
        "model": str(result.get("model") or model or ""),
        "audio_path": str(local_path or ""),
    }
    if result.get("error"):
        payload["error"] = str(result.get("error"))
    return json.dumps(payload, ensure_ascii=False)


AUDIO_TRANSCRIBE_SCHEMA = {
    "name": "audio_transcribe",
    "description": (
        "Transcribe speech from an audio attachment on demand. Accepts an "
        "HTTP/HTTPS URL, file:// URL, or local file path."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "audio_url": {
                "type": "string",
                "description": "Audio URL (http/https, file://) or local file path to transcribe.",
            },
            "model": {
                "type": "string",
                "description": "Optional STT model override.",
            },
        },
        "required": ["audio_url"],
    },
}


def _handle_audio_transcribe(args: Dict[str, Any], **kw: Any) -> Any:
    return audio_transcribe_tool(
        audio_url=args.get("audio_url", ""),
        model=args.get("model") or None,
    )


registry.register(
    name="audio_transcribe",
    toolset="audio",
    schema=AUDIO_TRANSCRIBE_SCHEMA,
    handler=_handle_audio_transcribe,
    check_fn=_check_audio_transcribe_requirements,
    is_async=True,
    emoji="🎙️",
)
