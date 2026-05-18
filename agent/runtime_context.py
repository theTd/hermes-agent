"""Gateway/session-scoped runtime metadata threaded through an agent run."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, Optional


@dataclass
class AgentRuntimeContext:
    """Gateway/session-scoped runtime metadata threaded through an agent run."""

    user_id: Optional[str] = None
    user_name: Optional[str] = None
    gateway_session_key: Optional[str] = None
    chat_id: Optional[str] = None
    chat_name: Optional[str] = None
    chat_type: Optional[str] = None
    thread_id: Optional[str] = None
    agent_context: str = "primary"

    @staticmethod
    def _normalize_agent_context(value: Optional[Any]) -> str:
        return str(value or "primary").strip() or "primary"

    @classmethod
    def resolve(
        cls,
        *,
        runtime_context: Optional["AgentRuntimeContext"] = None,
    ) -> "AgentRuntimeContext":
        """Build a normalized runtime context from a context object or None."""
        if isinstance(runtime_context, cls):
            context = runtime_context.copy()
        elif runtime_context is None:
            context = cls()
        else:
            raise TypeError("runtime_context must be an AgentRuntimeContext or None")

        context.agent_context = cls._normalize_agent_context(context.agent_context)
        return context

    @classmethod
    def from_source(
        cls,
        source: Optional[Any] = None,
        *,
        gateway_session_key: Optional[str] = None,
        agent_context: Optional[str] = None,
    ) -> "AgentRuntimeContext":
        """Build runtime context from a gateway/session source object."""
        return cls(
            user_id=getattr(source, "user_id", None) if source is not None else None,
            user_name=getattr(source, "user_name", None) if source is not None else None,
            gateway_session_key=gateway_session_key,
            chat_id=getattr(source, "chat_id", None) if source is not None else None,
            chat_name=getattr(source, "chat_name", None) if source is not None else None,
            chat_type=getattr(source, "chat_type", None) if source is not None else None,
            thread_id=getattr(source, "thread_id", None) if source is not None else None,
            agent_context=cls._normalize_agent_context(agent_context),
        )

    def copy(self) -> "AgentRuntimeContext":
        """Return a detached copy for downstream mutation-safe handoff."""
        return replace(self)

    def to_agent_kwargs(self) -> Dict[str, "AgentRuntimeContext"]:
        """Return normalized ``AIAgent`` constructor kwargs."""
        return {"runtime_context": self.copy()}

    def to_memory_provider_kwargs(
        self,
        *,
        platform: Optional[str],
        hermes_home: Optional[str] = None,
        session_title: Optional[str] = None,
        agent_identity: Optional[str] = None,
        agent_workspace: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        include_empty_chat_fields: bool = True,
        include_gateway_session_key: bool = True,
    ) -> Dict[str, Any]:
        """Serialize runtime context for ``MemoryManager.initialize_all()``."""
        kwargs: Dict[str, Any] = {
            "platform": str(platform or ""),
            "agent_context": self._normalize_agent_context(self.agent_context),
        }
        if hermes_home is not None:
            kwargs["hermes_home"] = hermes_home

        for key, value in (
            ("chat_id", self.chat_id),
            ("chat_name", self.chat_name),
            ("chat_type", self.chat_type),
            ("thread_id", self.thread_id),
        ):
            if value is not None:
                kwargs[key] = str(value)
            elif include_empty_chat_fields:
                kwargs[key] = ""

        if self.user_id:
            kwargs["user_id"] = str(self.user_id)
        if self.user_name:
            kwargs["user_name"] = str(self.user_name)
        if include_gateway_session_key and self.gateway_session_key:
            kwargs["gateway_session_key"] = str(self.gateway_session_key)
        if session_title:
            kwargs["session_title"] = str(session_title)
        if agent_identity:
            kwargs["agent_identity"] = str(agent_identity)
        if agent_workspace:
            kwargs["agent_workspace"] = str(agent_workspace)
        if parent_session_id:
            kwargs["parent_session_id"] = str(parent_session_id)
        return kwargs
