"""RiskEngine: every hard rule + fail-closed behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from solalpha.domain import DetectorSignal, Signal
from solalpha.foundation.bus import Bus
from solalpha.foundation.health import HealthRegistry
from solalpha.observability.portfolio import PortfolioTracker
from solalpha.signal.kill_switch import KillSwitch
from solalpha.signal.mode_manager import ModeManager
from solalpha.signal.risk_engine import RiskEngine

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)


def _signal(*, confidence: float = 0.9, suggested_usd: float = 20.0, mint: str = "M1") -> Signal:
    ds = DetectorSignal(detector="cluster", mint=mint, score=confidence, observed_at=_NOW)
    return Signal(
        signal_id=f"sg-{mint}",
        created_at=_NOW,
        mint=mint,
        direction="buy",
        detectors=(ds,),
        confidence=confidence,
        suggested_usd=suggested_usd,
        rationale="test",
        inputs_hash="h",
        trace_id=f"t-{mint}",
    )


async def _engine(store: object, clock: object, app_config: object) -> tuple:
    ks = KillSwitch(store, clock, app_config.kill_switch.file_path)  # type: ignore[arg-type]
    await ks.load()
    pt = PortfolioTracker(store, clock)  # type: ignore[arg-type]
    await pt.load()
    health = HealthRegistry(clock)  # type: ignore[arg-type]
    mm = ModeManager(app_config, Bus(), store, clock, ks, pt, health)  # type: ignore[arg-type]
    return ks, pt, RiskEngine(app_config, store, clock, ks, pt, mm)  # type: ignore[arg-type]


async def test_healthy_signal_approved(store: object, clock: object, app_config: object) -> None:
    _, _, risk = await _engine(store, clock, app_config)
    decision = await risk.evaluate(_signal())
    assert decision.decision in ("approved", "scaled")
    assert not decision.reasons


async def test_kill_switch_rejects(store: object, clock: object, app_config: object) -> None:
    ks, _, risk = await _engine(store, clock, app_config)
    await ks.arm("halt", "op")
    decision = await risk.evaluate(_signal())
    assert decision.decision == "rejected"
    assert any("kill_switch_armed" in r for r in decision.reasons)


async def test_low_confidence_rejected(store: object, clock: object, app_config: object) -> None:
    _, _, risk = await _engine(store, clock, app_config)
    decision = await risk.evaluate(_signal(confidence=0.10))
    assert decision.decision == "rejected"
    assert any("confidence_below_min" in r for r in decision.reasons)


async def test_inflight_cap_rejects_second(
    store: object, clock: object, app_config: object
) -> None:
    _, _, risk = await _engine(store, clock, app_config)
    d1 = await risk.evaluate(_signal(mint="MX"))
    assert d1.decision in ("approved", "scaled")
    # Second signal for the same mint hits the inflight cap.
    d2 = await risk.evaluate(_signal(mint="MX").model_copy(update={"signal_id": "sg-2"}))
    assert d2.decision == "rejected"
    assert any("inflight_per_mint_hit" in r for r in d2.reasons)
    # Resolving the order frees the slot.
    risk.on_order_resolved("MX")
    assert risk.inflight("MX") == 0


async def test_quarantined_mint_rejected(store: object, clock: object, app_config: object) -> None:
    _, _, risk = await _engine(store, clock, app_config)
    until = (clock.now() + timedelta(hours=1)).isoformat()  # type: ignore[attr-defined]
    await store.execute(  # type: ignore[attr-defined]
        "INSERT INTO quarantine (key, kind, until, reason) VALUES (?, ?, ?, ?)",
        ("MQ", "mint", until, "test"),
    )
    decision = await risk.evaluate(_signal(mint="MQ"))
    assert decision.decision == "rejected"
    assert any("quarantined" in r for r in decision.reasons)


async def test_blacklisted_mint_rejected(store: object, clock: object, app_config: object) -> None:
    _, _, risk = await _engine(store, clock, app_config)
    await store.execute(  # type: ignore[attr-defined]
        "INSERT INTO blacklist (key, kind, added_at, reason) VALUES (?, ?, ?, ?)",
        ("MB", "mint", clock.now().isoformat(), "test"),  # type: ignore[attr-defined]
    )
    decision = await risk.evaluate(_signal(mint="MB"))
    assert decision.decision == "rejected"
    assert any("blacklisted" in r for r in decision.reasons)


async def test_per_trade_cap_scales(store: object, clock: object, app_config: object) -> None:
    _, _, risk = await _engine(store, clock, app_config)
    huge = _signal(suggested_usd=10_000.0)
    decision = await risk.evaluate(huge)
    assert decision.decision == "scaled"
    assert decision.approved_usd <= app_config.risk.per_trade_usd_cap  # type: ignore[attr-defined]


async def test_fails_closed_on_internal_error(
    store: object, clock: object, app_config: object
) -> None:
    """A broken dependency must produce `rejected`, never an approval."""
    _, _, risk = await _engine(store, clock, app_config)

    class _Boom:
        def armed(self) -> bool:
            raise RuntimeError("kill switch exploded")

        def reason(self) -> str | None:
            return None

    risk._kill = _Boom()  # type: ignore[attr-defined]
    decision = await risk.evaluate(_signal())
    assert decision.decision == "rejected"
    assert any("risk_internal_error" in r for r in decision.reasons)


async def test_decision_helper_stamps_shared_context(
    store: object, clock: object, app_config: object
) -> None:
    """`_decision` wires signal_id, clock timestamp, and mode in one place."""
    _, _, risk = await _engine(store, clock, app_config)
    sig = _signal()
    decision = risk._decision(sig, "scaled", 12.5, ("r1", "r2"))  # type: ignore[attr-defined]
    assert decision.signal_id == sig.signal_id
    assert decision.decision == "scaled"
    assert decision.approved_usd == 12.5
    assert decision.reasons == ("r1", "r2")
    assert decision.ts == clock.now()  # type: ignore[attr-defined]
    assert decision.mode_at_decision == risk._mode_manager.mode  # type: ignore[attr-defined]
