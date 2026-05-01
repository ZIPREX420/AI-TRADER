"""Combine wallet signals + token filter + bridge-pattern + cluster context → Candidate."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.types import Candidate, PrePumpSignal, Side, WalletEvent


@dataclass
class _PatternMatch:
    pattern_id: str
    rank_score: float
    threshold: float = 0.55


@dataclass
class _Features:
    has_cluster: int = 0
    prepump_kind: int = 0  # 0=none, 1=CLUSTER_HIT, 2=EARLY_FLOCK, 3=STAIR, 4=PRE_INFLOW
    high_wallet_score: int = 0
    regime: str = "N"
    high_entropy: int = 0
    route_compression: int = 0
    lp_injection: int = 0
    pumpfun_grad: int = 0


def _fingerprint(f: _Features) -> str:
    return "".join([
        str(f.has_cluster),
        str(f.prepump_kind),
        str(f.high_wallet_score),
        f.regime,
        str(f.high_entropy),
        str(f.route_compression),
        str(f.lp_injection),
        str(f.pumpfun_grad),
    ])


_PREPUMP_KIND_INDEX = {
    "CLUSTER_HIT": 1,
    "EARLY_FLOCK": 2,
    "STAIR": 3,
    "PRE_INFLOW": 4,
}


@dataclass
class SignalEngine:
    bridge_match: Callable[[str], Optional[_PatternMatch]] = field(default=lambda fp: None)
    wallet_score: Callable[[str], float] = field(default=lambda w: 0.0)
    token_safety: Callable[[str], float] = field(default=lambda mint: 0.8)
    cluster_count: Callable[[str], int] = field(default=lambda mint: 0)
    regime: str = "NORMAL"
    threshold_floor: float = 0.55
    weights: tuple[float, float, float, float] = (0.40, 0.25, 0.20, 0.15)

    def evaluate(
        self,
        wallet_event: Optional[WalletEvent] = None,
        prepump: Optional[PrePumpSignal] = None,
        regime_override: Optional[str] = None,
        extra_signals: Optional[dict] = None,
    ) -> Optional[Candidate]:
        mint = (wallet_event.mint if wallet_event else None) or (prepump.mint if prepump else None)
        if not mint:
            return None
        wallets: list[str] = []
        if wallet_event and wallet_event.side == Side.BUY:
            wallets.append(wallet_event.wallet)
        if prepump and prepump.wallets:
            for w in prepump.wallets:
                if w not in wallets:
                    wallets.append(w)
        # Channel W: max wallet score among triggers
        scores = [float(self.wallet_score(w)) for w in wallets]
        W = max(scores) if scores else 0.0
        # Channel T: token safety (rug filter pass margin) ∈ [0,1]
        T = float(self.token_safety(mint))
        # Channel C: cluster recency
        cl_count = int(self.cluster_count(mint))
        C = 1.0 if cl_count >= 3 else (0.5 if cl_count >= 2 else 0.0)
        # Channel P: bridge match score
        feat = _Features(
            has_cluster=1 if cl_count >= 3 else 0,
            prepump_kind=_PREPUMP_KIND_INDEX.get(prepump.kind, 0) if prepump else 0,
            high_wallet_score=1 if W > 0.7 else 0,
            regime=(regime_override or self.regime)[:1],
            high_entropy=int(bool((extra_signals or {}).get("entropy", 0) > 3.5)),
            route_compression=int(bool((extra_signals or {}).get("route_compression"))),
            lp_injection=int(bool((extra_signals or {}).get("lp_injection"))),
            pumpfun_grad=int(bool((extra_signals or {}).get("pumpfun_grad"))),
        )
        fp = _fingerprint(feat)
        match = self.bridge_match(fp) if self.bridge_match else None
        P = float(getattr(match, "rank_score", 0.0)) if match else 0.0

        w1, w2, w3, w4 = self.weights
        confidence = w1 * W + w2 * T + w3 * P + w4 * C

        threshold = max(self.threshold_floor, float(getattr(match, "threshold", self.threshold_floor)))
        if confidence < threshold:
            return None

        source_kinds = []
        if wallet_event and wallet_event.side == Side.BUY:
            source_kinds.append("wallet_buy")
        if prepump:
            source_kinds.append(prepump.kind.lower())
        if match:
            source_kinds.append(f"pattern:{match.pattern_id}")

        return Candidate(
            mint=mint,
            confidence=round(confidence, 4),
            pattern_id=getattr(match, "pattern_id", None),
            fingerprint=fp,
            source_kinds=source_kinds,
            wallets=wallets,
            cluster_ids=[],
            side=Side.BUY,
        )
