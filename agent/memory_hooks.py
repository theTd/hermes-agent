"""Memory-related helpers extracted from the agent loop.

Pure functions and static helpers for memory operations: detection,
validation, observability emission, and tool dispatch.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from agent.memory_manager import build_memory_context_block


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def turn_used_tool(messages: list, start_idx: int, tool_name: str) -> bool:
    """Check whether a tool was used after the current turn began."""
    if not messages or start_idx >= len(messages):
        return False
    for msg in messages[start_idx:]:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("tool_name") or "").strip() == tool_name:
            return True
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if str((fn or {}).get("name") or "").strip() == tool_name:
                return True
    return False


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def memory_tool_result_success(result: str) -> bool:
    try:
        parsed = json.loads(result)
    except Exception:
        return False
    return bool(parsed.get("success"))


def memory_tool_unavailable(result: str) -> bool:
    try:
        parsed = json.loads(result)
    except Exception:
        return False
    return (
        parsed.get("success") is False
        and "Memory is not available" in str(parsed.get("error") or "")
    )


# ---------------------------------------------------------------------------
# Observability emitters
# ---------------------------------------------------------------------------

def emit_memory_prefetch_usage(
    query: str,
    details: list[dict[str, Any]],
    merged_content: str,
    *,
    duration_ms: float = 0.0,
    emit_event_fn: Any = None,
    event_type_cls: Any = None,
    preview_text_fn: Any = None,
) -> None:
    if not details or not event_type_cls:
        return
    _emit = emit_event_fn
    _preview = preview_text_fn or (lambda v, m=200: str(v)[:m])

    provider_names: list[str] = []
    for detail in details:
        name = str(detail.get("provider") or "").strip()
        if name and name not in provider_names:
            provider_names.append(name)
    provider_label = ", ".join(provider_names) if provider_names else "memory providers"
    _emit(
        event_type_cls.AGENT_MEMORY_USED,
        {
            "source": "prefetch",
            "provider_names": provider_names,
            "provider_count": len(provider_names),
            "query_preview": _preview(query, 200),
            "content_chars": len(merged_content or ""),
            "summary": f"Prefetched memory from {provider_label}",
            "duration_ms": duration_ms,
        },
    )


def emit_auto_injection_context(
    *,
    base_user_message: str,
    prefetch_query: str,
    prefetch_details: list[dict[str, Any]],
    prefetch_merged: str,
    plugin_user_context: str,
    emit_event_fn: Any = None,
    event_type_cls: Any = None,
    preview_text_fn: Any = None,
) -> None:
    if not event_type_cls:
        return

    _emit = emit_event_fn
    _preview = preview_text_fn or (lambda v, m=200: str(v)[:m])

    provider_names: list[str] = []
    provider_blocks: list[str] = []
    provider_param_blocks: list[str] = []
    for detail in prefetch_details or []:
        provider_name = str(detail.get("provider") or "").strip() or "unknown"
        provider_content = str(detail.get("content") or "").strip()
        provider_debug = detail.get("prefetch_debug")
        if provider_name and provider_name not in provider_names:
            provider_names.append(provider_name)
        if provider_content:
            provider_blocks.append(f"## {provider_name}\n{provider_content}")
        if isinstance(provider_debug, dict) and provider_debug:
            try:
                debug_text = json.dumps(provider_debug, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            except Exception:
                debug_text = str(provider_debug)
            provider_param_blocks.append(f"## {provider_name}\n{debug_text}")

    fenced_prefetch = build_memory_context_block(prefetch_merged) if prefetch_merged else ""
    injected_parts = [part for part in [fenced_prefetch, plugin_user_context] if part]
    effective_user_message = (
        base_user_message + "\n\n" + "\n\n".join(injected_parts)
        if injected_parts else base_user_message
    )
    summary = (
        f"Auto-injected {len(injected_parts)} context block(s)"
        if injected_parts else "No auto-injected context"
    )

    _emit(
        event_type_cls.AGENT_MEMORY_USED,
        {
            "stage": "context",
            "source": "auto_injection",
            "summary": summary,
            "injection_sources": [
                source
                for source, content in (
                    ("memory_prefetch", fenced_prefetch),
                    ("plugin_pre_llm_call", plugin_user_context),
                )
                if content
            ],
            "prefetch_query_preview": _preview(prefetch_query, 200),
            "provider_names": provider_names,
            "provider_count": len(provider_names),
            "memory_prefetch_by_provider": "\n\n".join(provider_blocks),
            "memory_prefetch_content": prefetch_merged,
            "memory_prefetch_fenced": fenced_prefetch,
            "memory_prefetch_params_by_provider": "\n\n".join(provider_param_blocks),
            "plugin_user_context": plugin_user_context,
            "user_message_before_auto_injection": base_user_message,
            "user_message_after_auto_injection": effective_user_message,
        },
    )


def emit_memory_tool_usage(
    function_name: str,
    function_args: dict,
    function_result: str,
    *,
    tool_call_id: Optional[str] = None,
    memory_manager: Any = None,
    emit_event_fn: Any = None,
    event_type_cls: Any = None,
    preview_text_fn: Any = None,
    detect_tool_failure_fn: Any = None,
) -> None:
    if not event_type_cls:
        return

    _emit = emit_event_fn
    _preview = preview_text_fn or (lambda v, m=200: str(v)[:m])
    _detect_failure = detect_tool_failure_fn

    is_builtin_memory = function_name == "memory"
    is_provider_memory = bool(memory_manager and memory_manager.has_tool(function_name))
    if not is_builtin_memory and not is_provider_memory:
        return
    if _detect_failure:
        is_error_result, _ = _detect_failure(function_name, function_result)
        if is_error_result:
            return

    if is_builtin_memory:
        action = str(function_args.get("action") or "").strip() or "unknown"
        target = str(function_args.get("target") or "memory").strip() or "memory"
        payload = {
            "source": "builtin_tool",
            "tool_name": "memory",
            "tool_call_id": tool_call_id or "",
            "action": action,
            "target": target,
            "content_preview": _preview(function_args.get("content"), 200),
            "summary": f"memory.{action} → {target}",
        }
    else:
        provider = memory_manager.get_provider_for_tool(function_name) if memory_manager else None
        provider_name = str(getattr(provider, "name", "") or "").strip() or "external"
        payload = {
            "source": "provider_tool",
            "provider_name": provider_name,
            "tool_name": function_name,
            "tool_call_id": tool_call_id or "",
            "args_preview": _preview(function_args, 300),
            "summary": f"{provider_name}.{function_name}",
        }

    payload["result_preview"] = _preview(function_result, 200)
    _emit(event_type_cls.AGENT_MEMORY_USED, payload)


# ---------------------------------------------------------------------------
# Write notification
# ---------------------------------------------------------------------------

def notify_external_memory_write(
    function_args: dict,
    target: str,
    result: str,
    *,
    memory_manager: Any = None,
) -> None:
    if not (
        memory_manager
        and function_args.get("action") in ("add", "replace", "remove")
        and memory_tool_result_success(result)
    ):
        return
    try:
        memory_content = function_args.get("content", "")
        if function_args.get("action") == "remove":
            memory_content = function_args.get("old_text", "")
        memory_manager.on_memory_write(
            function_args.get("action", ""),
            target,
            memory_content,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def execute_memory_tool(
    function_args: dict,
    *,
    memory_store: Any = None,
    memory_manager: Any = None,
) -> str:
    target = function_args.get("target", "memory")
    action = function_args.get("action")
    content = function_args.get("content")
    old_text = function_args.get("old_text")

    if memory_store is not None:
        from tools.memory_tool import memory_tool as _memory_tool
        result = _memory_tool(
            action=action,
            target=target,
            content=content,
            old_text=old_text,
            store=memory_store,
        )
        if memory_tool_unavailable(result) and memory_manager is not None:
            return memory_manager.handle_memory_write(
                action,
                target,
                content=content,
                old_text=old_text,
            )
        notify_external_memory_write(
            function_args, target, result, memory_manager=memory_manager,
        )
        return result

    if memory_manager is not None:
        return memory_manager.handle_memory_write(
            action,
            target,
            content=content,
            old_text=old_text,
        )

    from tools.memory_tool import memory_tool as _memory_tool
    return _memory_tool(
        action=action,
        target=target,
        content=content,
        old_text=old_text,
        store=None,
    )
