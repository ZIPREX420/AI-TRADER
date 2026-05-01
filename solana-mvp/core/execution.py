"""Executor abstraction + LiveExecutor (Jupiter/Raydium/Jito) + MockExecutor.

LiveExecutor orchestrates the full v9 flow:
    pre_check → quote(Jupiter) → fallback(Raydium) → route_selector.choose →
    tx_builder.parse + sign → submit (Jito for buys, RPC for sells) →
    confirm (dual-RPC race) → retry-with-bump (1×).

MockExecutor is deterministic with seeded RNG; preserves the same ExecResult contract
so the rest of the system is mode-agnostic.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import httpx

from . import execution_jupiter, execution_raydium, route_selector, submit, confirm, tx_builder
from .types import Candidate, ExecError, ExecResult, Quote, Side

log = logging.getLogger("execution")


# ─────────────────────────────────────────────────────────────────────────────
# Executor ABC
# ─────────────────────────────────────────────────────────────────────────────
class Executor(ABC):
    @abstractmethod
    async def submit_buy(self, mint: str, sol_lamports: int, candidate: Candidate) -> ExecResult: ...

    @abstractmethod
    async def submit_sell(self, mint: str, token_raw: int, candidate: Candidate) -> ExecResult: ...

    @abstractmethod
    async def get_balance_lamports(self) -> int: ...

    @property
    def kind(self) -> str:
        return self.__class__.__name__


# ─────────────────────────────────────────────────────────────────────────────
# MockExecutor (paper / sim)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class FrictionModel:
    base_slippage_bps: float = 50.0
    impact_coef: float = 1.5
    fee_lamports: int = 250_000
    jupiter_fee_pct: float = 0.0025
    drop_rate: float = 0.05
    latency_ms_p50: float = 800.0
    latency_ms_p95: float = 2200.0

    def sample_latency_s(self, rng: random.Random) -> float:
        mu = math.log(max(self.latency_ms_p50, 1.0) / 1000.0)
        sigma = max(0.1, math.log(max(self.latency_ms_p95, self.latency_ms_p50 + 1) /
                                  max(self.latency_ms_p50, 1.0)) / 1.645)
        return max(0.05, rng.lognormvariate(mu, sigma))


class MockExecutor(Executor):
    """Deterministic. price_at(ts, mint) → SOL/token (or None if unknown)."""

    def __init__(
        self,
        price_at: Callable[[float, str], Optional[float]],
        *,
        balance_lamports: int = 1_000_000_000,
        tvl_at: Callable[[float, str], float] = lambda t, m: 30_000.0,
        friction: Optional[FrictionModel] = None,
        seed: int = 42,
    ):
        self._price_at = price_at
        self._tvl_at = tvl_at
        self._friction = friction or FrictionModel()
        self._rng = random.Random(seed)
        self._balance = balance_lamports

    async def get_balance_lamports(self) -> int:
        return self._balance

    async def submit_buy(self, mint: str, sol_lamports: int, candidate: Candidate) -> ExecResult:
        return self._swap(in_mint="SOL", out_mint=mint, amount=sol_lamports, side_buy=True,
                          candidate=candidate)

    async def submit_sell(self, mint: str, token_raw: int, candidate: Candidate) -> ExecResult:
        return self._swap(in_mint=mint, out_mint="SOL", amount=token_raw, side_buy=False,
                          candidate=candidate)

    def _swap(self, *, in_mint: str, out_mint: str, amount: int,
              side_buy: bool, candidate: Candidate) -> ExecResult:
        submit_ts = time.time()
        latency_s = self._friction.sample_latency_s(self._rng)
        confirm_ts = submit_ts + latency_s

        if self._rng.random() < self._friction.drop_rate:
            return ExecResult(
                ok=False, in_amount=amount, out_amount=0,
                error=ExecError.DROPPED.value,
                elapsed_ms=latency_s * 1000.0,
                submit_ts=submit_ts, confirm_ts=confirm_ts,
                fee_lamports=self._friction.fee_lamports,
                route_source="mock", mode="paper",
            )

        target_mint = out_mint if side_buy else in_mint
        price = self._price_at(confirm_ts, target_mint)
        if price is None or price <= 0:
            return ExecResult(
                ok=False, in_amount=amount, out_amount=0,
                error=ExecError.NO_ROUTE.value,
                elapsed_ms=latency_s * 1000.0,
                submit_ts=submit_ts, confirm_ts=confirm_ts,
                fee_lamports=self._friction.fee_lamports,
                route_source="mock", mode="paper",
            )

        if side_buy:
            sol_in = amount / 1e9
        else:
            sol_in = amount * price
        tvl_usd = max(self._tvl_at(confirm_ts, target_mint), 1.0)
        sol_value_in_pool = max(tvl_usd / 150.0, 0.001)
        impact = self._friction.impact_coef * (sol_in / sol_value_in_pool)
        slip = self._friction.base_slippage_bps / 10_000.0 + impact

        if side_buy:
            tokens_per_sol = 1.0 / price
            gross = (amount / 1e9) * tokens_per_sol
            net = gross * (1.0 - slip) * (1.0 - self._friction.jupiter_fee_pct)
            net_raw = max(0, int(net * 1e9))
            self._balance = max(0, self._balance - amount - self._friction.fee_lamports)
        else:
            sol_gross = (amount / 1e9) * price
            sol_net = sol_gross * (1.0 - slip) * (1.0 - self._friction.jupiter_fee_pct)
            net_raw = max(0, int(sol_net * 1e9) - self._friction.fee_lamports)
            self._balance += net_raw

        return ExecResult(
            ok=True,
            in_amount=amount,
            out_amount=net_raw,
            sig=None,
            price_sol=price,
            slippage_realized=slip,
            fee_lamports=self._friction.fee_lamports,
            elapsed_ms=latency_s * 1000.0,
            submit_ts=submit_ts,
            confirm_ts=confirm_ts,
            route_source="mock",
            mode="paper",
        )


# ─────────────────────────────────────────────────────────────────────────────
# LiveExecutor
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class _LiveDeps:
    """Injectable dependencies for testability."""
    quote_jupiter: Callable[..., Awaitable[Optional[Quote]]] = execution_jupiter.quote
    quote_raydium: Callable[..., Awaitable[Optional[Quote]]] = execution_raydium.quote
    swap_tx_jupiter: Callable[..., Awaitable[Optional[str]]] = execution_jupiter.swap_tx
    swap_tx_raydium: Callable[..., Awaitable[Optional[str]]] = execution_raydium.swap_tx
    submit_bundle: Callable[..., Awaitable[Optional[str]]] = submit.submit_jito_bundle
    submit_rpc: Callable[..., Awaitable[Optional[str]]] = submit.submit_rpc
    confirm_signature: Callable[..., Awaitable[bool]] = confirm.confirm_signature


class LiveExecutor(Executor):
    """Production executor. Async. Strict timeouts everywhere. Persists pending sigs.

    Requires an RPC client (`SolanaRPC`) with `best_http()` and `all_http()` and a
    keypair (or test signer with `.pubkey()`). Uses callable hooks for HTTP boundaries
    so tests can substitute fakes.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        rpc,                       # SolanaRPC
        keypair,                   # solders.Keypair
        jito_url: str,
        jito_tip_lamports: int,
        slippage_bps: int = 500,
        max_pi_buy: float = 0.04,
        max_pi_sell: float = 0.08,
        confirm_timeout_s: float = 30.0,
        retry_on_not_confirmed: bool = True,
        risk_exec=None,
        deps: Optional[_LiveDeps] = None,
        # Optional override for the signing/serialization pipeline (tests).
        sign_and_serialize: Optional[Callable[[str, Any], tuple[bytes, str, str]]] = None,
        # Override for blockhash fetch (tests).
        get_recent_blockhash: Optional[Callable[[], Awaitable[Any]]] = None,
        # Override for tip-tx serialization (tests).
        build_tip_tx_b58: Optional[Callable[[int, Any], Awaitable[str]]] = None,
    ):
        self._http = http
        self._rpc = rpc
        self._kp = keypair
        self._jito_url = jito_url
        self._jito_tip_lamports = int(jito_tip_lamports)
        self._slippage_bps = int(slippage_bps)
        self._max_pi_buy = max_pi_buy
        self._max_pi_sell = max_pi_sell
        self._confirm_timeout_s = confirm_timeout_s
        self._retry = retry_on_not_confirmed
        self._risk = risk_exec
        self._deps = deps or _LiveDeps()
        self._sign_and_serialize = sign_and_serialize
        self._get_recent_blockhash = get_recent_blockhash
        self._build_tip_tx_b58 = build_tip_tx_b58

    @property
    def user_pubkey(self) -> str:
        try:
            return str(self._kp.pubkey())
        except Exception:
            return "unknown"

    async def get_balance_lamports(self) -> int:
        return 0

    async def submit_buy(self, mint: str, sol_lamports: int, candidate: Candidate) -> ExecResult:
        return await self._swap(
            in_mint="So11111111111111111111111111111111111111112",
            out_mint=mint, amount=sol_lamports, side=Side.BUY, candidate=candidate,
        )

    async def submit_sell(self, mint: str, token_raw: int, candidate: Candidate) -> ExecResult:
        return await self._swap(
            in_mint=mint, out_mint="So11111111111111111111111111111111111111112",
            amount=token_raw, side=Side.SELL, candidate=candidate,
        )

    # ────────── orchestration ──────────
    async def _swap(self, *, in_mint: str, out_mint: str, amount: int,
                    side: Side, candidate: Candidate) -> ExecResult:
        t0 = time.time()
        slip = self._slippage_bps

        # 1) Quote — Jupiter primary, Raydium fallback
        jq, rq = await asyncio.gather(
            self._safe(self._deps.quote_jupiter(self._http, in_mint, out_mint, amount, slip)),
            self._safe(self._deps.quote_raydium(self._http, in_mint, out_mint, amount, slip)),
            return_exceptions=False,
        )
        chosen = route_selector.choose(jq, rq, side, self._max_pi_buy, self._max_pi_sell)
        if chosen is None:
            return self._fail(amount, ExecError.NO_ROUTE, t0)

        # 2) Swap tx (build via the chosen source)
        if chosen.source == "jupiter":
            tx_b64 = await self._safe(self._deps.swap_tx_jupiter(
                self._http, chosen.raw, self.user_pubkey, prio_lamports="auto",
            ))
        else:
            tx_b64 = await self._safe(self._deps.swap_tx_raydium(
                self._http, chosen.raw, self.user_pubkey, prio_lamports="auto",
            ))
        if not tx_b64:
            return self._fail(amount, ExecError.QUOTE_FAILED, t0)

        # 3) Sign + serialize (and optionally build Jito tip tx for buys)
        try:
            signed_b58, signature, signed_bytes_hex = await self._sign_pipeline(tx_b64)
        except Exception as e:
            log.warning(f"sign pipeline failed: {type(e).__name__}: {e}")
            return self._fail(amount, ExecError.SIGN_FAILED, t0)

        # 4) Submit
        attempt = 0
        max_attempts = 2 if self._retry else 1
        last_error = ExecError.SUBMIT_FAILED.value
        sig_str = signature
        while attempt < max_attempts:
            attempt += 1
            sig_or_err = await self._submit_one(side, signed_b58, signed_bytes_hex)
            if sig_or_err is None or isinstance(sig_or_err, str) and sig_or_err.startswith("ERR:"):
                last_error = (sig_or_err or ExecError.SUBMIT_FAILED.value).removeprefix("ERR:")
                if self._risk:
                    self._risk.record_submit_outcome(False, error=last_error)
                if attempt < max_attempts:
                    await asyncio.sleep(0.5)
                    continue
                return self._fail(amount, last_error, t0, partial_route=chosen)
            sig_str = sig_or_err

            # 5) Confirm
            urls = self._rpc.all_http() if self._rpc else []
            ok = await self._deps.confirm_signature(self._http, urls, sig_str,
                                                    timeout_s=self._confirm_timeout_s)
            if ok:
                if self._risk:
                    self._risk.record_submit_outcome(True)
                    self._risk.remove_pending(sig_str)
                elapsed = (time.time() - t0) * 1000.0
                return ExecResult(
                    ok=True, in_amount=amount, out_amount=int(chosen.out_amount),
                    sig=sig_str, price_sol=self._derive_price_sol(chosen, side),
                    slippage_realized=float(chosen.price_impact_pct),
                    fee_lamports=0, elapsed_ms=elapsed,
                    submit_ts=t0, confirm_ts=time.time(),
                    route_source=chosen.source, mode="live",
                )
            # not confirmed — record and possibly retry-with-bump
            if self._risk:
                self._risk.record_submit_outcome(False, error=ExecError.NOT_CONFIRMED.value)
                self._risk.remove_pending(sig_str)
            last_error = ExecError.NOT_CONFIRMED.value
            if attempt < max_attempts:
                await asyncio.sleep(0.4)
                # On retry, re-quote (price may have moved) and re-sign
                rq_jup = await self._safe(self._deps.quote_jupiter(
                    self._http, in_mint, out_mint, amount, int(slip * 1.10),
                ))
                if rq_jup is not None and rq_jup.out_amount > 0:
                    chosen = rq_jup
                tx_b64 = await self._safe(self._deps.swap_tx_jupiter(
                    self._http, chosen.raw, self.user_pubkey, prio_lamports="auto",
                ))
                if not tx_b64:
                    return self._fail(amount, ExecError.QUOTE_FAILED, t0, partial_route=chosen)
                signed_b58, signature, signed_bytes_hex = await self._sign_pipeline(tx_b64)
                continue
        return self._fail(amount, last_error, t0, partial_route=chosen)

    # ────────── helpers ──────────
    async def _sign_pipeline(self, tx_b64: str) -> tuple[str, str, str]:
        """Returns (b58_signed, signature_str, hex_signed_bytes). Test override hook honored."""
        if self._sign_and_serialize is not None:
            res = self._sign_and_serialize(tx_b64, self._kp)
            if asyncio.iscoroutine(res):
                res = await res
            tx_bytes, b58, sig_str = res  # type: ignore[misc]
            return b58, sig_str, tx_bytes.hex()

        unsigned = tx_builder.parse_swap_tx_b64(tx_b64)
        signed = tx_builder.sign_versioned(unsigned, self._kp)
        b58 = tx_builder.serialize_b58(signed)
        sig_str = tx_builder.signature_str(signed)
        return b58, sig_str, bytes(signed).hex()

    async def _submit_one(self, side: Side, signed_b58: str, signed_hex: str) -> Optional[str]:
        try:
            if side == Side.BUY and self._jito_url:
                tip_b58 = await self._build_tip_b58()
                txs = [signed_b58]
                if tip_b58:
                    txs.append(tip_b58)
                bundle_id = await self._deps.submit_bundle(self._http, self._jito_url, txs)
                if not bundle_id:
                    # fall back to RPC submit
                    sig = await self._deps.submit_rpc(
                        self._http, self._rpc.best_http() or "", bytes.fromhex(signed_hex),
                        skip_preflight=False,
                    )
                    return sig if sig else None
                # The signature for the swap tx == its first signature; we deliberately do
                # NOT use the bundle_id as the tx identifier.
                sig = signed_b58_to_sig(signed_b58)
                if self._risk and sig:
                    self._risk.add_pending(sig, mint="?", fee=0, mode="live")
                return sig
            else:
                sig = await self._deps.submit_rpc(
                    self._http, self._rpc.best_http() or "", bytes.fromhex(signed_hex),
                    skip_preflight=False,
                )
                if self._risk and sig:
                    self._risk.add_pending(sig, mint="?", fee=0, mode="live")
                return sig
        except Exception as e:
            log.warning(f"submit error: {type(e).__name__}: {e}")
            return None

    async def _build_tip_b58(self) -> Optional[str]:
        if self._jito_tip_lamports <= 0:
            return None
        if self._build_tip_tx_b58 is not None:
            try:
                bh = await (self._get_recent_blockhash() if self._get_recent_blockhash else _noop_blockhash())
                res = self._build_tip_tx_b58(self._jito_tip_lamports, bh)
                if asyncio.iscoroutine(res):
                    res = await res
                return res  # type: ignore[return-value]
            except Exception:
                return None
        # Real path requires a recent blockhash from the chain
        if self._get_recent_blockhash is None:
            return None
        try:
            bh = await self._get_recent_blockhash()
            tip_tx = tx_builder.build_tip_tx(self._kp, self._jito_tip_lamports, bh)
            return tx_builder.serialize_b58(tip_tx)
        except Exception:
            return None

    async def _safe(self, awaitable: Any) -> Any:
        try:
            return await awaitable
        except Exception as e:
            log.warning(f"safe-await error: {type(e).__name__}: {e}")
            return None

    def _fail(self, amount: int, error: str, t0: float,
              partial_route: Optional[Quote] = None) -> ExecResult:
        return ExecResult(
            ok=False, in_amount=amount, out_amount=0, error=error,
            elapsed_ms=(time.time() - t0) * 1000.0,
            submit_ts=t0, confirm_ts=time.time(),
            route_source=partial_route.source if partial_route else "",
            mode="live",
        )

    @staticmethod
    def _derive_price_sol(q: Quote, side: Side) -> float:
        if q.in_amount <= 0 or q.out_amount <= 0:
            return 0.0
        if side == Side.BUY:
            # SOL→token: price (sol per token) = sol_in_lamports / token_out_raw  → assume 9 decimals each
            return (q.in_amount / 1e9) / max(q.out_amount / 1e9, 1e-12)
        else:
            return (q.out_amount / 1e9) / max(q.in_amount / 1e9, 1e-12)


async def _noop_blockhash():
    return None


def signed_b58_to_sig(signed_b58: str) -> str:
    """Best-effort: derive the signature string from a base58-serialized VersionedTransaction.

    The first 64 bytes of a serialized v0 transaction (after the 1-byte signature count)
    are the first signature. We don't decode here; the test/runtime path uses
    `tx_builder.signature_str` from the signed object directly. This helper exists for
    the bundle-submit codepath where we already only have the b58 form.
    """
    try:
        import base58
        raw = base58.b58decode(signed_b58)
        if len(raw) < 65:
            return ""
        sig_bytes = raw[1:65]
        return base58.b58encode(sig_bytes).decode("ascii")
    except Exception:
        return ""
