"""In-process uvicorn server for the FastAPI backend.

The server runs in the same event loop as the strategy.  It disables
uvicorn's signal capture so the bot's SIGINT/SIGTERM handler remains the
single owner of graceful shutdown.

A failure to bind (for example, port 8033 already in use) is logged and the
trading loops continue; the backend is an observability/control surface, not
a prerequisite for trading.
"""
from __future__ import annotations

import asyncio
import contextlib

import uvicorn

from src.config import Settings
from src.infra.logging import get_logger
from src.runtime_config import RuntimeConfig
from src.store import StateStore
from src.web.app import create_app

log = get_logger("web")


class _Server(uvicorn.Server):
    @staticmethod
    def capture_signals():  # type: ignore[override]
        return contextlib.nullcontext()


class WebServer:
    def __init__(
        self,
        settings: Settings,
        runner,
        store: StateStore,
        runtime: RuntimeConfig,
    ) -> None:
        self.settings = settings
        self.app = create_app(settings, runner, store, runtime)
        self._config = uvicorn.Config(
            self.app,
            host=settings.web.host,
            port=int(settings.web.port),
            log_level=settings.web.log_level,
            access_log=False,
            lifespan="on",
        )
        self._server = _Server(self._config)
        self._task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"http://{self.settings.web.host}:{self.settings.web.port}"

    async def start(self) -> None:
        if not self.settings.web.enabled:
            log.info("web.disabled")
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._server.serve(), name="web-server"
        )
        # uvicorn sets `started` from inside serve(); wait briefly so the
        # startup log has the right state (and bind errors are visible).
        for _ in range(50):
            if self._server.started:
                log.info(
                    "web.started url={} auth={}",
                    self.url,
                    "on" if self.settings.web.auth_enabled else "off",
                )
                return
            if self._task.done():
                exc = self._task.exception()
                log.error(
                    "web.start_failed url={} error={}",
                    self.url,
                    exc or "server exited",
                )
                self._task = None
                return
            await asyncio.sleep(0.1)
        log.warning("web.start_timeout url={}", self.url)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("web.stop_error error={}", exc)
        finally:
            self._task = None
            log.info("web.stopped")
