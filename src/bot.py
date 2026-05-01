"""Main entrypoint: wires every module into an event-driven asyncio loop."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

from .config import Config, load_config, load_keypair, load_smart_wallets
from .constants import (
    JUPITER_PRICE_URL,
    LAMPORTS_PER_SOL,
    POOL_PROGRAMS,
    PUMPFUN,
    RAYDIUM_AMM_V4,
    SOL_MINT,
)
from .executor import execute_swap
from .ingest import LogEvent, stream_chunks
from .logger import Telegram, TradeLog
from .position_manager import PositionManager
from .risk_manager import Position, RiskManager
from .rug_filter import run_checks
from .signal_engine import SignalEngine
from .token_scanner import parse_new_token
from .wallet_tracker import parse_swap

log = logging.getLogger("bot")


async def fetch_sol_price_usd(http: httpx.AsyncClient) -> float:
    try:
        r = await http.get(JUPITER_PRICE_URL, params={"ids": SOL_MINT}, timeout=4.0)
        d = r.json()
        return float(d["data"][SOL_MINT]["price"])
    except Exception:
        return 150.0  # safe fallback


async def get_sol_balance(client: AsyncClient, pubkey) -> float:
    try:
        r = await client.get_balance(pubkey)
        return r.value / LAMPORTS_PER_SOL
    except Exception:
        return 0.0


async def main(cfg: Optional[Config] = None):
    cfg = cfg or load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    if not cfg.helius_api_key:
        raise RuntimeError("HELIUS_API_KEY missing in .env")

    kp = load_keypair(cfg.keypair_path)
    pubkey = kp.pubkey()
    log.info(f"loaded keypair {pubkey}")

    smart_wallets = load_smart_wallets()
    if not smart_wallets:
        log.warning("no smart wallets loaded; new-token scanner will run, copy mode will be idle")

    client = AsyncClient(cfg.helius_http)
    http = httpx.AsyncClient(http2=False)

    sol_price = await fetch_sol_price_usd(http)
    sol_balance = await get_sol_balance(client, pubkey)
    capital_sol = cfg.capital_usd / sol_price
    log.info(f"sol_price=${sol_price:.2f} balance={sol_balance:.4f} SOL capital_sol={capital_sol:.4f}")

    risk = RiskManager(
        max_position_pct=cfg.max_position_pct,
        max_open=cfg.max_open_positions,
        daily_loss_halt_pct=cfg.daily_loss_halt_pct,
        sol_reserve=cfg.sol_reserve,
    )
    risk.init_state(capital_sol=capital_sol, capital_usd=cfg.capital_usd, sol_price_usd=sol_price)

    engine = SignalEngine()
    trade_log = TradeLog(cfg.db_path)
    await trade_log.init()

    tg = Telegram(cfg.telegram_bot_token, cfg.telegram_chat_id)
    await tg.send(
        f"🟢 INSTANT-AI-TRADER online\n"
        f"dry_run={cfg.dry_run} capital_usd=${cfg.capital_usd} cap_sol={capital_sol:.4f}\n"
        f"smart_wallets={len(smart_wallets)} max_open={cfg.max_open_positions}"
    )

    pm = PositionManager(risk, engine, trade_log, tg, cfg)

    queue: asyncio.Queue[LogEvent] = asyncio.Queue(maxsize=10_000)
    tasks: list[asyncio.Task] = []

    if smart_wallets:
        tasks += await stream_chunks(cfg.helius_ws, smart_wallets, queue, chunk_size=50, name="wallets")

    pool_subs = [RAYDIUM_AMM_V4, PUMPFUN]
    tasks += await stream_chunks(cfg.helius_ws, pool_subs, queue, chunk_size=2, name="pools")

    tasks.append(asyncio.create_task(pm.loop(client=client, http=http, kp=kp), name="positions"))
    tasks.append(asyncio.create_task(_pnl_refresher(risk, http), name="pnl_refresh"))

    smart_set = set(smart_wallets)

    log.info(f"streams up: {len(tasks)} tasks; entering signal loop")
    try:
        while True:
            evt = await queue.get()
            try:
                await _handle_event(
                    evt=evt,
                    cfg=cfg,
                    smart_set=smart_set,
                    client=client,
                    http=http,
                    kp=kp,
                    risk=risk,
                    engine=engine,
                    trade_log=trade_log,
                    tg=tg,
                )
            except Exception as e:
                log.exception(f"event handler err: {e!r}")
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await http.aclose()
        await client.close()


async def _pnl_refresher(risk: RiskManager, http: httpx.AsyncClient):
    while True:
        await asyncio.sleep(60)
        try:
            p = await fetch_sol_price_usd(http)
            if risk.state:
                risk.state.sol_price_usd = p
        except Exception:
            pass


async def _handle_event(
    *,
    evt: LogEvent,
    cfg: Config,
    smart_set: set[str],
    client: AsyncClient,
    http: httpx.AsyncClient,
    kp,
    risk: RiskManager,
    engine: SignalEngine,
    trade_log: TradeLog,
    tg: Telegram,
):
    sig_obj = None
    if evt.mention in smart_set:
        sig_obj = await parse_swap(client, evt, evt.mention)
    elif evt.mention in (RAYDIUM_AMM_V4, PUMPFUN):
        sig_obj = await parse_new_token(client, evt)

    if sig_obj is None:
        return

    await trade_log.log_signal(
        kind=sig_obj.kind,
        mint=sig_obj.mint,
        source=getattr(sig_obj, "source", getattr(sig_obj, "wallet", "")),
        score=0.0,
        payload=sig_obj.__dict__,
    )

    if risk.state is None:
        return

    order = engine.evaluate(sig_obj, risk.state.capital_usd, risk.state.sol_price_usd)
    if order is None:
        return

    sol_balance = await get_sol_balance(client, kp.pubkey())
    ok, reason = risk.allow(order.mint, order.sol_size, sol_balance)
    if not ok:
        log.info(f"risk reject {order.mint[:8]} {reason}")
        return

    filt = await run_checks(client, http, order.mint)
    if not filt.ok:
        log.info(f"filter reject {order.mint[:8]} {filt.reason}")
        return

    sol_lamports = int(order.sol_size * LAMPORTS_PER_SOL)
    res = await execute_swap(
        client=client,
        http=http,
        kp=kp,
        input_mint=SOL_MINT,
        output_mint=order.mint,
        amount_lamports=sol_lamports,
        slippage_bps=cfg.slippage_bps,
        dry_run=cfg.dry_run,
        jito_url=cfg.jito_url,
        jito_tip_lamports=cfg.jito_tip_lamports,
        use_jito=True,
    )

    decimals = await _mint_decimals(client, order.mint)
    token_ui = (res.out_amount / (10 ** decimals)) if decimals else 0.0
    price_sol = (order.sol_size / token_ui) if token_ui > 0 else 0.0

    await trade_log.log_trade(
        side="buy",
        mint=order.mint,
        sol_amount=order.sol_size,
        token_amount=token_ui,
        price_sol=price_sol,
        signature=res.signature,
        source=order.source,
        dry_run=res.dry_run,
        success=res.ok,
        error=res.error,
        elapsed_ms=res.elapsed_ms,
        payload={
            "score": order.score,
            "reason": order.reason,
            "filter": filt.__dict__,
            "price_impact": res.price_impact_pct,
            "route": res.route_summary,
        },
    )

    if res.ok and not res.dry_run:
        risk.open(Position(
            mint=order.mint,
            sol_in=order.sol_size,
            tokens=token_ui,
            entry_price_sol=price_sol,
            opened_at=time.time(),
            source=order.source,
            high_water_price_sol=price_sol,
        ))
        await tg.send(
            f"🟢 BUY {order.mint[:8]}…\n"
            f"src={order.source} sol={order.sol_size:.4f} tok={token_ui:.4f}\n"
            f"score={order.score:.2f} pi={res.price_impact_pct:.2%} route={res.route_summary[:80]}"
        )
    elif res.ok and res.dry_run:
        log.info(f"DRY BUY {order.mint[:8]} sol={order.sol_size:.4f} score={order.score:.2f}")


async def _mint_decimals(client, mint: str) -> int | None:
    try:
        r = await client.get_token_supply(Pubkey.from_string(mint))
        return int(r.value.decimals)
    except Exception:
        return None


if __name__ == "__main__":
    asyncio.run(main())
