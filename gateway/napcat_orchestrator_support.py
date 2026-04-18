"""Branch-local support for NapCat gateway orchestration helpers."""

from __future__ import annotations

import json
import hashlib
import logging
import re
import time
from datetime import datetime
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

    def _orchestrator_config(self):
        getter = getattr(self.host, "gateway_config", None)
        config = getter() if callable(getter) else getattr(self.runner, "config", None)
        return get_gateway_orchestrator_config(config, platform=Platform.NAPCAT)

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
        # Compatibility fallback for direct support construction in tests or
        # legacy call sites that still pass a bare runner.
        return getattr(self.runner, "_running_agents", {}).get(session_key)

    def _get_running_agent_started_at(self, session_key: str) -> float:
        getter = getattr(self.host, "get_running_agent_started_at", None)
        if callable(getter):
            return float(getter(session_key) or 0)
        # Compatibility fallback for direct support construction in tests or
        # legacy call sites that still pass a bare runner.
        return float(getattr(self.runner, "_running_agents_ts", {}).get(session_key, 0) or 0)

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
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def canonicalize_history_decision(cls, decision: OrchestratorDecision) -> str:
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

        return json.dumps(
            {
                "action": action,
                "payload": payload,
                "complexity_score": cls.normalize_complexity_score(
                    decision.complexity_score,
                    default=1,
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def canonicalize_routing_summary_text(
        cls,
        text: str,
        *,
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
                )
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
                )
            )

        if action == "ignore":
            return cls.canonicalize_history_decision(
                OrchestratorDecision(
                    complexity_score=normalized_score,
                    respond_now=False,
                    cancel_children=cancel_children,
                    ignore_message=True,
                )
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

    def build_memory_context_snapshot(
        self,
        *,
        user_message: str,
        source: SessionSource,
        session_key: str,
        session_id: str = "",
        turn_number: int = 1,
    ) -> Dict[str, Any]:
        query = str(user_message or "").strip()

        builtin_block = self._build_builtin_memory_context_block(source=source)
        provider_snapshot = self._build_provider_memory_context_snapshot(
            query=query,
            source=source,
            session_key=session_key,
            session_id=session_id,
            turn_number=turn_number,
        )
        provider_block = str(provider_snapshot.get("provider_block") or "").strip()
        blocks: list[str] = []
        if builtin_block:
            blocks.append(builtin_block)
        if provider_block:
            blocks.append(provider_block)

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
            "builtin_block": builtin_block,
            "provider_block": provider_block,
            "combined_block": "\n\n".join(block for block in blocks if block).strip(),
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
        }

    def _build_builtin_memory_context_block(
        self,
        *,
        source: SessionSource,
    ) -> str:
        runner_config = getattr(self.runner, "config", None)
        memory_cfg = getattr(runner_config, "memory", None)
        if not memory_cfg and callable(self._raw_config_loader):
            raw_cfg = self._load_raw_config()
            memory_cfg = raw_cfg.get("memory", {}) if isinstance(raw_cfg, dict) else {}

        def _cfg_get(name: str, default: Any) -> Any:
            if isinstance(memory_cfg, dict):
                return memory_cfg.get(name, default)
            return getattr(memory_cfg, name, default)

        memory_enabled = bool(_cfg_get("memory_enabled", False))
        user_profile_enabled = bool(_cfg_get("user_profile_enabled", False))
        if not memory_enabled and not user_profile_enabled:
            return ""

        try:
            from tools.memory_tool import MemoryStore
        except Exception:
            logger.debug("Could not import built-in memory store for orchestrator", exc_info=True)
            return ""

        try:
            store = MemoryStore(
                memory_char_limit=int(_cfg_get("memory_char_limit", 2200) or 2200),
                user_char_limit=int(_cfg_get("user_char_limit", 1375) or 1375),
                chat_char_limit=int(
                    _cfg_get("chat_char_limit", _cfg_get("memory_char_limit", 2200)) or 2200
                ),
                platform=getattr(getattr(source, "platform", None), "value", "") if source else "",
                user_id=getattr(source, "user_id", None) if source else None,
                user_name=getattr(source, "user_name", None) if source else None,
                chat_id=getattr(source, "chat_id", None) if source else None,
                chat_type=getattr(source, "chat_type", None) if source else None,
                thread_id=getattr(source, "thread_id", None) if source else None,
            )
            store.load_from_disk()
        except Exception:
            logger.debug("Could not load built-in memory store for orchestrator", exc_info=True)
            return ""

        blocks: list[str] = []
        if memory_enabled:
            mem_block = store.format_for_system_prompt("memory")
            if mem_block:
                blocks.append(mem_block)
            chat_block = store.format_for_system_prompt("chat")
            if chat_block:
                blocks.append(chat_block)
        if user_profile_enabled:
            user_block = store.format_for_system_prompt("user")
            if user_block:
                blocks.append(user_block)
        return "\n\n".join(block for block in blocks if block).strip()

    def _build_provider_memory_context_snapshot(
        self,
        *,
        query: str,
        source: SessionSource,
        session_key: str,
        session_id: str = "",
        turn_number: int = 1,
    ) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = {
            "configured_provider": "",
            "provider_available": False,
            "prefetch_attempted": False,
            "provider_error": "",
            "provider_details": [],
            "provider_merged_content": "",
            "provider_block": "",
        }
        if not query:
            return snapshot

        runner_config = getattr(self.runner, "config", None)
        memory_cfg = getattr(runner_config, "memory", None)
        if not memory_cfg and callable(self._raw_config_loader):
            raw_cfg = self._load_raw_config()
            memory_cfg = raw_cfg.get("memory", {}) if isinstance(raw_cfg, dict) else {}
        if isinstance(memory_cfg, dict):
            provider_name = str(memory_cfg.get("provider", "") or "").strip()
        else:
            provider_name = str(getattr(memory_cfg, "provider", "") or "").strip()
        snapshot["configured_provider"] = provider_name
        if not provider_name:
            return snapshot

        try:
            from agent.memory_manager import MemoryManager, build_memory_context_block
            from hermes_constants import get_hermes_home
            from plugins.memory import load_memory_provider
        except Exception:
            logger.debug("Could not import memory provider helpers for orchestrator", exc_info=True)
            snapshot["provider_error"] = "import_failed"
            return snapshot

        manager = MemoryManager()
        provider = load_memory_provider(provider_name)
        if not provider:
            snapshot["provider_error"] = "provider_not_loaded"
            return snapshot
        snapshot["provider_available"] = bool(provider.is_available())
        if not snapshot["provider_available"]:
            snapshot["provider_error"] = "provider_unavailable"
            return snapshot

        platform_name = getattr(getattr(source, "platform", None), "value", "") if source else ""
        effective_session_id = str(session_id or "").strip() or self.orchestrator_internal_session_id(session_key)
        init_kwargs = {
            "platform": platform_name,
            "hermes_home": str(get_hermes_home()),
            "agent_context": "primary",
            "chat_id": getattr(source, "chat_id", None) if source else None,
            "chat_type": getattr(source, "chat_type", None) if source else None,
            "thread_id": getattr(source, "thread_id", None) if source else None,
            "gateway_session_key": session_key,
        }
        session_title_getter = getattr(self.host, "get_session_title", None)
        if callable(session_title_getter) and effective_session_id:
            try:
                session_title = session_title_getter(effective_session_id)
                if session_title:
                    init_kwargs["session_title"] = session_title
            except Exception:
                logger.debug("Could not load orchestrator session title for memory prefetch", exc_info=True)
        if source is not None and getattr(source, "user_id", None):
            init_kwargs["user_id"] = getattr(source, "user_id", None)
        try:
            from hermes_cli.profiles import get_active_profile_name

            profile = get_active_profile_name()
            init_kwargs["agent_identity"] = profile
            init_kwargs["agent_workspace"] = "hermes"
        except Exception:
            pass

        try:
            manager.add_provider(provider)
            manager.initialize_all(session_id=effective_session_id, **init_kwargs)
            manager.on_turn_start(turn_number, query, platform=platform_name)
            snapshot["prefetch_attempted"] = True
            details: List[Dict[str, Any]] = []
            prefetch_with_details = getattr(manager, "prefetch_all_with_details", None)
            if callable(prefetch_with_details):
                raw_details = prefetch_with_details(query, session_id=effective_session_id)
                if isinstance(raw_details, list):
                    details = [detail for detail in raw_details if isinstance(detail, dict)]
            else:
                prefetched = str(
                    manager.prefetch_all(query, session_id=effective_session_id) or ""
                ).strip()
                if prefetched:
                    detail: Dict[str, Any] = {
                        "provider": str(getattr(provider, "name", "") or provider_name or "unknown"),
                        "content": prefetched,
                    }
                    debug_getter = getattr(provider, "get_last_prefetch_debug_info", None)
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
            return snapshot
        except Exception as exc:
            logger.debug("Orchestrator memory prefetch failed", exc_info=True)
            snapshot["provider_error"] = str(exc) or exc.__class__.__name__
            return snapshot
        finally:
            try:
                manager.shutdown_all()
            except Exception:
                logger.debug("Orchestrator memory shutdown failed", exc_info=True)

    @staticmethod
    def build_system_prompt() -> str:
        return (
            "You are Hermes Gateway's session orchestrator.\n"
            "You are the light controller, not the heavy worker.\n"
            "Your job is to decide ONE of these outcomes for each turn:\n"
            "1. Give a short direct reply for a trivial request.\n"
            "2. Hand the turn off to a child task for deeper work.\n"
            "3. Ignore the message (allowed mainly in group chats).\n\n"
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
            "Rules:\n"
            "- Treat conversation_history and the injected routing context as authoritative context for routing. Reuse that context before guessing.\n"
            "- Always include a top-level complexity_score from 0 to 100.\n"
            "- Use action=reply only for the simplest direct replies: very short greetings/check-ins, or questions asking about the current full-agent/child status.\n"
            "- Use action=spawn for any non-trivial work.\n"
            "- Do not decide or mention model/provider/runtime details. Full-agent routing is handled elsewhere.\n"
            "- Do not recommend, select, or mention toolsets. Child agents always use Hermes' normal full-agent tool behavior.\n"
            "- The system routes spawned work to L1 for scores 0-16 and to L2 for scores 17-100, so score honestly.\n"
            "- If the user asks about progress, status, what is running, whether you are stuck, or what the child just did, DO NOT spawn a new child. Use action=reply and answer from child_board or full_agent_status.\n"
            "- If there is already an active child and the user sends a short status check-in such as '你在吗', '还在吗', or '有结果吗', use action=reply with the current child status. Do not spawn.\n"
            "- If child_board already shows an active child for the same or very similar goal, DO NOT spawn a duplicate child. Reply with the existing child status.\n"
            "- If child_board shows an active child and you still choose action=spawn for additional work, include payload.handoff_reply as a short user-facing acknowledgement describing the current task and what will be handled next.\n"
            "- If there is no active child already running, omit payload.handoff_reply for spawn.\n"
            "- If this turn is clearly a follow-up to the same topic as a prior high-complexity turn in this same chat context, do not score it below that prior high score.\n"
            "- Requests to analyze a project, repository, repo structure, codebase, files, directories, architecture, implementation, bug, trace, or investigation are NOT trivial. Hand them off even if the message is short.\n"
            "- Requests involving image generation, TTS, or STT are not automatically high complexity. If the work is otherwise straightforward, keep the complexity_score low enough to avoid over-escalating just because it is media-related.\n"
            "- If the user corrects you, says you got a fact wrong, revises a durable preference/identity/workflow detail, or says some version of 'not X, it is Y', do NOT use action=reply. Use action=spawn so the full agent can reconcile the correction and update memory if needed.\n"
            "- If the user says 'your project', 'this project', 'the repo', or similar in this gateway/workspace context, interpret that as the current active workspace/repository unless the user clearly points to some other external project.\n"
            "- Do not turn workspace ambiguity into a trivial direct reply. If analysis of the current workspace is plausible, hand the turn off.\n"
            "- If history shows that a similar direct reply already failed or confused the user, prefer handing the task off instead of asking another shallow clarification.\n"
            "- Do NOT use action=reply for identity questions, capability questions, explanations, factual questions, thanks, confirmations, or ordinary conversation beyond a minimal greeting/check-in.\n"
            "- If the answer needs current, live, or externally verified information (for example weather, latest news, current prices, scores, schedules, or anything that should be checked online), hand the turn off even if the user message is short.\n"
            "- If the turn is not one of those simplest direct-reply cases, prefer action=spawn.\n"
            "- In DMs, prefer replying instead of ignoring.\n"
            "- In groups, default to action=ignore unless the message is clearly related to the agent in context.\n"
            "- Group messages are clearly related to the agent only when at least one of these is true: trigger_reason shows an explicit bot trigger, child_board/full_agent_status shows the user is asking about active agent work, the recent context makes it explicit that the user is continuing a thread with the agent, or prefetched group memory explicitly defines that exact short message as a fixed trigger/keyword/command for Hermes.\n"
            "- A group message being interesting, answerable, technical, or urgent is NOT enough by itself. If it is ambiguous whether the user is talking to the agent or to other humans in the group, use action=ignore.\n"
            "- The payload must stay minimal. For reply use payload.text. For spawn use payload.goal and only add payload.handoff_reply when an active child is already running and the user should see an immediate acknowledgement.\n\n"
            "Examples:\n"
            '{"action":"reply","payload":{"text":"在，我在。"},"complexity_score":3}\n'
            '{"action":"reply","payload":{"text":"Still working on the repo analysis. Current step: iteration 2. Current tool: terminal. Latest progress: inspecting tests."},"complexity_score":12}\n'
            '{"action":"reply","payload":{"text":"I already have that background task running. Still working on: analyze repo. Latest progress: tracing gateway orchestrator code."},"complexity_score":14}\n'
            '{"action":"reply","payload":{"text":"Still working on: query latest earthquake. Current tool: terminal. Latest progress: checking live USGS data."},"complexity_score":10}\n'
            '{"action":"spawn","payload":{"goal":"Analyze the current workspace project structure and summarize the main directories and modules"},"complexity_score":34}\n'
            '{"action":"spawn","payload":{"goal":"Review the new follow-up request after the current repo analysis finishes","handoff_reply":"我先继续把当前的仓库分析做完，等会接着帮你看这个新问题。"},"complexity_score":41}\n'
            '{"action":"spawn","payload":{"goal":"Answer who Hermes Gateway is and what it can do"},"complexity_score":22}\n'
            '{"action":"spawn","payload":{"goal":"Check the current weather and reply briefly"},"complexity_score":18}\n'
            '{"action":"spawn","payload":{"goal":"Generate an image for the user based on their prompt"},"complexity_score":12}\n'
            '{"action":"spawn","payload":{"goal":"Convert the provided text to speech and return the audio"},"complexity_score":11}\n'
            '{"action":"spawn","payload":{"goal":"Transcribe the uploaded audio clip to text"},"complexity_score":10}\n'
            '{"action":"spawn","payload":{"goal":"Handle the user correction, fix the answer, and update memory if needed: 不是，我是后端，不是前端"},"complexity_score":24}\n'
            '{"action":"spawn","payload":{"goal":"Deeply analyze the repository and implement the requested feature"},"complexity_score":88}\n'
            '{"action":"ignore","payload":{},"complexity_score":0}\n'
            '{"action":"ignore","payload":{"cancel_children":[]},"complexity_score":0}'
        )

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
            if role == "session_meta":
                try:
                    canonical = self.canonicalize_history_decision(
                        parse_orchestrator_decision(raw_content)
                    )
                except Exception:
                    continue
                if canonical:
                    seeded.append({"role": "assistant", "content": canonical})
                continue
            if role not in {"user", "assistant"}:
                continue
            content = self.trim_status_text(raw_content, max_len=max_content_len)
            if content:
                seeded.append({"role": role, "content": content})
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
    ) -> List[Dict[str, str]]:
        history = list(compact_history or [])
        if not history or max_messages <= 0:
            return history

        stable_limit = min(_ORCHESTRATOR_ROUTING_STABLE_PREFIX_MESSAGES, max_messages)
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
        self.seed_routing_history(session, history)
        trimmed_user = self.trim_status_text(user_message, max_len=220)
        if trimmed_user:
            session.append_routing_history_entry(role="user", content=trimmed_user)

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
            assistant_content = self.canonicalize_history_decision(assistant_decision)

        if assistant_content:
            session.append_routing_history_entry(
                role="assistant",
                content=assistant_content,
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

        def _normalize_assistant_content(raw_text: str) -> str:
            text = cls.trim_status_text(raw_text, max_len=max_content_len)
            if not text:
                return ""
            legacy_routing = cls.canonicalize_routing_summary_text(
                text,
                max_len=max_content_len,
            )
            if legacy_routing:
                return legacy_routing
            try:
                decision = parse_orchestrator_decision(text)
                return cls.canonicalize_history_decision(decision)
            except Exception:
                pass
            return cls.canonicalize_reply_history_json(
                strip_orchestrator_complexity_marker(text),
                complexity_score=extract_orchestrator_complexity_marker(text),
                max_len=max_content_len,
            )

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
                    "content": _normalize_assistant_content(
                        f"Tool activity summary: used {', '.join(pending_tools[:6])}."
                    ),
                }
            )
            pending_tools.clear()
            pending_seen.clear()

        def _merge_consecutive_role(existing: List[Dict[str, str]], role: str, content: str) -> None:
            """Append *content* under *role*, merging with the last entry if it
            has the same role so that the output always alternates user/assistant."""
            if existing and existing[-1]["role"] == role:
                existing[-1]["content"] = existing[-1]["content"] + "\n" + content
            else:
                existing.append({"role": role, "content": content})

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
            normalized_content = (
                _normalize_assistant_content(content)
                if role == "assistant"
                else cls.trim_status_text(content, max_len=max_content_len)
            )
            if not normalized_content:
                continue
            _merge_consecutive_role(compact_history, role, normalized_content)

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
        return self.build_turn_user_context(
            user_message=user_message,
            source=source,
            session_entry=session_entry,
            platform_turn=platform_turn,
            session=session,
            event_metadata=event_metadata,
            memory_context_block=memory_context_block,
        )

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
        platform_name = getattr(getattr(source, "platform", None), "value", "")
        board = session.child_board()
        running_agent_status = self.running_agent_snapshot(
            getattr(session_entry, "session_key", "") or session.session_key
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
                session_context["session_title"] = session_title_getter(session_context["session_id"]) or ""
            except Exception:
                logger.debug("Could not load orchestrator session title for prompt payload", exc_info=True)
        turn_payload = {
            "platform": platform_name,
            "chat_type": getattr(source, "chat_type", ""),
            "session_context": session_context,
            "group_signal": {
                "trigger_reason": signals["trigger_reason"],
                "recent_group_context": signals["recent_group_context"],
            },
            "dynamic_disabled_skills": list(platform_turn.dynamic_disabled_skills or []),
            "full_agent_status": running_agent_status,
            "child_board": board,
        }
        parts: list[str] = [
            "[Context note: Current gateway turn context. The following metadata is not user-authored; use it only for routing and decision-making.]\n"
            + json.dumps(turn_payload, ensure_ascii=False)
        ]
        if memory_context_block:
            parts.append(
                "[Context note: Prefetched memory context for routing only. The following block is not user-authored.]\n"
                + str(memory_context_block).strip()
            )
        routing_prompt = str(getattr(platform_turn, "routing_prompt", "") or "").strip()
        if routing_prompt:
            parts.append(routing_prompt)
        return "\n\n".join(part for part in parts if part).strip()

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
