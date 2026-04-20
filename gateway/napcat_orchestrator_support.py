"""Branch-local support for NapCat gateway orchestration helpers."""

from __future__ import annotations

import copy
import json
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from gateway.orchestrator import (
    GatewayChildState,
    GatewayOrchestratorSession,
    OrchestratorChildRequest,
    OrchestratorDecision,
    dedupe_keys_similar,
    extract_orchestrator_complexity_marker,
    extract_orchestrator_routing_field,
    extract_orchestrator_reply_summary,
    is_orchestrator_routing_summary,
    normalize_dedupe_key,
    parse_orchestrator_decision,
    strip_orchestrator_complexity_marker,
)
from gateway.config import Platform, get_gateway_orchestrator_config
from gateway.napcat_metadata import (
    PREFETCHED_MEMORY_CONTEXT_KEY,
    RECENT_GROUP_CONTEXT_KEY,
    TRIGGER_REASON_KEY,
)
from gateway.napcat_promotion import NapCatPromotionSupport
from gateway.platforms.base import AdapterTurnPlan
from gateway.platforms.napcat_turn_context import get_napcat_promotion_plan
from gateway.session import SessionSource

logger = logging.getLogger(__name__)

_ORCHESTRATOR_L2_COMPLEXITY_THRESHOLD = 16
_ORCHESTRATOR_ROUTING_STABLE_PREFIX_MESSAGES = 8
_ORCHESTRATOR_ROUTER_MEMORY_TTL_SECONDS = 600


@dataclass
class _BuiltinMemoryCacheEntry:
    block: str = ""
    group_block: str = ""
    user_block: str = ""
    file_state: tuple[tuple[str, Optional[int], Optional[int]], ...] = ()


@dataclass
class _ProviderRuntimeHandle:
    cache_key: str
    session_key: str
    session_id: str
    provider_name: str
    profile_name: str
    manager: Any = None
    provider: Any = None
    initialized: bool = False
    init_kwargs: Dict[str, Any] = field(default_factory=dict)
    last_message_signature: str = ""
    last_snapshot: Dict[str, Any] = field(default_factory=dict)
    last_snapshot_ts: float = 0.0


class NapCatOrchestratorSupport:
    """Own branch-local orchestrator helpers outside gateway/run.py."""

    def __init__(
        self,
        runner: Any,
        *,
        host: Optional[Any] = None,
        raw_config_loader: Optional[Callable[[], Dict[str, Any]]] = None,
        agent_pending_sentinel: Any = None,
    ) -> None:
        self.runner = runner
        self.host = host if host is not None else runner
        self._raw_config_loader = raw_config_loader
        self._agent_pending_sentinel = agent_pending_sentinel
        self._builtin_memory_cache: Dict[str, _BuiltinMemoryCacheEntry] = {}
        self._provider_runtime_cache: Dict[str, _ProviderRuntimeHandle] = {}
        self._memory_cache_lock = threading.RLock()

    def _orchestrator_config(self):
        getter = getattr(self.host, "gateway_config", None)
        config = getter() if callable(getter) else getattr(self.runner, "config", None)
        return get_gateway_orchestrator_config(config, platform=Platform.NAPCAT)

    @staticmethod
    def _stable_json_dumps(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _normalize_turn_child_board(
        self,
        session: GatewayOrchestratorSession,
    ) -> Dict[str, Any]:
        board = session.child_board()
        active_children: List[Dict[str, str]] = []
        for raw_child in board.get("active_children") or []:
            if not isinstance(raw_child, dict):
                continue
            child_id = str(raw_child.get("child_id") or "").strip()
            goal = self.trim_status_text(raw_child.get("goal") or "", max_len=90)
            status = str(raw_child.get("status") or "").strip()
            entry = {
                "child_id": child_id,
                "goal": goal,
                "status": status,
            }
            active_children.append(entry)
        active_children.sort(
            key=lambda item: (
                item.get("status") or "",
                item.get("child_id") or "",
                item.get("goal") or "",
            )
        )
        return {
            "active_children": active_children,
            "last_decision_summary": self.trim_status_text(
                board.get("last_decision_summary") or "",
                max_len=120,
            ),
        }

    def _normalize_running_agent_status_for_prompt(
        self,
        snapshot: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, str]]:
        if not snapshot:
            return None
        normalized: Dict[str, str] = {
            "status": str(snapshot.get("status") or "").strip() or "running",
        }
        current_step = str(snapshot.get("current_step") or "").strip()
        current_tool = str(snapshot.get("current_tool") or "").strip()
        if current_step:
            normalized["current_step"] = current_step
        if current_tool:
            normalized["current_tool"] = current_tool
        return normalized

    def _build_turn_context_payload(
        self,
        *,
        user_message: str,
        source: SessionSource,
        session_entry: Any,
        platform_turn: AdapterTurnPlan,
        session: GatewayOrchestratorSession,
        event_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        platform_name = getattr(getattr(source, "platform", None), "value", "")
        running_agent_status = self._normalize_running_agent_status_for_prompt(
            self.running_agent_snapshot(
                getattr(session_entry, "session_key", "") or session.session_key
            )
        )
        signals = self.build_turn_signals(
            user_message=user_message,
            source=source,
            event_metadata=event_metadata,
        )
        session_context = {
            "session_key": getattr(session_entry, "session_key", ""),
            "session_id": getattr(session_entry, "session_id", ""),
            "session_title": "",
            "source": source.to_dict() if hasattr(source, "to_dict") else {},
        }
        session_title_getter = getattr(self.host, "get_session_title", None)
        if callable(session_title_getter) and session_context["session_id"]:
            try:
                session_context["session_title"] = (
                    session_title_getter(session_context["session_id"]) or ""
                )
            except Exception:
                logger.debug(
                    "Could not load orchestrator session title for prompt payload",
                    exc_info=True,
                )
        return {
            "platform": platform_name,
            "chat_type": getattr(source, "chat_type", ""),
            "session_context": session_context,
            "group_signal": {
                "trigger_reason": signals["trigger_reason"],
                "recent_group_context": self.trim_status_text(
                    signals["recent_group_context"] or "",
                    max_len=220,
                ),
            },
            "dynamic_disabled_skills": sorted(
                str(item).strip()
                for item in (platform_turn.dynamic_disabled_skills or [])
                if str(item).strip()
            ),
            "full_agent_status": running_agent_status,
            "child_board": self._normalize_turn_child_board(session),
        }

    def _build_turn_structured_context(
        self,
        *,
        user_message: str,
        source: SessionSource,
        session_entry: Any,
        platform_turn: AdapterTurnPlan,
        session: GatewayOrchestratorSession,
        event_metadata: Optional[Dict[str, Any]] = None,
        memory_context_block: str = "",
    ) -> str:
        turn_payload = self._build_turn_context_payload(
            user_message=user_message,
            source=source,
            session_entry=session_entry,
            platform_turn=platform_turn,
            session=session,
            event_metadata=event_metadata,
        )
        parts: list[str] = [
            "[Context note: Current gateway turn context. The following metadata is not user-authored; use it only for routing and decision-making.]\n"
            + self._stable_json_dumps(turn_payload)
        ]
        if memory_context_block:
            parts.append(
                "[Context note: Prefetched memory context for routing only. The following block is not user-authored.]\n"
                + str(memory_context_block).strip()
            )
        return "\n\n".join(part for part in parts if part).strip()

    def _orchestrator_context_length(self) -> int:
        config = self._orchestrator_config()
        try:
            return max(0, int(getattr(config, "orchestrator_context_length", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _routing_history_max_entries(self) -> int:
        config = self._orchestrator_config()
        try:
            return max(0, int(getattr(config, "routing_history_max_entries", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _routing_stable_prefix_messages(self) -> int:
        config = self._orchestrator_config()
        try:
            return max(
                0,
                int(
                    getattr(
                        config,
                        "routing_stable_prefix_messages",
                        _ORCHESTRATOR_ROUTING_STABLE_PREFIX_MESSAGES,
                    )
                    or _ORCHESTRATOR_ROUTING_STABLE_PREFIX_MESSAGES
                ),
            )
        except (TypeError, ValueError):
            return _ORCHESTRATOR_ROUTING_STABLE_PREFIX_MESSAGES

    def _load_raw_config(self) -> Dict[str, Any]:
        loader = getattr(self.host, "load_raw_gateway_config", None)
        if callable(loader):
            try:
                loaded = loader()
            except Exception:
                logger.debug("NapCat raw config loader failed", exc_info=True)
                return {}
            return loaded if isinstance(loaded, dict) else {}

        loader = self._raw_config_loader
        if not callable(loader):
            return {}
        try:
            loaded = loader()
        except Exception:
            logger.debug("NapCat raw config loader failed", exc_info=True)
            return {}
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _message_signature(message: str) -> str:
        normalized = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not normalized:
            return ""
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_file_state(path: Optional[Path]) -> tuple[str, Optional[int], Optional[int]]:
        if path is None:
            return ("", None, None)
        try:
            stat = path.stat()
        except OSError:
            return (str(path), None, None)
        return (str(path), int(getattr(stat, "st_mtime_ns", 0) or 0), int(stat.st_size))

    def _memory_config(self) -> Any:
        runner_config = getattr(self.runner, "config", None)
        memory_cfg = getattr(runner_config, "memory", None)
        if not memory_cfg and callable(self._raw_config_loader):
            raw_cfg = self._load_raw_config()
            memory_cfg = raw_cfg.get("memory", {}) if isinstance(raw_cfg, dict) else {}
        return memory_cfg

    @staticmethod
    def _config_get(memory_cfg: Any, name: str, default: Any) -> Any:
        if isinstance(memory_cfg, dict):
            return memory_cfg.get(name, default)
        return getattr(memory_cfg, name, default)

    def _configured_memory_provider_name(self) -> str:
        memory_cfg = self._memory_config()
        return str(self._config_get(memory_cfg, "provider", "") or "").strip()

    @staticmethod
    def _active_profile_name() -> str:
        try:
            from hermes_cli.profiles import get_active_profile_name

            return str(get_active_profile_name() or "").strip()
        except Exception:
            return ""

    def invalidate_provider_memory_snapshot(self, session_key: str) -> None:
        cleaned_session_key = str(session_key or "").strip()
        if not cleaned_session_key:
            return
        with self._memory_cache_lock:
            for handle in self._provider_runtime_cache.values():
                if handle.session_key != cleaned_session_key:
                    continue
                provider = getattr(handle, "provider", None)
                should_invalidate = True
                checker = getattr(provider, "should_invalidate_cached_prefetch", None)
                if callable(checker):
                    try:
                        should_invalidate = bool(checker())
                    except Exception:
                        logger.debug(
                            "Provider cache invalidation hint failed; invalidating snapshot",
                            exc_info=True,
                        )
                        should_invalidate = True
                if not should_invalidate:
                    continue
                handle.last_message_signature = ""
                handle.last_snapshot = {}
                handle.last_snapshot_ts = 0.0

    def drop_orchestrator_session_caches(self, session_key: str) -> None:
        cleaned_session_key = str(session_key or "").strip()
        if not cleaned_session_key:
            return
        with self._memory_cache_lock:
            stale_keys = [
                cache_key
                for cache_key, handle in self._provider_runtime_cache.items()
                if handle.session_key == cleaned_session_key
            ]
            for cache_key in stale_keys:
                handle = self._provider_runtime_cache.pop(cache_key, None)
                if handle is None or not getattr(handle, "initialized", False):
                    continue
                try:
                    handle.manager.shutdown_all()
                except Exception:
                    logger.debug("Could not shut down cached orchestrator memory provider", exc_info=True)

    def reconcile_orchestrator_session_caches(self, active_session_keys: set[str]) -> None:
        active = {str(key or "").strip() for key in active_session_keys if str(key or "").strip()}
        with self._memory_cache_lock:
            stale_sessions = {
                handle.session_key
                for handle in self._provider_runtime_cache.values()
                if handle.session_key not in active
            }
        for session_key in stale_sessions:
            self.drop_orchestrator_session_caches(session_key)

    def append_transcript_event(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        child_id: str = "",
        reply_kind: str = "",
        internal_source: str = "",
    ) -> None:
        text = str(content or "").strip()
        if not text:
            return
        payload: Dict[str, Any] = {
            "role": role,
            "content": text,
            "timestamp": datetime.now().isoformat(),
        }
        if child_id:
            payload["child_id"] = child_id
        if reply_kind:
            payload["reply_kind"] = reply_kind
        if internal_source:
            payload["internal_source"] = internal_source
        append_fn = getattr(self.host, "append_to_transcript", None)
        if callable(append_fn):
            append_fn(session_id, payload)
            return
        self.runner.session_store.append_to_transcript(session_id, payload)

    def _get_running_agent(self, session_key: str) -> Optional[Any]:
        getter = getattr(self.host, "get_running_agent", None)
        if callable(getter):
            return getter(session_key)
        return None

    def _get_running_agent_started_at(self, session_key: str) -> float:
        getter = getattr(self.host, "get_running_agent_started_at", None)
        if callable(getter):
            return float(getter(session_key) or 0)
        return 0.0

    @staticmethod
    def trim_status_text(text: str, *, max_len: int = 220) -> str:
        cleaned = re.sub(r"\s+", " ", str(text or "").strip())
        if len(cleaned) <= max_len:
            return cleaned
        return cleaned[: max_len - 3].rstrip() + "..."

    @classmethod
    def canonicalize_reply_history_json(
        cls,
        text: str,
        *,
        complexity_score: Optional[int] = None,
        history_source: str = "",
        reply_kind: str = "",
        child_id: str = "",
        max_len: int = 500,
    ) -> str:
        normalized_text = cls.trim_status_text(text, max_len=max_len)
        if not normalized_text:
            return ""
        payload: Dict[str, Any] = {
            "action": "reply",
            "payload": {
                "text": normalized_text,
            },
        }
        normalized_score = (
            cls.normalize_complexity_score(complexity_score, default=1)
            if complexity_score is not None else 1
        )
        payload["complexity_score"] = normalized_score
        cleaned_history_source = str(history_source or "").strip()
        cleaned_reply_kind = str(reply_kind or "").strip()
        cleaned_child_id = str(child_id or "").strip()
        if cleaned_history_source:
            payload["history_source"] = cleaned_history_source
        if cleaned_reply_kind:
            payload["reply_kind"] = cleaned_reply_kind
        if cleaned_child_id:
            payload["child_id"] = cleaned_child_id
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def canonicalize_history_decision(
        cls,
        decision: OrchestratorDecision,
        *,
        history_source: str = "",
        reply_kind: str = "",
        child_id: str = "",
    ) -> str:
        action = "reply"
        payload: Dict[str, Any] = {}

        if decision.ignore_message:
            action = "ignore"
        elif decision.spawn_children:
            action = "spawn"
            if len(decision.spawn_children) == 1:
                payload["goal"] = str(decision.spawn_children[0].goal or "").strip()
            else:
                payload["spawn_children"] = [
                    {
                        key: value
                        for key, value in {
                            "goal": str(item.goal or "").strip(),
                            "complexity_score": (
                                cls.normalize_complexity_score(
                                    item.complexity_score,
                                    default=decision.complexity_score,
                                )
                                if item.complexity_score is not None else None
                            ),
                        }.items()
                        if value not in (None, "", [])
                    }
                    for item in decision.spawn_children
                    if str(getattr(item, "goal", "") or "").strip()
                ]
            handoff_reply = str(getattr(decision, "spawn_handoff_reply", "") or "").strip()
            if handoff_reply:
                payload["handoff_reply"] = handoff_reply
        else:
            payload["text"] = str(decision.immediate_reply or "").strip()

        if decision.cancel_children:
            payload["cancel_children"] = list(decision.cancel_children)

        history_payload: Dict[str, Any] = {
            "action": action,
            "payload": payload,
            "complexity_score": cls.normalize_complexity_score(
                decision.complexity_score,
                default=1,
            ),
        }
        cleaned_history_source = str(history_source or "").strip()
        cleaned_reply_kind = str(reply_kind or "").strip()
        cleaned_child_id = str(child_id or "").strip()
        if cleaned_history_source:
            history_payload["history_source"] = cleaned_history_source
        if cleaned_reply_kind:
            history_payload["reply_kind"] = cleaned_reply_kind
        if cleaned_child_id:
            history_payload["child_id"] = cleaned_child_id
        return json.dumps(
            history_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def canonicalize_routing_summary_text(
        cls,
        text: str,
        *,
        history_source: str = "",
        max_len: int = 500,
    ) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""

        stripped = strip_orchestrator_complexity_marker(raw)
        summary_text = stripped
        reply_text = ""
        if "Routing decision" in stripped:
            prefix, marker, suffix = stripped.partition("Routing decision")
            if marker:
                reply_text = cls.trim_status_text(prefix, max_len=max_len)
                summary_text = f"{marker}{suffix}".strip()

        if not is_orchestrator_routing_summary(summary_text):
            return ""

        action = str(extract_orchestrator_routing_field(summary_text, "action") or "").strip().lower() or "reply"
        cancel_children = [
            item.strip()
            for item in str(extract_orchestrator_routing_field(summary_text, "cancel_children") or "").split(",")
            if item.strip()
        ]
        normalized_score = cls.normalize_complexity_score(
            extract_orchestrator_complexity_marker(raw)
            or extract_orchestrator_routing_field(summary_text, "complexity_score"),
            default=1,
        )

        if action == "reply":
            effective_reply = reply_text or cls.trim_status_text(
                extract_orchestrator_reply_summary(summary_text),
                max_len=max_len,
            )
            if not effective_reply:
                return ""
            return cls.canonicalize_history_decision(
                OrchestratorDecision(
                    complexity_score=normalized_score,
                    respond_now=True,
                    immediate_reply=effective_reply,
                    cancel_children=cancel_children,
                ),
                history_source=history_source or "orchestrator",
            )

        if action == "spawn":
            goals_field = str(extract_orchestrator_routing_field(summary_text, "goals") or "").strip()
            goals = [item.strip() for item in goals_field.split("|") if item.strip()]
            if not goals:
                return ""
            return cls.canonicalize_history_decision(
                OrchestratorDecision(
                    complexity_score=normalized_score,
                    respond_now=False,
                    spawn_children=[OrchestratorChildRequest(goal=goals[0])],
                    cancel_children=cancel_children,
                ),
                history_source=history_source or "orchestrator",
            )

        if action == "ignore":
            return cls.canonicalize_history_decision(
                OrchestratorDecision(
                    complexity_score=normalized_score,
                    respond_now=False,
                    cancel_children=cancel_children,
                    ignore_message=True,
                ),
                history_source=history_source or "orchestrator",
            )

        return ""

    @classmethod
    def sanitize_immediate_reply(cls, text: str, *, max_len: int = 160) -> str:
        cleaned = cls.trim_status_text(text, max_len=max_len)
        if not cleaned:
            return ""
        if not is_orchestrator_routing_summary(cleaned):
            return cleaned
        recovered = cls.trim_status_text(
            extract_orchestrator_reply_summary(cleaned),
            max_len=max_len,
        )
        return recovered

    @staticmethod
    def _history_source_from_message(msg: Dict[str, Any]) -> str:
        internal_source = str((msg or {}).get("internal_source") or "").strip()
        if internal_source == "child_agent":
            return "full_agent"
        if internal_source == "orchestrator":
            return "orchestrator"
        return ""

    @staticmethod
    def _history_metadata_from_json(raw_text: str) -> Dict[str, str]:
        text = str(raw_text or "").strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        metadata: Dict[str, str] = {}
        for key in ("history_source", "reply_kind", "child_id"):
            value = str(payload.get(key) or "").strip()
            if value:
                metadata[key] = value
        return metadata

    @staticmethod
    def _extract_embedded_json_objects(raw_text: str) -> List[Dict[str, Any]]:
        text = str(raw_text or "")
        if not text:
            return []
        decoder = json.JSONDecoder()
        idx = 0
        objects: List[Dict[str, Any]] = []
        while idx < len(text):
            start = text.find("{", idx)
            if start < 0:
                break
            try:
                parsed, consumed = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                idx = start + 1
                continue
            if isinstance(parsed, dict):
                objects.append(parsed)
            idx = start + consumed
        return objects

    @classmethod
    def _infer_embedded_history_source(
        cls,
        decision: OrchestratorDecision,
        *,
        explicit_source: str = "",
        saw_orchestrator_action: bool = False,
    ) -> str:
        cleaned_explicit = str(explicit_source or "").strip()
        if cleaned_explicit:
            return cleaned_explicit
        if decision.spawn_children or decision.ignore_message:
            return "orchestrator"
        if saw_orchestrator_action:
            return "full_agent"
        return ""

    @classmethod
    def _canonicalize_embedded_decision_entries(
        cls,
        raw_text: str,
        *,
        history_source: str = "",
        reply_kind: str = "",
        child_id: str = "",
    ) -> List[str]:
        entries: List[str] = []
        saw_orchestrator_action = False
        for obj in cls._extract_embedded_json_objects(raw_text):
            raw_obj = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            try:
                decision = parse_orchestrator_decision(raw_obj)
            except Exception:
                continue
            obj_meta = cls._history_metadata_from_json(raw_obj)
            inferred_source = cls._infer_embedded_history_source(
                decision,
                explicit_source=obj_meta.get("history_source") or history_source,
                saw_orchestrator_action=saw_orchestrator_action,
            )
            entry = cls.canonicalize_history_decision(
                decision,
                history_source=inferred_source,
                reply_kind=obj_meta.get("reply_kind") or reply_kind,
                child_id=obj_meta.get("child_id") or child_id,
            )
            if entry:
                entries.append(entry)
            if decision.spawn_children or decision.ignore_message:
                saw_orchestrator_action = True
            elif decision.respond_now and inferred_source == "orchestrator":
                saw_orchestrator_action = True
        if len(entries) <= 1:
            return []
        return entries

    @classmethod
    def format_child_status(cls, child: GatewayChildState) -> str:
        goal = cls.trim_status_text(child.goal, max_len=90)
        latest = cls.trim_status_text(
            child.last_progress_message or child.summary or child.error,
            max_len=160,
        )
        if str(child.status or "").strip().lower() == "queued":
            parts = [f"Queued next: {goal}."]
            parts.append("Waiting for the current background task to finish.")
            if latest:
                parts.append(f"Latest progress: {latest}.")
            return " ".join(parts)
        parts = [f"Still working on: {goal}."]
        if child.current_step:
            parts.append(f"Current step: {child.current_step}.")
        if child.current_tool:
            parts.append(f"Current tool: {child.current_tool}.")
        if latest:
            parts.append(f"Latest progress: {latest}.")
        elif child.started_at:
            parts.append(f"Started at {child.started_at}.")
        return " ".join(parts)

    @classmethod
    def format_recent_child_status(cls, child: GatewayChildState) -> str:
        goal = cls.trim_status_text(child.goal, max_len=90)
        summary = cls.trim_status_text(
            child.summary or child.actual_user_reply_sent or child.error,
            max_len=160,
        )
        status = child.status or "finished"
        if summary:
            return f"No background task is running right now. The last task ({goal}) {status}: {summary}"
        return f"No background task is running right now. The last task ({goal}) {status}."

    def running_agent_snapshot(self, session_key: str) -> Optional[Dict[str, Any]]:
        running_agent = self._get_running_agent(session_key)
        if not running_agent or running_agent is self._agent_pending_sentinel:
            return None

        try:
            summary = running_agent.get_activity_summary()
        except Exception:
            logger.debug("Could not read running agent activity summary for orchestrator", exc_info=True)
            return {"status": "running"}

        api_call_count = 0
        max_iterations = 0
        try:
            api_call_count = int(summary.get("api_call_count", 0) or 0)
        except (TypeError, ValueError):
            api_call_count = 0
        try:
            max_iterations = int(summary.get("max_iterations", 0) or 0)
        except (TypeError, ValueError):
            max_iterations = 0

        current_tool = str(summary.get("current_tool") or "").strip()
        current_activity = str(summary.get("last_activity_desc") or "").strip()
        current_step = ""
        if max_iterations > 0:
            current_step = f"iteration {api_call_count}/{max_iterations}"
        elif api_call_count > 0:
            current_step = f"iteration {api_call_count}"

        elapsed_minutes = 0
        started_at = self._get_running_agent_started_at(session_key)
        if started_at:
            try:
                elapsed_minutes = max(0, int((time.time() - float(started_at)) / 60))
            except Exception:
                elapsed_minutes = 0

        return {
            "status": "running",
            "current_step": current_step,
            "current_tool": current_tool,
            "current_activity": current_activity or current_tool,
            "api_call_count": api_call_count,
            "max_iterations": max_iterations,
            "elapsed_minutes": elapsed_minutes,
        }

    @classmethod
    def format_running_agent_status(cls, snapshot: Optional[Dict[str, Any]]) -> str:
        if not snapshot:
            return ""
        parts = ["Still working on the current request."]
        current_step = cls.trim_status_text(snapshot.get("current_step") or "", max_len=80)
        current_tool = cls.trim_status_text(snapshot.get("current_tool") or "", max_len=60)
        current_activity = cls.trim_status_text(
            snapshot.get("current_activity") or "",
            max_len=160,
        )
        elapsed_minutes = 0
        try:
            elapsed_minutes = int(snapshot.get("elapsed_minutes", 0) or 0)
        except (TypeError, ValueError):
            elapsed_minutes = 0

        if current_step:
            parts.append(f"Current step: {current_step}.")
        if current_tool:
            parts.append(f"Current tool: {current_tool}.")
        if current_activity:
            parts.append(f"Latest progress: {current_activity}.")
        if elapsed_minutes > 0:
            parts.append(f"Elapsed: {elapsed_minutes} min.")
        return " ".join(parts)

    @staticmethod
    def is_progress_query(message: str) -> bool:
        lowered = str(message or "").strip().lower()
        if not lowered:
            return False
        markers = (
            "progress",
            "status",
            "update",
            "what are you doing",
            "what did you do",
            "still running",
            "stuck",
            "running now",
            "跑到哪",
            "进度",
            "卡住",
            "在干嘛",
            "做到哪",
            "刚才做了什么",
            "还在跑",
            "有结果吗",
            "还没好",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def is_active_child_status_checkin(message: str) -> bool:
        lowered = re.sub(r"\s+", "", str(message or "").strip().lower())
        if not lowered:
            return False
        exact_markers = {
            "你在吗",
            "你在嘛",
            "还在吗",
            "还在嘛",
            "在吗",
            "在嘛",
            "有人吗",
        }
        if lowered in exact_markers:
            return True
        substring_markers = (
            "有结果吗",
            "有结果没",
            "跑到哪",
            "做到哪",
            "在干嘛",
            "还在跑",
            "还没好",
        )
        return any(marker in lowered for marker in substring_markers)

    @staticmethod
    def simple_reply(message: str) -> str:
        lowered = str(message or "").strip().lower()
        if not lowered:
            return ""
        if lowered in {
            "hi",
            "hello",
            "hey",
            "yo",
            "ping",
            "在吗",
            "在嘛",
            "你在吗",
            "你在嘛",
            "还在吗",
            "还在嘛",
            "有人吗",
        }:
            return "在，我在。"
        return ""

    @staticmethod
    def is_user_correction(message: str) -> bool:
        raw = str(message or "").strip()
        if not raw:
            return False

        lowered = raw.lower()
        compact = re.sub(r"\s+", "", raw.lower())

        english_markers = (
            "to correct you",
            "correction:",
            "you got that wrong",
            "you got it wrong",
            "you were wrong",
            "that's wrong",
            "that is wrong",
            "that's incorrect",
            "that is incorrect",
            "you misunderstood",
            "you misread",
            "i meant",
            "not what i said",
            "don't say that again",
            "don't do that again",
            "next time don't",
        )
        chinese_markers = (
            "纠正一下",
            "更正一下",
            "更正",
            "纠正你",
            "你说错了",
            "你说错",
            "你搞错了",
            "你搞错",
            "你记错了",
            "你记错",
            "你记反了",
            "你记反",
            "你理解错了",
            "你理解错",
            "不是这样",
            "不是这个",
            "不是那个",
            "不是这回事",
            "不是我说的",
            "不要再这么说",
            "别再这么说",
            "以后别再",
            "下次别再",
        )
        correction_patterns = (
            r"^不是[，,：: ]",
            r"^不对[，,：: ]",
            r"^纠正[，,：: ]",
            r"^更正[，,：: ]",
            r"不是.{0,80}是",
            r"是.{0,80}不是",
            r"\bit'?s\s+.+?\s+not\s+.+",
            r"\bi(?:'m| am)\s+.+?\s+not\s+.+",
            r"\bnot\s+.+?\bbut\s+.+",
        )

        if any(marker in lowered for marker in english_markers):
            return True
        if any(marker in compact for marker in chinese_markers):
            return True
        return any(re.search(pattern, raw, flags=re.IGNORECASE) for pattern in correction_patterns)

    @staticmethod
    def is_explicit_memory_request(message: str) -> bool:
        raw = str(message or "").strip()
        if not raw:
            return False

        lowered = raw.lower()
        compact = re.sub(r"\s+", "", raw.lower())

        english_markers = (
            "save this",
            "save that",
            "keep this in mind",
            "keep that in mind",
            "don't forget this",
            "don't forget that",
            "write this down",
            "write that down",
            "commit this to memory",
            "put this in memory",
            "store this",
            "store that",
        )
        chinese_markers = (
            "记住这个",
            "记住那个",
            "那你记住",
            "把这个记下来",
            "把那个记下来",
            "记下来",
            "别忘了这个",
            "别忘了那个",
            "记一下",
            "存一下",
            "保存这个",
            "保存那个",
            "记住啊",
            "记住咯",
            "你记住",
        )
        memory_patterns = (
            r"^记住[，,：: ]",
            r"^记下[，,：: ]",
            r"^存[一下]",
            r"^保存[，,：: ]",
            r"^remember[，,：: ]",
            r"^remember\s+(this|that|it)\b",
            r"^note\s+(this|that|it)\b",
            r"^save\s+(this|that|it)\s+(to\s+)?memory",
            r"^keep\s+(this|that|it)\s+in\s+mind",
            r"^don't\s+forget\s+(this|that|it)",
            r"^write\s+(this|that|it)\s+down",
        )

        if any(marker in lowered for marker in english_markers):
            return True
        if any(marker in compact for marker in chinese_markers):
            return True
        return any(re.search(pattern, raw, flags=re.IGNORECASE) for pattern in memory_patterns)

    @staticmethod
    def requires_external_info_handoff(message: str) -> bool:
        lowered = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not lowered:
            return False

        weather_markers = (
            "weather",
            "forecast",
            "temperature",
            "rain",
            "snow",
            "humidity",
            "天气",
            "气温",
            "温度",
            "降雨",
            "下雨",
            "预报",
            "湿度",
        )
        if any(marker in lowered for marker in weather_markers):
            return True

        time_markers = (
            "latest",
            "recent",
            "current",
            "today",
            "tonight",
            "tomorrow",
            "now",
            "live",
            "实时",
            "最新",
            "最近",
            "当前",
            "现在",
            "今天",
            "今日",
            "明天",
            "昨晚",
            "昨天",
            "刚刚",
        )
        search_markers = (
            "look up",
            "search",
            "browse",
            "google",
            "查一下",
            "查查",
            "搜一下",
            "搜搜",
            "搜索",
            "上网查",
            "联网查",
            "网上查",
        )
        live_info_markers = (
            "news",
            "headline",
            "price",
            "pricing",
            "stock",
            "stocks",
            "bitcoin",
            "btc",
            "ethereum",
            "eth",
            "crypto",
            "score",
            "scores",
            "schedule",
            "standings",
            "flight",
            "traffic",
            "exchange rate",
            "rate",
            "新闻",
            "头条",
            "热搜",
            "价格",
            "股价",
            "币价",
            "汇率",
            "油价",
            "比分",
            "赛程",
            "战绩",
            "排名",
            "航班",
            "路况",
        )
        return any(marker in lowered for marker in live_info_markers) and (
            any(marker in lowered for marker in time_markers)
            or any(marker in lowered for marker in search_markers)
        )

    @staticmethod
    def requires_device_state_handoff(message: str) -> bool:
        raw = str(message or "").strip()
        lowered = re.sub(r"\s+", " ", raw.lower())
        compact = re.sub(r"\s+", "", raw.lower())
        if not lowered:
            return False
        if "状态机" in compact or "state machine" in lowered:
            return False

        explicit_phrases = (
            "设备状态",
            "所有设备",
            "全部设备",
            "家里的设备",
            "我家的设备",
            "家庭设备",
            "device status",
            "all devices",
            "home devices",
            "sensor readings",
            "smart home status",
        )
        if any(phrase in compact for phrase in explicit_phrases if re.search(r"[\u4e00-\u9fff]", phrase)):
            return True
        if any(phrase in lowered for phrase in explicit_phrases if not re.search(r"[\u4e00-\u9fff]", phrase)):
            return True

        home_context_markers = (
            "我家",
            "家里",
            "家里的",
            "家庭",
            "全屋",
            "客厅",
            "卧室",
            "书房",
            "米家",
            "小米",
            "智能家居",
            "smart home",
            "home device",
            "household",
        )
        generic_device_markers = (
            "设备",
            "device",
            "devices",
            "传感器",
            "sensor",
            "sensors",
        )
        specific_device_markers = (
            "台灯",
            "灯",
            "空调",
            "除湿机",
            "加湿器",
            "插座",
            "门锁",
            "窗帘",
            "扫地机",
            "扫地机器人",
            "喂食器",
            "空气检测仪",
            "温湿度计",
            "摄像头",
            "热水器",
            "lamp",
            "light",
            "air conditioner",
            "ac",
            "dehumidifier",
            "humidifier",
            "plug",
            "camera",
            "feeder",
        )
        state_query_markers = (
            "状态",
            "细节",
            "详情",
            "信息",
            "读数",
            "温度",
            "湿度",
            "亮度",
            "色温",
            "功率",
            "电量",
            "目标",
            "开着",
            "关着",
            "亮着",
            "运行",
            "在线",
            "离线",
            "开没开",
            "关没关",
            "status",
            "detail",
            "details",
            "reading",
            "readings",
            "temperature",
            "humidity",
            "brightness",
            "power",
            "battery",
            "target",
            "running",
            "online",
            "offline",
        )
        query_markers = (
            "看一下",
            "看下",
            "看看",
            "查一下",
            "查下",
            "查查",
            "帮我看",
            "帮我查",
            "告诉我",
            "列一下",
            "列出",
            "汇总",
            "什么",
            "多少",
            "吗",
            "？",
            "?",
            "show me",
            "check",
            "look at",
            "tell me",
            "list",
            "summarize",
            "what",
            "whether",
        )

        has_home_context = any(marker in lowered for marker in home_context_markers)
        has_generic_device = any(marker in lowered for marker in generic_device_markers)
        has_specific_device = any(marker in lowered for marker in specific_device_markers)
        has_state_query = any(marker in lowered for marker in state_query_markers)
        has_query_intent = has_state_query or any(marker in lowered for marker in query_markers)

        if has_specific_device and has_state_query:
            return True
        if has_home_context and has_generic_device and has_query_intent:
            return True
        return has_generic_device and has_state_query and has_home_context

    @staticmethod
    def is_group_ignore_candidate(message: str) -> bool:
        lowered = str(message or "").strip().lower()
        if not lowered:
            return True
        if len(lowered) > 24:
            return False
        low_value = {
            "ok",
            "okay",
            "kk",
            "收到",
            "知道了",
            "好的",
            "好",
            "嗯",
            "哈哈",
            "lol",
            "666",
            "…",
            ".",
            "?",
            "？",
        }
        return lowered in low_value

    @staticmethod
    def group_has_recent_discussion_line(recent_group_context: str) -> bool:
        return bool(
            [
                line.strip()
                for line in str(recent_group_context or "").splitlines()
                if line.strip()
            ]
        )

    @staticmethod
    def is_group_banter_candidate(message: str) -> bool:
        raw = re.sub(r"\s+", " ", str(message or "").strip())
        if not raw:
            return False

        lowered = raw.lower()
        compact = re.sub(r"\s+", "", lowered)
        if len(raw) > 32 or len(compact) > 24:
            return False
        if re.search(r"https?://|www\.|[`/\\\\]|\.py\b|\.js\b|\.ts\b|\.go\b", lowered):
            return False

        blocking_markers = (
            "repo",
            "repository",
            "codebase",
            "project",
            "bug",
            "fix",
            "implement",
            "analysis",
            "analyze",
            "debug",
            "weather",
            "latest",
            "price",
            "score",
            "schedule",
            "仓库",
            "项目",
            "代码",
            "实现",
            "分析",
            "排查",
            "调试",
            "天气",
            "最新",
            "价格",
            "比分",
            "赛程",
        )
        if any(marker in lowered for marker in blocking_markers):
            return False

        chinese_markers = (
            "笑死",
            "绷不住",
            "蚌埠住",
            "逆天",
            "离谱",
            "绝了",
            "抽象",
            "节目效果",
            "有活",
            "好家伙",
            "这也行",
            "这下",
            "麻了",
            "坏了",
            "乐",
            "草",
            "幽默",
            "典中典",
        )
        english_markers = (
            "lmao",
            "lmfao",
            "bruh",
            "wild",
            "crazy",
            "no way",
            "damn",
        )
        return any(marker in compact for marker in chinese_markers) or any(
            marker in lowered for marker in english_markers
        )

    @staticmethod
    def is_group_trigger_explicit(trigger_reason: str) -> bool:
        return str(trigger_reason or "").strip().lower() in {
            "mention",
            "reply_to_bot",
            "keyword",
            "regex",
            "poke",
            "dm",
        }

    @staticmethod
    def is_group_direct_agent_reference(message: str) -> bool:
        compact = re.sub(r"\s+", "", str(message or "").strip().lower())
        if not compact:
            return False
        direct_markers = (
            "hermes",
            "hermesagent",
            "agent",
            "bot",
            "assistant",
            "机器人",
        )
        return any(marker in compact for marker in direct_markers)

    @staticmethod
    def is_group_followup_marker(message: str) -> bool:
        lowered = str(message or "").strip().lower()
        compact = re.sub(r"\s+", "", lowered)
        if not compact:
            return False
        compact_markers = (
            "你刚才",
            "刚才你",
            "你上面",
            "上面你",
            "你之前",
            "前面你",
            "你说的",
            "按你说的",
            "接着说",
            "接着讲",
            "继续说",
            "继续讲",
            "继续展开",
            "展开说",
            "细说",
            "详细说",
            "详细一点",
            "具体一点",
            "解释一下",
            "那你",
            "然后呢",
            "所以呢",
        )
        phrase_markers = (
            "as you said",
            "you said",
            "your last answer",
            "your previous answer",
            "continue that",
            "continue with that",
            "expand on that",
            "follow up on that",
        )
        return any(marker in compact for marker in compact_markers) or any(
            marker in lowered for marker in phrase_markers
        )

    @staticmethod
    def is_group_isolated_direct_address(message: str) -> bool:
        raw = re.sub(r"\s+", " ", str(message or "").strip())
        if not raw:
            return False

        compact = re.sub(r"\s+", "", raw.lower())
        if len(compact) > 36 or "你" not in raw:
            return False

        patterns = (
            r"^不对[啊呀吧吗]?[，,：:!！。 ]?.*你",
            r"^不是[啊呀吧吗]?[，,：:!！。 ]?.*你",
            r"^你是不是.*",
            r".*我没.*[和跟]你说过.*",
            r".*你记(错|串)了.*",
            r".*你搞错了.*",
            r".*你理解错了.*",
        )
        return any(re.search(pattern, raw, flags=re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def group_memory_declares_agent_trigger(
        user_message: str,
        memory_context: str,
    ) -> bool:
        msg = re.sub(r"\s+", " ", str(user_message or "").strip()).lower()
        if not msg or len(msg) > 48:
            return False

        memory = str(memory_context or "").lower()
        if not memory or msg not in memory:
            return False

        directive_markers = (
            "固定指令",
            "固定口令",
            "关键词",
            "关键字",
            "口令",
            "触发词",
            "群规则",
            "group rule",
            "fixed instruction",
            "fixed trigger",
            "trigger phrase",
            "trigger word",
            "keyword",
        )
        action_markers = (
            "自动调用",
            "自动触发",
            "自动执行",
            "自动回复",
            "单独说",
            "said alone",
            "says alone",
            "when anyone says",
            "when someone says",
            "if anyone says",
            "if someone says",
            "trigger",
            "triggers",
        )

        for match in re.finditer(re.escape(msg), memory):
            start = max(0, match.start() - 160)
            end = min(len(memory), match.end() + 160)
            window = memory[start:end]
            if any(marker in window for marker in directive_markers) and any(
                marker in window for marker in action_markers
            ):
                return True
        return False

    @classmethod
    def has_recent_assistant_context(
        cls,
        history: List[Dict[str, Any]],
        *,
        max_messages: int = 8,
        max_user_gap: int = 1,
    ) -> bool:
        inspected = 0
        user_gap = 0
        for msg in reversed(history or []):
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip()
            if role in {"system", "session_meta", "tool"}:
                continue
            inspected += 1
            if max_messages > 0 and inspected > max_messages:
                break
            if role == "assistant":
                if str(msg.get("content") or "").strip():
                    return user_gap <= max_user_gap
                continue
            if role == "user":
                user_gap += 1
                if user_gap > max_user_gap:
                    return False
        return False

    def group_message_clearly_agent_related(
        self,
        *,
        user_message: str,
        source: SessionSource,
        session: GatewayOrchestratorSession,
        history: List[Dict[str, Any]],
        event_metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if source.chat_type == "dm":
            return True

        metadata = dict(event_metadata or {})
        trigger_reason = str(metadata.get(TRIGGER_REASON_KEY) or "").strip().lower()
        if self.is_group_trigger_explicit(trigger_reason):
            return True

        msg = str(user_message or "").strip()
        if not msg:
            return False

        if self.is_group_direct_agent_reference(msg):
            return True

        if (
            not self.group_has_recent_discussion_line(metadata.get(RECENT_GROUP_CONTEXT_KEY) or "")
            and self.is_group_isolated_direct_address(msg)
        ):
            return True

        active_child = self.relevant_active_child(session, msg)
        if active_child is not None and (
            self.is_progress_query(msg)
            or self.is_active_child_status_checkin(msg)
            or session.find_duplicate_child(normalize_dedupe_key(msg)) is not None
        ):
            return True

        if self.has_recent_assistant_context(history) and self.is_group_followup_marker(msg):
            return True

        recent_group_context = str(metadata.get(RECENT_GROUP_CONTEXT_KEY) or "").strip()
        if recent_group_context and self.is_group_followup_marker(msg):
            return True

        memory_context = str(metadata.get(PREFETCHED_MEMORY_CONTEXT_KEY) or "").strip()
        if memory_context and self.group_memory_declares_agent_trigger(msg, memory_context):
            return True

        return False

    def group_banter_reply(
        self,
        *,
        user_message: str,
        source: SessionSource,
        session: GatewayOrchestratorSession,
        history: List[Dict[str, Any]],
        event_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        if source.chat_type == "dm":
            return ""

        msg = str(user_message or "").strip()
        if not msg or self.is_group_ignore_candidate(msg):
            return ""

        metadata = dict(event_metadata or {})
        trigger_reason = str(metadata.get(TRIGGER_REASON_KEY) or "").strip().lower()
        if self.is_group_trigger_explicit(trigger_reason):
            return ""
        if self.is_group_direct_agent_reference(msg):
            return ""
        if self.is_group_followup_marker(msg):
            return ""
        if self.has_recent_assistant_context(history):
            return ""
        if self.group_has_recent_discussion_line(metadata.get(RECENT_GROUP_CONTEXT_KEY) or ""):
            return ""
        if self.relevant_active_child(session, msg) is not None:
            return ""
        if (
            self.is_user_correction(msg)
            or self.is_explicit_memory_request(msg)
            or self.requires_external_info_handoff(msg)
            or self.requires_device_state_handoff(msg)
        ):
            return ""
        if not self.is_group_banter_candidate(msg):
            return ""

        compact = re.sub(r"\s+", "", msg.lower())
        if "笑死" in compact or "lmao" in compact or "lmfao" in compact:
            return "这波确实有点好笑。"
        if "绷不住" in compact or "蚌埠住" in compact:
            return "这下我也有点绷不住了。"
        if "逆天" in compact:
            return "这下确实有点逆天。"
        if "离谱" in compact:
            return "确实，多少有点离谱。"
        if "好家伙" in compact or "这也行" in compact or "noway" in compact.replace(" ", ""):
            return "好家伙，这也行。"
        if "抽象" in compact or "节目效果" in compact or "有活" in compact:
            return "这波节目效果是有了。"
        return "这波确实有点东西。"

    def relevant_active_child(
        self,
        session: GatewayOrchestratorSession,
        user_message: str,
    ) -> Optional[GatewayChildState]:
        dedupe_key = normalize_dedupe_key(user_message)
        duplicate = session.find_duplicate_child(dedupe_key)
        if duplicate is not None:
            return duplicate
        running_children = session.running_children()
        if running_children:
            running_children.sort(
                key=lambda child: (
                    child.progress_seq,
                    child.last_activity_ts or child.started_at or "",
                ),
                reverse=True,
            )
            return running_children[0]
        queued_children = session.queued_children()
        if not queued_children:
            return None
        queued_children.sort(
            key=lambda child: (
                child.progress_seq,
                child.last_activity_ts or child.started_at or "",
            ),
            reverse=True,
        )
        return queued_children[0]

    def progress_reply(
        self,
        session: GatewayOrchestratorSession,
        user_message: str,
    ) -> str:
        child = self.relevant_active_child(session, user_message)
        if child is not None:
            return self.format_child_status(child)
        running_snapshot = self.running_agent_snapshot(session.session_key)
        if running_snapshot is not None:
            return self.format_running_agent_status(running_snapshot)
        if session.recent_children:
            return self.format_recent_child_status(session.recent_children[0])
        return "There is no active background task right now."

    def build_turn_signals(
        self,
        *,
        user_message: str,
        source: SessionSource,
        event_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        del user_message
        metadata = dict(event_metadata or {})
        return {
            "trigger_reason": str(metadata.get(TRIGGER_REASON_KEY) or ("dm" if source.chat_type == "dm" else "")),
            "recent_group_context": str(metadata.get(RECENT_GROUP_CONTEXT_KEY) or "").strip(),
        }

    @staticmethod
    def merge_memory_context_into_prompt(
        base_prompt: str,
        memory_context_block: str,
        *,
        note: str = (
            "[Context note: Prefetched memory context for full-agent handoff only. "
            "The following block is not user-authored.]"
        ),
    ) -> str:
        prompt = str(base_prompt or "").strip()
        memory_block = str(memory_context_block or "").strip()
        if not memory_block:
            return prompt
        extra = f"{note}\n{memory_block}".strip()
        return "\n\n".join(part for part in (prompt, extra) if part).strip()

    def build_memory_context_block(
        self,
        *,
        user_message: str,
        source: SessionSource,
        session_key: str,
        session_id: str = "",
        turn_number: int = 1,
    ) -> str:
        snapshot = self.build_memory_context_snapshot(
            user_message=user_message,
            source=source,
            session_key=session_key,
            session_id=session_id,
            turn_number=turn_number,
        )
        return str(snapshot.get("combined_block") or "").strip()

    def build_router_memory_context_snapshot(
        self,
        *,
        user_message: str,
        source: SessionSource,
        session_key: str,
        session_id: str = "",
        turn_number: int = 1,
    ) -> Dict[str, Any]:
        builtin_blocks = self._build_builtin_memory_context_blocks(source=source)
        builtin_group_block = builtin_blocks.get("group_block", "")
        builtin_user_block = builtin_blocks.get("user_block", "")
        provider_snapshot = self._get_cached_provider_memory_context_snapshot(
            source=source,
            session_key=session_key,
            session_id=session_id,
            query=str(user_message or "").strip(),
            max_age_seconds=_ORCHESTRATOR_ROUTER_MEMORY_TTL_SECONDS,
        )
        if (
            not str(provider_snapshot.get("provider_block") or "").strip()
            and self._configured_memory_provider_name()
            and str(user_message or "").strip()
        ):
            provider_snapshot = self._build_provider_memory_context_snapshot(
                query=str(user_message or "").strip(),
                source=source,
                session_key=session_key,
                session_id=session_id,
                turn_number=turn_number,
            )
        combined = self._compose_memory_context_snapshot(
            builtin_block=builtin_group_block + ("\n\n" + builtin_user_block if builtin_user_block else ""),
            provider_snapshot=provider_snapshot,
        )
        group_combined = self._compose_memory_context_snapshot(
            builtin_block=builtin_group_block,
            provider_snapshot=provider_snapshot,
        )
        user_combined = self._compose_memory_context_snapshot(
            builtin_block=builtin_user_block,
            provider_snapshot=None,
        )
        combined["group_block"] = group_combined["combined_block"]
        combined["user_block"] = user_combined["combined_block"]
        combined["group_builtin_block"] = builtin_group_block
        combined["user_builtin_block"] = builtin_user_block
        combined["provider_prefetch_cache_hit"] = bool(provider_snapshot.get("provider_prefetch_cache_hit"))
        combined["provider_runtime_reused"] = bool(provider_snapshot.get("provider_runtime_reused"))
        combined["provider_context_reused"] = bool(provider_snapshot.get("provider_context_reused"))
        return combined

    def build_provider_memory_context_snapshot_if_needed(
        self,
        *,
        user_message: str,
        source: SessionSource,
        session_key: str,
        session_id: str = "",
        turn_number: int = 1,
    ) -> Dict[str, Any]:
        query = str(user_message or "").strip()
        if not query:
            return self._empty_provider_memory_snapshot(
                configured_provider=self._configured_memory_provider_name()
            )
        return self._build_provider_memory_context_snapshot(
            query=query,
            source=source,
            session_key=session_key,
            session_id=session_id,
            turn_number=turn_number,
        )

    def build_memory_context_snapshot(
        self,
        *,
        user_message: str,
        source: SessionSource,
        session_key: str,
        session_id: str = "",
        turn_number: int = 1,
    ) -> Dict[str, Any]:
        builtin_block = self._build_builtin_memory_context_block(source=source)
        provider_snapshot = self.build_provider_memory_context_snapshot_if_needed(
            user_message=user_message,
            source=source,
            session_key=session_key,
            session_id=session_id,
            turn_number=turn_number,
        )
        return self._compose_memory_context_snapshot(
            builtin_block=builtin_block,
            provider_snapshot=provider_snapshot,
        )

    def _compose_memory_context_snapshot(
        self,
        *,
        builtin_block: str,
        provider_snapshot: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        provider_snapshot = dict(provider_snapshot or {})
        provider_block = str(provider_snapshot.get("provider_block") or "").strip()
        blocks = [
            block
            for block in (
                str(builtin_block or "").strip(),
                provider_block,
            )
            if block
        ]
        provider_details = provider_snapshot.get("provider_details")
        if not isinstance(provider_details, list):
            provider_details = []

        provider_names: list[str] = []
        provider_blocks: list[str] = []
        provider_param_blocks: list[str] = []
        for detail in provider_details:
            if not isinstance(detail, dict):
                continue
            provider_name = str(detail.get("provider") or "").strip() or "unknown"
            provider_content = str(detail.get("content") or "").strip()
            provider_debug = detail.get("prefetch_debug")
            if provider_name and provider_name not in provider_names:
                provider_names.append(provider_name)
            if provider_content:
                provider_blocks.append(f"## {provider_name}\n{provider_content}")
            if isinstance(provider_debug, dict) and provider_debug:
                try:
                    debug_text = json.dumps(
                        provider_debug,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                        default=str,
                    )
                except Exception:
                    debug_text = str(provider_debug)
                provider_param_blocks.append(f"## {provider_name}\n{debug_text}")

        return {
            "builtin_block": str(builtin_block or "").strip(),
            "provider_block": provider_block,
            "combined_block": "\n\n".join(blocks).strip(),
            "provider_details": provider_details,
            "provider_names": provider_names,
            "provider_count": len(provider_names),
            "memory_prefetch_by_provider": "\n\n".join(provider_blocks),
            "memory_prefetch_content": str(provider_snapshot.get("provider_merged_content") or "").strip(),
            "memory_prefetch_fenced": provider_block,
            "memory_prefetch_params_by_provider": "\n\n".join(provider_param_blocks),
            "configured_provider": str(provider_snapshot.get("configured_provider") or "").strip(),
            "provider_available": provider_snapshot.get("provider_available"),
            "prefetch_attempted": bool(provider_snapshot.get("prefetch_attempted")),
            "provider_error": str(provider_snapshot.get("provider_error") or "").strip(),
            "provider_prefetch_cache_hit": bool(provider_snapshot.get("provider_prefetch_cache_hit")),
            "provider_runtime_reused": bool(provider_snapshot.get("provider_runtime_reused")),
            "provider_context_reused": bool(provider_snapshot.get("provider_context_reused")),
        }

    def _build_builtin_memory_context_block(
        self,
        *,
        source: SessionSource,
    ) -> str:
        memory_cfg = self._memory_config()
        memory_enabled = bool(self._config_get(memory_cfg, "memory_enabled", False))
        user_profile_enabled = bool(self._config_get(memory_cfg, "user_profile_enabled", False))
        if not memory_enabled and not user_profile_enabled:
            return ""

        try:
            from tools.memory_tool import MemoryStore, get_memory_dir
        except Exception:
            logger.debug("Could not import built-in memory store for orchestrator", exc_info=True)
            return ""

        memory_char_limit = int(self._config_get(memory_cfg, "memory_char_limit", 2200) or 2200)
        user_char_limit = int(self._config_get(memory_cfg, "user_char_limit", 1375) or 1375)
        chat_char_limit = int(
            self._config_get(memory_cfg, "chat_char_limit", memory_char_limit) or 2200
        )
        platform_name = getattr(getattr(source, "platform", None), "value", "") if source else ""
        user_id = getattr(source, "user_id", None) if source else None
        user_name = getattr(source, "user_name", None) if source else None
        chat_id = getattr(source, "chat_id", None) if source else None
        chat_type = getattr(source, "chat_type", None) if source else None
        thread_id = getattr(source, "thread_id", None) if source else None
        cache_key = json.dumps(
            {
                "memory_dir": str(get_memory_dir()),
                "memory_enabled": memory_enabled,
                "user_profile_enabled": user_profile_enabled,
                "memory_char_limit": memory_char_limit,
                "user_char_limit": user_char_limit,
                "chat_char_limit": chat_char_limit,
                "platform": platform_name,
                "user_id": user_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "thread_id": thread_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        memory_dir = get_memory_dir()
        legacy_user_path = memory_dir / "USER.md"
        scoped_user_path = (
            memory_dir / "users" / str(platform_name or "") / f"{user_id}.md"
            if platform_name and user_id
            else None
        )
        scoped_chat_filename = str(chat_id or "")
        if scoped_chat_filename and thread_id:
            scoped_chat_filename = f"{scoped_chat_filename}__{thread_id}"
        scoped_chat_path = (
            memory_dir
            / "chats"
            / str(platform_name or "")
            / str(chat_type or "group")
            / f"{scoped_chat_filename}.md"
            if platform_name and chat_id and str(chat_type or "").lower() != "dm"
            else None
        )
        file_state = tuple(
            state
            for state in (
                self._safe_file_state(memory_dir / "MEMORY.md"),
                self._safe_file_state(legacy_user_path),
                self._safe_file_state(scoped_user_path),
                self._safe_file_state(scoped_chat_path),
            )
            if state[0]
        )
        with self._memory_cache_lock:
            cached = self._builtin_memory_cache.get(cache_key)
            if cached is not None and cached.file_state == file_state:
                return cached.block

        try:
            store = MemoryStore(
                memory_char_limit=memory_char_limit,
                user_char_limit=user_char_limit,
                chat_char_limit=chat_char_limit,
                platform=platform_name,
                user_id=user_id,
                user_name=user_name,
                chat_id=chat_id,
                chat_type=chat_type,
                thread_id=thread_id,
            )
            store.load_from_disk()
        except Exception:
            logger.debug("Could not load built-in memory store for orchestrator", exc_info=True)
            return ""

        group_blocks: list[str] = []
        user_blocks: list[str] = []
        if memory_enabled:
            mem_block = store.format_for_system_prompt("memory")
            if mem_block:
                group_blocks.append(mem_block)
            chat_block = store.format_for_system_prompt("chat")
            if chat_block:
                group_blocks.append(chat_block)
        if user_profile_enabled:
            user_block = store.format_for_system_prompt("user")
            if user_block:
                user_blocks.append(user_block)
        rendered = "\n\n".join(block for block in (group_blocks + user_blocks) if block).strip()
        with self._memory_cache_lock:
            self._builtin_memory_cache[cache_key] = _BuiltinMemoryCacheEntry(
                block=rendered,
                group_block="\n\n".join(block for block in group_blocks if block).strip(),
                user_block="\n\n".join(block for block in user_blocks if block).strip(),
                file_state=file_state,
            )
        return rendered

    def _build_builtin_memory_context_blocks(
        self,
        *,
        source: SessionSource,
    ) -> Dict[str, str]:
        """Return separated group-level and user-level builtin memory blocks."""
        memory_cfg = self._memory_config()
        memory_enabled = bool(self._config_get(memory_cfg, "memory_enabled", False))
        user_profile_enabled = bool(self._config_get(memory_cfg, "user_profile_enabled", False))
        if not memory_enabled and not user_profile_enabled:
            return {"group_block": "", "user_block": ""}

        self._build_builtin_memory_context_block(source=source)

        platform_name = getattr(getattr(source, "platform", None), "value", "") if source else ""
        user_id = getattr(source, "user_id", None) if source else None
        chat_id = getattr(source, "chat_id", None) if source else None
        chat_type = getattr(source, "chat_type", None) if source else None
        thread_id = getattr(source, "thread_id", None) if source else None
        memory_char_limit = int(self._config_get(memory_cfg, "memory_char_limit", 2200) or 2200)
        user_char_limit = int(self._config_get(memory_cfg, "user_char_limit", 1375) or 1375)
        chat_char_limit = int(
            self._config_get(memory_cfg, "chat_char_limit", memory_char_limit) or 2200
        )
        from tools.memory_tool import get_memory_dir
        cache_key = json.dumps(
            {
                "memory_dir": str(get_memory_dir()),
                "memory_enabled": memory_enabled,
                "user_profile_enabled": user_profile_enabled,
                "memory_char_limit": memory_char_limit,
                "user_char_limit": user_char_limit,
                "chat_char_limit": chat_char_limit,
                "platform": platform_name,
                "user_id": user_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "thread_id": thread_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        with self._memory_cache_lock:
            cached = self._builtin_memory_cache.get(cache_key)
            if cached is not None:
                return {
                    "group_block": cached.group_block,
                    "user_block": cached.user_block,
                }
        return {"group_block": "", "user_block": ""}

    def _empty_provider_memory_snapshot(
        self,
        *,
        configured_provider: str = "",
        provider_available: bool = False,
        provider_error: str = "",
    ) -> Dict[str, Any]:
        return {
            "configured_provider": str(configured_provider or "").strip(),
            "provider_available": bool(provider_available),
            "prefetch_attempted": False,
            "provider_error": str(provider_error or "").strip(),
            "provider_details": [],
            "provider_merged_content": "",
            "provider_block": "",
            "provider_prefetch_cache_hit": False,
            "provider_runtime_reused": False,
            "provider_context_reused": False,
        }

    def _provider_runtime_cache_key(
        self,
        *,
        session_key: str,
        provider_name: str,
        profile_name: str,
    ) -> str:
        return json.dumps(
            {
                "session_key": str(session_key or "").strip(),
                "provider_name": str(provider_name or "").strip(),
                "profile_name": str(profile_name or "").strip(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _provider_init_kwargs(
        self,
        *,
        source: SessionSource,
        session_key: str,
        session_id: str,
    ) -> Dict[str, Any]:
        from hermes_constants import get_hermes_home

        platform_name = getattr(getattr(source, "platform", None), "value", "") if source else ""
        init_kwargs: Dict[str, Any] = {
            "platform": platform_name,
            "hermes_home": str(get_hermes_home()),
            "agent_context": "primary",
            "chat_id": getattr(source, "chat_id", None) if source else None,
            "chat_type": getattr(source, "chat_type", None) if source else None,
            "thread_id": getattr(source, "thread_id", None) if source else None,
            "gateway_session_key": session_key,
        }
        session_title_getter = getattr(self.host, "get_session_title", None)
        if callable(session_title_getter) and session_id:
            try:
                session_title = session_title_getter(session_id)
                if session_title:
                    init_kwargs["session_title"] = session_title
            except Exception:
                logger.debug("Could not load orchestrator session title for memory prefetch", exc_info=True)
        if source is not None and getattr(source, "user_id", None):
            init_kwargs["user_id"] = getattr(source, "user_id", None)
        profile_name = self._active_profile_name()
        if profile_name:
            init_kwargs["agent_identity"] = profile_name
            init_kwargs["agent_workspace"] = "hermes"
        return init_kwargs

    def _cached_provider_runtime_handle(
        self,
        *,
        session_key: str,
        provider_name: str,
        session_id: str,
        profile_name: str,
    ) -> Optional[_ProviderRuntimeHandle]:
        runtime_cache_key = self._provider_runtime_cache_key(
            session_key=session_key,
            provider_name=provider_name,
            profile_name=profile_name,
        )
        with self._memory_cache_lock:
            handle = self._provider_runtime_cache.get(runtime_cache_key)
            if handle is not None and handle.session_id == session_id:
                return handle
        return None

    def _get_cached_provider_memory_context_snapshot(
        self,
        *,
        source: SessionSource,
        session_key: str,
        session_id: str = "",
        query: str = "",
        max_age_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        del source
        provider_name = self._configured_memory_provider_name()
        snapshot = self._empty_provider_memory_snapshot(configured_provider=provider_name)
        if not provider_name:
            return snapshot
        effective_session_id = (
            str(session_id or "").strip() or self.orchestrator_internal_session_id(session_key)
        )
        profile_name = self._active_profile_name()
        handle = self._cached_provider_runtime_handle(
            session_key=session_key,
            provider_name=provider_name,
            session_id=effective_session_id,
            profile_name=profile_name,
        )
        if handle is None:
            return snapshot
        query_signature = self._message_signature(query)
        if (
            max_age_seconds is not None
            and float(max_age_seconds) > 0
            and handle.last_snapshot_ts > 0
            and (time.time() - float(handle.last_snapshot_ts)) > float(max_age_seconds)
        ):
            snapshot["provider_available"] = True
            snapshot["provider_runtime_reused"] = True
            return snapshot
        cached = copy.deepcopy(handle.last_snapshot or {})
        if not cached:
            snapshot["provider_available"] = True
            snapshot["provider_runtime_reused"] = True
            return snapshot
        if query_signature and handle.last_message_signature != query_signature:
            snapshot["provider_available"] = True
            snapshot["provider_runtime_reused"] = True
            return snapshot
        cached["provider_context_reused"] = bool(str(cached.get("provider_block") or "").strip())
        cached["provider_runtime_reused"] = True
        cached["prefetch_attempted"] = False
        cached["provider_prefetch_cache_hit"] = bool(str(cached.get("provider_block") or "").strip())
        return cached

    def _build_provider_memory_context_snapshot(
        self,
        *,
        query: str,
        source: SessionSource,
        session_key: str,
        session_id: str = "",
        turn_number: int = 1,
    ) -> Dict[str, Any]:
        provider_name = self._configured_memory_provider_name()
        snapshot = self._empty_provider_memory_snapshot(configured_provider=provider_name)
        if not query:
            return snapshot
        if not provider_name:
            return snapshot

        try:
            from agent.memory_manager import MemoryManager, build_memory_context_block
            from plugins.memory import load_memory_provider
        except Exception:
            logger.debug("Could not import memory provider helpers for orchestrator", exc_info=True)
            snapshot["provider_error"] = "import_failed"
            return snapshot

        platform_name = getattr(getattr(source, "platform", None), "value", "") if source else ""
        effective_session_id = str(session_id or "").strip() or self.orchestrator_internal_session_id(session_key)
        profile_name = self._active_profile_name()
        runtime_cache_key = self._provider_runtime_cache_key(
            session_key=session_key,
            provider_name=provider_name,
            profile_name=profile_name,
        )
        init_kwargs = self._provider_init_kwargs(
            source=source,
            session_key=session_key,
            session_id=effective_session_id,
        )

        try:
            with self._memory_cache_lock:
                stale_cache_keys = [
                    cache_key
                    for cache_key, handle in self._provider_runtime_cache.items()
                    if handle.session_key == session_key and cache_key != runtime_cache_key
                ]
            for cache_key in stale_cache_keys:
                stale_handle = None
                with self._memory_cache_lock:
                    stale_handle = self._provider_runtime_cache.pop(cache_key, None)
                if stale_handle is not None and getattr(stale_handle, "initialized", False):
                    try:
                        stale_handle.manager.shutdown_all()
                    except Exception:
                        logger.debug("Could not shut down stale orchestrator memory provider", exc_info=True)

            with self._memory_cache_lock:
                handle = self._provider_runtime_cache.get(runtime_cache_key)
            runtime_reused = handle is not None
            if handle is None or handle.session_id != effective_session_id:
                provider = load_memory_provider(provider_name)
                if not provider:
                    snapshot["provider_error"] = "provider_not_loaded"
                    return snapshot
                snapshot["provider_available"] = bool(provider.is_available())
                if not snapshot["provider_available"]:
                    snapshot["provider_error"] = "provider_unavailable"
                    return snapshot
                manager = MemoryManager()
                manager.add_provider(provider)
                handle = _ProviderRuntimeHandle(
                    cache_key=runtime_cache_key,
                    session_key=session_key,
                    session_id=effective_session_id,
                    provider_name=provider_name,
                    profile_name=profile_name,
                    manager=manager,
                    provider=provider,
                    init_kwargs=dict(init_kwargs),
                )
                with self._memory_cache_lock:
                    self._provider_runtime_cache[runtime_cache_key] = handle
            else:
                snapshot["provider_available"] = True
                handle.init_kwargs = dict(init_kwargs)

            if not handle.initialized:
                handle.manager.initialize_all(session_id=effective_session_id, **init_kwargs)
                handle.initialized = True

            handle.manager.on_turn_start(turn_number, query, platform=platform_name)
            snapshot["provider_available"] = True
            snapshot["provider_runtime_reused"] = runtime_reused
            message_signature = self._message_signature(query)
            cached_snapshot = copy.deepcopy(handle.last_snapshot or {})
            if message_signature and handle.last_message_signature == message_signature and cached_snapshot:
                cached_snapshot["provider_prefetch_cache_hit"] = True
                cached_snapshot["provider_runtime_reused"] = True
                cached_snapshot["provider_context_reused"] = bool(
                    str(cached_snapshot.get("provider_block") or "").strip()
                )
                cached_snapshot["prefetch_attempted"] = False
                return cached_snapshot

            snapshot["prefetch_attempted"] = True
            details: List[Dict[str, Any]] = []
            prefetch_with_details = getattr(handle.manager, "prefetch_all_with_details", None)
            if callable(prefetch_with_details):
                raw_details = prefetch_with_details(query, session_id=effective_session_id)
                if isinstance(raw_details, list):
                    details = [detail for detail in raw_details if isinstance(detail, dict)]
            else:
                prefetched = str(
                    handle.manager.prefetch_all(query, session_id=effective_session_id) or ""
                ).strip()
                if prefetched:
                    detail: Dict[str, Any] = {
                        "provider": str(
                            getattr(handle.provider, "name", "") or provider_name or "unknown"
                        ),
                        "content": prefetched,
                    }
                    debug_getter = getattr(handle.provider, "get_last_prefetch_debug_info", None)
                    if callable(debug_getter):
                        debug_info = debug_getter()
                        if isinstance(debug_info, dict) and debug_info:
                            detail["prefetch_debug"] = debug_info
                    details = [detail]

            merged_content = "\n\n".join(
                str(detail.get("content") or "").strip()
                for detail in details
                if str(detail.get("content") or "").strip()
            ).strip()
            snapshot["provider_details"] = details
            snapshot["provider_merged_content"] = merged_content
            snapshot["provider_block"] = build_memory_context_block(merged_content) if merged_content else ""
            snapshot["provider_runtime_reused"] = runtime_reused
            snapshot["provider_context_reused"] = False
            snapshot["provider_prefetch_cache_hit"] = False
            handle.last_message_signature = message_signature
            handle.last_snapshot = copy.deepcopy(snapshot)
            handle.last_snapshot_ts = time.time()
            return snapshot
        except Exception as exc:
            logger.debug("Orchestrator memory prefetch failed", exc_info=True)
            snapshot["provider_error"] = str(exc) or exc.__class__.__name__
            return snapshot

    @classmethod
    def _build_system_prompt_shared_core(cls) -> str:
        return (
            "You are Hermes Gateway's session orchestrator.\n"
            "You are the light controller, not the heavy worker.\n"
            "Choose exactly one outcome per turn: reply, spawn, or ignore.\n\n"
            "Return ONLY one JSON object. No markdown fences. No prose outside JSON.\n\n"
            "Formatting rules (highest priority):\n"
            "- The output must be valid JSON that parses with Python json.loads without repair.\n"
            "- Return exactly one top-level JSON object.\n"
            '- The top-level object must contain "action", "payload", and "complexity_score".\n'
            "- payload must always be a JSON object, never a string, array, number, boolean, or null.\n"
            "- Use only double quotes for JSON keys and string values.\n"
            "- Do not emit trailing commas.\n"
            "- Do not emit comments.\n"
            "- Before finishing, silently verify that braces and brackets are balanced and payload closes with } not ].\n\n"
            "Allowed output shapes:\n"
            '{"action":"reply","payload":{"text":"short direct reply"},"complexity_score":3}\n'
            '{"action":"spawn","payload":{"goal":"child task goal"},"complexity_score":34}\n'
            '{"action":"spawn","payload":{"goal":"child task goal","handoff_reply":"short acknowledgement"},"complexity_score":41}\n'
            '{"action":"ignore","payload":{},"complexity_score":0}\n'
            '{"action":"ignore","payload":{"cancel_children":["child-1"]},"complexity_score":0}\n\n'
            "Invalid outputs (DO NOT imitate):\n"
            '{"action":"reply","payload":{"text":"ok"}],"complexity_score":3} <- invalid because payload closes with ]\n'
            '{"action":"reply","payload":"ok","complexity_score":3} <- invalid because payload must be an object\n'
            '{"respond_now":true,"immediate_reply":"ok"} <- invalid because that is the legacy schema, not the required schema\n\n'
            "Shared routing rules:\n"
            "- Treat conversation_history and the injected routing context as authoritative context for routing. Reuse that context before guessing.\n"
            '- conversation_history assistant entries may include optional top-level fields such as "history_source", "reply_kind", or "child_id".\n'
            '- history_source="orchestrator" means that assistant entry is a prior routing/control decision. history_source="full_agent" means it is the actual user-visible reply produced after handoff.\n'
            '- A prior spawn decision is not proof that the task already completed, and an old full-agent summary is not fresh truth.\n'
            '- If the latest relevant assistant context has history_source="full_agent" and the user is asking to drill into, continue, refine, verify, or act on that result, treat it as substantive follow-up work for the full agent. Use action=spawn, not action=reply.\n'
            '- If the latest relevant assistant context has history_source="orchestrator" (a prior router-authored shallow reply) and the current user message is questioning, challenging, pushing back on, dissatisfied with, repeating, or asking why you did or did not do something about that prior reply, you MUST use action=spawn. The user pushback is a signal that the shallow router reply was insufficient. Do NOT chain another action=reply on top of a contested orchestrator reply.\n'
            "- If the user asks you to redo, retry, regenerate, try again, run it again, or do it once more (e.g., 'try again', 'redo', 'one more time', 're-run', '再来一次', '再试一次', '重做', '重新跑一遍', '再做一遍'), you MUST use action=spawn so the full agent can re-execute the original task.\n"
            '- Only use action=reply for the simplest direct replies or pure status/check-in questions about running work.\n'
            '- Any request asking for current device state, home device status, smart-home facts, sensor readings, or whether a household device is on/off/running MUST use action=spawn unless the user is only asking about currently running child/full-agent progress.\n'
            "- Any user request to perform a concrete action (open/close/start/stop/turn on/turn off/send/post/create/delete/run/execute/install/configure/restart/打开/关闭/启动/停止/发送/创建/删除/运行/执行/安装/配置/重启/帮我X/给我X) MUST use action=spawn so the full agent can actually perform the work. Do NOT use action=reply to merely acknowledge an action request without executing it.\n"
            "- Any question about current time, current date, day of week, '现在几点', '今天几号', '今天星期几', '现在是什么时间', 'what time is it', 'what is today's date', 'now', or anything that depends on the live wall-clock MUST use action=spawn. Your training-time clock is unreliable, so do NOT answer such questions with action=reply.\n"
            "- If a single user message contains both a greeting/check-in AND a concrete task, question, or request, treat the task as primary. Use action=spawn whenever the task itself would require spawn. Do NOT respond only to the greeting and silently drop the task.\n"
            "- If the user message is long-form (more than roughly 200 characters, multiple sentences across multiple lines, or multi-paragraph), use action=spawn unless the entire content is purely a status check-in or extended greeting.\n"
            "- Always include a top-level complexity_score from 0 to 100.\n"
            "- Do not decide or mention model/provider/runtime details. Full-agent routing is handled elsewhere.\n"
            "- Do not recommend, select, or mention toolsets. Child agents always use Hermes' normal full-agent tool behavior.\n"
            "- The system routes spawned work to L1 for scores 0-16 and to L2 for scores 17-100, so score honestly.\n"
            "- If a child is currently still running (child_board shows an active running child) and the user asks about its progress, status, what is running, whether you are stuck, or what the child is doing right now, use action=reply and answer from child_board or full_agent_status. This rule applies only while the child is still running — once it has finished and produced a full_agent reply, follow-up questions are governed by the history_source=\"full_agent\" rule above (use action=spawn).\n"
            "- If child_board is empty, stale, or has no active child matching what the user is asking about, do NOT fabricate progress or invent a 'still working on it' reply. Use action=spawn instead.\n"
            "- If child_board already shows an active child for the same or very similar goal, do not spawn a duplicate child.\n"
            "- If child_board shows an active child and you still choose action=spawn for additional work, include payload.handoff_reply as a short user-facing acknowledgement. If there is no active child already running, omit payload.handoff_reply for spawn.\n"
            "- If this turn is clearly a follow-up to the same topic as a prior high-complexity turn in this same chat context, do not score it below that prior high score.\n"
            "- Requests involving image generation, TTS, or STT MUST use action=spawn since they require tool execution. They are not automatically high complexity, though — if the work is otherwise straightforward, keep the complexity_score low enough to avoid over-escalating just because it is media-related.\n"
            "- If the user corrects you, says you got a fact wrong, revises a durable preference/identity/workflow detail, or says some version of 'not X, it is Y', you MUST use action=spawn so the full agent can reconcile the correction and update memory if needed.\n"
            "- If the user explicitly asks you to remember, save, or persist a fact, preference, or piece of information (e.g., 'remember this', '记住这个', '把这个记下来', 'keep this in mind', 'don't forget this'), you MUST use action=spawn so the full agent can use the memory tool to persist it.\n"
            "- If the user teaches you a new fact, workaround, device quirk, or operational tip you did not previously know (e.g., 'X does not work, you have to do Y first', '自动模式调温度是没用的，得先调制冷'), you MUST use action=spawn and include the new knowledge in the goal so the full agent can save it to memory before completing any related action.\n"
            "- If the user says 'your project', 'this project', 'the repo', or similar in this gateway/workspace context, interpret that as the current active workspace/repository unless the user clearly points to some other external project.\n"
            "- If the user references a workspace, project, repository, codebase, files, directories, architecture, implementation, bug, trace, or asks for any code analysis, lookup, or investigation, you MUST use action=spawn. Workspace ambiguity is never a reason for a trivial direct reply.\n"
            "- If the user pastes a code block, error log, stack trace, exception output, traceback, or any multi-line technical artifact, you MUST use action=spawn even if the surrounding text looks like a casual question.\n"
            "- If the user includes a URL/link or asks you to fetch, open, look at, summarize, or comment on a web link, you MUST use action=spawn so the full agent can fetch and inspect it.\n"
            "- If the user message references or includes an image, file, attachment, voice note, sticker, video, or any non-text content beyond your direct visibility, you MUST use action=spawn so the full agent can handle it.\n"
            "- Any user message asking about your memory, recall, history, past conversation, or your ability to check/search/look-up earlier context (English or Chinese: 'history', '历史', '历史消息', '聊天记录', '之前', '上次', '聊过', '说过', '记得', '记忆', '查一下', '翻一下', '查历史', 'remember', 'recall', 'memory', 'previous messages', 'what we talked about') MUST use action=spawn with complexity_score >= 20. Do NOT answer such questions with action=reply.\n"
            "- Even when routing_history appears short or empty, if the user asks you to check past conversation, memory, or what was previously discussed, you still MUST use action=spawn. Let the full agent investigate and explain if there truly is nothing — never invent a 'no history' shallow reply yourself.\n"
            "- Identity questions, capability questions (what can you do, do you have X feature, can you do Y), explanations, factual questions, or any request that needs fresh, live, current, or externally grounded information MUST use action=spawn, not action=reply.\n"
            "- Requests to generate, translate, write, compose, summarize, or rewrite content MUST use action=spawn so the full agent can produce the actual output.\n"
            "- If the user is telling YOU (the agent) to stop, abort, cancel, back off, or be quiet (e.g., 'stop', 'cancel', 'abort', '算了', '不用了', '别管了', '停下来', '不做了', '别说了', 'shut up', '安静'), use action=ignore. If a child is currently running on the related task, include cancel_children in payload with the matching child id(s). This rule only applies when the user is asking YOU to stop. If the user is asking you to stop, turn off, or pause a DEVICE (e.g., 'stop the dehumidifier', '把空调关了', 'turn off the lights'), that is a device action and follows the action-verb rule above (MUST use action=spawn).\n"
            "- The payload must stay minimal. For reply use payload.text. For spawn use payload.goal and only add payload.handoff_reply when an active child is already running and the user should see an immediate acknowledgement.\n"
        )

    @staticmethod
    def _build_system_prompt_dm_rules() -> str:
        return (
            "DM routing rules:\n"
            "- In DMs, do not use action=ignore unless the user explicitly told you to stop, abort, or be quiet.\n"
            "- Use action=reply for minimal greetings/check-ins and active-work status questions.\n"
            "- Shared-core MUST-spawn rules always take precedence over DM reply preference. If any shared-core rule says MUST spawn, use action=spawn even though we are in a DM.\n"
        )

    @staticmethod
    def _build_system_prompt_group_rules() -> str:
        return (
            "Group routing rules:\n"
            "- If trigger_reason indicates an explicit bot trigger (an @-mention of Hermes, a fixed prefix/keyword/command, a direct reply to Hermes, or any unambiguous addressing of the bot), the message is NEVER ignorable banter. You MUST pick action=reply or action=spawn based on its content; the ignore-default below does not apply in that case.\n"
            "- In groups, default to action=ignore. Only avoid ignore when the message is clearly related to the agent in context. Treat banter as an ultra-rare edge case, not a second default.\n"
            "- Group messages are clearly related to the agent only when at least one of these is true: trigger_reason shows an explicit bot trigger, child_board/full_agent_status shows the user is asking about active agent work, the recent context makes it explicit that the user is continuing a thread with the agent, or prefetched group memory explicitly defines that exact short message as a fixed trigger/keyword/command for Hermes.\n"
            "- Banter-style interjections are a narrow exception, not a default behavior. Allow them only when the message is a very short standalone reaction, has no concrete request/task/question to solve, has no technical/advice-seeking intent, recent_group_context does not show an active human discussion line, and the message is so self-contained that silence is still the normal outcome.\n"
            "- Do not interject just because a line is funny, answerable, sympathetic, or easy to react to. A short reaction alone is not sufficient permission to speak.\n"
            "- If a group message could plausibly be meant for other humans, could start a substantive discussion, or would benefit from more than a tiny reaction, use action=ignore instead of inserting Hermes into the conversation.\n"
            "- If a human in the room could naturally reply, laugh, or continue the thread without Hermes, prefer action=ignore.\n"
            "- When uncertain about whether a banter interjection is welcome, prefer action=ignore. Missing a chance to banter is better than barging into the group.\n"
            "- If the message looks like humans talking to each other in an active thread, use action=ignore unless they clearly addressed Hermes.\n"
            "- A group message being interesting, answerable, technical, or urgent is NOT enough by itself. If it is ambiguous whether the user is talking to the agent or to other humans in the group, use action=ignore.\n"
        )

    @staticmethod
    def _build_system_prompt_examples(chat_type: str) -> str:
        examples = [
            '{"action":"reply","payload":{"text":"在，我在。"},"complexity_score":3}',
            '{"action":"reply","payload":{"text":"Still working on the repo analysis. Current step: iteration 2. Current tool: terminal. Latest progress: inspecting tests."},"complexity_score":12}',
            '{"action":"spawn","payload":{"goal":"Analyze the current workspace project structure and summarize the main directories and modules"},"complexity_score":34}',
            '{"action":"spawn","payload":{"goal":"看看除湿机细节信息"},"complexity_score":18} <- recent history_source=\"full_agent\" device result, user is drilling into that result',
            '{"action":"spawn","payload":{"goal":"Check the current weather and reply briefly"},"complexity_score":18}',
            '{"action":"spawn","payload":{"goal":"Handle the user correction, fix the answer, and update memory if needed: 不是，我是后端，不是前端"},"complexity_score":24}',
            '{"action":"spawn","payload":{"goal":"用户对刚才那条 router 浅回复不满，重新认真回答：你不会查历史消息吗"},"complexity_score":22} <- previous reply was history_source="orchestrator", user is pushing back',
            '{"action":"spawn","payload":{"goal":"Save the new fact to memory and then adjust the device: 自动模式下调温度无效，必须先切换到制冷模式"},"complexity_score":20} <- user taught a new device quirk that should be persisted',
        ]
        if str(chat_type or "").strip().lower() == "dm":
            examples.append('{"action":"reply","payload":{"text":"哈，我在的说。"},"complexity_score":2}')
        else:
            examples.extend(
                [
                    '{"action":"reply","payload":{"text":"我还在看刚才那个仓库问题，当前先盯着 gateway 这块。"},"complexity_score":8} <- group user is explicitly checking Hermes\'s active work',
                    '{"action":"ignore","payload":{},"complexity_score":0} <- group message is a short reaction like "这也行"，but it is not clearly directed at Hermes',
                    '{"action":"ignore","payload":{},"complexity_score":0} <- group message is already part of an ongoing human discussion line',
                ]
            )
        return "Examples:\n" + "\n".join(examples)

    @classmethod
    def build_system_prompt(cls, chat_type: str = "dm") -> str:
        normalized_chat_type = str(chat_type or "").strip().lower()
        sections = [cls._build_system_prompt_shared_core()]
        if normalized_chat_type == "dm":
            sections.append(cls._build_system_prompt_dm_rules())
        else:
            sections.append(cls._build_system_prompt_group_rules())
        sections.append(cls._build_system_prompt_examples(normalized_chat_type))
        return "\n".join(section.rstrip() for section in sections if section).strip()

    def seed_routing_history(
        self,
        session: GatewayOrchestratorSession,
        history: List[Dict[str, Any]],
        *,
        max_messages: int = 24,
        max_content_len: int = 220,
    ) -> None:
        if session.routing_history:
            return
        seeded: List[Dict[str, str]] = []
        for msg in history or []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip()
            raw_content = str(msg.get("content") or "")
            ts = str(msg.get("timestamp") or "").strip()
            if role == "session_meta":
                try:
                    canonical = self.canonicalize_history_decision(
                        parse_orchestrator_decision(raw_content),
                        history_source="orchestrator",
                    )
                except Exception:
                    continue
                if canonical:
                    entry: Dict[str, str] = {"role": "assistant", "content": canonical}
                    if ts:
                        entry["timestamp"] = ts
                    seeded.append(entry)
                continue
            if role not in {"user", "assistant"}:
                continue
            content = self.trim_status_text(raw_content, max_len=max_content_len)
            if content:
                if role == "assistant":
                    compacted = self.compact_history(
                        [msg],
                        max_messages=max_messages,
                        max_content_len=max_content_len,
                    )
                    seeded.extend(compacted)
                else:
                    entry = {"role": role, "content": content}
                    if ts:
                        entry["timestamp"] = ts
                    seeded.append(entry)
        if max_messages > 0 and len(seeded) > max_messages:
            seeded = seeded[-max_messages:]
        for entry in seeded:
            session.append_routing_history_entry(**entry, max_entries=max_messages * 2 if max_messages > 0 else 0)

    def build_routing_history(
        self,
        *,
        session: GatewayOrchestratorSession,
        history: List[Dict[str, Any]],
        max_messages: int = 24,
    ) -> List[Dict[str, str]]:
        self.seed_routing_history(session, history, max_messages=max_messages)
        compacted = self.compact_history(
            list(session.routing_history),
            max_messages=max_messages,
        )
        return self.compose_cached_routing_history(
            session=session,
            compact_history=compacted,
            max_messages=max_messages,
            stable_prefix_messages=self._routing_stable_prefix_messages(),
        )

    def build_routing_history_for_model(
        self,
        *,
        session: GatewayOrchestratorSession,
        history: List[Dict[str, Any]],
        model: str,
        runtime_kwargs: Optional[Dict[str, Any]] = None,
        system_prompt: str = "",
        user_message: str = "",
        turn_system_context: str = "",
        turn_user_context: str = "",
    ) -> List[Dict[str, str]]:
        from agent.model_metadata import get_model_context_length, estimate_request_tokens_rough

        self.seed_routing_history(session, history, max_messages=0)
        compacted = self.compact_history(
            list(session.routing_history),
            max_messages=0,
        )
        if not compacted:
            return compacted

        runtime_kwargs = runtime_kwargs or {}
        configured_context_length = self._orchestrator_context_length()
        context_length = get_model_context_length(
            model,
            base_url=str(runtime_kwargs.get("base_url") or ""),
            api_key=str(runtime_kwargs.get("api_key") or ""),
            config_context_length=configured_context_length or None,
            provider=str(runtime_kwargs.get("provider") or ""),
        )
        effective_system = str(system_prompt or "").strip()
        if turn_system_context:
            effective_system = (
                effective_system + "\n\n" + str(turn_system_context).strip()
            ).strip()
        effective_user = str(user_message or "").strip()
        if turn_user_context:
            effective_user = (
                effective_user + "\n\n" + str(turn_user_context).strip()
            ).strip()
        projected_messages = list(compacted)
        if effective_user:
            projected_messages.append({"role": "user", "content": effective_user})
        estimated_tokens = estimate_request_tokens_rough(
            projected_messages,
            system_prompt=effective_system,
        )
        if estimated_tokens <= context_length:
            return compacted

        stable_prefix_messages = self._routing_stable_prefix_messages()
        candidate_count = len(compacted)
        while candidate_count > 1:
            candidate_count = max(1, candidate_count // 2)
            candidate = self.compose_cached_routing_history(
                session=session,
                compact_history=compacted,
                max_messages=candidate_count,
                stable_prefix_messages=stable_prefix_messages,
            )
            projected_candidate = list(candidate)
            if effective_user:
                projected_candidate.append({"role": "user", "content": effective_user})
            candidate_tokens = estimate_request_tokens_rough(
                projected_candidate,
                system_prompt=effective_system,
            )
            if candidate_tokens <= context_length:
                return candidate
        return self.compose_cached_routing_history(
            session=session,
            compact_history=compacted,
            max_messages=1,
            stable_prefix_messages=stable_prefix_messages,
        )

    @staticmethod
    def _history_entries_match(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        return (
            str((left or {}).get("role") or "").strip()
            == str((right or {}).get("role") or "").strip()
            and str((left or {}).get("content") or "")
            == str((right or {}).get("content") or "")
        )

    @classmethod
    def _common_history_prefix_len(
        cls,
        left: List[Dict[str, Any]],
        right: List[Dict[str, Any]],
    ) -> int:
        limit = min(len(left or []), len(right or []))
        matched = 0
        while matched < limit and cls._history_entries_match(left[matched], right[matched]):
            matched += 1
        return matched

    @classmethod
    def compose_cached_routing_history(
        cls,
        *,
        session: GatewayOrchestratorSession,
        compact_history: List[Dict[str, str]],
        max_messages: int = 24,
        stable_prefix_messages: int = _ORCHESTRATOR_ROUTING_STABLE_PREFIX_MESSAGES,
    ) -> List[Dict[str, str]]:
        history = list(compact_history or [])
        if not history or max_messages <= 0:
            return history

        stable_limit = min(max(0, stable_prefix_messages), max_messages)
        if stable_limit <= 0:
            return history[:max_messages]

        session.capture_routing_stable_prefix(history, max_entries=stable_limit)
        stable_prefix = list(session.routing_stable_prefix[:stable_limit])
        if not stable_prefix or len(history) <= len(stable_prefix):
            return history[:max_messages]

        tail_keep = max_messages - len(stable_prefix)
        if tail_keep <= 0:
            return stable_prefix[:max_messages]

        shared_prefix = cls._common_history_prefix_len(stable_prefix, history)
        available_tail = len(history) - shared_prefix
        tail_take = min(tail_keep, max(0, available_tail))
        if tail_take <= 0:
            return stable_prefix[:max_messages]

        tail = list(history[-tail_take:])
        head = list(stable_prefix)
        if head and tail and head[-1]["role"] == tail[0]["role"]:
            head[-1] = {
                "role": head[-1]["role"],
                "content": head[-1]["content"] + "\n" + tail[0]["content"],
            }
            tail = tail[1:]
        return (head + tail)[:max_messages]

    @classmethod
    def format_routing_decision(
        cls,
        *,
        action: str,
        decision: OrchestratorDecision,
        response: str = "",
        reply_semantics: str = "",
        spawn_result: Optional[Dict[str, Any]] = None,
        suppressed_status_reply: bool = False,
    ) -> str:
        normalized_action = str(action or "").strip().lower() or "reply"
        fields: List[str] = [
            "Routing decision",
            f"action={normalized_action}",
            f"complexity_score={decision.complexity_score}",
        ]
        if normalized_action == "reply":
            reply_kind = str(reply_semantics or "").strip() or cls.classify_reply(response) or "final"
            fields.append(f"reply_kind={reply_kind}")
            if suppressed_status_reply:
                fields.append("reply_suppressed=true")
            response_summary = cls.trim_status_text(response, max_len=120)
            if response_summary:
                fields.append(f"reply_summary={response_summary}")
        elif normalized_action == "spawn":
            goals = [
                cls.trim_status_text(request.goal, max_len=90)
                for request in decision.spawn_children
                if str(getattr(request, "goal", "") or "").strip()
            ]
            if goals:
                fields.append(f"goals={' | '.join(goals[:3])}")
            if spawn_result:
                if spawn_result.get("started"):
                    fields.append(f"spawn_started={int(spawn_result.get('started') or 0)}")
                if spawn_result.get("queued"):
                    fields.append(f"spawn_queued={int(spawn_result.get('queued') or 0)}")
                if spawn_result.get("reused"):
                    fields.append(f"spawn_reused={int(spawn_result.get('reused') or 0)}")
                if spawn_result.get("skipped"):
                    fields.append(f"spawn_skipped={int(spawn_result.get('skipped') or 0)}")
        elif normalized_action == "ignore":
            fields.append("ignored=true")

        if decision.cancel_children:
            fields.append(f"cancel_children={','.join(decision.cancel_children[:6])}")
        return "; ".join(field for field in fields if field)

    def record_routing_turn(
        self,
        *,
        session: GatewayOrchestratorSession,
        history: List[Dict[str, Any]],
        user_message: str,
        decision: OrchestratorDecision,
        response: str = "",
        reply_semantics: str = "",
        spawn_result: Optional[Dict[str, Any]] = None,
        suppressed_status_reply: bool = False,
    ) -> str:
        self.seed_routing_history(session, history, max_messages=0)
        max_entries = self._routing_history_max_entries()
        trimmed_user = self.trim_status_text(user_message, max_len=220)
        if trimmed_user:
            session.append_routing_history_entry(
                role="user",
                content=trimmed_user,
                timestamp=datetime.now().isoformat(),
                max_entries=max_entries,
            )

        action = "reply"
        if decision.ignore_message:
            action = "ignore"
        elif spawn_result and (
            int(spawn_result.get("started") or 0) > 0
            or int(spawn_result.get("queued") or 0) > 0
        ):
            action = "spawn"
        elif response or reply_semantics == "status":
            action = "reply"
        elif decision.spawn_children:
            action = "spawn"
        elif not decision.respond_now:
            action = "ignore"

        assistant_decision = OrchestratorDecision(
            complexity_score=self.normalize_complexity_score(
                decision.complexity_score,
                default=1,
            ),
            respond_now=action == "reply",
            immediate_reply=self.trim_status_text(
                (response if not suppressed_status_reply else "") or decision.immediate_reply,
                max_len=220,
            ) if action == "reply" else "",
            spawn_handoff_reply=self.trim_status_text(
                decision.spawn_handoff_reply,
                max_len=220,
            ) if action == "spawn" else "",
            spawn_children=list(decision.spawn_children) if action == "spawn" else [],
            cancel_children=list(decision.cancel_children),
            ignore_message=action == "ignore",
        )
        assistant_content = ""
        if action != "reply" or assistant_decision.immediate_reply:
            assistant_content = self.canonicalize_history_decision(
                assistant_decision,
                history_source="orchestrator",
                reply_kind=str(reply_semantics or "").strip(),
            )

        if assistant_content:
            session.append_routing_history_entry(
                role="assistant",
                content=assistant_content,
                timestamp=datetime.now().isoformat(),
                max_entries=max_entries,
            )
        return assistant_content

    @staticmethod
    def extract_routing_history_entry_complexity(entry: Dict[str, Any]) -> Optional[int]:
        if not isinstance(entry, dict):
            return None
        content = str(entry.get("content") or "")
        score = extract_orchestrator_complexity_marker(content)
        if score is not None:
            return score
        try:
            return parse_orchestrator_decision(content).complexity_score
        except Exception:
            return None

    @classmethod
    def extract_routing_history_entry_metadata(cls, entry: Dict[str, Any]) -> Dict[str, str]:
        if not isinstance(entry, dict):
            return {}
        return cls._history_metadata_from_json(str(entry.get("content") or ""))

    @staticmethod
    def extract_routing_history_entry_reply_text(entry: Dict[str, Any]) -> str:
        if not isinstance(entry, dict):
            return ""
        content = str(entry.get("content") or "").strip()
        if not content:
            return ""
        try:
            decision = parse_orchestrator_decision(content)
        except Exception:
            return ""
        if not decision.respond_now:
            return ""
        return str(decision.immediate_reply or "").strip()

    @staticmethod
    def _extract_followup_focus_terms(message: str) -> List[str]:
        normalized = normalize_dedupe_key(message)
        if not normalized:
            return []
        simplified = normalized
        stop_phrases = (
            "看看",
            "看下",
            "看一下",
            "查查",
            "查下",
            "查一下",
            "一下",
            "一个",
            "一下子",
            "细节",
            "详情",
            "详细",
            "信息",
            "情况",
            "具体",
            "展开",
            "继续",
            "再看",
            "再说",
            "说说",
            "讲讲",
            "补充",
            "里面",
            "其中",
            "刚才",
            "刚刚",
            "上面",
            "这个",
            "那个",
            "这些",
            "那些",
            "结果",
            "设备",
            "状态",
            "所有",
            "全部",
            "detail",
            "details",
            "specific",
            "specifics",
            "expand",
            "elaborate",
            "continue",
            "followup",
        )
        for phrase in stop_phrases:
            simplified = simplified.replace(phrase, " ")
        terms = [
            item.strip()
            for item in re.findall(r"[a-z0-9\u4e00-\u9fff]+", simplified)
            if len(item.strip()) >= 2
        ]
        deduped: List[str] = []
        seen: set[str] = set()
        for term in terms:
            if term not in seen:
                seen.add(term)
                deduped.append(term)
        if deduped:
            return deduped
        collapsed = normalized.replace(" ", "")
        return [collapsed] if len(collapsed) >= 2 else []

    @staticmethod
    def _is_substantive_full_agent_followup_message(message: str) -> bool:
        lowered = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not lowered:
            return False
        markers = (
            "这个",
            "那个",
            "这些",
            "那些",
            "上面",
            "刚才",
            "刚刚",
            "里面",
            "其中",
            "细节",
            "详情",
            "详细",
            "具体",
            "展开",
            "继续",
            "补充",
            "再看",
            "再说",
            "说说",
            "讲讲",
            "单独",
            "fullagent",
            "this",
            "that",
            "those",
            "detail",
            "details",
            "specific",
            "specifics",
            "expand",
            "elaborate",
            "continue",
            "follow up",
            "drill into",
            "deep dive",
        )
        return any(marker in lowered for marker in markers)

    def full_agent_followup_requires_handoff(
        self,
        session: GatewayOrchestratorSession,
        user_message: str,
    ) -> bool:
        msg = str(user_message or "").strip()
        if not msg:
            return False
        if self.is_progress_query(msg) or self.is_active_child_status_checkin(msg):
            return False

        focus_terms = self._extract_followup_focus_terms(msg)
        substantive_followup = self._is_substantive_full_agent_followup_message(msg)

        def _matches_reference(reference_text: str) -> bool:
            normalized_reference = normalize_dedupe_key(reference_text)
            if not normalized_reference:
                return False
            return any(term in normalized_reference for term in focus_terms if term)

        for idx in range(len(session.routing_history) - 1, -1, -1):
            entry = session.routing_history[idx]
            if str(entry.get("role") or "").strip() != "assistant":
                continue
            metadata = self.extract_routing_history_entry_metadata(entry)
            if metadata.get("history_source") != "full_agent":
                continue

            reply_text = self.extract_routing_history_entry_reply_text(entry)
            if reply_text and _matches_reference(reply_text):
                return True

            previous_user_text = ""
            for back in range(idx - 1, -1, -1):
                prev = session.routing_history[back]
                if str(prev.get("role") or "").strip() != "user":
                    continue
                previous_user_text = str(prev.get("content") or "").strip()
                break
            if previous_user_text and _matches_reference(previous_user_text):
                return True

            if substantive_followup:
                return True
            break

        for child in session.recent_children[:3]:
            summary = str(child.summary or child.actual_user_reply_sent or "").strip()
            goal = str(child.goal or "").strip()
            if summary and _matches_reference(summary):
                return True
            if goal and _matches_reference(goal):
                return True

        return False

    def related_complexity_floor(
        self,
        session: GatewayOrchestratorSession,
        user_message: str,
    ) -> Optional[int]:
        current_key = normalize_dedupe_key(user_message)
        followup_like = self.is_group_followup_marker(user_message)
        threshold = _ORCHESTRATOR_L2_COMPLEXITY_THRESHOLD
        candidates: List[int] = []

        def _consider(score: Any) -> None:
            normalized = self.normalize_complexity_score(score, default=0)
            if normalized > threshold:
                candidates.append(normalized)

        if current_key:
            for child in list(session.active_children.values()) + list(session.recent_children):
                child_key = child.dedupe_key or normalize_dedupe_key(child.goal)
                if child_key and dedupe_keys_similar(current_key, child_key):
                    _consider(getattr(child, "complexity_score", 0))

        if not session.routing_history:
            return max(candidates) if candidates else None

        for idx in range(len(session.routing_history) - 1, -1, -1):
            entry = session.routing_history[idx]
            if str(entry.get("role") or "").strip() != "assistant":
                continue
            score = self.extract_routing_history_entry_complexity(entry)
            if score is None or score <= threshold:
                continue

            previous_user_key = ""
            for back in range(idx - 1, -1, -1):
                prev = session.routing_history[back]
                if str(prev.get("role") or "").strip() != "user":
                    continue
                previous_user_key = normalize_dedupe_key(prev.get("content") or "")
                break

            if followup_like or not current_key or not previous_user_key:
                _consider(score)
                if followup_like:
                    break
                continue

            if dedupe_keys_similar(current_key, previous_user_key):
                _consider(score)
                break

        return max(candidates) if candidates else None

    @staticmethod
    def extract_used_tool_names(messages: List[Dict[str, Any]]) -> List[str]:
        tool_names: List[str] = []
        seen: set[str] = set()
        for msg in messages or []:
            for tool_call in msg.get("tool_calls") or []:
                fn = tool_call.get("function") or {}
                name = str(fn.get("name") or "").strip()
                if name and name not in seen:
                    seen.add(name)
                    tool_names.append(name)
            tool_name = str(msg.get("tool_name") or "").strip()
            if tool_name and tool_name not in seen:
                seen.add(tool_name)
                tool_names.append(tool_name)
        return tool_names

    @classmethod
    def compact_history(
        cls,
        history: List[Dict[str, Any]],
        *,
        max_messages: int = 24,
        max_content_len: int = 500,
    ) -> List[Dict[str, str]]:
        compact_history: List[Dict[str, str]] = []
        pending_tools: List[str] = []
        pending_seen: set[str] = set()

        def _normalize_assistant_contents(msg: Dict[str, Any]) -> List[str]:
            raw_text = str((msg or {}).get("content") or "")
            text = cls.trim_status_text(raw_text, max_len=max_content_len)
            if not text:
                return []
            default_history_source = cls._history_source_from_message(msg)
            default_reply_kind = str((msg or {}).get("reply_kind") or "").strip()
            default_child_id = str((msg or {}).get("child_id") or "").strip()
            raw_meta = cls._history_metadata_from_json(text)
            effective_history_source = raw_meta.get("history_source") or default_history_source
            effective_reply_kind = raw_meta.get("reply_kind") or default_reply_kind
            effective_child_id = raw_meta.get("child_id") or default_child_id
            legacy_routing = cls.canonicalize_routing_summary_text(
                text,
                history_source=effective_history_source,
                max_len=max_content_len,
            )
            if legacy_routing:
                return [legacy_routing]
            try:
                decision = parse_orchestrator_decision(text)
                if decision.respond_now and decision.immediate_reply:
                    embedded_entries = cls._canonicalize_embedded_decision_entries(
                        decision.immediate_reply,
                        history_source=effective_history_source,
                        reply_kind=effective_reply_kind,
                        child_id=effective_child_id,
                    )
                    if embedded_entries:
                        return embedded_entries
                return [
                    cls.canonicalize_history_decision(
                        decision,
                        history_source=effective_history_source,
                        reply_kind=effective_reply_kind,
                        child_id=effective_child_id,
                    )
                ]
            except Exception:
                pass
            embedded_entries = cls._canonicalize_embedded_decision_entries(
                text,
                history_source=effective_history_source,
                reply_kind=effective_reply_kind,
                child_id=effective_child_id,
            )
            if embedded_entries:
                return embedded_entries
            return [
                cls.canonicalize_reply_history_json(
                    strip_orchestrator_complexity_marker(text),
                    complexity_score=extract_orchestrator_complexity_marker(text),
                    history_source=effective_history_source,
                    reply_kind=effective_reply_kind,
                    child_id=effective_child_id,
                    max_len=max_content_len,
                )
            ]

        def _remember_tools(names: List[str]) -> None:
            for name in names:
                cleaned = str(name or "").strip()
                if cleaned and cleaned not in pending_seen:
                    pending_seen.add(cleaned)
                    pending_tools.append(cleaned)

        def _flush_pending_tools() -> None:
            if not pending_tools:
                return
            compact_history.append(
                {
                    "role": "assistant",
                    "content": _normalize_assistant_contents(
                        {"content": f"Tool activity summary: used {', '.join(pending_tools[:6])}."}
                    )[0],
                }
            )
            pending_tools.clear()
            pending_seen.clear()

        def _merge_consecutive_role(
            existing: List[Dict[str, str]], role: str, content: str, timestamp: str = ""
        ) -> None:
            """Append *content* under *role*, merging with the last entry if it
            has the same role so that the output always alternates user/assistant."""
            if (
                existing
                and existing[-1]["role"] == role
                and not (
                    role == "assistant"
                    and str(existing[-1].get("content") or "").lstrip().startswith("{")
                    and str(content or "").lstrip().startswith("{")
                )
            ):
                existing[-1]["content"] = existing[-1]["content"] + "\n" + content
            else:
                entry: Dict[str, str] = {"role": role, "content": content}
                if timestamp:
                    entry["timestamp"] = timestamp
                existing.append(entry)

        for msg in history or []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip()
            if role in {"session_meta", "system"}:
                continue

            tool_names: List[str] = []
            for tool_call in msg.get("tool_calls") or []:
                fn = tool_call.get("function") or {}
                name = str(fn.get("name") or "").strip()
                if name:
                    tool_names.append(name)
            if role == "tool":
                tool_name = str(msg.get("tool_name") or msg.get("name") or "").strip()
                if tool_name:
                    tool_names.append(tool_name)
            if tool_names:
                _remember_tools(tool_names)

            if role == "tool":
                continue

            content = str(msg.get("content") or "").strip()
            if not content:
                continue

            if role == "assistant" and msg.get("tool_calls"):
                continue
            if role not in {"user", "assistant"}:
                continue

            _flush_pending_tools()
            msg_ts = str(msg.get("timestamp") or "").strip()
            if role == "assistant":
                normalized_contents = _normalize_assistant_contents(msg)
                for normalized_content in normalized_contents:
                    if not normalized_content:
                        continue
                    _merge_consecutive_role(compact_history, role, normalized_content, msg_ts)
                continue
            normalized_content = cls.trim_status_text(content, max_len=max_content_len)
            if not normalized_content:
                continue
            _merge_consecutive_role(compact_history, role, normalized_content, msg_ts)

        _flush_pending_tools()
        if max_messages > 0 and len(compact_history) > max_messages:
            stable_prefix = min(
                _ORCHESTRATOR_ROUTING_STABLE_PREFIX_MESSAGES,
                max_messages,
            )
            if stable_prefix > 0 and len(compact_history) > stable_prefix:
                head = list(compact_history[:stable_prefix])
                tail_keep = max_messages - stable_prefix
                if tail_keep <= 0:
                    return head[:max_messages]
                tail = list(compact_history[-tail_keep:])
                if head and tail and head[-1]["role"] == tail[0]["role"]:
                    head[-1]["content"] = head[-1]["content"] + "\n" + tail[0]["content"]
                    tail = tail[1:]
                return (head + tail)[:max_messages]
            return compact_history[-max_messages:]
        return compact_history

    @staticmethod
    def build_turn_message(
        *,
        user_message: str,
        source: SessionSource,
        session_entry: Any,
        platform_turn: AdapterTurnPlan,
        session: GatewayOrchestratorSession,
        event_metadata: Optional[Dict[str, Any]] = None,
        memory_context_block: str = "",
    ) -> str:
        del source, session_entry, platform_turn, session, event_metadata, memory_context_block
        return str(user_message or "").strip()

    def build_turn_system_context(
        self,
        *,
        user_message: str,
        source: SessionSource,
        session_entry: Any,
        platform_turn: AdapterTurnPlan,
        session: GatewayOrchestratorSession,
        event_metadata: Optional[Dict[str, Any]] = None,
        memory_context_block: str = "",
    ) -> str:
        routing_prompt = str(getattr(platform_turn, "routing_prompt", "") or "").strip()
        structured_context = self._build_turn_structured_context(
            user_message=user_message,
            source=source,
            session_entry=session_entry,
            platform_turn=platform_turn,
            session=session,
            event_metadata=event_metadata,
            memory_context_block=memory_context_block,
        )
        return "\n\n".join(
            part
            for part in (structured_context, routing_prompt)
            if part
        ).strip()

    def build_turn_user_context(
        self,
        *,
        user_message: str,
        source: SessionSource,
        session_entry: Any,
        platform_turn: AdapterTurnPlan,
        session: GatewayOrchestratorSession,
        event_metadata: Optional[Dict[str, Any]] = None,
        memory_context_block: str = "",
    ) -> str:
        return self._build_turn_structured_context(
            user_message=user_message,
            source=source,
            session_entry=session_entry,
            platform_turn=platform_turn,
            session=session,
            event_metadata=event_metadata,
            memory_context_block=memory_context_block,
        )

    @staticmethod
    def result_has_visible_assistant_reply(result: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(result, dict):
            return False
        final_response = str(result.get("final_response") or "").strip()
        if not final_response:
            return False
        if bool(result.get("already_sent")) or bool(result.get("response_previewed")):
            return True
        messages = result.get("messages") or []
        if not isinstance(messages, list):
            return False
        history_rewritten = bool(result.get("history_rewritten"))
        try:
            history_offset = int(result.get("history_offset", 0) or 0)
        except (TypeError, ValueError):
            history_offset = 0
        current_turn_messages = messages if history_rewritten else messages[max(0, history_offset) :]
        for msg in current_turn_messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "assistant":
                continue
            if msg.get("tool_calls"):
                continue
            if str(msg.get("content") or "").strip() == final_response:
                return True
        return False

    @staticmethod
    def complexity_score_requires_handoff(
        score: int,
        *,
        threshold: Optional[int] = None,
    ) -> bool:
        effective_threshold = (
            _ORCHESTRATOR_L2_COMPLEXITY_THRESHOLD
            if threshold is None
            else int(threshold or 0)
        )
        return int(score or 0) > effective_threshold

    @staticmethod
    def normalize_complexity_score(value: Any, *, default: int = 100) -> int:
        try:
            score = int(value)
        except (TypeError, ValueError):
            score = default
        return max(0, min(100, score))

    def resolve_child_model_override(
        self,
        request: OrchestratorChildRequest,
        platform_turn: AdapterTurnPlan,
    ) -> Dict[str, Any]:
        request_override = dict(request.turn_model_override or {})
        if request_override:
            return request_override
        config = self._orchestrator_config()
        policy = str(getattr(config, "child_model_policy", "promotion") or "promotion").strip().lower()
        if policy == "none":
            return {}
        promotion_plan = get_napcat_promotion_plan(platform_turn)
        snapshot = dict(promotion_plan.config_snapshot or {})
        if snapshot.get("l1") or snapshot.get("l2"):
            safeguards = snapshot.get("safeguards") if isinstance(snapshot.get("safeguards"), dict) else {}
            score_threshold = NapCatPromotionSupport.normalize_complexity_threshold(
                safeguards.get("l2_complexity_score_threshold")
            )
            if self.complexity_score_requires_handoff(
                self.normalize_complexity_score(
                    getattr(request, "complexity_score", None),
                    default=18,
                ),
                threshold=score_threshold,
            ):
                return dict(snapshot.get("l2") or {})
            return dict(snapshot.get("l1") or {})
        if promotion_plan.turn_model_override:
            return dict(promotion_plan.turn_model_override)
        if promotion_plan.stage == "l2":
            return dict(snapshot.get("l2") or {})
        if promotion_plan.stage == "l1":
            return dict(snapshot.get("l1") or {})
        return {}

    def resolve_child_toolsets(
        self,
        request: OrchestratorChildRequest,
        dynamic_disabled_toolsets: Optional[List[str]] = None,
        source: Optional[SessionSource] = None,
    ) -> List[str]:
        config = self._orchestrator_config()
        requested = [
            str(item).strip()
            for item in (request.allowed_toolsets or [])
            if str(item).strip()
        ]
        defaults = [
            str(item).strip()
            for item in (getattr(config, "child_default_toolsets", []) or [])
            if str(item).strip()
        ]
        disabled = {
            str(item).strip()
            for item in (dynamic_disabled_toolsets or [])
            if str(item).strip()
        }
        selected = requested or defaults
        if not selected and source is not None:
            platform = str(source.platform.value).strip().lower()
            if platform and platform != "local":
                selected = [f"hermes-{platform}"]
        if not selected:
            return []
        return [name for name in selected if name not in disabled]

    @staticmethod
    def orchestrator_internal_session_id(session_key: str) -> str:
        return f"{session_key}::orchestrator"

    @staticmethod
    def orchestrator_child_session_id(parent_session_id: str, child_id: str) -> str:
        base = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(parent_session_id or "").strip()) or "gateway"
        return f"{base}__orchestrator_child__{child_id}"

    @staticmethod
    def orchestrator_fullagent_session_id(parent_session_id: str) -> str:
        base = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(parent_session_id or "").strip()) or "gateway"
        return f"{base}__orchestrator_fullagent"

    @staticmethod
    def orchestrator_fullagent_runtime_session_key(session_key: str) -> str:
        return f"{session_key}::fullagent"

    @staticmethod
    def orchestrator_child_runtime_session_key(session_key: str, child_id: str) -> str:
        return f"{session_key}::child:{child_id}"

    @classmethod
    def orchestrator_cache_key_belongs_to_session(
        cls,
        session_key: str,
        cache_key: str,
    ) -> bool:
        normalized_cache_key = str(cache_key or "").strip()
        if not normalized_cache_key:
            return False
        return (
            normalized_cache_key == cls.orchestrator_fullagent_runtime_session_key(session_key)
            or normalized_cache_key.startswith(f"{session_key}::child:")
        )

    @classmethod
    def orchestrator_child_runtime_identity(
        cls,
        parent_session_id: str,
        session_key: str,
        dedupe_key: str,
    ) -> tuple[str, str]:
        normalized_key = normalize_dedupe_key(dedupe_key)
        if not normalized_key:
            normalized_key = str(dedupe_key or "").strip().lower()
        if not normalized_key:
            child_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(dedupe_key or "").strip()) or "child"
            return (
                cls.orchestrator_child_session_id(parent_session_id, child_id),
                cls.orchestrator_child_runtime_session_key(session_key, child_id),
            )

        token = hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()[:16]
        base = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(parent_session_id or "").strip()) or "gateway"
        return (
            f"{base}__orchestrator_worker__{token}",
            f"{session_key}::child:{token}",
        )

    @classmethod
    def is_status_reply(cls, text: str) -> bool:
        normalized = str(text or "").strip().lower()
        if not normalized:
            return False
        return normalized.startswith(
            (
                "still working on:",
                "still working on the current request.",
                "no background task is running right now.",
                "there is no active background task right now.",
                "background task capacity is full right now.",
            )
        )

    @classmethod
    def classify_reply(cls, text: str) -> str:
        normalized = str(text or "").strip()
        if not normalized:
            return ""
        if cls.is_status_reply(normalized):
            return "status"
        return "final"
