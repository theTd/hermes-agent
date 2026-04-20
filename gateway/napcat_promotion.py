"""Branch-local helpers for NapCat model promotion."""

from __future__ import annotations

import re
from typing import Any


class NapCatPromotionSupport:
    """Encapsulate promotion helpers so the shared gateway runner stays generic."""

    L2_PROMOTION_FALLBACK_RESPONSE = "抱歉，处理遇到了问题，请稍后重试。"

    @staticmethod
    def normalize_complexity_threshold(value: Any, *, default: int = 70) -> int:
        """Normalize the L2 complexity threshold to the 0-100 scoring scale."""
        try:
            threshold = int(value)
        except (TypeError, ValueError):
            threshold = default
        return max(0, min(100, threshold))

    @staticmethod
    def extract_complexity_score(text: str) -> int | None:
        """Extract ``[[COMPLEXITY:N]]`` from a response."""
        if not text:
            return None
        match = re.search(
            r"\[\[\s*COMPLEXITY\s*:\s*(100|[1-9]?\d)\s*\]\]",
            str(text),
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None

    @classmethod
    def parse_result(cls, text: str, protocol: dict) -> str:
        """Classify an L1 result as reply, no_reply, or escalate."""
        stripped = str(text or "").strip()
        if not stripped:
            return "no_reply"

        cleaned = re.sub(
            r"\[\[\s*COMPLEXITY\s*:\s*(100|[1-9]?\d)\s*\]\]\s*",
            "",
            stripped,
            flags=re.IGNORECASE,
        ).strip()
        no_reply_marker = str(protocol.get("no_reply_marker") or "[[NO_REPLY]]").strip()
        escalate_marker = str(protocol.get("escalate_marker") or "[[ESCALATE_L2]]").strip()
        if no_reply_marker and cleaned == no_reply_marker:
            return "no_reply"
        if escalate_marker and cleaned == escalate_marker:
            return "escalate"
        return "reply"

    @classmethod
    def clip_high_score_response(cls, raw: str, protocol: dict, threshold: int) -> str:
        """Collapse high-score replies to control markers only."""
        score = cls.extract_complexity_score(raw)
        if score is None or score < cls.normalize_complexity_threshold(threshold):
            return raw

        marker_parts = [f"[[COMPLEXITY:{score}]]"]
        cleaned = str(raw or "")
        for marker_name in ("no_reply_marker", "escalate_marker"):
            marker = str(protocol.get(marker_name) or "").strip()
            if marker and marker in cleaned:
                marker_parts.append(marker)
                break
        return " ".join(marker_parts)

    @classmethod
    def extract_turn_complexity_score(cls, agent_result: dict) -> int | None:
        """Use only current-turn assistant messages when extracting complexity."""
        if not isinstance(agent_result, dict):
            return None

        messages = agent_result.get("messages") or []
        history_offset = agent_result.get("history_offset", 0)

        try:
            history_offset = int(history_offset or 0)
        except (TypeError, ValueError):
            history_offset = 0
        if history_offset < 0:
            history_offset = 0

        current_turn_messages = messages[history_offset:] if history_offset < len(messages) else []
        texts: list[str] = []

        final_response = str(agent_result.get("final_response") or "").strip()
        if final_response:
            texts.append(final_response)

        for msg in current_turn_messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            content = str(msg.get("content") or "").strip()
            if content:
                texts.append(content)

        scores = [
            score
            for score in (cls.extract_complexity_score(text) for text in texts)
            if score is not None
        ]
        if not scores:
            return None
        return max(scores)

    @staticmethod
    def inject_protocol_before_extra_prompt(
        context_prompt: str,
        extra_prompt: str,
        protocol_prompt: str,
    ) -> str:
        """Place the promotion protocol before the platform extra prompt when possible."""
        context_text = str(context_prompt or "")
        extra_text = str(extra_prompt or "")
        protocol_text = str(protocol_prompt or "").strip()
        if not protocol_text:
            return context_text
        if not extra_text or extra_text not in context_text:
            parts = [part for part in (context_text.strip(), protocol_text) if part]
            return "\n\n".join(parts)
        return context_text.replace(extra_text, f"{protocol_text}\n\n{extra_text}", 1)

    @staticmethod
    def check_l1_complexity(
        agent_result: dict,
        safeguards: dict,
    ) -> tuple[bool, str]:
        """Return ``(should_escalate, reason)`` based on L1 execution metrics."""
        if not safeguards:
            return False, ""
        messages = agent_result.get("messages") or []
        api_calls = agent_result.get("api_calls") or 0
        history_offset = agent_result.get("history_offset", 0)

        try:
            history_offset = int(history_offset or 0)
        except (TypeError, ValueError):
            history_offset = 0
        if history_offset < 0:
            history_offset = 0

        current_turn_messages = messages[history_offset:] if history_offset < len(messages) else []

        max_api = safeguards.get("l1_max_api_calls", 4)
        if max_api and api_calls > max_api:
            return True, f"api_calls={api_calls}>{max_api}"

        max_tools = safeguards.get("l1_max_tool_calls", 2)
        tool_call_count = sum(len(msg.get("tool_calls") or []) for msg in current_turn_messages)
        if max_tools and tool_call_count > max_tools:
            return True, f"tool_calls={tool_call_count}>{max_tools}"

        force_toolsets = set(safeguards.get("force_l2_toolsets") or [])
        if force_toolsets and current_turn_messages:
            try:
                from tools.registry import registry as tool_registry

                for msg in current_turn_messages:
                    for tc in msg.get("tool_calls") or []:
                        name = (tc.get("function") or {}).get("name", "")
                        if name:
                            toolset = tool_registry.get_toolset_for_tool(name)
                            if toolset and toolset in force_toolsets:
                                return True, f"force_toolset={toolset}({name})"
            except Exception:
                pass

        return False, ""

    @staticmethod
    def sanitize_response(text: str, protocol: dict, fallback: str) -> str:
        """Strip control markers from the final response text."""
        cleaned = re.sub(
            r"\[\[\s*COMPLEXITY\s*:\s*(100|[1-9]?\d)\s*\]\]\s*",
            "",
            str(text or ""),
            flags=re.IGNORECASE,
        )
        if protocol:
            for marker in protocol.values():
                if isinstance(marker, str) and marker:
                    cleaned = cleaned.replace(marker, "")
        cleaned = cleaned.strip()
        return cleaned if cleaned else fallback

    @classmethod
    def sanitize_interim_text(cls, text: str, protocol: dict | None = None) -> str:
        """Strip internal control markers/placeholders from interim commentary."""
        cleaned = cls.sanitize_response(text, protocol or {}, "")
        if re.fullmatch(r"\[\s*Sent [^\]]+\]", cleaned, flags=re.IGNORECASE):
            return ""
        return cleaned

    @classmethod
    def sanitize_transcript_messages(
        cls,
        messages: list[dict[str, Any]],
        protocol: dict,
        final_response: str,
    ) -> list[dict[str, Any]]:
        """Remove internal promotion markers from persisted assistant messages."""
        if not protocol or not messages:
            return messages

        last_visible_assistant_idx = -1
        for idx, msg in enumerate(messages):
            if msg.get("role") != "assistant":
                continue
            if msg.get("tool_calls"):
                continue
            if isinstance(msg.get("content"), str):
                last_visible_assistant_idx = idx

        sanitized: list[dict[str, Any]] = []
        for idx, msg in enumerate(messages):
            if msg.get("role") != "assistant":
                sanitized.append(msg)
                continue

            content = msg.get("content")
            if not isinstance(content, str):
                sanitized.append(msg)
                continue

            cleaned = cls.sanitize_response(content, protocol, "")
            if cleaned:
                updated = dict(msg)
                updated["content"] = cleaned
                sanitized.append(updated)
                continue

            if idx == last_visible_assistant_idx and final_response:
                updated = dict(msg)
                updated["content"] = final_response
                sanitized.append(updated)

            elif not content.strip():
                sanitized.append(msg)

        return sanitized
