"""Portfolio sizer.

Given a combined `Signal`, returns it with `suggested_usd` filled in:

    suggested = clamp(
        equity * per_trade_pct * confidence_multiplier,
        0,
        per_trade_usd_cap,
    )
    if mode == DEGRADED_RPC:
        suggested *= degraded_rpc_size_factor

`confidence_multiplier` is a linear ramp from 0 at `min_confidence` to 1 at
1.0 -- so a signal that just clears the floor sizes very small. Signals
below `min_confidence` size to zero and are rejected downstream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from solalpha.domain import Signal
    from solalpha.foundation.config import AppConfig
    from solalpha.signal.mode_manager import ModeManager

_log = get_logger(__name__)


class PortfolioSizer:
    """Stateless sizer; reads live mode + cfg on every call."""

    def __init__(self, cfg: AppConfig, mode_manager: ModeManager) -> None:
        self._cfg = cfg
        self._mode_manager = mode_manager

    def size(self, signal: Signal) -> Signal:
        equity = self._cfg.risk.starting_equity_usd
        confidence = signal.confidence
        min_conf = self._cfg.risk.min_confidence
        if confidence < min_conf or equity <= 0:
            return signal.model_copy(update={"suggested_usd": 0.0})
        # Linear ramp from `min_conf` -> `1.0`.
        denom = max(1e-9, 1.0 - min_conf)
        multiplier = (confidence - min_conf) / denom
        multiplier = max(0.0, min(1.0, multiplier))
        base = equity * self._cfg.risk.per_trade_pct * multiplier
        # Cap by the hard per-trade USD limit.
        usd = min(base, self._cfg.risk.per_trade_usd_cap)
        # Mode-aware reduction.
        if self._mode_manager.mode == "DEGRADED_RPC":
            usd *= self._cfg.mode_manager.degraded_rpc_size_factor
        usd = max(0.0, usd)
        return signal.model_copy(update={"suggested_usd": usd})


__all__ = ["PortfolioSizer"]
