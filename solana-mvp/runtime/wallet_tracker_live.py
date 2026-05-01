"""Subscription manager for tracked smart wallets.

Subscribes top-N wallets by score over chunked WS connections. For each event,
fetches the parsed transaction (Helius enhanced API or RPC), decodes via tx_decoder,
emits WalletEvent + (optionally) PrePumpSignal via cluster_detector to the queue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from core.types import PrePumpSignal, WalletEvent
from signal.cluster_detector import ClusterDetector
from signal.tx_decoder import decode_swap

log = logging.getLogger("wallet_tracker_live")


@dataclass
class TrackedWallet:
    pubkey: str
    score: float
    cluster_id: str = ""


def _read_scored(path: Path) -> list[TrackedWallet]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out: list[TrackedWallet] = []
    if isinstance(raw, list):
        for r in raw:
            if not isinstance(r, dict):
                continue
            pk = str(r.get("pubkey", "")).strip()
            if not pk:
                continue
            out.append(TrackedWallet(
                pubkey=pk,
                score=float(r.get("S", 0.0)),
                cluster_id=str(r.get("cluster_id", "")),
            ))
    out.sort(key=lambda w: -w.score)
    return out


class WalletTrackerLive:
    """Live subscription pipeline. All external calls are dependency-injected."""

    def __init__(
        self,
        *,
        rpc,                                # SolanaRPC
        scored_wallets_path: str | Path = "data/smart_wallets_scored.json",
        out_queue: asyncio.Queue[Any],
        fetch_tx: Callable[[str], Awaitable[Optional[dict]]],
        max_wallets: int = 100,
        cluster_detector: Optional[ClusterDetector] = None,
        wallet_age: Optional[Callable[[str], float]] = None,
        rotate_period_s: float = 300.0,
        min_score: float = 0.55,
    ):
        self._rpc = rpc
        self._path = Path(scored_wallets_path)
        self._queue = out_queue
        self._fetch_tx = fetch_tx
        self._max_wallets = max_wallets
        self._wallet_age = wallet_age or (lambda _w: float("inf"))
        self._rotate_period_s = rotate_period_s
        self._min_score = min_score
        self._wallets: list[TrackedWallet] = []
        self._wallet_index: dict[str, TrackedWallet] = {}
        self._cluster = cluster_detector or ClusterDetector(
            wallet_age=self._wallet_age,
            cluster_of=lambda w: self._wallet_index.get(w, TrackedWallet(w, 0.0)).cluster_id or w[:6],
        )
        self._stopped = False
        self._stream_task: Optional[asyncio.Task] = None
        self._rotate_task: Optional[asyncio.Task] = None
        self._raw_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=10_000)

    def _refresh_wallet_set(self) -> None:
        loaded = _read_scored(self._path)
        kept = [w for w in loaded if w.score >= self._min_score][: self._max_wallets]
        self._wallets = kept
        self._wallet_index = {w.pubkey: w for w in kept}

    async def run(self) -> None:
        self._refresh_wallet_set()
        self._stream_task = asyncio.create_task(self._stream_loop(), name="tracker.stream")
        self._rotate_task = asyncio.create_task(self._rotate_loop(), name="tracker.rotate")
        consume = asyncio.create_task(self._consume_loop(), name="tracker.consume")
        try:
            await consume
        finally:
            self._stopped = True
            for t in (self._stream_task, self._rotate_task):
                if t and not t.done():
                    t.cancel()

    async def _stream_loop(self) -> None:
        # Build mentions from current wallet set; subscribe via RPC.
        if not self._wallets:
            log.warning("no tracked wallets; idling stream")
            while not self._stopped:
                await asyncio.sleep(5.0)
            return
        mentions = [w.pubkey for w in self._wallets]
        try:
            await self._rpc.stream_events(self._raw_queue, mentions=mentions)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"stream_loop error: {type(e).__name__}: {e}")

    async def _consume_loop(self) -> None:
        while not self._stopped:
            try:
                ev = await asyncio.wait_for(self._raw_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            mention = getattr(ev, "mention", "") or ""
            sig = getattr(ev, "signature", "") or ""
            if not mention or not sig:
                continue
            wallet = mention if mention in self._wallet_index else None
            if wallet is None:
                # Helius mentions can be prefix-matched; skip otherwise
                continue
            try:
                tx_json = await self._fetch_tx(sig)
            except Exception as e:
                log.debug(f"fetch_tx failed: {type(e).__name__}: {e}")
                continue
            if not tx_json:
                continue
            decoded = decode_swap(tx_json, wallet)
            if not decoded:
                continue
            await self._queue.put(decoded)
            sigp = self._cluster.update(decoded,
                                        instruction_byte_hash=tx_json.get("instruction_byte_hash"),
                                        fee_payer=tx_json.get("feePayer"))
            if sigp is not None:
                await self._queue.put(sigp)

    async def _rotate_loop(self) -> None:
        last_mtime = 0.0
        try:
            last_mtime = self._path.stat().st_mtime if self._path.exists() else 0.0
        except OSError:
            pass
        while not self._stopped:
            await asyncio.sleep(self._rotate_period_s)
            try:
                mtime = self._path.stat().st_mtime if self._path.exists() else 0.0
            except OSError:
                continue
            if mtime > last_mtime:
                last_mtime = mtime
                self._refresh_wallet_set()
                # Restart stream task with new mentions
                if self._stream_task and not self._stream_task.done():
                    self._stream_task.cancel()
                self._stream_task = asyncio.create_task(self._stream_loop(),
                                                       name="tracker.stream")
