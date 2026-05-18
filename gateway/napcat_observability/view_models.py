"""
NapCat observability view model generation.

Converts raw trace + events into frontend-ready view models.
This mirrors the logic in napcat_web/src/features/observability/stage-mapper.ts
but runs on the backend so the frontend can be a pure renderer.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from gateway.napcat_observability.schema import EventType, event_stage


STAGE_ORDER = [
    "inbound",
    "trigger",
    "context",
    "model",
    "tools",
    "memory_skill_routing",
    "final",
    "error",
    "raw",
]

STAGE_TITLES = {
    "inbound": "Inbound",
    "trigger": "Trigger",
    "context": "Context / Prompt",
    "model": "Model / Streaming",
    "tools": "Tools",
    "memory_skill_routing": "Memory / Skill / Routing",
    "final": "Final",
    "error": "Error",
    "raw": "Raw",
}

STAGE_PRIORITY = {
    "raw": 0,
    "inbound": 1,
    "trigger": 2,
    "context": 3,
    "model": 4,
    "tools": 5,
    "memory_skill_routing": 6,
    "final": 7,
    "error": 8,
}


def _sort_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def sort_key(e: Dict[str, Any]) -> Tuple:
        seq = e.get("payload", {}).get("sequence", 0)
        return (e.get("ts", 0), seq, e.get("event_id", ""))
    return sorted(events, key=sort_key)


def _latest_of(events: List[Dict[str, Any]], event_type: str) -> Optional[Dict[str, Any]]:
    for e in reversed(events):
        if e.get("event_type") == event_type:
            return e
    return None


def _trim_text(value: Any, max_chars: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def _fallback_stage(event: Dict[str, Any]) -> str:
    stage = str(event.get("payload", {}).get("stage", "")).strip()
    if stage in STAGE_ORDER:
        return stage
    try:
        return event_stage(event["event_type"])
    except (ValueError, KeyError):
        return "raw"


def _build_tool_calls(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tool_map: Dict[str, Dict[str, Any]] = {}
    open_fallback_ids: Dict[str, List[str]] = {}

    for event in events:
        et = event.get("event_type", "")
        if not et.startswith("agent.tool."):
            continue
        payload = event.get("payload", {}) or {}
        tool_name = str(payload.get("tool_name", "tool") or "tool").strip() or "tool"
        explicit_id = str(payload.get("tool_call_id", "")).strip()

        if explicit_id:
            tid = explicit_id
        elif et == "agent.tool.called":
            tid = f"{tool_name}:{event.get('ts', 0)}:{event.get('trace_id', '')}"
            open_fallback_ids.setdefault(tool_name, []).append(tid)
        else:
            queue = open_fallback_ids.get(tool_name, [])
            tid = queue.pop(0) if queue else f"{tool_name}:{event.get('ts', 0)}:{event.get('trace_id', '')}"
            open_fallback_ids[tool_name] = queue

        existing = tool_map.get(tid, {
            "id": tid,
            "toolName": tool_name,
            "status": "running",
            "startedAt": event.get("ts", 0),
            "endedAt": None,
            "durationMs": None,
            "argsPreview": "",
            "resultPreview": "",
            "stdoutPreview": "",
            "stderrPreview": "",
            "error": "",
        })

        next_item = dict(existing)
        next_item["startedAt"] = min(next_item["startedAt"], event.get("ts", 0))
        next_item["argsPreview"] = next_item["argsPreview"] or str(payload.get("args_preview", ""))
        next_item["resultPreview"] = next_item["resultPreview"] or str(payload.get("result_preview", ""))
        next_item["stdoutPreview"] = next_item["stdoutPreview"] or str(payload.get("stdout_preview", ""))
        next_item["stderrPreview"] = next_item["stderrPreview"] or str(payload.get("stderr_preview", ""))
        next_item["error"] = next_item["error"] or str(payload.get("error", ""))

        if et == "agent.tool.completed":
            next_item["status"] = "completed"
            next_item["endedAt"] = event.get("ts", 0)
        elif et == "agent.tool.failed":
            next_item["status"] = "failed"
            next_item["endedAt"] = event.get("ts", 0)
            next_item["error"] = next_item["error"] or str(payload.get("error", "Tool failed"))

        dur = _resolve_duration_ms(payload)
        if isinstance(dur, (int, float)) and dur >= 0:
            next_item["durationMs"] = dur
        elif next_item["endedAt"] is not None:
            next_item["durationMs"] = max(0, (next_item["endedAt"] - next_item["startedAt"]) * 1000)

        tool_map[tid] = next_item

    return sorted(tool_map.values(), key=lambda x: (x["startedAt"], x["id"]))


def _build_stream_chunks(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    chunks = []
    accumulated: Dict[str, Dict[str, Any]] = {}
    for event in events:
        et = event.get("event_type", "")
        if et not in ("agent.reasoning.delta", "agent.response.delta"):
            continue
        kind = "thinking" if et == "agent.reasoning.delta" else "response"
        payload = event.get("payload", {}) or {}
        agent_scope = str(payload.get("agent_scope", ""))
        child_id = str(payload.get("child_id", ""))
        llm_request_id = str(payload.get("llm_request_id", ""))
        sequence = payload.get("sequence", 0)
        base_key = f"{kind}:{agent_scope}:{child_id}:{llm_request_id or 'legacy'}"
        prev = accumulated.get(base_key, {"content": "", "lastSequence": 0})
        explicit_content = payload.get("content", "") if isinstance(payload.get("content"), str) else ""
        delta = payload.get("delta", "") if isinstance(payload.get("delta"), str) else ""
        should_reset = not llm_request_id and sequence > 0 and prev["lastSequence"] > 0 and sequence <= prev["lastSequence"]
        # Guard against truncated content fields wiping earlier accumulated text.
        use_content = explicit_content and len(explicit_content) >= len(prev["content"])
        next_content = explicit_content if use_content else (delta if should_reset else prev["content"] + delta)
        accumulated[base_key] = {"content": next_content, "lastSequence": sequence if sequence > 0 else prev["lastSequence"]}
        chunks.append({
            "kind": kind,
            "content": next_content,
            "ts": event.get("ts", 0),
            "sequence": sequence,
            "stage": _fallback_stage(event),
            "source": str(payload.get("source", "")),
            "agentScope": agent_scope,
            "childId": child_id,
        })
    return sorted(chunks, key=lambda x: (x["ts"], x["sequence"], x["kind"]))


def _derive_active_stream(events: List[Dict[str, Any]]) -> Optional[bool]:
    active = None
    for event in events:
        et = event.get("event_type", "")
        if et in ("agent.reasoning.delta", "agent.response.delta"):
            active = True
            continue
        if et in (
            "agent.response.final", "agent.response.suppressed", "gateway.turn.short_circuited",
            "error.raised", "orchestrator.turn.completed", "orchestrator.turn.failed",
            "orchestrator.turn.ignored", "orchestrator.child.completed",
            "orchestrator.child.cancelled", "orchestrator.child.failed",
        ):
            active = False
    return active


def _build_child_tasks(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: Dict[str, Dict[str, Any]] = {}
    for event in events:
        et = event.get("event_type", "")
        if not et.startswith("orchestrator.child."):
            continue
        payload = event.get("payload", {}) or {}
        child_id = str(payload.get("child_id", "")).strip()
        if not child_id:
            continue

        existing = items.get(child_id, {
            "childId": child_id,
            "goal": _trim_text(payload.get("goal", "Background task")),
            "requestKind": str(payload.get("request_kind", "")).strip(),
            "runtimeSessionId": str(payload.get("runtime_session_id", "")).strip(),
            "runtimeSessionKey": str(payload.get("runtime_session_key", "")).strip(),
            "status": "queued",
            "startedAt": None,
            "endedAt": None,
            "durationMs": None,
            "resultPreview": "",
            "error": "",
            "reason": "",
            "summary": "",
            "lastEventAt": event.get("ts", 0),
            "eventCount": 0,
            "isActive": True,
            "wasReused": False,
        })

        next_item = dict(existing)
        next_item["goal"] = _trim_text(payload.get("goal", existing["goal"]))
        next_item["requestKind"] = str(payload.get("request_kind", existing["requestKind"])).strip()
        next_item["runtimeSessionId"] = str(payload.get("runtime_session_id", existing["runtimeSessionId"])).strip()
        next_item["runtimeSessionKey"] = str(payload.get("runtime_session_key", existing["runtimeSessionKey"])).strip()
        next_item["lastEventAt"] = max(existing["lastEventAt"], event.get("ts", 0))
        next_item["eventCount"] = existing["eventCount"] + 1

        if et == "orchestrator.child.spawned":
            next_item["status"] = payload.get("status", "running") if existing["status"] != "reused" else "reused"
            next_item["startedAt"] = existing["startedAt"] or event.get("ts", 0)
            next_item["summary"] = _trim_text(payload.get("summary", "Started background task."))
        elif et == "orchestrator.child.reused":
            next_item["status"] = "reused"
            next_item["wasReused"] = True
            next_item["startedAt"] = existing["startedAt"] or event.get("ts", 0)
            next_item["summary"] = _trim_text(payload.get("summary", "Reused an existing active child task."))
        elif et == "orchestrator.child.completed":
            next_item["status"] = "completed"
            next_item["startedAt"] = existing["startedAt"] or event.get("ts", 0)
            next_item["endedAt"] = event.get("ts", 0)
            next_item["resultPreview"] = _trim_text(payload.get("result_preview", payload.get("summary", existing["resultPreview"])))
            next_item["summary"] = next_item["resultPreview"] or _trim_text(payload.get("summary", "Child task completed."))
        elif et == "orchestrator.child.failed":
            next_item["status"] = "failed"
            next_item["startedAt"] = existing["startedAt"] or event.get("ts", 0)
            next_item["endedAt"] = event.get("ts", 0)
            next_item["error"] = _trim_text(payload.get("error", payload.get("result_preview", existing["error"] or "Background task failed.")))
            next_item["resultPreview"] = _trim_text(payload.get("result_preview", existing["resultPreview"]))
            next_item["summary"] = next_item["error"] or next_item["resultPreview"] or "Background task failed."
        elif et == "orchestrator.child.cancelled":
            next_item["status"] = "cancelled"
            next_item["startedAt"] = existing["startedAt"] or event.get("ts", 0)
            next_item["endedAt"] = event.get("ts", 0)
            next_item["reason"] = _trim_text(payload.get("reason", existing["reason"] or "cancelled"))
            next_item["summary"] = next_item["reason"] or "Child task was cancelled."

        next_item["isActive"] = next_item["status"] not in ("completed", "failed", "cancelled")
        if next_item["startedAt"] is not None and next_item["endedAt"] is not None:
            next_item["durationMs"] = max(0, (next_item["endedAt"] - next_item["startedAt"]) * 1000)
        items[child_id] = next_item

    result = list(items.values())
    result.sort(key=lambda x: (0 if x["isActive"] else 1, -x["lastEventAt"], x["childId"]))
    return result


def _build_main_orchestrator_state(events: List[Dict[str, Any]], child_tasks: List[Dict[str, Any]], trace_status: str) -> Optional[Dict[str, Any]]:
    started = _latest_of(events, "orchestrator.turn.started")
    decision = _latest_of(events, "orchestrator.decision.parsed")
    if not started and not decision:
        return None

    reused_count = sum(1 for c in child_tasks if c.get("wasReused"))
    spawn_count = decision.get("payload", {}).get("spawn_count", 0) if decision else 0
    cancel_count = decision.get("payload", {}).get("cancel_count", 0) if decision else 0
    respond_now = bool(decision.get("payload", {}).get("respond_now")) if decision else False
    ignore_message = bool(decision.get("payload", {}).get("ignore_message")) if decision else False
    used_fallback = bool(decision.get("payload", {}).get("used_fallback")) if decision else False
    immediate_reply_preview = _trim_text(decision.get("payload", {}).get("immediate_reply_preview", ""), 180) if decision else ""
    reasoning_summary = _trim_text(decision.get("payload", {}).get("reasoning_summary", ""), 180) if decision else ""
    message_preview = _trim_text(started.get("payload", {}).get("message_preview", ""), 180) if started else ""
    raw_response = str(decision.get("payload", {}).get("raw_response", "")).strip() if decision else ""

    status = "running"
    if decision:
        if ignore_message:
            status = "ignored"
        elif spawn_count > 0:
            status = "spawned"
        elif reused_count > 0:
            status = "reused"
        elif respond_now:
            status = "responded"
    elif trace_status == "error":
        status = "failed"

    summary = "Main orchestrator is evaluating the turn."
    if status == "ignored":
        summary = "Main orchestrator ignored the message."
    elif status == "spawned":
        summary = f"Main orchestrator spawned {spawn_count} child task{'s' if spawn_count != 1 else ''}."
    elif status == "reused":
        summary = f"Main orchestrator reused {reused_count} active child task{'s' if reused_count != 1 else ''}."
    elif status == "responded":
        summary = f"Main orchestrator replied directly: {immediate_reply_preview}" if immediate_reply_preview else "Main orchestrator replied directly."
    elif status == "failed":
        summary = "Main orchestrator ended with an error."
    if used_fallback:
        summary = f"{summary} Fallback heuristic was used."
    if reasoning_summary and status != "responded":
        summary = f"{summary} {reasoning_summary}".strip()

    return {
        "status": status,
        "startedAt": started.get("ts") if started else None,
        "decidedAt": decision.get("ts") if decision else None,
        "usedFallback": used_fallback,
        "respondNow": respond_now,
        "ignoreMessage": ignore_message,
        "spawnCount": spawn_count,
        "cancelCount": cancel_count,
        "reusedCount": reused_count,
        "immediateReplyPreview": immediate_reply_preview,
        "reasoningSummary": reasoning_summary,
        "messagePreview": message_preview,
        "rawResponse": raw_response,
        "summary": _trim_text(summary, 240),
    }


def _summarize_stage(key: str, stage_events: List[Dict[str, Any]], tool_calls: List[Dict[str, Any]], stream_chunks: List[Dict[str, Any]]) -> str:
    if not stage_events:
        return "No events"

    if key == "inbound":
        received = _latest_of(stage_events, "message.received")
        return _trim_text(
            (received.get("payload", {}).get("text_preview") if received else None)
            or (received.get("payload", {}).get("text") if received else None)
            or f"Received {len(stage_events)} inbound event{'s' if len(stage_events) != 1 else ''}",
            160,
        )
    elif key == "trigger":
        rejected = _latest_of(stage_events, "group.trigger.rejected")
        if rejected:
            return f"Rejected: {_trim_text(rejected.get('payload', {}).get('final_reason') or rejected.get('payload', {}).get('rejection_layer') or 'blocked', 120)}"
        evaluated = _latest_of(stage_events, "group.trigger.evaluated")
        if evaluated:
            return f"Accepted: {_trim_text(evaluated.get('payload', {}).get('trigger_reason') or 'triggered', 120)}"
        context_only = _latest_of(stage_events, "group.context_only_emitted")
        if context_only:
            return f"Context only: {_trim_text(context_only.get('payload', {}).get('trigger_reason') or 'no reply', 120)}"
        return f"{len(stage_events)} trigger event{'s' if len(stage_events) != 1 else ''}"
    elif key == "context":
        decision = _latest_of(stage_events, "orchestrator.decision.parsed")
        if decision:
            return _trim_text(
                decision.get("payload", {}).get("reasoning_summary")
                or decision.get("payload", {}).get("immediate_reply_preview")
                or "Orchestrator decision parsed",
                160,
            )
        started = _latest_of(stage_events, "agent.run.started")
        return _trim_text(
            f"Prompt captured for {started.get('payload', {}).get('model', 'model')}" if started and started.get("payload", {}).get("prompt") else started.get("payload", {}).get("model", "Context prepared") if started else "Context prepared",
            160,
        )
    elif key == "model":
        last_request = _latest_of(stage_events, "agent.model.requested")
        if stream_chunks:
            thinking_count = sum(1 for c in stream_chunks if c["kind"] == "thinking")
            response_count = sum(1 for c in stream_chunks if c["kind"] == "response")
            if thinking_count > 0 and response_count > 0:
                return f"Streaming {thinking_count} thinking + {response_count} response chunk{'s' if len(stream_chunks) != 1 else ''}"
            if thinking_count > 0:
                return f"Streaming {thinking_count} thinking chunk{'s' if thinking_count != 1 else ''}"
            if response_count > 0:
                return f"Streaming {response_count} response chunk{'s' if response_count != 1 else ''}"
        return _trim_text(last_request.get("payload", {}).get("model", "Model activity") if last_request else "Model activity", 160)
    elif key == "tools":
        return f"{len(tool_calls)} tool call{'s' if len(tool_calls) != 1 else ''}" if tool_calls else f"{len(stage_events)} tool event{'s' if len(stage_events) != 1 else ''}"
    elif key == "memory_skill_routing":
        memory_event = _latest_of(stage_events, "agent.memory.used")
        skill_event = _latest_of(stage_events, "agent.skill.used")
        return _trim_text(
            (skill_event.get("payload", {}).get("skill_name") if skill_event else None)
            or (memory_event.get("payload", {}).get("summary") if memory_event else None)
            or "Memory / skill routing activity",
            160,
        )
    elif key == "final":
        turn_ignored = _latest_of(stage_events, "orchestrator.turn.ignored")
        turn_completed = _latest_of(stage_events, "orchestrator.turn.completed")
        child_completed = _latest_of(stage_events, "orchestrator.child.completed")
        child_cancelled = _latest_of(stage_events, "orchestrator.child.cancelled")
        final_response = _latest_of(stage_events, "agent.response.final")
        suppressed = _latest_of(stage_events, "agent.response.suppressed")
        short_circuit = _latest_of(stage_events, "gateway.turn.short_circuited")
        return _trim_text(
            (turn_ignored.get("payload", {}).get("reason") if turn_ignored else None)
            or (turn_ignored.get("payload", {}).get("summary") if turn_ignored else None)
            or (turn_completed.get("payload", {}).get("summary") if turn_completed else None)
            or (turn_completed.get("payload", {}).get("response_preview") if turn_completed else None)
            or (child_completed.get("payload", {}).get("result_preview") if child_completed else None)
            or (child_completed.get("payload", {}).get("summary") if child_completed else None)
            or (child_cancelled.get("payload", {}).get("reason") if child_cancelled else None)
            or (child_cancelled.get("payload", {}).get("summary") if child_cancelled else None)
            or (final_response.get("payload", {}).get("response_preview") if final_response else None)
            or (short_circuit.get("payload", {}).get("response_preview") if short_circuit else None)
            or (suppressed.get("payload", {}).get("reason") if suppressed else None)
            or "Finalized",
            160,
        )
    elif key == "error":
        child_failed = _latest_of(stage_events, "orchestrator.child.failed")
        error_event = _latest_of(stage_events, "error.raised")
        return _trim_text(
            (child_failed.get("payload", {}).get("error") if child_failed else None)
            or (child_failed.get("payload", {}).get("result_preview") if child_failed else None)
            or (child_failed.get("payload", {}).get("summary") if child_failed else None)
            or (error_event.get("payload", {}).get("error_message") if error_event else None)
            or (error_event.get("payload", {}).get("error") if error_event else None)
            or (error_event.get("payload", {}).get("traceback_summary") if error_event else None)
            or "Error raised",
            160,
        )
    elif key == "raw":
        return f"{len(stage_events)} uncategorized event{'s' if len(stage_events) != 1 else ''}"
    return f"{len(stage_events)} event{'s' if len(stage_events) != 1 else ''}"


def _stage_status(key: str, stage_events: List[Dict[str, Any]], latest_stage: str, trace_status: str, active_stream: bool) -> str:
    if not stage_events:
        return "idle"
    if key == "error" or trace_status == "error":
        return "error" if any(e.get("event_type") in ("error.raised", "orchestrator.child.failed") for e in stage_events) else "completed"
    if latest_stage == key and (active_stream or trace_status == "in_progress"):
        return "active"
    return "completed"


# ---------------------------------------------------------------------------
# Trace timeline helpers (mirrors frontend stage-mapper.ts buildTraceTimeline)
# ---------------------------------------------------------------------------

_STANDALONE_TIMELINE_EVENT_TYPES = {
    "message.received",
    "orchestrator.turn.started",
    "orchestrator.decision.parsed",
    "orchestrator.turn.completed",
    "orchestrator.turn.ignored",
    "orchestrator.turn.failed",
    "orchestrator.child.spawned",
    "orchestrator.child.reused",
    "orchestrator.child.completed",
    "orchestrator.child.cancelled",
    "orchestrator.child.failed",
    "agent.tool.called",
    "agent.tool.completed",
    "agent.tool.failed",
    "agent.memory.called",
    "agent.memory.used",
    "agent.skill.called",
    "agent.skill.used",
    "policy.applied",
    "agent.response.final",
    "agent.response.suppressed",
    "gateway.turn.short_circuited",
    "error.raised",
}

_REQUEST_LOCAL_ONLY_EVENT_TYPES = {
    "agent.memory.used",
    "agent.skill.used",
    "policy.applied",
}


def _derive_lane_identity(event: Dict[str, Any], has_orchestrator_activity: bool, has_main_agent_lane: bool) -> Dict[str, str]:
    payload = event.get("payload") or {}
    agent_scope = str(payload.get("agent_scope", "")).strip()
    child_id = str(payload.get("child_id", "")).strip()
    event_type = str(event.get("event_type", ""))

    if child_id:
        return {"key": f"child:{child_id}", "title": f"Child {child_id}", "agentScope": agent_scope or "orchestrator_child", "childId": child_id}
    if agent_scope == "main_orchestrator" or (event_type.startswith("orchestrator.") and not event_type.startswith("orchestrator.child.")):
        return {"key": "main_orchestrator", "title": "Main Orchestrator", "agentScope": "main_orchestrator", "childId": ""}
    if event_type == "message.received" and has_orchestrator_activity:
        return {"key": "main_orchestrator", "title": "Main Orchestrator", "agentScope": "main_orchestrator", "childId": ""}
    if not agent_scope and has_orchestrator_activity and not has_main_agent_lane and event_type in ("agent.response.final", "agent.response.suppressed", "gateway.turn.short_circuited"):
        return {"key": "main_orchestrator", "title": "Main Orchestrator", "agentScope": "main_orchestrator", "childId": ""}
    return {"key": "main_agent", "title": "Main Agent", "agentScope": agent_scope or "main_agent", "childId": ""}


def _parse_non_negative_number(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:  # NaN
        return None
    return parsed


def _resolve_duration_ms(payload: Dict[str, Any]) -> Any:
    """Return duration_ms from payload, falling back to payload.timing.duration_ms."""
    dur = payload.get("duration_ms")
    if dur is not None:
        return dur
    timing = payload.get("timing") or {}
    return timing.get("duration_ms")


def _normalize_comparable_text(value: str) -> str:
    return " ".join(value.split()).strip()


def _trim_payload_preview(value: Any, max_chars: int = 220) -> str:
    try:
        if value is None:
            return ""
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        text = text.strip()
        return text[:max_chars]
    except Exception:
        return str(value or "").strip()[:max_chars]


def _event_summary(event: Dict[str, Any]) -> str:
    event_type = str(event.get("event_type", ""))
    payload = event.get("payload") or {}
    if event_type == "message.received":
        return _trim_text(
            str(payload.get("text_preview") or payload.get("text") or payload.get("raw_message") or "Inbound message"),
            180,
        )
    if event_type == "orchestrator.turn.started":
        return _trim_text(str(payload.get("message_preview") or "Main orchestrator started evaluating the turn."), 180)
    if event_type == "orchestrator.decision.parsed":
        return _trim_text(
            str(payload.get("reasoning_summary") or payload.get("immediate_reply_preview") or "Main orchestrator parsed a routing decision."),
            180,
        )
    if event_type == "orchestrator.turn.ignored":
        return _trim_text(str(payload.get("reason") or payload.get("summary") or "Main orchestrator ignored the message."), 180)
    if event_type == "orchestrator.turn.completed":
        return _trim_text(str(payload.get("summary") or payload.get("response_preview") or "Main orchestrator finished the turn."), 180)
    if event_type in ("orchestrator.child.spawned", "orchestrator.child.reused", "orchestrator.child.completed", "orchestrator.child.cancelled", "orchestrator.child.failed"):
        return _trim_text(
            str(payload.get("summary") or payload.get("result_preview") or payload.get("error") or payload.get("goal") or payload.get("reason") or event_type),
            180,
        )
    if event_type == "agent.tool.called":
        return _trim_text(str(payload.get("tool_name") or "Tool called"), 180)
    if event_type == "agent.tool.completed":
        return _trim_text(str(payload.get("result_preview") or payload.get("tool_name") or "Tool completed"), 180)
    if event_type == "agent.tool.failed":
        return _trim_text(str(payload.get("error") or payload.get("tool_name") or "Tool failed"), 180)
    if event_type == "agent.memory.called":
        return _trim_text(str(payload.get("summary") or payload.get("source") or "Memory prefetch started"), 180)
    if event_type == "agent.memory.used":
        return _trim_text(str(payload.get("summary") or payload.get("source") or "Memory activity"), 180)
    if event_type == "agent.skill.called":
        return _trim_text(str(payload.get("summary") or payload.get("skill_name") or "Skill call started"), 180)
    if event_type == "agent.skill.used":
        return _trim_text(str(payload.get("summary") or payload.get("skill_name") or "Skill activity"), 180)
    if event_type == "policy.applied":
        return _trim_text(str(payload.get("summary") or payload.get("policy_name") or "Policy applied"), 180)
    if event_type == "agent.response.final":
        return _trim_text(str(payload.get("response_preview") or "Final response emitted"), 180)
    if event_type == "agent.response.suppressed":
        return _trim_text(str(payload.get("reason") or "Response suppressed"), 180)
    if event_type == "error.raised":
        return _trim_text(
            str(payload.get("error_message") or payload.get("error") or payload.get("traceback_summary") or "Error raised"),
            180,
        )
    return _trim_text(str(payload.get("summary") or payload.get("preview") or event_type), 180)


def _timeline_entry_kind(event_type: str) -> str:
    if event_type == "message.received":
        return "inbound"
    if event_type in ("agent.memory.called", "agent.memory.used"):
        return "memory"
    if event_type in ("agent.skill.called", "agent.skill.used"):
        return "skill"
    if event_type == "policy.applied":
        return "policy"
    if event_type in ("agent.response.final", "agent.response.suppressed"):
        return "final"
    if event_type == "error.raised" or event_type.endswith(".failed"):
        return "error"
    if event_type.startswith("orchestrator."):
        return "orchestrator"
    if event_type.startswith("agent.tool."):
        return "tool"
    return "event"


def _derive_ttft_ms(started_at: float, event_ts: float) -> float:
    return max(0, round((event_ts - started_at) * 1000))


def _apply_thinking_delta(block: Dict[str, Any], event: Dict[str, Any]) -> None:
    payload = event.get("payload") or {}
    content = payload.get("content") if isinstance(payload.get("content"), str) else ""
    delta = payload.get("delta") if isinstance(payload.get("delta"), str) else ""
    sequence = _parse_non_negative_number(payload.get("sequence"))
    if sequence is None:
        sequence = len(block.get("thinkingSteps", [])) + 1

    current_full = block.get("thinkingFullText") or block.get("thinkingText") or ""
    next_full = current_full
    next_source = "delta"

    # Only treat content as a full replacement when it is at least as long as
    # the current accumulated text. This guards against truncated or malformed
    # content fields accidentally wiping earlier reasoning.
    if content and len(content) >= len(current_full):
        if not current_full or content.startswith(current_full):
            next_full = content
            next_source = "suffix"
        elif _normalize_comparable_text(content) == _normalize_comparable_text(current_full):
            next_full = current_full
            next_source = "suffix"
        else:
            next_full = content
            next_source = "reset"
    elif delta.strip():
        next_full = current_full + delta

    block["thinkingFullText"] = next_full
    block["thinkingText"] = next_full
    comparable = _normalize_comparable_text(next_full)
    if not comparable:
        block["thinkingSteps"] = []
        block["thinkingStepCount"] = 0
        return
    block["thinkingSteps"] = [{"sequence": int(sequence), "ts": event.get("ts", 0), "text": next_full.strip(), "source": next_source}]
    block["thinkingStepCount"] = 1


def _append_stream_text(existing: str, event: Dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    full_content = str(payload.get("content", "")).strip()
    if full_content:
        return full_content
    delta = str(payload.get("delta", "")).strip()
    if not delta:
        return existing
    return existing + delta


def _build_trace_timeline(trace: Dict[str, Any], events: List[Dict[str, Any]], headline: str) -> Dict[str, Any]:
    """Build a full TraceTimeline with requestBlocks and timelineEntries."""
    lanes: Dict[str, Dict[str, Any]] = {}
    request_blocks_by_lane: Dict[str, Dict[str, Dict[str, Any]]] = {}
    lane_order: Dict[str, int] = {}
    open_request_ids: Dict[str, str] = {}
    recent_request_ids: Dict[str, str] = {}
    request_order = 0

    # Open-operation tracking for lifecycle timeline entries
    open_tool_calls: Dict[str, Dict[str, Any]] = {}
    open_orchestrator_turns: Dict[str, Any] = {}
    open_child_tasks: Dict[str, Any] = {}
    open_memory_ops: Dict[str, Any] = {}
    open_skill_ops: Dict[str, Any] = {}

    has_orchestrator_activity = any(
        str(e.get("event_type", "")).startswith("orchestrator.") and not str(e.get("event_type", "")).startswith("orchestrator.child.")
        for e in events
    )
    trace_status = str(trace.get("status", ""))

    def ensure_lane(event: Dict[str, Any]) -> Dict[str, Any]:
        identity = _derive_lane_identity(event, has_orchestrator_activity, "main_agent" in lanes)
        key = identity["key"]
        existing = lanes.get(key)
        if existing:
            existing["agentScope"] = existing.get("agentScope") or identity["agentScope"]
            existing["childId"] = existing.get("childId") or identity["childId"]
            if event.get("ts", 0) < (existing.get("startedAt") or float("inf")):
                existing["startedAt"] = event.get("ts", 0)
            if event.get("ts", 0) > (existing.get("endedAt") or 0):
                existing["endedAt"] = event.get("ts", 0)
            existing["rawEvents"].append(event)
            return existing
        lane = {
            "key": key,
            "title": identity["title"],
            "agentScope": identity["agentScope"],
            "childId": identity["childId"],
            "model": "",
            "provider": "",
            "startedAt": event.get("ts", 0),
            "endedAt": event.get("ts", 0),
            "status": "idle",
            "requestBlocks": [],
            "timelineEntries": [],
            "rawEvents": [event],
        }
        lanes[key] = lane
        lane_order[key] = len(lane_order)
        return lane

    def resolve_request_id(lane_key: str, event: Dict[str, Any]) -> Tuple[Optional[str], str]:
        payload = event.get("payload") or {}
        explicit = str(payload.get("llm_request_id", "")).strip()
        event_type = str(event.get("event_type", ""))
        if explicit:
            if event_type == "agent.model.requested":
                open_request_ids[lane_key] = explicit
            recent_request_ids[lane_key] = explicit
            return explicit, "exact"
        if event_type == "agent.model.requested":
            synthetic = f"legacy:{lane_key}:requested:{event.get('event_id', '')}"
            open_request_ids[lane_key] = synthetic
            recent_request_ids[lane_key] = synthetic
            return synthetic, "legacy"
        current_open = open_request_ids.get(lane_key, "")
        current_recent = recent_request_ids.get(lane_key, "")
        if current_open:
            return current_open, "legacy" if current_open.startswith("legacy:") else "exact"
        if current_recent:
            return current_recent, "legacy" if current_recent.startswith("legacy:") else "exact"
        if event_type in ("agent.tool.called", "agent.tool.completed", "agent.tool.failed", "agent.reasoning.delta", "agent.response.delta", "agent.model.completed"):
            synthetic = f"legacy:{lane_key}:implicit:{event.get('event_id', '')}"
            recent_request_ids[lane_key] = synthetic
            return synthetic, "legacy"
        return None, "legacy"

    def ensure_request_block(lane_key: str, request_id: str, event: Dict[str, Any], correlation_mode: str) -> Dict[str, Any]:
        nonlocal request_order
        by_lane = request_blocks_by_lane.setdefault(lane_key, {})
        existing = by_lane.get(request_id)
        if existing:
            existing["rawEvents"].append(event)
            return existing
        block = {
            "id": request_id,
            "correlationMode": correlation_mode,
            "laneKey": lane_key,
            "agentScope": "",
            "childId": "",
            "model": "",
            "provider": "",
            "startedAt": event.get("ts", 0),
            "endedAt": None,
            "ttftMs": None,
            "durationMs": None,
            "status": "running",
            "requestBody": "",
            "requestPreview": "",
            "thinkingText": "",
            "thinkingFullText": "",
            "thinkingSteps": [],
            "thinkingStepCount": 0,
            "responseText": "",
            "toolCalls": [],
            "memoryEvents": [],
            "skillEvents": [],
            "policyEvents": [],
            "rawEvents": [event],
            "usage": {
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
                "reasoningTokens": 0,
                "promptTokens": 0,
                "totalTokens": 0,
                "apiCalls": 0,
            },
            "_order": request_order,
            "_streaming": None,
        }
        request_order += 1
        by_lane[request_id] = block
        return block

    for event in events:
        lane = ensure_lane(event)
        event_type = str(event.get("event_type", ""))

        # Update lane status
        if event_type.endswith(".failed") or event_type == "error.raised":
            lane["status"] = "error"
        elif trace_status == "completed" and lane.get("status") != "error":
            lane["status"] = "completed"
        else:
            lane["status"] = "running"

        request_id, correlation_mode = resolve_request_id(lane["key"], event)
        if request_id:
            block = ensure_request_block(lane["key"], request_id, event, correlation_mode)
            block["agentScope"] = block.get("agentScope") or lane.get("agentScope", "")
            block["childId"] = block.get("childId") or lane.get("childId", "")
            block["model"] = block.get("model") or str(event.get("payload", {}).get("model", ""))
            block["provider"] = block.get("provider") or str(event.get("payload", {}).get("provider", ""))
            if event.get("ts", 0) < block["startedAt"]:
                block["startedAt"] = event.get("ts", 0)
            if not lane.get("model") and block["model"]:
                lane["model"] = block["model"]
            if not lane.get("provider") and block["provider"]:
                lane["provider"] = block["provider"]

            payload = event.get("payload") or {}
            if event_type == "agent.model.requested":
                block["requestBody"] = block.get("requestBody") or str(payload.get("request_body", ""))
                block["requestPreview"] = block.get("requestPreview") or _trim_payload_preview(payload.get("request_preview") or payload.get("request_body"))
                if isinstance(payload.get("streaming"), bool):
                    block["_streaming"] = payload["streaming"]
                block["status"] = "running"
                open_request_ids[lane["key"]] = request_id
                recent_request_ids[lane["key"]] = request_id
            elif event_type == "agent.model.completed":
                block["endedAt"] = event.get("ts", 0)
                duration = _parse_non_negative_number(_resolve_duration_ms(payload))
                block["durationMs"] = duration if duration is not None else max(0, (event.get("ts", 0) - block["startedAt"]) * 1000)
                ttft = _parse_non_negative_number(payload.get("ttft_ms")) or _parse_non_negative_number(payload.get("time_to_first_token_ms"))
                block["ttftMs"] = ttft if ttft is not None else block.get("ttftMs")
                if block["ttftMs"] is None and block.get("_streaming") is False:
                    block["ttftMs"] = block["durationMs"]
                block["status"] = "failed" if payload.get("success") is False else "completed"
                block["usage"] = {
                    "inputTokens": max(0, int(payload.get("input_tokens", 0) or 0)),
                    "outputTokens": max(0, int(payload.get("output_tokens", 0) or 0)),
                    "cacheReadTokens": max(0, int(payload.get("cache_read_tokens", 0) or 0)),
                    "cacheWriteTokens": max(0, int(payload.get("cache_write_tokens", 0) or 0)),
                    "reasoningTokens": max(0, int(payload.get("reasoning_tokens", 0) or 0)),
                    "promptTokens": max(0, int(payload.get("prompt_tokens", 0) or 0)),
                    "totalTokens": max(0, int(payload.get("total_tokens", 0) or 0)),
                    "apiCalls": max(0, int(payload.get("api_calls", 1) or 0)),
                }
                if open_request_ids.get(lane["key"]) == request_id:
                    del open_request_ids[lane["key"]]
            elif event_type == "agent.reasoning.delta":
                if block.get("ttftMs") is None:
                    block["ttftMs"] = _derive_ttft_ms(block["startedAt"], event.get("ts", 0))
                _apply_thinking_delta(block, event)
            elif event_type == "agent.response.delta":
                if block.get("ttftMs") is None:
                    block["ttftMs"] = _derive_ttft_ms(block["startedAt"], event.get("ts", 0))
                block["responseText"] = _append_stream_text(block.get("responseText", ""), event)
            elif event_type == "agent.memory.used":
                block["memoryEvents"].append(event)
            elif event_type == "agent.skill.used":
                block["skillEvents"].append(event)
            elif event_type == "policy.applied":
                block["policyEvents"].append(event)
            elif event_type == "agent.response.final":
                if not block.get("responseText"):
                    block["responseText"] = str(payload.get("response_preview", ""))

        request_local_only = bool(
            request_id and event_type in _REQUEST_LOCAL_ONLY_EVENT_TYPES
        )

        def _make_timeline_entry(evt: Dict[str, Any], req_id: Optional[str], st: str) -> Dict[str, Any]:
            return {
                "id": evt.get("event_id", ""),
                "kind": _timeline_entry_kind(event_type),
                "ts": evt.get("ts", 0),
                "startedAt": evt.get("ts", 0),
                "endedAt": evt.get("ts", 0) if st != "running" else None,
                "laneKey": lane["key"],
                "llmRequestId": req_id,
                "title": event_type,
                "summary": _event_summary(evt),
                "status": st,
                "durationMs": _parse_non_negative_number(evt.get("payload", {}).get("duration_ms")),
                "payloadPreview": _trim_payload_preview(evt.get("payload")),
                "event": evt,
            }

        if event_type in _STANDALONE_TIMELINE_EVENT_TYPES and not request_local_only:
            payload = event.get("payload") or {}

            # Tool call lifecycle
            if event_type == "agent.tool.called":
                tool_call_id = str(payload.get("tool_call_id", ""))
                entry = _make_timeline_entry(event, request_id, "running")
                open_tool_calls.setdefault(lane["key"], {})[tool_call_id] = entry
            elif event_type == "agent.tool.completed":
                tool_call_id = str(payload.get("tool_call_id", ""))
                open_entry = open_tool_calls.get(lane["key"], {}).pop(tool_call_id, None)
                if open_entry:
                    open_entry["status"] = "completed"
                    open_entry["endedAt"] = event.get("ts", 0)
                    dur = _parse_non_negative_number(_resolve_duration_ms(payload))
                    open_entry["durationMs"] = dur if dur is not None else max(0, (event.get("ts", 0) - open_entry["startedAt"]) * 1000)
                    open_entry["title"] = event_type
                    open_entry["summary"] = _event_summary(event)
                    lane["timelineEntries"].append(open_entry)
                else:
                    lane["timelineEntries"].append(_make_timeline_entry(event, request_id, "completed"))
            elif event_type == "agent.tool.failed":
                tool_call_id = str(payload.get("tool_call_id", ""))
                open_entry = open_tool_calls.get(lane["key"], {}).pop(tool_call_id, None)
                if open_entry:
                    open_entry["status"] = "failed"
                    open_entry["endedAt"] = event.get("ts", 0)
                    dur = _parse_non_negative_number(_resolve_duration_ms(payload))
                    open_entry["durationMs"] = dur if dur is not None else max(0, (event.get("ts", 0) - open_entry["startedAt"]) * 1000)
                    open_entry["title"] = event_type
                    open_entry["summary"] = _event_summary(event)
                    lane["timelineEntries"].append(open_entry)
                else:
                    lane["timelineEntries"].append(_make_timeline_entry(event, request_id, "failed"))

            # Orchestrator turn lifecycle
            elif event_type == "orchestrator.turn.started":
                entry = _make_timeline_entry(event, request_id, "running")
                prev_entry = open_orchestrator_turns.pop(lane["key"], None)
                if prev_entry:
                    lane["timelineEntries"].append(prev_entry)
                open_orchestrator_turns[lane["key"]] = entry
            elif event_type in ("orchestrator.turn.completed", "orchestrator.turn.ignored", "orchestrator.turn.failed"):
                open_entry = open_orchestrator_turns.pop(lane["key"], None)
                status_map = {
                    "orchestrator.turn.completed": "completed",
                    "orchestrator.turn.ignored": "ignored",
                    "orchestrator.turn.failed": "failed",
                }
                if open_entry:
                    open_entry["status"] = status_map[event_type]
                    open_entry["endedAt"] = event.get("ts", 0)
                    dur = _parse_non_negative_number(_resolve_duration_ms(payload))
                    open_entry["durationMs"] = dur if dur is not None else max(0, (event.get("ts", 0) - open_entry["startedAt"]) * 1000)
                    open_entry["title"] = event_type
                    open_entry["summary"] = _event_summary(event)
                    lane["timelineEntries"].append(open_entry)
                else:
                    lane["timelineEntries"].append(_make_timeline_entry(event, request_id, status_map[event_type]))

            # Child task lifecycle
            elif event_type == "orchestrator.child.spawned":
                child_id = str(payload.get("child_id", ""))
                if child_id:
                    entry = _make_timeline_entry(event, request_id, "running")
                    open_child_tasks[child_id] = entry
                else:
                    lane["timelineEntries"].append(_make_timeline_entry(event, request_id, "completed"))
            elif event_type in ("orchestrator.child.completed", "orchestrator.child.cancelled", "orchestrator.child.failed"):
                child_id = str(payload.get("child_id", ""))
                open_entry = open_child_tasks.pop(child_id, None) if child_id else None
                status_map = {
                    "orchestrator.child.completed": "completed",
                    "orchestrator.child.cancelled": "cancelled",
                    "orchestrator.child.failed": "failed",
                }
                if open_entry:
                    open_entry["status"] = status_map[event_type]
                    open_entry["endedAt"] = event.get("ts", 0)
                    dur = _parse_non_negative_number(_resolve_duration_ms(payload))
                    open_entry["durationMs"] = dur if dur is not None else max(0, (event.get("ts", 0) - open_entry["startedAt"]) * 1000)
                    open_entry["title"] = event_type
                    open_entry["summary"] = _event_summary(event)
                    lane["timelineEntries"].append(open_entry)
                else:
                    lane["timelineEntries"].append(_make_timeline_entry(event, request_id, status_map[event_type]))

            # Memory lifecycle
            elif event_type == "agent.memory.called":
                entry = _make_timeline_entry(event, request_id, "running")
                open_memory_ops[lane["key"]] = entry
            elif event_type == "agent.memory.used":
                open_entry = open_memory_ops.pop(lane["key"], None)
                if open_entry:
                    open_entry["status"] = "completed"
                    open_entry["endedAt"] = event.get("ts", 0)
                    dur = _parse_non_negative_number(_resolve_duration_ms(payload))
                    open_entry["durationMs"] = dur if dur is not None else max(0, (event.get("ts", 0) - open_entry["startedAt"]) * 1000)
                    open_entry["title"] = event_type
                    open_entry["summary"] = _event_summary(event)
                    lane["timelineEntries"].append(open_entry)
                else:
                    lane["timelineEntries"].append(_make_timeline_entry(event, request_id, "completed"))

            # Skill lifecycle
            elif event_type == "agent.skill.called":
                entry = _make_timeline_entry(event, request_id, "running")
                open_skill_ops[lane["key"]] = entry
            elif event_type == "agent.skill.used":
                open_entry = open_skill_ops.pop(lane["key"], None)
                if open_entry:
                    open_entry["status"] = "completed"
                    open_entry["endedAt"] = event.get("ts", 0)
                    dur = _parse_non_negative_number(_resolve_duration_ms(payload))
                    open_entry["durationMs"] = dur if dur is not None else max(0, (event.get("ts", 0) - open_entry["startedAt"]) * 1000)
                    open_entry["title"] = event_type
                    open_entry["summary"] = _event_summary(event)
                    lane["timelineEntries"].append(open_entry)
                else:
                    lane["timelineEntries"].append(_make_timeline_entry(event, request_id, "completed"))

            # All other standalone events (single-shot, no lifecycle)
            else:
                lane["timelineEntries"].append(_make_timeline_entry(event, request_id,
                    "error" if event_type.endswith(".failed") or event_type == "error.raised" else "completed"))

    # Flush remaining open entries
    for lane_key, entry in open_orchestrator_turns.items():
        if lane_key in lanes:
            if trace_status == "completed" and entry["status"] == "running":
                entry["status"] = "completed"
                entry["endedAt"] = entry.get("startedAt", 0)
            lanes[lane_key]["timelineEntries"].append(entry)
    for lane_key, tools in open_tool_calls.items():
        if lane_key in lanes:
            for entry in tools.values():
                lanes[lane_key]["timelineEntries"].append(entry)
    for child_id, entry in open_child_tasks.items():
        lane_key = entry.get("laneKey", "")
        if lane_key in lanes:
            lanes[lane_key]["timelineEntries"].append(entry)
    for lane_key, entry in open_memory_ops.items():
        if lane_key in lanes:
            if trace_status == "completed" and entry["status"] == "running":
                entry["status"] = "completed"
                entry["endedAt"] = entry.get("startedAt", 0)
            lanes[lane_key]["timelineEntries"].append(entry)
    for lane_key, entry in open_skill_ops.items():
        if lane_key in lanes:
            if trace_status == "completed" and entry["status"] == "running":
                entry["status"] = "completed"
                entry["endedAt"] = entry.get("startedAt", 0)
            lanes[lane_key]["timelineEntries"].append(entry)

    # Post-process lanes
    lane_list = []
    for lane in lanes.values():
        by_lane = request_blocks_by_lane.get(lane["key"], {})
        blocks = sorted(
            by_lane.values(),
            key=lambda b: (b["startedAt"], b["_order"]),
        )
        # Build tool calls per block
        for block in blocks:
            block["toolCalls"] = _build_tool_calls(block["rawEvents"])
            # Strip internal fields
            block.pop("_order", None)
            block.pop("_streaming", None)

        request_entries = []
        for block in blocks:
            request_entries.append({
                "id": f"{lane['key']}:{block['id']}",
                "kind": "llm_request",
                "ts": block["startedAt"],
                "startedAt": block["startedAt"],
                "endedAt": block["endedAt"],
                "laneKey": lane["key"],
                "llmRequestId": block["id"],
                "title": block["model"] or "LLM request",
                "summary": _trim_text(
                    block["responseText"] or block["thinkingText"] or block["requestPreview"] or block["requestBody"] or "Model invocation",
                    180,
                ),
                "status": block["status"],
                "durationMs": block["durationMs"],
                "payloadPreview": _trim_payload_preview({
                    "requestBody": block["requestBody"],
                    "thinking": block["thinkingText"],
                    "response": block["responseText"],
                }),
                "event": block["rawEvents"][0] if block["rawEvents"] else None,
            })

        timeline_entries = sorted(
            lane["timelineEntries"] + request_entries,
            key=lambda e: (e["ts"], e["id"]),
        )

        raw_events = lane["rawEvents"]
        last_event = raw_events[-1] if raw_events else None
        is_ignored = any(str(e.get("event_type", "")) == "orchestrator.turn.ignored" for e in raw_events)
        has_failed = any(str(e.get("event_type", "")).endswith(".failed") or str(e.get("event_type", "")) == "error.raised" for e in raw_events)
        has_requested = any(str(e.get("event_type", "")) == "agent.model.requested" for e in raw_events)
        has_completed = any(str(e.get("event_type", "")) == "agent.model.completed" for e in raw_events)

        lane_status = "error" if has_failed else ("ignored" if is_ignored else ("running" if has_requested and not has_completed else "completed"))

        lane_list.append({
            **lane,
            "status": lane_status,
            "endedAt": last_event.get("ts", 0) if last_event else lane.get("endedAt"),
            "requestBlocks": [{k: v for k, v in b.items() if not k.startswith("_")} for b in blocks],
            "timelineEntries": timeline_entries,
        })

    lane_list = [l for l in lane_list if l.get("requestBlocks") or l.get("timelineEntries")]
    lane_list.sort(key=lambda x: (
        0 if x["key"] == "main_orchestrator" else 1 if x["key"] == "main_agent" else 2,
        x.get("startedAt") or 0,
        x["key"],
    ))

    return {
        "traceId": trace.get("trace_id", ""),
        "sessionKey": trace.get("session_key", ""),
        "headline": headline,
        "status": trace_status,
        "lastEventAt": events[-1].get("ts", 0) if events else trace.get("ended_at") or trace.get("started_at", 0),
        "requestCount": sum(len(l.get("requestBlocks", [])) for l in lane_list),
        "laneCount": len(lane_list),
        "lanes": lane_list,
    }


def _derive_headline(trace: Dict[str, Any], events: List[Dict[str, Any]], child_tasks: List[Dict[str, Any]]) -> Tuple[str, str]:
    child_error = next((c for c in child_tasks if c["status"] == "failed" and c["summary"]), None)
    if child_error:
        return child_error["summary"], "child_error"

    child_result = next((c for c in child_tasks if c["status"] == "completed" and c["summary"]), None)
    if child_result:
        return child_result["summary"], "child_result"

    final_response = _latest_of(events, "agent.response.final")
    if final_response and final_response.get("payload", {}).get("response_preview"):
        return str(final_response["payload"]["response_preview"]), "response"

    error_event = _latest_of(events, "error.raised")
    if error_event:
        return str(
            error_event.get("payload", {}).get("error_message")
            or error_event.get("payload", {}).get("error")
            or error_event.get("payload", {}).get("traceback_summary")
            or "Error raised"
        ), "error"

    inbound = _latest_of(events, "message.received")

    context_only = _latest_of(events, "group.context_only_emitted")
    if context_only:
        _co_reason = str(
            context_only.get("payload", {}).get("trigger_reason")
            or context_only.get("payload", {}).get("reason")
            or "no reply"
        ).strip()
        _co_preview = str(
            inbound.get("payload", {}).get("text_preview")
            or inbound.get("payload", {}).get("text")
            or "[image attachment]"
        ).strip() if inbound else "[image attachment]"
        return f"Context only: {_co_preview} ({_co_reason})", "context_only"

    if inbound:
        return str(
            inbound.get("payload", {}).get("text_preview")
            or inbound.get("payload", {}).get("text")
            or inbound.get("payload", {}).get("raw_message")
            or "Inbound message"
        ), "inbound"

    summary_headline = str(trace.get("summary", {}).get("headline", "")).strip()
    if summary_headline:
        return summary_headline, "summary"

    trace_created = _latest_of(events, "trace.created")
    if trace_created and (trace.get("chat_id") or trace.get("message_id") or trace.get("user_id")):
        chat_type = str(trace.get("chat_type", trace_created.get("chat_type", ""))).strip().lower()
        return ("Inbound group message received" if chat_type == "group" else "Inbound message received"), "trace"

    return "No trace headline available", "empty"


def build_live_trace_group(trace: Dict[str, Any], raw_events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a LiveTraceGroup view model from a trace dict and its events."""
    events = _sort_events(raw_events)

    grouped_events: Dict[str, List[Dict[str, Any]]] = {stage: [] for stage in STAGE_ORDER}
    for event in events:
        stage = _fallback_stage(event)
        grouped_events.setdefault(stage, []).append(event)

    stream_chunks = _build_stream_chunks(events)
    tool_calls = _build_tool_calls(events)
    child_tasks = _build_child_tasks(events)
    active_children = [c for c in child_tasks if c["isActive"]]
    recent_children = [c for c in child_tasks if not c["isActive"]]
    main_orchestrator = _build_main_orchestrator_state(events, child_tasks, trace.get("status", ""))
    derived_active_stream = _derive_active_stream(events)
    active_stream = derived_active_stream if derived_active_stream is not None else bool(trace.get("active_stream"))

    summary_stage = str(trace.get("latest_stage", "")).strip()
    latest_stage = summary_stage if summary_stage in STAGE_ORDER else "raw"
    if latest_stage == "raw" and events:
        for event in events:
            stage = _fallback_stage(event)
            if STAGE_PRIORITY.get(stage, 0) >= STAGE_PRIORITY.get(latest_stage, 0):
                latest_stage = stage

    headline, headline_source = _derive_headline(trace, events, child_tasks)
    last_event_at = events[-1].get("ts", 0) if events else trace.get("ended_at") or trace.get("started_at", 0)
    ended_at = trace.get("ended_at") or (None if trace.get("status") == "in_progress" else last_event_at)

    stages = []
    for key in STAGE_ORDER:
        stage_events = grouped_events.get(key, [])
        if key == "raw" and not stage_events:
            continue
        stages.append({
            "key": key,
            "title": STAGE_TITLES[key],
            "status": _stage_status(key, stage_events, latest_stage, trace.get("status", ""), active_stream),
            "summary": _summarize_stage(key, stage_events, tool_calls, stream_chunks),
            "events": stage_events,
        })

    return {
        "traceId": trace.get("trace_id", ""),
        "status": trace.get("status", ""),
        "startedAt": trace.get("started_at", 0),
        "endedAt": ended_at,
        "durationMs": (ended_at - trace["started_at"]) * 1000 if ended_at is not None and trace.get("started_at") else None,
        "latestStage": latest_stage,
        "activeStream": active_stream,
        "eventCount": trace.get("event_count", len(events)),
        "toolCallCount": trace.get("tool_call_count", len(tool_calls)),
        "errorCount": trace.get("error_count", sum(1 for e in events if e.get("event_type") in ("error.raised", "orchestrator.child.failed"))),
        "model": str(trace.get("model") or trace.get("summary", {}).get("model") or next((e.get("payload", {}).get("model", "") for e in events if e.get("payload", {}).get("model")), "")),
        "headline": headline,
        "headlineSource": headline_source,
        "lastEventAt": last_event_at,
        "mainOrchestrator": main_orchestrator,
        "childTasks": child_tasks,
        "childDetailsById": {},  # Simplified — child details omitted for brevity
        "activeChildren": active_children,
        "recentChildren": recent_children,
        "stages": stages,
        "toolCalls": tool_calls,
        "streamChunks": stream_chunks,
        "rawEvents": events,
        "timeline": _build_trace_timeline(trace, events, headline),
        "trace": trace,
    }

