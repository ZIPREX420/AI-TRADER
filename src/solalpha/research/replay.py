"""Deterministic replay engine + walk-forward harness."""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow.parquet as pq

from solalpha.domain import NormalizedSwap
from solalpha.foundation.bus import Bus
from solalpha.foundation.clock import FakeClock, SystemClock
from solalpha.foundation.errors import ReplayDataError
from solalpha.foundation.health import HealthRegistry
from solalpha.foundation.logging import bind_trace_id, get_logger
from solalpha.foundation.state import SqliteStore
from solalpha.observability.portfolio import PortfolioTracker
from solalpha.research.metrics import (
    SessionMetrics,
    compute_session_metrics,
    trades_from_fills,
)
from solalpha.research.readonly_guard import ReadonlyGuard, assert_readonly
from solalpha.research.sim_executor import build_sim_executor
from solalpha.signal.combiner import ConfidenceCombiner
from solalpha.signal.detectors import (
    ClusterDetector,
    FlowAnomalyDetector,
    PrePumpDetector,
)
from solalpha.signal.kill_switch import KillSwitch
from solalpha.signal.mode_manager import ModeManager
from solalpha.signal.risk_engine import RiskEngine
from solalpha.signal.sizer import PortfolioSizer
from solalpha.signal.smart_wallet_scorer import SmartWalletScorer

if TYPE_CHECKING:
    from solalpha.domain import Fill, OrderIntent, Signal
    from solalpha.foundation.config import AppConfig

_log = get_logger(__name__)


async def replay_session(cfg: AppConfig, session: Path) -> dict[str, Any]:
    if not session.exists():  # noqa: ASYNC240 -- CLI-driven, blocking stat is fine
        raise ReplayDataError(f"replay session not found: {session}")
    swaps = _load_swaps(session)
    if not swaps:
        return {
            "session": str(session),
            "n_swaps": 0,
            "metrics": SessionMetrics(
                n_trades=0,
                total_pnl_usd=0.0,
                hit_rate=0.0,
                sharpe=0.0,
                max_drawdown=0.0,
                exposure_s=0.0,
                turnover_usd=0.0,
            ).model_dump(mode="json"),
            "signal_inputs_hashes": [],
        }
    metrics, hashes = await _run_replay(cfg, swaps)
    return {
        "session": str(session),
        "n_swaps": len(swaps),
        "metrics": metrics.model_dump(mode="json"),
        "signal_inputs_hashes": hashes,
    }


async def run_walkforward(cfg: AppConfig) -> dict[str, Any]:
    base = cfg.persistence.parquet_root / "normalized"
    if not base.exists():
        raise ReplayDataError(
            f"no normalized-swap parquet directory at {base}; "
            "run `solalpha research backfill` first"
        )
    swaps = _load_partitions(base)
    if not swaps:
        return {
            "n_swaps": 0,
            "folds": [],
            "gate_passed": False,
            "min_oos_sharpe": cfg.research.min_oos_sharpe,
        }
    train_d = cfg.research.walkforward_train_days
    test_d = cfg.research.walkforward_test_days
    folds: list[dict[str, Any]] = []
    pass_count = 0
    earliest = min(s.block_time for s in swaps)
    latest = max(s.block_time for s in swaps)
    cursor = earliest + timedelta(days=train_d)
    while cursor + timedelta(days=test_d) <= latest:
        test_end = cursor + timedelta(days=test_d)
        test_swaps = [s for s in swaps if cursor <= s.block_time < test_end]
        metrics, _ = await _run_replay(cfg, test_swaps)
        passed = metrics.sharpe >= cfg.research.min_oos_sharpe
        folds.append(
            {
                "test_start": cursor.isoformat(),
                "test_end": test_end.isoformat(),
                "n_swaps": len(test_swaps),
                "metrics": metrics.model_dump(mode="json"),
                "passed": passed,
            }
        )
        if passed:
            pass_count += 1
        cursor = test_end
    return {
        "n_swaps": len(swaps),
        "folds": folds,
        "gate_passed": bool(folds) and pass_count == len(folds),
        "pass_count": pass_count,
        "min_oos_sharpe": cfg.research.min_oos_sharpe,
    }


def _load_swaps(session: Path) -> list[NormalizedSwap]:
    try:
        table = pq.read_table(session)  # type: ignore[no-untyped-call]
    except Exception as e:
        raise ReplayDataError(f"failed to read {session}: {e}") from e
    return _table_to_swaps(table)


def _load_partitions(root: Path) -> list[NormalizedSwap]:
    swaps: list[NormalizedSwap] = []
    for path in sorted(root.glob("dt=*/part-*.parquet")):
        try:
            t = pq.read_table(path)  # type: ignore[no-untyped-call]
        except (OSError, ValueError) as e:
            _log.warning("partition_unreadable", path=str(path), exc=str(e))
            continue
        swaps.extend(_table_to_swaps(t))
    swaps.sort(key=lambda s: s.block_time)
    return swaps


def _table_to_swaps(table: object) -> list[NormalizedSwap]:
    rows = table.to_pylist()  # type: ignore[attr-defined]
    out: list[NormalizedSwap] = []
    for r in rows:
        try:
            out.append(NormalizedSwap.model_validate(r))
        except Exception as e:
            _log.debug("replay_row_skipped", exc=str(e))
            continue
    return out


async def _run_replay(
    cfg: AppConfig,
    swaps: list[NormalizedSwap],
) -> tuple[SessionMetrics, list[str]]:
    if not swaps:
        return (
            SessionMetrics(
                n_trades=0,
                total_pnl_usd=0.0,
                hit_rate=0.0,
                sharpe=0.0,
                max_drawdown=0.0,
                exposure_s=0.0,
                turnover_usd=0.0,
            ),
            [],
        )
    base_ts = min(s.block_time for s in swaps)
    clock = FakeClock(start=base_ts)
    # `ignore_cleanup_errors=True`: on Windows, `aiosqlite.close()` returns
    # before the worker-thread file handles for the WAL `-shm`/`-wal` side
    # files are fully released by the OS, and the TemporaryDirectory's
    # rmtree races them with `WinError 32`. The temp dir is in the OS temp
    # area, so the OS reclaims any leftover bytes on reboot; on POSIX
    # cleanup succeeds normally and this flag is a no-op.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        store = SqliteStore(tmp / "replay.db", clock=clock)
        await store.connect()
        live_store = SqliteStore(cfg.persistence.sqlite_path, clock=SystemClock())
        await live_store.connect()
        guarded = ReadonlyGuard(live_store)
        assert_readonly(guarded)
        try:
            return await _drive(cfg, swaps, store, clock)
        finally:
            await live_store.close()
            await store.close()


async def _drive(
    cfg: AppConfig,
    swaps: list[NormalizedSwap],
    store: SqliteStore,
    clock: FakeClock,
) -> tuple[SessionMetrics, list[str]]:
    seen_wallets = {s.wallet for s in swaps}
    for w in seen_wallets:
        await store.execute(
            "INSERT INTO smart_wallets (wallet, added_at, source, weight, score, last_active_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (w, clock.now().isoformat(), "replay", 1.0, 0.9, clock.now().isoformat()),
        )
    scorer = SmartWalletScorer(
        store,
        clock,
        decay_half_life_days=cfg.smart_wallets.decay_half_life_days,
        min_score_smart=cfg.smart_wallets.min_score_to_subscribe,
    )
    await scorer.refresh()
    kill = KillSwitch(store, clock, cfg.kill_switch.file_path)
    portfolio = PortfolioTracker(store, clock)
    await portfolio.load()
    health = HealthRegistry(clock)
    bus = Bus()
    mode_manager = ModeManager(cfg, bus, store, clock, kill, portfolio, health)
    risk = RiskEngine(cfg, store, clock, kill, portfolio, mode_manager)
    sizer = PortfolioSizer(cfg, mode_manager)
    detectors = (
        PrePumpDetector(cfg.signals.prepump),
        ClusterDetector(cfg.signals.cluster, scorer),
        FlowAnomalyDetector(cfg.signals.flow_anomaly),
    )
    combiner = ConfidenceCombiner(cfg.signals.weights)
    executor = build_sim_executor(cfg, clock)

    inputs_hashes: list[str] = []
    fills: list[Fill] = []
    directions: dict[str, str] = {}
    for swap in swaps:
        clock.advance(max(0.0, (swap.block_time - clock.now()).total_seconds()))
        for d in detectors:
            await d.observe(swap)
        now = clock.now()
        for d in detectors:
            for ds in d.poll(now):
                await combiner.add(ds)
        for signal in combiner.poll(now):
            with bind_trace_id(signal.trace_id):
                inputs_hashes.append(signal.inputs_hash)
                sized = sizer.size(signal)
                decision = await risk.evaluate(sized)
                if decision.decision == "rejected":
                    continue
                approved = sized.model_copy(update={"suggested_usd": decision.approved_usd})
                intent = _intent_from_signal(approved, cfg)
                order, fill = await executor.execute(intent)
                directions[order.order_id] = order.direction
                fills.append(fill)
                await portfolio.apply_fill(fill, order)

    trade_returns = trades_from_fills(fills, directions)
    return compute_session_metrics(trade_returns), inputs_hashes


def _intent_from_signal(signal: Signal, cfg: AppConfig) -> OrderIntent:
    from solalpha.domain import OrderIntent as _OrderIntent

    return _OrderIntent(
        signal_id=signal.signal_id,
        mint=signal.mint,
        direction=signal.direction,
        intended_usd=signal.suggested_usd,
        intended_input_amount_raw=int(signal.suggested_usd * 1_000_000),
        max_slippage_bps=cfg.risk.max_slippage_bps,
        trace_id=signal.trace_id,
    )


__all__ = ["replay_session", "run_walkforward"]
