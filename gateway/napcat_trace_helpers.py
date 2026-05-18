"""Branch-local helpers for resolving NapCat runtime trace state."""

from __future__ import annotations

from typing import Any, Optional

from gateway.runtime_extension import get_gateway_trace_context


def augment_trace_metadata(
    event: Any,
    metadata: Optional[Dict[str, Any]] = None,
    *,
    trace_attr: str = "",
    metadata_key: str = "",
) -> Optional[Dict[str, Any]]:
    """Merge trace context from an event into outgoing message metadata.

    Used by platform adapters to thread observability trace state through
    message delivery without adapters needing to import observability types.
    """
    merged = dict(metadata or {})
    trace_ctx = getattr(event, trace_attr, None) if (event is not None and trace_attr) else None
    if trace_ctx is not None and metadata_key:
        merged.setdefault(metadata_key, trace_ctx)
    return merged or None


def resolve_napcat_trace_context(
    host: Any,
    event: Any,
    *,
    source: Optional[Any] = None,
) -> Optional[Any]:
    """Resolve trace context through adapter-declared attrs before generic fallback."""
    if event is None:
        return None

    resolved_source = source or getattr(event, "source", None)
    adapter = None

    adapter_for_source = getattr(host, "adapter_for_source", None)
    if callable(adapter_for_source) and resolved_source is not None:
        adapter = adapter_for_source(resolved_source)

    if adapter is None:
        adapter_for_platform = getattr(host, "adapter_for_platform", None)
        platform = getattr(resolved_source, "platform", None)
        if callable(adapter_for_platform):
            adapter = adapter_for_platform(platform)

    raw_trace_attr = getattr(adapter, "TRACE_CONTEXT_ATTR", "")
    trace_attr = raw_trace_attr.strip() if isinstance(raw_trace_attr, str) else ""
    if trace_attr:
        trace_ctx = getattr(event, trace_attr, None)
        if trace_ctx is not None:
            return trace_ctx

    trace_ctx = get_gateway_trace_context(event)
    if trace_ctx is not None:
        return trace_ctx

    # Keep the legacy branch-local event attr as the last fallback so existing
    # in-memory tests and partially constructed events still resolve trace state.
    return getattr(event, "napcat_trace_ctx", None)
