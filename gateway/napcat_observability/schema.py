"""
NapCat observability event schema.

All telemetry events share a unified structure with typed payloads.
This module is the single source of truth for event field names,
event type enums, severity levels, and serialization.
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

class EventType(str, enum.Enum):
    """Fixed set of NapCat observability event types."""

    # Trace lifecycle
    TRACE_CREATED = "trace.created"

    # Adapter lifecycle
    ADAPTER_LIFECYCLE = "adapter.lifecycle"
    ADAPTER_WS_ACTION = "adapter.ws_action"

    # Message ingestion
    MESSAGE_RECEIVED = "message.received"
    MESSAGE_RECALLED = "message.recalled"
    MESSAGE_MEDIA_EXTRACTED = "message.media_extracted"
    MESSAGE_REPLY_CONTEXT_RESOLVED = "message.reply_context_resolved"

    # Group trigger evaluation
    GROUP_TRIGGER_EVALUATED = "group.trigger.evaluated"
    GROUP_TRIGGER_REJECTED = "group.trigger.rejected"
    GROUP_CONTEXT_ONLY_EMITTED = "group.context_only_emitted"

    # Gateway turn
    GATEWAY_TURN_PREPARED = "gateway.turn.prepared"
    GATEWAY_TURN_SHORT_CIRCUITED = "gateway.turn.short_circuited"
    ORCHESTRATOR_TURN_STARTED = "orchestrator.turn.started"
    ORCHESTRATOR_DECISION_PARSED = "orchestrator.decision.parsed"
    ORCHESTRATOR_TURN_COMPLETED = "orchestrator.turn.completed"
    ORCHESTRATOR_TURN_FAILED = "orchestrator.turn.failed"
    ORCHESTRATOR_TURN_IGNORED = "orchestrator.turn.ignored"
    ORCHESTRATOR_STATUS_REPLY = "orchestrator.status.reply"
    ORCHESTRATOR_CHILD_QUEUED = "orchestrator.child.queued"
    ORCHESTRATOR_CHILD_SPAWNED = "orchestrator.child.spawned"
    ORCHESTRATOR_CHILD_REUSED = "orchestrator.child.reused"
    ORCHESTRATOR_CHILD_CANCELLED = "orchestrator.child.cancelled"
    ORCHESTRATOR_CHILD_COMPLETED = "orchestrator.child.completed"
    ORCHESTRATOR_CHILD_FAILED = "orchestrator.child.failed"

    # Agent execution
    AGENT_RUN_STARTED = "agent.run.started"
    AGENT_MODEL_REQUESTED = "agent.model.requested"
    AGENT_MODEL_COMPLETED = "agent.model.completed"
    AGENT_REASONING_DELTA = "agent.reasoning.delta"
    AGENT_RESPONSE_DELTA = "agent.response.delta"
    AGENT_TOOL_CALLED = "agent.tool.called"
    AGENT_TOOL_COMPLETED = "agent.tool.completed"
    AGENT_TOOL_FAILED = "agent.tool.failed"
    AGENT_MEMORY_CALLED = "agent.memory.called"
    AGENT_MEMORY_USED = "agent.memory.used"
    AGENT_SKILL_CALLED = "agent.skill.called"
    AGENT_SKILL_USED = "agent.skill.used"
    AGENT_RESPONSE_FINAL = "agent.response.final"
    AGENT_RESPONSE_SUPPRESSED = "agent.response.suppressed"

    # Policy & errors
    POLICY_APPLIED = "policy.applied"
    ERROR_RAISED = "error.raised"


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

class Severity(str, enum.Enum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


# Mapping: event types that always imply a specific severity
_EVENT_DEFAULT_SEVERITY: Dict[EventType, Severity] = {
    EventType.ERROR_RAISED: Severity.ERROR,
    EventType.AGENT_TOOL_FAILED: Severity.WARN,
    EventType.GROUP_TRIGGER_REJECTED: Severity.INFO,
    EventType.AGENT_RESPONSE_SUPPRESSED: Severity.INFO,
    EventType.GATEWAY_TURN_SHORT_CIRCUITED: Severity.INFO,
    EventType.ORCHESTRATOR_TURN_IGNORED: Severity.INFO,
    EventType.ORCHESTRATOR_CHILD_FAILED: Severity.ERROR,
    EventType.ORCHESTRATOR_TURN_FAILED: Severity.ERROR,
}


def default_severity(event_type: EventType) -> Severity:
    """Return the default severity for a given event type."""
    return _EVENT_DEFAULT_SEVERITY.get(event_type, Severity.INFO)


# ---------------------------------------------------------------------------
# Event priorities (for drop policy)
# ---------------------------------------------------------------------------

class EventPriority(int, enum.Enum):
    """Priority levels for queue drop policy. Higher = more important."""
    LOW = 0       # debug, media extraction details
    NORMAL = 1    # standard events
    HIGH = 2      # errors, alerts, final responses
    CRITICAL = 3  # lifecycle events, never drop

_EVENT_PRIORITY: Dict[EventType, EventPriority] = {
    EventType.TRACE_CREATED: EventPriority.CRITICAL,
    EventType.ADAPTER_LIFECYCLE: EventPriority.CRITICAL,
    EventType.ERROR_RAISED: EventPriority.HIGH,
    EventType.AGENT_RESPONSE_FINAL: EventPriority.HIGH,
    EventType.AGENT_RESPONSE_SUPPRESSED: EventPriority.HIGH,
    EventType.ORCHESTRATOR_TURN_COMPLETED: EventPriority.HIGH,
    EventType.ORCHESTRATOR_TURN_FAILED: EventPriority.HIGH,
    EventType.ORCHESTRATOR_TURN_IGNORED: EventPriority.HIGH,
    EventType.AGENT_TOOL_FAILED: EventPriority.HIGH,
    EventType.ORCHESTRATOR_STATUS_REPLY: EventPriority.NORMAL,
    EventType.ORCHESTRATOR_CHILD_QUEUED: EventPriority.NORMAL,
    EventType.ORCHESTRATOR_CHILD_SPAWNED: EventPriority.NORMAL,
    EventType.ORCHESTRATOR_CHILD_REUSED: EventPriority.NORMAL,
    EventType.ORCHESTRATOR_CHILD_CANCELLED: EventPriority.HIGH,
    EventType.ORCHESTRATOR_CHILD_COMPLETED: EventPriority.HIGH,
    EventType.ORCHESTRATOR_CHILD_FAILED: EventPriority.HIGH,
    EventType.AGENT_REASONING_DELTA: EventPriority.LOW,
    EventType.AGENT_RESPONSE_DELTA: EventPriority.LOW,
    EventType.MESSAGE_MEDIA_EXTRACTED: EventPriority.LOW,
}


def event_priority(event_type: EventType) -> EventPriority:
    """Return the priority for an event type."""
    return _EVENT_PRIORITY.get(event_type, EventPriority.NORMAL)


_EVENT_STAGE: Dict[EventType, str] = {
    EventType.TRACE_CREATED: "inbound",
    EventType.ADAPTER_LIFECYCLE: "raw",
    EventType.ADAPTER_WS_ACTION: "raw",
    EventType.MESSAGE_RECEIVED: "inbound",
    EventType.MESSAGE_RECALLED: "inbound",
    EventType.MESSAGE_MEDIA_EXTRACTED: "inbound",
    EventType.MESSAGE_REPLY_CONTEXT_RESOLVED: "inbound",
    EventType.GROUP_TRIGGER_EVALUATED: "trigger",
    EventType.GROUP_TRIGGER_REJECTED: "trigger",
    EventType.GROUP_CONTEXT_ONLY_EMITTED: "trigger",
    EventType.GATEWAY_TURN_PREPARED: "context",
    EventType.GATEWAY_TURN_SHORT_CIRCUITED: "final",
    EventType.ORCHESTRATOR_TURN_STARTED: "context",
    EventType.ORCHESTRATOR_DECISION_PARSED: "context",
    EventType.ORCHESTRATOR_TURN_COMPLETED: "final",
    EventType.ORCHESTRATOR_TURN_FAILED: "error",
    EventType.ORCHESTRATOR_TURN_IGNORED: "final",
    EventType.ORCHESTRATOR_STATUS_REPLY: "context",
    EventType.ORCHESTRATOR_CHILD_QUEUED: "tools",
    EventType.ORCHESTRATOR_CHILD_SPAWNED: "tools",
    EventType.ORCHESTRATOR_CHILD_REUSED: "tools",
    EventType.ORCHESTRATOR_CHILD_CANCELLED: "final",
    EventType.ORCHESTRATOR_CHILD_COMPLETED: "final",
    EventType.ORCHESTRATOR_CHILD_FAILED: "error",
    EventType.AGENT_RUN_STARTED: "context",
    EventType.AGENT_MODEL_REQUESTED: "model",
    EventType.AGENT_MODEL_COMPLETED: "model",
    EventType.AGENT_REASONING_DELTA: "model",
    EventType.AGENT_RESPONSE_DELTA: "model",
    EventType.AGENT_TOOL_CALLED: "tools",
    EventType.AGENT_TOOL_COMPLETED: "tools",
    EventType.AGENT_TOOL_FAILED: "tools",
    EventType.AGENT_MEMORY_CALLED: "memory_skill_routing",
    EventType.AGENT_MEMORY_USED: "memory_skill_routing",
    EventType.AGENT_SKILL_CALLED: "memory_skill_routing",
    EventType.AGENT_SKILL_USED: "memory_skill_routing",
    EventType.AGENT_RESPONSE_FINAL: "final",
    EventType.AGENT_RESPONSE_SUPPRESSED: "final",
    EventType.POLICY_APPLIED: "memory_skill_routing",
    EventType.ERROR_RAISED: "error",
}


def event_stage(event_type: EventType | str) -> str:
    """Return the default UI stage for a given event type."""
    if isinstance(event_type, str):
        event_type = EventType(event_type)
    return _EVENT_STAGE.get(event_type, "raw")


# ---------------------------------------------------------------------------
# NapCat Event
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class NapcatEvent:
    """A single NapCat observability event."""

    event_type: EventType
    payload: Dict[str, Any]

    # Trace context — set by caller or inherited
    trace_id: str = ""
    span_id: str = field(default_factory=_new_id)
    parent_span_id: str = ""

    # Session/message context
    session_key: str = ""
    session_id: str = ""
    platform: str = "napcat"
    chat_type: str = ""      # "private" or "group"
    chat_id: str = ""
    user_id: str = ""
    message_id: str = ""

    # Metadata
    event_id: str = field(default_factory=_new_id)
    ts: float = field(default_factory=time.time)
    severity: Severity = Severity.INFO

    def __post_init__(self):
        if isinstance(self.event_type, str):
            self.event_type = EventType(self.event_type)
        if isinstance(self.severity, str):
            self.severity = Severity(self.severity)
        if not self.severity or self.severity == Severity.INFO:
            self.severity = default_severity(self.event_type)
        if not self.trace_id:
            self.trace_id = _new_id()

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict suitable for JSON storage."""
        d = asdict(self)
        d["event_type"] = self.event_type.value
        d["severity"] = self.severity.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "NapcatEvent":
        """Deserialize from a plain dict."""
        d = dict(d)  # shallow copy
        if "event_type" in d:
            d["event_type"] = EventType(d["event_type"])
        if "severity" in d:
            d["severity"] = Severity(d["severity"])
        return cls(**d)

    @property
    def priority(self) -> EventPriority:
        return event_priority(self.event_type)


# ---------------------------------------------------------------------------
# Payload truncation
# ---------------------------------------------------------------------------

def truncate_field(value: str, max_chars: int, suffix: str = "... [truncated]") -> str:
    """Truncate a string field if it exceeds max_chars."""
    if not value or len(value) <= max_chars:
        return value
    cut = max_chars - len(suffix)
    if cut < 0:
        cut = 0
    return value[:cut] + suffix


def truncate_payload(
    payload: Dict[str, Any],
    *,
    max_stdout_chars: int = 8000,
    max_stderr_chars: int = 4000,
) -> Dict[str, Any]:
    """Apply standard truncation rules to a payload dict.

    Truncates known large fields while preserving observability-critical
    structured context such as the full captured prompt.
    """
    result = dict(payload)
    _FIELD_LIMITS = {
        "stdout": max_stdout_chars,
        "stderr": max_stderr_chars,
        "content": max_stdout_chars,
        "response": max_stdout_chars,
        "raw_message": max_stdout_chars,
    }
    for key, limit in _FIELD_LIMITS.items():
        if key in result and isinstance(result[key], str):
            result[key] = truncate_field(result[key], limit)
    return result
