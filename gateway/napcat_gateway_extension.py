"""Branch-local gateway helpers for NapCat-specific runtime behavior."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, Iterable, List, Optional

from gateway.config import Platform
from gateway.napcat_metadata import (
    PREFETCHED_MEMORY_CONTEXT_KEY,
    REFERENCED_IMAGE_TYPES_KEY,
    REFERENCED_IMAGE_URLS_KEY,
)
from gateway.napcat_orchestrator_runtime import NapCatOrchestratorRuntime
from gateway.napcat_orchestrator_support import NapCatOrchestratorSupport
from gateway.napcat_promotion import NapCatPromotionSupport
from gateway.napcat_trace_helpers import resolve_napcat_trace_context
from gateway.napcat_transcript_helpers import (
    decorate_napcat_transcript_messages,
    handle_napcat_recall_event,
    load_transcript_messages_for_rewrite,
    napcat_recall_tombstone,
    napcat_transcript_metadata_for_event,
)
from gateway.orchestrator import GatewayChildState, GatewayOrchestratorSession, parse_orchestrator_decision, utc_now_iso
from gateway.platforms.base import AdapterSessionDefaults, AdapterTurnPlan, MessageEvent
from gateway.platforms.napcat_turn_context import (
    attach_napcat_turn_metadata,
    get_napcat_promotion_plan,
)
from gateway.runtime_extension import (
    GatewayRuntimeContext,
    build_default_gateway_runtime_context,
    emit_gateway_event,
    emit_gateway_trace_event,
)
from gateway.session import SessionSource

logger = logging.getLogger(__name__)


class NapCatGatewayExtension:
    """Own NapCat-specific runtime services without bloating gateway/run.py."""

    def __init__(
        self,
        runner: Any,
        *,
        raw_config_loader: Optional[Callable[[], Dict[str, Any]]] = None,
        agent_pending_sentinel: Any = None,
        cleanup_agent_fn: Optional[Callable] = None,
        runtime_context: Optional[GatewayRuntimeContext] = None,
    ):
        self.runner = runner
        self._raw_config_loader = raw_config_loader
        self._cleanup_agent_fn = cleanup_agent_fn
        self._reply_usage_footer_sessions: set[str] = set()
        self._orchestrator_sessions: Dict[str, GatewayOrchestratorSession] = {}
        self._observability_service: Optional[Any] = None
        self._runtime_context = runtime_context or build_default_gateway_runtime_context()
        self._orchestrator_support = NapCatOrchestratorSupport(
            runner,
            host=self,
            raw_config_loader=raw_config_loader,
            agent_pending_sentinel=agent_pending_sentinel,
        )
        self._orchestrator_runtime = NapCatOrchestratorRuntime(
            runner,
            support=self._orchestrator_support,
            host=self,
            runtime_context=self._runtime_context,
        )

    def orchestrator_support(self) -> NapCatOrchestratorSupport:
        return self._orchestrator_support

    def orchestrator_runtime(self) -> NapCatOrchestratorRuntime:
        return self._orchestrator_runtime

    def runtime_context(self) -> GatewayRuntimeContext:
        return self._runtime_context

    def build_platform_adapter(self, platform: Any, config: Any) -> Any:
        if platform != Platform.NAPCAT:
            return None
        from gateway.platforms.napcat import NapCatAdapter, check_napcat_requirements

        if not check_napcat_requirements():
            logger.warning("napcat platform requirements unavailable")
            return None
        return NapCatAdapter(config)

    @staticmethod
    def bind_connected_adapter(
        adapter: Any,
        *,
        session_model_overrides: Optional[dict[str, dict[str, str]]] = None,
    ) -> None:
        if hasattr(adapter, "_session_model_overrides_ref"):
            adapter._session_model_overrides_ref = session_model_overrides or {}

    def gateway_config(self) -> Any:
        return getattr(self.runner, "config", None)

    def session_key_for_source(self, source: SessionSource) -> str:
        """Resolve a session key through the runner's generic host seam."""
        resolver = getattr(self.runner, "_session_key_for_source", None)
        if not callable(resolver):
            raise AttributeError("Runner does not expose _session_key_for_source")
        return resolver(source)

    def load_raw_gateway_config(self) -> Dict[str, Any]:
        loader = self._raw_config_loader
        if not callable(loader):
            return {}
        loaded = loader()
        return loaded if isinstance(loaded, dict) else {}

    def append_to_transcript(self, session_id: str, payload: Dict[str, Any]) -> None:
        self.runner.session_store.append_to_transcript(session_id, payload)

    def get_running_agent(self, session_key: str) -> Optional[Any]:
        return getattr(self.runner, "_running_agents", {}).get(session_key)

    def get_running_agent_started_at(self, session_key: str) -> float:
        return float(getattr(self.runner, "_running_agents_ts", {}).get(session_key, 0) or 0)

    def get_session_title(self, session_id: str) -> str:
        session_db = getattr(self.runner, "_session_db", None)
        if not session_db or not session_id:
            return ""
        title = session_db.get_session_title(session_id)
        return str(title or "")

    def get_session_entry(self, session_key: str) -> Optional[Any]:
        getter = getattr(self.runner.session_store, "get_session_entry", None)
        return getter(session_key) if callable(getter) else None

    def rewrite_transcript(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        rewriter = getattr(self.runner.session_store, "rewrite_transcript", None)
        if callable(rewriter):
            rewriter(session_id, messages)

    def orchestrator_enabled_for_source(self, source: Optional[SessionSource]) -> bool:
        if not source:
            return False
        config = self._orchestrator_support._orchestrator_config()
        if not config:
            return False
        try:
            return bool(config.is_enabled_for_platform(source.platform))
        except Exception:
            return False

    def adapter_for_platform(self, platform: Optional[Any]) -> Optional[Any]:
        return getattr(self.runner, "adapters", {}).get(platform)

    def resolve_session_agent_runtime(
        self,
        *,
        source: SessionSource,
        session_key: str,
    ) -> tuple[Any, Dict[str, Any]]:
        resolver = getattr(self.runner, "_resolve_session_agent_runtime", None)
        if not callable(resolver):
            raise AttributeError("Runner does not expose _resolve_session_agent_runtime")
        return resolver(
            source=source,
            session_key=session_key,
        )

    def apply_runtime_override_payload(
        self,
        model: Any,
        runtime_kwargs: Dict[str, Any],
        override_payload: Dict[str, Any],
    ) -> tuple[Any, Dict[str, Any]]:
        resolver = getattr(self.runner, "_apply_runtime_override_payload", None)
        if not callable(resolver):
            raise AttributeError("Runner does not expose _apply_runtime_override_payload")
        return resolver(
            model,
            runtime_kwargs,
            override_payload,
        )

    async def run_agent(self, **kwargs: Any) -> Dict[str, Any]:
        runner = getattr(self.runner, "_run_agent", None)
        if not callable(runner):
            raise AttributeError("Runner does not expose _run_agent")
        return await runner(**kwargs)

    def load_transcript(self, session_id: str) -> List[Dict[str, Any]]:
        return self.runner.session_store.load_transcript(session_id)

    def response_reply_to_message_id(self, event: MessageEvent) -> Optional[str]:
        resolver = getattr(self.runner, "_response_reply_to_message_id", None)
        if callable(resolver):
            return resolver(event)
        return event.message_id

    def should_send_voice_reply(
        self,
        event: MessageEvent,
        text: str,
        agent_messages: List[Dict[str, Any]],
        *,
        already_sent: bool = False,
    ) -> bool:
        if not hasattr(self.runner, "_voice_mode"):
            return False
        resolver = getattr(self.runner, "_should_send_voice_reply", None)
        if not callable(resolver):
            return False
        return bool(
            resolver(
                event,
                text,
                agent_messages,
                already_sent=already_sent,
            )
        )

    async def send_voice_reply(self, event: MessageEvent, text: str) -> None:
        sender = getattr(self.runner, "_send_voice_reply", None)
        if not callable(sender):
            return
        await sender(event, text)

    async def deliver_media_from_response(
        self,
        text: str,
        event: MessageEvent,
        adapter: Any,
    ) -> None:
        sender = getattr(self.runner, "_deliver_media_from_response", None)
        if not callable(sender):
            return
        await sender(text, event, adapter)

    def adapter_for_source(self, source: Optional[SessionSource]) -> Optional[Any]:
        resolver = getattr(self.runner, "_adapter_for_source", None)
        if callable(resolver):
            return resolver(source)
        return self.adapter_for_platform(getattr(source, "platform", None))

    def trace_context_for_event(
        self,
        event: Any,
        *,
        source: Optional[SessionSource] = None,
    ) -> Optional[Any]:
        return resolve_napcat_trace_context(self, event, source=source)

    def trace_metadata_key(
        self,
        *,
        source: Optional[SessionSource] = None,
        adapter: Optional[Any] = None,
    ) -> str:
        resolved_adapter = adapter or self.adapter_for_source(source)
        raw_key = getattr(resolved_adapter, "TRACE_METADATA_KEY", "")
        metadata_key = raw_key.strip() if isinstance(raw_key, str) else ""
        if metadata_key:
            return metadata_key
        if getattr(getattr(source, "platform", None), "value", "") == "napcat":
            return "_napcat_trace_ctx"
        return ""

    def image_analysis_cache(
        self,
        *,
        platform: Optional[Any] = None,
        source: Optional[SessionSource] = None,
    ) -> tuple[Optional[Any], Optional[Any]]:
        adapter = self.adapter_for_source(source) if source is not None else self.adapter_for_platform(platform)
        if adapter is not None:
            try:
                get_image_analysis_cache = getattr(adapter, "get_image_analysis_cache", None)
                if callable(get_image_analysis_cache):
                    cache_pair = get_image_analysis_cache(
                        source or SessionSource(platform=platform, chat_id="", chat_type="dm")
                    )
                    if isinstance(cache_pair, (tuple, list)) and len(cache_pair) == 2:
                        return cache_pair[0], cache_pair[1]
            except Exception:
                return None, None
        target_platform = platform or getattr(source, "platform", None)
        if getattr(target_platform, "value", "") == "napcat":
            try:
                from gateway.platforms.napcat_vision_cache import (
                    cache_image_analysis,
                    get_cached_analysis,
                )

                return get_cached_analysis, cache_image_analysis
            except Exception:
                return None, None
        return None, None

    def should_emit_non_llm_status_messages(
        self,
        source: Optional[SessionSource],
    ) -> bool:
        adapter = self.adapter_for_source(source)
        if not adapter:
            return True
        return bool(getattr(adapter, "EMIT_NON_LLM_STATUS_MESSAGES", True))

    def background_tasks(self) -> set[Any]:
        tasks = getattr(self.runner, "_background_tasks", None)
        if tasks is None:
            self.runner._background_tasks = set()
            tasks = self.runner._background_tasks
        return tasks

    def register_background_task(self, task: asyncio.Task) -> None:
        background_tasks = self.background_tasks()
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    def _fallback_spawn_handoff_reply(
        self,
        *,
        current_child: Optional[GatewayChildState],
        next_child: Optional[GatewayChildState],
    ) -> str:
        current_goal = ""
        next_goal = ""
        if current_child is not None:
            current_goal = self._orchestrator_support.trim_status_text(
                current_child.goal,
                max_len=80,
            )
        if next_child is not None:
            next_goal = self._orchestrator_support.trim_status_text(
                next_child.goal,
                max_len=80,
            )
        if current_goal and next_goal:
            return f"I'll finish {current_goal} first, then continue with {next_goal}."
        if next_goal:
            return f"I queued {next_goal} and will continue once the current background task finishes."
        return "I queued the next background task and will continue once the current one finishes."

    async def run_orchestrator_turn(
        self,
        *,
        event: MessageEvent,
        source: SessionSource,
        session_entry: Any,
        session_key: str,
        history: List[Dict[str, Any]],
        context_prompt: str,
        platform_turn: AdapterTurnPlan,
        dynamic_disabled_skills: List[str],
        dynamic_disabled_toolsets: List[str],
        message_text: str,
        emit_trace_event: Optional[Callable[..., None]] = None,
        event_type_cls: Optional[Any] = None,
        severity_cls: Optional[Any] = None,
        complexity_threshold: Optional[int] = None,
    ) -> Optional[str]:
        runtime_context = self.runtime_context()
        emit_trace_event = emit_trace_event or runtime_context.emit_trace_event or emit_gateway_trace_event
        event_type_cls = event_type_cls or runtime_context.event_type_cls
        severity_cls = severity_cls or runtime_context.severity_cls
        if complexity_threshold is None:
            complexity_threshold = runtime_context.complexity_threshold

        support = self.orchestrator_support()
        runtime = self.orchestrator_runtime()
        session = self.get_or_create_orchestrator_session(session_key)
        session.main_agent_running = True
        trace_ctx = self.trace_context_for_event(event, source=source)
        emit_trace_event(
            trace_ctx,
            getattr(event_type_cls, "ORCHESTRATOR_TURN_STARTED", None),
            {
                "session_key": session_key,
                "message_preview": message_text[:200],
                "active_children": session.active_child_count(),
            },
        )
        try:
            turn_number = 1 + sum(1 for msg in history or [] if msg.get("role") == "user")
            routing_history = support.build_routing_history(
                session=session,
                history=history,
            )
            memory_context_snapshot = support.build_memory_context_snapshot(
                user_message=message_text,
                source=source,
                session_key=session_key,
                session_id=session_entry.session_id,
                turn_number=turn_number,
            )
            memory_context_block = str(memory_context_snapshot.get("combined_block") or "").strip()
            event_metadata = dict(getattr(event, "metadata", None) or {})
            if memory_context_block:
                event_metadata[PREFETCHED_MEMORY_CONTEXT_KEY] = memory_context_block
            event.metadata = event_metadata
            memory_sources = [
                source_name
                for source_name, content in (
                    ("builtin_memory", memory_context_snapshot.get("builtin_block")),
                    ("memory_provider_prefetch", memory_context_snapshot.get("memory_prefetch_fenced")),
                )
                if str(content or "").strip()
            ]
            emit_trace_event(
                trace_ctx,
                getattr(event_type_cls, "AGENT_MEMORY_USED", None),
                {
                    "stage": "context",
                    "source": "gateway_prefetch",
                    "agent_scope": "main_orchestrator",
                    "summary": (
                        f"Gateway prepared {len(memory_sources)} memory context block(s) for orchestrator"
                        if memory_sources else "No gateway-prefetched memory for orchestrator"
                    ),
                    "injection_sources": memory_sources,
                    "prefetch_query_preview": message_text[:200],
                    "provider_names": list(memory_context_snapshot.get("provider_names") or []),
                    "provider_count": int(memory_context_snapshot.get("provider_count") or 0),
                    "configured_provider": str(memory_context_snapshot.get("configured_provider") or "").strip(),
                    "provider_available": memory_context_snapshot.get("provider_available"),
                    "prefetch_attempted": bool(memory_context_snapshot.get("prefetch_attempted")),
                    "provider_error": str(memory_context_snapshot.get("provider_error") or "").strip(),
                    "builtin_memory_context": str(memory_context_snapshot.get("builtin_block") or "").strip(),
                    "memory_prefetch_by_provider": str(
                        memory_context_snapshot.get("memory_prefetch_by_provider") or ""
                    ).strip(),
                    "memory_prefetch_content": str(
                        memory_context_snapshot.get("memory_prefetch_content") or ""
                    ).strip(),
                    "memory_prefetch_fenced": str(
                        memory_context_snapshot.get("memory_prefetch_fenced") or ""
                    ).strip(),
                    "memory_prefetch_params_by_provider": str(
                        memory_context_snapshot.get("memory_prefetch_params_by_provider") or ""
                    ).strip(),
                    "gateway_memory_context": memory_context_block,
                },
            )
            controller_message = support.build_turn_message(
                user_message=message_text,
                source=source,
                session_entry=session_entry,
                platform_turn=platform_turn,
                session=session,
                event_metadata=event_metadata,
                memory_context_block=memory_context_block,
            )
            decision_text = ""
            try:
                structured_turn_context = support.build_turn_user_context(
                    user_message=message_text,
                    source=source,
                    session_entry=session_entry,
                    platform_turn=platform_turn,
                    session=session,
                    event_metadata=event_metadata,
                    memory_context_block=memory_context_block,
                )
                orchestrator_result = await runtime.run_orchestrator_agent(
                    session_key=session_key,
                    source=source,
                    history=routing_history,
                    user_message=controller_message,
                    turn_system_context=None,
                    turn_user_context="\n\n".join(
                        part
                        for part in (
                            structured_turn_context,
                            str(
                                getattr(platform_turn, "controller_context", "")
                                or ""
                            ).strip(),
                            str(getattr(platform_turn, "user_context", "") or "").strip(),
                        )
                        if part
                    ).strip(),
                    trace_ctx=trace_ctx,
                )
                decision_text = str(orchestrator_result.get("final_response") or "").strip()
                decision = parse_orchestrator_decision(decision_text)
            except Exception as exc:
                decision = runtime.heuristic_orchestrator_decision(
                    user_message=message_text,
                    source=source,
                    platform_turn=platform_turn,
                    session=session,
                    event_metadata=event_metadata,
                    failure_reason=str(exc) or decision_text,
                )
            decision.complexity_score = support.normalize_complexity_score(
                getattr(decision, "complexity_score", None),
                default=100,
            )
            raw_immediate_reply = str(decision.immediate_reply or "")
            decision.immediate_reply = support.sanitize_immediate_reply(
                raw_immediate_reply,
                max_len=160,
            )
            decision.spawn_handoff_reply = support.sanitize_immediate_reply(
                getattr(decision, "spawn_handoff_reply", "") or "",
                max_len=160,
            )
            if (
                raw_immediate_reply.strip()
                and not decision.immediate_reply
                and decision.respond_now
                and not decision.spawn_children
                and not decision.ignore_message
            ):
                decision = runtime.heuristic_orchestrator_decision(
                    user_message=message_text,
                    source=source,
                    platform_turn=platform_turn,
                    session=session,
                    event_metadata=event_metadata,
                    failure_reason=decision_text or raw_immediate_reply,
                )
                decision.complexity_score = support.normalize_complexity_score(
                    getattr(decision, "complexity_score", None),
                    default=100,
                )
                decision.immediate_reply = support.sanitize_immediate_reply(
                    decision.immediate_reply,
                    max_len=160,
                )
            active_child = support.relevant_active_child(session, message_text)
            used_status_reply_override = False
            if active_child is not None and (
                support.is_progress_query(message_text)
                or support.is_active_child_status_checkin(message_text)
            ):
                decision.complexity_score = 0
                decision.respond_now = True
                decision.immediate_reply = support.trim_status_text(
                    support.format_child_status(active_child),
                    max_len=160,
                )
                decision.spawn_children = []
                decision.cancel_children = []
                decision.ignore_message = False
                used_status_reply_override = True
                if not decision.reasoning_summary:
                    decision.reasoning_summary = (
                        "Answered an active child status query from the child board."
                    )
            requires_external_info = support.requires_external_info_handoff(message_text)
            if requires_external_info and not decision.ignore_message:
                if not decision.spawn_children:
                    decision.spawn_children = [
                        runtime.build_default_orchestrator_child_request(
                            user_message=message_text,
                            complexity_score=max(decision.complexity_score, 18),
                        )
                    ]
                if decision.respond_now or decision.complexity_score <= complexity_threshold:
                    decision.complexity_score = max(decision.complexity_score, 18)
                    decision.respond_now = False
                    decision.immediate_reply = ""
                    if not decision.reasoning_summary:
                        decision.reasoning_summary = (
                            "Short request still needs a child because it requires current external information."
                        )
            correction_handoff_required = support.is_user_correction(message_text)
            if correction_handoff_required and not decision.ignore_message and not used_status_reply_override:
                if not decision.spawn_children:
                    decision.spawn_children = [
                        runtime.build_default_orchestrator_child_request(
                            user_message=message_text,
                            complexity_score=max(decision.complexity_score, 24),
                        )
                    ]
                decision.complexity_score = max(decision.complexity_score, 24)
                decision.respond_now = False
                decision.immediate_reply = ""
                if not decision.reasoning_summary:
                    decision.reasoning_summary = (
                        "User correction requires full-agent handling so memory can be reconciled."
                    )
            if not decision.ignore_message and not used_status_reply_override:
                related_floor = support.related_complexity_floor(session, message_text)
                if related_floor is not None:
                    decision.complexity_score = max(decision.complexity_score, related_floor)
            if decision.spawn_children:
                for request in decision.spawn_children:
                    request.complexity_score = max(
                        support.normalize_complexity_score(
                            getattr(request, "complexity_score", None),
                            default=decision.complexity_score,
                        ),
                        decision.complexity_score,
                    )
            session.last_decision = decision
            session.last_decision_summary = decision.reasoning_summary or (
                "spawn child" if decision.spawn_children else "reply now" if decision.respond_now else "ignore"
            )
            emit_trace_event(
                trace_ctx,
                getattr(event_type_cls, "ORCHESTRATOR_DECISION_PARSED", None),
                {
                    "session_key": session_key,
                    "complexity_score": decision.complexity_score,
                    "respond_now": decision.respond_now,
                    "ignore_message": decision.ignore_message,
                    "spawn_count": len(decision.spawn_children),
                    "cancel_count": len(decision.cancel_children),
                    "used_fallback": decision.used_fallback,
                    "reasoning_summary": decision.reasoning_summary,
                    "raw_response": decision.raw_response,
                    "immediate_reply_preview": support.trim_status_text(
                        decision.immediate_reply,
                        max_len=160,
                    ),
                },
            )
            if decision.cancel_children:
                await self.cancel_orchestrator_children(
                    session_key,
                    child_ids=decision.cancel_children,
                    reason="cancelled by orchestrator",
                )
            active_child_before_spawn = (
                support.relevant_active_child(session, message_text)
                if decision.spawn_children and not decision.ignore_message
                else None
            )
            spawn_result = {"started": 0, "queued": 0, "reused": 0, "skipped": 0}
            if decision.spawn_children:
                spawn_result = await runtime.spawn_orchestrator_children(
                    event=event,
                    session_entry=session_entry,
                    source=source,
                    requests=decision.spawn_children,
                    context_prompt=context_prompt,
                    channel_prompt=getattr(event, "channel_prompt", None),
                    dynamic_disabled_skills=dynamic_disabled_skills,
                    dynamic_disabled_toolsets=dynamic_disabled_toolsets,
                    event_message_id=event.message_id,
                    platform_turn=platform_turn,
                    trace_ctx=trace_ctx,
                    originating_user_message=message_text,
                )
            accepted_child_ids = list(spawn_result.get("started_child_ids", [])) + list(
                spawn_result.get("queued_child_ids", [])
            )
            accepted_children = [
                session.active_children.get(child_id)
                for child_id in accepted_child_ids
                if session.active_children.get(child_id) is not None
            ]
            response = "" if decision.spawn_children else str(decision.immediate_reply or "").strip()
            if (
                decision.spawn_children
                and not decision.ignore_message
                and not response
                and active_child_before_spawn is not None
                and not spawn_result.get("reused")
                and (spawn_result.get("started") or spawn_result.get("queued"))
            ):
                response = str(getattr(decision, "spawn_handoff_reply", "") or "").strip()
                if not response:
                    next_child = accepted_children[0] if accepted_children else None
                    response = self._fallback_spawn_handoff_reply(
                        current_child=active_child_before_spawn,
                        next_child=next_child,
                    )
            if spawn_result["started"] or spawn_result.get("queued"):
                reply_semantics = runtime.classify_orchestrator_reply(response)
                suppressed_status_reply = False
                if response and reply_semantics == "status":
                    runtime.emit_orchestrator_turn_event(
                        trace_ctx,
                        event_type=getattr(event_type_cls, "ORCHESTRATOR_STATUS_REPLY", None),
                        session_key=session_key,
                        response_text=response,
                        reason="status_reply",
                        extra_payload={"reply_semantics": "status"},
                    )
                support.append_transcript_event(
                    session_entry.session_id,
                    role="user",
                    content=message_text,
                )
                for child_id in accepted_child_ids:
                    child = session.active_children.get(child_id)
                    if child is not None:
                        child.user_turn_recorded = True
                support.record_routing_turn(
                    session=session,
                    history=history,
                    user_message=message_text,
                    decision=decision,
                    response=response,
                    reply_semantics=reply_semantics,
                    spawn_result=spawn_result,
                    suppressed_status_reply=suppressed_status_reply,
                )
                if response and not decision.ignore_message and reply_semantics == "final":
                    support.append_transcript_event(
                        session_entry.session_id,
                        role="assistant",
                        content=response,
                        internal_source="main_agent",
                    )
                    runtime.emit_orchestrator_turn_event(
                        trace_ctx,
                        event_type=getattr(event_type_cls, "ORCHESTRATOR_TURN_COMPLETED", None),
                        session_key=session_key,
                        response_text=response,
                        reason="spawn_handoff",
                        extra_payload={"reply_semantics": "final"},
                    )
                    return response
                if response and reply_semantics == "status":
                    return response or None
                return None
            elif decision.spawn_children and not decision.ignore_message and not response:
                if (
                    getattr(support._orchestrator_config(), "child_progress_reply_enabled", True)
                    and spawn_result["reused"]
                ):
                    response = support.progress_reply(session, message_text)
                elif spawn_result["skipped"]:
                    response = "Background task capacity is full right now."
            reply_semantics = runtime.classify_orchestrator_reply(response)
            suppressed_status_reply = False
            if response and reply_semantics == "status":
                runtime.emit_orchestrator_turn_event(
                    trace_ctx,
                    event_type=getattr(event_type_cls, "ORCHESTRATOR_STATUS_REPLY", None),
                    session_key=session_key,
                    response_text=response,
                    reason="status_reply",
                    extra_payload={"reply_semantics": "status"},
                )
            if response and reply_semantics == "status":
                if not self.should_emit_non_llm_status_messages(source):
                    suppressed_status_reply = True
                    response = ""
            support.record_routing_turn(
                session=session,
                history=history,
                user_message=message_text,
                decision=decision,
                response=response,
                reply_semantics=reply_semantics,
                spawn_result=spawn_result,
                suppressed_status_reply=suppressed_status_reply,
            )
            support.append_transcript_event(
                session_entry.session_id,
                role="user",
                content=message_text,
            )
            if response and not decision.ignore_message and reply_semantics == "final":
                support.append_transcript_event(
                    session_entry.session_id,
                    role="assistant",
                    content=response,
                    internal_source="main_agent",
                )
                runtime.emit_orchestrator_turn_event(
                    trace_ctx,
                    event_type=getattr(event_type_cls, "ORCHESTRATOR_TURN_COMPLETED", None),
                    session_key=session_key,
                    response_text=response,
                    reason="respond_now",
                    extra_payload={"reply_semantics": "final"},
                )
                return response
            if decision.ignore_message:
                runtime.emit_orchestrator_turn_event(
                    trace_ctx,
                    event_type=getattr(event_type_cls, "ORCHESTRATOR_TURN_IGNORED", None),
                    session_key=session_key,
                    reason="ignore_message",
                )
                return None
            if reply_semantics == "status":
                return response or None
            if not response and not suppressed_status_reply:
                runtime.emit_orchestrator_turn_event(
                    trace_ctx,
                    event_type=getattr(event_type_cls, "ORCHESTRATOR_TURN_FAILED", None),
                    session_key=session_key,
                    reason="no_terminal_outcome",
                    severity=severity_cls.ERROR if severity_cls else None,
                    extra_payload={
                        "decision_summary": session.last_decision_summary,
                        "raw_response": decision.raw_response,
                    },
                )
            return response or None
        finally:
            session.main_agent_running = False

    def reply_usage_footer_sessions(self) -> set[str]:
        """Return a copy of the per-session reply-usage footer toggle set."""
        return set(self._reply_usage_footer_sessions)

    def replace_reply_usage_footer_sessions(self, values: Optional[Iterable[str]]) -> None:
        """Replace the tracked reply-usage footer sessions from test/setup code."""
        self._reply_usage_footer_sessions = {
            str(value).strip()
            for value in (values or ())
            if str(value).strip()
        }

    def reply_usage_footer_enabled(self, session_key: Optional[str]) -> bool:
        return bool(session_key) and session_key in self._reply_usage_footer_sessions

    async def handle_usagefooter_command(self, event: MessageEvent) -> str:
        """Handle /usagefooter toggle state for the current session."""
        args = event.get_command_args().strip().lower() or "status"
        session_key = self.session_key_for_source(event.source)

        if args == "status":
            state = "ON" if session_key in self._reply_usage_footer_sessions else "OFF"
            return (
                "📎 **Reply Usage Footer**\n\n"
                f"Current state: **{state}**\n\n"
                "_Usage:_ `/usagefooter <on|off|status>`"
            )

        if args == "on":
            self._reply_usage_footer_sessions.add(session_key)
            return (
                "📎 ✓ Reply usage footer: **ON**\n"
                "Per-reply token/cache/reasoning stats will be appended after each response in this chat."
            )

        if args == "off":
            self._reply_usage_footer_sessions.discard(session_key)
            return "📎 ✓ Reply usage footer: **OFF** for this chat."

        return (
            f"⚠️ Unknown argument: `{args}`\n\n"
            "**Valid options:** on, off, status"
        )

    def get_orchestrator_session(self, session_key: str) -> Optional[GatewayOrchestratorSession]:
        return self._orchestrator_sessions.get(session_key)

    def get_or_create_orchestrator_session(self, session_key: str) -> GatewayOrchestratorSession:
        session = self._orchestrator_sessions.get(session_key)
        if session is None:
            session = GatewayOrchestratorSession(session_key=session_key)
            self._orchestrator_sessions[session_key] = session
        return session

    def orchestrator_sessions(self) -> Dict[str, GatewayOrchestratorSession]:
        return self._orchestrator_sessions

    def replace_orchestrator_sessions(
        self,
        values: Optional[Dict[str, GatewayOrchestratorSession]],
    ) -> None:
        """Replace orchestrator session state through an explicit extension seam."""
        self._orchestrator_sessions = dict(values or {})

    def iter_orchestrator_sessions(self) -> Iterable[tuple[str, GatewayOrchestratorSession]]:
        return self._orchestrator_sessions.items()

    @staticmethod
    def normalize_adapter_turn_plan(plan: Any) -> AdapterTurnPlan:
        if isinstance(plan, AdapterTurnPlan):
            return plan
        if isinstance(plan, dict):
            turn_plan = AdapterTurnPlan(
                extra_prompt=str(plan.get("extra_prompt") or "").strip(),
                routing_prompt=str(plan.get("routing_prompt") or "").strip(),
                user_context=str(plan.get("user_context") or "").strip(),
                controller_context=str(plan.get("controller_context") or "").strip(),
                message_prefix=str(plan.get("message_prefix") or "").strip(),
                direct_response=str(plan.get("direct_response") or "").strip(),
                dynamic_disabled_skills=list(plan.get("dynamic_disabled_skills") or []),
                dynamic_disabled_toolsets=list(plan.get("dynamic_disabled_toolsets") or []),
                super_admin=bool(plan.get("super_admin")),
            )
            return attach_napcat_turn_metadata(turn_plan, plan)
        return AdapterTurnPlan()

    def orchestrator_session_keys(self) -> list[str]:
        return list(self._orchestrator_sessions.keys())

    def pop_orchestrator_session(self, session_key: str) -> Optional[GatewayOrchestratorSession]:
        return self._orchestrator_sessions.pop(session_key, None)

    def clear_orchestrator_sessions(self) -> None:
        self._orchestrator_sessions.clear()

    def observability_service(self) -> Optional[Any]:
        """Return the active NapCat observability runtime service, if any."""
        return self._observability_service

    async def cancel_orchestrator_children(
        self,
        session_key: str,
        *,
        child_ids: Optional[List[str]] = None,
        reason: str = "",
        clear_session: bool = False,
    ) -> None:
        session = self.get_orchestrator_session(session_key)
        if not session:
            return
        session.queue_drain_paused = bool(clear_session)
        targets = list(session.active_children.values())
        if child_ids:
            wanted = set(child_ids)
            targets = [child for child in targets if child.child_id in wanted]
        pending_tasks: List[asyncio.Task] = []
        task_to_child: Dict[asyncio.Task, GatewayChildState] = {}
        for child in targets:
            child.cancel_requested = True
            child.status = "cancelled"
            child.error = reason or child.error or "cancelled"
            child.finished_at = utc_now_iso()
            if reason or not child.summary:
                child.summary = reason or child.summary or child.error
            if hasattr(child, "record_progress"):
                child.record_progress(
                    kind="cancelled",
                    message="Child task was cancelled.",
                    step="cancelled",
                    tool=getattr(child, "current_tool", ""),
                )
            runtime_control = getattr(child, "runtime_control", None)
            if runtime_control is not None:
                runtime_control.cancel_requested = True
                runtime_control.interrupt(reason or "cancelled")
            task = getattr(child, "task", None)
            if task and not task.done():
                task.cancel()
                pending_tasks.append(task)
                task_to_child[task] = child
            elif not getattr(child, "lifecycle_finalized", False):
                self.orchestrator_runtime().finalize_orchestrator_child(
                    session=session,
                    child_state=child,
                )
        if pending_tasks:
            done, pending = await asyncio.wait(pending_tasks, timeout=2.0)
            for task in done:
                child = task_to_child.get(task)
                if child is None or getattr(child, "lifecycle_finalized", False):
                    continue
                self.orchestrator_runtime().finalize_orchestrator_child(
                    session=session,
                    child_state=child,
                )
            for task in pending:
                child = task_to_child.get(task)
                if child is None or getattr(child, "lifecycle_finalized", False):
                    continue
                self.orchestrator_runtime().finalize_orchestrator_child(
                    session=session,
                    child_state=child,
                )
        if clear_session:
            self.pop_orchestrator_session(session_key)
            evict_children = getattr(self.runner, "_evict_cached_child_agents", None)
            if callable(evict_children):
                evict_children(session_key)
            if session.main_agent is not None and self._cleanup_agent_fn:
                try:
                    self._cleanup_agent_fn(session.main_agent)
                except Exception:
                    pass
        else:
            session.queue_drain_paused = False

    def clear_orchestrator_session(self, session_key: str) -> None:
        session = self.pop_orchestrator_session(session_key)
        if not session:
            evict_children = getattr(self.runner, "_evict_cached_child_agents", None)
            if callable(evict_children):
                evict_children(session_key)
            return
        for child in session.active_children.values():
            task = getattr(child, "task", None)
            if task and not task.done():
                task.cancel()
        evict_children = getattr(self.runner, "_evict_cached_child_agents", None)
        if callable(evict_children):
            evict_children(session_key)
        if session.main_agent is not None and self._cleanup_agent_fn:
            try:
                self._cleanup_agent_fn(session.main_agent)
            except Exception:
                pass

    def running_agent_snapshot(self, session_key: str) -> Optional[Dict[str, Any]]:
        return self._orchestrator_support.running_agent_snapshot(session_key)

    @staticmethod
    def format_running_agent_status(snapshot: Optional[Dict[str, Any]]) -> str:
        return NapCatOrchestratorSupport.format_running_agent_status(snapshot)

    @staticmethod
    def is_progress_query(message: str) -> bool:
        return NapCatOrchestratorSupport.is_progress_query(message)

    @staticmethod
    def promotion_plan_for_turn(turn_plan: Any) -> Any:
        return get_napcat_promotion_plan(turn_plan)

    @staticmethod
    def promotion_support() -> Any:
        return NapCatPromotionSupport

    @staticmethod
    def orchestrator_internal_session_id(session_key: str) -> str:
        return NapCatOrchestratorSupport.orchestrator_internal_session_id(session_key)

    @staticmethod
    def orchestrator_child_session_id(parent_session_id: str, child_id: str) -> str:
        return NapCatOrchestratorSupport.orchestrator_child_session_id(parent_session_id, child_id)

    @staticmethod
    def orchestrator_fullagent_session_id(parent_session_id: str) -> str:
        return NapCatOrchestratorSupport.orchestrator_fullagent_session_id(parent_session_id)

    @staticmethod
    def orchestrator_fullagent_runtime_session_key(session_key: str) -> str:
        return NapCatOrchestratorSupport.orchestrator_fullagent_runtime_session_key(session_key)

    @staticmethod
    def orchestrator_child_runtime_session_key(session_key: str, child_id: str) -> str:
        return NapCatOrchestratorSupport.orchestrator_child_runtime_session_key(session_key, child_id)

    @staticmethod
    def is_orchestrator_status_reply(text: str) -> bool:
        return NapCatOrchestratorRuntime.is_orchestrator_status_reply(text)

    @staticmethod
    def classify_orchestrator_reply(text: str) -> str:
        return NapCatOrchestratorRuntime.classify_orchestrator_reply(text)

    async def start_observability_runtime(self) -> None:
        """Start the in-process NapCat observability runtime when enabled."""
        try:
            napcat_cfg = self.runner.config.platforms.get(getattr(Platform, "NAPCAT"))
            if napcat_cfg and napcat_cfg.enabled:
                from gateway.napcat_observability.runtime_service import (
                    NapcatObservabilityRuntimeService,
                )

                self._observability_service = NapcatObservabilityRuntimeService.from_loaded_config()
                await self._observability_service.start()
        except Exception:
            logger.warning("NapCat observability startup failed", exc_info=True)

    async def stop_observability_runtime(self) -> None:
        """Stop the in-process NapCat observability runtime when present."""
        if self._observability_service is None:
            return
        try:
            await self._observability_service.stop()
        except Exception:
            logger.warning("NapCat observability shutdown failed", exc_info=True)
        finally:
            self._observability_service = None

    def _napcat_session_defaults_from_config(
        self,
        source: Optional[SessionSource],
    ) -> AdapterSessionDefaults:
        """Resolve NapCat session defaults without leaking config parsing into the runner."""
        platform_name = getattr(getattr(source, "platform", None), "value", "")
        if not source or platform_name != "napcat" or source.chat_type != "group":
            return AdapterSessionDefaults()

        config = getattr(self.runner, "config", None)
        platform_cfg = None
        try:
            for candidate_platform, candidate_cfg in getattr(config, "platforms", {}).items():
                if getattr(candidate_platform, "value", "") == "napcat":
                    platform_cfg = candidate_cfg
                    break
        except Exception:
            platform_cfg = None

        raw_groups = getattr(platform_cfg, "extra", {}) or {}
        raw_groups = raw_groups.get("yolo_groups") or []
        if isinstance(raw_groups, str):
            raw_groups = [part.strip() for part in raw_groups.split(",") if part.strip()]

        configured_groups = set()
        for group_id in raw_groups:
            normalized = str(group_id or "").strip()
            if normalized.lower().startswith("group:"):
                normalized = normalized.split(":", 1)[1].strip()
            if normalized:
                configured_groups.add(normalized)

        return AdapterSessionDefaults(auto_yolo_default=source.chat_id in configured_groups)

    def apply_session_defaults(
        self,
        source: SessionSource,
        session_key: str,
        *,
        adapter: Optional[Any] = None,
    ) -> None:
        """Apply branch-local session defaults, including offline NapCat fallbacks."""
        defaults: Any = None
        if adapter is not None:
            try:
                session_defaults_for_source = getattr(adapter, "session_defaults_for_source", None)
                if callable(session_defaults_for_source):
                    defaults = session_defaults_for_source(source, session_key)
            except Exception as exc:
                logger.debug("Platform session defaults resolution failed: %s", exc)

        if defaults is None:
            defaults = self._napcat_session_defaults_from_config(source)

        if isinstance(defaults, dict):
            defaults = AdapterSessionDefaults(
                auto_yolo_default=defaults.get("auto_yolo_default"),
            )
        elif not isinstance(defaults, AdapterSessionDefaults):
            return

        auto_yolo_default = getattr(defaults, "auto_yolo_default", None)
        if auto_yolo_default is None:
            return

        from tools.approval import set_session_auto_yolo_default

        set_session_auto_yolo_default(session_key, bool(auto_yolo_default))

    def transcript_metadata_for_event(
        self,
        event: Optional[MessageEvent],
        source: Optional[SessionSource],
    ) -> Dict[str, Any]:
        return napcat_transcript_metadata_for_event(event, source)

    def referenced_image_inputs(
        self,
        event: Optional[MessageEvent],
        source: Optional[SessionSource],
    ) -> list[tuple[str, str]]:
        metadata = getattr(event, "metadata", None) or {}
        if getattr(getattr(source, "platform", None), "value", "") != "napcat":
            return []
        urls = list(metadata.get(REFERENCED_IMAGE_URLS_KEY) or [])
        types = list(metadata.get(REFERENCED_IMAGE_TYPES_KEY) or [])
        return [
            (str(path), str(types[index] if index < len(types) else ""))
            for index, path in enumerate(urls)
            if str(path or "").strip()
        ]

    def decorate_transcript_messages(
        self,
        messages: List[Dict[str, Any]],
        *,
        event: Optional[MessageEvent],
        source: Optional[SessionSource],
    ) -> List[Dict[str, Any]]:
        return decorate_napcat_transcript_messages(messages, event=event, source=source)

    def load_transcript_messages_for_rewrite(self, session_id: str) -> List[Dict[str, Any]]:
        return load_transcript_messages_for_rewrite(self.runner.session_store, session_id)

    @staticmethod
    def recall_tombstone(chat_type: str, *, missing_match: bool = False) -> str:
        return napcat_recall_tombstone(chat_type, missing_match=missing_match)

    async def handle_recall_event(self, recall_event: Any) -> None:
        await handle_napcat_recall_event(
            self,
            recall_event,
            emit_event_fn=emit_gateway_event,
        )

    def hydrate_event_trace_context(self, source: SessionSource, event: Any, session_entry: Any) -> None:
        """Backfill session identifiers onto the active branch trace context."""
        if source.platform != Platform.NAPCAT:
            return

        trace_ctx = self.trace_context_for_event(event, source=source)
        if trace_ctx is not None and getattr(event, "gateway_trace_ctx", None) is None:
            event.gateway_trace_ctx = trace_ctx
        if trace_ctx is not None and hasattr(trace_ctx, "session_key"):
            trace_ctx.session_key = session_entry.session_key
            trace_ctx.session_id = session_entry.session_id

        adapter = self.adapter_for_platform(source.platform)
        adapter_trace_ctx = getattr(adapter, "_current_trace_ctx", None)
        if adapter_trace_ctx is None or not hasattr(adapter_trace_ctx, "session_key"):
            return

        event_trace_id = getattr(trace_ctx, "trace_id", "") if trace_ctx is not None else ""
        adapter_trace_id = getattr(adapter_trace_ctx, "trace_id", "")
        if trace_ctx is None or not event_trace_id or adapter_trace_ctx is trace_ctx or adapter_trace_id == event_trace_id:
            adapter_trace_ctx.session_key = session_entry.session_key
            adapter_trace_ctx.session_id = session_entry.session_id
