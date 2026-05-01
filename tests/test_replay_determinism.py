"""Same seed + same inputs → identical event order + identical executor outputs."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
from src.research.clock import ReplayClock
from src.research.replay import Replay, ReplayEvent
from src.research.simulated_executor import FrictionModel, SimulatedExecutor


def test_event_sort_is_stable():
    a = ReplayEvent(ts=10.0, slot=1, sig="x", kind="swap", payload={})
    b = ReplayEvent(ts=10.0, slot=1, sig="x", kind="swap", payload={})
    c = ReplayEvent(ts=11.0, slot=1, sig="x", kind="swap", payload={})
    s = sorted([c, b, a], key=lambda e: e.sort_key())
    assert s[0].ts <= s[1].ts <= s[2].ts


def test_seeded_executor_is_deterministic():
    fm = FrictionModel(latency_ms_p50=400, latency_ms_p95=800, drop_rate=0.10)
    def make():
        clock = ReplayClock(_now=0.0)
        return SimulatedExecutor(clock=clock, price_at=lambda t, m: 0.001,
                                 friction=fm, rng=random.Random(123))
    e1 = make(); e2 = make()
    seq1 = [e1.buy("M", int(0.05*1e9), 500) for _ in range(20)]
    seq2 = [e2.buy("M", int(0.05*1e9), 500) for _ in range(20)]
    for r1, r2 in zip(seq1, seq2):
        assert r1.ok == r2.ok
        assert r1.out_amount == r2.out_amount
        assert abs(r1.elapsed_ms - r2.elapsed_ms) < 1e-6


if __name__ == "__main__":
    test_event_sort_is_stable()
    test_seeded_executor_is_deterministic()
    print("OK determinism")
