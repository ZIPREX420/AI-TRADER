"""Promotion gate: decides which discovered patterns become live shadow patterns."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from .evaluation import compute, oos_split, walk_forward, Metrics


@dataclass
class SelectionGate:
    n_trades_min: int = 100
    sharpe_min: float = 1.20
    pf_min: float = 1.60
    max_dd_pct: float = 0.30
    oos_ratio_min: float = 0.60
    bootstrap_5_pos: bool = True
    consistency_max: float = 1.50
    anti_overlap_max_pct: float = 0.05


@dataclass
class SelectionResult:
    promoted: bool
    metrics: Metrics
    oos_ratio: float
    walk_forward_stability: float
    rank_score: float
    failures: list[str]


def _rank_score(m: Metrics, oos_ratio: float) -> float:
    return (
        0.40 * min(m.sharpe_lite / 2.0, 1.0)
        + 0.30 * min(max(oos_ratio, 0.0), 1.0)
        + 0.20 * max(0.0, 1.0 - m.consistency / 2.0)
        + 0.10 * max(0.0, 1.0 - m.mdd / 0.30)
    )


def evaluate_run(run_db_path: str,
                 capital_sol: float,
                 anti_pattern_overlap_count: int = 0,
                 gate: Optional[SelectionGate] = None) -> SelectionResult:
    gate = gate or SelectionGate()
    metrics = compute(run_db_path)
    oos = oos_split(run_db_path, train_frac=0.7)
    wf = walk_forward(run_db_path, window_days=30)
    failures: list[str] = []

    if metrics.n < gate.n_trades_min:
        failures.append(f"n<{gate.n_trades_min}")
    if metrics.sharpe_lite < gate.sharpe_min:
        failures.append(f"sharpe<{gate.sharpe_min}")
    if metrics.profit_factor < gate.pf_min:
        failures.append(f"pf<{gate.pf_min}")
    if metrics.mdd > capital_sol * gate.max_dd_pct:
        failures.append(f"dd>{gate.max_dd_pct*100:.0f}%")
    if oos["ratio"] < gate.oos_ratio_min:
        failures.append(f"oos_ratio<{gate.oos_ratio_min}")
    if gate.bootstrap_5_pos and metrics.bootstrap_mean_5 <= 0:
        failures.append("bootstrap_5<=0")
    if metrics.consistency > gate.consistency_max:
        failures.append(f"consistency>{gate.consistency_max}")
    if metrics.n > 0 and (anti_pattern_overlap_count / metrics.n) > gate.anti_overlap_max_pct:
        failures.append(f"anti_overlap>{gate.anti_overlap_max_pct*100:.0f}%")

    promoted = len(failures) == 0
    return SelectionResult(
        promoted=promoted,
        metrics=metrics,
        oos_ratio=oos["ratio"],
        walk_forward_stability=wf.get("std_sharpe", 0.0),
        rank_score=_rank_score(metrics, oos["ratio"]),
        failures=failures,
    )


def write_selection(out_dir: Path, run_id: str, sel: SelectionResult) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / ("promoted_patterns.json" if sel.promoted else "rejected_patterns.json")
    existing = []
    if target.exists():
        try:
            existing = json.loads(target.read_text())
        except Exception:
            existing = []
    existing.append({
        "run_id": run_id,
        "metrics": asdict(sel.metrics),
        "oos_ratio": sel.oos_ratio,
        "rank_score": sel.rank_score,
        "failures": sel.failures,
    })
    target.write_text(json.dumps(existing, indent=2))
    return target
