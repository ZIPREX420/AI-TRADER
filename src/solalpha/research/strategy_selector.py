"""Strategy selector -- ranks detector-weight presets by walk-forward metrics."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from solalpha.foundation.config import SignalsWeightsConfig


class StrategyCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    weights: SignalsWeightsConfig
    pass_count: int
    fold_count: int
    avg_sharpe: float
    avg_pnl_usd: float


class StrategyChoice(BaseModel):
    model_config = ConfigDict(frozen=True)

    chosen: StrategyCandidate | None
    runner_up: StrategyCandidate | None
    reason: str


def select_best(
    candidates: list[StrategyCandidate],
    *,
    min_oos_sharpe: float,
) -> StrategyChoice:
    if not candidates:
        return StrategyChoice(chosen=None, runner_up=None, reason="no candidates")
    passing = [c for c in candidates if c.fold_count > 0 and c.pass_count == c.fold_count]
    ranked = sorted(
        passing or candidates,
        key=lambda c: (c.avg_sharpe, c.avg_pnl_usd),
        reverse=True,
    )
    if not passing:
        top = ranked[0]
        return StrategyChoice(
            chosen=None,
            runner_up=top,
            reason=(
                f"no candidate passed all folds at min_oos_sharpe={min_oos_sharpe:.2f}; "
                f"top by Sharpe was {top.name}"
            ),
        )
    chosen = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None
    return StrategyChoice(
        chosen=chosen,
        runner_up=runner,
        reason=f"{chosen.name} cleared all {chosen.fold_count} folds",
    )


__all__ = ["StrategyCandidate", "StrategyChoice", "select_best"]
