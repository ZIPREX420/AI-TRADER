"""Score, dedupe, and dispatch signals from wallet_tracker + token_scanner."""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Union

from .wallet_tracker import CopySignal
from .token_scanner import NewTokenSignal

log = logging.getLogger("signal_engine")

Signal = Union[CopySignal, NewTokenSignal]


@dataclass
class TradeOrder:
    mint: str
    score: float
    source: str  # "copy:<wallet>" | "new:raydium" | "new:pumpfun"
    sol_size: float
    reason: str
    sig_origin: str


class SignalEngine:
    """Tracks recent mints to avoid duplicate buys; merges co-occurring copy + new signals."""

    def __init__(self, dedupe_window_s: float = 300.0):
        self.dedupe_window_s = dedupe_window_s
        self.recent: deque[tuple[str, float]] = deque(maxlen=2048)
        self.copy_seen: dict[str, list[tuple[str, float]]] = {}  # mint → [(wallet, ts)]
        self.exit_seen: dict[str, list[tuple[str, float]]] = {}  # mint → [(wallet, ts)]

    def _seen_recent(self, mint: str, now: float) -> bool:
        cutoff = now - self.dedupe_window_s
        while self.recent and self.recent[0][1] < cutoff:
            self.recent.popleft()
        return any(m == mint for m, _ in self.recent)

    def _record_smart_exit(self, mint: str, wallet: str, now: float) -> int:
        lst = self.exit_seen.setdefault(mint, [])
        lst.append((wallet, now))
        cutoff = now - 3600.0  # 1 hour
        lst[:] = [(w, t) for w, t in lst if t > cutoff]
        return len({w for w, _ in lst})

    def _record_smart_buy(self, mint: str, wallet: str, now: float) -> int:
        lst = self.copy_seen.setdefault(mint, [])
        if not any(w == wallet for w, _ in lst):
            lst.append((wallet, now))
        cutoff = now - 600.0  # 10 minutes
        lst[:] = [(w, t) for w, t in lst if t > cutoff]
        return len({w for w, _ in lst})

    def evaluate(self, sig: Signal, capital_usd: float, sol_price_usd: float) -> TradeOrder | None:
        now = time.time()

        if isinstance(sig, CopySignal):
            if sig.direction == "sell":
                # Track exits; don't open trade. Position manager consumes via separate path.
                self._record_smart_exit(sig.mint, sig.wallet, now)
                return None
            buyers = self._record_smart_buy(sig.mint, sig.wallet, now)
            exits_recent = len(self.exit_seen.get(sig.mint, []))
            if exits_recent >= 3:
                return None
            if self._seen_recent(sig.mint, now):
                return None
            self.recent.append((sig.mint, now))
            score = 0.7 + 0.05 * min(buyers - 1, 3)  # cluster bonus
            sol_size = (capital_usd * 0.07) / max(sol_price_usd, 1e-6)
            reason = f"copy:{sig.wallet[:6]}…buyers={buyers} age={sig.age_ms:.0f}ms"
            return TradeOrder(
                mint=sig.mint,
                score=score,
                source=f"copy:{sig.wallet}",
                sol_size=sol_size,
                reason=reason,
                sig_origin=sig.sig,
            )

        if isinstance(sig, NewTokenSignal):
            if self._seen_recent(sig.mint, now):
                return None
            self.recent.append((sig.mint, now))
            score = 0.30 if sig.source == "raydium" else 0.35  # pumpfun graduates slightly higher prior
            sol_size = (capital_usd * 0.05) / max(sol_price_usd, 1e-6)
            reason = f"new:{sig.source} age={sig.age_ms:.0f}ms"
            return TradeOrder(
                mint=sig.mint,
                score=score,
                source=f"new:{sig.source}",
                sol_size=sol_size,
                reason=reason,
                sig_origin=sig.sig,
            )

        return None

    def smart_exit_count(self, mint: str) -> int:
        return len({w for w, _ in self.exit_seen.get(mint, [])})

    def smart_buyers(self, mint: str) -> set[str]:
        return {w for w, _ in self.copy_seen.get(mint, [])}
