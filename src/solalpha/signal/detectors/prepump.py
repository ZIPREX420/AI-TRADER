"""Pre-pump detector.

Watches per-mint buy/sell flow over a sliding `window_s` window and emits a
signal when:
  * buy_pressure_ratio = sum(buy_usd) / max(epsilon, sum(sell_usd)) >=
    `min_buy_pressure_ratio`
  * liquidity_slope_pct_per_min, computed by comparing the average
    per-minute buy volume in the first half vs the second half of the
    window, exceeds `min_liquidity_slope_pct_per_min`.

A per-mint cooldown of `window_s / 2` prevents the combiner from being
flooded by repeated emissions for the same condition.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from solalpha.domain import DetectorSignal

if TYPE_CHECKING:
    from datetime import datetime

    from solalpha.domain import NormalizedSwap
    from solalpha.foundation.config import SignalsPrePumpConfig

_EPSILON = 1e-9


class PrePumpDetector:
    """Per-mint buy-pressure + slope monitor."""

    name = "prepump"

    def __init__(self, cfg: SignalsPrePumpConfig) -> None:
        self._cfg = cfg
        # Each entry: (epoch_seconds, side: "buy"|"sell", usd_value).
        self._per_mint: dict[str, deque[tuple[float, str, float]]] = {}
        self._last_emit: dict[str, float] = {}

    async def observe(self, swap: NormalizedSwap) -> None:
        ts = swap.block_time.timestamp()
        buf = self._per_mint.setdefault(swap.mint, deque())
        buf.append((ts, swap.side, swap.usd_value))
        cutoff = ts - self._cfg.window_s
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def poll(self, now: datetime) -> list[DetectorSignal]:
        out: list[DetectorSignal] = []
        now_ts = now.timestamp()
        cooldown = self._cfg.window_s / 2.0
        for mint, buf in list(self._per_mint.items()):
            cutoff = now_ts - self._cfg.window_s
            while buf and buf[0][0] < cutoff:
                buf.popleft()
            if not buf:
                self._per_mint.pop(mint, None)
                continue
            last = self._last_emit.get(mint)
            if last is not None and now_ts - last < cooldown:
                continue
            buy_usd = sum(v for t, side, v in buf if side == "buy")
            sell_usd = sum(v for t, side, v in buf if side == "sell")
            ratio = buy_usd / max(_EPSILON, sell_usd)
            if ratio < self._cfg.min_buy_pressure_ratio:
                continue
            slope = self._slope(buf, now_ts)
            if slope < self._cfg.min_liquidity_slope_pct_per_min:
                continue
            score = min(1.0, ratio / (self._cfg.min_buy_pressure_ratio * 2.0))
            out.append(
                DetectorSignal(
                    detector="prepump",
                    mint=mint,
                    score=score,
                    features={
                        "buy_pressure_ratio": float(ratio),
                        "buy_usd_window": float(buy_usd),
                        "sell_usd_window": float(sell_usd),
                        "slope_per_min": float(slope),
                    },
                    observed_at=now,
                )
            )
            self._last_emit[mint] = now_ts
        return out

    def _slope(self, buf: deque[tuple[float, str, float]], now_ts: float) -> float:
        """Return % growth in buy USD between first vs second half of the window."""
        half = self._cfg.window_s / 2.0
        first_half = [v for t, side, v in buf if side == "buy" and t < now_ts - half]
        second_half = [v for t, side, v in buf if side == "buy" and t >= now_ts - half]
        if not first_half:
            return float(sum(second_half)) / max(_EPSILON, half / 60.0)
        first_sum = sum(first_half)
        second_sum = sum(second_half)
        if first_sum <= 0:
            return 0.0
        return (second_sum - first_sum) / first_sum


__all__ = ["PrePumpDetector"]
