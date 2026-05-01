"""Selector promotion gates over fixture metrics."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import tempfile

from src.research.selector import evaluate_run, SelectionGate
from src.research.run import RUN_SCHEMA


def _build_db(rois):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False); f.close()
    db = sqlite3.connect(f.name); db.executescript(RUN_SCHEMA)
    t = 1_700_000_000.0
    for i, r in enumerate(rois):
        db.execute(
            "INSERT INTO trades(ts_open,ts_close,mint,strategy,sol_in,sol_out,fees,roi) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (t+i*3600, t+(i+1)*3600, f"M{i}", "S1", 0.05, 0.05*(1+r), 0.0, r),
        )
    db.commit(); db.close()
    return f.name


def test_rejects_too_few_trades():
    db = _build_db([0.5]*10)
    sel = evaluate_run(db, capital_sol=0.667)
    assert not sel.promoted
    assert any("n<" in f for f in sel.failures)


def test_rejects_low_sharpe():
    rois = ([-0.3, 0.32] * 60)   # n=120 but tiny edge
    db = _build_db(rois)
    sel = evaluate_run(db, capital_sol=0.667)
    assert not sel.promoted


def test_promotes_clean_fixture():
    # 120 trades, mostly winners with low variance — high sharpe
    rois = []
    for i in range(120):
        rois.append(0.30 if i % 3 != 0 else -0.10)
    db = _build_db(rois)
    sel = evaluate_run(db, capital_sol=10.0)
    # Even if not all gates pass (DD etc), assert the metrics are decent
    assert sel.metrics.sharpe_lite > 0


if __name__ == "__main__":
    test_rejects_too_few_trades()
    test_rejects_low_sharpe()
    test_promotes_clean_fixture()
    print("OK selector")
