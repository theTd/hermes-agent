"""Helpers for packaging gateway agent run results and reply post-processing."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Mapping, Optional

from agent.reply_usage_footer import (
    append_reply_usage_footer,
    build_reply_usage_footer,
    capture_usage_snapshot,
    diff_usage_snapshots,
    extract_delegate_usage_from_messages,
    merge_usage_dicts,
)


@dataclass(frozen=True)
class AgentRunMetrics:
    """Normalized per-turn metrics derived from an agent run result."""

    last_prompt_tokens: int
    input_tokens: int
    output_tokens: int
    model: Optional[str]
    turn_usage: dict[str, int]
    history_offset: int
    history_rewritten: bool


def capture_agent_usage_snapshot(agent: Any) -> dict[str, int]:
    """Capture the cumulative usage counters from an agent instance."""
    return capture_usage_snapshot(agent)


def build_agent_run_metrics(
    *,
    agent: Any,
    agent_result: Mapping[str, Any],
    agent_history: list[dict[str, Any]],
    usage_before: Optional[Mapping[str, Any]],
) -> AgentRunMetrics:
    """Build normalized per-turn metrics for gateway response handling."""
    last_prompt_tokens = 0
    input_tokens = 0
    output_tokens = 0
    resolved_model = getattr(agent, "model", None) if agent is not None else None
    if agent is not None and hasattr(agent, "context_compressor"):
        last_prompt_tokens = int(
            getattr(getattr(agent, "context_compressor", None), "last_prompt_tokens", 0) or 0
        )
        input_tokens = int(getattr(agent, "session_prompt_tokens", 0) or 0)
        output_tokens = int(getattr(agent, "session_completion_tokens", 0) or 0)

    all_messages = list(agent_result.get("messages", []) or [])
    if len(agent_history) < len(all_messages):
        current_turn_messages = all_messages[len(agent_history):]
    else:
        current_turn_messages = all_messages

    turn_usage = merge_usage_dicts(
        diff_usage_snapshots(usage_before or {}, capture_usage_snapshot(agent)),
        extract_delegate_usage_from_messages(current_turn_messages),
    )
    history_rewritten = bool(agent_result.get("history_rewritten"))
    history_offset = 0 if history_rewritten else len(agent_history)

    return AgentRunMetrics(
        last_prompt_tokens=last_prompt_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=resolved_model,
        turn_usage=turn_usage,
        history_offset=history_offset,
        history_rewritten=history_rewritten,
    )


def extract_tool_generated_image_paths(content: Any) -> list[str]:
    """Return local image paths surfaced in structured tool results."""
    if not isinstance(content, str):
        return []
    content = content.strip()
    if not content or "MEDIA:" in content:
        return []

    try:
        payload = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []

    image_paths: list[str] = []
    seen: set[str] = set()
    candidate_keys = {
        "image",
        "image_path",
        "local_path",
        "screenshot_path",
        "output_path",
        "thumbnail_path",
    }
    image_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp")

    def _maybe_add(value: Any, *, key_hint: str = "") -> None:
        if not isinstance(value, str):
            return
        candidate = value.strip().strip("`\"'")
        if not candidate:
            return
        lowered = candidate.lower()
        if not lowered.endswith(image_exts):
            return
        if not (candidate.startswith("/") or candidate.startswith("~/")):
            return
        if key_hint and key_hint not in candidate_keys:
            return
        if candidate not in seen:
            seen.add(candidate)
            image_paths.append(candidate)

    def _walk(node: Any, *, key_hint: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                _walk(value, key_hint=str(key or "").strip().lower())
            return
        if isinstance(node, list):
            for item in node:
                _walk(item, key_hint=key_hint)
            return
        _maybe_add(node, key_hint=key_hint)

    _walk(payload)
    return image_paths


def collect_tool_result_attachment_lines(
    messages: list[dict[str, Any]],
    *,
    history_media_paths: set[str],
    final_response: str,
) -> tuple[list[str], bool]:
    """Collect attachment lines that should be appended to the final reply."""
    lines: list[str] = []
    has_voice_directive = False
    seen: set[str] = set()
    response_text = str(final_response or "")

    for msg in messages or []:
        if msg.get("role") not in ("tool", "function"):
            continue

        content = str(msg.get("content", "") or "")
        if not content:
            continue

        if "MEDIA:" in content:
            for match in re.finditer(r"MEDIA:(\S+)", content):
                path = match.group(1).strip().rstrip('",}')
                if path and path not in history_media_paths and path not in response_text:
                    tag = f"MEDIA:{path}"
                    if tag not in seen:
                        seen.add(tag)
                        lines.append(tag)
            if "[[audio_as_voice]]" in content:
                has_voice_directive = True

        for path in extract_tool_generated_image_paths(content):
            if path in history_media_paths or path in response_text:
                continue
            tag = f"MEDIA:{path}"
            if tag not in seen:
                seen.add(tag)
                lines.append(tag)

    return lines, has_voice_directive


def maybe_append_tool_result_attachments(
    final_response: str,
    *,
    messages: list[dict[str, Any]],
    history_media_paths: set[str],
) -> str:
    """Append attachment lines surfaced by tool results when needed."""
    attachment_lines, has_voice_directive = collect_tool_result_attachment_lines(
        messages,
        history_media_paths=history_media_paths,
        final_response=final_response,
    )
    if not attachment_lines:
        return final_response
    if has_voice_directive:
        attachment_lines.insert(0, "[[audio_as_voice]]")
    return str(final_response or "") + "\n" + "\n".join(attachment_lines)


def build_reply_usage_footer_text(agent_result: Mapping[str, Any], *, enabled: bool) -> str:
    """Build a per-reply usage footer for the current result when enabled."""
    if not enabled:
        return ""
    return build_reply_usage_footer(
        agent_result.get("turn_usage"),
        model=agent_result.get("model"),
        provider=agent_result.get("provider"),
        base_url=agent_result.get("base_url"),
    )


def append_reply_usage_footer_text(response: str, footer: str) -> str:
    """Append a usage footer to *response* when present."""
    return append_reply_usage_footer(response, footer)


def collect_tools_used(messages: list[dict[str, Any]]) -> list[str]:
    """Collect unique tool names used across a turn."""
    tools_used: list[str] = []
    seen: set[str] = set()
    for msg in messages or []:
        for tool_call in msg.get("tool_calls") or []:
            name = str((tool_call.get("function") or {}).get("name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                tools_used.append(name)
        tool_name = str(msg.get("tool_name") or "").strip()
        if tool_name and tool_name not in seen:
            seen.add(tool_name)
            tools_used.append(tool_name)
    return tools_used


def emit_agent_response_trace(
    *,
    trace_ctx: Optional[Any],
    emit_event: Callable[..., Any],
    final_event_type: Optional[Any],
    suppressed_event_type: Optional[Any],
    response: str,
    response_previewed: bool,
    agent_messages: list[dict[str, Any]],
    agent_result: Mapping[str, Any],
    promotion_active: bool,
    promotion_stage_used: str,
    promotion_escalated: bool,
    promotion_reason: str,
    promotion_l1_model: str,
    promotion_l2_model: str,
) -> None:
    """Emit a final or suppressed response event through the provided emitter."""
    if not trace_ctx:
        return

    if response:
        emit_event(
            final_event_type,
            {
                "response_preview": str(response)[:200],
                "response_previewed": bool(response_previewed),
                "tools_used": collect_tools_used(agent_messages),
                "api_call_count": agent_result.get("api_calls", 0),
                "model": agent_result.get("model", ""),
                "provider": agent_result.get("provider", ""),
                "api_mode": agent_result.get("api_mode", ""),
                "promotion_active": promotion_active,
                "promotion_stage_used": promotion_stage_used,
                "promotion_escalated": promotion_escalated,
                "promotion_reason": promotion_reason,
                "promotion_l1_model": promotion_l1_model,
                "promotion_l2_model": promotion_l2_model,
            },
            trace_ctx=trace_ctx,
        )
        return

    if suppressed_event_type is None:
        return
    emit_event(
        suppressed_event_type,
        {
            "reason": agent_result.get("error", "no_response") or "no_response",
            "interrupted": bool(agent_result.get("interrupted")),
            "api_calls": agent_result.get("api_calls", 0),
        },
        trace_ctx=trace_ctx,
    )
