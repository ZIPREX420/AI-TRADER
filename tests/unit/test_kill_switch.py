"""KillSwitch: arm/disarm, file-probe sync, persistence."""

from __future__ import annotations

import pytest

from solalpha.signal.kill_switch import KillSwitch

pytestmark = pytest.mark.unit


async def test_starts_disarmed(store: object, clock: object, app_config: object) -> None:
    ks = KillSwitch(store, clock, app_config.kill_switch.file_path)  # type: ignore[arg-type]
    await ks.load()
    assert ks.armed() is False


async def test_arm_sets_state_and_file(store: object, clock: object, app_config: object) -> None:
    ks = KillSwitch(store, clock, app_config.kill_switch.file_path)  # type: ignore[arg-type]
    await ks.load()
    await ks.arm("manual halt", "operator")
    assert ks.armed() is True
    assert ks.reason() == "manual halt"
    assert app_config.kill_switch.file_path.exists()  # type: ignore[attr-defined]


async def test_disarm_clears_state_and_file(
    store: object, clock: object, app_config: object
) -> None:
    ks = KillSwitch(store, clock, app_config.kill_switch.file_path)  # type: ignore[arg-type]
    await ks.load()
    await ks.arm("x", "op")
    await ks.disarm("op")
    assert ks.armed() is False
    assert not app_config.kill_switch.file_path.exists()  # type: ignore[attr-defined]


async def test_persists_across_reload(store: object, clock: object, app_config: object) -> None:
    ks1 = KillSwitch(store, clock, app_config.kill_switch.file_path)  # type: ignore[arg-type]
    await ks1.load()
    await ks1.arm("persisted", "op")
    # A fresh KillSwitch over the same store sees the armed state.
    ks2 = KillSwitch(store, clock, app_config.kill_switch.file_path)  # type: ignore[arg-type]
    await ks2.load()
    assert ks2.armed() is True
    assert ks2.reason() == "persisted"


async def test_file_probe_arms(store: object, clock: object, app_config: object) -> None:
    fp = app_config.kill_switch.file_path  # type: ignore[attr-defined]
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.touch()
    ks = KillSwitch(store, clock, fp)  # type: ignore[arg-type]
    await ks.load()
    # File present at load time -> armed.
    assert ks.armed() is True
