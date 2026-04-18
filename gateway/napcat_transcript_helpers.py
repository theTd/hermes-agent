"""Branch-local NapCat transcript and recall helpers."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from gateway.config import Platform
from gateway.napcat_metadata import (
    TRANSCRIPT_DIRECTION_INBOUND,
    TRANSCRIPT_DIRECTION_KEY,
    TRANSCRIPT_RECALLED_AT_KEY,
    TRANSCRIPT_RECALLED_KEY,
    TRANSCRIPT_RECALL_INTERNAL_SOURCE,
    TRANSCRIPT_RECALL_OPERATOR_ID_KEY,
    TRANSCRIPT_RECALL_TYPE_KEY,
)
from gateway.platforms.base import MessageEvent
from gateway.runtime_extension import GatewayEventType, emit_gateway_event
from gateway.session import SessionSource

logger = logging.getLogger(__name__)


def napcat_transcript_metadata_for_event(
    event: Optional[MessageEvent],
    source: Optional[SessionSource],
) -> Dict[str, Any]:
    if not event or not source:
        return {}
    platform_value = getattr(getattr(source, "platform", None), "value", "")
    if platform_value != "napcat":
        return {}
    message_id = str(getattr(event, "message_id", "") or "").strip()
    if not message_id:
        return {}
    return {
        "platform": platform_value,
        "platform_message_id": message_id,
        "platform_chat_type": str(getattr(source, "chat_type", "") or ""),
        "platform_chat_id": str(getattr(source, "chat_id", "") or ""),
        "platform_user_id": str(getattr(source, "user_id", "") or ""),
        TRANSCRIPT_DIRECTION_KEY: TRANSCRIPT_DIRECTION_INBOUND,
    }


def decorate_napcat_transcript_messages(
    messages: List[Dict[str, Any]],
    *,
    event: Optional[MessageEvent],
    source: Optional[SessionSource],
) -> List[Dict[str, Any]]:
    metadata = napcat_transcript_metadata_for_event(event, source)
    if not metadata:
        return messages

    user_marked = False
    decorated: List[Dict[str, Any]] = []
    for msg in messages:
        entry = dict(msg)
        if not user_marked and entry.get("role") == "user":
            for key, value in metadata.items():
                entry.setdefault(key, value)
            user_marked = True
        decorated.append(entry)
    return decorated


def load_transcript_messages_for_rewrite(session_store: Any, session_id: str) -> List[Dict[str, Any]]:
    transcript_path_getter = getattr(session_store, "get_transcript_path", None)
    if callable(transcript_path_getter):
        try:
            transcript_path = transcript_path_getter(session_id)
            if transcript_path.exists():
                messages: List[Dict[str, Any]] = []
                with open(transcript_path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(payload, dict):
                            messages.append(payload)
                if messages:
                    return messages
        except Exception:
            logger.debug("Failed to load transcript JSONL for rewrite", exc_info=True)

    try:
        loaded = session_store.load_transcript(session_id)
    except Exception:
        return []
    return [dict(msg) for msg in loaded if isinstance(msg, dict)]


def napcat_recall_tombstone(chat_type: str, *, missing_match: bool = False) -> str:
    if missing_match:
        return (
            "[System note: A QQ message recall event was received, but no matching transcript entry "
            "was found to rewrite.]"
        )
    if chat_type == "group":
        return (
            "[System note: A previously recorded QQ group message was retracted and should no longer "
            "be treated as active context.]"
        )
    return (
        "[System note: A previously recorded QQ direct message was retracted and should no longer "
        "be treated as active context.]"
    )


async def handle_napcat_recall_event(
    host: Any,
    recall_event: Any,
    *,
    emit_event_fn: Optional[Any] = None,
) -> None:
    message_id = str(getattr(recall_event, "message_id", "") or "").strip()
    chat_id = str(getattr(recall_event, "chat_id", "") or "").strip()
    chat_type = str(getattr(recall_event, "chat_type", "") or "").strip()
    user_id = str(getattr(recall_event, "user_id", "") or "").strip()
    operator_id = str(getattr(recall_event, "operator_id", "") or "").strip()
    recall_type = str(getattr(recall_event, "recall_type", "") or "").strip()
    trace_ctx = getattr(recall_event, "trace_ctx", None)

    if not message_id or not chat_id or chat_type not in {"dm", "group"}:
        return

    source = SessionSource(
        platform=Platform("napcat"),
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id or None,
    )
    session_key = host.session_key_for_source(source)
    session_entry = host.get_session_entry(session_key)

    transcript_match_found = False
    if session_entry:
        messages = host.load_transcript_messages_for_rewrite(session_entry.session_id)
        recall_note_exists = any(
            str(item.get("internal_source") or "") == TRANSCRIPT_RECALL_INTERNAL_SOURCE
            and str(item.get("platform_message_id") or "") == message_id
            for item in messages
            if isinstance(item, dict)
        )

        rewritten = False
        for idx, item in enumerate(messages):
            if not isinstance(item, dict):
                continue
            if str(item.get("platform") or "") != "napcat":
                continue
            if str(item.get("platform_message_id") or "") != message_id:
                continue
            transcript_match_found = True
            if item.get(TRANSCRIPT_RECALLED_KEY):
                rewritten = True
                break
            updated = dict(item)
            updated["content"] = host.recall_tombstone(chat_type)
            updated[TRANSCRIPT_RECALLED_KEY] = True
            updated[TRANSCRIPT_RECALL_TYPE_KEY] = recall_type
            updated[TRANSCRIPT_RECALLED_AT_KEY] = datetime.now().isoformat()
            if operator_id:
                updated[TRANSCRIPT_RECALL_OPERATOR_ID_KEY] = operator_id
            messages[idx] = updated
            rewritten = True
            break

        if transcript_match_found and rewritten:
            host.rewrite_transcript(session_entry.session_id, messages)
        elif not transcript_match_found and not recall_note_exists:
            payload = {
                "role": "system",
                "content": host.recall_tombstone(chat_type, missing_match=True),
                "timestamp": datetime.now().isoformat(),
                "platform": "napcat",
                "platform_message_id": message_id,
                "platform_chat_type": chat_type,
                "platform_chat_id": chat_id,
                "platform_user_id": user_id,
                TRANSCRIPT_RECALLED_KEY: True,
                TRANSCRIPT_RECALL_TYPE_KEY: recall_type,
                TRANSCRIPT_RECALLED_AT_KEY: datetime.now().isoformat(),
                "internal_source": TRANSCRIPT_RECALL_INTERNAL_SOURCE,
            }
            if operator_id:
                payload[TRANSCRIPT_RECALL_OPERATOR_ID_KEY] = operator_id
            host.append_to_transcript(session_entry.session_id, payload)

    emitter = emit_event_fn or emit_gateway_event
    emitter(
        GatewayEventType.MESSAGE_RECALLED,
        {
            "chat_type": chat_type,
            "chat_id": chat_id,
            "message_id": message_id,
            "user_id": user_id,
            "operator_id": operator_id,
            "recall_type": recall_type,
            "transcript_match_found": transcript_match_found,
            "session_key": session_key,
        },
        trace_ctx=trace_ctx,
    )
