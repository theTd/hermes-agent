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

    class EventBatch(BaseModel):
        events: List[Dict[str, Any]]

    class IngestResponse(BaseModel):
        ingested: int
        status: str = "ok"

    @router.post("/events", response_model=IngestResponse)
    async def ingest_events(batch: EventBatch):
        """Receive a batch of events from the main process."""
        if not batch.events:
            return IngestResponse(ingested=0)

        try:
            count = store.insert_events_batch(batch.events)
            return IngestResponse(ingested=count)
        except Exception as e:
            _log.error("Ingest failed: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/health")
    async def ingest_health():
        return {"status": "ok"}

    return router
