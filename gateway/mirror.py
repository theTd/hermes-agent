"""
Session mirroring for cross-platform message delivery.

When a message is sent to a platform (via send_message or cron delivery),
this module appends a "delivery-mirror" record to the target session's
transcript so the receiving-side agent has context about what was sent.

Standalone -- works from CLI, cron, and gateway contexts without needing
the full SessionStore machinery.
"""

import json
import logging
from datetime import datetime
from typing import Optional

from hermes_cli.config import get_hermes_home

logger = logging.getLogger(__name__)

_SESSIONS_DIR = get_hermes_home() / "sessions"
_SESSIONS_INDEX = _SESSIONS_DIR / "sessions.json"


def mirror_to_session(
    platform: str,
    chat_id: str,
    message_text: str,
    source_label: str = "cli",
    thread_id: Optional[str] = None,
    preferred_user_id: Optional[str] = None,
    broadcast: bool = False,
    resolved_session_ids: Optional[list[str]] = None,
) -> bool:
    """
    Append a delivery-mirror message to the target session's transcript.

    Finds the gateway session that matches the given platform + chat_id,
    then writes a mirror entry to both the JSONL transcript and SQLite DB.

    Returns True if mirrored successfully, False if no matching session or error.
    All errors are caught -- this is never fatal.
    """
    try:
        session_ids = list(resolved_session_ids or [])
        if not session_ids:
            session_ids = _find_session_ids(
                platform,
                str(chat_id),
                thread_id=thread_id,
                preferred_user_id=preferred_user_id,
            )
        if not session_ids:
            logger.debug(
                "Mirror: no session found for %s:%s:%s (preferred_user_id=%s)",
                platform,
                chat_id,
                thread_id,
                preferred_user_id,
            )
            return False

        if not broadcast:
            session_ids = session_ids[:1]

        mirror_msg = {
            "role": "assistant",
            "content": message_text,
            "timestamp": datetime.now().isoformat(),
            "mirror": True,
            "mirror_source": source_label,
        }

        wrote_any = False
        for session_id in session_ids:
            _append_to_jsonl(session_id, mirror_msg)
            _append_to_sqlite(session_id, mirror_msg)
            wrote_any = True

        logger.debug(
            "Mirror: wrote to %d session(s) %s (from %s, preferred_user_id=%s, broadcast=%s)",
            len(session_ids),
            session_ids,
            source_label,
            preferred_user_id,
            broadcast,
        )
        return wrote_any

    except Exception as e:
        logger.debug(
            "Mirror failed for %s:%s:%s (preferred_user_id=%s): %s",
            platform,
            chat_id,
            thread_id,
            preferred_user_id,
            e,
        )
        return False


def _find_session_ids(
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    preferred_user_id: Optional[str] = None,
) -> list[str]:
    """
    Find the active session_id for a platform + chat_id pair.

    Scans sessions.json entries and matches where origin.chat_id == chat_id
    on the right platform.  DM session keys don't embed the chat_id
    (e.g. "agent:main:telegram:dm"), so we check the origin dict.
    """
    if not _SESSIONS_INDEX.exists():
        return []

    try:
        with open(_SESSIONS_INDEX, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    platform_lower = platform.lower()
    preferred_matches: list[tuple[str, str]] = []
    matches: list[tuple[str, str]] = []
    preferred_user_id = str(preferred_user_id or "").strip()

    for _key, entry in data.items():
        origin = entry.get("origin") or {}
        entry_platform = (origin.get("platform") or entry.get("platform", "")).lower()

        if entry_platform != platform_lower:
            continue

        origin_chat_id = str(origin.get("chat_id", "")).strip()
        target_chat_id = str(chat_id or "").strip()
        if origin_chat_id != target_chat_id:
            continue

        origin_thread_id = origin.get("thread_id")
        if thread_id is not None and str(origin_thread_id or "") != str(thread_id):
            continue

        session_id = entry.get("session_id")
        if not session_id:
            continue

        updated = entry.get("updated_at", "")
        origin_user_id = str(origin.get("user_id") or "").strip()
        if preferred_user_id and origin_user_id == preferred_user_id:
            preferred_matches.append((updated, session_id))
        else:
            matches.append((updated, session_id))

    ordered = sorted(preferred_matches, reverse=True) + sorted(matches, reverse=True)
    seen: set[str] = set()
    session_ids: list[str] = []
    for _updated, session_id in ordered:
        if session_id in seen:
            continue
        seen.add(session_id)
        session_ids.append(session_id)

    return session_ids


def _find_session_id(
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    preferred_user_id: Optional[str] = None,
) -> Optional[str]:
    """Return the best matching session_id for backwards-compatible callers."""
    session_ids = _find_session_ids(
        platform,
        chat_id,
        thread_id=thread_id,
        preferred_user_id=preferred_user_id,
    )
    return session_ids[0] if session_ids else None


def _append_to_jsonl(session_id: str, message: dict) -> None:
    """Append a message to the JSONL transcript file."""
    transcript_path = _SESSIONS_DIR / f"{session_id}.jsonl"
    try:
        with open(transcript_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("Mirror JSONL write failed: %s", e)


def _append_to_sqlite(session_id: str, message: dict) -> None:
    """Append a message to the SQLite session database."""
    db = None
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        db.append_message(
            session_id=session_id,
            role=message.get("role", "assistant"),
            content=message.get("content"),
        )
    except Exception as e:
        logger.debug("Mirror SQLite write failed: %s", e)
    finally:
        if db is not None:
            db.close()
