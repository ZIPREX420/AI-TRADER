"""Cluster detector.

Emits when at least `wallets_required` *distinct smart wallets* buy the
same mint within `window_s` and their combined buy USD exceeds
`min_total_buy_usd`. "Smart" is decided by `SmartWalletScorer.is_smart`.

Per-mint cooldown of `window_s` prevents repeat emissions for the same
buying cluster; a fresh emission requires at least one additional smart
wallet to join after the cooldown.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from solalpha.domain import DetectorSignal

if TYPE_CHECKING:
    from datetime import datetime

    from solalpha.domain import NormalizedSwap
    from solalpha.foundation.config import SignalsClusterConfig
    from solalpha.signal.smart_wallet_scorer import SmartWalletScorer


class ClusterDetector:
    """Smart-wallet co-buy detector."""

    name = "cluster"

    def __init__(
        self,
        cfg: SignalsClusterConfig,
        scorer: SmartWalletScorer,
    ) -> None:
        self._cfg = cfg
        self._scorer = scorer
        # Each entry: (epoch_seconds, wallet, usd_value).
        self._per_mint: dict[str, deque[tuple[float, str, float]]] = {}
        self._last_emit: dict[str, float] = {}

    async def observe(self, swap: NormalizedSwap) -> None:
        if swap.side != "buy":
            return
        if not self._scorer.is_smart(swap.wallet):
            return
        ts = swap.block_time.timestamp()
        buf = self._per_mint.setdefault(swap.mint, deque())
        buf.append((ts, swap.wallet, swap.usd_value))
        cutoff = ts - self._cfg.window_s
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def poll(self, now: datetime) -> list[DetectorSignal]:
        out: list[DetectorSignal] = []
        now_ts = now.timestamp()
        for mint, buf in list(self._per_mint.items()):
            cutoff = now_ts - self._cfg.window_s
            while buf and buf[0][0] < cutoff:
                buf.popleft()
            if not buf:
                self._per_mint.pop(mint, None)
                continue
            last = self._last_emit.get(mint)
            if last is not None and now_ts - last < self._cfg.window_s:
                continue
            wallets = {entry[1] for entry in buf}
            total_usd = sum(v for _, _, v in buf)
            if len(wallets) < self._cfg.wallets_required:
                continue
            if total_usd < self._cfg.min_total_buy_usd:
                continue
            score = min(
                1.0,
                len(wallets) / float(self._cfg.wallets_required * 2),
            )
            out.append(
                DetectorSignal(
                    detector="cluster",
                    mint=mint,
                    score=score,
                    features={
                        "unique_smart_wallets": float(len(wallets)),
                        "total_buy_usd": float(total_usd),
                        "window_s": float(self._cfg.window_s),
                    },
                    observed_at=now,
                )
            )
            self._last_emit[mint] = now_ts
        return out


__all__ = ["ClusterDetector"]
