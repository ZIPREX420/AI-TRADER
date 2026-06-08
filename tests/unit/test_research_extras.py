"""research: pattern miner, strategy selector, readonly guard."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from solalpha.domain import NormalizedSwap
from solalpha.foundation.config import SignalsWeightsConfig
from solalpha.foundation.errors import ResearchWriteBlocked
from solalpha.research.pattern_miner import mine_patterns
from solalpha.research.readonly_guard import ReadonlyGuard, assert_readonly
from solalpha.research.strategy_selector import StrategyCandidate, select_best

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
_WEIGHTS = SignalsWeightsConfig(prepump=0.35, cluster=0.45, flow_anomaly=0.20)


def _swap(usd: float, side: str = "buy", mint: str = "M1") -> NormalizedSwap:
    return NormalizedSwap(
        event_id=f"e-{usd}-{side}-{mint}",
        signature="s",
        slot=1,
        block_time=_NOW,
        venue="jupiter",
        wallet="W1",
        mint=mint,
        side=side,  # type: ignore[arg-type]
        input_mint="So11111111111111111111111111111111111111112",
        output_mint=mint,
        input_amount_raw=100,
        output_amount_raw=200,
        usd_value=usd,
        received_at=_NOW,
    )


# ---- pattern miner ----


def test_mine_patterns_empty() -> None:
    assert mine_patterns([]) == []


def test_mine_patterns_finds_a_cluster() -> None:
    # 20 near-identical small buys form one tight DBSCAN cluster.
    swaps = [_swap(10.0) for _ in range(20)]
    clusters = mine_patterns(swaps, eps=0.5, min_samples=5)
    assert len(clusters) >= 1
    biggest = clusters[0]
    assert biggest.size >= 5
    assert "M1" in biggest.mints


def test_mine_patterns_noise_excluded() -> None:
    # A handful of scattered swaps with min_samples high -> all noise.
    swaps = [_swap(float(10**i)) for i in range(4)]
    clusters = mine_patterns(swaps, eps=0.1, min_samples=10)
    assert clusters == []


# ---- strategy selector ----


def test_select_best_no_candidates() -> None:
    choice = select_best([], min_oos_sharpe=0.5)
    assert choice.chosen is None
    assert "no candidates" in choice.reason


def test_select_best_prefers_full_pass() -> None:
    cands = [
        StrategyCandidate(
            name="A",
            weights=_WEIGHTS,
            pass_count=3,
            fold_count=3,
            avg_sharpe=1.0,
            avg_pnl_usd=10.0,
        ),
        StrategyCandidate(
            name="B",
            weights=_WEIGHTS,
            pass_count=2,
            fold_count=3,
            avg_sharpe=5.0,
            avg_pnl_usd=99.0,
        ),
    ]
    choice = select_best(cands, min_oos_sharpe=0.5)
    # B has higher Sharpe but failed a fold; A cleared all folds.
    assert choice.chosen is not None
    assert choice.chosen.name == "A"


def test_select_best_none_pass_returns_runner_up() -> None:
    cands = [
        StrategyCandidate(
            name="A",
            weights=_WEIGHTS,
            pass_count=1,
            fold_count=3,
            avg_sharpe=2.0,
            avg_pnl_usd=10.0,
        ),
        StrategyCandidate(
            name="B",
            weights=_WEIGHTS,
            pass_count=0,
            fold_count=3,
            avg_sharpe=1.0,
            avg_pnl_usd=5.0,
        ),
    ]
    choice = select_best(cands, min_oos_sharpe=0.5)
    assert choice.chosen is None
    assert choice.runner_up is not None
    assert choice.runner_up.name == "A"


# ---- readonly guard ----


async def test_readonly_guard_blocks_execute(store: object) -> None:
    guard = ReadonlyGuard(store)  # type: ignore[arg-type]
    with pytest.raises(ResearchWriteBlocked):
        await guard.execute("DELETE FROM kill_switch")


async def test_readonly_guard_blocks_journal(store: object) -> None:
    guard = ReadonlyGuard(store)  # type: ignore[arg-type]
    with pytest.raises(ResearchWriteBlocked):
        await guard.journal("evt", {"a": 1})


async def test_readonly_guard_allows_reads(store: object) -> None:
    guard = ReadonlyGuard(store)  # type: ignore[arg-type]
    row = await guard.fetch_one("SELECT armed FROM kill_switch WHERE id = 1")
    assert row is not None


def test_assert_readonly_rejects_raw_store(store: object) -> None:
    with pytest.raises(ResearchWriteBlocked):
        assert_readonly(store)
    # A guarded store passes.
    assert_readonly(ReadonlyGuard(store))  # type: ignore[arg-type]
