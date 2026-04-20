"""Napcat-specific gateway instrumentation mixin.

This module extracts napcat's orchestrator delegation and cache-eviction
methods from ``gateway/run.py`` so upstream changes to that file do not
collide with napcat method definitions.

Mixed into ``GatewayRunner`` via ``class GatewayRunner(NapcatGatewayMixin):``.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class NapcatGatewayMixin:
    """Provides orchestrator and eviction helpers for the gateway runner."""

    # -- Orchestrator delegation --------------------------------------

    def _orchestrator_internal_session_id(self, session_key: str) -> str:
        """Return a stable internal session id for the in-memory orchestrator."""
        return self._get_gateway_extension().orchestrator_internal_session_id(session_key)

    def _orchestrator_child_session_id(self, parent_session_id: str, child_id: str) -> str:
        """Return an isolated persisted session id for a background child agent."""
        return self._get_gateway_extension().orchestrator_child_session_id(parent_session_id, child_id)

    def _orchestrator_child_runtime_session_key(self, session_key: str, child_id: str) -> str:
        """Return an isolated runtime key for a background child agent."""
        return self._get_gateway_extension().orchestrator_child_runtime_session_key(session_key, child_id)

    def _orchestrator_cache_key_belongs_to_session(self, session_key: str, cache_key: str) -> bool:
        """Return True when a cache entry belongs to the session's orchestrator subtree."""
        matcher = getattr(self._get_gateway_extension(), "orchestrator_cache_key_belongs_to_session", None)
        if callable(matcher):
            return bool(matcher(session_key, cache_key))
        return False

    # -- Cache eviction -----------------------------------------------

    def _evict_cached_orchestrator_agents(self, session_key: str) -> None:
        """Remove cached orchestrator background agents for a parent session."""
        _lock = getattr(self, "_agent_cache_lock", None)
        _cache = getattr(self, "_agent_cache", None)
        if not _lock or _cache is None:
            return

        removed: list[Any] = []
        with _lock:
            orchestrator_keys = [
                key
                for key in list(_cache.keys())
                if self._orchestrator_cache_key_belongs_to_session(session_key, str(key))
            ]
            for key in orchestrator_keys:
                cached = _cache.pop(key, None)
                if isinstance(cached, tuple):
                    removed.append(cached[0])
                elif cached is not None:
                    removed.append(cached)

        for agent in removed:
            if agent is not None:
                self._cleanup_agent_resources(agent)

    def _evict_cached_child_agents(self, session_key: str) -> None:
        """Backward-compatible wrapper for orchestrator cache eviction."""
        self._evict_cached_orchestrator_agents(session_key)

    # -- Busy-session orchestrator bypass -----------------------------

    async def _maybe_bypass_busy_for_orchestrator(
        self,
        event,
        session_key: str,
        adapter,
    ) -> bool:
        """If only orchestrator children are running, handle the message directly.

        Returns True when the bypass handled the message (caller should stop).
        Returns False when normal busy-session flow should continue.
        """
        running_agent = self._running_agents.get(session_key)
        extension = self._get_gateway_extension()
        orchestrator_session = extension.get_orchestrator_session(session_key)
        orchestrator_child_only_busy = bool(
            extension.orchestrator_enabled_for_source(
                getattr(event, "source", None)
            )
            and orchestrator_session is not None
            and orchestrator_session.has_active_children()
            and not orchestrator_session.main_agent_running
            and (
                running_agent is None
                or running_agent is getattr(
                    # _AGENT_PENDING_SENTINEL is defined in gateway/run.py
                    # Use getattr to avoid import-time dependency.
                    __import__("gateway.run", fromlist=["_AGENT_PENDING_SENTINEL"]),
                    "_AGENT_PENDING_SENTINEL",
                    None,
                )
            )
        )
        if not orchestrator_child_only_busy:
            return False

        logger.info(
            "Bypassing adapter busy-session guard for %s: active orchestrator child without foreground main-agent lock.",
            session_key,
        )
        try:
            thread_meta = {"thread_id": event.source.thread_id} if event.source.thread_id else None
            response = await self._handle_message(event)
            if response:
                await adapter._send_with_retry(
                    chat_id=event.source.chat_id,
                    content=response,
                    reply_to=event.message_id,
                    metadata=thread_meta,
                )
        except Exception as exc:
            logger.error(
                "Busy-session orchestrator bypass failed for %s: %s",
                session_key,
                exc,
                exc_info=True,
            )
        return True

    async def _maybe_reply_progress_status(
        self,
        event,
        session_key: str,
        adapter,
    ) -> bool:
        """If the incoming message is a progress query, reply with running status.

        Returns True when a progress reply was sent (caller should stop).
        """
        extension = self._get_gateway_extension()
        if not extension.is_progress_query(event.text):
            return False
        running_snapshot = extension.running_agent_snapshot(session_key)
        if running_snapshot is None:
            return False
        _should_emit = getattr(self, "_should_emit_non_llm_status_messages", None)
        if callable(_should_emit) and not _should_emit(event.source):
            return True  # consumed but not sent
        thread_meta = {"thread_id": event.source.thread_id} if event.source.thread_id else None
        await adapter._send_with_retry(
            chat_id=event.source.chat_id,
            content=extension.format_running_agent_status(running_snapshot),
            reply_to=event.message_id,
            metadata=thread_meta,
        )
        return True
