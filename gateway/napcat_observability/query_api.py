"""
NapCat observability query API.

REST endpoints for querying events, traces, sessions, alerts,
and message processing chains.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from gateway.napcat_observability.repository import ObservabilityRepository

_log = logging.getLogger(__name__)


def create_query_router(repo: ObservabilityRepository):
    """Create a FastAPI router for query endpoints."""
    try:
        from fastapi import APIRouter, HTTPException, Query
    except ImportError:
        raise SystemExit("Query API requires fastapi: pip install hermes-agent[web]")

    router = APIRouter(prefix="/api/napcat", tags=["napcat-query"])

    @router.get("/runtime")
    async def get_runtime():
        """Get current runtime state."""
        return repo.store.get_all_runtime_state()

    @router.get("/sessions")
    async def get_sessions(
        time_start: Optional[float] = Query(None),
        time_end: Optional[float] = Query(None),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        sessions, total = repo.query_sessions(
            time_start=time_start, time_end=time_end,
            limit=limit, offset=offset,
        )
        return {"sessions": sessions, "total": total}

    @router.get("/traces")
    async def get_traces(
        time_start: Optional[float] = Query(None),
        time_end: Optional[float] = Query(None),
        session_key: Optional[str] = Query(None),
        chat_id: Optional[str] = Query(None),
        user_id: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
        event_type: Optional[str] = Query(None),
        search: Optional[str] = Query(None),
        view: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        traces, total = repo.query_traces(
            time_start=time_start, time_end=time_end,
            session_key=session_key, chat_id=chat_id,
            user_id=user_id, status=status,
            event_type=event_type, search=search, view=view,
            limit=limit, offset=offset,
        )
        return {"traces": traces, "total": total}

    @router.get("/traces/{trace_id}")
    async def get_trace(trace_id: str):
        trace = repo.get_trace(trace_id)
        if not trace:
            raise HTTPException(status_code=404, detail="Trace not found")
        return trace

    @router.get("/events")
    async def get_events(
        trace_id: Optional[str] = Query(None),
        time_start: Optional[float] = Query(None),
        time_end: Optional[float] = Query(None),
        session_key: Optional[str] = Query(None),
        chat_id: Optional[str] = Query(None),
        user_id: Optional[str] = Query(None),
        message_id: Optional[str] = Query(None),
        event_type: Optional[str] = Query(None),
        severity: Optional[str] = Query(None),
        search: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        events, total = repo.query_events(
            trace_id=trace_id,
            time_start=time_start, time_end=time_end,
            session_key=session_key, chat_id=chat_id,
            user_id=user_id, message_id=message_id,
            event_type=event_type, severity=severity,
            search=search, limit=limit, offset=offset,
        )
        return {"events": events, "total": total}

    @router.get("/messages/{message_id}")
    async def get_message(message_id: str):
        result = repo.get_message_events(message_id)
        if not result["events"]:
            raise HTTPException(status_code=404, detail="Message not found")
        return result

    @router.get("/alerts")
    async def get_alerts(
        acknowledged: Optional[bool] = Query(None),
        severity: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        alerts, total = repo.query_alerts(
            acknowledged=acknowledged, severity=severity,
            limit=limit, offset=offset,
        )
        return {"alerts": alerts, "total": total}

    @router.get("/stats")
    async def get_stats():
        """Get publisher statistics."""
        from gateway.napcat_observability.publisher import get_stats
        return get_stats()

    return router
