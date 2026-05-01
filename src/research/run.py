"""Replay driver: feeds events through a strategy harness, writes run.db identical to live."""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .clock import ReplayClock
from .replay import Replay, ReplayEvent, PriceProvider
from .simulated_executor import FrictionModel, SimulatedExecutor, SimSwapResult

log = logging.getLogger("run")

RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_open REAL, ts_close REAL,
    mint TEXT, source TEXT, strategy TEXT,
    sol_in REAL, sol_out REAL, fees REAL, roi REAL,
    peak_ratio REAL, hold_s REAL, exit_kind TEXT,
    score_pred REAL,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_mint ON trades(mint);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);

CREATE TABLE IF NOT EXISTS pnl(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, mint TEXT, realized_sol REAL, note TEXT
);

CREATE TABLE IF NOT EXISTS attribution(
    trade_id INTEGER PRIMARY KEY,
    ts_open REAL, ts_close REAL,
    mint TEXT, source_kinds TEXT, wallets TEXT, cluster_ids TEXT,
    copy_score REAL, wallet_score_max REAL, prepump_kind TEXT,
    features TEXT, outcome TEXT, score_pred REAL, weights_used TEXT,
    strategy TEXT, regime_at_open TEXT
);

CREATE TABLE IF NOT EXISTS shadow_trades(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_open REAL, ts_close REAL,
    strategy TEXT, mint TEXT, regime TEXT,
    score_pred REAL, executed INTEGER,
    entry_price_sol REAL, exit_price_sol REAL,
    sim_roi REAL, peak_ratio REAL, exit_reason TEXT
);

CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY, value TEXT
);
"""


@dataclass
class Position:
    strategy: str
    mint: str
    sol_in: float
    tokens: float
    entry_price_sol: float
    opened_at: float
    score: float
    high_water: float
    source: str = ""

    @property
    def cost_basis_sol(self) -> float:
        return self.sol_in


@dataclass
class StrategyConfig:
    """Generic strategy config used by the default harness."""
    id: str
    tp_levels: tuple[tuple[float, float], ...]   # ((ratio, fraction), ...)
    sl: float
    max_hold_s: float
    base_size_pct: float = 0.05


# Reasonable defaults matching v4 §1
DEFAULT_STRATEGIES: dict[str, StrategyConfig] = {
    "S1_COPY":    StrategyConfig("S1_COPY",    ((1.80, 0.35), (3.00, 0.35), (6.00, 0.20)), 0.65, 3600, 0.07),
    "S2_CLUSTER": StrategyConfig("S2_CLUSTER", ((1.50, 0.50), (2.50, 0.40)),                0.75,  900, 0.06),
    "S3_NEW":     StrategyConfig("S3_NEW",     ((1.40, 0.50), (2.00, 0.40)),                0.80,  600, 0.04),
    "S4_DIP":     StrategyConfig("S4_DIP",     ((2.00, 0.50), (3.00, 0.30)),                0.80, 1800, 0.05),
    "S5_SCALP":   StrategyConfig("S5_SCALP",   ((1.20, 0.60), (1.40, 0.30)),                0.90,  300, 0.04),
}


@dataclass
class HarnessState:
    capital_sol: float
    cash_sol: float
    positions: dict[str, Position] = field(default_factory=dict)
    realized: float = 0.0
    smart_wallets: set[str] = field(default_factory=set)
    cluster_window: dict[str, list[tuple[float, str]]] = field(default_factory=dict)  # mint→[(ts,wallet)]
    last_seen_buy_ts: dict[str, float] = field(default_factory=dict)
    open_db: sqlite3.Connection = None  # type: ignore


def _open_db(run_dir: Path) -> sqlite3.Connection:
    run_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(run_dir / "run.db"))
    db.executescript(RUN_SCHEMA)
    db.commit()
    return db


def _record_trade_open(db, ts_open, mint, strategy, source, sol_in, tokens, score):
    cur = db.execute(
        "INSERT INTO trades(ts_open,mint,source,strategy,sol_in,score_pred,payload) VALUES(?,?,?,?,?,?,?)",
        (ts_open, mint, source, strategy, sol_in, score, json.dumps({"tokens_initial": tokens})),
    )
    db.commit()
    return cur.lastrowid


def _record_trade_close(db, trade_id, ts_close, sol_out, fees, peak, hold_s, exit_kind):
    row = db.execute("SELECT sol_in FROM trades WHERE id=?", (trade_id,)).fetchone()
    sol_in = float(row[0]) if row else 0.0
    roi = (sol_out - sol_in - fees) / max(sol_in, 1e-9)
    db.execute(
        "UPDATE trades SET ts_close=?,sol_out=?,fees=?,roi=?,peak_ratio=?,hold_s=?,exit_kind=? WHERE id=?",
        (ts_close, sol_out, fees, roi, peak, hold_s, exit_kind, trade_id),
    )
    db.execute(
        "INSERT INTO pnl(ts,mint,realized_sol,note) VALUES(?,?,?,?)",
        (ts_close, "", sol_out - sol_in - fees, exit_kind),
    )
    db.commit()
    return roi


def _evaluate_exit(pos: Position, current_price: float, now_ts: float,
                   cfg: StrategyConfig, fraction_sold: float) -> tuple[bool, float, str]:
    if current_price <= 0:
        return False, 0.0, "no_price"
    ratio = current_price / pos.entry_price_sol
    if ratio > pos.high_water / pos.entry_price_sol:
        pos.high_water = current_price * 1.0
    if ratio <= cfg.sl:
        return True, 1.0 - fraction_sold, "stop_loss"
    cumulative = 0.0
    for tp_ratio, tp_frac in cfg.tp_levels:
        cumulative += tp_frac
        if ratio >= tp_ratio and fraction_sold < cumulative - 1e-9:
            target = cumulative
            return True, max(0.0, target - fraction_sold), f"tp@{tp_ratio:.2f}"
    if now_ts - pos.opened_at >= cfg.max_hold_s:
        return True, 1.0 - fraction_sold, "time_stop"
    return False, 0.0, "hold"


def _maybe_open(state: HarnessState, ev: ReplayEvent, strategies: dict[str, StrategyConfig],
                executor: SimulatedExecutor, db, regime: str = "NORMAL") -> Optional[int]:
    """Default open logic used by the harness:
    - Smart-wallet buys on tracked wallets → S1_COPY
    - Pool init/migrate events → S3_NEW
    - 3+ distinct smart wallets buying same mint within 90s → S2_CLUSTER
    """
    payload = ev.payload
    mint = payload.get("mint", "")
    if not mint:
        return None

    chosen: Optional[StrategyConfig] = None
    source = ""
    score = 0.6

    if ev.kind == "swap" and int(payload.get("side", 1)) == 0:
        wallet = payload.get("wallet", "")
        if wallet in state.smart_wallets:
            window = state.cluster_window.setdefault(mint, [])
            window.append((ev.ts, wallet))
            window[:] = [(t, w) for (t, w) in window if t >= ev.ts - 90]
            unique = len({w for _, w in window})
            if unique >= 3 and "S2_CLUSTER" in strategies:
                chosen = strategies["S2_CLUSTER"]
                source = f"cluster:{unique}"
                score = 0.7
            elif "S1_COPY" in strategies:
                chosen = strategies["S1_COPY"]
                source = f"copy:{wallet[:8]}"
                score = 0.65
    elif ev.kind == "pool" and payload.get("event_kind") in ("init", "migrate"):
        if "S3_NEW" in strategies:
            chosen = strategies["S3_NEW"]
            source = f"new:{payload.get('event_kind')}"
            score = 0.55

    if chosen is None:
        return None
    if mint in state.positions:
        return None
    if state.cash_sol <= 0.05:
        return None

    sol_size = min(state.capital_sol * chosen.base_size_pct, state.cash_sol - 0.05)
    if sol_size <= 0.001:
        return None
    sol_lamports = int(sol_size * 1e9)
    res = executor.buy(mint, sol_lamports, slippage_bps=500)
    if not res.ok:
        return None
    tokens = res.out_amount / 1e9 if chosen.id != "S3_NEW" else res.out_amount / 1e9
    pos = Position(
        strategy=chosen.id, mint=mint,
        sol_in=sol_size, tokens=tokens,
        entry_price_sol=res.price_sol,
        opened_at=res.confirm_ts, score=score,
        high_water=res.price_sol, source=source,
    )
    state.positions[mint] = pos
    state.cash_sol -= sol_size
    return _record_trade_open(db, res.confirm_ts, mint, chosen.id, source, sol_size, tokens, score)


def _manage_positions(state: HarnessState, now_ts: float, prices: PriceProvider,
                      strategies: dict[str, StrategyConfig],
                      executor: SimulatedExecutor, db,
                      open_trade_ids: dict[str, int],
                      sold_fraction: dict[str, float]):
    for mint, pos in list(state.positions.items()):
        cfg = strategies.get(pos.strategy)
        if cfg is None:
            continue
        price = prices.at(now_ts, mint) or pos.entry_price_sol
        if price > pos.high_water:
            pos.high_water = price
        sold = sold_fraction.get(mint, 0.0)
        should, frac, reason = _evaluate_exit(pos, price, now_ts, cfg, sold)
        if not should or frac <= 0:
            continue
        tokens_to_sell = pos.tokens * frac
        token_raw = int(tokens_to_sell * 1e9)
        res = executor.sell(mint, token_raw, slippage_bps=500)
        if not res.ok:
            continue
        sol_out = res.out_amount / 1e9
        state.cash_sol += sol_out
        sold_fraction[mint] = sold + frac
        if sold_fraction[mint] >= 0.999:
            tid = open_trade_ids.pop(mint, None)
            if tid is not None:
                _record_trade_close(db, tid, res.confirm_ts, sol_out * (1.0 / frac),
                                    fees=res.fee_lamports / 1e9 / frac,
                                    peak=pos.high_water / pos.entry_price_sol,
                                    hold_s=res.confirm_ts - pos.opened_at,
                                    exit_kind=reason)
            del state.positions[mint]


def run_replay(*, start_ts: float, end_ts: float, capital_sol: float,
               smart_wallets: Iterable[str] = (),
               strategies: Optional[dict[str, StrategyConfig]] = None,
               run_dir: Optional[Path] = None,
               friction: Optional[FrictionModel] = None,
               seed: int = 42) -> dict:
    """Run a backtest. Returns paths and summary stats."""
    strategies = strategies or DEFAULT_STRATEGIES
    run_id = uuid.uuid4().hex[:8]
    run_dir = run_dir or Path("data/research/runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    rep = Replay(start_ts=start_ts, end_ts=end_ts, seed=seed)
    executor = SimulatedExecutor(
        clock=rep.clock,
        price_at=lambda t, m: rep.prices.at(t, m),
        friction=friction or FrictionModel(),
        rng=rep.rng,
    )
    state = HarnessState(
        capital_sol=capital_sol, cash_sol=capital_sol,
        smart_wallets=set(smart_wallets),
    )
    db = _open_db(run_dir)
    state.open_db = db
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("run_id", run_id))
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("start_ts", str(start_ts)))
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("end_ts", str(end_ts)))
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("seed", str(seed)))
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("capital_sol", str(capital_sol)))
    db.commit()

    open_trade_ids: dict[str, int] = {}
    sold_fraction: dict[str, float] = {}
    last_management = start_ts

    n_events = 0
    t0 = time.time()
    for ev in rep:
        n_events += 1
        # manage existing positions every 30s of replay time
        if ev.ts - last_management >= 30:
            _manage_positions(state, ev.ts, rep.prices, strategies, executor, db,
                              open_trade_ids, sold_fraction)
            last_management = ev.ts
        tid = _maybe_open(state, ev, strategies, executor, db)
        if tid is not None:
            open_trade_ids[ev.payload.get("mint", "")] = tid
            sold_fraction[ev.payload.get("mint", "")] = 0.0

    # final management at end_ts
    _manage_positions(state, end_ts, rep.prices, strategies, executor, db,
                      open_trade_ids, sold_fraction)
    # force-close any remaining positions
    for mint, pos in list(state.positions.items()):
        price = rep.prices.at(end_ts, mint) or pos.entry_price_sol
        sol_value = pos.tokens * price
        state.cash_sol += sol_value
        tid = open_trade_ids.get(mint)
        if tid is not None:
            _record_trade_close(db, tid, end_ts, sol_value, fees=0.0,
                                peak=pos.high_water / pos.entry_price_sol,
                                hold_s=end_ts - pos.opened_at, exit_kind="forced_eod")
        del state.positions[mint]

    elapsed = time.time() - t0
    summary = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "events": n_events,
        "elapsed_s": elapsed,
        "final_cash_sol": state.cash_sol,
        "starting_capital_sol": capital_sol,
    }
    db.close()
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--capital", type=float, default=0.667, help="capital in SOL")
    p.add_argument("--smart-wallets", default="data/smart_wallets.json")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO)
    import datetime as dt
    s = dt.datetime.fromisoformat(args.start).replace(tzinfo=dt.timezone.utc).timestamp()
    e = dt.datetime.fromisoformat(args.end).replace(tzinfo=dt.timezone.utc).timestamp()
    sw = json.loads(Path(args.smart_wallets).read_text()) if Path(args.smart_wallets).exists() else []
    summary = run_replay(start_ts=s, end_ts=e, capital_sol=args.capital,
                         smart_wallets=sw, seed=args.seed)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
