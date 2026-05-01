"""ModeManager state-machine tests. Pure logic, no I/O beyond tmp_path."""
from __future__ import annotations

import time

from core.types import Mode
from runtime.mode_manager import ModeManager


def test_boots_in_live(tmp_path):
    mm = ModeManager(state_dir=str(tmp_path), initial=Mode.LIVE)
    assert mm.current().mode == Mode.LIVE


def test_halt_file_overrides_all(tmp_path):
    mm = ModeManager(state_dir=str(tmp_path), initial=Mode.LIVE)
    halt = tmp_path / "HALT"
    halt.write_text("manual")
    new_mode = mm.evaluate()
    assert new_mode == Mode.HALT
    assert mm.is_halted()


def test_consec_not_confirmed_triggers_degraded_exec(tmp_path):
    mm = ModeManager(state_dir=str(tmp_path), initial=Mode.LIVE)
    for _ in range(3):
        mm.report_exec_result(False, error="not_confirmed")
    new_mode = mm.evaluate()
    assert new_mode == Mode.DEGRADED_EXEC


def test_streams_silent_triggers_degraded_rpc(tmp_path):
    mm = ModeManager(state_dir=str(tmp_path), initial=Mode.LIVE)
    # Endpoint healthy in the past, then went silent for >60s
    mm._stream_last_ok["helius"] = time.time() - 120
    mm._stream_last_ok["quicknode"] = time.time() - 200
    new_mode = mm.evaluate()
    assert new_mode == Mode.DEGRADED_RPC


def test_executor_routing_uses_mock_on_degraded_exec(tmp_path):
    mm = ModeManager(state_dir=str(tmp_path), initial=Mode.LIVE)

    class _Live:
        kind = "live"

    class _Mock:
        kind = "mock"

    live, mock = _Live(), _Mock()
    mm.bind_executors(live=live, mock=mock)
    assert mm.executor() is live  # LIVE → live
    for _ in range(3):
        mm.report_exec_result(False, error="not_confirmed")
    mm.evaluate()
    assert mm.current().mode == Mode.DEGRADED_EXEC
    assert mm.executor() is mock  # paper-route active


def test_manual_paper_override(tmp_path):
    mm = ModeManager(state_dir=str(tmp_path), initial=Mode.LIVE)
    mm.manual_paper("test")
    assert mm.evaluate() == Mode.PAPER
    mm.clear_manual()
    assert mm.evaluate() == Mode.LIVE


def test_persistence_survives_reload(tmp_path):
    mm = ModeManager(state_dir=str(tmp_path), initial=Mode.LIVE)
    mm.manual_paper("persist_test")
    # New instance reads same dir
    mm2 = ModeManager(state_dir=str(tmp_path), initial=Mode.LIVE)
    assert mm2.current().mode == Mode.PAPER


def test_history_records_transitions(tmp_path):
    mm = ModeManager(state_dir=str(tmp_path), initial=Mode.LIVE)
    mm.manual_paper("via_test")
    hist = mm.current().history
    assert any(h.get("to") == Mode.PAPER.value for h in hist)


def test_clear_halt_returns_to_live(tmp_path):
    mm = ModeManager(state_dir=str(tmp_path), initial=Mode.LIVE)
    halt = tmp_path / "HALT"
    halt.write_text("x")
    mm.evaluate()
    assert mm.current().mode == Mode.HALT
    mm.clear_halt()
    assert mm.current().mode == Mode.LIVE
    assert not (tmp_path / "HALT").exists()
