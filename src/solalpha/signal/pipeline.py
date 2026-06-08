"""Signal pipeline -- the long-running worker that wires the signal plane.

Two cooperating async tasks share one detector set + combiner:
  * `_consume()` subscribes to `NORMALIZED_TOPIC`; every `NormalizedSwap`
    is fed to each detector's `observe()`.
  * `_emit()` polls the detectors and the combiner on a `poll_interval_s`
    cadence; every emitted `Signal` is sized, run through the risk engine,
    and -- if approved or scaled -- republished on `SIGNALS_TOPIC` for the
    execution plane. Approved signals also persist to the `signals` and
    `risk_decisions` SQLite tables for audit and replay.

The pipeline is mode-agnostic; the worker supervisor decides whether it
runs in the current mode. The risk engine itself rejects appropriately in
`HALT` (kill-switch armed) and `DEGRADED_RPC` (size-scaled via the sizer).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import anyio

from solalpha.foundation import metrics
from solalpha.foundation.bus import NORMALIZED_TOPIC, SIGNALS_TOPIC
from solalpha.foundation.logging import bind_trace_id, get_logger
from solalpha.signal.combiner import ConfidenceCombiner
from solalpha.signal.detectors import (
    ClusterDetector,
    Detector,
    FlowAnomalyDetector,
    PrePumpDetector,
)
from solalpha.signal.sizer import PortfolioSizer

if TYPE_CHECKING:
    from solalpha.domain import RiskDecision, Signal
    from solalpha.foundation.bus import Bus
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.config import AppConfig
    from solalpha.foundation.state import SqliteStore
    from solalpha.signal.mode_manager import ModeManager
    from solalpha.signal.risk_engine import RiskEngine
    from solalpha.signal.smart_wallet_scorer import SmartWalletScorer

_log = get_logger(__name__)


class SignalPipeline:
    """End-to-end signal plane: NORMALIZED -> Signal -> RiskDecision -> SIGNALS."""

    name = "signal_pipeline"
    modes: tuple[str, ...] = ()  # always-on (risk engine rejects per-mode)

    def __init__(
        self,
        cfg: AppConfig,
        bus: Bus,
        store: SqliteStore,
        clock: Clock,
        scorer: SmartWalletScorer,
        mode_manager: ModeManager,
        risk_engine: RiskEngine,
        *,
        poll_interval_s: float = 1.0,
    ) -> None:
        self._cfg = cfg
        self._bus = bus
        self._store = store
        self._clock = clock
        self._mode_manager = mode_manager
        self._risk = risk_engine
        self._poll_interval_s = poll_interval_s
        # Detectors share a single combiner + sizer instance.
        self._detectors: tuple[Detector, ...] = (
            PrePumpDetector(cfg.signals.prepump),
            ClusterDetector(cfg.signals.cluster, scorer),
            FlowAnomalyDetector(cfg.signals.flow_anomaly),
        )
        self._combiner = ConfidenceCombiner(cfg.signals.weights)
        self._sizer = PortfolioSizer(cfg, mode_manager)

    async def run(self) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._consume)
            tg.start_soon(self._emit)

    # ---- ingest loop ----

    async def _consume(self) -> None:
        topic = await self._bus.topic(NORMALIZED_TOPIC)
        async with topic.subscribe() as recv:
            async for swap in recv:
                for d in self._detectors:
                    try:
                        await d.observe(swap)
                    except Exception as e:
                        _log.warning(
                            "detector_observe_error",
                            detector=d.name,
                            exc=str(e),
                            exc_type=type(e).__name__,
                        )

    # ---- emit loop ----

    async def _emit(self) -> None:
        signals_topic = await self._bus.topic(SIGNALS_TOPIC)
        while True:
            await self._clock.sleep(self._poll_interval_s)
            now = self._clock.now()
            # Pull detector outputs into the combiner.
            for d in self._detectors:
                try:
                    for ds in d.poll(now):
                        metrics.SIGNALS_EMITTED.labels(detector=d.name).inc()
                        await self._combiner.add(ds)
                except Exception as e:
                    _log.warning(
                        "detector_poll_error",
                        detector=d.name,
                        exc=str(e),
                        exc_type=type(e).__name__,
                    )
            signals = self._combiner.poll(now)
            if not signals:
                continue
            for signal in signals:
                await self._process(signal, signals_topic)

    async def _process(self, signal: Signal, signals_topic: object) -> None:
        with bind_trace_id(signal.trace_id):
            sized = self._sizer.size(signal)
            decision = await self._risk.evaluate(sized)
            await self._persist_signal(sized)
            await self._persist_decision(decision)
            if decision.decision == "rejected":
                return
            approved = sized.model_copy(update={"suggested_usd": decision.approved_usd})
            publish = getattr(signals_topic, "publish", None)
            if publish is None:
                _log.error("signals_topic_invalid", type=type(signals_topic).__name__)
                return
            await publish(approved)

    # ---- persistence ----

    async def _persist_signal(self, signal: Signal) -> None:
        detectors_json = json.dumps(
            [d.model_dump(mode="json") for d in signal.detectors], sort_keys=True
        )
        await self._store.execute(
            """
            INSERT OR REPLACE INTO signals (
                signal_id, created_at, mint, direction, detectors_json,
                confidence, suggested_usd, rationale, inputs_hash, trace_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.signal_id,
                signal.created_at.isoformat(),
                signal.mint,
                signal.direction,
                detectors_json,
                signal.confidence,
                signal.suggested_usd,
                signal.rationale,
                signal.inputs_hash,
                signal.trace_id,
            ),
        )

    async def _persist_decision(self, decision: RiskDecision) -> None:
        reasons_json = json.dumps(list(decision.reasons))
        await self._store.execute(
            """
            INSERT OR REPLACE INTO risk_decisions (
                signal_id, decision, approved_usd, reasons_json, ts, mode_at_decision
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                decision.signal_id,
                decision.decision,
                decision.approved_usd,
                reasons_json,
                decision.ts.isoformat(),
                decision.mode_at_decision,
            ),
        )


__all__ = ["SignalPipeline"]
