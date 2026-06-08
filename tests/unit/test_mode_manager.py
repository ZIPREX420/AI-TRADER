"""ModeManager: HALT latch + immediate exit, healthy default, operator override."""

from __future__ import annotations

import json

import pytest

from solalpha.foundation.bus import Bus
from solalpha.foundation.health import HealthRegistry, Probe
from solalpha.observability.portfolio import PortfolioTracker
from solalpha.signal.kill_switch import KillSwitch
from solalpha.signal.mode_manager import ModeManager

pytestmark = pytest.mark.unit


async def _build(store: object, clock: object, app_config: object) -> tuple:
    ks = KillSwitch(store, clock, app_config.kill_switch.file_path)  # type: ignore[arg-type]
    await ks.load()
    pt = PortfolioTracker(store, clock)  # type: ignore[arg-type]
    await pt.load()
    health = HealthRegistry(clock)  # type: ignore[arg-type]
    mm = ModeManager(app_config, Bus(), store, clock, ks, pt, health)  # type: ignore[arg-type]
    return ks, pt, health, mm


async def test_starts_in_paper(store: object, clock: object, app_config: object) -> None:
    _, _, _, mm = await _build(store, clock, app_config)
    assert mm.mode == "PAPER"


async def test_stays_paper_when_not_live_eligible(
    store: object, clock: object, app_config: object
) -> None:
    _, _, health, mm = await _build(store, clock, app_config)
    snap = await health.snapshot()
    await mm.tick(snap)
    assert mm.mode == "PAPER"


async def test_kill_switch_latches_halt(store: object, clock: object, app_config: object) -> None:
    ks, _, health, mm = await _build(store, clock, app_config)
    snap = await health.snapshot()
    await mm.tick(snap)
    assert mm.mode == "PAPER"
    await ks.arm("test", "operator")
    await mm.tick(snap)
    assert mm.mode == "HALT"  # latches immediately, no hysteresis


async def test_halt_exits_immediately_on_disarm(
    store: object, clock: object, app_config: object
) -> None:
    ks, _, health, mm = await _build(store, clock, app_config)
    snap = await health.snapshot()
    await ks.arm("test", "operator")
    await mm.tick(snap)
    assert mm.mode == "HALT"
    await ks.disarm("operator")
    await mm.tick(snap)
    assert mm.mode == "PAPER"  # operator action resumes immediately


async def test_transitions_recorded(store: object, clock: object, app_config: object) -> None:
    ks, _, health, mm = await _build(store, clock, app_config)
    snap = await health.snapshot()
    await ks.arm("t", "op")
    await mm.tick(snap)
    rows = await store.fetch_all(  # type: ignore[attr-defined]
        "SELECT from_mode, to_mode FROM mode_transitions ORDER BY id"
    )
    assert ("PAPER", "HALT") in [(r["from_mode"], r["to_mode"]) for r in rows]


# ---- operator override (`solalpha mode set`) ----


async def test_operator_override_absent(store: object, clock: object, app_config: object) -> None:
    _, _, _, mm = await _build(store, clock, app_config)
    assert mm._operator_override() is None


async def test_operator_override_malformed_is_ignored(
    store: object, clock: object, app_config: object
) -> None:
    _, _, health, mm = await _build(store, clock, app_config)
    path = app_config.persistence.data_dir / ".operator_mode"  # type: ignore[attr-defined]
    path.write_text("{not valid json", encoding="utf-8")
    assert mm._operator_override() is None
    # A corrupt probe file must never wedge the tick loop.
    await mm.tick(await health.snapshot())
    assert mm.mode == "PAPER"


async def test_operator_override_rejects_non_paper_mode(
    store: object, clock: object, app_config: object
) -> None:
    _, _, _, mm = await _build(store, clock, app_config)
    path = app_config.persistence.data_dir / ".operator_mode"  # type: ignore[attr-defined]
    path.write_text(json.dumps({"mode": "LIVE", "reason": "nope"}), encoding="utf-8")
    # LIVE is health-driven; an operator may only pin PAPER.
    assert mm._operator_override() is None


async def test_operator_override_pins_paper_immediately(
    store: object, clock: object, app_config: object
) -> None:
    _, _, health, mm = await _build(store, clock, app_config)

    async def _exec_down() -> Probe:
        return Probe(status="down")

    # Drive the manager out of PAPER into DEGRADED_EXEC via a down probe.
    health.register("jupiter", _exec_down)
    await mm.tick(await health.snapshot())  # records the pending transition
    assert mm.mode == "PAPER"
    clock.advance(app_config.mode_manager.hysteresis_to_paper_s + 1.0)  # type: ignore[attr-defined]
    await mm.tick(await health.snapshot())
    assert mm.mode == "DEGRADED_EXEC"
    # Operator pins PAPER -> applied on the very next tick, no hysteresis.
    path = app_config.persistence.data_dir / ".operator_mode"  # type: ignore[attr-defined]
    path.write_text(json.dumps({"mode": "PAPER", "reason": "drill"}), encoding="utf-8")
    await mm.tick(await health.snapshot())
    assert mm.mode == "PAPER"


async def test_kill_switch_outranks_operator_override(
    store: object, clock: object, app_config: object
) -> None:
    ks, _, health, mm = await _build(store, clock, app_config)
    path = app_config.persistence.data_dir / ".operator_mode"  # type: ignore[attr-defined]
    path.write_text(json.dumps({"mode": "PAPER", "reason": "x"}), encoding="utf-8")
    await ks.arm("emergency", "operator")
    await mm.tick(await health.snapshot())
    # A real HALT condition always wins over an operator PAPER pin.
    assert mm.mode == "HALT"
