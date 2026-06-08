"""HTTP exporter: `/metrics`, `/health`, `/status`.

`MetricsServer` is an `aiohttp`-hosted HTTP server bound to
`metrics.host:metrics.port` (per the active config profile). It serves:

  * `GET /metrics` -- Prometheus exposition (`prometheus_client.generate_latest`)
  * `GET /health` -- live `HealthSnapshot` JSON (refreshes probes on demand)
  * `GET /status` -- richer status report: health snapshot, mode, component
    detail. Other planes can register additional component-detail providers.

A background task also writes a `HealthSnapshot` row to `health_snapshots`
every `persist_interval_s` seconds so `solalpha status` (CLI) can read the
last snapshot offline.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

import anyio
from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST

from solalpha.foundation import metrics
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.health import HealthRegistry, HealthSnapshot
    from solalpha.foundation.state import SqliteStore

_log = get_logger(__name__)


class StatusProvider(Protocol):
    """Hook so other planes can contribute to `/status` without circular imports."""

    async def status(self) -> dict[str, Any]:
        """Return a JSON-serializable dict to be merged under `components.<key>`."""


class MetricsServer:
    """Anyio-hosted HTTP server for `/metrics`, `/health`, `/status`."""

    def __init__(
        self,
        registry: HealthRegistry,
        store: SqliteStore,
        clock: Clock,
        *,
        host: str,
        port: int,
        persist_interval_s: float = 10.0,
        providers: dict[str, StatusProvider] | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._clock = clock
        self._host = host
        self._port = port
        self._persist_interval_s = persist_interval_s
        self._providers: dict[str, StatusProvider] = dict(providers or {})

    def add_provider(self, key: str, provider: StatusProvider) -> None:
        self._providers[key] = provider

    async def run(self) -> None:
        """Run the HTTP server until cancelled. Persists snapshots on a side task."""
        app = web.Application()
        app.router.add_get("/metrics", self._handle_metrics)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/status", self._handle_status)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        _log.info("exporter_started", host=self._host, port=self._port)
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._persist_loop)
                await anyio.sleep_forever()
        finally:
            await runner.cleanup()
            _log.info("exporter_stopped")

    # ---- handlers ----

    async def _handle_metrics(self, _: web.Request) -> web.Response:
        return web.Response(body=metrics.render_metrics(), content_type=CONTENT_TYPE_LATEST)

    async def _handle_health(self, _: web.Request) -> web.Response:
        snap = await self._registry.snapshot()
        return web.json_response(self._snapshot_to_dict(snap))

    async def _handle_status(self, _: web.Request) -> web.Response:
        snap = await self._registry.snapshot()
        doc: dict[str, Any] = {"health": self._snapshot_to_dict(snap)}
        components: dict[str, Any] = {}
        for key, provider in self._providers.items():
            try:
                components[key] = await provider.status()
            except Exception as e:
                components[key] = {"status": "error", "error": str(e)}
                _log.warning("status_provider_failed", key=key, exc=str(e))
        if components:
            doc["components"] = components
        return web.json_response(doc)

    # ---- persistence side task ----

    async def _persist_loop(self) -> None:
        while True:
            try:
                snap = await self._registry.snapshot()
                payload = json.dumps(self._snapshot_to_dict(snap), sort_keys=True)
                await self._store.execute(
                    "INSERT INTO health_snapshots (ts, snapshot_json) VALUES (?, ?)",
                    (snap.ts.isoformat(), payload),
                )
            except Exception as e:
                _log.warning("health_persist_failed", exc=str(e))
            await self._clock.sleep(self._persist_interval_s)

    @staticmethod
    def _snapshot_to_dict(snap: HealthSnapshot) -> dict[str, Any]:
        # Probe objects are nested pydantic models; model_dump already handles them.
        return snap.model_dump(mode="json")


__all__ = ["MetricsServer", "StatusProvider"]
