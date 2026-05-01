"""Pure-logic tests for SignalEngine — no network."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time

from src.signal_engine import SignalEngine
from src.wallet_tracker import CopySignal
from src.token_scanner import NewTokenSignal


def test_dedupe_within_window():
    e = SignalEngine(dedupe_window_s=300)
    s1 = CopySignal("copy", "W"*44, "M"*44, "buy", 0.1, 1000.0, "sig1", 1, time.time(), 50.0)
    s2 = CopySignal("copy", "W"*44, "M"*44, "buy", 0.1, 1000.0, "sig2", 2, time.time(), 50.0)
    o1 = e.evaluate(s1, capital_usd=100, sol_price_usd=150)
    o2 = e.evaluate(s2, capital_usd=100, sol_price_usd=150)
    assert o1 is not None
    assert o2 is None  # deduped


def test_sell_does_not_open():
    e = SignalEngine()
    s = CopySignal("copy", "W"*44, "M"*44, "sell", 0.5, 5000.0, "sig", 1, time.time(), 10.0)
    assert e.evaluate(s, 100, 150) is None
    assert e.smart_exit_count("M"*44) == 1


def test_three_smart_exits_blocks_buy():
    e = SignalEngine()
    mint = "M"*44
    for w in ("A"*44, "B"*44, "C"*44):
        e.evaluate(CopySignal("copy", w, mint, "sell", 0.3, 1000.0, f"s{w[0]}", 1, time.time(), 10.0), 100, 150)
    buy = CopySignal("copy", "D"*44, mint, "buy", 0.1, 1000.0, "sb", 1, time.time(), 10.0)
    assert e.evaluate(buy, 100, 150) is None


def test_new_token_creates_order():
    e = SignalEngine()
    s = NewTokenSignal("new", "M"*44, "raydium", "sig", 1, time.time(), 100.0)
    o = e.evaluate(s, capital_usd=100, sol_price_usd=150)
    assert o is not None
    assert o.source.startswith("new:")
    assert 0.0 < o.sol_size < 1.0


def test_copy_size_is_capped():
    e = SignalEngine()
    s = CopySignal("copy", "W"*44, "M"*44, "buy", 10.0, 1000.0, "sig", 1, time.time(), 30.0)
    o = e.evaluate(s, capital_usd=100, sol_price_usd=150)
    expected = (100 * 0.07) / 150
    assert abs(o.sol_size - expected) < 1e-9


if __name__ == "__main__":
    test_dedupe_within_window()
    test_sell_does_not_open()
    test_three_smart_exits_blocks_buy()
    test_new_token_creates_order()
    test_copy_size_is_capped()
    print("ALL SIGNAL ENGINE TESTS PASSED")
