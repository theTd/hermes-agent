"""Branch-local observability backend for NapCat runtime events."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from gateway.napcat_observability.publisher import emit_napcat_event
from gateway.napcat_observability.schema import EventType, Severity
from gateway.napcat_observability.trace import current_napcat_trace


class NapCatObservabilityBackend:
    """Expose NapCat observability through the generic backend seam."""

    event_type_cls = EventType
    severity_cls = Severity

    def get_current_trace_context(self) -> Optional[Any]:
        try:
            return current_napcat_trace.get()
        except Exception:
            return None

    @contextmanager
    def bind_trace_context(self, trace_ctx: Optional[Any]) -> Iterator[None]:
        if not trace_ctx:
            yield
            return

        token = None
        try:
            token = current_napcat_trace.set(trace_ctx)
            yield
        finally:
            if token is not None:
                try:
                    current_napcat_trace.reset(token)
                except Exception:
                    pass

    def emit_event(
        self,
        event_type: Optional[Any],
        payload: Dict[str, Any],
        *,
        trace_ctx: Optional[Any] = None,
        severity: Optional[Any] = None,
    ) -> bool:
        if not event_type:
            return False
        try:
            return emit_napcat_event(
                event_type,
                payload,
                trace_ctx=trace_ctx,
                severity=severity,
            ) is not None
        except Exception:
            return False
