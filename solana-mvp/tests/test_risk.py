"""RiskExec hard-gate tests. Pure logic, tmp_path-isolated state."""
from __future__ import annotations

import time

from core.risk_exec import RiskCtx, RiskExec
from core.types import Candidate, Mode, Side


def _candidate(mint: str = "M" * 44, side: Side = Side.BUY) -> Candidate:
    return Candidate(
        mint=mint, confidence=0.7, pattern_id=None, fingerprint="00000000",
        source_kinds=["test"], wallets=["W" * 44], cluster_ids=[], side=side,
    )


def test_balance_low_blocks_buy(tmp_path):
    risk = RiskExec(state_dir=str(tmp_path))
    cand = _candidate()
    ctx = RiskCtx(
        sol_balance_lamports=10_000_000,  # 0.01 SOL
        expected_size_lamports=50_000_000,  # 0.05 SOL
        sol_reserve_lamports=50_000_000,    # 0.05 SOL reserve
    )
    decision = risk.pre_trade_gates(cand, ctx)
    assert not decision.ok and decision.reason == "balance_low"


def test_token_balance_zero_blocks_sell(tmp_path):
    risk = RiskExec(state_dir=str(tmp_path))
    cand = _candidate(side=Side.SELL)
    ctx = RiskCtx(
        sol_balance_lamports=1_000_000_000,
        token_balance_raw=0,
        expected_size_lamports=50_000_000,
    )
    decision = risk.pre_trade_gates(cand, ctx)
    assert not decision.ok and "token_balance" in decision.reason


def test_quarantine_blocks(tmp_path):
    risk = RiskExec(state_dir=str(tmp_path))
    cand = _candidate(mint="QUAR" + "A" * 40)
    risk.quarantine(cand.mint, time.time() + 600)
    ctx = RiskCtx(
        sol_balance_lamports=1_000_000_000, expected_size_lamports=50_000_000,
        sol_reserve_lamports=10_000_000,
    )
    assert risk.pre_trade_gates(cand, ctx).reason == "quarantined"


def test_fee_threshold_blocks_when_friction_too_high(tmp_path):
    risk = RiskExec(state_dir=str(tmp_path))
    cand = _candidate()
    ctx = RiskCtx(
        sol_balance_lamports=1_000_000_000,
        expected_size_lamports=10_000_000,        # 0.01 SOL
        expected_fee_lamports=5_000_000,           # 0.005 SOL
        expected_slip_bps=5_000,                   # 50% — huge
        fee_threshold_pct=0.015,
        sol_reserve_lamports=10_000_000,
    )
    d = risk.pre_trade_gates(cand, ctx)
    assert not d.ok and d.reason.startswith("fee_threshold")


def test_latency_gate_blocks(tmp_path):
    risk = RiskExec(state_dir=str(tmp_path))
    cand = _candidate()
    ctx = RiskCtx(
        sol_balance_lamports=1_000_000_000, expected_size_lamports=50_000_000,
        sol_reserve_lamports=10_000_000,
        last_latencies_ms=(2500.0, 2600.0, 2400.0, 2700.0, 2800.0),
        latency_p50_max_s=1.5,
    )
    d = risk.pre_trade_gates(cand, ctx)
    assert not d.ok and d.reason.startswith("latency_p50")


def test_three_critical_errors_writes_halt(tmp_path):
    risk = RiskExec(state_dir=str(tmp_path))
    now = time.time()
    for _ in range(3):
        risk.record_submit_outcome(False, error="balance_low", now=now)
    assert risk.is_halted()


def test_size_dampener_engages_after_high_slip(tmp_path):
    risk = RiskExec(state_dir=str(tmp_path))
    # 10 closes with avg slip ~ 0.10 (above 0.08 threshold)
    for _ in range(10):
        risk.record_close(roi=0.0, realized_slip=0.10, expected_slip=0.05)
    assert risk.size_dampener_factor() == 0.5


def test_size_dampener_resets_after_n_trades(tmp_path):
    risk = RiskExec(state_dir=str(tmp_path))
    for _ in range(10):
        risk.record_close(roi=0.0, realized_slip=0.10, expected_slip=0.05)
    assert risk.size_dampener_factor() == 0.5
    # Run a bunch of clean closes to advance trade_n past until_trade_n
    for _ in range(25):
        risk.record_close(roi=0.05, realized_slip=0.01, expected_slip=0.01)
    # The new clean closes have low slip, so factor stays 1.0
    assert risk.size_dampener_factor() == 1.0


def test_pending_watchdog_marks_stuck(tmp_path):
    risk = RiskExec(state_dir=str(tmp_path))
    # Inject an old pending submission directly
    pending = risk.pending_store.load() or {}
    pending["sig_old"] = {"submit_ts": time.time() - 90, "fee": 0, "mint": "X", "mode": "live"}
    pending["sig_too_old"] = {"submit_ts": time.time() - 200, "fee": 0, "mint": "Y", "mode": "live"}
    risk.pending_store.save(pending)
    actions = risk.watchdog_tick()
    assert actions.get("sig_old") == "ghost_cancel"
    assert actions.get("sig_too_old") == "abandon"


def test_consec_not_confirmed_increments(tmp_path):
    risk = RiskExec(state_dir=str(tmp_path))
    for _ in range(3):
        risk.record_submit_outcome(False, error="not_confirmed")
    assert risk.consec_not_confirmed() == 3
    risk.record_submit_outcome(True)
    assert risk.consec_not_confirmed() == 0


def test_jito_only_mode_after_three_sandwich_flags(tmp_path):
    risk = RiskExec(state_dir=str(tmp_path))
    # 3 trades each with realized > 3*expected
    for _ in range(3):
        risk.record_close(roi=0.0, realized_slip=0.30, expected_slip=0.05)
    assert risk.jito_only_mode() is True
