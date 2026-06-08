"""Multi-endpoint Solana JSON-RPC pool with score-based failover.

`RpcPool` owns a small set of HTTPS RPC endpoints and round-robins between
them in priority order of a rolling health score:

    score(ep) = success_rate / (1 + p50_latency_s)

Failing endpoints are quarantined for `health_quarantine_s` and skipped.
The mode manager flips to `DEGRADED_RPC` when `healthy_count()` falls below
`health_min_healthy`.

The pool publishes:
  * Prometheus metrics: `solalpha_rpc_request_latency_seconds`,
    `solalpha_rpc_quarantined`, `solalpha_rpc_healthy_endpoints`.
  * A `probe()` coroutine the `HealthRegistry` can register.

Errors are mapped onto `foundation.errors`:
  * `RpcTransientError`     -- timeouts, connection resets, 429, 5xx, RPC
                               error -32603 (internal error), -32005 (node
                               behind), -32007 (slot was skipped).
  * `RpcPermanentError`     -- 4xx other than 429, RPC -32601/-32602/-32600.
  * `NoHealthyRpcError`     -- raised by `call()` when every endpoint is
                               quarantined or all retries failed.
"""

from __future__ import annotations

import json
import secrets
from collections import deque
from typing import TYPE_CHECKING, Any

import httpx

from solalpha.foundation import metrics
from solalpha.foundation.errors import (
    NoHealthyRpcError,
    RpcPermanentError,
    RpcTransientError,
)
from solalpha.foundation.health import Probe
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    from solalpha.foundation.clock import Clock

_log = get_logger(__name__)

# Codes that mean the caller is wrong; do not retry.
_PERMANENT_RPC_CODES: frozenset[int] = frozenset({-32600, -32601, -32602})


class _EndpointState:
    """Rolling success/latency window for a single endpoint."""

    __slots__ = ("quarantined_until", "recent", "url")

    def __init__(self, url: str) -> None:
        self.url = url
        # Each entry: (monotonic_ts, success, latency_seconds).
        self.recent: deque[tuple[float, bool, float]] = deque(maxlen=200)
        self.quarantined_until: float | None = None


class RpcPool:
    """Score-ranked pool of Solana RPC endpoints."""

    def __init__(
        self,
        urls: Iterable[str],
        clock: Clock,
        *,
        request_timeout_s: float = 8.0,
        health_quarantine_s: float = 30.0,
        health_window_s: float = 60.0,
        health_min_success_rate: float = 0.6,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        url_list = [u.strip() for u in urls if u and u.strip()]
        if not url_list:
            raise ValueError("RpcPool requires at least one URL")
        self._endpoints: list[_EndpointState] = [_EndpointState(u) for u in url_list]
        self._clock = clock
        self._timeout_s = request_timeout_s
        self._quarantine_s = health_quarantine_s
        self._window_s = health_window_s
        self._min_success_rate = health_min_success_rate
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(request_timeout_s),
            http2=False,
        )
        metrics.RPC_HEALTHY.set(len(self._endpoints))

    @property
    def urls(self) -> list[str]:
        return [ep.url for ep in self._endpoints]

    # ---- lifecycle ----

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def reload(self, urls: Iterable[str]) -> int:
        """Reconcile the live endpoint set with `urls`.

        Endpoints already present keep their rolling score/quarantine state;
        new URLs are added with a fresh (optimistic) score; URLs no longer
        listed are dropped. Returns the new endpoint count. Raises
        `ValueError` if `urls` resolves to an empty list -- the pool must
        always retain at least one endpoint.

        This is the in-process half of `solalpha reload-rpc` (and the POSIX
        SIGHUP path). It is synchronous and does not interrupt in-flight
        `call()`s: a call already holding an `_EndpointState` reference
        finishes against it; the next `call()` selects from the new set.
        """
        seen: set[str] = set()
        url_list: list[str] = []
        for u in urls:
            cleaned = u.strip() if u else ""
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                url_list.append(cleaned)
        if not url_list:
            raise ValueError("RpcPool.reload requires at least one URL")
        existing = {ep.url: ep for ep in self._endpoints}
        self._endpoints = [existing.get(u) or _EndpointState(u) for u in url_list]
        # Clear the quarantine gauge for endpoints that were dropped.
        for url in existing:
            if url not in seen:
                metrics.RPC_QUARANTINED.labels(endpoint=url).set(0)
        metrics.RPC_HEALTHY.set(len(self._endpoints))
        _log.info("rpc_pool_reloaded", endpoints=len(self._endpoints))
        return len(self._endpoints)

    # ---- public API ----

    async def call(
        self,
        method: str,
        params: list[Any] | dict[str, Any] | None = None,
        *,
        retries: int = 2,
    ) -> Any:
        """Run a single JSON-RPC call against the healthiest endpoint.

        Retries up to `retries` more endpoints on transient failure. Returns
        the parsed `result` field. Raises `NoHealthyRpcError` if every viable
        endpoint failed, `RpcPermanentError` on a permanent JSON-RPC error.
        """
        last_transient: Exception | None = None
        tried: set[str] = set()
        attempts = retries + 1
        for _ in range(attempts):
            ep = self._select(exclude=tried)
            if ep is None:
                break
            tried.add(ep.url)
            try:
                return await self._call_one(ep, method, params)
            except RpcPermanentError:
                raise
            except RpcTransientError as e:
                last_transient = e
                self._quarantine(ep, reason=str(e))
                continue
        if last_transient is not None:
            raise NoHealthyRpcError(
                f"all endpoints exhausted for {method}: {last_transient}"
            ) from last_transient
        raise NoHealthyRpcError(f"no healthy endpoints for {method}")

    def healthy_count(self) -> int:
        now = self._clock.monotonic()
        return sum(1 for ep in self._endpoints if not self._is_quarantined(ep, now))

    async def probe(self) -> Probe:
        """HealthRegistry probe coroutine."""
        now = self._clock.monotonic()
        healthy = 0
        worst_status = "ok"
        for ep in self._endpoints:
            if self._is_quarantined(ep, now):
                worst_status = "degraded"
                continue
            healthy += 1
        if healthy == 0:
            worst_status = "down"
        elif healthy < max(1, len(self._endpoints) // 2):
            worst_status = "degraded"
        metrics.RPC_HEALTHY.set(healthy)
        return Probe(
            status=worst_status,
            details={
                "healthy": healthy,
                "total": len(self._endpoints),
                "endpoints": {
                    ep.url: {
                        "quarantined": self._is_quarantined(ep, now),
                        "score": round(self._score(ep, now), 4),
                    }
                    for ep in self._endpoints
                },
            },
        )

    # ---- internals ----

    def _select(self, *, exclude: set[str]) -> _EndpointState | None:
        now = self._clock.monotonic()
        candidates = [
            ep
            for ep in self._endpoints
            if ep.url not in exclude and not self._is_quarantined(ep, now)
        ]
        if not candidates:
            return None
        # Highest score wins; deterministic tie-break by URL for replay.
        candidates.sort(key=lambda e: (-self._score(e, now), e.url))
        return candidates[0]

    def _is_quarantined(self, ep: _EndpointState, now_mono: float) -> bool:
        if ep.quarantined_until is None:
            return False
        if ep.quarantined_until <= now_mono:
            ep.quarantined_until = None
            metrics.RPC_QUARANTINED.labels(endpoint=ep.url).set(0)
            return False
        return True

    def _score(self, ep: _EndpointState, now_mono: float) -> float:
        # Drop samples older than the rolling window.
        window_floor = now_mono - self._window_s
        recent = [s for s in ep.recent if s[0] >= window_floor]
        if not recent:
            # Optimistic prior so a brand-new pool actually issues calls.
            return 1.0
        successes = sum(1 for _, ok, _ in recent if ok)
        rate = successes / len(recent)
        latencies = sorted(lat for _, ok, lat in recent if ok)
        p50 = latencies[len(latencies) // 2] if latencies else self._timeout_s
        return rate / (1.0 + p50)

    def _quarantine(self, ep: _EndpointState, *, reason: str) -> None:
        ep.quarantined_until = self._clock.monotonic() + self._quarantine_s
        metrics.RPC_QUARANTINED.labels(endpoint=ep.url).set(1)
        _log.warning(
            "rpc_quarantined",
            endpoint=ep.url,
            until_s=self._quarantine_s,
            reason=reason,
        )

    def _record(
        self,
        ep: _EndpointState,
        *,
        ok: bool,
        latency_s: float,
        method: str,
    ) -> None:
        ep.recent.append((self._clock.monotonic(), ok, latency_s))
        metrics.RPC_REQUEST_LATENCY.labels(
            endpoint=ep.url,
            method=method,
            outcome="ok" if ok else "error",
        ).observe(latency_s)

    async def _call_one(
        self,
        ep: _EndpointState,
        method: str,
        params: list[Any] | dict[str, Any] | None,
    ) -> Any:
        body = {
            "jsonrpc": "2.0",
            "id": secrets.randbits(31),
            "method": method,
            "params": params if params is not None else [],
        }
        started = self._clock.monotonic()
        try:
            resp = await self._client.post(ep.url, json=body)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            elapsed = self._clock.monotonic() - started
            self._record(ep, ok=False, latency_s=elapsed, method=method)
            raise RpcTransientError(f"{type(e).__name__}: {e}", endpoint=ep.url) from e
        elapsed = self._clock.monotonic() - started
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            self._record(ep, ok=False, latency_s=elapsed, method=method)
            raise RpcTransientError(
                f"HTTP {resp.status_code} from {ep.url}",
                endpoint=ep.url,
                code=resp.status_code,
            )
        if 400 <= resp.status_code < 500:
            self._record(ep, ok=False, latency_s=elapsed, method=method)
            raise RpcPermanentError(
                f"HTTP {resp.status_code} from {ep.url}",
                endpoint=ep.url,
                code=resp.status_code,
            )
        try:
            payload = resp.json()
        except json.JSONDecodeError as e:
            self._record(ep, ok=False, latency_s=elapsed, method=method)
            raise RpcTransientError(f"malformed JSON from {ep.url}: {e}", endpoint=ep.url) from e
        if "error" in payload and payload["error"] is not None:
            err = payload["error"]
            code = int(err.get("code", 0)) if isinstance(err, dict) else 0
            message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            self._record(ep, ok=False, latency_s=elapsed, method=method)
            if code in _PERMANENT_RPC_CODES:
                raise RpcPermanentError(
                    f"RPC error {code} from {ep.url}: {message}",
                    endpoint=ep.url,
                    code=code,
                )
            # Every non-permanent code (known-transient, 0, or unknown) is
            # treated as transient so the caller fails over to another endpoint.
            raise RpcTransientError(
                f"RPC error {code} from {ep.url}: {message}",
                endpoint=ep.url,
                code=code,
            )
        self._record(ep, ok=True, latency_s=elapsed, method=method)
        return payload.get("result")


__all__ = ["RpcPool"]
