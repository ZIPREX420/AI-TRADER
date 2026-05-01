"""Run.db → metrics + bootstrap CIs + OOS + walk-forward stability."""
from __future__ import annotations

import math
import random
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class Metrics:
    n: int
    total_roi: float
    wr: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    expectancy: float
    sharpe_lite: float
    mdd: float
    recovery_days: float
    tail_p95_gain: float
    tail_p05_loss: float
    trades_per_day: float
    alpha_per_trade: float
    consistency: float
    bootstrap_mean_5: float
    bootstrap_mean_95: float


def _load_rois(db_path: str) -> list[tuple[float, float, float]]:
    """Returns list of (ts_close, roi, sol_pnl) sorted by ts_close, only closed trades."""
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            "SELECT ts_close, sol_in, sol_out, fees, roi FROM trades "
            "WHERE ts_close IS NOT NULL ORDER BY ts_close ASC"
        ).fetchall()
    finally:
        db.close()
    out = []
    for ts_close, sol_in, sol_out, fees, roi in rows:
        sol_in = float(sol_in or 0)
        sol_out = float(sol_out or 0)
        fees = float(fees or 0)
        pnl = sol_out - sol_in - fees
        roi_v = float(roi if roi is not None else (pnl / max(sol_in, 1e-9)))
        out.append((float(ts_close), roi_v, pnl))
    return out


def _quantile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = q * (len(xs) - 1)
    f = math.floor(k); c = math.ceil(k)
    if f == c:
        return xs[f]
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _bootstrap_mean(rois: list[float], n_resample: int = 1000, seed: int = 42) -> tuple[float, float]:
    if len(rois) < 5:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = []
    n = len(rois)
    for _ in range(n_resample):
        sample = [rois[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    return _quantile(means, 0.05), _quantile(means, 0.95)


def _drawdown(cum_pnl: list[float]) -> tuple[float, float]:
    """Returns (max_drawdown_sol, median_recovery_days_unused_placeholder)."""
    peak = -1e18
    mdd = 0.0
    for v in cum_pnl:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd, 0.0


def _consistency(rois: list[tuple[float, float]]) -> float:
    """std(monthly_sharpe) / |mean(monthly_sharpe)|."""
    if not rois:
        return 0.0
    by_month: dict[str, list[float]] = {}
    import datetime as dt
    for ts, r in rois:
        key = dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m")
        by_month.setdefault(key, []).append(r)
    monthly_sharpes = []
    for vs in by_month.values():
        if len(vs) >= 3:
            s = _stdev(vs)
            if s > 0:
                monthly_sharpes.append((sum(vs) / len(vs)) / s)
    if not monthly_sharpes:
        return 0.0
    mean = sum(monthly_sharpes) / len(monthly_sharpes)
    if abs(mean) < 1e-9:
        return 999.0
    return _stdev(monthly_sharpes) / abs(mean)


def compute(db_path: str) -> Metrics:
    rows = _load_rois(db_path)
    if not rows:
        return Metrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    rois = [r for _, r, _ in rows]
    pnls = [p for _, _, p in rows]
    cum_pnl = []
    s = 0.0
    for p in pnls:
        s += p; cum_pnl.append(s)
    n = len(rois)
    wins = [r for r in rois if r > 0]
    losses = [r for r in rois if r < 0]
    sum_pos = sum(wins) if wins else 0.0
    sum_neg = sum(losses) if losses else 0.0
    pf = sum_pos / abs(sum_neg) if sum_neg < 0 else (math.inf if sum_pos > 0 else 0.0)
    span_days = max(1.0, (rows[-1][0] - rows[0][0]) / 86400.0)
    mdd, _ = _drawdown(cum_pnl)
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    wr = len(wins) / n
    expectancy = wr * avg_win + (1 - wr) * avg_loss
    sharpe = (sum(rois) / n) / max(_stdev(rois), 1e-3)
    b5, b95 = _bootstrap_mean(rois)
    return Metrics(
        n=n, total_roi=sum(rois), wr=wr,
        avg_win=avg_win, avg_loss=avg_loss,
        profit_factor=pf, expectancy=expectancy,
        sharpe_lite=sharpe, mdd=mdd, recovery_days=0.0,
        tail_p95_gain=_quantile(rois, 0.95),
        tail_p05_loss=_quantile(rois, 0.05),
        trades_per_day=n / span_days,
        alpha_per_trade=sum(rois) / n,
        consistency=_consistency([(t, r) for t, r, _ in rows]),
        bootstrap_mean_5=b5, bootstrap_mean_95=b95,
    )


def oos_split(db_path: str, train_frac: float = 0.7) -> dict:
    rows = _load_rois(db_path)
    if len(rows) < 10:
        return {"in_sample_sharpe": 0.0, "out_of_sample_sharpe": 0.0, "ratio": 0.0}
    cut = int(len(rows) * train_frac)
    is_rois = [r for _, r, _ in rows[:cut]]
    oos_rois = [r for _, r, _ in rows[cut:]]
    is_sharpe = (sum(is_rois) / max(len(is_rois), 1)) / max(_stdev(is_rois), 1e-3)
    oos_sharpe = (sum(oos_rois) / max(len(oos_rois), 1)) / max(_stdev(oos_rois), 1e-3)
    ratio = oos_sharpe / is_sharpe if is_sharpe != 0 else 0.0
    return {"in_sample_sharpe": is_sharpe, "out_of_sample_sharpe": oos_sharpe, "ratio": ratio}


def walk_forward(db_path: str, window_days: int = 30) -> dict:
    rows = _load_rois(db_path)
    if len(rows) < 5:
        return {"n_windows": 0, "mean_sharpe": 0.0, "std_sharpe": 0.0}
    t0 = rows[0][0]; tN = rows[-1][0]
    windows = []
    cur_start = t0
    while cur_start < tN:
        cur_end = cur_start + window_days * 86400
        wnd = [r for ts, r, _ in rows if cur_start <= ts < cur_end]
        if len(wnd) >= 3:
            sh = (sum(wnd) / len(wnd)) / max(_stdev(wnd), 1e-3)
            windows.append(sh)
        cur_start = cur_end
    if not windows:
        return {"n_windows": 0, "mean_sharpe": 0.0, "std_sharpe": 0.0}
    return {
        "n_windows": len(windows),
        "mean_sharpe": sum(windows) / len(windows),
        "std_sharpe": _stdev(windows),
    }


def report(db_path: str) -> dict:
    return {
        "metrics": asdict(compute(db_path)),
        "oos": oos_split(db_path),
        "walk_forward": walk_forward(db_path),
    }
