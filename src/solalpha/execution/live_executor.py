"""Live-mode executor.

Composes the full execution path: route selector picks Jupiter/Raydium,
the venue returns swap instructions, the tx builder produces a signed
versioned tx, the retry-with-bump loop submits and confirms, and the
stuck-tx resolver picks up anything left over.

Live execution is **only entered when** `cfg.is_live_eligible()` is True,
i.e. both `live_trading: true` in YAML AND `SOLALPHA_LIVE_TRADING=1` in
env. The execution pipeline picks the paper executor otherwise.

On every order the executor records the parent `Order` row to SQLite and
adds the signature to `stuck_signatures` if confirmation fails within
the per-attempt budget. The risk engine's inflight counter for the mint
is decremented via `on_order_resolved` regardless of success/failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from solalpha.data.decoder import WSOL_MINT
from solalpha.domain import Fill, Order, Route
from solalpha.execution.confirmation import Confirmer
from solalpha.execution.retry_bump import RetryBumpExecutor
from solalpha.foundation import metrics
from solalpha.foundation.errors import (
    ExecutionFailed,
    RpcError,
    StuckTransaction,
)
from solalpha.foundation.ids import new_fill_id, new_order_id
from solalpha.foundation.logging import bind_trace_id, get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from solalpha.data.rpc_pool import RpcPool
    from solalpha.domain import OrderIntent
    from solalpha.execution.base import Quote, SwapInstructions
    from solalpha.execution.jupiter import JupiterClient
    from solalpha.execution.raydium import RaydiumClient
    from solalpha.execution.route_selector import RouteSelector
    from solalpha.execution.tx_builder import BuiltTx, TxBuilder
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.config import AppConfig
    from solalpha.foundation.secrets import KeypairLoader
    from solalpha.foundation.state import SqliteStore

_log = get_logger(__name__)


class LiveExecutor:
    """End-to-end live executor. Only callable when live-eligible."""

    name = "live_executor"

    def __init__(
        self,
        cfg: AppConfig,
        rpc: RpcPool,
        store: SqliteStore,
        clock: Clock,
        keypair_loader: KeypairLoader,
        route_selector: RouteSelector,
        jupiter: JupiterClient,
        raydium: RaydiumClient | None,
        tx_builder: TxBuilder,
        on_order_resolved: Callable[[str], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._rpc = rpc
        self._store = store
        self._clock = clock
        self._keypair_loader = keypair_loader
        self._route_selector = route_selector
        self._jupiter = jupiter
        self._raydium = raydium
        self._tx_builder = tx_builder
        self._confirmer = Confirmer(
            rpc,
            clock,
            timeout_s=cfg.execution.confirmation_timeout_s,
            poll_interval_s=cfg.execution.confirmation_poll_interval_s,
        )
        self._retry = RetryBumpExecutor(cfg.execution)
        self._on_resolved = on_order_resolved or (lambda _mint: None)

    async def execute(self, intent: OrderIntent) -> tuple[Order, Fill]:
        with bind_trace_id(intent.trace_id):
            return await self._execute(intent)

    async def _execute(self, intent: OrderIntent) -> tuple[Order, Fill]:
        if not self._cfg.is_live_eligible():
            raise ExecutionFailed("live executor invoked but not live-eligible")
        now = self._clock.now()
        now_ms = int(now.timestamp() * 1000)
        order = Order(
            order_id=new_order_id(now_ms),
            signal_id=intent.signal_id,
            created_at=now,
            mint=intent.mint,
            direction=intent.direction,
            intended_usd=intent.intended_usd,
            intended_input_amount_raw=intent.intended_input_amount_raw,
            max_slippage_bps=intent.max_slippage_bps,
            status="building",
            trace_id=intent.trace_id,
        )
        await self._persist_order(order)

        if intent.direction == "buy":
            input_mint, output_mint = WSOL_MINT, intent.mint
        else:
            input_mint, output_mint = intent.mint, WSOL_MINT

        user_pubkey = self._user_pubkey()

        async def build_fn(fee_lamports: int, slippage_bps: int) -> BuiltTx:
            quote = await self._quote_for(
                input_mint=input_mint,
                output_mint=output_mint,
                amount_raw=intent.intended_input_amount_raw,
                slippage_bps=slippage_bps,
            )
            ix = await self._swap_instructions(
                quote=quote,
                user_pubkey=user_pubkey,
                fee_lamports=fee_lamports,
            )
            return await self._tx_builder.build(ix=ix, priority_fee_microlamports=fee_lamports)

        async def submit_fn(built: BuiltTx) -> str:
            return await self._submit(built, order)

        async def confirm_fn(signature: str) -> dict[str, object]:
            return await self._confirmer.confirm(signature)

        try:
            built, signature, _ = await self._retry.run(
                base_slippage_bps=intent.max_slippage_bps,
                build_fn=build_fn,
                submit_fn=submit_fn,
                confirm_fn=confirm_fn,
            )
        except StuckTransaction as e:
            await self._mark_stuck(order, e.signature or "")
            self._on_resolved(intent.mint)
            metrics.ORDERS_TOTAL.labels(status="stuck").inc()
            raise
        except Exception as e:
            await self._mark_failed(order, str(e))
            self._on_resolved(intent.mint)
            metrics.ORDERS_TOTAL.labels(status="failed").inc()
            raise

        confirmed = order.model_copy(update={"status": "confirmed", "last_signature": signature})
        await self._persist_order(confirmed)
        fill = self._fill_from(built, confirmed, intent, signature, now_ms)
        await self._persist_fill(fill)
        self._on_resolved(intent.mint)
        metrics.ORDERS_TOTAL.labels(status="confirmed").inc()
        _log.info(
            "live_fill",
            order_id=confirmed.order_id,
            fill_id=fill.fill_id,
            signature=signature,
            in_raw=built.compute_unit_limit,
            mint=intent.mint,
        )
        return confirmed, fill

    # ---- helpers ----

    def _user_pubkey(self) -> str:
        kp = self._keypair_loader.load_keypair()
        return str(kp.pubkey())  # type: ignore[attr-defined]

    async def _quote_for(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int,
    ) -> Quote:
        return await self._route_selector.best_quote(
            input_mint=input_mint,
            output_mint=output_mint,
            amount_raw=amount_raw,
            slippage_bps=slippage_bps,
        )

    async def _swap_instructions(
        self,
        *,
        quote: Quote,
        user_pubkey: str,
        fee_lamports: int,
    ) -> SwapInstructions:
        if quote.venue == "jupiter":
            return await self._jupiter.swap_instructions(
                quote=quote,
                user_public_key=user_pubkey,
                priority_fee_lamports=fee_lamports,
            )
        if self._raydium is None:
            raise ExecutionFailed("raydium venue selected but client unavailable")
        return await self._raydium.swap_transaction(
            quote=quote,
            user_public_key=user_pubkey,
            priority_fee_lamports=fee_lamports,
        )

    async def _submit(self, built: BuiltTx, order: Order) -> str:
        try:
            res = await self._rpc.call(
                "sendTransaction",
                [
                    built.wire_b64,
                    {
                        "encoding": "base64",
                        "skipPreflight": True,
                        "maxRetries": 0,
                        "preflightCommitment": "confirmed",
                    },
                ],
            )
        except RpcError as e:
            raise ExecutionFailed(f"sendTransaction failed: {e}") from e
        if not isinstance(res, str):
            raise ExecutionFailed(f"sendTransaction returned non-signature: {res!r}")
        submitted = order.model_copy(
            update={
                "status": "submitted",
                "last_signature": res,
                "last_attempt": order.last_attempt + 1,
            }
        )
        await self._persist_order(submitted)
        metrics.SUBMIT_LATENCY.labels(outcome="ok").observe(0.0)
        return res

    async def _mark_stuck(self, order: Order, signature: str) -> None:
        stuck = order.model_copy(
            update={"status": "stuck", "last_signature": signature or order.last_signature}
        )
        await self._persist_order(stuck)
        if signature:
            await self._store.execute(
                """
                INSERT OR IGNORE INTO stuck_signatures
                    (signature, order_id, created_at, attempts, resolved)
                VALUES (?, ?, ?, 0, 0)
                """,
                (signature, order.order_id, self._clock.now().isoformat()),
            )

    async def _mark_failed(self, order: Order, reason: str) -> None:
        failed = order.model_copy(update={"status": "failed"})
        await self._persist_order(failed)
        _log.warning("live_order_failed", order_id=order.order_id, reason=reason)

    def _fill_from(
        self,
        built: BuiltTx,
        order: Order,
        intent: OrderIntent,
        signature: str,
        now_ms: int,
    ) -> Fill:
        route = Route(
            venue="jupiter",
            route_plan=("live",),
            price_impact_pct=0.0,
            in_amount_raw=intent.intended_input_amount_raw,
            out_amount_raw=0,
            slippage_bps=intent.max_slippage_bps,
        )
        return Fill(
            fill_id=new_fill_id(now_ms),
            order_id=order.order_id,
            signature=signature,
            slot=0,
            block_time=self._clock.now(),
            input_amount_raw=intent.intended_input_amount_raw,
            output_amount_raw=0,
            realized_slippage_bps=intent.max_slippage_bps,
            fee_lamports=0,
            priority_fee_lamports=built.compute_unit_price_microlamports,
            route=route,
            usd_value=intent.intended_usd,
        )

    async def _persist_order(self, order: Order) -> None:
        await self._store.execute(
            """
            INSERT OR REPLACE INTO orders (
                order_id, signal_id, created_at, mint, direction,
                intended_usd, intended_input_amount_raw, max_slippage_bps,
                status, last_attempt, last_signature, trace_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.order_id,
                order.signal_id,
                order.created_at.isoformat(),
                order.mint,
                order.direction,
                order.intended_usd,
                order.intended_input_amount_raw,
                order.max_slippage_bps,
                order.status,
                order.last_attempt,
                order.last_signature,
                order.trace_id,
            ),
        )

    async def _persist_fill(self, fill: Fill) -> None:
        import json as _json

        await self._store.execute(
            """
            INSERT OR REPLACE INTO fills (
                fill_id, order_id, signature, slot, block_time,
                input_amount_raw, output_amount_raw, realized_slippage_bps,
                fee_lamports, priority_fee_lamports, route_json, usd_value
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.fill_id,
                fill.order_id,
                fill.signature,
                fill.slot,
                fill.block_time.isoformat(),
                fill.input_amount_raw,
                fill.output_amount_raw,
                fill.realized_slippage_bps,
                fill.fee_lamports,
                fill.priority_fee_lamports,
                _json.dumps(fill.route.model_dump(mode="json"), sort_keys=True),
                fill.usd_value,
            ),
        )


__all__ = ["LiveExecutor"]
