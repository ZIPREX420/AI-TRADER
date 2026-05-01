"""Main async runtime loop. CLI: python -m runtime.loop --mode {live,paper}."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from connectors import helius as helius_factory
from connectors import quicknode as quicknode_factory
from connectors.solana_rpc import Endpoint, SolanaRPC
from core import config as cfg_mod
from core.execution import Executor, LiveExecutor, MockExecutor
from core.risk_exec import RiskCtx, RiskExec
from core.types import Candidate, ExecResult, Mode, PrePumpSignal, Side, WalletEvent
from runtime.feedback import Feedback
from runtime.logger import TradeLog
from runtime.mode_manager import ModeManager
from signal.cluster_detector import ClusterDetector
from signal.signal_engine import SignalEngine

log = logging.getLogger("runtime.loop")


def _make_endpoints(cfg: cfg_mod.Settings) -> list[Endpoint]:
    eps: list[Endpoint] = []
    if cfg.helius_api_key:
        h = helius_factory.make(cfg.helius_api_key, name="helius", priority=1)
        eps.append(Endpoint(name=h.name, http_url=h.http_url, ws_url=h.ws_url,
                            priority=h.priority, max_subs_per_ws=h.max_subs_per_ws))
    if cfg.quicknode_http or cfg.quicknode_ws:
        q = quicknode_factory.make(cfg.quicknode_http, cfg.quicknode_ws, name="quicknode", priority=2)
        eps.append(Endpoint(name=q.name, http_url=q.http_url, ws_url=q.ws_url,
                            priority=q.priority, max_subs_per_ws=q.max_subs_per_ws))
    return eps


def _mock_price_feed(price: float = 1e-6):
    def _at(_ts: float, _mint: str) -> float:
        return price
    return _at


def _build_live_executor(cfg: cfg_mod.Settings, http: httpx.AsyncClient,
                         rpc: SolanaRPC, risk: RiskExec) -> Optional[LiveExecutor]:
    try:
        kp = cfg_mod.load_keypair(cfg.keypair_path)
    except FileNotFoundError:
        log.warning("keypair not found at %s; live executor disabled", cfg.keypair_path)
        return None
    return LiveExecutor(
        http=http,
        rpc=rpc,
        keypair=kp,
        jito_url=cfg.jito_url,
        jito_tip_lamports=cfg.jito_tip_lamports,
        slippage_bps=int((cfg.slippage_bps_min + cfg.slippage_bps_max) // 2),
        risk_exec=risk,
    )


async def _fetch_tx_via_helius(http: httpx.AsyncClient, api_key: str, sig: str) -> Optional[dict]:
    if not api_key or not sig:
        return None
    url = f"https://api.helius.xyz/v0/transactions?api-key={api_key}"
    payload = {"transactions": [sig]}
    try:
        r = await http.post(url, json=payload, timeout=8.0)
    except (httpx.TimeoutException, httpx.TransportError):
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if isinstance(data, list) and data:
        return data[0]
    return None


async def _evaluate_loop_periodic(mode_mgr: ModeManager, period_s: float = 30.0) -> None:
    while True:
        try:
            mode_mgr.evaluate()
        except Exception as e:  # never let evaluator die
            log.warning(f"mode_mgr.evaluate err: {type(e).__name__}: {e}")
        await asyncio.sleep(period_s)


async def _watchdog_periodic(risk: RiskExec, period_s: float = 10.0) -> None:
    while True:
        try:
            risk.watchdog_tick()
        except Exception as e:
            log.warning(f"risk watchdog err: {type(e).__name__}: {e}")
        await asyncio.sleep(period_s)


async def _process_event(
    *,
    ev: Any,
    cfg: cfg_mod.Settings,
    sig_engine: SignalEngine,
    risk: RiskExec,
    mode_mgr: ModeManager,
    log_store: TradeLog,
    feed: Feedback,
) -> None:
    candidate: Optional[Candidate] = None
    if isinstance(ev, WalletEvent):
        candidate = sig_engine.evaluate(wallet_event=ev)
    elif isinstance(ev, PrePumpSignal):
        candidate = sig_engine.evaluate(prepump=ev)
    if candidate is None:
        return
    log_store.signal(candidate.mint, kind=",".join(candidate.source_kinds),
                     payload={"confidence": candidate.confidence, "fp": candidate.fingerprint})
    # Pre-trade gates
    expected_size_lamports = int(cfg.capital_sol * cfg.max_position_pct * 1e9)
    ctx = RiskCtx(
        sol_balance_lamports=int(cfg.capital_sol * 1e9),
        token_balance_raw=10**12 if candidate.side == Side.SELL else 0,
        expected_fee_lamports=cfg.jito_tip_lamports + 250_000,
        expected_slip_bps=cfg.slippage_bps_max,
        expected_size_lamports=expected_size_lamports,
        last_latencies_ms=tuple(),
        mode=mode_mgr.current().mode,
        sol_reserve_lamports=int(cfg.sol_reserve * 1e9),
        fee_threshold_pct=cfg.fee_threshold_pct,
        latency_p50_max_s=1.5,
    )
    decision = risk.pre_trade_gates(candidate, ctx)
    if not decision.ok:
        log_store.skip(candidate, decision.reason)
        return
    executor = mode_mgr.executor()
    if executor is None:
        log_store.skip(candidate, "no_executor")
        return
    size_lamports = int(expected_size_lamports * risk.size_dampener_factor())
    if size_lamports < int(0.001 * 1e9):
        log_store.skip(candidate, "size_too_small")
        return
    try:
        if candidate.side == Side.BUY:
            res: ExecResult = await executor.submit_buy(candidate.mint, size_lamports, candidate)
        else:
            res = await executor.submit_sell(candidate.mint, ctx.token_balance_raw, candidate)
    except Exception as e:
        log.warning(f"executor error: {type(e).__name__}: {e}")
        return
    mode_mgr.report_exec_result(res.ok, error=res.error)
    if res.ok:
        trade_id = log_store.entry(candidate, res, mode=res.mode)
        log_store.telemetry("entry", mint=candidate.mint, mode=res.mode,
                            sig=res.sig, latency_ms=res.elapsed_ms,
                            confidence=candidate.confidence)
    else:
        log_store.failure(candidate, res)
        log_store.telemetry("failure", mint=candidate.mint, mode=res.mode,
                            error=res.error, latency_ms=res.elapsed_ms)
        if res.error in ("not_confirmed", "submit_failed", "slippage_exceeded"):
            risk.quarantine(candidate.mint, time.time() + 15 * 60)


async def main(mode_arg: str = "paper") -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    cfg = cfg_mod.load()
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.logs_dir).mkdir(parents=True, exist_ok=True)

    rpc = SolanaRPC(_make_endpoints(cfg), state_dir=cfg.state_dir)
    risk = RiskExec(state_dir=cfg.state_dir)
    log_store = TradeLog(cfg.db_path)
    feed = Feedback()

    http = httpx.AsyncClient()
    mock_exec = MockExecutor(price_at=_mock_price_feed())
    live_exec = _build_live_executor(cfg, http, rpc, risk) if mode_arg == "live" else None

    initial_mode = Mode.LIVE if (mode_arg == "live" and live_exec is not None) else Mode.PAPER
    mode_mgr = ModeManager(state_dir=cfg.state_dir, initial=initial_mode)
    mode_mgr.bind_executors(live=live_exec or mock_exec, mock=mock_exec)
    if mode_arg == "paper":
        mode_mgr.manual_paper(reason="cli_paper")

    sig_engine = SignalEngine(
        bridge_match=lambda fp: None,
        wallet_score=lambda w: 0.7,
        token_safety=lambda mint: 0.8,
        cluster_count=lambda mint: 0,
        regime="NORMAL",
    )

    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=10_000)
    tasks = [
        asyncio.create_task(_evaluate_loop_periodic(mode_mgr), name="mode_eval"),
        asyncio.create_task(_watchdog_periodic(risk), name="watchdog"),
    ]

    # Optional: start RPC stream (no-op if no endpoints)
    if rpc.endpoints:
        tasks.append(asyncio.create_task(rpc.stream_events(queue, mentions=[]), name="rpc_stream"))

    stop = asyncio.Event()

    def _signal_handler(*_args: Any) -> None:
        stop.set()

    try:
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(s, _signal_handler)
            except NotImplementedError:
                pass
    except RuntimeError:
        pass

    log.info("solana-mvp loop online; mode=%s", mode_mgr.current().mode.value)
    try:
        while not stop.is_set():
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await _process_event(
                    ev=ev, cfg=cfg, sig_engine=sig_engine, risk=risk,
                    mode_mgr=mode_mgr, log_store=log_store, feed=feed,
                )
            except Exception as e:
                log.warning(f"process_event error: {type(e).__name__}: {e}")
    finally:
        for t in tasks:
            t.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            pass
        await http.aclose()
        try:
            await rpc.stop()
        except Exception:
            pass
    return 0


def cli() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["live", "paper"], default="paper")
    args = p.parse_args()
    return asyncio.run(main(args.mode))


if __name__ == "__main__":
    sys.exit(cli())
