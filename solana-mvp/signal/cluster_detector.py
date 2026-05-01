"""Per-mint rolling 90s window. Fires CLUSTER_HIT/EARLY_FLOCK/STAIR/PRE_INFLOW signals."""
from __future__ import annotations

import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.types import PrePumpSignal, Side, WalletEvent


@dataclass
class _Entry:
    ts: float
    wallet: str
    cluster_id: str
    sol_amount: float
    instruction_byte_hash: Optional[str] = None
    fee_payer: Optional[str] = None


def _wallet_age_default(_pubkey: str) -> float:
    return float("inf")


@dataclass
class ClusterDetector:
    wallet_age: Callable[[str], float] = field(default=_wallet_age_default)
    cluster_of: Callable[[str], str] = field(default=lambda w: w[:6])
    window_s: float = 90.0
    early_flock_window_s: float = 60.0
    stair_window_s: float = 120.0
    preinflow_window_s: float = 30.0
    cluster_min_sol: float = 5.0
    fired: dict[str, set[str]] = field(default_factory=dict)
    _state: dict[str, deque] = field(default_factory=dict)

    def _evict(self, mint: str, now: float) -> None:
        dq = self._state.get(mint)
        if dq is None:
            return
        cutoff = now - max(self.window_s, self.stair_window_s)
        while dq and dq[0].ts < cutoff:
            dq.popleft()

    def _has_been_fired(self, mint: str, kind: str) -> bool:
        return kind in self.fired.get(mint, set())

    def _mark_fired(self, mint: str, kind: str) -> None:
        self.fired.setdefault(mint, set()).add(kind)

    def update(
        self,
        ev: WalletEvent,
        instruction_byte_hash: Optional[str] = None,
        fee_payer: Optional[str] = None,
    ) -> Optional[PrePumpSignal]:
        if ev.side != Side.BUY:
            return None
        now = ev.ts
        cluster_id = self.cluster_of(ev.wallet)
        dq = self._state.setdefault(ev.mint, deque(maxlen=400))
        dq.append(
            _Entry(
                ts=now,
                wallet=ev.wallet,
                cluster_id=cluster_id,
                sol_amount=ev.sol_amount,
                instruction_byte_hash=instruction_byte_hash,
                fee_payer=fee_payer,
            )
        )
        self._evict(ev.mint, now)

        # Anti-bot guard: if every recent entry shares the same byte-hash or fee_payer → reject
        recent = [e for e in dq if now - e.ts <= self.window_s]
        if len(recent) >= 3 and self._anti_bot_uniform(recent):
            return None

        for check in (self._check_cluster_hit, self._check_early_flock, self._check_stair, self._check_preinflow):
            sig = check(ev.mint, recent, now)
            if sig is not None:
                if not self._has_been_fired(ev.mint, sig.kind):
                    self._mark_fired(ev.mint, sig.kind)
                    return sig
        return None

    @staticmethod
    def _anti_bot_uniform(recent: list[_Entry]) -> bool:
        hashes = {e.instruction_byte_hash for e in recent if e.instruction_byte_hash}
        payers = {e.fee_payer for e in recent if e.fee_payer}
        if hashes and len(hashes) == 1:
            return True
        if payers and len(payers) == 1 and len(recent) >= 4:
            return True
        return False

    # ───────────── rule checks ─────────────
    def _check_cluster_hit(self, mint: str, recent: list[_Entry], now: float) -> Optional[PrePumpSignal]:
        in_window = [e for e in recent if now - e.ts <= self.window_s]
        clusters = {e.cluster_id for e in in_window}
        total_sol = sum(e.sol_amount for e in in_window)
        if len(clusters) >= 3 and total_sol >= self.cluster_min_sol:
            return PrePumpSignal(
                kind="CLUSTER_HIT",
                mint=mint,
                magnitude=total_sol,
                ts=now,
                wallets=[e.wallet for e in in_window],
            )
        return None

    def _check_early_flock(self, mint: str, recent: list[_Entry], now: float) -> Optional[PrePumpSignal]:
        in_window = [e for e in recent if now - e.ts <= self.early_flock_window_s]
        eligible = [
            e for e in in_window
            if 0.05 <= e.sol_amount <= 1.0 and self.wallet_age(e.wallet) < 86_400
        ]
        unique = {e.wallet for e in eligible}
        if len(unique) >= 5:
            return PrePumpSignal(
                kind="EARLY_FLOCK",
                mint=mint,
                magnitude=float(len(unique)),
                ts=now,
                wallets=list(unique),
            )
        return None

    def _check_stair(self, mint: str, recent: list[_Entry], now: float) -> Optional[PrePumpSignal]:
        in_window = [e for e in recent if now - e.ts <= self.stair_window_s]
        in_window.sort(key=lambda e: e.ts)
        if len(in_window) < 4:
            return None
        sizes = [e.sol_amount for e in in_window]
        # monotonic increase from index 0
        for i in range(1, len(sizes)):
            if sizes[i] <= sizes[i - 1]:
                return None
        if sum(sizes) < 2.0:
            return None
        # stride variance σ > 0.1
        strides = [sizes[i] - sizes[i - 1] for i in range(1, len(sizes))]
        if len(strides) < 2:
            return None
        stride_std = statistics.stdev(strides)
        if stride_std <= 0.1:
            return None
        return PrePumpSignal(
            kind="STAIR",
            mint=mint,
            magnitude=stride_std,
            ts=now,
            wallets=[e.wallet for e in in_window],
        )

    def _check_preinflow(self, mint: str, recent: list[_Entry], now: float) -> Optional[PrePumpSignal]:
        in_window = [e for e in recent if now - e.ts <= self.preinflow_window_s]
        eligible = [e for e in in_window if 0.1 <= e.sol_amount <= 0.5]
        unique = {e.wallet for e in eligible}
        if len(unique) >= 3:
            return PrePumpSignal(
                kind="PRE_INFLOW",
                mint=mint,
                magnitude=float(len(unique)),
                ts=now,
                wallets=list(unique),
            )
        return None

    def reset(self) -> None:
        self._state.clear()
        self.fired.clear()
