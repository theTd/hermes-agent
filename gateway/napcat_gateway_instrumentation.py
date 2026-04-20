"""Napcat-specific gateway instrumentation mixin.

This module extracts napcat's orchestrator delegation and cache-eviction
methods from ``gateway/run.py`` so upstream changes to that file do not
collide with napcat method definitions.

Mixed into ``GatewayRunner`` via ``class GatewayRunner(NapcatGatewayMixin):``.
"""

import logging
from typing import Any, Dict, Optional

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

    # -- Promotion dispatch -------------------------------------------

    async def _run_promotion_dispatch(
        self,
        *,
        promotion_plan,
        promotion_support,
        l1_model: str,
        l2_model: str,
        agent_run_kwargs: Dict[str, Any],
        extra_prompt: str,
        event,
        source,
    ) -> Dict[str, Any]:
        """Execute promotion L1/L2 dispatch and return the agent result.

        This method encapsulates the full promotion decision logic so that
        upstream changes to ``_handle_message_with_agent`` do not collide
        with napcat's promotion stage switching.

        Returns a dict with:
            - agent_result: result from ``_run_agent`` (may be None)
            - stage_used: the promotion stage that was used
            - escalated: whether the turn was escalated to L2
            - reason: the reason for the stage selection
            - return_none: if True, the caller should return None immediately
        """
        _promo_snapshot = promotion_plan.config_snapshot or {}
        _promo_protocol = _promo_snapshot.get("protocol", {})
        _promo_safeguards = _promo_snapshot.get("safeguards", {})
        _promo_optimizations = _promo_snapshot.get("optimizations", {}) or {}
        _single_stage_l1 = bool(_promo_optimizations.get("single_stage_l1"))
        _force_direct_l2 = bool(
            promotion_plan.stage == "l2"
            or _promo_optimizations.get("force_direct_l2")
        )

        _stage_used = ""
        _escalated = False
        _reason = ""

        if _force_direct_l2:
            _stage_used = "l2"
            _escalated = True
            _reason = str(
                _promo_optimizations.get("force_direct_l2_reason") or "explicit_user_request"
            )
            logger.info("Promotion direct to L2: %s", _reason)
            l2_override = promotion_plan.turn_model_override or _promo_snapshot.get("l2")
            agent_result = await self._run_agent(
                **{
                    **agent_run_kwargs,
                    "turn_model_override": l2_override,
                    "promotion_stage": "l2",
                    "promotion_protocol": _promo_protocol,
                }
            )
            return {
                "agent_result": agent_result,
                "stage_used": _stage_used,
                "escalated": _escalated,
                "reason": _reason,
            }

        # Inject L1 protocol prompt into context
        _l1_context = promotion_support.inject_protocol_before_extra_prompt(
            agent_run_kwargs["context_prompt"],
            extra_prompt,
            promotion_plan.protocol_prompt,
        )

        l1_result = await self._run_agent(
            **{
                **agent_run_kwargs,
                "context_prompt": _l1_context,
                "turn_model_override": promotion_plan.turn_model_override,
                "promotion_stage": "l1",
                "promotion_protocol": _promo_protocol,
            }
        )

        if _single_stage_l1:
            _stage_used = "l1"
            return {
                "agent_result": l1_result,
                "stage_used": _stage_used,
                "escalated": _escalated,
                "reason": _reason,
            }

        l1_response = str(l1_result.get("final_response") or "").strip()
        score_threshold = promotion_support.normalize_complexity_threshold(
            (_promo_safeguards or {}).get("l2_complexity_score_threshold")
        )
        l1_complexity_score = promotion_support.extract_turn_complexity_score(l1_result)
        l1_response = promotion_support.clip_high_score_response(
            l1_response,
            _promo_protocol,
            score_threshold,
        )
        action = promotion_support.parse_result(l1_response, _promo_protocol)

        if action == "no_reply":
            if source.chat_type == "dm":
                _stage_used = "l2"
                _escalated = True
                _reason = "dm_no_reply_forbidden"
                logger.info("Promotion L1: no_reply in DM — switching to L2")
                l2_override = _promo_snapshot.get("l2")
                agent_result = await self._run_agent(
                    **{
                        **agent_run_kwargs,
                        "turn_model_override": l2_override,
                        "promotion_stage": "l2",
                        "promotion_protocol": _promo_protocol,
                    }
                )
                return {
                    "agent_result": agent_result,
                    "stage_used": _stage_used,
                    "escalated": _escalated,
                    "reason": _reason,
                }
            else:
                _stage_used = "l1"
                _reason = "no_reply"
                logger.info("Promotion L1: no_reply — dropping turn")
                _trace_ctx = self._trace_context_for_event(event, source=source)
                if _trace_ctx:
                    from gateway.run import _emit_gateway_event, _GatewayEventType
                    _emit_gateway_event(
                        _GatewayEventType.AGENT_RESPONSE_FINAL,
                        {
                            "response_preview": "",
                            "response_previewed": False,
                            "tools_used": [],
                            "api_call_count": l1_result.get("api_calls", 0),
                            "model": l1_result.get("model", "") or l1_model,
                            "provider": l1_result.get("provider", ""),
                            "api_mode": l1_result.get("api_mode", ""),
                            "promotion_active": True,
                            "promotion_stage_used": _stage_used,
                            "promotion_escalated": _escalated,
                            "promotion_reason": _reason,
                            "promotion_l1_model": l1_model,
                            "promotion_l2_model": l2_model,
                            "response_suppressed": True,
                        },
                        trace_ctx=_trace_ctx,
                    )
                return {
                    "agent_result": None,
                    "stage_used": _stage_used,
                    "escalated": _escalated,
                    "reason": _reason,
                    "return_none": True,
                }

        elif l1_complexity_score is not None and l1_complexity_score >= score_threshold:
            _stage_used = "l2"
            _escalated = True
            _reason = f"complexity_score={l1_complexity_score}>={score_threshold}"
            logger.info("Promotion L1 forced to L2: %s", _reason)
            l2_override = _promo_snapshot.get("l2")
            agent_result = await self._run_agent(
                **{
                    **agent_run_kwargs,
                    "turn_model_override": l2_override,
                    "promotion_stage": "l2",
                    "promotion_protocol": _promo_protocol,
                }
            )
            return {
                "agent_result": agent_result,
                "stage_used": _stage_used,
                "escalated": _escalated,
                "reason": _reason,
            }

        elif action == "escalate":
            _stage_used = "l2"
            _escalated = True
            _reason = "marker"
            logger.info("Promotion L1: escalate — switching to L2")
            l2_override = _promo_snapshot.get("l2")
            agent_result = await self._run_agent(
                **{
                    **agent_run_kwargs,
                    "turn_model_override": l2_override,
                    "promotion_stage": "l2",
                    "promotion_protocol": _promo_protocol,
                }
            )
            return {
                "agent_result": agent_result,
                "stage_used": _stage_used,
                "escalated": _escalated,
                "reason": _reason,
            }

        else:
            # L1 gave a reply — check complexity before accepting
            should_escalate, reason = promotion_support.check_l1_complexity(
                l1_result, _promo_safeguards,
            )
            if should_escalate:
                _stage_used = "l2"
                _escalated = True
                _reason = reason
                logger.info("Promotion L1 forced to L2: %s", reason)
                l2_override = _promo_snapshot.get("l2")
                agent_result = await self._run_agent(
                    **{
                        **agent_run_kwargs,
                        "turn_model_override": l2_override,
                        "promotion_stage": "l2",
                        "promotion_protocol": _promo_protocol,
                    }
                )
                return {
                    "agent_result": agent_result,
                    "stage_used": _stage_used,
                    "escalated": _escalated,
                    "reason": _reason,
                }
            else:
                _stage_used = "l1"
                _reason = "accepted_l1"
                return {
                    "agent_result": l1_result,
                    "stage_used": _stage_used,
                    "escalated": _escalated,
                    "reason": _reason,
                }
