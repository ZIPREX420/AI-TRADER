"""Shared dataclasses + enums. Pure types, no I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Mode(str, Enum):
    LIVE = "LIVE"
    DEGRADED_RPC = "DEGRADED_RPC"
    DEGRADED_EXEC = "DEGRADED_EXEC"
    PAPER = "PAPER"
    HALT = "HALT"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class ExecError(str, Enum):
    QUOTE_FAILED = "quote_failed"
    NO_ROUTE = "no_route"
    PRICE_IMPACT_EXCEEDED = "price_impact_exceeded"
    BUILD_FAILED = "build_failed"
    SIGN_FAILED = "sign_failed"
    SUBMIT_FAILED = "submit_failed"
    DROPPED = "dropped"
    NOT_CONFIRMED = "not_confirmed"
    SLIPPAGE_EXCEEDED = "slippage_exceeded"
    BALANCE_LOW = "balance_low"
    QUARANTINED = "quarantined"
    HALTED = "halted"


@dataclass
class Event:
    ts: float
    slot: int
    signature: str
    kind: str  # log | swap | pool | transfer
    source: str  # endpoint name
    mention: str
    raw_logs: list[str] = field(default_factory=list)
    block_time: Optional[float] = None


@dataclass
class WalletEvent:
    ts: float
    slot: int
    signature: str
    wallet: str
    mint: str
    side: Side
    sol_amount: float
    token_amount: float
    price_sol: float


@dataclass
class PrePumpSignal:
    kind: str  # CLUSTER_HIT | EARLY_FLOCK | STAIR | PRE_INFLOW
    mint: str
    magnitude: float
    ts: float
    wallets: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    mint: str
    confidence: float
    pattern_id: Optional[str]
    fingerprint: str
    source_kinds: list[str] = field(default_factory=list)
    wallets: list[str] = field(default_factory=list)
    cluster_ids: list[str] = field(default_factory=list)
    side: Side = Side.BUY


@dataclass
class Quote:
    in_mint: str
    out_mint: str
    in_amount: int
    out_amount: int
    price_impact_pct: float
    route: str  # human-readable summary
    source: str  # "jupiter" | "raydium"
    raw: Any = None  # opaque payload (dict for jupiter)


@dataclass
class ExecResult:
    ok: bool
    in_amount: int
    out_amount: int
    sig: Optional[str] = None
    price_sol: float = 0.0
    slippage_realized: float = 0.0
    fee_lamports: int = 0
    elapsed_ms: float = 0.0
    error: Optional[str] = None
    submit_ts: float = 0.0
    confirm_ts: float = 0.0
    route_source: str = ""
    mode: str = "live"


@dataclass
class Position:
    strategy: str
    mint: str
    sol_in: float
    tokens: float
    entry_price_sol: float
    opened_at: float
    high_water: float
    score: float
    sold_pct: float = 0.0
    pattern_id: Optional[str] = None


@dataclass
class Decision:
    ok: bool
    reason: str = "ok"


@dataclass
class ModeState:
    mode: Mode
    since_ts: float
    reason: str
    manual_override: bool = False
    history: list[dict] = field(default_factory=list)


@dataclass
class HealthSample:
    ts: float
    ok: bool
    latency_ms: float = 0.0
