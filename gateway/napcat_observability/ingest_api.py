"""
NapCat observability ingest API.

Receives event batches from the main Hermes process when the
observability service runs as a separate process.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from gateway.napcat_observability.store import ObservabilityStore

_log = logging.getLogger(__name__)


def create_ingest_router(store: ObservabilityStore):
    """Create a FastAPI router for event ingestion."""
    try:
        from fastapi import APIRouter, HTTPException
        from pydantic import BaseModel
    except ImportError:
        raise SystemExit("Ingest API requires fastapi: pip install hermes-agent[web]")

    router = APIRouter(prefix="/api/napcat/ingest", tags=["ingest"])

    class NapcatIngestEvent(BaseModel):
        event_id: str
        trace_id: str
        span_id: str
        ts: float
        event_type: str
        payload: Dict[str, Any] = {}
        parent_span_id: str = ""
        session_key: str = ""
        session_id: str = ""
        platform: str = "napcat"
        chat_type: str = ""
        chat_id: str = ""
        user_id: str = ""
        message_id: str = ""
        severity: str = "info"

    class EventBatch(BaseModel):
        events: List[NapcatIngestEvent]

    class IngestResponse(BaseModel):
        ingested: int
        rejected: int = 0
        status: str = "ok"

    @router.post("/events", response_model=IngestResponse)
    async def ingest_events(batch: EventBatch):
        """Receive a batch of events from the main process."""
        if not batch.events:
            return IngestResponse(ingested=0)

        event_dicts = [e.model_dump() for e in batch.events]
        try:
            count = store.insert_events_batch(event_dicts)
            return IngestResponse(ingested=count, rejected=len(event_dicts) - count)
        except Exception as e:
            _log.error("Ingest failed: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/health")
    async def ingest_health():
        return {"status": "ok"}

    return router
