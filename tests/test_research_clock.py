"""Clock abstraction tests — pure logic."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.research.clock import ReplayClock, WallClock


def test_replay_clock_advances_forward_only():
    c = ReplayClock(_now=100.0)
    assert c.now() == 100.0
    c.advance_to(150.0)
    assert c.now() == 150.0
    c.advance_to(120.0)   # should not go backward
    assert c.now() == 150.0
    c.advance_by(10.0)
    assert c.now() == 160.0
    c.advance_by(-5.0)    # ignored
    assert c.now() == 160.0


def test_wall_clock_returns_unix_now():
    import time
    w = WallClock()
    t = w.now()
    assert abs(t - time.time()) < 5.0


if __name__ == "__main__":
    test_replay_clock_advances_forward_only()
    test_wall_clock_returns_unix_now()
    print("OK clock")
