"""
NapCat observability control actions.

Limited operational actions protected by the allow_control_actions config flag.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from gateway.napcat_observability.store import ObservabilityStore

_log = logging.getLogger(__name__)


def create_actions_router(store: ObservabilityStore, *, allow_actions: bool = False):
    """Create a FastAPI router for control action endpoints."""
    try:
        from fastapi import APIRouter, HTTPException
        from pydantic import BaseModel
    except ImportError:
        raise SystemExit("Actions API requires fastapi: pip install hermes-agent[web]")

    router = APIRouter(prefix="/api/napcat/actions", tags=["napcat-actions"])

    def _check_allowed():
        if not allow_actions:
            raise HTTPException(
                status_code=403,
                detail="Control actions are disabled. Set napcat_observability.allow_control_actions=true to enable.",
            )

    class CleanupRequest(BaseModel):
        retention_days: int = 30

    class DiagnosticLevelRequest(BaseModel):
        level: str = "info"  # debug, info, warn, error

    @router.post("/reconnect")
    async def reconnect_adapter():
        """Force reconnect the NapCat adapter."""
        _check_allowed()
        # This would signal the adapter to reconnect.
        # For now, update runtime state to indicate reconnect requested.
        store.set_runtime_state("reconnect_requested", {
            "requested": True,
            "status": "pending",
        })
        return {"status": "ok", "message": "Reconnect requested"}

    @router.post("/cleanup")
    async def cleanup_data(req: CleanupRequest):
        """Manually trigger data cleanup."""
        _check_allowed()
        result = store.cleanup_expired(retention_days=req.retention_days)
        return {"status": "ok", "cleaned": result}

    @router.post("/clear-all")
    async def clear_all_data():
        """Delete all persisted observability data."""
        _check_allowed()
        result = store.clear_all()
        return {"status": "ok", "cleared": result}

    @router.post("/diagnostic-level")
    async def set_diagnostic_level(req: DiagnosticLevelRequest):
        """Set diagnostic logging level."""
        _check_allowed()
        import logging as _logging
        level_map = {
            "debug": _logging.DEBUG,
            "info": _logging.INFO,
            "warn": _logging.WARNING,
            "error": _logging.ERROR,
        }
        level = level_map.get(req.level.lower())
        if level is None:
            raise HTTPException(status_code=400, detail=f"Invalid level: {req.level}")

        _logging.getLogger("gateway.napcat_observability").setLevel(level)
        store.set_runtime_state("diagnostic_level", {"level": req.level})
        return {"status": "ok", "level": req.level}

    @router.post("/alerts/{alert_id}/acknowledge")
    async def acknowledge_alert(alert_id: str):
        """Acknowledge an alert."""
        _check_allowed()
        found = store.acknowledge_alert(alert_id)
        if not found:
            raise HTTPException(status_code=404, detail="Alert not found")
        return {"status": "ok"}

    return router
