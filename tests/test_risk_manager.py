"""Pure-logic tests for RiskManager."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from src.risk_manager import RiskManager, Position


def make_rm():
    rm = RiskManager(max_position_pct=0.07, max_open=3, daily_loss_halt_pct=-0.30, sol_reserve=0.05)
    rm.init_state(capital_sol=0.667, capital_usd=100, sol_price_usd=150)
    return rm


def test_allow_basic():
    rm = make_rm()
    ok, why = rm.allow("MINT1", sol_size=0.04, current_balance_sol=0.7)
    assert ok, why


def test_reject_below_reserve():
    rm = make_rm()
    ok, why = rm.allow("MINT1", sol_size=0.04, current_balance_sol=0.06)
    assert not ok and "reserve" in why


def test_reject_size_over_cap():
    rm = make_rm()
    ok, why = rm.allow("MINT1", sol_size=0.50, current_balance_sol=1.0)
    assert not ok and "size_over_cap" in why


def test_reject_max_open():
    rm = make_rm()
    for i in range(3):
        rm.open(Position(mint=f"MINT{i}", sol_in=0.04, tokens=1000, entry_price_sol=0.0001,
                          opened_at=time.time(), source="test"))
    ok, why = rm.allow("MINT9", sol_size=0.04, current_balance_sol=1.0)
    assert not ok and why == "max_open"


def test_close_records_pnl_and_removes():
    rm = make_rm()
    rm.open(Position(mint="M", sol_in=0.05, tokens=1_000_000, entry_price_sol=5e-8,
                     opened_at=time.time(), source="t"))
    pnl = rm.close("M", sol_out=0.10, partial_pct=1.0)
    assert pnl is not None and abs(pnl - 0.05) < 1e-9
    assert "M" not in rm.state.open_positions
    assert abs(rm.state.realized_pnl_sol_today - 0.05) < 1e-9


def test_partial_close():
    rm = make_rm()
    rm.open(Position(mint="M", sol_in=0.10, tokens=1_000_000, entry_price_sol=1e-7,
                     opened_at=time.time(), source="t"))
    pnl1 = rm.close("M", sol_out=0.10, partial_pct=0.5)
    assert abs(pnl1 - 0.05) < 1e-9
    assert "M" in rm.state.open_positions
    pnl2 = rm.close("M", sol_out=0.30, partial_pct=0.5)
    assert "M" not in rm.state.open_positions
    assert abs(rm.state.realized_pnl_sol_today - 0.30) < 1e-9


def test_daily_loss_halts():
    rm = make_rm()
    rm.open(Position(mint="M", sol_in=0.50, tokens=1, entry_price_sol=0.5,
                     opened_at=time.time(), source="t"))
    rm.close("M", sol_out=0.20, partial_pct=1.0)  # lose 0.30 SOL on 0.667 cap = -45%
    ok, why = rm.allow("M2", sol_size=0.04, current_balance_sol=1.0)
    assert not ok and "halt" in why
    assert rm.is_halted


if __name__ == "__main__":
    test_allow_basic()
    test_reject_below_reserve()
    test_reject_size_over_cap()
    test_reject_max_open()
    test_close_records_pnl_and_removes()
    test_partial_close()
    test_daily_loss_halts()
    print("ALL RISK MANAGER TESTS PASSED")
