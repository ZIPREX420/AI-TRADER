"""Component health registry and HealthSnapshot model.

Components register a probe coroutine; HealthRegistry caches results for a
short TTL. ModeManager subscribes to snapshots; Prometheus reads from
metrics directly. The /health endpoint returns a HealthSnapshot.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field

from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from solalpha.foundation.clock import Clock

_log = get_logger(__name__)

ProbeStatus = Literal["ok", "degraded", "down"]
OverallStatus = Literal["ok", "degraded", "down"]


class Probe(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: ProbeStatus
    latency_ms: float = 0.0
    last_error: str | None = None
    details: dict[str, object] = Field(default_factory=dict)


class HealthSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: datetime
    overall: OverallStatus
    components: dict[str, Probe]
    mode: str = "PAPER"
    open_positions: int = 0
    daily_pnl_usd: float = 0.0
    kill_switch_armed: bool = False


ProbeFn = Callable[[], Awaitable[Probe]]


class HealthRegistry:
    """Registers component probes and aggregates them."""

    def __init__(self, clock: Clock, *, cache_ttl_s: float = 5.0) -> None:
        self._clock = clock
        self._ttl = cache_ttl_s
        self._probes: dict[str, ProbeFn] = {}
        self._cache: dict[str, tuple[float, Probe]] = {}
        self._lock = anyio.Lock()
        self._auxiliary: dict[str, Any] = {
            "mode": "PAPER",
            "open_positions": 0,
            "daily_pnl_usd": 0.0,
            "kill_switch_armed": False,
        }

    def register(self, name: str, probe: ProbeFn) -> None:
        self._probes[name] = probe

    def set_aux(self, key: str, value: Any) -> None:
        self._auxiliary[key] = value

    async def _run_probe(self, name: str, probe: ProbeFn) -> Probe:
        try:
            return await probe()
        except Exception as e:  # probes must never escape
            _log.warning("probe_failed", probe=name, exc=str(e), exc_type=type(e).__name__)
            return Probe(status="down", last_error=f"{type(e).__name__}: {e}")

    async def snapshot(self, *, force_refresh: bool = False) -> HealthSnapshot:
        results: dict[str, Probe] = {}
        now_mono = self._clock.monotonic()
        async with self._lock:
            stale_names = []
            for name, probe in self._probes.items():
                cached = self._cache.get(name)
                if cached and not force_refresh and now_mono - cached[0] < self._ttl:
                    results[name] = cached[1]
                else:
                    stale_names.append((name, probe))

            for name, probe in stale_names:
                p = await self._run_probe(name, probe)
                self._cache[name] = (self._clock.monotonic(), p)
                results[name] = p

        overall: OverallStatus = "ok"
        for p in results.values():
            if p.status == "down":
                overall = "down"
                break
            if p.status == "degraded":
                overall = "degraded"
        return HealthSnapshot(
            ts=self._clock.now(),
            overall=overall,
            components=results,
            mode=str(self._auxiliary.get("mode", "PAPER")),
            open_positions=int(self._auxiliary.get("open_positions", 0) or 0),
            daily_pnl_usd=float(self._auxiliary.get("daily_pnl_usd", 0.0) or 0.0),
            kill_switch_armed=bool(self._auxiliary.get("kill_switch_armed", False)),
        )


__all__ = [
    "HealthRegistry",
    "HealthSnapshot",
    "OverallStatus",
    "Probe",
    "ProbeFn",
    "ProbeStatus",
]
