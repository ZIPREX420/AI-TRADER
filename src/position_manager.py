"""TP ladder, hard SL, mirror-exit, time-stop. Polls Jupiter price for held positions."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx
from solders.keypair import Keypair

from .constants import LAMPORTS_PER_SOL, SOL_MINT
from .executor import execute_swap
from .logger import TradeLog, Telegram
from .risk_manager import Position, RiskManager
from .signal_engine import SignalEngine

log = logging.getLogger("positions")


@dataclass
class ExitDecision:
    should_exit: bool
    fraction: float  # of original tokens
    reason: str


def decide_exit(
    pos: Position,
    current_price_sol: float,
    smart_exit_count: int,
    now: float,
) -> ExitDecision:
    """TP ladder: 2x→50%, 5x→30% more; SL −40%; mirror-exit ≥3 smart sells; time-stop 60min flat (<1.2x)."""
    entry = pos.entry_price_sol
    if entry <= 0:
        return ExitDecision(False, 0.0, "no_entry_price")

    ratio = current_price_sol / entry

    # Hard stop loss
    if ratio <= 0.60:
        return ExitDecision(True, 1.0 - pos.sold_pct, "stop_loss_-40%")

    # Mirror exit
    if smart_exit_count >= 3:
        return ExitDecision(True, 1.0 - pos.sold_pct, f"mirror_exit:{smart_exit_count}")

    # TP ladder
    if ratio >= 5.0 and pos.sold_pct < 0.80:
        target = 0.80
        return ExitDecision(True, max(0.0, target - pos.sold_pct), "tp_5x")
    if ratio >= 2.0 and pos.sold_pct < 0.50:
        target = 0.50
        return ExitDecision(True, max(0.0, target - pos.sold_pct), "tp_2x")

    # Trailing on remaining moonbag
    if pos.sold_pct >= 0.80:
        if pos.high_water_price_sol > 0:
            drawdown = current_price_sol / pos.high_water_price_sol
            if drawdown <= 0.70:
                return ExitDecision(True, 1.0 - pos.sold_pct, "trail_-30%")

    # Time stop: 60 min flat (< 1.2x)
    if now - pos.opened_at > 3600 and ratio < 1.2:
        return ExitDecision(True, 1.0 - pos.sold_pct, "time_stop")

    return ExitDecision(False, 0.0, "hold")


async def get_price_sol(http: httpx.AsyncClient, mint: str, probe_tokens_lamports: int = 1_000_000) -> float | None:
    """Sell-side price probe via tiny Jupiter quote (token → SOL). Returns SOL/token."""
    try:
        from .constants import JUPITER_QUOTE_URL
        r = await http.get(
            JUPITER_QUOTE_URL,
            params={
                "inputMint": mint,
                "outputMint": SOL_MINT,
                "amount": probe_tokens_lamports,
                "slippageBps": 1500,
                "swapMode": "ExactIn",
            },
            timeout=4.0,
        )
        if r.status_code != 200:
            return None
        d = r.json()
        out = int(d.get("outAmount", 0))
        if out == 0:
            return None
        sol = out / LAMPORTS_PER_SOL
        return sol / (probe_tokens_lamports / LAMPORTS_PER_SOL)
    except Exception:
        return None


class PositionManager:
    def __init__(
        self,
        risk: RiskManager,
        engine: SignalEngine,
        trade_log: TradeLog,
        tg: Telegram,
        cfg,
    ):
        self.risk = risk
        self.engine = engine
        self.trade_log = trade_log
        self.tg = tg
        self.cfg = cfg

    async def loop(self, *, client, http: httpx.AsyncClient, kp: Keypair, interval_s: float = 5.0):
        while True:
            try:
                await self._tick(client=client, http=http, kp=kp)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"position loop err: {e!r}")
            await asyncio.sleep(interval_s)

    async def _tick(self, *, client, http: httpx.AsyncClient, kp: Keypair):
        if self.risk.state is None:
            return
        positions = list(self.risk.state.open_positions.values())
        if not positions:
            return
        now = time.time()
        for pos in positions:
            price = await get_price_sol(http, pos.mint)
            if price is None:
                continue
            if price > pos.high_water_price_sol:
                pos.high_water_price_sol = price
            smart_exits = self.engine.smart_exit_count(pos.mint)
            decision = decide_exit(pos, price, smart_exits, now)
            if not decision.should_exit or decision.fraction <= 0:
                continue
            tokens_to_sell = pos.tokens * decision.fraction
            if tokens_to_sell <= 0:
                continue
            # Convert tokens_to_sell (UI amount) → raw units; for simplicity use lamports-of-token
            # via token_amount we already store UI amount; we need decimals → fetch from mint
            decimals = await _mint_decimals(client, pos.mint)
            if decimals is None:
                continue
            raw = int(tokens_to_sell * (10 ** decimals))
            if raw <= 0:
                continue
            res = await execute_swap(
                client=client,
                http=http,
                kp=kp,
                input_mint=pos.mint,
                output_mint=SOL_MINT,
                amount_lamports=raw,
                slippage_bps=self.cfg.slippage_bps,
                dry_run=self.cfg.dry_run,
                jito_url=self.cfg.jito_url,
                jito_tip_lamports=self.cfg.jito_tip_lamports,
                use_jito=True,
            )
            sol_out = res.out_amount / LAMPORTS_PER_SOL if res.ok else 0.0
            pnl_sol = self.risk.close(pos.mint, sol_out, partial_pct=decision.fraction) if res.ok else 0.0
            await self.trade_log.log_trade(
                side="sell",
                mint=pos.mint,
                sol_amount=sol_out,
                token_amount=tokens_to_sell,
                price_sol=price,
                signature=res.signature,
                source=f"exit:{decision.reason}",
                dry_run=res.dry_run,
                success=res.ok,
                error=res.error,
                elapsed_ms=res.elapsed_ms,
                payload={"price_impact": res.price_impact_pct, "route": res.route_summary},
            )
            if res.ok:
                await self.trade_log.log_pnl(pos.mint, pnl_sol or 0.0, self.risk.state.sol_price_usd, decision.reason)
                msg = (
                    f"🔻 SELL {decision.reason} {pos.mint[:6]}…\n"
                    f"frac={decision.fraction:.0%} sol_out={sol_out:.4f} pnl={pnl_sol:.4f} SOL"
                )
                await self.tg.send(msg)


async def _mint_decimals(client, mint: str) -> int | None:
    try:
        from solders.pubkey import Pubkey
        r = await client.get_token_supply(Pubkey.from_string(mint))
        return int(r.value.decimals)
    except Exception:
        return None
