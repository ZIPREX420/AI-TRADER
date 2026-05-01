"""Cluster detector + tx_decoder tests. Pure logic."""
from __future__ import annotations

import time

from core.types import Side, WalletEvent
from signal.cluster_detector import ClusterDetector
from signal.tx_decoder import cache_clear, cache_size, decode_swap


def _ev(ts: float, wallet: str, mint: str = "MINT" + "A" * 40,
        sol: float = 0.5, side: Side = Side.BUY) -> WalletEvent:
    return WalletEvent(
        ts=ts, slot=int(ts * 1000), signature=f"sig_{wallet}_{ts}",
        wallet=wallet, mint=mint, side=side,
        sol_amount=sol, token_amount=sol * 1_000_000, price_sol=1e-6,
    )


def test_cluster_hit_fires_on_three_distinct_clusters():
    cd = ClusterDetector(cluster_of=lambda w: w[:1])  # group by first char
    t0 = time.time()
    sig = None
    sig = cd.update(_ev(t0 + 0, "A_alice", sol=2.0)) or sig
    sig = cd.update(_ev(t0 + 5, "B_bob", sol=2.0)) or sig
    sig = cd.update(_ev(t0 + 10, "C_carol", sol=2.0)) or sig
    assert sig is not None
    assert sig.kind == "CLUSTER_HIT"


def test_cluster_hit_does_not_fire_when_total_too_low():
    cd = ClusterDetector(cluster_of=lambda w: w[:1])
    t0 = time.time()
    sigs = []
    for letter, sol in (("A", 0.5), ("B", 0.5), ("C", 0.5)):
        s = cd.update(_ev(t0, f"{letter}_x", sol=sol))
        if s:
            sigs.append(s)
    # Σ sol = 1.5 < cluster_min_sol(5)
    assert not any(s.kind == "CLUSTER_HIT" for s in sigs)


def test_early_flock_requires_young_wallets():
    young = lambda w: 100.0 if w.startswith("Y") else 1e9
    cd = ClusterDetector(wallet_age=young, cluster_of=lambda w: w)
    t0 = time.time()
    sig = None
    for i in range(5):
        s = cd.update(_ev(t0 + i, f"Y_w{i}", sol=0.2))
        if s and s.kind == "EARLY_FLOCK":
            sig = s
    assert sig is not None and sig.kind == "EARLY_FLOCK"


def test_stair_pattern_with_monotonic_increases():
    cd = ClusterDetector(cluster_of=lambda w: w)
    t0 = time.time()
    sizes = [0.1, 0.3, 0.6, 1.0]  # strides: 0.2, 0.3, 0.4 → σ ~ 0.1+
    sig = None
    for i, sz in enumerate(sizes):
        s = cd.update(_ev(t0 + i * 5, f"w_{i}", sol=sz))
        if s and s.kind == "STAIR":
            sig = s
    assert sig is not None and sig.kind == "STAIR"


def test_pre_inflow_three_small_buys():
    cd = ClusterDetector(cluster_of=lambda w: w)
    t0 = time.time()
    sig = None
    for i in range(3):
        s = cd.update(_ev(t0 + i, f"W_{i}", sol=0.2))
        if s and s.kind == "PRE_INFLOW":
            sig = s
    assert sig is not None


def test_anti_bot_uniform_byte_hash_drops():
    cd = ClusterDetector(cluster_of=lambda w: w[:1])
    t0 = time.time()
    sigs = []
    for letter in ("A", "B", "C", "D"):
        s = cd.update(_ev(t0, f"{letter}_x", sol=2.0),
                      instruction_byte_hash="SAMEHASH")
        if s:
            sigs.append(s)
    assert sigs == []  # all uniform → suppressed


def test_cluster_does_not_double_fire_for_same_kind():
    cd = ClusterDetector(cluster_of=lambda w: w[:1])
    t0 = time.time()
    fired = 0
    for letter in ("A", "B", "C"):
        s = cd.update(_ev(t0 + 0, f"{letter}_x", sol=2.0))
        if s and s.kind == "CLUSTER_HIT":
            fired += 1
    # Add a 4th distinct cluster — should not re-fire CLUSTER_HIT
    s = cd.update(_ev(t0 + 1, "D_x", sol=2.0))
    assert fired == 1
    assert s is None or s.kind != "CLUSTER_HIT"


def test_decode_swap_returns_buy():
    cache_clear()
    wallet = "W" * 44
    mint = "M" * 44
    tx = {
        "signature": "sig_decode",
        "timestamp": 1700000000,
        "slot": 1234567,
        "events": {
            "swap": {
                "nativeInput": {"account": wallet, "amount": "100000000"},
                "tokenOutputs": [
                    {"userAccount": wallet, "mint": mint,
                     "rawTokenAmount": {"tokenAmount": "1000000000", "decimals": 9}}
                ],
            }
        },
    }
    ev = decode_swap(tx, wallet)
    assert ev is not None
    assert ev.side == Side.BUY
    assert ev.mint == mint
    assert abs(ev.sol_amount - 0.1) < 1e-9


def test_decode_swap_lru_cache_hit():
    cache_clear()
    wallet = "W" * 44
    mint = "M" * 44
    tx = {
        "signature": "sig_cache_test",
        "timestamp": 1700000000,
        "slot": 1234567,
        "events": {
            "swap": {
                "nativeInput": {"account": wallet, "amount": "100000000"},
                "tokenOutputs": [
                    {"userAccount": wallet, "mint": mint,
                     "rawTokenAmount": {"tokenAmount": "1000000000", "decimals": 9}}
                ],
            }
        },
    }
    n0 = cache_size()
    a = decode_swap(tx, wallet)
    b = decode_swap(tx, wallet)
    assert a is not None and b is not None
    assert cache_size() >= n0 + 1


def test_decode_swap_returns_none_for_non_swap():
    cache_clear()
    tx = {"signature": "x", "timestamp": 0, "slot": 0, "events": {}}
    assert decode_swap(tx, "W" * 44) is None


def test_decode_swap_returns_sell():
    cache_clear()
    wallet = "W" * 44
    mint = "M" * 44
    tx = {
        "signature": "sig_sell",
        "timestamp": 1700000000,
        "slot": 1,
        "events": {
            "swap": {
                "nativeOutput": {"account": wallet, "amount": "200000000"},
                "tokenInputs": [
                    {"userAccount": wallet, "mint": mint,
                     "rawTokenAmount": {"tokenAmount": "2000000000", "decimals": 9}}
                ],
            }
        },
    }
    ev = decode_swap(tx, wallet)
    assert ev is not None
    assert ev.side == Side.SELL
