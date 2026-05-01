"""SimulatedExecutor: deterministic outputs for fixed seed; latency advances clock."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
from src.research.clock import ReplayClock
from src.research.simulated_executor import FrictionModel, SimulatedExecutor


def make_exec(price=0.0001, drop_rate=0.0, seed=42):
    clock = ReplayClock(_now=1_700_000_000.0)
    fm = FrictionModel(drop_rate=drop_rate, fee_lamports=100_000,
                       latency_ms_p50=400, latency_ms_p95=900)
    rng = random.Random(seed)
    px = lambda t, m: price
    return SimulatedExecutor(clock=clock, price_at=px, friction=fm, rng=rng)


def test_buy_returns_tokens_and_advances_clock():
    e = make_exec()
    t0 = e.clock.now()
    res = e.buy("MINT", sol_lamports=int(0.1 * 1e9), slippage_bps=500)
    assert res.ok
    assert res.out_amount > 0
    assert e.clock.now() > t0
    assert res.elapsed_ms > 0


def test_drop_rate_fails_submission():
    e = make_exec(drop_rate=1.0)
    res = e.buy("MINT", sol_lamports=int(0.1 * 1e9), slippage_bps=500)
    assert not res.ok and res.error == "dropped"


def test_slippage_exceeded_fails():
    e = make_exec()
    e.tvl_at = lambda t, m: 1.0   # ~$1 TVL → impact massive
    res = e.buy("MINT", sol_lamports=int(0.5 * 1e9), slippage_bps=10)
    assert not res.ok and res.error == "slippage_exceeded"


def test_determinism_with_seed():
    a = make_exec(seed=99)
    b = make_exec(seed=99)
    ra = a.buy("M", int(0.05 * 1e9), 500)
    rb = b.buy("M", int(0.05 * 1e9), 500)
    assert ra.out_amount == rb.out_amount
    assert ra.elapsed_ms == rb.elapsed_ms


def test_no_price_fails():
    clock = ReplayClock(_now=0)
    e = SimulatedExecutor(clock=clock, price_at=lambda t, m: None,
                          friction=FrictionModel(drop_rate=0.0),
                          rng=random.Random(1))
    res = e.buy("M", int(0.1 * 1e9), 500)
    assert not res.ok and res.error == "no_price"


if __name__ == "__main__":
    test_buy_returns_tokens_and_advances_clock()
    test_drop_rate_fails_submission()
    test_slippage_exceeded_fails()
    test_determinism_with_seed()
    test_no_price_fails()
    print("OK simulated_executor")
