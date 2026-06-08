"""Flow-anomaly detector.

Per-mint z-score detector. For each mint we maintain a rolling
`baseline_window_s` of per-`bucket_s` aggregated buy volume; a new bucket
that is `z_threshold` standard deviations above the baseline mean fires a
signal.

The detector is intentionally crude (no seasonality, no median-of-medians)
because the signal-plane risk engine and confidence combiner act as
filters downstream; this detector's job is to be *fast* and *not miss*
genuine anomalies. False positives are expected and harmless.
"""

from __future__ import annotations

import math
from collections import deque
from typing import TYPE_CHECKING

from solalpha.domain import DetectorSignal

if TYPE_CHECKING:
    from datetime import datetime

    from solalpha.domain import NormalizedSwap
    from solalpha.foundation.config import SignalsFlowAnomalyConfig


# Bucket granularity: one second. Coarser would smooth too much; finer
# explodes per-mint memory for high-throughput tokens.
_BUCKET_S = 1.0


class FlowAnomalyDetector:
    """Per-mint volume-z-score detector."""

    name = "flow_anomaly"

    def __init__(self, cfg: SignalsFlowAnomalyConfig) -> None:
        self._cfg = cfg
        self._max_buckets = max(60, int(cfg.baseline_window_s / _BUCKET_S))
        # mint -> deque[(bucket_start_ts, buy_usd_in_bucket)]
        self._per_mint: dict[str, deque[tuple[float, float]]] = {}
        self._last_emit: dict[str, float] = {}

    async def observe(self, swap: NormalizedSwap) -> None:
        if swap.side != "buy":
            return
        ts = swap.block_time.timestamp()
        bucket = math.floor(ts / _BUCKET_S) * _BUCKET_S
        buf = self._per_mint.setdefault(swap.mint, deque())
        if buf and buf[-1][0] == bucket:
            last_ts, last_v = buf[-1]
            buf[-1] = (last_ts, last_v + swap.usd_value)
        else:
            buf.append((bucket, swap.usd_value))
        while len(buf) > self._max_buckets:
            buf.popleft()

    def poll(self, now: datetime) -> list[DetectorSignal]:
        out: list[DetectorSignal] = []
        now_ts = now.timestamp()
        cooldown = 60.0  # one emission per minute per mint
        for mint, buf in list(self._per_mint.items()):
            cutoff = now_ts - self._cfg.baseline_window_s
            while buf and buf[0][0] < cutoff:
                buf.popleft()
            if len(buf) < 30:
                continue  # need at least 30s of history for a useful stdev
            last = self._last_emit.get(mint)
            if last is not None and now_ts - last < cooldown:
                continue
            values = [v for _, v in buf]
            *baseline, current = values
            if not baseline:
                continue
            mean = sum(baseline) / len(baseline)
            var = sum((v - mean) ** 2 for v in baseline) / max(1, len(baseline) - 1)
            stdev = math.sqrt(var) if var > 0 else 0.0
            # Floor the stdev at 10% of the mean (or 1.0) so a perfectly
            # constant baseline still allows large absolute deviations to
            # fire. Real on-chain flow always has some variance, but tests
            # and the genuine "first wallet to enter a brand-new mint"
            # case can produce zero-variance windows.
            stdev = max(stdev, 0.1 * mean, 1.0)
            z = (current - mean) / stdev
            if z < self._cfg.z_threshold:
                continue
            score = min(1.0, z / (self._cfg.z_threshold * 2.0))
            out.append(
                DetectorSignal(
                    detector="flow_anomaly",
                    mint=mint,
                    score=score,
                    features={
                        "z_score": float(z),
                        "current_bucket_usd": float(current),
                        "baseline_mean": float(mean),
                        "baseline_stdev": float(stdev),
                    },
                    observed_at=now,
                )
            )
            self._last_emit[mint] = now_ts
        return out


__all__ = ["FlowAnomalyDetector"]
