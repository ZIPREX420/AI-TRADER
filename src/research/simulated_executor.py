"""Deterministic simulator of swap execution. Models latency, slippage, fees, drop rate."""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .clock import ReplayClock


@dataclass
class FrictionModel:
    """Friction parameters (calibrated from live data; these are conservative defaults)."""
    base_slippage_bps: float = 50.0           # always-applied
    impact_coef: float = 1.5                  # extra slippage = coef * (sol/tvl)
    fee_lamports: int = 250_000               # priority+jito+jup avg, in lamports
    jupiter_fee_pct: float = 0.0025           # 0.25% aggregator
    drop_rate: float = 0.05                   # 5% submissions don't land
    latency_ms_p50: float = 800.0
    latency_ms_p95: float = 2200.0

    def sample_latency_s(self, rng: random.Random) -> float:
        # Lognormal calibrated so median ≈ p50, ~p95
        mu = math.log(self.latency_ms_p50 / 1000.0)
        sigma = max(0.1, math.log(self.latency_ms_p95 / max(self.latency_ms_p50, 1.0)) / 1.645)
        return max(0.05, rng.lognormvariate(mu, sigma))


@dataclass
class SimSwapResult:
    ok: bool
    in_amount: int          # lamports (or token raw)
    out_amount: int
    price_sol: float        # sol per token at execution
    slippage_realized: float
    fee_lamports: int
    elapsed_ms: float
    error: Optional[str] = None
    submit_ts: float = 0.0
    confirm_ts: float = 0.0


@dataclass
class SimulatedExecutor:
    clock: ReplayClock
    price_at: Callable[[float, str], Optional[float]]   # (ts, mint) → SOL/token
    tvl_at: Callable[[float, str], float] = lambda ts, m: 30_000.0
    friction: FrictionModel = field(default_factory=FrictionModel)
    rng: random.Random = field(default_factory=lambda: random.Random(42))

    def buy(self, mint: str, sol_lamports: int, slippage_bps: int) -> SimSwapResult:
        return self._swap(in_mint="SOL", out_mint=mint,
                          amount=sol_lamports, slippage_bps=slippage_bps, side_buy=True)

    def sell(self, mint: str, token_raw: int, slippage_bps: int) -> SimSwapResult:
        return self._swap(in_mint=mint, out_mint="SOL",
                          amount=token_raw, slippage_bps=slippage_bps, side_buy=False)

    def _swap(self, *, in_mint: str, out_mint: str, amount: int, slippage_bps: int,
              side_buy: bool) -> SimSwapResult:
        submit_ts = self.clock.now()
        latency_s = self.friction.sample_latency_s(self.rng)
        self.clock.advance_by(latency_s)
        confirm_ts = self.clock.now()

        if self.rng.random() < self.friction.drop_rate:
            return SimSwapResult(
                ok=False, in_amount=amount, out_amount=0, price_sol=0.0,
                slippage_realized=0.0, fee_lamports=self.friction.fee_lamports,
                elapsed_ms=latency_s * 1000.0, error="dropped",
                submit_ts=submit_ts, confirm_ts=confirm_ts,
            )

        target_mint = out_mint if side_buy else in_mint
        price = self.price_at(confirm_ts, target_mint)
        if price is None or price <= 0:
            return SimSwapResult(
                ok=False, in_amount=amount, out_amount=0, price_sol=0.0,
                slippage_realized=0.0, fee_lamports=self.friction.fee_lamports,
                elapsed_ms=latency_s * 1000.0, error="no_price",
                submit_ts=submit_ts, confirm_ts=confirm_ts,
            )

        # Slippage = base + impact·(sol/tvl)
        if side_buy:
            sol_in = amount / 1e9
        else:
            sol_in = (amount * price)
        tvl_usd = self.tvl_at(confirm_ts, target_mint)
        sol_value_in_pool = max(tvl_usd / 150.0, 0.001)   # rough USD→SOL
        impact = self.friction.impact_coef * (sol_in / sol_value_in_pool)
        slip = self.friction.base_slippage_bps / 10_000.0 + impact
        # Cap at requested slippage tolerance — fail if exceeded
        if slip > slippage_bps / 10_000.0:
            return SimSwapResult(
                ok=False, in_amount=amount, out_amount=0, price_sol=price,
                slippage_realized=slip, fee_lamports=self.friction.fee_lamports,
                elapsed_ms=latency_s * 1000.0, error="slippage_exceeded",
                submit_ts=submit_ts, confirm_ts=confirm_ts,
            )

        if side_buy:
            tokens_per_sol = 1.0 / price
            gross = (amount / 1e9) * tokens_per_sol
            net = gross * (1.0 - slip) * (1.0 - self.friction.jupiter_fee_pct)
            net_raw = int(net * 1e9)
        else:
            sol_gross = (amount / 1e9) * price
            sol_net = sol_gross * (1.0 - slip) * (1.0 - self.friction.jupiter_fee_pct)
            net_raw = int(sol_net * 1e9) - self.friction.fee_lamports

        return SimSwapResult(
            ok=True, in_amount=amount, out_amount=max(0, net_raw),
            price_sol=price, slippage_realized=slip,
            fee_lamports=self.friction.fee_lamports,
            elapsed_ms=latency_s * 1000.0, error=None,
            submit_ts=submit_ts, confirm_ts=confirm_ts,
        )
