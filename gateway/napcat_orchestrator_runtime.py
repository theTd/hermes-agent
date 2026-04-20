"""Branch-local NapCat orchestrator runtime helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

from gateway.config import Platform
from gateway.napcat_orchestrator_support import NapCatOrchestratorSupport
from gateway.orchestrator import (
    GatewayChildLaunchSpec,
    GatewayChildRuntimeControl,
    GatewayChildState,
    GatewayOrchestratorSession,
    OrchestratorChildRequest,
    OrchestratorDecision,
    new_child_id,
    normalize_dedupe_key,
    utc_now_iso,
)
from gateway.platforms.base import AdapterTurnPlan, MessageEvent
from gateway.runtime_extension import GatewayRuntimeContext, build_default_gateway_runtime_context
from gateway.session import SessionSource

_MEDIA_TAG_PATTERN = re.compile(
    r'''[`"']?MEDIA:\s*(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|(?:~/|/)\S+(?:[^\S\n]+\S+)*?\.(?:png|jpe?g|gif|webp|mp4|mov|avi|mkv|webm|ogg|opus|mp3|wav|m4a)(?=[\s`"',;:)\]}]|$)|\S+)[`"']?'''
)


class NapCatOrchestratorRuntime:
    """Own branch-local orchestrator runtime behavior outside gateway/run.py."""

    def __init__(
        self,
        runner: Any,
        *,
        support: NapCatOrchestratorSupport,
        host: Optional[Any] = None,
        runtime_context: Optional[GatewayRuntimeContext] = None,
    ) -> None:
        self.runner = runner
        self.support = support
        self.host = host if host is not None else runner
        self.runtime_context = runtime_context or build_default_gateway_runtime_context()

    def _event_types(self) -> Any:
        return self.runtime_context.event_type_cls

    def _severity_types(self) -> Any:
        return self.runtime_context.severity_cls

    def _bind_trace_context(self, trace_ctx: Optional[Any]):
        return self.runtime_context.bind_trace_context_fn(trace_ctx)

    def _emit_event(
        self,
        event_type: Optional[Any],
        payload: Dict[str, Any],
        *,
        trace_ctx: Optional[Any] = None,
        severity: Optional[Any] = None,
    ) -> bool:
        if not event_type:
            return False
        emitter = self.runtime_context.emit_event
        emitter(event_type, payload, trace_ctx=trace_ctx, severity=severity)
        return True

    def _emit_trace_event(
        self,
        trace_ctx: Optional[Any],
        event_type: Optional[Any],
        payload: Dict[str, Any],
        *,
        severity: Optional[Any] = None,
    ) -> bool:
        if not event_type:
            return False
        emitter = self.runtime_context.emit_trace_event
        if callable(emitter):
            emitter(trace_ctx, event_type, payload, severity=severity)
            return True
        return self._emit_event(
            event_type,
            payload,
            trace_ctx=trace_ctx,
            severity=severity,
        )

    def _resolve_fullagent_runtime_identity(
        self,
        *,
        session: GatewayOrchestratorSession,
        parent_session_id: str,
    ) -> tuple[str, str]:
        runtime_session_id = str(getattr(session, "fullagent_runtime_session_id", "") or "").strip()
        runtime_session_key = str(getattr(session, "fullagent_runtime_session_key", "") or "").strip()
        if runtime_session_id and runtime_session_key:
            return runtime_session_id, runtime_session_key

        return session.bind_fullagent_runtime(
            runtime_session_id=self.support.orchestrator_fullagent_session_id(parent_session_id),
            runtime_session_key=self.support.orchestrator_fullagent_runtime_session_key(session.session_key),
        )

    def build_default_orchestrator_child_request(
        self,
        *,
        user_message: str,
        config: Optional[Any] = None,
        complexity_score: Optional[int] = None,
    ) -> OrchestratorChildRequest:
        cfg = config or self.support._orchestrator_config()
        return OrchestratorChildRequest(
            goal=user_message,
            complexity_score=(
                self.support.normalize_complexity_score(
                    complexity_score,
                    default=18,
                )
                if complexity_score is not None
                else None
            ),
            may_reply_directly=bool(getattr(cfg, "child_direct_reply_enabled", True)),
            reply_style="concise",
            priority="normal",
            dedupe_key=normalize_dedupe_key(user_message),
        )

    def heuristic_orchestrator_decision(
        self,
        *,
        user_message: str,
        source: SessionSource,
        platform_turn: AdapterTurnPlan,
        session: GatewayOrchestratorSession,
        event_metadata: Optional[Dict[str, Any]] = None,
        failure_reason: str = "",
    ) -> OrchestratorDecision:
        from gateway.platforms.napcat_turn_context import get_napcat_promotion_plan

        msg = str(user_message or "").strip()
        lowered = msg.lower()
        config = self.support._orchestrator_config()
        signals = self.support.build_turn_signals(
            user_message=msg,
            source=source,
            event_metadata=event_metadata,
        )
        active_child = self.support.relevant_active_child(session, msg)
        if self.support.is_progress_query(msg):
            return OrchestratorDecision(
                complexity_score=1,
                respond_now=True,
                immediate_reply=self.support.progress_reply(session, msg),
                reasoning_summary="Fallback answered a child progress request from child board.",
                raw_response=failure_reason,
                used_fallback=True,
            )
        if active_child is not None and self.support.is_active_child_status_checkin(msg):
            return OrchestratorDecision(
                complexity_score=1,
                respond_now=True,
                immediate_reply=self.support.format_child_status(active_child),
                reasoning_summary="Fallback answered an active child status check-in.",
                raw_response=failure_reason,
                used_fallback=True,
            )
        duplicate = active_child
        if duplicate is not None and session.find_duplicate_child(normalize_dedupe_key(msg)) is not None:
            return OrchestratorDecision(
                complexity_score=1,
                respond_now=True,
                immediate_reply=self.support.format_child_status(duplicate),
                reasoning_summary="Fallback reused a similar active child.",
                raw_response=failure_reason,
                used_fallback=True,
            )
        group_related = self.support.group_message_clearly_agent_related(
            user_message=msg,
            source=source,
            session=session,
            history=session.routing_history,
            event_metadata=event_metadata,
        )
        banter_reply = self.support.group_banter_reply(
            user_message=msg,
            source=source,
            session=session,
            history=session.routing_history,
            event_metadata=event_metadata,
        )
        if source.chat_type != "dm" and not group_related and not banter_reply:
            return OrchestratorDecision(
                complexity_score=1,
                ignore_message=True,
                reasoning_summary=(
                    "Fallback ignored a group message that was neither agent-directed nor safe for an isolated banter interjection."
                ),
                raw_response=failure_reason,
                used_fallback=True,
            )
        if source.chat_type != "dm" and self.support.is_group_ignore_candidate(msg):
            return OrchestratorDecision(
                complexity_score=1,
                ignore_message=True,
                reasoning_summary="Fallback ignored low-value group chatter.",
                raw_response=failure_reason,
                used_fallback=True,
            )
        if source.chat_type != "dm" and banter_reply:
            return OrchestratorDecision(
                complexity_score=6,
                respond_now=True,
                immediate_reply=banter_reply,
                reasoning_summary="Fallback allowed a brief isolated group banter interjection.",
                raw_response=failure_reason,
                used_fallback=True,
            )
        if self.support.is_user_correction(msg):
            return OrchestratorDecision(
                complexity_score=24,
                respond_now=False,
                immediate_reply="",
                spawn_children=[
                    self.build_default_orchestrator_child_request(
                        user_message=msg,
                        config=config,
                        complexity_score=24,
                    )
                ],
                reasoning_summary=(
                    "Fallback routed a user correction to a child so the full agent can reconcile it."
                ),
                raw_response=failure_reason,
                used_fallback=True,
            )
        if self.support.is_explicit_memory_request(msg):
            return OrchestratorDecision(
                complexity_score=24,
                respond_now=False,
                immediate_reply="",
                spawn_children=[
                    self.build_default_orchestrator_child_request(
                        user_message=msg,
                        config=config,
                        complexity_score=24,
                    )
                ],
                reasoning_summary=(
                    "Fallback routed an explicit memory request to a child so the full agent can persist it."
                ),
                raw_response=failure_reason,
                used_fallback=True,
            )
        simple_reply = self.support.simple_reply(msg)
        if simple_reply:
            return OrchestratorDecision(
                complexity_score=1,
                respond_now=True,
                immediate_reply=simple_reply,
                reasoning_summary="Fallback directly answered a simple message.",
                raw_response=failure_reason,
                used_fallback=True,
            )
        requires_external_info = self.support.requires_external_info_handoff(msg)
        if requires_external_info:
            return OrchestratorDecision(
                complexity_score=18,
                respond_now=False,
                immediate_reply="",
                spawn_children=[
                    self.build_default_orchestrator_child_request(
                        user_message=msg,
                        config=config,
                        complexity_score=18,
                    )
                ],
                reasoning_summary="Fallback routed a short time-sensitive query to a child task.",
                raw_response=failure_reason,
                used_fallback=True,
            )
        if self.support.requires_device_state_handoff(msg):
            return OrchestratorDecision(
                complexity_score=18,
                respond_now=False,
                immediate_reply="",
                spawn_children=[
                    self.build_default_orchestrator_child_request(
                        user_message=msg,
                        config=config,
                        complexity_score=18,
                    )
                ],
                reasoning_summary=(
                    "Fallback routed a smart-home or device-state query to a child task instead of guessing."
                ),
                raw_response=failure_reason,
                used_fallback=True,
            )
        heavy_markers = (
            "fully implement",
            "implement",
            "implementation",
            "fix",
            "bug",
            "feature",
            "codebase",
            "repository",
            "repo",
            "analyze",
            "analysis",
            "debug",
            "deep",
            "trace",
            "investigate",
            "refactor",
            "write tests",
            "深入",
            "详细",
            "实现",
            "排查",
            "调查",
            "项目",
            "仓库",
            "代码库",
        )
        score = 2
        if msg:
            score = 18
        if any(marker in lowered for marker in heavy_markers):
            score += 38
        if len(msg) > 80:
            score += 12
        if len(msg) > 180:
            score += 15
        if msg.count("\n") >= 3:
            score += 10
        promotion_plan = get_napcat_promotion_plan(platform_turn)
        if promotion_plan.is_active():
            score += 12
        score = self.support.normalize_complexity_score(score, default=18)
        if self.support.complexity_score_requires_handoff(score):
            dedupe_key = normalize_dedupe_key(msg)
            duplicate = session.find_duplicate_child(dedupe_key)
            if duplicate:
                return OrchestratorDecision(
                    complexity_score=1,
                    respond_now=True,
                    immediate_reply=self.support.format_child_status(duplicate),
                    reasoning_summary="Fallback reused an equivalent active child task.",
                    raw_response=failure_reason,
                    used_fallback=True,
                )
            return OrchestratorDecision(
                complexity_score=score,
                respond_now=False,
                immediate_reply="",
                spawn_children=[
                    self.build_default_orchestrator_child_request(
                        user_message=msg,
                        config=config,
                        complexity_score=score,
                    )
                ],
                reasoning_summary="Fallback heuristic routed the request to a child task.",
                raw_response=failure_reason,
                used_fallback=True,
            )
        if source.chat_type != "dm" and (
            not msg or str(signals["trigger_reason"] or "").strip() == "no_trigger"
        ):
            return OrchestratorDecision(
                complexity_score=1,
                ignore_message=True,
                reasoning_summary="Fallback ignored an empty or non-trigger group message.",
                raw_response=failure_reason,
                used_fallback=True,
            )
        return OrchestratorDecision(
            complexity_score=score,
            respond_now=False,
            immediate_reply="",
            reasoning_summary="Fallback refused to guess a structured controller response.",
            raw_response=failure_reason,
            used_fallback=True,
        )

    async def run_orchestrator_agent(
        self,
        *,
        session_key: str,
        source: SessionSource,
        history: List[Dict[str, Any]],
        user_message: str,
        turn_system_context: str = "",
        turn_user_context: str = "",
        trace_ctx: Optional[Any] = None,
    ) -> Dict[str, Any]:
        import hashlib

        from hermes_constants import parse_reasoning_effort
        from agent.runtime_context import AgentRuntimeContext
        import run_agent as _run_agent_mod
        AIAgent = _run_agent_mod.AIAgent

        session = self.host.get_or_create_orchestrator_session(session_key)
        model, runtime_kwargs = self.host.resolve_session_agent_runtime(
            source=source,
            session_key=session_key,
        )
        config = self.support._orchestrator_config()
        override_payload = {}
        if config and getattr(config, "orchestrator_model", ""):
            override_payload["model"] = config.orchestrator_model
        if config and getattr(config, "orchestrator_provider", ""):
            override_payload["provider"] = config.orchestrator_provider
        if config and getattr(config, "orchestrator_base_url", ""):
            override_payload["base_url"] = config.orchestrator_base_url
        if config and getattr(config, "orchestrator_api_key", ""):
            override_payload["api_key"] = config.orchestrator_api_key
        if override_payload:
            model, runtime_kwargs = self.host.apply_runtime_override_payload(
                model,
                runtime_kwargs,
                override_payload,
            )
        reasoning_config = parse_reasoning_effort(
            str(getattr(config, "orchestrator_reasoning_effort", "") or "").strip()
        )
        orchestrator_system_prompt = self.support.build_system_prompt(source.chat_type)

        signature = json.dumps(
            {
                "model": model,
                "provider": runtime_kwargs.get("provider"),
                "base_url": runtime_kwargs.get("base_url"),
                "api_key_fingerprint": (
                    hashlib.sha256(str(runtime_kwargs.get("api_key") or "").encode()).hexdigest()
                    if runtime_kwargs.get("api_key")
                    else ""
                ),
                "reasoning_config": reasoning_config,
                "system_prompt": orchestrator_system_prompt,
            },
            sort_keys=True,
        )
        agent = session.main_agent
        if agent is None or session.main_agent_signature != signature:
            platform_key = "cli" if source.platform == Platform.LOCAL else source.platform.value
            runtime_context_kwargs = AgentRuntimeContext.from_source(
                source,
                gateway_session_key=session_key,
                agent_context="orchestrator",
            ).to_agent_kwargs()
            agent = AIAgent(
                model=model,
                **runtime_kwargs,
                **runtime_context_kwargs,
                max_iterations=1,
                quiet_mode=True,
                verbose_logging=False,
                enabled_toolsets=[],
                skip_context_files=True,
                skip_memory=True,
                reasoning_config=reasoning_config,
                ephemeral_system_prompt=None,
                session_id=self.support.orchestrator_internal_session_id(session_key),
                platform=platform_key,
                session_db=None,
                persist_session=False,
                split_session_on_compress=False,
            )
            session.main_agent = agent
            session.main_agent_signature = signature

        agent.ephemeral_system_prompt = None
        agent._cached_system_prompt = orchestrator_system_prompt
        _orchestrator_prompt = orchestrator_system_prompt

        def _fixed_system_prompt(*args, **kwargs):
            return _orchestrator_prompt

        agent._build_system_prompt = _fixed_system_prompt

        loop = asyncio.get_running_loop()
        event_types = self._event_types()
        agent_trace_ctx = (
            trace_ctx.child_span(agent_scope="main_orchestrator")
            if trace_ctx and hasattr(trace_ctx, "child_span")
            else trace_ctx
        )
        live_lock = threading.Lock()
        live_state = {
            "reasoning": {"content": "", "sequence": 0},
            "response": {"content": "", "sequence": 0},
        }

        def _emit_orchestrator_live_delta(kind: str, text: Optional[str]) -> None:
            if not agent_trace_ctx or not event_types:
                return
            if kind == "reasoning":
                event_type = event_types.AGENT_REASONING_DELTA
            elif kind == "response":
                event_type = event_types.AGENT_RESPONSE_DELTA
            else:
                return

            delta = "" if text is None else str(text)
            if not delta:
                return

            with live_lock:
                state = live_state[kind]
                state["content"] += delta
                state["sequence"] = int(state["sequence"]) + 1
                payload = {
                    "delta": delta,
                    "chars": len(state["content"]),
                    "delta_chars": len(delta),
                    "sequence": int(state["sequence"]),
                }

            self._emit_event(
                event_type,
                payload,
                trace_ctx=agent_trace_ctx,
            )

        def _emit_orchestrator_live_suffix(kind: str, full_text: Optional[str]) -> None:
            text = str(full_text or "")
            if not text:
                return
            with live_lock:
                streamed = str(live_state[kind]["content"] or "")
            if not streamed:
                _emit_orchestrator_live_delta(kind, text)
                return
            if text.startswith(streamed):
                suffix = text[len(streamed):]
                if suffix:
                    _emit_orchestrator_live_delta(kind, suffix)

        def _run_sync() -> Dict[str, Any]:
            prev_reasoning_cb = getattr(agent, "reasoning_callback", None)
            prev_stream_delta_cb = getattr(agent, "stream_delta_callback", None)
            try:
                agent.reasoning_callback = (
                    (lambda text: _emit_orchestrator_live_delta("reasoning", text))
                    if agent_trace_ctx and event_types
                    else None
                )
                agent.stream_delta_callback = (
                    (lambda text: _emit_orchestrator_live_delta("response", text))
                    if agent_trace_ctx and event_types
                    else None
                )
                routing_history = self.support.build_routing_history_for_model(
                    session=session,
                    history=history,
                    model=model,
                    runtime_kwargs=runtime_kwargs,
                    system_prompt=orchestrator_system_prompt,
                    user_message=user_message,
                    turn_system_context=turn_system_context or "",
                    turn_user_context=turn_user_context or "",
                )
                with self._bind_trace_context(agent_trace_ctx):
                    result = agent.run_conversation(
                        user_message=user_message,
                        conversation_history=routing_history,
                        turn_system_context=turn_system_context or None,
                        turn_user_context=turn_user_context or None,
                    )
                if agent_trace_ctx and event_types:
                    _emit_orchestrator_live_suffix(
                        "reasoning",
                        str(result.get("last_reasoning") or "").strip(),
                    )
                    _emit_orchestrator_live_suffix(
                        "response",
                        str(result.get("final_response") or "").strip(),
                    )
                return result
            finally:
                agent.reasoning_callback = prev_reasoning_cb
                agent.stream_delta_callback = prev_stream_delta_cb

        return await loop.run_in_executor(None, _run_sync)

    async def send_orchestrator_public_reply(
        self,
        *,
        event: MessageEvent,
        content: str,
        reply_to: Optional[str] = None,
        agent_messages: Optional[List[Dict[str, Any]]] = None,
        already_sent: bool = False,
    ) -> bool:
        text = str(content or "").strip()
        if not text:
            return False
        adapter = self.host.adapter_for_platform(event.source.platform)
        if not adapter:
            return False

        original_metadata = getattr(event, "metadata", None)
        metadata_obj = original_metadata if isinstance(original_metadata, dict) else {}
        if not isinstance(original_metadata, dict):
            setattr(event, "metadata", metadata_obj)
        previous_reply_to = metadata_obj.get("response_reply_to_message_id")
        if reply_to is not None:
            metadata_obj["response_reply_to_message_id"] = reply_to

        try:
            if (
                self.host.should_send_voice_reply(
                    event,
                    text,
                    list(agent_messages or []),
                    already_sent=already_sent,
                )
            ):
                await self.host.send_voice_reply(event, text)

            try:
                media_files, stripped = adapter.extract_media(text)
            except Exception:
                _log.debug("extract_media failed", exc_info=True)
                media_files, stripped = [], text
            try:
                images, text_content = adapter.extract_images(
                    stripped,
                    extra_domains=adapter.config.extra.get("image_url_domains"),
                )
            except Exception:
                _log.debug("extract_images failed", exc_info=True)
                images, text_content = [], stripped
            text_content = text_content.replace("[[audio_as_voice]]", "").strip()
            text_content = re.sub(r"MEDIA:\s*\S+", "", text_content).strip()
            try:
                local_files, text_content = adapter.extract_local_files(text_content)
            except Exception:
                _log.debug("extract_local_files failed", exc_info=True)
                local_files = []

            metadata = {"thread_id": event.source.thread_id} if event.source.thread_id else None
            if not already_sent and text_content:
                await adapter.send(
                    event.source.chat_id,
                    text_content,
                    reply_to=self.host.response_reply_to_message_id(event),
                    metadata=metadata,
                )
            if media_files or images or local_files:
                await self.host.deliver_media_from_response(text, event, adapter)
            return True
        finally:
            if reply_to is not None:
                if previous_reply_to is None:
                    metadata_obj.pop("response_reply_to_message_id", None)
                else:
                    metadata_obj["response_reply_to_message_id"] = previous_reply_to
            # Restore original metadata if it was not a dict
            if not isinstance(original_metadata, dict):
                event.metadata = original_metadata

    @staticmethod
    def orchestrator_child_trace_ctx(
        child_state: GatewayChildState,
        fallback_trace_ctx: Optional[Any] = None,
    ) -> Optional[Any]:
        return getattr(child_state, "trace_ctx", None) or fallback_trace_ctx

    def emit_orchestrator_turn_event(
        self,
        trace_ctx: Optional[Any],
        *,
        event_type: Optional[Any],
        session_key: str,
        response_text: str = "",
        reason: str = "",
        severity: Optional[Any] = None,
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not event_type:
            return
        payload = {
            "session_key": session_key,
            "response_preview": str(response_text or "")[:200],
            "reply_text": str(response_text or "")[:240],
            "reason": str(reason or "").strip(),
        }
        if extra_payload:
            payload.update(extra_payload)
        self._emit_trace_event(
            trace_ctx,
            event_type,
            payload,
            severity=severity,
        )

    def emit_orchestrator_child_terminal_event(
        self,
        *,
        child_state: GatewayChildState,
        event_type: Optional[Any],
        fallback_trace_ctx: Optional[Any] = None,
        payload: Optional[Dict[str, Any]] = None,
        severity: Optional[Any] = None,
    ) -> None:
        if not event_type or getattr(child_state, "terminal_event_emitted", False):
            return
        event_payload = {
            "session_key": getattr(child_state, "session_key", ""),
            "child_id": getattr(child_state, "child_id", ""),
            "goal": getattr(child_state, "goal", ""),
            "request_kind": getattr(child_state, "request_kind", ""),
            "status": getattr(child_state, "status", ""),
            "summary": getattr(child_state, "summary", "") or "",
            "runtime_session_id": getattr(child_state, "runtime_session_id", ""),
            "runtime_session_key": getattr(child_state, "runtime_session_key", ""),
        }
        if payload:
            event_payload.update(payload)
        self._emit_trace_event(
            self.orchestrator_child_trace_ctx(child_state, fallback_trace_ctx),
            event_type,
            event_payload,
            severity=severity,
        )
        child_state.terminal_event_emitted = True

    def append_orchestrator_child_result_to_transcript(
        self,
        session_id: str,
        *,
        session: GatewayOrchestratorSession,
        child_state: GatewayChildState,
        contents: List[str],
        reply_kind: str,
    ) -> None:
        texts = [
            str(item or "").strip()
            for item in (contents or [])
            if str(item or "").strip()
        ]
        if not texts:
            return
        if not child_state.user_turn_recorded:
            self.support.append_transcript_event(
                session_id,
                role="user",
                content=child_state.originating_user_message,
                child_id=child_state.child_id,
                internal_source="child_agent",
            )
            child_state.user_turn_recorded = True
        for idx, text in enumerate(texts):
            terminal_reply = idx == len(texts) - 1
            self.support.append_transcript_event(
                session_id,
                role="assistant",
                content=text,
                child_id=child_state.child_id,
                reply_kind=reply_kind if terminal_reply else "",
                internal_source="child_agent",
            )
            routing_entry = self.support.canonicalize_reply_history_json(
                text,
                complexity_score=child_state.complexity_score,
                history_source="full_agent",
                reply_kind=reply_kind if terminal_reply else "interim",
                child_id=child_state.child_id,
            )
            if routing_entry:
                session.append_routing_history_entry(
                    role="assistant",
                    content=routing_entry,
                    timestamp=datetime.now().isoformat(),
                )

    @staticmethod
    def _filtered_gateway_history_length(history: Optional[List[Dict[str, Any]]]) -> int:
        count = 0
        for msg in history or []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip()
            if not role or role in {"session_meta", "system"}:
                continue
            if "tool_calls" in msg or "tool_call_id" in msg or role == "tool":
                count += 1
                continue
            if msg.get("content"):
                count += 1
        return count

    def _child_turn_messages(
        self,
        *,
        result: Dict[str, Any],
        history: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        all_messages = list(result.get("messages", []) or [])
        history_offset = result.get("history_offset")
        if not isinstance(history_offset, int) or history_offset < 0:
            history_offset = self._filtered_gateway_history_length(history)
        elif result.get("history_rewritten"):
            history_offset = self._filtered_gateway_history_length(history)

        if len(all_messages) <= history_offset:
            return []
        return all_messages[history_offset:]

    @staticmethod
    def _promotion_protocol_for_turn(platform_turn: AdapterTurnPlan) -> Dict[str, Any]:
        from gateway.platforms.napcat_turn_context import get_napcat_promotion_plan

        promotion_plan = get_napcat_promotion_plan(platform_turn)
        return dict((getattr(promotion_plan, "config_snapshot", None) or {}).get("protocol") or {})

    def _sanitize_child_public_reply_text(
        self,
        text: str,
        *,
        protocol: Dict[str, Any],
    ) -> str:
        cleaned = str(text or "").strip()
        if not cleaned:
            return ""
        if not protocol:
            return self._strip_unusable_media_tags(cleaned)

        from gateway.napcat_promotion import NapCatPromotionSupport

        cleaned = str(NapCatPromotionSupport.sanitize_response(cleaned, protocol, "") or "").strip()
        return self._strip_unusable_media_tags(cleaned)

    @staticmethod
    def _strip_unusable_media_tags(text: str) -> str:
        cleaned = str(text or "").strip()
        if not cleaned or "MEDIA:" not in cleaned:
            return cleaned

        removed = False

        def _replace(match: re.Match[str]) -> str:
            nonlocal removed

            path = str(match.group("path") or "").strip()
            if len(path) >= 2 and path[0] == path[-1] and path[0] in "`\"'":
                path = path[1:-1].strip()
            path = path.lstrip("`\"'").rstrip("`\"',.;:)}]")
            expanded = os.path.expanduser(path)
            if not path or not (path.startswith("/") or path.startswith("~/")) or not os.path.isfile(expanded):
                removed = True
                return ""
            return match.group(0)

        cleaned = _MEDIA_TAG_PATTERN.sub(_replace, cleaned)
        if not removed:
            return cleaned.strip()

        if "MEDIA:" not in cleaned:
            cleaned = cleaned.replace("[[audio_as_voice]]", "")
        cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def resolve_child_transcript_replies(
        self,
        *,
        result: Dict[str, Any],
        platform_turn: AdapterTurnPlan,
        history: Optional[List[Dict[str, Any]]],
        terminal_content: str,
    ) -> List[str]:
        protocol = self._promotion_protocol_for_turn(platform_turn)
        terminal_reply = self._sanitize_child_public_reply_text(
            terminal_content,
            protocol=protocol,
        )
        turn_messages = self._child_turn_messages(result=result, history=history)

        if protocol and turn_messages:
            from gateway.napcat_promotion import NapCatPromotionSupport

            turn_messages = NapCatPromotionSupport.sanitize_transcript_messages(
                turn_messages,
                protocol,
                terminal_reply,
            )

        replies: List[str] = []
        for msg in turn_messages:
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role") or "").strip() != "assistant":
                continue
            if msg.get("tool_calls"):
                continue
            content = self._sanitize_child_public_reply_text(
                msg.get("content") or "",
                protocol=protocol,
            )
            if not content:
                continue
            if replies and replies[-1] == content:
                continue
            replies.append(content)

        if terminal_reply and (not replies or replies[-1] != terminal_reply):
            replies.append(terminal_reply)
        return replies

    def _make_child_launch_spec(
        self,
        *,
        event: MessageEvent,
        session_entry: Any,
        source: SessionSource,
        request: OrchestratorChildRequest,
        context_prompt: str,
        channel_prompt: Optional[str],
        dynamic_disabled_skills: List[str],
        dynamic_disabled_toolsets: List[str],
        event_message_id: Optional[str],
        platform_turn: AdapterTurnPlan,
        trace_ctx: Optional[Any],
        originating_user_message: str,
    ) -> GatewayChildLaunchSpec:
        return GatewayChildLaunchSpec(
            event=event,
            session_entry=session_entry,
            source=source,
            request=request,
            context_prompt=context_prompt,
            channel_prompt=channel_prompt,
            dynamic_disabled_skills=list(dynamic_disabled_skills or []),
            dynamic_disabled_toolsets=list(dynamic_disabled_toolsets or []),
            event_message_id=str(event_message_id or "").strip(),
            platform_turn=platform_turn,
            trace_ctx=trace_ctx,
            originating_user_message=originating_user_message,
        )

    def _build_child_state(
        self,
        *,
        session: GatewayOrchestratorSession,
        session_entry: Any,
        request: OrchestratorChildRequest,
        launch_spec: GatewayChildLaunchSpec,
        trace_ctx: Optional[Any],
    ) -> GatewayChildState:
        child_id = new_child_id()
        dedupe_key = (
            str(request.dedupe_key or "").strip()
            or normalize_dedupe_key(request.goal)
            or child_id
        )
        runtime_session_id, runtime_session_key = self._resolve_fullagent_runtime_identity(
            session=session,
            parent_session_id=session_entry.session_id,
        )
        return GatewayChildState(
            child_id=child_id,
            session_key=session_entry.session_key,
            goal=request.goal,
            originating_user_message=launch_spec.originating_user_message,
            runtime_session_id=runtime_session_id,
            runtime_session_key=runtime_session_key,
            request_kind=request.request_kind,
            complexity_score=request.complexity_score,
            may_reply_directly=request.may_reply_directly,
            reply_style=request.reply_style,
            priority=request.priority,
            model_tier=request.model_tier,
            turn_model_override=dict(request.turn_model_override or {}),
            allowed_toolsets=list(request.allowed_toolsets or []),
            dedupe_key=dedupe_key,
            runtime_control=GatewayChildRuntimeControl(
                runtime_session_key=runtime_session_key
            ),
            trace_ctx=(
                trace_ctx.child_span(
                    agent_scope="orchestrator_child",
                    child_id=child_id,
                )
                if trace_ctx and hasattr(trace_ctx, "child_span")
                else trace_ctx
            ),
            launch_spec=launch_spec,
        )

    def _emit_orchestrator_child_spawned(
        self,
        *,
        child_state: GatewayChildState,
        fallback_trace_ctx: Optional[Any],
    ) -> None:
        event_types = self._event_types()
        self._emit_trace_event(
            fallback_trace_ctx,
            getattr(event_types, "ORCHESTRATOR_CHILD_SPAWNED", None),
            {
                "session_key": child_state.session_key,
                "child_id": child_state.child_id,
                "goal": child_state.goal,
                "request_kind": child_state.request_kind,
                "status": child_state.status,
                "runtime_session_id": child_state.runtime_session_id,
                "runtime_session_key": child_state.runtime_session_key,
            },
        )

    def _emit_orchestrator_child_queued(
        self,
        *,
        child_state: GatewayChildState,
        fallback_trace_ctx: Optional[Any],
    ) -> None:
        event_types = self._event_types()
        self._emit_trace_event(
            fallback_trace_ctx,
            getattr(event_types, "ORCHESTRATOR_CHILD_QUEUED", None),
            {
                "session_key": child_state.session_key,
                "child_id": child_state.child_id,
                "goal": child_state.goal,
                "request_kind": child_state.request_kind,
                "status": child_state.status,
                "runtime_session_id": child_state.runtime_session_id,
                "runtime_session_key": child_state.runtime_session_key,
            },
        )

    def _start_orchestrator_child(
        self,
        *,
        session: GatewayOrchestratorSession,
        child_state: GatewayChildState,
        fallback_trace_ctx: Optional[Any],
    ) -> bool:
        launch_spec = getattr(child_state, "launch_spec", None)
        if launch_spec is None or child_state.cancel_requested or child_state.lifecycle_finalized:
            return False
        child_state.status = "running"
        child_state.launch_spec = None
        child_state.record_progress(
            kind="lifecycle",
            message="Child task starting.",
            step="running",
            tool=child_state.current_tool,
        )
        task = asyncio.create_task(
            self.run_orchestrator_child(
                event=launch_spec.event,
                session_entry=launch_spec.session_entry,
                source=launch_spec.source,
                request=launch_spec.request,
                child_state=child_state,
                context_prompt=launch_spec.context_prompt,
                channel_prompt=launch_spec.channel_prompt,
                dynamic_disabled_skills=list(launch_spec.dynamic_disabled_skills or []),
                dynamic_disabled_toolsets=list(launch_spec.dynamic_disabled_toolsets or []),
                event_message_id=launch_spec.event_message_id,
                platform_turn=launch_spec.platform_turn or AdapterTurnPlan(),
                trace_ctx=launch_spec.trace_ctx,
            )
        )
        child_state.task = task
        self.host.register_background_task(task)
        self._emit_orchestrator_child_spawned(
            child_state=child_state,
            fallback_trace_ctx=fallback_trace_ctx,
        )
        return True

    async def drain_orchestrator_children(
        self,
        session_key: str,
        *,
        fallback_trace_ctx: Optional[Any] = None,
    ) -> None:
        session = self.host.get_orchestrator_session(session_key)
        if not session or session.queue_drain_paused:
            return
        config = self.support._orchestrator_config()
        max_concurrency = int(getattr(config, "child_max_concurrency", 1) or 1)
        while session.running_child_count() < max_concurrency:
            child_state = session.next_queued_child()
            if child_state is None:
                break
            if child_state.cancel_requested:
                child_state.status = "cancelled"
                child_state.error = child_state.error or "cancelled"
                child_state.summary = child_state.summary or child_state.error
                self.finalize_orchestrator_child(
                    session=session,
                    child_state=child_state,
                    fallback_trace_ctx=fallback_trace_ctx,
                )
                continue
            if not self._start_orchestrator_child(
                session=session,
                child_state=child_state,
                fallback_trace_ctx=fallback_trace_ctx,
            ):
                child_state.status = "failed"
                child_state.error = child_state.error or "missing child launch context"
                child_state.summary = child_state.summary or child_state.error
                self.finalize_orchestrator_child(
                    session=session,
                    child_state=child_state,
                    fallback_trace_ctx=fallback_trace_ctx,
                )
                continue

    def finalize_orchestrator_child(
        self,
        *,
        session: GatewayOrchestratorSession,
        child_state: GatewayChildState,
        fallback_trace_ctx: Optional[Any] = None,
    ) -> None:
        if getattr(child_state, "lifecycle_finalized", False):
            return

        event_types = self._event_types()
        severity_types = self._severity_types()
        status = str(getattr(child_state, "status", "") or "failed").strip().lower()
        if status == "completed":
            self.emit_orchestrator_child_terminal_event(
                child_state=child_state,
                event_type=getattr(event_types, "ORCHESTRATOR_CHILD_COMPLETED", None),
                fallback_trace_ctx=fallback_trace_ctx,
                payload={
                    "result_preview": child_state.summary,
                    "tools_used": child_state.tool_usage,
                },
            )
        elif status == "cancelled":
            self.emit_orchestrator_child_terminal_event(
                child_state=child_state,
                event_type=getattr(event_types, "ORCHESTRATOR_CHILD_CANCELLED", None),
                fallback_trace_ctx=fallback_trace_ctx,
                payload={"reason": child_state.error or child_state.summary or "cancelled"},
            )
        else:
            child_state.status = "failed"
            self.emit_orchestrator_child_terminal_event(
                child_state=child_state,
                event_type=getattr(event_types, "ORCHESTRATOR_CHILD_FAILED", None),
                fallback_trace_ctx=fallback_trace_ctx,
                payload={
                    "error": child_state.error or child_state.summary or "unknown error",
                    "result_preview": child_state.summary,
                    "tools_used": child_state.tool_usage,
                },
                severity=getattr(severity_types, "ERROR", None) if severity_types else None,
            )

        runtime_control = getattr(child_state, "runtime_control", None)
        if runtime_control is not None:
            runtime_control.finished = True
        child_state.task = None
        child_state.launch_spec = None
        child_state.lifecycle_finalized = True
        self.support.invalidate_provider_memory_snapshot(session.session_key)
        session.archive_child(child_state)
        if not session.queue_drain_paused:
            try:
                task = asyncio.get_running_loop().create_task(
                    self.drain_orchestrator_children(
                        session.session_key,
                        fallback_trace_ctx=fallback_trace_ctx,
                    )
                )
            except RuntimeError:
                task = None
            if task is not None:
                self.host.register_background_task(task)

    async def run_orchestrator_child(
        self,
        *,
        event: MessageEvent,
        session_entry: Any,
        source: SessionSource,
        request: OrchestratorChildRequest,
        child_state: GatewayChildState,
        context_prompt: str,
        channel_prompt: Optional[str],
        dynamic_disabled_skills: List[str],
        dynamic_disabled_toolsets: List[str],
        event_message_id: Optional[str],
        platform_turn: AdapterTurnPlan,
        trace_ctx: Optional[Any],
    ) -> None:
        session_key = session_entry.session_key
        session = self.host.get_or_create_orchestrator_session(session_key)
        resolved_toolsets = self.support.resolve_child_toolsets(
            request,
            dynamic_disabled_toolsets=dynamic_disabled_toolsets,
        )
        child_state.status = "running"
        child_state.started_at = utc_now_iso()
        child_state.record_progress(
            kind="lifecycle",
            message="Child task started.",
            step="running",
        )

        loop = asyncio.get_running_loop()

        def _register_child_runtime(agent: Any, runtime_session_key: str) -> None:
            control = getattr(child_state, "runtime_control", None)
            if control is None:
                control = GatewayChildRuntimeControl()
                child_state.runtime_control = control
            control.agent = agent
            control.runtime_session_key = str(runtime_session_key or "").strip()
            control.cancel_requested = bool(child_state.cancel_requested)

        def _record_child_progress(event_kind: str, payload: Optional[Dict[str, Any]] = None) -> None:
            data = dict(payload or {})

            def _apply_update() -> None:
                if event_kind == "tool":
                    tool_name = str(data.get("tool_name") or "").strip()
                    preview = self.support.trim_status_text(
                        data.get("preview") or tool_name or "Tool started.",
                        max_len=180,
                    )
                    step = child_state.current_step or "running"
                    child_state.record_progress(
                        kind="tool.started",
                        message=preview,
                        step=step,
                        tool=tool_name,
                        remember_tool=True,
                    )
                elif event_kind == "step":
                    iteration = int(data.get("iteration") or 0)
                    child_state.record_progress(
                        kind="step",
                        message="",
                        step=f"iteration {iteration}",
                    )
                elif event_kind == "status":
                    message = self.support.trim_status_text(
                        data.get("message") or "",
                        max_len=180,
                    )
                    status_kind = str(data.get("status_type") or "status").strip() or "status"
                    child_state.record_progress(
                        kind=status_kind,
                        message=message or "Status updated.",
                        step=child_state.current_step or "running",
                    )
                elif event_kind == "interim_assistant":
                    text = self.support.trim_status_text(
                        data.get("text") or "",
                        max_len=180,
                    )
                    child_state.record_progress(
                        kind="assistant_commentary",
                        message="" if child_state.last_progress_message else text,
                        step=child_state.current_step or "running",
                    )

            loop.call_soon_threadsafe(_apply_update)

        try:
            history = self.host.load_transcript(session_entry.session_id)
            result = await self.host.run_agent(
                message=request.goal,
                context_prompt=context_prompt,
                history=history,
                source=source,
                session_id=child_state.runtime_session_id or session_entry.session_id,
                session_key=session_key,
                session_runtime_key=child_state.runtime_session_key
                or self.support.orchestrator_fullagent_runtime_session_key(session_key),
                parent_session_id=session_entry.session_id,
                event_message_id=event_message_id,
                channel_prompt=channel_prompt,
                turn_user_context=platform_turn.user_context,
                dynamic_disabled_skills=dynamic_disabled_skills,
                dynamic_disabled_toolsets=dynamic_disabled_toolsets,
                turn_model_override=self.support.resolve_child_model_override(
                    request,
                    platform_turn,
                ),
                trace_ctx=self.orchestrator_child_trace_ctx(child_state, trace_ctx),
                enabled_toolsets_override=resolved_toolsets or None,
                orchestrator_progress_callback=_record_child_progress,
                orchestrator_runtime_callback=_register_child_runtime,
            )
            if child_state.cancel_requested:
                child_state.status = "cancelled"
                child_state.finished_at = utc_now_iso()
                child_state.error = child_state.error or "cancelled"
                child_state.summary = child_state.summary or child_state.error
                child_state.record_progress(
                    kind="cancelled",
                    message="Child task was cancelled.",
                    step="cancelled",
                    tool=child_state.current_tool,
                )
                return
            child_failed = bool(result.get("failed"))
            child_state.status = "failed" if child_failed else "completed"
            child_state.finished_at = utc_now_iso()
            child_state.summary = str(result.get("final_response") or "").strip()[:1000]
            child_state.error = child_state.summary if child_failed else child_state.error
            child_state.tool_usage = [
                str(item).strip()
                for item in (self.support.extract_used_tool_names(result.get("messages", [])) or [])
                if str(item).strip()
            ]
            child_state.record_progress(
                kind="completed" if child_state.status == "completed" else "failed",
                message=child_state.summary
                or ("Task completed." if child_state.status == "completed" else "Task failed."),
                step=child_state.status,
                tool=child_state.current_tool,
            )
            response = str(result.get("final_response") or "").strip()
            if child_failed:
                error_reply = response or f"Background task failed: {child_state.error or 'unknown error'}"
                transcript_replies = self.resolve_child_transcript_replies(
                    result=result,
                    platform_turn=platform_turn,
                    history=history,
                    terminal_content=error_reply,
                )
                public_reply = transcript_replies[-1] if transcript_replies else error_reply
                self.append_orchestrator_child_result_to_transcript(
                    session_entry.session_id,
                    session=session,
                    child_state=child_state,
                    contents=transcript_replies,
                    reply_kind="error",
                )
                if request.may_reply_directly and public_reply:
                    await self.send_orchestrator_public_reply(
                        event=event,
                        content=public_reply,
                        reply_to=event_message_id,
                        agent_messages=result.get("messages", []),
                        already_sent=bool(result.get("already_sent")),
                    )
                    child_state.add_public_reply(public_reply, "error")
            else:
                transcript_replies = self.resolve_child_transcript_replies(
                    result=result,
                    platform_turn=platform_turn,
                    history=history,
                    terminal_content=response,
                )
                public_reply = transcript_replies[-1] if transcript_replies else response
                self.append_orchestrator_child_result_to_transcript(
                    session_entry.session_id,
                    session=session,
                    child_state=child_state,
                    contents=transcript_replies,
                    reply_kind="final",
                )
                if public_reply and request.may_reply_directly:
                    await self.send_orchestrator_public_reply(
                        event=event,
                        content=public_reply,
                        reply_to=event_message_id,
                        agent_messages=result.get("messages", []),
                        already_sent=bool(result.get("already_sent")),
                    )
                    child_state.add_public_reply(public_reply, "final")
        except asyncio.CancelledError:
            child_state.status = "cancelled"
            child_state.finished_at = utc_now_iso()
            child_state.error = child_state.error or "cancelled"
            child_state.summary = child_state.summary or child_state.error
            child_state.record_progress(
                kind="cancelled",
                message="Child task was cancelled.",
                step="cancelled",
                tool=child_state.current_tool,
            )
            raise
        except Exception as exc:
            if child_state.cancel_requested:
                child_state.status = "cancelled"
                child_state.error = child_state.error or str(exc) or "cancelled"
                child_state.summary = child_state.summary or child_state.error
            else:
                child_state.status = "failed"
                child_state.error = str(exc)
                child_state.summary = child_state.error
            child_state.finished_at = utc_now_iso()
            child_state.record_progress(
                kind="cancelled" if child_state.status == "cancelled" else "failed",
                message=child_state.error,
                step=child_state.status,
                tool=child_state.current_tool,
            )
            if child_state.status != "cancelled":
                error_reply = f"Background task failed: {child_state.error}"
                transcript_replies = self.resolve_child_transcript_replies(
                    result={},
                    platform_turn=platform_turn,
                    history=history,
                    terminal_content=error_reply,
                )
                public_reply = transcript_replies[-1] if transcript_replies else error_reply
                self.append_orchestrator_child_result_to_transcript(
                    session_entry.session_id,
                    session=session,
                    child_state=child_state,
                    contents=transcript_replies,
                    reply_kind="error",
                )
                if request.may_reply_directly:
                    await self.send_orchestrator_public_reply(
                        event=event,
                        content=public_reply,
                        reply_to=event_message_id,
                    )
                    child_state.add_public_reply(public_reply, "error")
        finally:
            self.finalize_orchestrator_child(
                session=session,
                child_state=child_state,
                fallback_trace_ctx=trace_ctx,
            )

    async def spawn_orchestrator_children(
        self,
        *,
        event: MessageEvent,
        session_entry: Any,
        source: SessionSource,
        requests: List[OrchestratorChildRequest],
        context_prompt: str,
        channel_prompt: Optional[str],
        dynamic_disabled_skills: List[str],
        dynamic_disabled_toolsets: List[str],
        event_message_id: Optional[str],
        platform_turn: AdapterTurnPlan,
        trace_ctx: Optional[Any],
        originating_user_message: str,
    ) -> Dict[str, Any]:
        session = self.host.get_or_create_orchestrator_session(session_entry.session_key)
        config = self.support._orchestrator_config()
        max_concurrency = int(getattr(config, "child_max_concurrency", 1) or 1)
        started = 0
        queued = 0
        reused = 0
        skipped = 0
        started_child_ids: List[str] = []
        queued_child_ids: List[str] = []
        event_types = self._event_types()

        for request in requests:
            request.dedupe_key = request.dedupe_key or normalize_dedupe_key(request.goal)
            request.complexity_score = self.support.normalize_complexity_score(
                getattr(request, "complexity_score", None),
                default=18,
            )
            request.may_reply_directly = bool(
                request.may_reply_directly and getattr(config, "child_direct_reply_enabled", True)
            )
            resolved_toolsets = self.support.resolve_child_toolsets(
                request,
                dynamic_disabled_toolsets=dynamic_disabled_toolsets,
            )
            request.allowed_toolsets = list(resolved_toolsets)
            duplicate = session.find_duplicate_child(request.dedupe_key)
            if duplicate is not None:
                reused += 1
                self._emit_trace_event(
                    trace_ctx,
                    getattr(event_types, "ORCHESTRATOR_CHILD_REUSED", None),
                    {
                        "session_key": session_entry.session_key,
                        "child_id": duplicate.child_id,
                        "goal": duplicate.goal,
                        "request_kind": duplicate.request_kind,
                        "status": duplicate.status,
                        "runtime_session_id": duplicate.runtime_session_id,
                        "runtime_session_key": duplicate.runtime_session_key,
                    },
                )
                continue
            launch_spec = self._make_child_launch_spec(
                event=event,
                session_entry=session_entry,
                source=source,
                request=request,
                context_prompt=context_prompt,
                channel_prompt=channel_prompt,
                dynamic_disabled_skills=dynamic_disabled_skills,
                dynamic_disabled_toolsets=dynamic_disabled_toolsets,
                event_message_id=event_message_id,
                platform_turn=platform_turn,
                trace_ctx=trace_ctx,
                originating_user_message=originating_user_message,
            )
            child_state = self._build_child_state(
                session=session,
                session_entry=session_entry,
                request=request,
                launch_spec=launch_spec,
                trace_ctx=trace_ctx,
            )
            child_id = child_state.child_id
            session.active_children[child_id] = child_state
            if session.running_child_count() < max_concurrency:
                if self._start_orchestrator_child(
                    session=session,
                    child_state=child_state,
                    fallback_trace_ctx=trace_ctx,
                ):
                    started += 1
                    started_child_ids.append(child_id)
                else:
                    skipped += 1
                    child_state.status = "failed"
                    child_state.error = "missing child launch context"
                    child_state.summary = child_state.error
                    self.finalize_orchestrator_child(
                        session=session,
                        child_state=child_state,
                        fallback_trace_ctx=trace_ctx,
                    )
            else:
                child_state.record_progress(
                    kind="queued",
                    message="Queued behind an active background task.",
                    step="queued",
                )
                queued += 1
                queued_child_ids.append(child_id)
                self._emit_orchestrator_child_queued(
                    child_state=child_state,
                    fallback_trace_ctx=trace_ctx,
                )
        return {
            "started": started,
            "queued": queued,
            "reused": reused,
            "skipped": skipped,
            "started_child_ids": started_child_ids,
            "queued_child_ids": queued_child_ids,
        }

    @classmethod
    def is_orchestrator_status_reply(cls, text: str) -> bool:
        normalized = str(text or "").strip().lower()
        if not normalized:
            return False
        return normalized.startswith(
            (
                "still working on:",
                "queued next:",
                "still working on the current request.",
                "no background task is running right now.",
                "there is no active background task right now.",
                "background task capacity is full right now.",
            )
        )

    @classmethod
    def classify_orchestrator_reply(cls, text: str) -> str:
        normalized = str(text or "").strip()
        if not normalized:
            return ""
        if cls.is_orchestrator_status_reply(normalized):
            return "status"
        return "final"
