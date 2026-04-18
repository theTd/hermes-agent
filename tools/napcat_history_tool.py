"""NapCat session history tool.

Reads QQ/NapCat message history for the current NapCat session by calling the
OneBot 11 history APIs exposed by NapCat.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from gateway.config import Platform, load_gateway_config
from gateway.platforms.napcat import call_napcat_action_once
from gateway.session_context import get_session_env
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_MAX_HISTORY_COUNT = 50


NAPCAT_READ_HISTORY_SCHEMA = {
    "name": "napcat_read_history",
    "description": (
        "Read message history from the current NapCat / QQ conversation. "
        "Works only inside an active NapCat session and automatically reads "
        "the current private chat or current group."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "count": {
                "type": "integer",
                "description": "How many messages to fetch. Default 20. Maximum 50.",
                "minimum": 1,
                "maximum": _MAX_HISTORY_COUNT,
            },
            "message_seq": {
                "type": "integer",
                "description": (
                    "Pagination cursor for older history. Use 0 for the latest "
                    "window, or pass an older message_seq returned by NapCat."
                ),
                "minimum": 0,
            },
            "reverse_order": {
                "type": "boolean",
                "description": "Whether NapCat should reverse the returned order. Default false.",
            },
        },
        "required": [],
    },
}


def _coerce_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _format_timestamp(ts: Any) -> str | None:
    try:
        if ts in (None, ""):
            return None
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _segment_types(segments: list[dict[str, Any]]) -> list[str]:
    types: list[str] = []
    for seg in segments:
        seg_type = str(seg.get("type") or "").strip().lower()
        if seg_type and seg_type not in types:
            types.append(seg_type)
    return types


def _render_segments(segments: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for seg in segments:
        seg_type = str(seg.get("type") or "").strip().lower()
        data = seg.get("data") or {}
        if seg_type == "text":
            text = str(data.get("text") or "")
            if text:
                parts.append(text)
        elif seg_type == "at":
            qq = str(data.get("qq") or "").strip()
            parts.append(f"@{qq}" if qq else "@")
        elif seg_type in {"image", "record", "audio", "voice", "video", "file", "reply"}:
            label_map = {
                "image": "[image]",
                "record": "[audio]",
                "audio": "[audio]",
                "voice": "[audio]",
                "video": "[video]",
                "file": "[file]",
                "reply": "[reply]",
            }
            parts.append(label_map[seg_type])
        elif seg_type:
            parts.append(f"[{seg_type}]")
    return "".join(parts).strip()


def _normalize_segments(message: Any) -> list[dict[str, Any]]:
    if isinstance(message, list):
        return [seg for seg in message if isinstance(seg, dict)]
    if isinstance(message, str):
        return [{"type": "text", "data": {"text": message}}]
    return []


def _extract_history_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("messages", "message_list", "records", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _resolve_current_target() -> tuple[str, str]:
    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    if platform != "napcat":
        raise ValueError("napcat_read_history is only available inside a NapCat session.")

    chat_type = get_session_env("HERMES_SESSION_CHAT_TYPE", "").strip().lower()
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "").strip()
    user_id = get_session_env("HERMES_SESSION_USER_ID", "").strip()

    if chat_type == "group":
        if not chat_id:
            raise ValueError("Current NapCat group session has no chat_id.")
        return "group", chat_id

    if chat_type in {"dm", "private"}:
        target_user = user_id or chat_id
        if not target_user:
            raise ValueError("Current NapCat private session has no user_id.")
        return "private", target_user

    if chat_id and user_id and chat_id != user_id:
        return "group", chat_id
    if user_id or chat_id:
        return "private", (user_id or chat_id)

    raise ValueError("Could not resolve the current NapCat conversation target.")


def _check_napcat_history() -> bool:
    if get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower() != "napcat":
        return False

    try:
        config = load_gateway_config()
        pconfig = config.platforms.get(Platform.NAPCAT)
    except Exception:
        return False

    if not pconfig or not getattr(pconfig, "enabled", False):
        return False
    extra = getattr(pconfig, "extra", {}) or {}
    return bool(str(extra.get("ws_url") or "").strip())


async def _napcat_read_history(args: dict[str, Any], **_kwargs) -> str:
    try:
        config = load_gateway_config()
        pconfig = config.platforms.get(Platform.NAPCAT)
        if not pconfig or not getattr(pconfig, "enabled", False):
            return tool_error("NapCat is not configured.", success=False)

        extra = getattr(pconfig, "extra", {}) or {}
        ws_url = str(extra.get("ws_url") or "").strip()
        token = getattr(pconfig, "token", None) or str(extra.get("token") or "").strip() or None
        if not ws_url:
            return tool_error("NapCat is configured without ws_url.", success=False)

        target_type, target_id = _resolve_current_target()
        count = max(1, min(_MAX_HISTORY_COUNT, _coerce_int(args.get("count"), 20)))
        message_seq = max(0, _coerce_int(args.get("message_seq"), 0))
        reverse_order = _coerce_bool(args.get("reverse_order"), default=False)

        action = "get_group_msg_history" if target_type == "group" else "get_friend_msg_history"
        params = {
            "message_seq": message_seq,
            "count": count,
            "reverseOrder": reverse_order,
        }
        if target_type == "group":
            params["group_id"] = target_id
            target_ref = f"group:{target_id}"
        else:
            params["user_id"] = target_id
            target_ref = f"private:{target_id}"

        response = await call_napcat_action_once(ws_url, token, action, params)
        if response.get("status") != "ok" or int(response.get("retcode", 0) or 0) != 0:
            message = response.get("message") or response.get("wording") or f"{action} failed"
            return tool_error(message, success=False)

        raw_items = _extract_history_items(response.get("data"))
        messages: list[dict[str, Any]] = []
        for item in raw_items:
            sender = item.get("sender") or {}
            segments = _normalize_segments(item.get("message"))
            rendered_text = _render_segments(segments)
            raw_message = str(item.get("raw_message") or "").strip()
            messages.append(
                {
                    "message_id": str(item.get("message_id") or ""),
                    "message_seq": item.get("message_seq"),
                    "time": item.get("time"),
                    "time_iso": _format_timestamp(item.get("time")),
                    "sender_id": str(sender.get("user_id") or item.get("user_id") or ""),
                    "sender_name": str(
                        sender.get("card")
                        or sender.get("nickname")
                        or sender.get("user_name")
                        or sender.get("uid")
                        or item.get("user_id")
                        or ""
                    ).strip(),
                    "text": rendered_text or raw_message,
                    "raw_message": raw_message,
                    "segment_types": _segment_types(segments),
                }
            )

        return json.dumps(
            {
                "success": True,
                "platform": "napcat",
                "chat_type": target_type,
                "target_ref": target_ref,
                "message_seq": message_seq,
                "reverse_order": reverse_order,
                "requested_count": count,
                "returned_count": len(messages),
                "messages": messages,
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.debug("napcat_read_history failed", exc_info=True)
        return tool_error(f"NapCat history read failed: {exc}", success=False)


registry.register(
    name="napcat_read_history",
    toolset="hermes-napcat",
    schema=NAPCAT_READ_HISTORY_SCHEMA,
    handler=_napcat_read_history,
    check_fn=_check_napcat_history,
    is_async=True,
    emoji="🕘",
    max_result_size_chars=60_000,
)
