"""Confidence combiner.

Blends per-detector `DetectorSignal`s into a single per-mint `Signal` with
a weighted confidence in [0, 1] and a stable `inputs_hash` so deterministic
replay can verify a recorded session reproduces identical signals.

Only fires when at least `min_distinct_detectors` distinct detectors have
emitted for the same mint inside a `signal_window_s` window. The Signal's
`suggested_usd` is 0 here; the sizer fills it in.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from typing import TYPE_CHECKING

from solalpha.domain import Signal
from solalpha.foundation.ids import new_signal_id, new_trace_id
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from solalpha.domain import DetectorSignal
    from solalpha.foundation.config import SignalsWeightsConfig

_log = get_logger(__name__)


class ConfidenceCombiner:
    """Per-mint windowed combiner over detector outputs."""

    def __init__(
        self,
        weights: SignalsWeightsConfig,
        *,
        signal_window_s: float = 120.0,
        min_distinct_detectors: int = 2,
        emit_cooldown_s: float = 30.0,
    ) -> None:
        self._weights = weights
        self._window_s = signal_window_s
        self._min_distinct = min_distinct_detectors
        self._cooldown_s = emit_cooldown_s
        # mint -> deque[DetectorSignal]
        self._buf: dict[str, deque[DetectorSignal]] = {}
        # mint -> monotonic-ish epoch of last emission, to throttle re-fires
        self._last_emit: dict[str, float] = {}

    async def add(self, signal: DetectorSignal) -> None:
        buf = self._buf.setdefault(signal.mint, deque())
        buf.append(signal)
        # Trim by observed_at; entries older than `window_s` are evicted.
        cutoff = signal.observed_at.timestamp() - self._window_s
        while buf and buf[0].observed_at.timestamp() < cutoff:
            buf.popleft()

    def poll(self, now: datetime) -> list[Signal]:
        out: list[Signal] = []
        now_ts = now.timestamp()
        cutoff = now_ts - self._window_s
        for mint, buf in list(self._buf.items()):
            while buf and buf[0].observed_at.timestamp() < cutoff:
                buf.popleft()
            if not buf:
                self._buf.pop(mint, None)
                continue
            last = self._last_emit.get(mint)
            if last is not None and now_ts - last < self._cooldown_s:
                continue
            # Pick the most recent signal per distinct detector.
            best_per_detector: dict[str, DetectorSignal] = {}
            for d in buf:
                cur = best_per_detector.get(d.detector)
                if cur is None or d.observed_at > cur.observed_at:
                    best_per_detector[d.detector] = d
            if len(best_per_detector) < self._min_distinct:
                continue
            signal = self._combine(mint, best_per_detector, now)
            out.append(signal)
            self._last_emit[mint] = now_ts
        return out

    def _combine(
        self,
        mint: str,
        per_detector: dict[str, DetectorSignal],
        now: datetime,
    ) -> Signal:
        detectors = tuple(sorted(per_detector.values(), key=lambda s: s.detector))
        weight_lookup = {
            "prepump": self._weights.prepump,
            "cluster": self._weights.cluster,
            "flow_anomaly": self._weights.flow_anomaly,
        }
        weighted_sum = sum(d.score * weight_lookup.get(d.detector, 0.0) for d in detectors)
        weight_total = sum(weight_lookup.get(d.detector, 0.0) for d in detectors)
        confidence = weighted_sum / weight_total if weight_total > 0 else 0.0
        confidence = max(0.0, min(1.0, confidence))
        now_ms = int(now.timestamp() * 1000)
        rationale = ", ".join(f"{d.detector}={d.score:.2f}" for d in detectors)
        return Signal(
            signal_id=new_signal_id(now_ms),
            created_at=now,
            mint=mint,
            direction="buy",
            detectors=detectors,
            confidence=confidence,
            suggested_usd=0.0,
            rationale=rationale,
            inputs_hash=self._hash(detectors, weight_lookup),
            trace_id=new_trace_id(now_ms),
        )

    @staticmethod
    def _hash(
        detectors: tuple[DetectorSignal, ...],
        weights: dict[str, float],
    ) -> str:
        payload = {
            "detectors": [
                {
                    "detector": d.detector,
                    "mint": d.mint,
                    "score": round(d.score, 6),
                    "features": {k: round(v, 6) for k, v in sorted(d.features.items())},
                    "observed_at": d.observed_at.isoformat(),
                }
                for d in detectors
            ],
            "weights": {k: round(v, 6) for k, v in sorted(weights.items())},
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


__all__ = ["ConfidenceCombiner"]
