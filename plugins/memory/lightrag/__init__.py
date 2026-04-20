"""LightRAG memory provider plugin.

REST-only integration for LightRAG Server. Supports:
- automatic recall via ``/query/data``
- optional explicit tools for structured search and sourced answers
- batched append-only writes via ``/documents/text``
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from plugins.memory.lightrag.fact_envelope import (
    FactEnvelope,
    build_fact_file_source,
    extract_fact_content,
    parse_fact_file_source,
    render_fact_envelope,
)
from plugins.memory.lightrag.query_lanes import classify_query
from tools.memory_tool import validate_atomic_memory_entry
from tools.registry import tool_error

logger = logging.getLogger(__name__)

try:
    import httpx
except Exception:  # pragma: no cover - exercised via is_available()
    httpx = None


_VALID_MEMORY_MODES = {"context", "tools", "hybrid"}
_VALID_QUERY_MODES = {"local", "global", "hybrid", "naive", "mix", "bypass"}
_WORKSPACE_RE = re.compile(r"[^a-zA-Z0-9_]")
_MIN_QUERY_LEN = 3
_EXPLICIT_MEMORY_FILE_ROOT = "hermes_explicit_memory"
_SEP = "<SEP>"


LIGHTRAG_SEARCH_SCHEMA = {
    "name": "lightrag_search",
    "description": (
        "Run structured retrieval against LightRAG and return raw entities, "
        "relationships, chunks, references, and retrieval metadata. Prefer the "
        "already injected LightRAG recall first. Use this when you need deeper "
        "retrieval, extra source detail, or explicit structure. Do not use it as "
        "the main conversation model."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "mode": {
                "type": "string",
                "enum": sorted(_VALID_QUERY_MODES),
                "description": "Retrieval mode. Defaults to the provider config.",
            },
            "top_k": {
                "type": "integer",
                "description": "Max entities/relationships to retrieve.",
            },
            "include_chunk_content": {
                "type": "boolean",
                "description": "Include chunk text in returned references when available.",
            },
        },
        "required": ["query"],
    },
}


LIGHTRAG_ANSWER_SCHEMA = {
    "name": "lightrag_answer",
    "description": (
        "Ask LightRAG to synthesize a sourced answer from retrieved context. "
        "Prefer injected LightRAG recall first, then lightrag_search if "
        "structured inspection is enough. Use this only when you need a deeper "
        "LightRAG-generated answer with citations. Do not treat LightRAG as the "
        "main chat model."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Question to answer."},
            "mode": {
                "type": "string",
                "enum": sorted(_VALID_QUERY_MODES),
                "description": "Retrieval mode. Defaults to the provider config.",
            },
            "top_k": {
                "type": "integer",
                "description": "Max entities/relationships to retrieve.",
            },
            "response_type": {
                "type": "string",
                "description": "Preferred answer format, e.g. 'Bullet Points'.",
            },
            "include_references": {
                "type": "boolean",
                "description": "Whether to include references in the response.",
            },
        },
        "required": ["query"],
    },
}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _as_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _as_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _sanitize_workspace(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return _WORKSPACE_RE.sub("_", raw)


def _derive_workspace(agent_identity: str | None = None) -> str:
    identity = str(agent_identity or "").strip()
    if not identity or identity == "default":
        return "hermes_default"
    return f"hermes_{_sanitize_workspace(identity)}"


def _user_workspace(platform: str, user_id: str) -> str:
    return f"{_sanitize_workspace(platform)}_user_{_sanitize_workspace(user_id)}"


def _chat_workspace(platform: str, chat_type: str, chat_id: str, thread_id: str = "") -> str:
    base = f"{_sanitize_workspace(platform)}_{_sanitize_workspace(chat_type or 'chat')}_{_sanitize_workspace(chat_id)}"
    if thread_id:
        return f"{base}__{_sanitize_workspace(thread_id)}"
    return base


def _load_napcat_authorized_group_ids(hermes_home: str, session_id: str) -> list[str]:
    if not hermes_home or not session_id:
        return []
    auth_path = Path(hermes_home) / "gateway" / "napcat_cross_session_auth.json"
    if not auth_path.exists():
        return []
    try:
        raw = json.loads(auth_path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.debug("Failed to parse %s", auth_path, exc_info=True)
        return []
    groups = ((raw.get(str(session_id)) or {}).get("groups") or {})
    if not isinstance(groups, dict):
        return []
    result = []
    for group_id in groups.keys():
        normalized = _sanitize_workspace(str(group_id))
        if normalized:
            result.append(normalized)
    return sorted(dict.fromkeys(result))


def _load_lightrag_config(
    hermes_home: str | None = None,
    *,
    agent_identity: str | None = None,
) -> dict[str, Any]:
    from hermes_constants import get_hermes_home

    config = {
        "endpoint": str(os.environ.get("LIGHTRAG_ENDPOINT", "")).strip(),
        "api_key": str(os.environ.get("LIGHTRAG_API_KEY", "")).strip(),
        "workspace": str(os.environ.get("LIGHTRAG_WORKSPACE", "")).strip(),
        "memory_mode": "hybrid",
        "query_mode": "mix",
        "prefetch_top_k": 5,
        "prefetch_max_chars": 2400,
        "write_batch_turns": 6,
        "write_on_session_end": True,
        "response_type": "Bullet Points",
        "timeout_secs": 10.0,
    }

    base_dir = Path(hermes_home) if hermes_home else get_hermes_home()
    config_path = base_dir / "lightrag.json"
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                config.update({k: v for k, v in raw.items() if v is not None and v != ""})
        except Exception:
            logger.debug("Failed to parse %s", config_path, exc_info=True)

    config["endpoint"] = str(config.get("endpoint", "")).strip().rstrip("/")
    config["api_key"] = str(config.get("api_key", "")).strip()
    config["workspace"] = _sanitize_workspace(str(config.get("workspace", "")).strip()) or _derive_workspace(agent_identity)

    memory_mode = str(config.get("memory_mode", "hybrid")).strip().lower()
    config["memory_mode"] = memory_mode if memory_mode in _VALID_MEMORY_MODES else "hybrid"

    query_mode = str(config.get("query_mode", "mix")).strip().lower()
    config["query_mode"] = query_mode if query_mode in _VALID_QUERY_MODES else "mix"

    config["prefetch_top_k"] = _as_int(config.get("prefetch_top_k"), 5, minimum=1, maximum=20)
    config["prefetch_max_chars"] = _as_int(config.get("prefetch_max_chars"), 2400, minimum=400, maximum=12000)
    config["write_batch_turns"] = _as_int(config.get("write_batch_turns"), 6, minimum=1, maximum=50)
    config["write_on_session_end"] = _as_bool(config.get("write_on_session_end"), True)
    response_type = str(config.get("response_type", "Bullet Points")).strip()
    config["response_type"] = response_type or "Bullet Points"
    config["timeout_secs"] = _as_float(config.get("timeout_secs"), 10.0, minimum=1.0, maximum=60.0)
    return config


def _clip_text(text: Any, limit: int = 220) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def _line_limit_join(title: str, lines: list[str], *, out: list[str], max_chars: int) -> None:
    if not lines:
        return
    candidate = "\n".join(out + ([title] if title else []) + lines)
    if len(candidate) > max_chars and not out:
        candidate = candidate[:max_chars].rstrip()
    if len(candidate) <= max_chars:
        if title:
            out.append(title)
        out.extend(lines)
        return

    kept: list[str] = []
    for line in lines:
        test = "\n".join(out + ([title] if title else []) + kept + [line])
        if len(test) > max_chars:
            break
        kept.append(line)
    if kept:
        if title:
            out.append(title)
        out.extend(kept)


def _format_recall_block(data: dict[str, Any], max_chars: int) -> str:
    entities = data.get("entities") or []
    relationships = data.get("relationships") or []
    chunks = data.get("chunks") or []
    references = data.get("references") or []

    fact_lines: list[str] = []
    seen_facts: set[str] = set()
    for entity in entities[:6]:
        if not isinstance(entity, dict):
            continue
        name = _clip_text(entity.get("entity_name") or entity.get("name") or "Entity", 60)
        entity_type = _clip_text(entity.get("entity_type") or entity.get("type") or "", 24)
        desc = _clip_text(entity.get("description") or entity.get("content") or "", 140)
        bits = [name]
        if entity_type:
            bits.append(f"({entity_type})")
        if desc:
            bits.append(f": {desc}")
        line = "".join(bits)
        if line not in seen_facts:
            fact_lines.append(f"- {line}")
            seen_facts.add(line)

    for chunk in chunks[:4]:
        if not isinstance(chunk, dict):
            continue
        content = extract_fact_content(chunk.get("content") or "")
        content = _clip_text(content, 160)
        if not content or content in seen_facts:
            continue
        fact_lines.append(f"- Chunk: {content}")
        seen_facts.add(content)

    relation_lines: list[str] = []
    seen_relations: set[str] = set()
    for rel in relationships[:6]:
        if not isinstance(rel, dict):
            continue
        src = _clip_text(rel.get("src_id") or rel.get("source") or "?", 50)
        tgt = _clip_text(rel.get("tgt_id") or rel.get("target") or "?", 50)
        desc = _clip_text(rel.get("description") or rel.get("keywords") or "", 120)
        line = f"- {src} -> {tgt}"
        if desc:
            line += f": {desc}"
        if line not in seen_relations:
            relation_lines.append(line)
            seen_relations.add(line)

    source_lines: list[str] = []
    seen_sources: set[str] = set()
    for ref in references[:8]:
        if not isinstance(ref, dict):
            continue
        ref_id = _clip_text(ref.get("reference_id") or "?", 24)
        file_path = _clip_text(ref.get("file_path") or "unknown", 120)
        line = f"- [{ref_id}] {file_path}"
        if line not in seen_sources:
            source_lines.append(line)
            seen_sources.add(line)

    if not fact_lines and not relation_lines and not source_lines:
        return ""

    out = ["## LightRAG Memory"]
    _line_limit_join("### Facts", fact_lines, out=out, max_chars=max_chars)
    _line_limit_join("### Relationships", relation_lines, out=out, max_chars=max_chars)
    _line_limit_join("### Sources", source_lines, out=out, max_chars=max_chars)
    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def _workspace_block_label(workspace: str, block: str) -> str:
    cleaned_block = str(block or "").strip()
    if not cleaned_block:
        return ""
    lines = cleaned_block.splitlines()
    if lines and lines[0].startswith("## LightRAG Memory"):
        lines[0] = f"## LightRAG Memory [{workspace}]"
    return "\n".join(lines)


def _retitle_block(block: str, title: str) -> str:
    cleaned_block = str(block or "").strip()
    if not cleaned_block:
        return ""
    lines = cleaned_block.splitlines()
    if lines and lines[0].startswith("## "):
        lines[0] = title
    else:
        lines.insert(0, title)
    return "\n".join(lines)


def _split_sep_values(value: Any) -> list[str]:
    parts = []
    for raw in str(value or "").split(_SEP):
        cleaned = raw.strip()
        if cleaned:
            parts.append(cleaned)
    return parts


def _session_scope_prefix(scope: str) -> str:
    return f"hermes:lightrag:{scope}:"


def _path_matches_scope(path: str, scope: str) -> bool:
    metadata = parse_fact_file_source(path)
    if metadata:
        subject = metadata.get("subject") or metadata.get("subject_scope") or ""
        source = metadata.get("source") or metadata.get("source_scope") or ""
        return scope in {subject, source}
    return path.startswith(f"{_EXPLICIT_MEMORY_FILE_ROOT}/{scope}/") or path.startswith(
        _session_scope_prefix(scope)
    )


def _item_matches_scopes(item: dict[str, Any], scopes: list[str]) -> bool:
    paths = _split_sep_values(item.get("file_path") or "")
    if not paths:
        return False
    return all(any(_path_matches_scope(path, scope) for scope in scopes) for path in paths)


class LightRAGMemoryProvider(MemoryProvider):
    """LightRAG memory provider using the server REST API."""

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._endpoint = ""
        self._api_key = ""
        self._workspace = ""
        self._hermes_home = ""
        self._platform = ""
        self._user_id = ""
        self._chat_id = ""
        self._chat_type = ""
        self._thread_id = ""
        self._session_id = ""
        self._agent_context = "primary"
        self._memory_mode = "hybrid"
        self._query_mode = "mix"
        self._prefetch_top_k = 5
        self._prefetch_max_chars = 2400
        self._write_batch_turns = 6
        self._write_on_session_end = True
        self._response_type = "Bullet Points"
        self._timeout_secs = 10.0
        self._last_prefetch_debug: dict[str, Any] = {}
        self._turn_buffer: list[dict[str, str]] = []
        self._buffer_lock = threading.Lock()
        self._flush_thread: Optional[threading.Thread] = None
        self._memory_write_thread: Optional[threading.Thread] = None
        self._batch_sequence = 0
        self._prefetch_snapshot_dirty = False

    @property
    def name(self) -> str:
        return "lightrag"

    def is_available(self) -> bool:
        cfg = _load_lightrag_config()
        return bool(httpx is not None and cfg.get("endpoint"))

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "endpoint",
                "description": "LightRAG server endpoint",
                "required": True,
            },
            {
                "key": "api_key",
                "description": "LightRAG API key (optional)",
                "secret": True,
                "env_var": "LIGHTRAG_API_KEY",
            },
            {
                "key": "workspace",
                "description": "Workspace name (blank = derive from Hermes profile)",
            },
            {
                "key": "memory_mode",
                "description": "Memory mode",
                "default": "hybrid",
                "choices": ["hybrid", "context", "tools"],
            },
            {
                "key": "query_mode",
                "description": "Default LightRAG query mode",
                "default": "mix",
                "choices": sorted(_VALID_QUERY_MODES),
            },
            {
                "key": "prefetch_top_k",
                "description": "Recall top_k for automatic prefetch",
                "default": "5",
            },
            {
                "key": "prefetch_max_chars",
                "description": "Max characters for injected recall",
                "default": "2400",
            },
            {
                "key": "write_batch_turns",
                "description": "Turns per batched document write",
                "default": "6",
            },
            {
                "key": "write_on_session_end",
                "description": "Flush remaining turns on session end",
                "default": "true",
                "choices": ["true", "false"],
            },
            {
                "key": "response_type",
                "description": "Default LightRAG answer format",
                "default": "Bullet Points",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        config_path = Path(hermes_home) / "lightrag.json"
        existing = {}
        if config_path.exists():
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    existing = raw
            except Exception:
                existing = {}
        merged = {**existing, **values}
        workspace = _sanitize_workspace(str(merged.get("workspace", "")).strip())
        if workspace:
            merged["workspace"] = workspace
        config_path.write_text(
            json.dumps(merged, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def initialize(self, session_id: str, **kwargs) -> None:
        self._config = _load_lightrag_config(
            kwargs.get("hermes_home"),
            agent_identity=kwargs.get("agent_identity"),
        )
        self._endpoint = self._config.get("endpoint", "")
        self._api_key = self._config.get("api_key", "")
        self._workspace = self._config.get("workspace", "")
        self._hermes_home = str(kwargs.get("hermes_home") or "")
        self._platform = str(kwargs.get("platform") or "").strip().lower()
        self._user_id = _sanitize_workspace(str(kwargs.get("user_id") or ""))
        self._chat_id = _sanitize_workspace(str(kwargs.get("chat_id") or ""))
        self._chat_type = str(kwargs.get("chat_type") or "").strip().lower()
        self._thread_id = _sanitize_workspace(str(kwargs.get("thread_id") or ""))
        self._session_id = str(session_id or "")
        self._agent_context = str(kwargs.get("agent_context") or "primary").strip().lower()
        self._memory_mode = self._config.get("memory_mode", "hybrid")
        self._query_mode = self._config.get("query_mode", "mix")
        self._prefetch_top_k = int(self._config.get("prefetch_top_k", 5))
        self._prefetch_max_chars = int(self._config.get("prefetch_max_chars", 2400))
        self._write_batch_turns = int(self._config.get("write_batch_turns", 6))
        self._write_on_session_end = bool(self._config.get("write_on_session_end", True))
        self._response_type = self._config.get("response_type", "Bullet Points")
        self._timeout_secs = float(self._config.get("timeout_secs", 10.0))

    def system_prompt_block(self) -> str:
        if not self._endpoint:
            return ""
        logical_scopes = ", ".join(self._read_workspaces()) or "none"
        lines = [
            "# LightRAG Memory",
            f"Active workspace: {self._workspace}. Logical scopes: {logical_scopes}. Mode: {self._memory_mode}.",
        ]
        if self._memory_mode in {"context", "hybrid"}:
            lines.append("Relevant recall may be injected automatically before a turn.")
            lines.append("Injected LightRAG recall is read-only context for the current turn; update durable memory through the Hermes memory tool instead.")
        if self._memory_mode in {"tools", "hybrid"} and getattr(self, "_agent_context", "primary") != "orchestrator":
            lines.append("Use lightrag_search for deeper retrieval and lightrag_answer only when you need a sourced LightRAG synthesis.")
        lines.append("When writing memory, store exactly one atomic fact per entry. Split multiple facts into separate memory writes.")
        lines.append("To add, correct, or remove durable LightRAG-backed facts, use the Hermes memory tool; Hermes mirrors successful writes to LightRAG.")
        lines.append("Do not treat LightRAG as the primary conversation model.")
        return "\n".join(lines)

    def get_last_prefetch_debug_info(self) -> dict[str, Any]:
        return dict(self._last_prefetch_debug)

    def should_invalidate_cached_prefetch(self) -> bool:
        return bool(self._prefetch_snapshot_dirty)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        if self._memory_mode == "context" or getattr(self, "_agent_context", "primary") == "orchestrator":
            return []
        return [LIGHTRAG_SEARCH_SCHEMA, LIGHTRAG_ANSWER_SCHEMA]

    def _headers(self, workspace: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        workspace_name = self._workspace or str(workspace or "").strip()
        if workspace_name:
            headers["LIGHTRAG-WORKSPACE"] = workspace_name
        return headers

    def _expand_query_top_k(self, top_k: int | None, scope_count: int) -> int | None:
        if top_k is None:
            return None
        multiplier = max(1, scope_count) * 4
        return min(80, max(top_k, top_k * multiplier))

    def _active_user_scope(self) -> str:
        if self._platform == "napcat" and self._user_id:
            return _user_workspace(self._platform, self._user_id)
        return ""

    def _active_chat_scope(self) -> str:
        if self._platform == "napcat" and self._chat_type == "group" and self._chat_id:
            return _chat_workspace(self._platform, self._chat_type, self._chat_id, self._thread_id)
        return ""

    def _shared_context_scopes(self) -> list[str]:
        if self._platform != "napcat":
            return []
        user_scope = self._active_user_scope()
        return [scope for scope in self._read_workspaces() if scope and scope != user_scope]

    @staticmethod
    def _path_matches_identity_lane(path: str, active_user_scope: str) -> tuple[bool, str]:
        metadata = parse_fact_file_source(path)
        if metadata:
            subject = metadata.get("subject") or metadata.get("subject_scope") or ""
            fact_type = metadata.get("fact_type") or ""
            source_kind = metadata.get("source_kind") or ""
            target = metadata.get("target") or ""
            if subject != active_user_scope:
                return False, "subject_mismatch"
            if target != "user":
                return False, "target_mismatch"
            if fact_type not in {"user_profile", "user_preference"}:
                return False, "fact_type_mismatch"
            if source_kind not in {"dm", "system", "import"}:
                return False, "source_kind_mismatch"
            return True, ""

        if path.startswith(f"{_EXPLICIT_MEMORY_FILE_ROOT}/{active_user_scope}/user/"):
            return True, ""
        return False, "untagged_identity_path"

    @staticmethod
    def _path_matches_shared_scope(
        path: str,
        shared_scope: str,
        active_user_scope: str,
    ) -> tuple[bool, str]:
        metadata = parse_fact_file_source(path)
        if metadata:
            subject = metadata.get("subject") or metadata.get("subject_scope") or ""
            source = metadata.get("source") or metadata.get("source_scope") or ""
            target = metadata.get("target") or ""
            if shared_scope not in {subject, source}:
                return False, "scope_mismatch"
            if target == "user":
                return False, "user_target_in_shared"
            return True, ""

        if active_user_scope and path.startswith(f"{_EXPLICIT_MEMORY_FILE_ROOT}/{active_user_scope}/user/"):
            return False, "user_scope_in_shared"
        if _path_matches_scope(path, shared_scope):
            return True, ""
        return False, "scope_mismatch"

    def _path_matches_lane_scope(self, path: str, scope: str, lane: str) -> tuple[bool, str]:
        active_user_scope = self._active_user_scope()
        shared_scopes = set(self._shared_context_scopes())
        if lane == "identity":
            if not active_user_scope:
                return (_path_matches_scope(path, scope), "" if _path_matches_scope(path, scope) else "scope_mismatch")
            return self._path_matches_identity_lane(path, active_user_scope)
        if lane == "shared_context":
            if scope in shared_scopes:
                return self._path_matches_shared_scope(path, scope, active_user_scope)
            return False, "not_shared_scope"
        if lane == "mixed":
            if active_user_scope and scope == active_user_scope:
                return self._path_matches_identity_lane(path, active_user_scope)
            if scope in shared_scopes:
                return self._path_matches_shared_scope(path, scope, active_user_scope)
        matched = _path_matches_scope(path, scope)
        return matched, "" if matched else "scope_mismatch"

    def _item_matches_lane_scope(self, item: dict[str, Any], scope: str, lane: str) -> tuple[bool, str]:
        paths = _split_sep_values(item.get("file_path") or "")
        if not paths:
            return False, "missing_file_path"
        for path in paths:
            matched, reason = self._path_matches_lane_scope(path, scope, lane)
            if not matched:
                return False, reason
        return True, ""

    def _filter_query_data(
        self,
        data: dict[str, Any],
        scopes: list[str],
        *,
        lane: str = "mixed",
        log: bool = False,
    ) -> dict[str, Any]:
        if not scopes:
            return {
                "entities": [],
                "relationships": [],
                "chunks": [],
                "references": [],
            }
        filtered = {
            "entities": [],
            "relationships": [],
            "chunks": [],
            "references": [],
        }
        accepted_counts = {"entities": 0, "relationships": 0, "chunks": 0, "references": 0}
        rejected_counts = {"entities": 0, "relationships": 0, "chunks": 0, "references": 0}
        first_rejected_reason = ""
        for key in ("entities", "relationships", "chunks", "references"):
            items = data.get(key) or []
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    rejected_counts[key] += 1
                    first_rejected_reason = first_rejected_reason or "invalid_item"
                    continue
                matched = False
                reject_reason = "scope_mismatch"
                for scope in scopes:
                    matched, reject_reason = self._item_matches_lane_scope(item, scope, lane)
                    if matched:
                        break
                if matched:
                    filtered[key].append(item)
                    accepted_counts[key] += 1
                else:
                    rejected_counts[key] += 1
                    first_rejected_reason = first_rejected_reason or reject_reason
        if log:
            logger.debug(
                "LightRAG recall filtered: lane=%s user=%s chat=%s accepted=%s rejected=%s reason=%s",
                lane,
                self._active_user_scope() or "-",
                self._active_chat_scope() or "-",
                accepted_counts,
                rejected_counts,
                first_rejected_reason or "none",
            )
        return filtered

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        if httpx is None:
            raise RuntimeError("httpx is not installed")
        url = f"{self._endpoint}{path}"
        try:
            response = httpx.request(
                method,
                url,
                json=payload,
                headers=self._headers(workspace),
                timeout=self._timeout_secs,
                follow_redirects=True,
            )
            response.raise_for_status()
        except Exception as exc:
            detail = ""
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None:
                detail = f" (HTTP {status})"
            raise RuntimeError(f"LightRAG request failed{detail}: {exc}") from exc

        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError("LightRAG returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise RuntimeError("LightRAG returned a non-object response")
        return data

    def _list_documents(self, *, workspace: str) -> list[dict[str, Any]]:
        result = self._request_json("GET", "/documents", None, workspace=workspace)
        documents: list[dict[str, Any]] = []
        statuses = result.get("statuses") or {}
        if not isinstance(statuses, dict):
            return documents
        for items in statuses.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    documents.append(item)
        return documents

    def _delete_documents(self, doc_ids: list[str], *, workspace: str) -> dict[str, Any]:
        if not doc_ids:
            return {"status": "success", "message": "nothing to delete"}
        return self._request_json(
            "DELETE",
            "/documents/delete_document",
            {
                "doc_ids": doc_ids,
                "delete_file": False,
                "delete_llm_cache": True,
            },
            workspace=workspace,
        )

    @staticmethod
    def _explicit_memory_label(target: str) -> str:
        if target == "user":
            return "user"
        if target == "chat":
            return "chat"
        return "memory"

    def _explicit_memory_prefix(self, workspace: str, target: str) -> str:
        normalized_target = self._explicit_memory_label(target)
        return f"{_EXPLICIT_MEMORY_FILE_ROOT}/{workspace}/{normalized_target}/"

    def _fact_type_for_memory(self, target: str, action: str = "add") -> str:
        normalized_target = self._explicit_memory_label(target)
        if str(action or "").strip().lower() == "remove":
            return {
                "user": "user_tombstone",
                "chat": "chat_tombstone",
                "memory": "memory_tombstone",
            }.get(normalized_target, "memory_tombstone")
        return {
            "user": "user_profile",
            "chat": "chat_profile",
            "memory": "memory_note",
        }.get(normalized_target, "memory_note")

    def _default_source_kind(self) -> str:
        if self._chat_type == "dm":
            return "dm"
        if self._chat_type == "group":
            return "group"
        return self._chat_type or "system"

    def _active_source_scope(self) -> str:
        if self._platform == "napcat":
            if self._chat_type == "group" and self._chat_id:
                return _chat_workspace(self._platform, self._chat_type, self._chat_id, self._thread_id)
            if self._chat_type == "dm" and self._user_id:
                return _user_workspace(self._platform, self._user_id)
        return self._workspace or ""

    def _build_memory_envelope(
        self,
        workspace: str,
        target: str,
        entry: str,
        *,
        action: str = "add",
    ) -> FactEnvelope:
        normalized_target = self._explicit_memory_label(target)
        source_kind = self._default_source_kind()
        subject_user_id = self._user_id if normalized_target == "user" else ""
        confidence = "self_reported" if normalized_target == "user" and source_kind == "dm" else "observed"
        return FactEnvelope(
            fact_type=self._fact_type_for_memory(normalized_target, action),
            subject_scope=workspace,
            source_scope=self._active_source_scope() or workspace,
            source_kind=source_kind,
            target=normalized_target,
            content=entry,
            subject_user_id=subject_user_id,
            speaker_user_id=self._user_id,
            source_chat_id=self._chat_id,
            confidence=confidence,
        )

    def _explicit_memory_file_source(
        self,
        workspace: str,
        target: str,
        entry: str,
        *,
        action: str = "add",
    ) -> str:
        return build_fact_file_source(
            self._build_memory_envelope(workspace, target, entry, action=action)
        )

    def _render_explicit_memory_doc(
        self,
        workspace: str,
        target: str,
        entry: str,
        *,
        action: str = "add",
    ) -> str:
        return render_fact_envelope(
            self._build_memory_envelope(workspace, target, entry, action=action)
        )

    def _extract_explicit_memory_entry(self, doc: dict[str, Any]) -> str:
        summary = str(doc.get("content_summary") or "").strip()
        if not summary:
            return ""
        return extract_fact_content(summary)

    def _fact_doc_matches_target(self, file_path: str, workspace: str, target: str) -> bool:
        metadata = parse_fact_file_source(file_path)
        if not metadata:
            return False
        normalized_target = self._explicit_memory_label(target)
        subject = metadata.get("subject") or metadata.get("subject_scope") or ""
        fact_target = metadata.get("target") or ""
        fact_type = metadata.get("fact_type") or ""
        if subject != workspace or fact_target != normalized_target:
            return False
        if fact_type.endswith("_tombstone"):
            return False
        if normalized_target == "user":
            return fact_type in {"user_profile", "user_preference"}
        if normalized_target == "chat":
            return fact_type in {"chat_profile", "project_context", "shared_context"}
        return fact_type in {"memory_note", "memory_profile"}

    def _list_explicit_memory_docs(self, workspace: str, target: str) -> list[dict[str, Any]]:
        prefix = self._explicit_memory_prefix(workspace, target)
        docs = []
        for doc in self._list_documents(workspace=workspace):
            file_path = str(doc.get("file_path") or "")
            if file_path.startswith(prefix) or self._fact_doc_matches_target(file_path, workspace, target):
                docs.append(doc)
        docs.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("file_path") or "")))
        return docs

    def _match_explicit_memory_docs(
        self,
        workspace: str,
        target: str,
        needle: str,
    ) -> list[tuple[dict[str, Any], str]]:
        query = str(needle or "").strip()
        if not query:
            return []
        matches: list[tuple[dict[str, Any], str]] = []
        for doc in self._list_explicit_memory_docs(workspace, target):
            entry = self._extract_explicit_memory_entry(doc)
            haystack = entry or str(doc.get("content_summary") or "")
            if query in haystack:
                matches.append((doc, entry))
        return matches

    def _validate_memory_target_workspaces(self, target: str) -> tuple[list[str], dict[str, Any] | None]:
        workspaces = self._memory_target_workspaces(target)
        if workspaces:
            return workspaces, None
        normalized = str(target or "").strip().lower()
        if normalized == "chat":
            return [], {
                "success": False,
                "error": (
                    "Current context has no available shared chat identity. "
                    "Writes to target='chat' require a non-DM chat scope."
                ),
            }
        if normalized == "user":
            return [], {
                "success": False,
                "error": (
                    "Current context has no available user identity. "
                    "Writes to target='user' require a user-scoped workspace."
                ),
            }
        return [], {
            "success": False,
            "error": "No writable LightRAG workspace is available for this memory target.",
        }

    def _read_workspaces(self) -> list[str]:
        if self._platform != "napcat":
            return [self._workspace] if self._workspace else []

        workspaces: list[str] = []
        if self._user_id:
            workspaces.append(_user_workspace(self._platform, self._user_id))

        if self._chat_type == "group" and self._chat_id:
            workspaces.append(
                _chat_workspace(self._platform, self._chat_type, self._chat_id, self._thread_id)
            )
        elif self._chat_type == "dm":
            for group_id in _load_napcat_authorized_group_ids(self._hermes_home, self._session_id):
                workspaces.append(_chat_workspace(self._platform, "group", group_id))

        if not workspaces and self._workspace:
            workspaces.append(self._workspace)
        return list(dict.fromkeys(workspaces))

    def _sync_target_workspaces(self) -> list[str]:
        if self._platform != "napcat":
            return [self._workspace] if self._workspace else []
        if self._chat_type == "group" and self._chat_id:
            return [_chat_workspace(self._platform, self._chat_type, self._chat_id, self._thread_id)]
        if self._chat_type == "dm" and self._user_id:
            return [_user_workspace(self._platform, self._user_id)]
        return [self._workspace] if self._workspace else []

    def _memory_target_workspaces(self, target: str) -> list[str]:
        normalized = str(target or "").strip().lower()
        if self._platform != "napcat":
            if normalized == "chat" and (not self._chat_id or self._chat_type == "dm"):
                return []
            return [self._workspace] if self._workspace else []
        if normalized == "user":
            if self._user_id:
                return [_user_workspace(self._platform, self._user_id)]
            return []
        if normalized == "chat":
            if self._chat_type == "group" and self._chat_id:
                return [_chat_workspace(self._platform, self._chat_type, self._chat_id, self._thread_id)]
            return []
        return [self._workspace] if self._workspace else []

    def _read_exact_user_memory(self) -> list[str]:
        user_scope = self._active_user_scope()
        if not self._hermes_home or not user_scope:
            return []
        user_dir = Path(self._hermes_home) / _EXPLICIT_MEMORY_FILE_ROOT / user_scope / "user"
        if not user_dir.exists():
            return []
        entries: list[str] = []
        for path in sorted(user_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8").strip()
            entry = extract_fact_content(text)
            if entry:
                entries.append(entry)
        return entries

    def _render_exact_user_memory(self) -> str:
        entries = self._read_exact_user_memory()
        if not entries:
            return ""
        scope = self._active_user_scope()
        return f"## User Profile [{scope}]\n" + "\n".join(
            f"- {entry}" for entry in entries[:12]
        )

    def _read_exact_chat_memory(self) -> list[str]:
        chat_scope = self._active_chat_scope()
        if not self._hermes_home or not chat_scope:
            return []
        chat_dir = Path(self._hermes_home) / _EXPLICIT_MEMORY_FILE_ROOT / chat_scope / "chat"
        if not chat_dir.exists():
            return []
        entries: list[str] = []
        for path in sorted(chat_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8").strip()
            entry = extract_fact_content(text)
            if entry:
                entries.append(entry)
        return entries

    def _render_exact_chat_memory(self) -> str:
        entries = self._read_exact_chat_memory()
        if not entries:
            return ""
        scope = self._active_chat_scope()
        return f"## Shared Context [{scope}]\n" + "\n".join(
            f"- {entry}" for entry in entries[:12]
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._last_prefetch_debug = {}
        if self._memory_mode not in {"context", "hybrid"}:
            return ""
        cleaned = str(query or "").strip()
        if len(cleaned) < _MIN_QUERY_LEN:
            return ""

        query_plan = classify_query(cleaned)
        exact_user_block = self._render_exact_user_memory()
        exact_chat_block = self._render_exact_chat_memory()
        user_scope = self._active_user_scope()
        chat_scope = self._active_chat_scope()
        shared_scopes = self._shared_context_scopes()
        all_scopes = self._read_workspaces()
        self._last_prefetch_debug = {
            "provider": self.name,
            "query": cleaned,
            "lane": query_plan.lane,
            "query_mode": self._query_mode,
            "prefetch_top_k_config": self._prefetch_top_k,
            "prefetch_max_chars": self._prefetch_max_chars,
            "memory_mode": self._memory_mode,
            "workspace_header": self._workspace,
            "all_workspaces": list(all_scopes),
            "user_scope": user_scope,
            "chat_scope": chat_scope,
            "shared_scopes": list(shared_scopes),
            "api_called": False,
            "used_exact_user_memory": bool(exact_user_block),
            "used_exact_chat_memory": bool(exact_chat_block),
        }
        if query_plan.lane == "identity" and exact_user_block:
            self._last_prefetch_debug["strategy"] = "exact_user_memory_short_circuit"
            self._last_prefetch_debug["render_scopes"] = [user_scope] if user_scope else []
            self._prefetch_snapshot_dirty = False
            return exact_user_block

        if not all_scopes:
            self._last_prefetch_debug["strategy"] = "exact_local_memory_only"
            self._last_prefetch_debug["render_scopes"] = []
            combined_local = "\n\n".join(
                block
                for block in (exact_user_block, exact_chat_block)
                if block
            ).strip()
            self._prefetch_snapshot_dirty = False
            return combined_local

        render_scopes = list(all_scopes)
        if query_plan.lane == "identity" and user_scope:
            render_scopes = [user_scope]
        elif query_plan.lane == "shared_context" and shared_scopes:
            render_scopes = list(shared_scopes)
            if exact_chat_block and chat_scope:
                render_scopes = [scope for scope in render_scopes if scope != chat_scope]
        elif query_plan.lane == "mixed":
            render_scopes = []
            if user_scope and not exact_user_block:
                render_scopes.append(user_scope)
            render_scopes.extend(
                scope for scope in shared_scopes
                if not (exact_chat_block and chat_scope and scope == chat_scope)
            )
            if not render_scopes:
                render_scopes = list(all_scopes)

        request_payload = {
            "query": cleaned,
            "mode": self._query_mode,
            "top_k": self._expand_query_top_k(self._prefetch_top_k, len(render_scopes)),
            "include_references": True,
            "include_chunk_content": False,
            "stream": False,
        }
        self._last_prefetch_debug.update({
            "strategy": "query_data",
            "render_scopes": list(render_scopes),
            "request_payload": request_payload,
            "api_called": True,
        })
        result = self._request_json(
            "POST",
            "/query/data",
            request_payload,
        )
        data = result.get("data") or {}
        blocks: list[str] = []
        if exact_user_block and query_plan.lane == "mixed":
            blocks.append(exact_user_block)
        if exact_chat_block and query_plan.lane in {"mixed", "shared_context"}:
            blocks.append(exact_chat_block)
        for workspace in render_scopes:
            scoped_data = self._filter_query_data(data, [workspace], lane=query_plan.lane, log=True)
            block = _format_recall_block(scoped_data, self._prefetch_max_chars)
            if block:
                if user_scope and workspace == user_scope:
                    blocks.append(_retitle_block(block, f"## User Profile [{workspace}]"))
                elif workspace in shared_scopes:
                    blocks.append(_retitle_block(block, f"## Shared Context [{workspace}]"))
                else:
                    blocks.append(_workspace_block_label(workspace, block))

        combined = "\n\n".join(blocks)
        if len(combined) > self._prefetch_max_chars:
            combined = combined[: self._prefetch_max_chars].rstrip()
        self._last_prefetch_debug["result_chars"] = len(combined)
        self._last_prefetch_debug["result_block_count"] = len(blocks)
        self._prefetch_snapshot_dirty = False
        return combined

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        # No-op: LightRAG prefetch is done synchronously per turn.
        return None

    def _build_turn_batch_text(
        self,
        turns: list[dict[str, str]],
        batch_sequence: int,
        workspace: str,
    ) -> str:
        lines = [
            "LightRAG session memory batch",
            f"workspace: {workspace}",
            f"session_id: {self._session_id}",
            f"batch_sequence: {batch_sequence}",
            f"turns: {len(turns)}",
            "",
            "Conversation digest:",
        ]
        for index, turn in enumerate(turns, start=1):
            lines.append(f"{index}. User: {_clip_text(turn.get('user', ''), 280)}")
            lines.append(f"   Assistant: {_clip_text(turn.get('assistant', ''), 280)}")
        return "\n".join(lines)

    def _post_document(self, text: str, file_source: str, *, workspace: str | None = None) -> dict[str, Any]:
        return self._request_json(
            "POST",
            "/documents/text",
            {
                "text": text,
                "file_source": file_source,
            },
            workspace=workspace,
        )

    def _flush_snapshot(self, turns: list[dict[str, str]], batch_sequence: int) -> None:
        if not turns:
            return
        for workspace in self._sync_target_workspaces():
            text = self._build_turn_batch_text(turns, batch_sequence, workspace)
            file_source = f"hermes:lightrag:{workspace}:session:{self._session_id}:batch:{batch_sequence}"
            self._post_document(text, file_source, workspace=workspace)

    def _start_flush_thread(self, turns: list[dict[str, str]], batch_sequence: int) -> None:
        def _run() -> None:
            try:
                self._flush_snapshot(turns, batch_sequence)
            except Exception:
                logger.debug("LightRAG batch flush failed", exc_info=True)

        self._prefetch_snapshot_dirty = True
        self._flush_thread = threading.Thread(
            target=_run,
            name="lightrag-flush",
            daemon=True,
        )
        self._flush_thread.start()

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        user_text = str(user_content or "").strip()
        assistant_text = str(assistant_content or "").strip()
        if not user_text and not assistant_text:
            return

        with self._buffer_lock:
            self._turn_buffer.append({"user": user_text, "assistant": assistant_text})
            should_flush = len(self._turn_buffer) >= self._write_batch_turns
            snapshot: list[dict[str, str]] = []
            batch_sequence = 0
            if should_flush and not (self._flush_thread and self._flush_thread.is_alive()):
                snapshot = list(self._turn_buffer)
                self._turn_buffer.clear()
                self._batch_sequence += 1
                batch_sequence = self._batch_sequence

        if snapshot:
            self._start_flush_thread(snapshot, batch_sequence)

    def _flush_remaining_sync(self) -> None:
        flush_thread = self._flush_thread
        if flush_thread and flush_thread.is_alive():
            flush_thread.join(timeout=max(1.0, self._timeout_secs))
        with self._buffer_lock:
            if not self._turn_buffer:
                return
            snapshot = list(self._turn_buffer)
            self._turn_buffer.clear()
            self._batch_sequence += 1
            batch_sequence = self._batch_sequence
        self._prefetch_snapshot_dirty = True
        self._flush_snapshot(snapshot, batch_sequence)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._write_on_session_end:
            try:
                self._flush_remaining_sync()
            except Exception:
                logger.debug("LightRAG session-end flush failed", exc_info=True)

    def _submit_explicit_memory(self, action: str, target: str, content: str) -> None:
        normalized_action = str(action or "").strip().lower() or "add"
        normalized_target = str(target or "").strip().lower() or "memory"
        for workspace in self._memory_target_workspaces(normalized_target):
            entry = _clip_text(content, 1000)
            self._post_document(
                self._render_explicit_memory_doc(
                    workspace,
                    normalized_target,
                    entry,
                    action=normalized_action,
                ),
                self._explicit_memory_file_source(
                    workspace,
                    normalized_target,
                    entry,
                    action=normalized_action,
                ),
                workspace=workspace,
            )

    def handle_memory_write(
        self,
        action: str,
        target: str,
        *,
        content: str | None = None,
        old_text: str | None = None,
    ) -> str:
        normalized_action = str(action or "").strip().lower()
        normalized_target = str(target or "").strip().lower() or "memory"
        if normalized_target not in {"memory", "user", "chat"}:
            return tool_error(
                f"Invalid target '{target}'. Use 'memory', 'user', or 'chat'.",
                success=False,
            )

        workspaces, scope_error = self._validate_memory_target_workspaces(normalized_target)
        if scope_error:
            return json.dumps(scope_error, ensure_ascii=False)

        try:
            if normalized_action == "add":
                entry = str(content or "").strip()
                if not entry:
                    return tool_error("Content is required for 'add' action.", success=False)
                atomicity_error = validate_atomic_memory_entry(entry)
                if atomicity_error:
                    return tool_error(atomicity_error, success=False)

                for workspace in workspaces:
                    file_source = self._explicit_memory_file_source(workspace, normalized_target, entry)
                    docs = self._list_explicit_memory_docs(workspace, normalized_target)
                    if any(str(doc.get("file_path") or "") == file_source for doc in docs):
                        return json.dumps(
                            {
                                "success": True,
                                "target": normalized_target,
                                "message": "Entry already exists (no duplicate added).",
                            },
                            ensure_ascii=False,
                        )
                    self._post_document(
                        self._render_explicit_memory_doc(workspace, normalized_target, entry),
                        file_source,
                        workspace=workspace,
                    )
                return json.dumps(
                    {
                        "success": True,
                        "target": normalized_target,
                        "message": "Entry added.",
                    },
                    ensure_ascii=False,
                )

            if normalized_action == "remove":
                needle = str(old_text or "").strip()
                if not needle:
                    return tool_error("old_text is required for 'remove' action.", success=False)

                for workspace in workspaces:
                    matches = self._match_explicit_memory_docs(workspace, normalized_target, needle)
                    if not matches:
                        return json.dumps(
                            {"success": False, "error": f"No entry matched '{needle}'."},
                            ensure_ascii=False,
                        )
                    unique_entries = list(dict.fromkeys(entry for _, entry in matches))
                    if len(unique_entries) > 1:
                        return json.dumps(
                            {
                                "success": False,
                                "error": f"Multiple entries matched '{needle}'. Be more specific.",
                                "matches": [_clip_text(entry, 80) for entry in unique_entries],
                            },
                            ensure_ascii=False,
                        )
                    self._delete_documents([str(matches[0][0].get("id") or "")], workspace=workspace)
                return json.dumps(
                    {
                        "success": True,
                        "target": normalized_target,
                        "message": "Entry removed.",
                    },
                    ensure_ascii=False,
                )

            if normalized_action == "replace":
                needle = str(old_text or "").strip()
                entry = str(content or "").strip()
                if not needle:
                    return tool_error("old_text is required for 'replace' action.", success=False)
                if not entry:
                    return tool_error("content is required for 'replace' action.", success=False)
                atomicity_error = validate_atomic_memory_entry(entry)
                if atomicity_error:
                    return tool_error(atomicity_error, success=False)

                for workspace in workspaces:
                    matches = self._match_explicit_memory_docs(workspace, normalized_target, needle)
                    if not matches:
                        return json.dumps(
                            {"success": False, "error": f"No entry matched '{needle}'."},
                            ensure_ascii=False,
                        )
                    unique_entries = list(dict.fromkeys(existing for _, existing in matches))
                    if len(unique_entries) > 1:
                        return json.dumps(
                            {
                                "success": False,
                                "error": f"Multiple entries matched '{needle}'. Be more specific.",
                                "matches": [_clip_text(existing, 80) for existing in unique_entries],
                            },
                            ensure_ascii=False,
                        )
                    self._delete_documents([str(matches[0][0].get("id") or "")], workspace=workspace)
                    self._post_document(
                        self._render_explicit_memory_doc(workspace, normalized_target, entry),
                        self._explicit_memory_file_source(workspace, normalized_target, entry),
                        workspace=workspace,
                    )
                return json.dumps(
                    {
                        "success": True,
                        "target": normalized_target,
                        "message": "Entry replaced.",
                    },
                    ensure_ascii=False,
                )

            return tool_error(
                f"Unknown action '{action}'. Use: add, replace, remove",
                success=False,
            )
        except Exception as exc:
            return tool_error(f"LightRAG memory write failed: {exc}", success=False)

    def build_live_memory_snapshot(self) -> str:
        labels = [
            ("memory", "MEMORY (your personal notes)"),
            ("chat", "CHAT PROFILE (shared context for this conversation)"),
            ("user", "USER PROFILE (who the user is)"),
        ]
        blocks: list[str] = []
        for target, label in labels:
            workspaces = self._memory_target_workspaces(target)
            entries: list[str] = []
            for workspace in workspaces:
                for doc in self._list_explicit_memory_docs(workspace, target):
                    entry = self._extract_explicit_memory_entry(doc)
                    if entry and entry not in entries:
                        entries.append(entry)
            if entries:
                blocks.append(f"## Current {label}:\n" + "\n§\n".join(entries))
        return "\n\n".join(blocks)

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        if not str(content or "").strip():
            return

        def _run() -> None:
            try:
                self._submit_explicit_memory(action, target, content)
            except Exception:
                logger.debug("LightRAG explicit memory mirror failed", exc_info=True)

        self._prefetch_snapshot_dirty = True
        self._memory_write_thread = threading.Thread(
            target=_run,
            name="lightrag-memory-write",
            daemon=True,
        )
        self._memory_write_thread.start()

    def _render_filtered_answer(self, data: dict[str, Any]) -> str:
        lines: list[str] = []
        seen: set[str] = set()
        for chunk in data.get("chunks") or []:
            if not isinstance(chunk, dict):
                continue
            content = _clip_text(chunk.get("content") or "", 220)
            if content and content not in seen:
                lines.append(f"- {content}")
                seen.add(content)
            if len(lines) >= 6:
                break
        if len(lines) < 6:
            for entity in data.get("entities") or []:
                if not isinstance(entity, dict):
                    continue
                name = _clip_text(entity.get("entity_name") or entity.get("name") or "Entity", 80)
                desc = _clip_text(entity.get("description") or entity.get("content") or "", 180)
                text = f"{name}: {desc}" if desc else name
                if text and text not in seen:
                    lines.append(f"- {text}")
                    seen.add(text)
                if len(lines) >= 6:
                    break
        if not lines:
            return "No matching LightRAG data found within the active logical scopes."
        return "\n".join(lines)

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        query = str((args or {}).get("query") or "").strip()
        if len(query) < _MIN_QUERY_LEN:
            return tool_error("LightRAG query must be at least 3 characters")
        query_plan = classify_query(query)

        mode = str((args or {}).get("mode") or self._query_mode).strip().lower()
        if mode not in _VALID_QUERY_MODES:
            mode = self._query_mode

        if tool_name == "lightrag_search":
            include_chunk_content = bool((args or {}).get("include_chunk_content", False))
            top_k = args.get("top_k")
            payload = {
                "query": query,
                "mode": mode,
                "top_k": self._expand_query_top_k(top_k, len(self._read_workspaces())),
                "include_references": True,
                "include_chunk_content": include_chunk_content,
                "stream": False,
            }
            workspaces = self._read_workspaces()
            result = self._request_json("POST", "/query/data", payload)
            raw_data = result.get("data") or {}
            by_workspace: dict[str, Any] = {}
            total_counts = {"entities": 0, "relationships": 0, "chunks": 0, "references": 0}
            for workspace in workspaces:
                data = self._filter_query_data(raw_data, [workspace], lane=query_plan.lane)
                counts = {
                    "entities": len(data.get("entities") or []),
                    "relationships": len(data.get("relationships") or []),
                    "chunks": len(data.get("chunks") or []),
                    "references": len(data.get("references") or []),
                }
                by_workspace[workspace] = {
                    "status": result.get("status", "success"),
                    "message": result.get("message", ""),
                    "counts": counts,
                    "data": data,
                    "metadata": result.get("metadata") or {},
                }
            filtered_all = self._filter_query_data(raw_data, workspaces, lane=query_plan.lane)
            total_counts = {
                "entities": len(filtered_all.get("entities") or []),
                "relationships": len(filtered_all.get("relationships") or []),
                "chunks": len(filtered_all.get("chunks") or []),
                "references": len(filtered_all.get("references") or []),
            }
            response_payload = {
                "query": query,
                "mode": mode,
                "workspaces": workspaces,
                "counts": total_counts,
                "results_by_workspace": by_workspace,
            }
            if len(workspaces) == 1:
                workspace = workspaces[0]
                single = by_workspace.get(workspace) or {}
                response_payload.update(
                    {
                        "workspace": workspace,
                        "status": single.get("status", "success"),
                        "message": single.get("message", ""),
                        "data": single.get("data") or {},
                        "metadata": single.get("metadata") or {},
                    }
                )
            return json.dumps(
                response_payload,
                ensure_ascii=False,
            )

        if tool_name == "lightrag_answer":
            payload = {
                "query": query,
                "mode": mode,
                "top_k": self._expand_query_top_k(args.get("top_k"), len(self._read_workspaces())),
                "include_chunk_content": False,
                "include_references": True,
                "stream": False,
            }
            workspaces = self._read_workspaces()
            result = self._request_json("POST", "/query/data", payload)
            raw_data = result.get("data") or {}
            if len(workspaces) == 1:
                workspace = workspaces[0]
                data = self._filter_query_data(raw_data, [workspace], lane=query_plan.lane)
                references = data.get("references") or []
                return json.dumps(
                    {
                        "query": query,
                        "mode": mode,
                        "workspace": workspace,
                        "response": self._render_filtered_answer(data),
                        "references": references,
                        "reference_count": len(references),
                    },
                    ensure_ascii=False,
                )
            responses = []
            total_reference_count = 0
            for workspace in workspaces:
                data = self._filter_query_data(raw_data, [workspace], lane=query_plan.lane)
                references = data.get("references") or []
                total_reference_count += len(references)
                responses.append(
                    {
                        "workspace": workspace,
                        "response": self._render_filtered_answer(data),
                        "references": references,
                        "reference_count": len(references),
                    }
                )
            return json.dumps(
                {
                    "query": query,
                    "mode": mode,
                    "workspaces": workspaces,
                    "responses": responses,
                    "reference_count": total_reference_count,
                },
                ensure_ascii=False,
            )

        return tool_error(f"Unknown LightRAG tool: {tool_name}")

    def shutdown(self) -> None:
        try:
            self._flush_remaining_sync()
        except Exception:
            logger.debug("LightRAG shutdown flush failed", exc_info=True)
        if self._memory_write_thread and self._memory_write_thread.is_alive():
            self._memory_write_thread.join(timeout=max(1.0, self._timeout_secs))
        self._flush_thread = None
        self._memory_write_thread = None


def register(ctx) -> None:
    ctx.register_memory_provider(LightRAGMemoryProvider())
