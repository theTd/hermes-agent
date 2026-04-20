"""
NapCat trace context management.

A trace represents the full processing chain for a single NapCat message,
from adapter receipt through gateway decision, agent execution, tool calls,
to final response or suppression.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class NapcatTraceContext:
    """Carries trace correlation IDs through the NapCat processing chain.

    Created once when a message arrives at the adapter, then threaded
    through gateway evaluation, agent execution, and tool calls.
    Child spans inherit trace_id and use the parent's span_id as
    parent_span_id.
    """

    trace_id: str = field(default_factory=_new_id)
    span_id: str = field(default_factory=_new_id)
    parent_span_id: str = ""

    # Session context
    session_key: str = ""
    session_id: str = ""

    # Message context
    chat_type: str = ""       # "private" or "group"
    chat_id: str = ""
    user_id: str = ""
    message_id: str = ""
    agent_scope: str = ""
    child_id: str = ""
    llm_request_id: str = ""

    def child_span(
        self,
        *,
        agent_scope: str = "",
        child_id: str = "",
    ) -> "NapcatTraceContext":
        """Create a child span inheriting trace_id and session context."""
        return NapcatTraceContext(
            trace_id=self.trace_id,
            span_id=_new_id(),
            parent_span_id=self.span_id,
            session_key=self.session_key,
            session_id=self.session_id,
            chat_type=self.chat_type,
            chat_id=self.chat_id,
            user_id=self.user_id,
            message_id=self.message_id,
            agent_scope=agent_scope or self.agent_scope,
            child_id=child_id or self.child_id,
            llm_request_id="",
        )

    def to_dict(self) -> dict:
        """Serialize to a plain dict."""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "session_key": self.session_key,
            "session_id": self.session_id,
            "chat_type": self.chat_type,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "message_id": self.message_id,
            "agent_scope": self.agent_scope,
            "child_id": self.child_id,
            "llm_request_id": self.llm_request_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NapcatTraceContext":
        """Deserialize from a plain dict."""
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


import contextvars
from typing import Optional

# Context variable for threading trace context through the call chain.
# Set in gateway/run.py before agent invocation, read in run_agent.py and model_tools.py.
current_napcat_trace: contextvars.ContextVar[Optional["NapcatTraceContext"]] = contextvars.ContextVar(
    "current_napcat_trace", default=None
)
