"""Research plane.

Historical backfill, deterministic replay, walk-forward metrics, pattern
mining, and strategy selection. The plane is read-only against the live
SQLite store via `ReadonlyGuard`; writes go only to the parquet research
dataset and a temporary replay DB.

The CLI commands resolve here:
  * `solalpha research backfill --since=...`  -> `run_backfill`
  * `solalpha research replay <session>`      -> `replay_session`
  * `solalpha research walkforward`           -> `run_walkforward`
"""

from __future__ import annotations

from solalpha.research.backfill import run_backfill
from solalpha.research.metrics import (
    SessionMetrics,
    TradeReturn,
    compute_session_metrics,
    trades_from_fills,
)
from solalpha.research.pattern_miner import PatternCluster, mine_patterns
from solalpha.research.readonly_guard import ReadonlyGuard, assert_readonly
from solalpha.research.replay import replay_session, run_walkforward
from solalpha.research.sim_executor import build_sim_executor
from solalpha.research.strategy_selector import (
    StrategyCandidate,
    StrategyChoice,
    select_best,
)

__all__ = [
    "PatternCluster",
    "ReadonlyGuard",
    "SessionMetrics",
    "StrategyCandidate",
    "StrategyChoice",
    "TradeReturn",
    "assert_readonly",
    "build_sim_executor",
    "compute_session_metrics",
    "mine_patterns",
    "replay_session",
    "run_backfill",
    "run_walkforward",
    "select_best",
    "trades_from_fills",
]
