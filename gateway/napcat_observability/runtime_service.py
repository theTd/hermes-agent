"""Runtime wiring for NapCat observability inside the gateway process."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from gateway.napcat_observability.ingest_client import IngestClient
from gateway.napcat_observability.publisher import init_publisher, shutdown_publisher
from gateway.napcat_observability.store import ObservabilityStore

_log = logging.getLogger(__name__)


class NapcatObservabilityRuntimeService:
    """Run persistence and the optional HTTP server inside the gateway process."""

    def __init__(
        self,
        obs_config: Optional[dict[str, Any]] = None,
        *,
        serve_http: bool = True,
    ):
        self.obs_config = dict(obs_config or {})
        self.serve_http = serve_http
        self.store: Optional[ObservabilityStore] = None
        self.ingest_client: Optional[IngestClient] = None
        self._http_server: Any = None
        self._http_task: Optional[asyncio.Task] = None
        self._ws_hub: Any = None
        self._started = False

    @classmethod
    def from_loaded_config(cls, *, serve_http: bool = True) -> "NapcatObservabilityRuntimeService":
        from hermes_cli.config import load_config

        cfg = load_config()
        return cls(cfg.get("napcat_observability", {}), serve_http=serve_http)

    @property
    def enabled(self) -> bool:
        return bool(self.obs_config.get("enabled", False))

    async def start(self) -> bool:
        """Start local persistence and the optional HTTP service."""
        if self._started:
            return True
        if not self.enabled:
            return False

        queue_size = int(self.obs_config.get("queue_size", 10000) or 10000)
        max_stdout_chars = int(self.obs_config.get("max_stdout_chars", 8000) or 8000)
        max_stderr_chars = int(self.obs_config.get("max_stderr_chars", 4000) or 4000)

        init_publisher(
            enabled=True,
            queue_size=queue_size,
            max_stdout_chars=max_stdout_chars,
            max_stderr_chars=max_stderr_chars,
        )

        self.store = ObservabilityStore()
        self.ingest_client = IngestClient(self.store)
        self.ingest_client.start()
        self._started = True

        retention_days = int(self.obs_config.get("retention_days", 30) or 30)
        try:
            cleanup = self.store.cleanup_expired(retention_days=retention_days)
            _log.info("NapCat observability cleanup on startup: %s", cleanup)
        except Exception:
            _log.debug("NapCat observability cleanup failed", exc_info=True)

        if self.serve_http:
            await self._start_http_server()

        return True

    async def stop(self) -> None:
        """Stop the HTTP task, flush persistence, and disable publishing."""
        if not self._started:
            shutdown_publisher()
            return

        if self._ws_hub is not None:
            try:
                await self._ws_hub.close_all_clients()
            except Exception:
                _log.warning("NapCat observability WebSocket client shutdown failed", exc_info=True)
            finally:
                self._ws_hub = None

        if self._http_server is not None:
            self._http_server.should_exit = True
        if self._http_task is not None:
            try:
                await asyncio.wait_for(self._http_task, timeout=5.0)
            except asyncio.TimeoutError:
                _log.warning("Timed out stopping NapCat observability HTTP server")
                self._http_task.cancel()
            except Exception:
                _log.warning("NapCat observability HTTP server shutdown failed", exc_info=True)
            finally:
                self._http_task = None
                self._http_server = None

        if self.ingest_client is not None:
            self.ingest_client.stop()
            self.ingest_client = None

        if self.store is not None:
            self.store.close()
            self.store = None

        shutdown_publisher()
        self._started = False

    async def _start_http_server(self) -> None:
        try:
            import uvicorn
        except ImportError:
            _log.warning(
                "NapCat observability HTTP UI requested but uvicorn is unavailable. "
                "Install hermes-agent[web] to expose the monitor port."
            )
            return

        try:
            from hermes_cli.napcat_observability_server import create_app
        except BaseException as exc:
            _log.warning(
                "NapCat observability HTTP UI is unavailable during import: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return

        bind_host = str(self.obs_config.get("bind_host", "127.0.0.1") or "127.0.0.1")
        bind_port = int(self.obs_config.get("port", 9120) or 9120)

        try:
            app = create_app(
                store=self.store,
                ingest_client=self.ingest_client,
                obs_cfg=self.obs_config,
                close_store_on_shutdown=False,
            )
        except BaseException as exc:
            _log.warning(
                "NapCat observability HTTP UI is unavailable: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return
        config = uvicorn.Config(app, host=bind_host, port=bind_port, log_level="info")
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        self._http_server = server
        self._ws_hub = getattr(getattr(app, "state", None), "hub", None)

        async def _serve() -> None:
            try:
                await server.serve()
            except asyncio.CancelledError:
                raise
            except BaseException:
                # uvicorn may raise SystemExit on bind/startup failures
                # (for example, when the monitor port is already in use).
                # Keep that failure isolated to the observability sidecar so
                # the main gateway process can continue running.
                _log.exception("NapCat observability HTTP server crashed")

        self._http_task = asyncio.create_task(
            _serve(),
            name="napcat-observability-http",
        )
        self._http_task.add_done_callback(self._handle_http_task_done)
        _log.info("NapCat observability HTTP server starting on http://%s:%s", bind_host, bind_port)

    def _handle_http_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.error(
                "NapCat observability HTTP server failed: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
