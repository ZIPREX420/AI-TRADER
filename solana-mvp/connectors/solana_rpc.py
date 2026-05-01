"""Multi-WS endpoint pool with dedup, health tracking, and submission routing."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Optional

import httpx

from core.state import JsonStore
from core.types import Event

log = logging.getLogger("rpc")


@dataclass
class Endpoint:
    name: str
    http_url: str
    ws_url: str
    priority: int = 1
    max_subs_per_ws: int = 50


@dataclass
class _Health:
    last_event_ts: float = 0.0
    last_failure_ts: float = 0.0
    consecutive_failures: int = 0
    error_window_5m: list[float] = field(default_factory=list)
    success_window_5m: list[float] = field(default_factory=list)
    latency_ms_recent: deque = field(default_factory=lambda: deque(maxlen=200))
    demoted_until: float = 0.0


class _LRU:
    def __init__(self, maxlen: int = 50_000, ttl_s: float = 60.0):
        self.maxlen = maxlen
        self.ttl_s = ttl_s
        self._d: OrderedDict[str, float] = OrderedDict()

    def seen(self, key: str, now: float) -> bool:
        cutoff = now - self.ttl_s
        # opportunistic eviction (a few entries)
        for _ in range(8):
            if not self._d:
                break
            k0, t0 = next(iter(self._d.items()))
            if t0 < cutoff:
                self._d.popitem(last=False)
            else:
                break
        if key in self._d:
            self._d.move_to_end(key)
            return True
        self._d[key] = now
        if len(self._d) > self.maxlen:
            self._d.popitem(last=False)
        return False


class SolanaRPC:
    def __init__(self, endpoints: Iterable[Endpoint], state_dir: str = "data/state",
                 dedupe_ttl_s: float = 60.0):
        self.endpoints: list[Endpoint] = [e for e in endpoints if e.http_url or e.ws_url]
        self.health: dict[str, _Health] = {e.name: _Health() for e in self.endpoints}
        self.dedupe = _LRU(maxlen=50_000, ttl_s=dedupe_ttl_s)
        self.health_store = JsonStore(Path(state_dir) / "rpc_health.json", {})
        self._ws_tasks: list[asyncio.Task] = []
        self._stopped = False

    # ───── health ─────
    def _hb_ok(self, name: str, latency_ms: float) -> None:
        h = self.health.get(name)
        if h is None:
            return
        now = time.time()
        h.last_event_ts = now
        h.consecutive_failures = 0
        h.success_window_5m.append(now)
        h.success_window_5m = [t for t in h.success_window_5m if now - t < 300]
        h.error_window_5m = [t for t in h.error_window_5m if now - t < 300]
        if latency_ms > 0:
            h.latency_ms_recent.append(latency_ms)

    def _hb_err(self, name: str) -> None:
        h = self.health.get(name)
        if h is None:
            return
        now = time.time()
        h.last_failure_ts = now
        h.consecutive_failures += 1
        h.error_window_5m.append(now)
        h.error_window_5m = [t for t in h.error_window_5m if now - t < 300]
        if h.consecutive_failures >= 5:
            h.demoted_until = now + 600  # 10 min cooldown

    def health_snapshot(self) -> dict:
        snap: dict[str, dict] = {}
        for name, h in self.health.items():
            lat = sorted(h.latency_ms_recent)
            n = len(lat)
            p50 = lat[n // 2] if n else 0.0
            p95 = lat[int(n * 0.95)] if n else 0.0
            total = len(h.success_window_5m) + len(h.error_window_5m)
            err_rate = (len(h.error_window_5m) / total) if total else 0.0
            snap[name] = {
                "latency_p50_ms": p50,
                "latency_p95_ms": p95,
                "error_rate_5m": round(err_rate, 4),
                "consec_fail": h.consecutive_failures,
                "last_event_ts": h.last_event_ts,
                "last_failure_ts": h.last_failure_ts,
                "demoted_until": h.demoted_until,
            }
        return snap

    def persist_health(self) -> None:
        self.health_store.save(self.health_snapshot())

    def is_degraded(self, silence_s: float = 60.0, err_rate_max: float = 0.30) -> bool:
        if not self.health:
            return False
        now = time.time()
        all_silent = True
        any_active = False
        for name, h in self.health.items():
            if h.last_event_ts > 0:
                any_active = True
            if h.last_event_ts and (now - h.last_event_ts) < silence_s:
                all_silent = False
        if any_active and all_silent:
            return True
        # error rate criterion
        any_healthy = False
        for h in self.health.values():
            total = len(h.success_window_5m) + len(h.error_window_5m)
            if total >= 5:
                rate = len(h.error_window_5m) / total
                if rate <= err_rate_max:
                    any_healthy = True
        if any_active and not any_healthy:
            return True
        return False

    def best_http(self) -> Optional[str]:
        now = time.time()
        candidates = []
        for ep in self.endpoints:
            if not ep.http_url:
                continue
            h = self.health[ep.name]
            if h.demoted_until > now:
                continue
            total = len(h.success_window_5m) + len(h.error_window_5m)
            err_rate = (len(h.error_window_5m) / total) if total else 0.0
            lat = sorted(h.latency_ms_recent)
            p50 = lat[len(lat) // 2] if lat else 0.0
            candidates.append((err_rate, p50, ep.priority, ep.http_url))
        candidates.sort()
        return candidates[0][3] if candidates else None

    def all_http(self) -> list[str]:
        return [ep.http_url for ep in self.endpoints if ep.http_url]

    # ───── streaming ─────
    async def stream_events(self, queue: asyncio.Queue, mentions: list[str]) -> None:
        """Subscribe to logsSubscribe across every endpoint × mention chunk.

        Re-publishes deduped, normalized Event objects into `queue`.
        """
        # late import to avoid hard dep when unused
        try:
            import websockets
        except Exception:
            log.error("websockets not installed; streaming disabled")
            return
        for ep in self.endpoints:
            if not ep.ws_url:
                continue
            chunk = ep.max_subs_per_ws
            for i in range(0, max(len(mentions), 1), chunk):
                m_chunk = mentions[i : i + chunk] if mentions else []
                t = asyncio.create_task(self._ws_consume(ep, m_chunk, queue))
                self._ws_tasks.append(t)
        try:
            await asyncio.gather(*self._ws_tasks)
        except asyncio.CancelledError:
            pass

    async def _ws_consume(self, ep: Endpoint, mentions: list[str], queue: asyncio.Queue) -> None:
        import websockets
        backoff = 1.0
        while not self._stopped:
            try:
                async with websockets.connect(ep.ws_url, ping_interval=20, ping_timeout=20) as ws:
                    sub_id_to_mention: dict[Any, str] = {}
                    pending: dict[int, str] = {}
                    if not mentions:
                        # Subscribe to all logs (heavy; only used as fallback)
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                            "params": [{"mentions": []}, {"commitment": "processed"}],
                        }))
                    else:
                        for idx, m in enumerate(mentions, start=1):
                            await ws.send(json.dumps({
                                "jsonrpc": "2.0", "id": idx, "method": "logsSubscribe",
                                "params": [{"mentions": [m]}, {"commitment": "processed"}],
                            }))
                            pending[idx] = m
                    backoff = 1.0
                    last_evt = time.time()
                    async for raw in ws:
                        last_evt = time.time()
                        try:
                            msg = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            self._hb_err(ep.name)
                            continue
                        if isinstance(msg, dict) and msg.get("id") in pending and "result" in msg:
                            sub_id_to_mention[msg["result"]] = pending.pop(msg["id"])
                            continue
                        params = msg.get("params") if isinstance(msg, dict) else None
                        if not params:
                            continue
                        result = params.get("result", {})
                        value = result.get("value", {})
                        if not value:
                            continue
                        sig = value.get("signature", "")
                        slot = result.get("context", {}).get("slot", 0)
                        err = value.get("err")
                        logs = value.get("logs", []) or []
                        sub_id = params.get("subscription")
                        mention = sub_id_to_mention.get(sub_id, mentions[0] if mentions else "")
                        if err is not None:
                            continue
                        key = f"{sig}:{slot}"
                        if self.dedupe.seen(key, time.time()):
                            continue
                        ev = Event(
                            ts=time.time(),
                            slot=int(slot),
                            signature=sig,
                            kind="log",
                            source=ep.name,
                            mention=mention,
                            raw_logs=list(logs),
                            block_time=None,
                        )
                        await queue.put(ev)
                        self._hb_ok(ep.name, latency_ms=0.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._hb_err(ep.name)
                log.warning(f"[{ep.name}] ws err: {type(e).__name__}: {e}; backoff={backoff}s")
                await asyncio.sleep(min(30.0, backoff))
                backoff = min(backoff * 1.5, 30.0)

    # ───── HTTP RPC (race semantics) ─────
    async def get_signature_statuses(self, http: httpx.AsyncClient, sigs: list[str]) -> Optional[dict]:
        url = self.best_http()
        if not url:
            return None
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
                   "params": [sigs, {"searchTransactionHistory": True}]}
        t0 = time.time()
        try:
            r = await http.post(url, json=payload, timeout=4.0)
        except (httpx.TimeoutException, httpx.TransportError):
            self._hb_err(self._url_to_name(url))
            return None
        if r.status_code != 200:
            self._hb_err(self._url_to_name(url))
            return None
        self._hb_ok(self._url_to_name(url), latency_ms=(time.time() - t0) * 1000.0)
        try:
            return r.json()
        except ValueError:
            return None

    def _url_to_name(self, url: str) -> str:
        for ep in self.endpoints:
            if ep.http_url == url:
                return ep.name
        return "unknown"

    async def stop(self) -> None:
        self._stopped = True
        for t in self._ws_tasks:
            if not t.done():
                t.cancel()
