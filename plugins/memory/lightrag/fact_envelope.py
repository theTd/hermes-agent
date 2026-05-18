from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
from typing import Any


_PATH_RE = re.compile(r"[^a-zA-Z0-9_.=-]")


@dataclass(frozen=True)
class FactEnvelope:
    fact_type: str
    subject_scope: str
    source_scope: str
    source_kind: str
    target: str
    content: str
    subject_user_id: str = ""
    speaker_user_id: str = ""
    source_chat_id: str = ""
    project_id: str = ""
    confidence: str = "observed"
    created_at: str = ""


def _safe_segment(value: str) -> str:
    return _PATH_RE.sub("_", str(value or "").strip())


def _digest(envelope: FactEnvelope) -> str:
    raw = "\0".join(
        [
            envelope.fact_type,
            envelope.subject_scope,
            envelope.source_scope,
            envelope.source_kind,
            envelope.target,
            envelope.content,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _infer_platform(envelope: FactEnvelope) -> str:
    for scope in (envelope.subject_scope, envelope.source_scope):
        cleaned = str(scope or "").strip()
        if "_" in cleaned:
            return cleaned.split("_", 1)[0]
    return "unknown"


def build_fact_file_source(envelope: FactEnvelope) -> str:
    return "/".join(
        [
            "hermes_facts",
            "v1",
            f"platform={_safe_segment(_infer_platform(envelope))}",
            f"fact_type={_safe_segment(envelope.fact_type)}",
            f"subject={_safe_segment(envelope.subject_scope)}",
            f"source={_safe_segment(envelope.source_scope)}",
            f"source_kind={_safe_segment(envelope.source_kind)}",
            f"target={_safe_segment(envelope.target)}",
            f"digest={_digest(envelope)}.md",
        ]
    )


def parse_fact_file_source(path: str) -> dict[str, str]:
    parts = str(path or "").split("/")
    if len(parts) < 3 or parts[0] != "hermes_facts" or parts[1] != "v1":
        return {}
    metadata: dict[str, str] = {}
    for part in parts[2:]:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        metadata[key] = value.removesuffix(".md")
    return metadata


def _source_label(envelope: FactEnvelope) -> str:
    if envelope.source_kind == "dm":
        peer = envelope.source_chat_id or envelope.subject_user_id or envelope.speaker_user_id
        if peer:
            return f"与 {peer} 的私聊"
        return "私聊"
    if envelope.source_kind == "group":
        if envelope.source_chat_id:
            return f"群 {envelope.source_chat_id}"
        return envelope.source_scope or "群聊"
    if envelope.source_kind == "import":
        return f"导入自 {envelope.source_scope or envelope.subject_scope}"
    if envelope.source_kind == "system":
        return "Hermes 系统"
    return envelope.source_scope or envelope.subject_scope or envelope.source_kind or "未知来源"


def render_fact_envelope(envelope: FactEnvelope) -> str:
    timestamp = envelope.created_at or datetime.now(timezone.utc).isoformat()
    header = [
        "---",
        "hermes_fact_version: 1",
        f"fact_type: {envelope.fact_type}",
        f"subject_scope: {envelope.subject_scope}",
        f"source_scope: {envelope.source_scope}",
        f"source_kind: {envelope.source_kind}",
        f"target: {envelope.target}",
    ]
    if envelope.subject_user_id:
        header.append(f"subject_user_id: {envelope.subject_user_id}")
    if envelope.speaker_user_id:
        header.append(f"speaker_user_id: {envelope.speaker_user_id}")
    if envelope.source_chat_id:
        header.append(f"source_chat_id: {envelope.source_chat_id}")
    if envelope.project_id:
        header.append(f"project_id: {envelope.project_id}")
    header.extend(
        [
            f"confidence: {envelope.confidence or 'observed'}",
            f"created_at: {timestamp}",
            "---",
            f"内容：{str(envelope.content or '').strip()}",
            f"信源：{_source_label(envelope)}",
        ]
    )
    return "\n".join(header).strip()


def extract_fact_content(text: Any) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    lines = raw.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines:
            if line.startswith("内容："):
                return line.split("：", 1)[1].strip()
            if line.startswith("content:"):
                return line.split(":", 1)[1].strip()
    if lines and lines[0].startswith("# Hermes "):
        return "\n".join(lines[1:]).strip()
    return raw
