from __future__ import annotations

import json

from typing import Any, Mapping, Optional


_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "api_calls",
)


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def capture_usage_snapshot(agent: Any) -> dict[str, int]:
    """Capture cumulative usage counters from an agent instance."""
    if agent is None:
        return {key: 0 for key in _USAGE_KEYS}
    return {
        "input_tokens": _to_int(getattr(agent, "session_input_tokens", 0)),
        "output_tokens": _to_int(getattr(agent, "session_output_tokens", 0)),
        "cache_read_tokens": _to_int(getattr(agent, "session_cache_read_tokens", 0)),
        "cache_write_tokens": _to_int(getattr(agent, "session_cache_write_tokens", 0)),
        "reasoning_tokens": _to_int(getattr(agent, "session_reasoning_tokens", 0)),
        "api_calls": _to_int(getattr(agent, "session_api_calls", 0)),
    }


def diff_usage_snapshots(
    before: Optional[Mapping[str, Any]],
    after: Optional[Mapping[str, Any]],
) -> dict[str, int]:
    """Compute a non-negative delta between two cumulative usage snapshots."""
    before = before or {}
    after = after or {}
    return {
        key: max(0, _to_int(after.get(key, 0)) - _to_int(before.get(key, 0)))
        for key in _USAGE_KEYS
    }


def merge_usage_dicts(*usage_dicts: Optional[Mapping[str, Any]]) -> dict[str, int]:
    merged = {key: 0 for key in _USAGE_KEYS}
    for usage in usage_dicts:
        if not usage:
            continue
        for key in _USAGE_KEYS:
            merged[key] += _to_int(usage.get(key, 0))
    return merged


def _extract_delegate_usage_from_payload(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {key: 0 for key in _USAGE_KEYS}
    results = payload.get("results")
    if not isinstance(results, list):
        return {key: 0 for key in _USAGE_KEYS}

    aggregate = {key: 0 for key in _USAGE_KEYS}
    for entry in results:
        if not isinstance(entry, dict):
            continue
        tokens = entry.get("tokens") or {}
        aggregate["input_tokens"] += _to_int(tokens.get("input"))
        aggregate["output_tokens"] += _to_int(tokens.get("output"))
        aggregate["cache_read_tokens"] += _to_int(tokens.get("cache_read"))
        aggregate["cache_write_tokens"] += _to_int(tokens.get("cache_write"))
        aggregate["reasoning_tokens"] += _to_int(tokens.get("reasoning"))
        aggregate["api_calls"] += _to_int(entry.get("api_calls"))
    return aggregate


def extract_delegate_usage_from_messages(messages: Optional[list[dict[str, Any]]]) -> dict[str, int]:
    """Extract child-agent usage from delegate_task tool results in a message slice."""
    if not messages:
        return {key: 0 for key in _USAGE_KEYS}

    tool_name_by_id: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tool_call in msg.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            tc_id = tool_call.get("id")
            fn = (tool_call.get("function") or {}).get("name")
            if tc_id and isinstance(fn, str):
                tool_name_by_id[tc_id] = fn

    aggregate = {key: 0 for key in _USAGE_KEYS}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") not in ("tool", "function"):
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except Exception:
            continue

        tool_call_id = msg.get("tool_call_id")
        mapped_name = tool_name_by_id.get(tool_call_id, "")
        looks_like_delegate = (
            mapped_name == "delegate_task"
            or (
                isinstance(payload, dict)
                and isinstance(payload.get("results"), list)
                and "total_duration_seconds" in payload
            )
        )
        if not looks_like_delegate:
            continue
        aggregate = merge_usage_dicts(aggregate, _extract_delegate_usage_from_payload(payload))

    return aggregate
