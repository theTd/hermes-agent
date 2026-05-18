"""Branch-local gateway helpers for NapCat-specific runtime behavior."""

from __future__ import annotations

import asyncio
import logging
import time
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
from gateway.orchestrator import (
    GatewayChildState,
    GatewayOrchestratorSession,
    normalize_dedupe_key,
    parse_orchestrator_decision,
    utc_now_iso,
)
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

    @staticmethod
    def _sanitize_orchestrator_decision_replies(
        support: NapCatOrchestratorSupport,
        decision: Any,
    ) -> Any:
        decision.complexity_score = support.normalize_complexity_score(
            getattr(decision, "complexity_score", None),
            default=100,
        )
        raw_immediate_reply = str(getattr(decision, "immediate_reply", "") or "")
        sanitized_immediate_reply = support.sanitize_immediate_reply(
            raw_immediate_reply,
            max_len=160,
        )
        if sanitized_immediate_reply:
            decision.immediate_reply = sanitized_immediate_reply
        else:
            decision.immediate_reply = support.trim_status_text(
                raw_immediate_reply,
                max_len=160,
            )

        raw_spawn_handoff_reply = str(getattr(decision, "spawn_handoff_reply", "") or "")
        sanitized_spawn_handoff_reply = support.sanitize_immediate_reply(
            raw_spawn_handoff_reply,
            max_len=160,
        )
        if sanitized_spawn_handoff_reply:
            decision.spawn_handoff_reply = sanitized_spawn_handoff_reply
        else:
            decision.spawn_handoff_reply = support.trim_status_text(
                raw_spawn_handoff_reply,
                max_len=160,
            )
        return decision

    @staticmethod
    def _decision_requires_spawn_handoff_reply(
        session: GatewayOrchestratorSession,
        decision: Any,
    ) -> bool:
        if (
            bool(getattr(decision, "ignore_message", False))
            or not bool(getattr(decision, "spawn_children", None))
            or session.running_child_count() <= 0
        ):
            return False
        for request in list(getattr(decision, "spawn_children", None) or []):
            dedupe_key = str(getattr(request, "dedupe_key", "") or "").strip()
            goal = str(getattr(request, "goal", "") or "").strip()
            request_key = dedupe_key or normalize_dedupe_key(goal)
            if session.find_duplicate_child(request_key) is None:
                return True
        return False

    async def _repair_missing_spawn_handoff_reply(
        self,
        *,
        runtime: NapCatOrchestratorRuntime,
        support: NapCatOrchestratorSupport,
        session_key: str,
        source: SessionSource,
        routing_history: List[Dict[str, Any]],
        controller_message: str,
        turn_system_context: str,
        turn_user_context: Optional[str],
        trace_ctx: Optional[Any],
    ) -> Any:
        repair_system_context = "\n\n".join(
            part
            for part in (
                turn_system_context,
                "Contract repair for this same turn:",
                '- child_board currently has at least one running child.',
                '- If you choose action="spawn", payload.handoff_reply is required and must be a short user-facing acknowledgement.',
                "- Re-answer this same turn from scratch and return only one JSON object.",
            )
            if str(part or "").strip()
        ).strip()
        repair_result = await runtime.run_orchestrator_agent(
            session_key=session_key,
            source=source,
            history=routing_history,
            user_message=controller_message,
            turn_system_context=repair_system_context,
            turn_user_context=turn_user_context or None,
            trace_ctx=trace_ctx,
        )
        repaired_text = str(repair_result.get("final_response") or "").strip()
        repaired_decision = parse_orchestrator_decision(repaired_text)
        return self._sanitize_orchestrator_decision_replies(support, repaired_decision)

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
        message_prepare_started_at: Optional[float] = None,
        message_prepare_ended_at: Optional[float] = None,
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
                "timing": (
                    {
                        "node": "gateway.inbound_preprocess",
                        "label": "Inbound Preprocess",
                        "started_at": message_prepare_started_at,
                        "ended_at": message_prepare_ended_at,
                        "duration_ms": round(
                            max(0.0, message_prepare_ended_at - message_prepare_started_at) * 1000,
                            2,
                        ),
                    }
                    if (
                        message_prepare_started_at is not None
                        and message_prepare_ended_at is not None
                    )
                    else {}
                ),
            },
        )
        try:
            turn_number = 1 + sum(1 for msg in history or [] if msg.get("role") == "user")
            routing_history = support.build_routing_history(
                session=session,
                history=history,
            )
            event_metadata = dict(getattr(event, "metadata", None) or {})
            _router_memory_started_at = time.time()
            emit_trace_event(
                trace_ctx,
                getattr(event_type_cls, "AGENT_MEMORY_CALLED", None),
                {
                    "stage": "context",
                    "source": "gateway_router_memory",
                    "agent_scope": "main_orchestrator",
                    "summary": "Gateway router memory prefetch started",
                    "prefetch_query_preview": message_text[:200],
                },
            )
            router_memory_snapshot = support.build_router_memory_context_snapshot(
                user_message=message_text,
                source=source,
                session_key=session_key,
                session_id=session_entry.session_id,
                turn_number=turn_number,
            )
            _router_memory_ended_at = time.time()
            is_dm = source.chat_type == "dm"
            if is_dm:
                system_memory_block = str(
                    router_memory_snapshot.get("combined_block") or ""
                ).strip()
                user_memory_block = ""
            else:
                system_memory_block = str(
                    router_memory_snapshot.get("group_block") or ""
                ).strip()
                user_memory_block = str(
                    router_memory_snapshot.get("user_block") or ""
                ).strip()
                if not user_memory_block:
                    user_memory_block = str(
                        router_memory_snapshot.get("combined_block") or ""
                    ).strip()
                    system_memory_block = ""
            router_memory_context_block = system_memory_block
            _prefetched_ctx = (
                system_memory_block + ("\n\n" + user_memory_block if user_memory_block else "")
            ).strip()
            if _prefetched_ctx:
                event_metadata[PREFETCHED_MEMORY_CONTEXT_KEY] = _prefetched_ctx
            event.metadata = event_metadata
            router_memory_sources = [
                source_name
                for source_name, content in (
                    ("builtin_memory", router_memory_snapshot.get("builtin_block")),
                    ("memory_provider_prefetch", router_memory_snapshot.get("memory_prefetch_fenced")),
                )
                if str(content or "").strip()
            ]
            _router_memory_duration_ms = round(
                max(0.0, _router_memory_ended_at - _router_memory_started_at) * 1000, 2,
            )
            emit_trace_event(
                trace_ctx,
                getattr(event_type_cls, "AGENT_MEMORY_USED", None),
                {
                    "stage": "context",
                    "source": "gateway_router_memory",
                    "agent_scope": "main_orchestrator",
                    "duration_ms": _router_memory_duration_ms,
                    "summary": (
                        f"Gateway prepared {len(router_memory_sources)} router memory block(s) for orchestrator"
                        if router_memory_sources else "No router memory context prepared for orchestrator"
                    ),
                    "injection_sources": router_memory_sources,
                    "prefetch_query_preview": message_text[:200],
                    "provider_names": list(router_memory_snapshot.get("provider_names") or []),
                    "provider_count": int(router_memory_snapshot.get("provider_count") or 0),
                    "configured_provider": str(router_memory_snapshot.get("configured_provider") or "").strip(),
                    "provider_available": router_memory_snapshot.get("provider_available"),
                    "prefetch_attempted": bool(router_memory_snapshot.get("prefetch_attempted")),
                    "provider_error": str(router_memory_snapshot.get("provider_error") or "").strip(),
                    "builtin_memory_context": str(router_memory_snapshot.get("builtin_block") or "").strip(),
                    "memory_prefetch_by_provider": str(
                        router_memory_snapshot.get("memory_prefetch_by_provider") or ""
                    ).strip(),
                    "memory_prefetch_content": str(
                        router_memory_snapshot.get("memory_prefetch_content") or ""
                    ).strip(),
                    "memory_prefetch_fenced": str(
                        router_memory_snapshot.get("memory_prefetch_fenced") or ""
                    ).strip(),
                    "memory_prefetch_params_by_provider": str(
                        router_memory_snapshot.get("memory_prefetch_params_by_provider") or ""
                    ).strip(),
                    "provider_prefetch_cache_hit": bool(
                        router_memory_snapshot.get("provider_prefetch_cache_hit")
                    ),
                    "provider_runtime_reused": bool(
                        router_memory_snapshot.get("provider_runtime_reused")
                    ),
                    "provider_context_reused": bool(
                        router_memory_snapshot.get("provider_context_reused")
                    ),
                    "gateway_memory_context": router_memory_context_block,
                    "timing": {
                        "node": "orchestrator.router_memory",
                        "label": "Router Memory",
                        "started_at": _router_memory_started_at,
                        "ended_at": _router_memory_ended_at,
                        "duration_ms": _router_memory_duration_ms,
                    },
                },
            )
            full_memory_context_block = router_memory_context_block
            child_context_prompt = context_prompt
            controller_message = support.build_turn_message(
                user_message=message_text,
                source=source,
                session_entry=session_entry,
                platform_turn=platform_turn,
                session=session,
                event_metadata=event_metadata,
                memory_context_block=router_memory_context_block,
            )
            system_turn_context = support.build_turn_system_context(
                user_message=message_text,
                source=source,
                session_entry=session_entry,
                platform_turn=platform_turn,
                session=session,
                event_metadata=event_metadata,
                memory_context_block=system_memory_block,
            )
            turn_system_context = "\n\n".join(
                part
                for part in (
                    system_turn_context,
                    str(
                        getattr(platform_turn, "controller_context", "")
                        or ""
                    ).strip(),
                )
                if part
            ).strip()
            _platform_user_ctx = str(getattr(platform_turn, "user_context", "") or "").strip()
            turn_user_context = "\n\n".join(
                part
                for part in (
                    _platform_user_ctx,
                    user_memory_block,
                )
                if part
            ).strip() or None
            if user_memory_block and not is_dm:
                platform_turn.user_context = "\n\n".join(
                    part
                    for part in (_platform_user_ctx, user_memory_block)
                    if part
                ).strip()
            decision_text = ""
            try:
                orchestrator_result = await runtime.run_orchestrator_agent(
                    session_key=session_key,
                    source=source,
                    history=routing_history,
                    user_message=controller_message,
                    turn_system_context=turn_system_context,
                    turn_user_context=turn_user_context,
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
            decision = self._sanitize_orchestrator_decision_replies(support, decision)
            if (
                self._decision_requires_spawn_handoff_reply(session, decision)
                and not str(getattr(decision, "spawn_handoff_reply", "") or "").strip()
            ):
                try:
                    decision = await self._repair_missing_spawn_handoff_reply(
                        runtime=runtime,
                        support=support,
                        session_key=session_key,
                        source=source,
                        routing_history=routing_history,
                        controller_message=controller_message,
                        turn_system_context=turn_system_context,
                        turn_user_context=turn_user_context,
                        trace_ctx=trace_ctx,
                    )
                except Exception:
                    logger.warning(
                        "Failed to repair missing orchestrator spawn handoff reply",
                        exc_info=True,
                    )
            if (
                self._decision_requires_spawn_handoff_reply(session, decision)
                and not str(getattr(decision, "spawn_handoff_reply", "") or "").strip()
            ):
                logger.warning(
                    "Rejecting orchestrator spawn decision without required handoff reply while another child is running"
                )
                decision.spawn_children = []
            if not decision.ignore_message and decision.spawn_children:
                _provider_memory_started_at = time.time()
                emit_trace_event(
                    trace_ctx,
                    getattr(event_type_cls, "AGENT_MEMORY_CALLED", None),
                    {
                        "stage": "context",
                        "source": "gateway_provider_enrich",
                        "agent_scope": "main_orchestrator",
                        "summary": "Gateway provider memory enrich started",
                        "prefetch_query_preview": message_text[:200],
                    },
                )
                provider_memory_snapshot = support.build_provider_memory_context_snapshot_if_needed(
                    user_message=message_text,
                    source=source,
                    session_key=session_key,
                    session_id=session_entry.session_id,
                    turn_number=turn_number,
                )
                _provider_memory_ended_at = time.time()
                _provider_memory_duration_ms = round(
                    max(0.0, _provider_memory_ended_at - _provider_memory_started_at) * 1000, 2,
                )
                _enrich_builtin = str(router_memory_snapshot.get("builtin_block") or "").strip()
                if not is_dm:
                    _enrich_builtin = str(router_memory_snapshot.get("group_builtin_block") or "").strip()
                provider_memory_context_snapshot = support._compose_memory_context_snapshot(
                    builtin_block=_enrich_builtin,
                    provider_snapshot=provider_memory_snapshot,
                )
                provider_memory_context_block = str(
                    provider_memory_snapshot.get("provider_block") or ""
                ).strip()
                full_memory_context_block = str(
                    provider_memory_context_snapshot.get("combined_block")
                    or router_memory_context_block
                ).strip()
                if full_memory_context_block:
                    event_metadata[PREFETCHED_MEMORY_CONTEXT_KEY] = full_memory_context_block
                    event.metadata = event_metadata
                    _current_user_ctx = str(getattr(platform_turn, "user_context", "") or "").strip()
                    if full_memory_context_block not in _current_user_ctx:
                        platform_turn.user_context = "\n\n".join(
                            part for part in (_current_user_ctx, full_memory_context_block) if part
                        ).strip()
                if full_memory_context_block:
                    child_context_prompt = support.merge_memory_context_into_prompt(
                        context_prompt,
                        full_memory_context_block,
                    )
                provider_memory_sources = [
                    source_name
                    for source_name, content in (
                        ("memory_provider_prefetch", provider_memory_snapshot.get("memory_prefetch_fenced")),
                    )
                    if str(content or "").strip()
                ]
                emit_trace_event(
                    trace_ctx,
                    getattr(event_type_cls, "AGENT_MEMORY_USED", None),
                    {
                        "stage": "context",
                        "source": "gateway_provider_enrich",
                        "agent_scope": "main_orchestrator",
                        "duration_ms": _provider_memory_duration_ms,
                        "summary": (
                            f"Gateway prepared {len(provider_memory_sources)} provider memory block(s) after routing"
                            if provider_memory_sources
                            else "No provider memory context prepared after routing"
                        ),
                        "injection_sources": provider_memory_sources,
                        "prefetch_query_preview": message_text[:200],
                        "provider_names": list(provider_memory_context_snapshot.get("provider_names") or []),
                        "provider_count": int(provider_memory_context_snapshot.get("provider_count") or 0),
                        "configured_provider": str(
                            provider_memory_context_snapshot.get("configured_provider") or ""
                        ).strip(),
                        "provider_available": provider_memory_context_snapshot.get("provider_available"),
                        "prefetch_attempted": bool(provider_memory_context_snapshot.get("prefetch_attempted")),
                        "provider_error": str(provider_memory_context_snapshot.get("provider_error") or "").strip(),
                        "builtin_memory_context": str(router_memory_snapshot.get("builtin_block") or "").strip(),
                        "memory_prefetch_by_provider": str(
                            provider_memory_context_snapshot.get("memory_prefetch_by_provider") or ""
                        ).strip(),
                        "memory_prefetch_content": str(
                            provider_memory_context_snapshot.get("memory_prefetch_content") or ""
                        ).strip(),
                        "memory_prefetch_fenced": provider_memory_context_block,
                        "memory_prefetch_params_by_provider": str(
                            provider_memory_context_snapshot.get("memory_prefetch_params_by_provider") or ""
                        ).strip(),
                        "provider_prefetch_cache_hit": bool(
                            provider_memory_context_snapshot.get("provider_prefetch_cache_hit")
                        ),
                        "provider_runtime_reused": bool(
                            provider_memory_context_snapshot.get("provider_runtime_reused")
                        ),
                        "provider_context_reused": bool(
                            provider_memory_context_snapshot.get("provider_context_reused")
                        ),
                        "gateway_memory_context": full_memory_context_block,
                        "timing": {
                            "node": "orchestrator.provider_memory_enrich",
                            "label": "Provider Memory Enrich",
                            "started_at": _provider_memory_started_at,
                            "ended_at": _provider_memory_ended_at,
                            "duration_ms": round(
                                max(0.0, _provider_memory_ended_at - _provider_memory_started_at)
                                * 1000,
                                2,
                            ),
                        },
                    },
                )
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
                    context_prompt=child_context_prompt,
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
        self._orchestrator_support.reconcile_orchestrator_session_caches(
            set(self._orchestrator_sessions.keys())
        )

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
        """Safely clear all orchestrator sessions, cancelling children first."""
        for session_key in list(self._orchestrator_sessions.keys()):
            self.clear_orchestrator_session(session_key)

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
        # Only resume queue drain if we are clearing the entire session.
        # When clear_session=False we must not clobber a pre-existing pause.
        if clear_session:
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
    def orchestrator_cache_key_belongs_to_session(session_key: str, cache_key: str) -> bool:
        return NapCatOrchestratorSupport.orchestrator_cache_key_belongs_to_session(
            session_key,
            cache_key,
        )

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

                runner_config = self.runner.config.to_dict() if hasattr(self.runner.config, "to_dict") else dict(self.runner.config)
                self._observability_service = NapcatObservabilityRuntimeService.from_runner_config(runner_config)
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

    def check_command_permission(
        self,
        adapter: Any,
        source: SessionSource,
        command: Optional[str],
    ) -> tuple[bool, Optional[str]]:
        """Restrict gateway commands to super admins on NapCat."""
        if not adapter or not command:
            return True, None
        from gateway.config import Platform
        if source.platform != Platform.NAPCAT:
            return True, None
        is_super_admin = getattr(adapter, "is_super_admin", None)
        if callable(is_super_admin) and is_super_admin(source.user_id):
            return True, None
        return False, "NapCat commands are restricted to super admins."

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
