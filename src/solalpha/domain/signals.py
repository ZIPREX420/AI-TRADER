"""Signal-plane domain models: detector output, combined signals, risk verdicts.

Flow: each detector emits `DetectorSignal`s; the combiner blends them into a
`Signal` (sized, with an `inputs_hash` for deterministic replay); the risk
engine emits a `RiskDecision`. Fields mirror the `signals` and `risk_decisions`
SQLite tables in `foundation/persistence_schema.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from solalpha.foundation.config import ModeStr
from solalpha.foundation.ids import SignalId, TraceId

Direction = Literal["buy", "sell"]
DetectorName = Literal["prepump", "cluster", "flow_anomaly"]
RiskVerdict = Literal["approved", "scaled", "rejected"]


class DetectorSignal(BaseModel):
    """Output of a single detector for a single mint."""

    model_config = ConfigDict(frozen=True)

    detector: DetectorName
    mint: str
    score: float
    features: dict[str, float] = Field(default_factory=dict)
    observed_at: datetime


class Signal(BaseModel):
    """A combined, sized trade signal -- the input to the risk engine.

    `inputs_hash` is a stable digest of every detector input that produced this
    signal; deterministic replay asserts identical hashes across runs.
    """

    model_config = ConfigDict(frozen=True)

    signal_id: SignalId
    created_at: datetime
    mint: str
    direction: Direction
    detectors: tuple[DetectorSignal, ...]
    confidence: float
    suggested_usd: float
    rationale: str
    inputs_hash: str
    trace_id: TraceId


class RiskDecision(BaseModel):
    """The risk engine's verdict on a `Signal`.

    `decision` is `approved` (full size), `scaled` (reduced size, e.g. in
    `DEGRADED_RPC`), or `rejected`. `reasons` always carries the triggering
    rule(s); on `RiskInternalError` the engine fails closed with `rejected`.
    """

    model_config = ConfigDict(frozen=True)

    signal_id: SignalId
    decision: RiskVerdict
    approved_usd: float
    reasons: tuple[str, ...] = ()
    ts: datetime
    mode_at_decision: ModeStr


__all__ = [
    "DetectorName",
    "DetectorSignal",
    "Direction",
    "RiskDecision",
    "RiskVerdict",
    "Signal",
]
