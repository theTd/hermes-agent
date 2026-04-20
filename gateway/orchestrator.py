"""
Gateway orchestrator session models and decision parsing helpers.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


_ORCH_COMPLEXITY_MARKER_RE = re.compile(
    r"\[\[\s*ORCH_COMPLEXITY\s*:\s*(100|[1-9]?\d)\s*\]\]",
    flags=re.IGNORECASE,
)
_ORCH_ROUTING_SUMMARY_PREFIX = "Routing decision"
_ORCH_ROUTING_FIELD_RE = re.compile(
    r"(?:^|;\s)(?P<key>[a-z_]+)=(?P<value>.*?)(?=(?:;\s[a-z_]+=)|$)",
    flags=re.IGNORECASE,
)


def new_child_id() -> str:
    return f"child_{uuid.uuid4().hex[:12]}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_dedupe_key(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return re.sub(r"[^a-z0-9\u4e00-\u9fff ]+", "", text)


def _lcs_ratio(a: str, b: str) -> float:
    """Return the longest-common-subsequence ratio of the shorter string."""
    if not a or not b:
        return 0.0
    # Simple DP for LCS (inputs are short dedupe keys)
    m, n = len(a), len(b)
    prev = [0] * (n + 1)
    curr = [0] * (n + 1)
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev, curr = curr, prev
    lcs_len = prev[n]
    return lcs_len / min(m, n)


def dedupe_keys_similar(left: str, right: str) -> bool:
    left_key = normalize_dedupe_key(left)
    right_key = normalize_dedupe_key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True

    shorter, longer = (
        (left_key, right_key) if len(left_key) <= len(right_key) else (right_key, left_key)
    )
    if len(shorter) >= 12 and shorter in longer:
        return True

    # Character-level LCS for CJK / short phrases without word boundaries
    if _lcs_ratio(left_key, right_key) >= 0.75:
        return True

    left_tokens = {token for token in left_key.split(" ") if token}
    right_tokens = {token for token in right_key.split(" ") if token}
    if left_tokens and right_tokens:
        overlap = left_tokens & right_tokens
        min_tokens = min(len(left_tokens), len(right_tokens))
        if min_tokens and len(overlap) >= 2 and (len(overlap) / min_tokens) >= 0.75:
            return True
    return False


@dataclass
class OrchestratorChildRequest:
    goal: str
    complexity_score: Optional[int] = None
    model_tier: str = ""
    turn_model_override: Dict[str, Any] = field(default_factory=dict)
    allowed_toolsets: List[str] = field(default_factory=list)
    may_reply_directly: bool = True
    reply_style: str = ""
    priority: str = "normal"
    request_kind: str = "analysis"
    dedupe_key: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OrchestratorChildRequest":
        goal = str(data.get("goal") or "").strip()
        if not goal:
            raise ValueError("spawn_children item missing goal")
        return cls(
            goal=goal,
            complexity_score=_normalize_complexity_score(
                data.get("complexity_score"),
                default=None,
                minimum=0,
            ),
            model_tier=str(data.get("model_tier") or "").strip(),
            turn_model_override=dict(data.get("turn_model_override") or {}),
            allowed_toolsets=[
                str(item).strip()
                for item in (data.get("allowed_toolsets") or [])
                if str(item).strip()
            ],
            may_reply_directly=bool(data.get("may_reply_directly", True)),
            reply_style=str(data.get("reply_style") or "").strip(),
            priority=str(data.get("priority") or "normal").strip() or "normal",
            request_kind=str(data.get("request_kind") or "analysis").strip() or "analysis",
            dedupe_key=str(data.get("dedupe_key") or "").strip(),
        )


@dataclass
class OrchestratorDecision:
    complexity_score: int = 100
    respond_now: bool = False
    immediate_reply: str = ""
    spawn_handoff_reply: str = ""
    spawn_children: List[OrchestratorChildRequest] = field(default_factory=list)
    cancel_children: List[str] = field(default_factory=list)
    ignore_message: bool = False
    reasoning_summary: str = ""
    raw_response: str = ""
    used_fallback: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "complexity_score": self.complexity_score,
            "respond_now": self.respond_now,
            "immediate_reply": self.immediate_reply,
            "spawn_handoff_reply": self.spawn_handoff_reply,
            "spawn_children": [item.__dict__ for item in self.spawn_children],
            "cancel_children": list(self.cancel_children),
            "ignore_message": self.ignore_message,
            "reasoning_summary": self.reasoning_summary,
            "raw_response": self.raw_response,
            "used_fallback": self.used_fallback,
        }


@dataclass
class GatewayChildRuntimeControl:
    runtime_session_key: str = ""
    agent: Any = None
    finished: bool = False
    cancel_requested: bool = False

    def interrupt(self, reason: str = "") -> bool:
        agent = getattr(self, "agent", None)
        if agent is None or getattr(self, "finished", False):
            return False
        try:
            agent.interrupt(reason or "cancelled")
            return True
        except Exception:
            return False


@dataclass
class GatewayChildLaunchSpec:
    event: Any = None
    session_entry: Any = None
    source: Any = None
    request: Any = None
    context_prompt: str = ""
    channel_prompt: Optional[str] = None
    dynamic_disabled_skills: List[str] = field(default_factory=list)
    dynamic_disabled_toolsets: List[str] = field(default_factory=list)
    event_message_id: str = ""
    platform_turn: Any = None
    trace_ctx: Any = None
    originating_user_message: str = ""


@dataclass
class GatewayChildState:
    child_id: str
    session_key: str
    goal: str
    originating_user_message: str
    runtime_session_id: str = ""
    runtime_session_key: str = ""
    request_kind: str = "analysis"
    complexity_score: int = 100
    status: str = "queued"
    summary: str = ""
    suggested_user_reply: str = ""
    actual_user_reply_sent: str = ""
    tool_usage: List[str] = field(default_factory=list)
    final_artifacts: List[str] = field(default_factory=list)
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    current_step: str = ""
    current_tool: str = ""
    last_progress_message: str = ""
    last_progress_kind: str = ""
    last_activity_ts: str = ""
    progress_seq: int = 0
    tool_history: List[Dict[str, Any]] = field(default_factory=list)
    may_reply_directly: bool = True
    reply_style: str = ""
    priority: str = "normal"
    model_tier: str = ""
    turn_model_override: Dict[str, Any] = field(default_factory=dict)
    allowed_toolsets: List[str] = field(default_factory=list)
    dedupe_key: str = ""
    public_replies: List[Dict[str, Any]] = field(default_factory=list)
    user_turn_recorded: bool = False
    cancel_requested: bool = False
    lifecycle_finalized: bool = False
    trace_ctx: Any = None
    runtime_control: Any = None
    terminal_event_emitted: bool = False
    task: Any = None
    launch_spec: Any = None

    def is_active(self) -> bool:
        return self.status in {"queued", "running"}

    def record_progress(
        self,
        *,
        kind: str,
        message: str = "",
        step: str = "",
        tool: str = "",
        remember_tool: bool = False,
    ) -> None:
        cleaned_message = re.sub(r"\s+", " ", str(message or "").strip())
        cleaned_tool = str(tool or "").strip()
        cleaned_step = str(step or "").strip()
        self.progress_seq += 1
        self.last_activity_ts = utc_now_iso()
        self.last_progress_kind = str(kind or "").strip()
        if cleaned_message:
            self.last_progress_message = cleaned_message
        if cleaned_step:
            self.current_step = cleaned_step
        if cleaned_tool:
            self.current_tool = cleaned_tool
        if remember_tool and cleaned_tool:
            self.tool_history.append(
                {
                    "tool": cleaned_tool,
                    "message": cleaned_message,
                    "timestamp": self.last_activity_ts,
                }
            )
            if len(self.tool_history) > 12:
                self.tool_history = self.tool_history[-12:]

    def add_public_reply(self, content: str, reply_kind: str) -> None:
        text = str(content or "").strip()
        if not text:
            return
        self.public_replies.append(
            {
                "reply_kind": reply_kind,
                "content": text,
                "timestamp": utc_now_iso(),
            }
        )
        if reply_kind in {"final", "error", "cancelled"}:
            self.actual_user_reply_sent = text


@dataclass
class GatewayOrchestratorSession:
    session_key: str
    main_agent: Any = None
    main_agent_signature: str = ""
    main_agent_running: bool = False
    fullagent_runtime_session_id: str = ""
    fullagent_runtime_session_key: str = ""
    active_children: Dict[str, GatewayChildState] = field(default_factory=dict)
    recent_children: List[GatewayChildState] = field(default_factory=list)
    routing_history: List[Dict[str, str]] = field(default_factory=list)
    routing_stable_prefix: List[Dict[str, str]] = field(default_factory=list)
    last_decision: Optional[OrchestratorDecision] = None
    last_decision_summary: str = ""
    queue_drain_paused: bool = False

    def has_active_children(self) -> bool:
        return any(child.is_active() for child in self.active_children.values())

    def running_children(self) -> List[GatewayChildState]:
        return [
            child for child in self.active_children.values()
            if child.status == "running"
        ]

    def queued_children(self) -> List[GatewayChildState]:
        return [
            child for child in self.active_children.values()
            if child.status == "queued"
        ]

    def active_child_count(self) -> int:
        return sum(1 for child in self.active_children.values() if child.is_active())

    def active_or_queued_child_count(self) -> int:
        return self.active_child_count()

    def running_child_count(self) -> int:
        return len(self.running_children())

    def bind_fullagent_runtime(
        self,
        *,
        runtime_session_id: str,
        runtime_session_key: str,
    ) -> tuple[str, str]:
        if runtime_session_id and not self.fullagent_runtime_session_id:
            self.fullagent_runtime_session_id = str(runtime_session_id).strip()
        if runtime_session_key and not self.fullagent_runtime_session_key:
            self.fullagent_runtime_session_key = str(runtime_session_key).strip()
        return (
            self.fullagent_runtime_session_id,
            self.fullagent_runtime_session_key,
        )

    def find_duplicate_child(self, dedupe_key: str) -> Optional[GatewayChildState]:
        if not dedupe_key:
            return None
        for child in self.active_children.values():
            child_key = child.dedupe_key or normalize_dedupe_key(child.goal)
            if child.is_active() and dedupe_keys_similar(child_key, dedupe_key):
                return child
        return None

    def next_queued_child(self) -> Optional[GatewayChildState]:
        for child in self.active_children.values():
            if child.status == "queued" and not child.cancel_requested:
                return child
        return None

    def archive_child(self, child: GatewayChildState, *, keep_recent: int = 10) -> None:
        self.active_children.pop(child.child_id, None)
        self.recent_children = [item for item in self.recent_children if item.child_id != child.child_id]
        self.recent_children.insert(0, child)
        if len(self.recent_children) > keep_recent:
            self.recent_children = self.recent_children[:keep_recent]

    def append_routing_history_entry(
        self,
        *,
        role: str,
        content: str,
        timestamp: str = "",
        max_entries: int = 48,
    ) -> None:
        entry = self._normalize_routing_history_entry(
            role=role, content=content, timestamp=timestamp
        )
        if entry is None:
            return
        self.routing_history.append(entry)
        if max_entries > 0 and len(self.routing_history) > max_entries:
            self.routing_history = self.routing_history[-max_entries:]

    @staticmethod
    def _normalize_routing_history_entry(
        *,
        role: str,
        content: str,
        timestamp: str = "",
    ) -> Optional[Dict[str, str]]:
        cleaned_role = str(role or "").strip()
        cleaned_content = re.sub(r"\s+", " ", str(content or "").strip())
        if cleaned_role not in {"user", "assistant"} or not cleaned_content:
            return None
        entry: Dict[str, str] = {"role": cleaned_role, "content": cleaned_content}
        if timestamp:
            entry["timestamp"] = timestamp
        return entry

    def capture_routing_stable_prefix(
        self,
        entries: List[Dict[str, str]],
        *,
        max_entries: int,
    ) -> None:
        if max_entries <= 0 or len(self.routing_stable_prefix) >= max_entries:
            return

        start = len(self.routing_stable_prefix)
        end = min(len(entries or []), max_entries)
        for idx in range(start, end):
            raw = entries[idx] or {}
            entry = self._normalize_routing_history_entry(
                role=raw.get("role") or "",
                content=raw.get("content") or "",
                timestamp=raw.get("timestamp") or "",
            )
            if entry is None:
                continue
            self.routing_stable_prefix.append(entry)
            if len(self.routing_stable_prefix) >= max_entries:
                break

    @staticmethod
    def _child_tools(child: GatewayChildState, *, limit: int = 6) -> List[str]:
        tools: List[str] = []
        seen: set[str] = set()
        for item in child.tool_history or []:
            name = str((item or {}).get("tool") or "").strip()
            if name and name not in seen:
                seen.add(name)
                tools.append(name)
        for name in child.tool_usage or []:
            cleaned = str(name or "").strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                tools.append(cleaned)
        current_tool = str(child.current_tool or "").strip()
        if current_tool and current_tool not in seen:
            tools.append(current_tool)
        if limit > 0 and len(tools) > limit:
            return tools[-limit:]
        return tools

    def child_board(self, *, active_limit: int = 6, recent_limit: int = 6) -> Dict[str, Any]:
        active_children = self.running_children() + self.queued_children()
        active = [
            {
                "child_id": child.child_id,
                "goal": child.goal,
                "status": child.status,
                "current_step": child.current_step,
                "current_tool": child.current_tool,
                "current_activity": child.last_progress_message or child.summary or child.error,
                "tools_seen": self._child_tools(child),
            }
            for child in active_children[:active_limit]
        ]
        recent = [
            {
                "child_id": child.child_id,
                "goal": child.goal,
                "status": child.status,
                "summary": child.summary or child.actual_user_reply_sent,
                "error": child.error,
                "tools_used": self._child_tools(child),
            }
            for child in self.recent_children[:recent_limit]
        ]
        return {
            "active_children": active,
            "recent_children": recent,
            "last_decision_summary": self.last_decision_summary,
        }


def _strip_code_fence(text: str) -> str:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def _extract_json_object(text: str) -> str:
    raw = _strip_code_fence(text)
    if raw.startswith("{") and raw.endswith("}"):
        return raw
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        return raw[start : end + 1]
    raise ValueError("orchestrator decision missing JSON object")


def _repair_orchestrator_json(text: str) -> str:
    """Apply a minimal structural repair for common malformed JSON wrappers.

    This only fixes mismatched closing brackets/braces outside strings and
    appends any missing trailing closers. It intentionally does not rewrite
    keys, values, commas, or quoting.
    """
    raw = str(text or "")
    if not raw:
        return raw

    open_to_close = {"{": "}", "[": "]"}
    close_to_open = {"}": "{", "]": "["}
    stack: list[str] = []
    repaired: list[str] = []
    in_string = False
    escaped = False
    changed = 0

    def _next_non_space(start: int) -> str:
        for idx in range(start, len(raw)):
            if not raw[idx].isspace():
                return raw[idx]
        return ""

    for idx, ch in enumerate(raw):
        if escaped:
            repaired.append(ch)
            escaped = False
            continue
        if ch == "\\" and in_string:
            repaired.append(ch)
            escaped = True
            continue
        if ch == '"':
            repaired.append(ch)
            in_string = not in_string
            continue
        if in_string:
            repaired.append(ch)
            continue

        if ch in open_to_close:
            stack.append(ch)
            repaired.append(ch)
            continue

        if ch in close_to_open:
            if not stack:
                repaired.append(ch)
                continue
            expected = open_to_close[stack[-1]]
            if ch != expected:
                changed += 1
                if changed > 2:
                    return raw
                next_sig = _next_non_space(idx + 1)
                if next_sig in {"", ",", "}", "]"}:
                    continue
                repaired.append(expected)
                stack.pop()
                continue
            repaired.append(ch)
            stack.pop()
            continue

        repaired.append(ch)

    if stack:
        missing = "".join(open_to_close[item] for item in reversed(stack))
        changed += len(missing)
        if changed > 2:
            return raw
        repaired.append(missing)

    return "".join(repaired)


def _load_orchestrator_decision_json(raw: str) -> Dict[str, Any]:
    stripped = _strip_code_fence(raw)
    json_text = stripped if stripped.startswith("{") else _extract_json_object(raw)
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        repaired = _repair_orchestrator_json(json_text)
        if repaired == json_text:
            raise
        payload = json.loads(repaired)
    if not isinstance(payload, dict):
        raise ValueError("orchestrator decision must be a JSON object")
    return payload


def _normalize_complexity_score(
    value: Any,
    *,
    default: Optional[int] = 100,
    minimum: int = 0,
) -> Optional[int]:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = default
    if score is None:
        return None
    return max(minimum, min(100, score))


def extract_orchestrator_complexity_marker(text: str) -> Optional[int]:
    raw = str(text or "")
    match = _ORCH_COMPLEXITY_MARKER_RE.search(raw)
    if not match:
        return None
    try:
        score = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return score if 0 <= score <= 100 else None


def strip_orchestrator_complexity_marker(text: str) -> str:
    raw = str(text or "").strip()
    return _ORCH_COMPLEXITY_MARKER_RE.sub("", raw, count=1).strip()


def is_orchestrator_routing_summary(text: str) -> bool:
    return strip_orchestrator_complexity_marker(text).startswith(_ORCH_ROUTING_SUMMARY_PREFIX)


def extract_orchestrator_routing_field(text: str, field: str) -> str:
    normalized = strip_orchestrator_complexity_marker(text)
    if not normalized.startswith(_ORCH_ROUTING_SUMMARY_PREFIX):
        return ""
    tail = normalized[len(_ORCH_ROUTING_SUMMARY_PREFIX) :]
    target = str(field or "").strip().lower()
    if not target:
        return ""
    for match in _ORCH_ROUTING_FIELD_RE.finditer(tail):
        if str(match.group("key") or "").strip().lower() == target:
            return str(match.group("value") or "").strip()
    return ""


def extract_orchestrator_reply_summary(text: str) -> str:
    return extract_orchestrator_routing_field(text, "reply_summary")


def prepend_orchestrator_complexity_marker(text: str, score: Any) -> str:
    normalized = _normalize_complexity_score(score, default=0, minimum=0)
    marker = f"[[ORCH_COMPLEXITY:{normalized}]]"
    body = str(text or "").strip()
    return f"{marker} {body}".strip()


def _normalize_cancel_children(value: Any) -> List[str]:
    return [str(item).strip() for item in (value or []) if str(item).strip()]


def _parse_action_payload_decision(payload: Dict[str, Any], *, raw: str) -> OrchestratorDecision:
    action = str(payload.get("action") or "").strip().lower()
    if not action:
        raise ValueError("orchestrator decision missing action")

    body = payload.get("payload")
    if body is None:
        body = {}
    if isinstance(body, str):
        if action == "reply":
            body = {"text": body}
        elif action == "spawn":
            body = {"goal": body}
        else:
            body = {}
    if not isinstance(body, dict):
        raise ValueError("orchestrator decision payload must be an object")

    reason = str(payload.get("reasoning_summary") or "").strip()
    cancel_children = _normalize_cancel_children(
        body.get("cancel_children") or payload.get("cancel_children")
    )

    if action == "reply":
        text = str(
            body.get("text")
            or body.get("reply")
            or body.get("immediate_reply")
            or ""
        ).strip()
        if not text:
            raise ValueError("reply action payload missing text")
        return OrchestratorDecision(
            complexity_score=_normalize_complexity_score(
                payload.get("complexity_score", body.get("complexity_score")),
                default=1,
                minimum=0,
            ),
            respond_now=True,
            immediate_reply=text,
            cancel_children=cancel_children,
            reasoning_summary=reason,
            raw_response=raw,
        )

    if action == "spawn":
        spawn_children: List[OrchestratorChildRequest] = []
        handoff_reply = str(
            body.get("handoff_reply")
            or body.get("text")
            or body.get("reply")
            or ""
        ).strip()
        child_payload = body.get("child")
        if isinstance(child_payload, dict):
            spawn_children.append(OrchestratorChildRequest.from_dict(child_payload))
        else:
            goal = str(body.get("goal") or "").strip()
            if goal:
                spawn_children.append(OrchestratorChildRequest.from_dict({"goal": goal}))
        if not spawn_children:
            for item in body.get("spawn_children") or []:
                if not isinstance(item, dict):
                    raise ValueError("spawn action payload spawn_children items must be objects")
                spawn_children.append(OrchestratorChildRequest.from_dict(item))
        if not spawn_children:
            raise ValueError("spawn action payload missing goal")
        return OrchestratorDecision(
            complexity_score=_normalize_complexity_score(
                payload.get("complexity_score", body.get("complexity_score")),
                default=18,
                minimum=0,
            ),
            respond_now=False,
            spawn_handoff_reply=handoff_reply,
            spawn_children=spawn_children,
            cancel_children=cancel_children,
            reasoning_summary=reason,
            raw_response=raw,
        )

    if action == "ignore":
        return OrchestratorDecision(
            complexity_score=_normalize_complexity_score(
                payload.get("complexity_score", body.get("complexity_score")),
                default=1,
                minimum=0,
            ),
            respond_now=False,
            cancel_children=cancel_children,
            ignore_message=True,
            reasoning_summary=reason,
            raw_response=raw,
        )

    raise ValueError(f"unsupported orchestrator action: {action}")


def parse_orchestrator_decision(text: str) -> OrchestratorDecision:
    raw = str(text or "").strip()
    payload = _load_orchestrator_decision_json(raw)

    if payload.get("action") is not None:
        return _parse_action_payload_decision(payload, raw=raw)

    spawn_children: List[OrchestratorChildRequest] = []
    for item in payload.get("spawn_children") or []:
        if not isinstance(item, dict):
            raise ValueError("spawn_children items must be objects")
        spawn_children.append(OrchestratorChildRequest.from_dict(item))

    cancel_children = _normalize_cancel_children(payload.get("cancel_children"))

    inferred_score = 100
    if (
        bool(payload.get("respond_now"))
        and str(payload.get("immediate_reply") or "").strip()
        and not (payload.get("spawn_children") or [])
        and not bool(payload.get("ignore_message"))
    ):
        inferred_score = 1

    return OrchestratorDecision(
        complexity_score=_normalize_complexity_score(
            payload.get("complexity_score"),
            default=inferred_score,
            minimum=0,
        ),
        respond_now=bool(payload.get("respond_now")),
        immediate_reply=str(payload.get("immediate_reply") or "").strip(),
        spawn_handoff_reply=str(payload.get("spawn_handoff_reply") or "").strip(),
        spawn_children=spawn_children,
        cancel_children=cancel_children,
        ignore_message=bool(payload.get("ignore_message")),
        reasoning_summary=str(payload.get("reasoning_summary") or "").strip(),
        raw_response=raw,
    )
