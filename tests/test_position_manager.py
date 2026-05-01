"""Pure-logic tests for exit decision matrix."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from src.risk_manager import Position
from src.position_manager import decide_exit


def pos(entry=1.0, sold_pct=0.0, opened_at=None, hwp=0.0):
    return Position(mint="M", sol_in=1.0, tokens=1.0, entry_price_sol=entry,
                    opened_at=opened_at or time.time(), source="t",
                    high_water_price_sol=hwp, sold_pct=sold_pct)


def test_stop_loss():
    d = decide_exit(pos(entry=1.0), current_price_sol=0.55, smart_exit_count=0, now=time.time())
    assert d.should_exit and "stop_loss" in d.reason and d.fraction == 1.0


def test_tp_2x_takes_50():
    d = decide_exit(pos(entry=1.0), current_price_sol=2.1, smart_exit_count=0, now=time.time())
    assert d.should_exit and d.fraction == 0.5 and "2x" in d.reason


def test_tp_5x_takes_30_more():
    d = decide_exit(pos(entry=1.0, sold_pct=0.5), current_price_sol=5.5, smart_exit_count=0, now=time.time())
    assert d.should_exit and abs(d.fraction - 0.30) < 1e-9 and "5x" in d.reason


def test_mirror_exit():
    d = decide_exit(pos(entry=1.0), current_price_sol=1.5, smart_exit_count=3, now=time.time())
    assert d.should_exit and "mirror" in d.reason and d.fraction == 1.0


def test_time_stop_flat():
    p = pos(entry=1.0, opened_at=time.time() - 4000)
    d = decide_exit(p, current_price_sol=1.05, smart_exit_count=0, now=time.time())
    assert d.should_exit and "time_stop" in d.reason


def test_hold():
    d = decide_exit(pos(entry=1.0), current_price_sol=1.3, smart_exit_count=0, now=time.time())
    assert not d.should_exit


def test_trail_after_partial_then_drawdown():
    p = pos(entry=1.0, sold_pct=0.80, hwp=10.0)
    d = decide_exit(p, current_price_sol=6.0, smart_exit_count=0, now=time.time())
    assert d.should_exit and "trail" in d.reason


if __name__ == "__main__":
    test_stop_loss()
    test_tp_2x_takes_50()
    test_tp_5x_takes_30_more()
    test_mirror_exit()
    test_time_stop_flat()
    test_hold()
    test_trail_after_partial_then_drawdown()
    print("ALL POSITION MANAGER TESTS PASSED")
