"""Evaluation metrics on a synthetic in-memory run.db."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import tempfile
import time
from pathlib import Path

from src.research.evaluation import compute, oos_split, walk_forward
from src.research.run import RUN_SCHEMA


def _build_db(rois: list[float]) -> str:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    db = sqlite3.connect(f.name)
    db.executescript(RUN_SCHEMA)
    base_t = 1_700_000_000.0
    for i, r in enumerate(rois):
        sol_in = 0.05
        sol_out = sol_in * (1 + r)
        db.execute(
            "INSERT INTO trades(ts_open,ts_close,mint,strategy,sol_in,sol_out,fees,roi) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (base_t + i * 3600, base_t + (i+1) * 3600, f"M{i:03d}",
             "S1", sol_in, sol_out, 0.0, r),
        )
    db.commit(); db.close()
    return f.name


def test_metrics_basic():
    rois = [0.5, -0.2, 0.8, -0.4, 1.5, -0.1, 0.3, -0.5, 0.6, 0.2]
    p = _build_db(rois)
    m = compute(p)
    assert m.n == len(rois)
    assert 0 < m.wr < 1
    assert m.profit_factor > 0
    assert m.tail_p95_gain > 0
    assert m.tail_p05_loss < 0


def test_bootstrap_ci_widens_with_variance():
    high_var = [-0.5, 1.5, -0.4, 1.2, -0.3, 1.0, -0.6, 1.3, -0.2, 1.1, -0.4, 0.9]
    low_var  = [0.1, 0.2, 0.05, 0.15, 0.12, 0.08, 0.10, 0.13, 0.07, 0.11, 0.09, 0.10]
    m_h = compute(_build_db(high_var))
    m_l = compute(_build_db(low_var))
    span_h = m_h.bootstrap_mean_95 - m_h.bootstrap_mean_5
    span_l = m_l.bootstrap_mean_95 - m_l.bootstrap_mean_5
    assert span_h > span_l


def test_oos_split_returns_ratio():
    rois = [0.2] * 14
    p = _build_db(rois)
    s = oos_split(p, train_frac=0.7)
    assert "in_sample_sharpe" in s
    assert "ratio" in s


def test_walk_forward_groups_by_window():
    rois = [0.1] * 60
    p = _build_db(rois)
    w = walk_forward(p, window_days=30)
    assert w["n_windows"] >= 1


if __name__ == "__main__":
    test_metrics_basic()
    test_bootstrap_ci_widens_with_variance()
    test_oos_split_returns_ratio()
    test_walk_forward_groups_by_window()
    print("OK evaluation")
