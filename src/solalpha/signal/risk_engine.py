"""Hard risk engine -- the single gate every `Signal` must pass before becoming an order.

Every rule is enforced here:
  * kill switch armed
  * mint quarantined / blacklisted (sqlite-backed)
  * confidence below `risk.min_confidence`
  * open-position count at the *hard ceiling* (`max_open_positions_ceiling`)
  * mint has freeze authority or mint authority (when the mint cache is
    wired) -- both red flags for rug-prone tokens
  * suggested_usd above the per-trade USD cap -- scale down rather than reject
  * inflight orders per mint >= `max_inflight_per_mint`

The engine **fails CLOSED**: any internal exception while evaluating turns
into `RiskDecision(decision="rejected", reasons=("risk_internal_error: ...",))`.
The architecture invariant is that *no order ever flows through here on a
swallowed exception*.

The slippage and price-impact rules are evaluated by the execution plane
against live quote data (Phase 4); this engine asserts the hard ceilings
on the *config-defined* numbers here so misconfiguration alone cannot
loosen them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from solalpha.domain import RiskDecision
from solalpha.foundation import metrics
from solalpha.foundation.logging import bind_trace_id, get_logger

if TYPE_CHECKING:
    from solalpha.data.caches import MintMetadataCache
    from solalpha.domain import Signal
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.config import AppConfig
    from solalpha.foundation.state import SqliteStore
    from solalpha.observability.portfolio import PortfolioTracker
    from solalpha.signal.kill_switch import KillSwitch
    from solalpha.signal.mode_manager import ModeManager

_log = get_logger(__name__)


class RiskEngine:
    """Hard risk gate over `Signal` -> `RiskDecision`."""

    def __init__(
        self,
        cfg: AppConfig,
        store: SqliteStore,
        clock: Clock,
        kill_switch: KillSwitch,
        portfolio: PortfolioTracker,
        mode_manager: ModeManager,
        *,
        mint_cache: MintMetadataCache | None = None,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._clock = clock
        self._kill = kill_switch
        self._portfolio = portfolio
        self._mode_manager = mode_manager
        self._mint_cache = mint_cache
        # Inflight counter per mint. The execution plane decrements on
        # confirmation/failure via `on_order_resolved`.
        self._inflight: dict[str, int] = {}
        # Re-assert the config-level hard ceilings on construction so a
        # misconfigured override is caught at startup, not at decision time.
        risk = cfg.risk
        if risk.max_slippage_bps > risk.hard_slippage_ceiling_bps:
            raise ValueError(f"max_slippage_bps {risk.max_slippage_bps} exceeds hard ceiling")
        if risk.max_open_positions > risk.max_open_positions_ceiling:
            raise ValueError(f"max_open_positions {risk.max_open_positions} exceeds ceiling")
        if risk.max_price_impact_pct > risk.max_price_impact_ceiling_pct:
            raise ValueError(f"max_price_impact_pct {risk.max_price_impact_pct} exceeds ceiling")

    async def evaluate(self, signal: Signal) -> RiskDecision:
        with bind_trace_id(signal.trace_id):
            return await self._evaluate(signal)

    # ---- inflight bookkeeping ----

    def on_order_resolved(self, mint: str) -> None:
        n = self._inflight.get(mint, 0)
        if n <= 1:
            self._inflight.pop(mint, None)
        else:
            self._inflight[mint] = n - 1

    def inflight(self, mint: str) -> int:
        return self._inflight.get(mint, 0)

    # ---- core ----

    async def _evaluate(self, signal: Signal) -> RiskDecision:
        reasons: list[str] = []
        approved_usd = signal.suggested_usd
        try:
            risk = self._cfg.risk

            # 1. Kill switch -- absolute precedence.
            if self._kill.armed():
                reasons.append(f"kill_switch_armed:{self._kill.reason() or 'unknown'}")

            # 2. Confidence floor.
            if signal.confidence < risk.min_confidence:
                reasons.append(
                    f"confidence_below_min({signal.confidence:.3f}<{risk.min_confidence:.3f})"
                )

            # 3. Open-positions ceiling.
            open_n = self._portfolio.open_positions_count()
            if open_n >= risk.max_open_positions_ceiling:
                reasons.append(
                    f"max_open_positions_ceiling_hit({open_n}>={risk.max_open_positions_ceiling})"
                )

            # 4. Daily loss + loss streak (HALT-equivalent inside risk engine).
            daily = await self._portfolio.daily_pnl()
            equity = risk.starting_equity_usd
            if equity > 0 and daily.pnl_usd <= -equity * risk.daily_loss_pct:
                reasons.append(
                    f"daily_loss_limit({daily.pnl_usd:.2f}<=-{equity * risk.daily_loss_pct:.2f})"
                )
            if self._portfolio.loss_streak() >= risk.loss_streak_max:
                reasons.append(
                    f"loss_streak_hit({self._portfolio.loss_streak()}>={risk.loss_streak_max})"
                )

            # 5. Inflight cap per mint.
            inflight = self.inflight(signal.mint)
            if inflight >= risk.max_inflight_per_mint:
                reasons.append(f"inflight_per_mint_hit({inflight}>={risk.max_inflight_per_mint})")

            # 6. Quarantine table.
            quarantined = await self._is_quarantined(signal.mint)
            if quarantined:
                reasons.append("mint_quarantined")

            # 7. Blacklist table.
            blacklisted = await self._is_blacklisted(signal.mint)
            if blacklisted:
                reasons.append("mint_blacklisted")

            # 8. Mint authority / freeze authority (when the cache is wired).
            if self._mint_cache is not None and not reasons:
                # Only consult the cache when we'd otherwise approve --
                # avoids burning RPC budget on already-rejected signals.
                try:
                    md = await self._mint_cache.get(signal.mint)
                    if md.has_freeze_authority:
                        reasons.append("mint_has_freeze_authority")
                    if md.has_mint_authority:
                        reasons.append("mint_has_mint_authority")
                except Exception as e:
                    _log.warning(
                        "mint_metadata_lookup_failed",
                        mint=signal.mint,
                        exc=str(e),
                    )
                    reasons.append(f"mint_metadata_unavailable:{type(e).__name__}")

            # 9. Per-trade USD cap -- scale, do not reject.
            decision: str = "approved"
            if approved_usd > risk.per_trade_usd_cap:
                approved_usd = risk.per_trade_usd_cap
                decision = "scaled"

            if reasons:
                metrics.RISK_DECISIONS.labels(decision="rejected").inc()
                for r in reasons:
                    metrics.RISK_REJECTIONS.labels(reason=_metric_label(r)).inc()
                _log.warning(
                    "risk_rejected",
                    signal_id=signal.signal_id,
                    mint=signal.mint,
                    reasons=reasons,
                )
                return self._decision(signal, "rejected", 0.0, tuple(reasons))

            metrics.RISK_DECISIONS.labels(decision=decision).inc()
            # Approve / scale: also bump inflight so subsequent signals for
            # the same mint hit the cap until the execution plane resolves.
            self._inflight[signal.mint] = self._inflight.get(signal.mint, 0) + 1
            _log.info(
                "risk_approved",
                signal_id=signal.signal_id,
                mint=signal.mint,
                decision=decision,
                approved_usd=approved_usd,
                confidence=signal.confidence,
            )
            return self._decision(signal, decision, approved_usd, ())

        except Exception as e:
            # Fail CLOSED -- never let an exception leak an approval.
            metrics.RISK_DECISIONS.labels(decision="rejected").inc()
            metrics.RISK_REJECTIONS.labels(reason="internal_error").inc()
            _log.error(
                "risk_internal_error",
                signal_id=signal.signal_id,
                mint=signal.mint,
                exc=str(e),
                exc_type=type(e).__name__,
            )
            return self._decision(
                signal,
                "rejected",
                0.0,
                (f"risk_internal_error:{type(e).__name__}",),
            )

    def _decision(
        self,
        signal: Signal,
        decision: str,
        approved_usd: float,
        reasons: tuple[str, ...],
    ) -> RiskDecision:
        """Build a `RiskDecision`, stamping the shared clock + mode context.

        Every exit from `_evaluate` -- reject, approve/scale, or fail-closed --
        returns through here, so `signal_id`, the timestamp, and the
        mode-at-decision are wired in exactly one place.
        """
        return RiskDecision(
            signal_id=signal.signal_id,
            decision=decision,
            approved_usd=approved_usd,
            reasons=reasons,
            ts=self._clock.now(),
            mode_at_decision=self._mode_manager.mode,
        )

    # ---- helpers ----

    async def _is_quarantined(self, key: str) -> bool:
        row = await self._store.fetch_one("SELECT until FROM quarantine WHERE key = ?", (key,))
        if row is None:
            return False
        until = row.get("until")
        if not isinstance(until, str):
            return True
        # Compare with current clock.
        return until > self._clock.now().isoformat()

    async def _is_blacklisted(self, key: str) -> bool:
        row = await self._store.fetch_one("SELECT 1 FROM blacklist WHERE key = ?", (key,))
        return row is not None


def _metric_label(reason: str) -> str:
    """Extract the bare rule name from `rule:detail` for Prometheus labels."""
    return reason.split("(", 1)[0].split(":", 1)[0]


__all__ = ["RiskEngine"]
