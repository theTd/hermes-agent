"""
NapCat Observability — independent monitoring service.

Provides a FastAPI backend for the NapCat monitoring dashboard,
including REST API, WebSocket stream, and event ingest endpoints.

Usage:
    python -m hermes_cli.main napcat-monitor
    python -m hermes_cli.main napcat-monitor --port 9120
"""

import logging
import shutil
import subprocess
import sys
import asyncio
import secrets
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes_cli.config import load_config

_log = logging.getLogger(__name__)
_NAPCAT_WEB_DIST = Path(__file__).parent / "napcat_web_dist"
_NAPCAT_WEB_SRC = PROJECT_ROOT / "napcat_web"
_NAPCAT_WEB_BUILD_ATTEMPTED = False


def _ensure_napcat_web_dist() -> bool:
    """Best-effort build for the NapCat monitor frontend."""
    global _NAPCAT_WEB_BUILD_ATTEMPTED

    if _NAPCAT_WEB_DIST.is_dir():
        return True
    if _NAPCAT_WEB_BUILD_ATTEMPTED:
        return _NAPCAT_WEB_DIST.is_dir()

    _NAPCAT_WEB_BUILD_ATTEMPTED = True
    if not (_NAPCAT_WEB_SRC / "package.json").exists():
        _log.info("NapCat frontend source not found at %s", _NAPCAT_WEB_SRC)
        return False

    npm = shutil.which("npm")
    if not npm:
        _log.warning(
            "NapCat frontend is not built and npm is unavailable. "
            "Install Node.js, then run: cd napcat_web && npm install && npm run build"
        )
        return False

    _log.info("Building NapCat monitoring frontend from %s", _NAPCAT_WEB_SRC)
    install = subprocess.run(
        [npm, "install", "--silent"],
        cwd=_NAPCAT_WEB_SRC,
        capture_output=True,
        text=True,
    )
    if install.returncode != 0:
        _log.warning("NapCat frontend npm install failed: %s", (install.stderr or install.stdout).strip())
        return False

    build = subprocess.run(
        [npm, "run", "build"],
        cwd=_NAPCAT_WEB_SRC,
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        _log.warning("NapCat frontend build failed: %s", (build.stderr or build.stdout).strip())
        return False

    return _NAPCAT_WEB_DIST.is_dir()


def _get_obs_config() -> dict:
    """Return the napcat_observability section from config."""
    cfg = load_config()
    return cfg.get("napcat_observability", {})


def create_app(
    *,
    store=None,
    ingest_client=None,
    obs_cfg: dict | None = None,
    close_store_on_shutdown: bool = True,
):
    """Create the FastAPI application for NapCat observability."""
    try:
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError:
        raise SystemExit(
            "NapCat observability requires fastapi and uvicorn.\n"
            "Install them with: pip install hermes-agent[web]"
        )

    app = FastAPI(
        title="NapCat Observability",
        description="NapCat monitoring and observability dashboard",
    )

    obs_cfg = obs_cfg or _get_obs_config()
    access_token = str(obs_cfg.get("access_token") or "").strip()
    auth_required = bool(access_token)
    session_cookie_name = "napcat_obs_session"
    session_max_age_seconds = 60 * 60 * 24 * 7
    active_sessions: set[str] = set()

    def _is_protected_http_path(path: str) -> bool:
        if not path.startswith("/api/napcat"):
            return False
        if path.startswith("/api/napcat/auth"):
            return False
        if path.startswith("/api/napcat/health"):
            return False
        if path.startswith("/api/napcat/ingest"):
            return False
        return True

    def _is_authenticated(session_id: str | None) -> bool:
        return bool(session_id and session_id in active_sessions)

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from fastapi import HTTPException, Request
    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def _enforce_observability_auth(request: Request, call_next):
        if not auth_required or not _is_protected_http_path(request.url.path):
            return await call_next(request)
        session_id = request.cookies.get(session_cookie_name)
        if _is_authenticated(session_id):
            return await call_next(request)
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    @app.get("/api/napcat/auth/status")
    async def auth_status(request: Request):
        if not auth_required:
            return {"enabled": False, "authenticated": True}
        session_id = request.cookies.get(session_cookie_name)
        return {
            "enabled": True,
            "authenticated": _is_authenticated(session_id),
        }

    @app.post("/api/napcat/auth/login")
    async def auth_login(request: Request):
        if not auth_required:
            return {"status": "disabled", "authenticated": True}

        try:
            payload = await request.json()
        except Exception:
            payload = {}

        token = payload.get("token", "") if isinstance(payload, dict) else ""
        token = token if isinstance(token, str) else ""
        if not secrets.compare_digest(token, access_token):
            raise HTTPException(status_code=401, detail="Invalid token")

        session_id = secrets.token_urlsafe(32)
        active_sessions.add(session_id)
        response = JSONResponse({"status": "ok", "authenticated": True})
        response.set_cookie(
            key=session_cookie_name,
            value=session_id,
            max_age=session_max_age_seconds,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    @app.post("/api/napcat/auth/logout")
    async def auth_logout(request: Request):
        session_id = request.cookies.get(session_cookie_name)
        if session_id:
            active_sessions.discard(session_id)

        response = JSONResponse({"status": "ok", "authenticated": False})
        response.delete_cookie(session_cookie_name, path="/")
        return response

    # Wire up store and ingest
    from gateway.napcat_observability.store import ObservabilityStore
    from gateway.napcat_observability.ingest_api import create_ingest_router

    if store is None:
        store = ObservabilityStore()
    app.state.store = store

    ingest_router = create_ingest_router(store)
    app.include_router(ingest_router)

    from gateway.napcat_observability.repository import ObservabilityRepository
    from gateway.napcat_observability.query_api import create_query_router
    from gateway.napcat_observability.actions import create_actions_router

    repo = ObservabilityRepository(store)
    app.state.repo = repo

    query_router = create_query_router(repo)
    app.include_router(query_router)

    actions_router = create_actions_router(
        store,
        allow_actions=obs_cfg.get("allow_control_actions", False),
    )
    app.include_router(actions_router)

    @app.get("/api/napcat/health")
    async def health():
        return {
            "status": "ok",
            "service": "napcat-observability",
            "store": str(store.db_path),
        }

    # Wire up WebSocket hub
    from gateway.napcat_observability.ws_hub import WebSocketHub

    hub = WebSocketHub(repo)
    app.state.hub = hub
    _ws_loop: dict[str, asyncio.AbstractEventLoop | None] = {"loop": None}

    @app.on_event("startup")
    async def _capture_ws_loop():
        _ws_loop["loop"] = asyncio.get_running_loop()

    # Periodic dashboard stats broadcast
    _dashboard_task: asyncio.Task | None = None

    async def _dashboard_broadcast_loop() -> None:
        while True:
            try:
                await asyncio.sleep(5)
                stats = await asyncio.to_thread(repo.query_dashboard_stats)
                await hub.broadcast_dashboard_update(stats)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.debug("Dashboard broadcast failed", exc_info=True)

    @app.on_event("startup")
    async def _start_dashboard_broadcast():
        nonlocal _dashboard_task
        _dashboard_task = asyncio.create_task(_dashboard_broadcast_loop())

    @app.on_event("shutdown")
    async def _stop_dashboard_broadcast():
        if _dashboard_task is not None:
            _dashboard_task.cancel()
            try:
                await _dashboard_task
            except asyncio.CancelledError:
                pass

    # Register hub as event consumer for real-time broadcasts
    from gateway.napcat_observability.publisher import register_consumer, unregister_consumer

    def _on_event(event):
        """Bridge sync publisher events to async WebSocket broadcasts."""
        loop = _ws_loop["loop"]
        if loop is None:
            return
        try:
            event_dict = event.to_dict()
            payload = event_dict.get("payload")
            if isinstance(payload, dict):
                timing = payload.get("timing")
                if not isinstance(timing, dict):
                    timing = {}
                timing.setdefault("ws_broadcast_scheduled_at", time.time())
                payload["timing"] = timing
            asyncio.run_coroutine_threadsafe(hub.broadcast_event(event_dict), loop)
        except Exception:
            _log.debug("Failed to schedule WebSocket event broadcast", exc_info=True)

    register_consumer(_on_event)
    if ingest_client is not None:
        def _schedule_trace_update(trace: dict) -> None:
            loop = _ws_loop["loop"]
            if loop is None:
                return
            try:
                asyncio.run_coroutine_threadsafe(hub.broadcast_trace_update(trace), loop)
            except Exception:
                _log.debug("Failed to schedule trace update broadcast", exc_info=True)

        def _schedule_group_update(group: dict) -> None:
            loop = _ws_loop["loop"]
            if loop is None:
                return
            try:
                asyncio.run_coroutine_threadsafe(hub.broadcast_group_update(group), loop)
            except Exception:
                _log.debug("Failed to schedule group update broadcast", exc_info=True)

        def _schedule_alert(alert: dict) -> None:
            loop = _ws_loop["loop"]
            if loop is None:
                return
            try:
                asyncio.run_coroutine_threadsafe(hub.broadcast_alert(alert), loop)
            except Exception:
                _log.debug("Failed to schedule alert broadcast", exc_info=True)

        ingest_client.add_trace_update_listener(
            _schedule_trace_update
        )
        ingest_client.add_group_update_listener(
            _schedule_group_update
        )
        ingest_client.add_alert_listener(
            _schedule_alert
        )

    @app.on_event("shutdown")
    async def shutdown():
        _ws_loop["loop"] = None
        unregister_consumer(_on_event)
        if close_store_on_shutdown:
            store.close()

    # WebSocket endpoint
    try:
        from starlette.websockets import WebSocket, WebSocketDisconnect
    except ImportError:
        from fastapi import WebSocket
        from fastapi.exceptions import WebSocketDisconnect

    @app.websocket("/ws/napcat/stream")
    async def websocket_stream(websocket: WebSocket):
        if auth_required and not _is_authenticated(websocket.cookies.get(session_cookie_name)):
            await websocket.close(code=4401, reason="Unauthorized")
            return

        await websocket.accept()
        await hub.connect(websocket)

        try:
            while True:
                data = await websocket.receive_json()
                msg_type = data.get("type", "")

                if msg_type == "backfill":
                    cursor = data.get("cursor", 0.0)
                    await hub.handle_backfill(websocket, cursor, filters=data.get("filters"))
                elif msg_type == "pause":
                    await hub.pause_client(websocket)
                elif msg_type == "resume":
                    await hub.resume_client(websocket)
                elif msg_type == "subscribe":
                    await hub.subscribe_client(websocket, data.get("filters"))
        except WebSocketDisconnect:
            pass
        except Exception:
            _log.debug("WebSocket error", exc_info=True)
        finally:
            await hub.disconnect(websocket)

    # ---------------------------------------------------------------------------
    # Serve the independent NapCat monitoring frontend (built Vite SPA)
    # ---------------------------------------------------------------------------
    if _ensure_napcat_web_dist():
        from fastapi.staticfiles import StaticFiles
        from fastapi.responses import FileResponse

        @app.get("/")
        async def serve_index():
            return FileResponse(_NAPCAT_WEB_DIST / "index.html")

        # SPA fallback: any non-API path serves index.html for client-side routing
        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            file_path = _NAPCAT_WEB_DIST / full_path
            if file_path.is_file():
                return FileResponse(file_path)
            return FileResponse(_NAPCAT_WEB_DIST / "index.html")

        # Mount static assets (JS, CSS, fonts, etc.)
        app.mount("/assets", StaticFiles(directory=str(_NAPCAT_WEB_DIST / "assets")), name="napcat-assets")
        if (_NAPCAT_WEB_DIST / "fonts").is_dir():
            app.mount("/fonts", StaticFiles(directory=str(_NAPCAT_WEB_DIST / "fonts")), name="napcat-fonts")

        _log.info("Serving NapCat monitoring frontend from %s", _NAPCAT_WEB_DIST)
    else:
        from fastapi.responses import HTMLResponse

        @app.get("/")
        async def frontend_missing():
            return HTMLResponse(
                """
                <!doctype html>
                <html lang="en">
                  <head>
                    <meta charset="utf-8" />
                    <meta name="viewport" content="width=device-width, initial-scale=1" />
                    <title>NapCat Observability</title>
                    <style>
                      body { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; background: #0f1115; color: #ece7dc; margin: 0; }
                      main { max-width: 760px; margin: 48px auto; padding: 24px; }
                      .card { border: 1px solid #343843; background: #171a21; padding: 24px; }
                      code { color: #f3b35f; }
                      p, li { line-height: 1.6; }
                    </style>
                  </head>
                  <body>
                    <main>
                      <div class="card">
                        <h1>NapCat Observability</h1>
                        <p>The API server is running, but the monitor frontend is not built yet.</p>
                        <p>Expected build output: <code>hermes_cli/napcat_web_dist</code></p>
                        <p>Build manually if needed:</p>
                        <ul>
                          <li><code>cd napcat_web</code></li>
                          <li><code>npm install</code></li>
                          <li><code>npm run build</code></li>
                        </ul>
                        <p>API health: <code>/api/napcat/health</code></p>
                      </div>
                    </main>
                  </body>
                </html>
                """
            )

        _log.info(
            "NapCat frontend not found at %s — API-only mode. "
            "Build with: cd napcat_web && npm run build",
            _NAPCAT_WEB_DIST,
        )

    return app


def start_server(
    host: str | None = None,
    port: int | None = None,
    allow_public: bool = False,
):
    """Start the NapCat observability server."""
    try:
        import uvicorn
    except ImportError:
        raise SystemExit(
            "NapCat observability requires uvicorn.\n"
            "Install with: pip install hermes-agent[web]"
        )

    obs_cfg = _get_obs_config()

    if not obs_cfg.get("enabled", False):
        _log.warning(
            "NapCat observability is disabled in config. "
            "Set napcat_observability.enabled=true to enable."
        )

    bind_host = host or obs_cfg.get("bind_host", "127.0.0.1")
    bind_port = port or obs_cfg.get("port", 9120)

    if bind_host not in ("127.0.0.1", "localhost", "::1") and not allow_public:
        print(
            f"WARNING: Binding to {bind_host} exposes the monitoring service on the network.\n"
            "Use --insecure to confirm, or set bind_host to 127.0.0.1."
        )
        sys.exit(1)

    app = create_app()

    print(f"NapCat Observability starting on http://{bind_host}:{bind_port}")
    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")
