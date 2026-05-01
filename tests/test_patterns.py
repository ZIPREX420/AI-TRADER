"""Pattern mining over fixture run.db + fixture parquet."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import sqlite3
import tempfile
from pathlib import Path

from src.research import patterns
from src.research.run import RUN_SCHEMA


def _build_run(rois_with_payload):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False); f.close()
    db = sqlite3.connect(f.name); db.executescript(RUN_SCHEMA)
    t = 1_700_000_000.0
    for i, (roi, payload) in enumerate(rois_with_payload):
        db.execute(
            "INSERT INTO trades(ts_open,ts_close,mint,strategy,sol_in,sol_out,fees,roi,payload) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (t+i*60, t+(i+1)*60, f"M{i}", "S1", 0.05, 0.05*(1+roi), 0.0, roi, json.dumps(payload)),
        )
    db.commit(); db.close()
    return f.name


def test_fingerprint_clustering_separates_winners_losers():
    # 12 winners with one fingerprint
    win_payload = {"has_cluster": True, "prepump_kind": 1, "wallet_score_max": 0.8,
                   "regime": "NORMAL", "entropy": 4.0, "route_compression": True,
                   "lp_injection": True, "pumpfun_grad": False}
    lose_payload = {"has_cluster": False, "prepump_kind": 0, "wallet_score_max": 0.3,
                    "regime": "VOLATILE", "entropy": 2.5, "route_compression": False,
                    "lp_injection": False, "pumpfun_grad": True}
    data = [(0.5, win_payload)] * 12 + [(-0.5, lose_payload)] * 12
    db = _build_run(data)
    buckets = patterns.cluster_fingerprints(db, min_n=10)
    kinds = {b.kind for b in buckets}
    assert "profitable" in kinds
    assert "losing" in kinds


def test_write_outputs_creates_files(tmp_path):
    out = patterns.write_outputs(tmp_path, [], [], [])
    for p in out.values():
        assert Path(p).exists()


if __name__ == "__main__":
    test_fingerprint_clustering_separates_winners_losers()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        test_write_outputs_creates_files(Path(td))
    print("OK patterns")
