"""Pattern mining: wallet predictive hits, pre-pump precursors, fingerprint clustering."""
from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from . import storage


@dataclass
class WalletCandidate:
    pubkey: str
    hits: int
    total_buys: int
    hit_rate: float


def mine_wallet_candidates(run_db_path: str,
                           start_ts: float, end_ts: float,
                           hit_window_pre_s: tuple[int, int] = (300, 30),
                           hold_min_s: int = 60,
                           min_hits: int = 3, min_hit_rate: float = 0.30) -> list[WalletCandidate]:
    """For each winning trade in run.db, find wallets that bought the same mint within
    [t_open-300s, t_open-30s] and were still holding at t_open+60s. Score them globally.
    """
    db = sqlite3.connect(run_db_path)
    try:
        winners = db.execute(
            "SELECT mint, ts_open, sol_in, ts_close FROM trades "
            "WHERE roi >= 0.10 AND ts_close IS NOT NULL"
        ).fetchall()
    finally:
        db.close()
    if not winners:
        return []

    swaps = list(storage.read_partition_iter("swaps", start_ts, end_ts))
    by_mint: dict[str, list[dict]] = defaultdict(list)
    for s in swaps:
        by_mint[s["mint"]].append(s)
    for k in by_mint:
        by_mint[k].sort(key=lambda r: r["ts"])

    hits: dict[str, int] = defaultdict(int)
    totals: dict[str, int] = defaultdict(int)

    for mint, ts_open, _sol, ts_close in winners:
        rows = by_mint.get(mint, [])
        if not rows:
            continue
        pre_lo = ts_open - hit_window_pre_s[0]
        pre_hi = ts_open - hit_window_pre_s[1]
        hold_t = ts_open + hold_min_s
        # Wallets that bought in pre-window
        pre_buyers: dict[str, float] = {}
        for r in rows:
            if r["ts"] > pre_hi:
                break
            if r["ts"] >= pre_lo and int(r.get("side", 0)) == 0:
                pre_buyers[r["wallet"]] = r["ts"]
        # Among them, who was still holding (no sell tx between buy and hold_t)?
        for w, t_buy in pre_buyers.items():
            sold_before_hold = any(
                r["wallet"] == w and int(r.get("side", 0)) == 1 and t_buy <= r["ts"] <= hold_t
                for r in rows
            )
            totals[w] += 1
            if not sold_before_hold:
                hits[w] += 1

    out: list[WalletCandidate] = []
    for w, h in hits.items():
        t = max(totals[w], 1)
        rate = h / t
        if h >= min_hits and rate >= min_hit_rate:
            out.append(WalletCandidate(pubkey=w, hits=h, total_buys=t, hit_rate=rate))
    out.sort(key=lambda c: (-c.hits, -c.hit_rate))
    return out


@dataclass
class PrecursorFeature:
    name: str
    z_score: float
    mu_pump: float
    mu_baseline: float


def mine_precursors(start_ts: float, end_ts: float,
                    pump_ratio_min: float = 2.0,
                    pump_window_s: int = 3600,
                    baseline_samples: int = 200,
                    seed: int = 42) -> list[PrecursorFeature]:
    """Find features that are systematically elevated before pumps vs random baselines."""
    import random as _r
    rng = _r.Random(seed)

    swaps = list(storage.read_partition_iter("swaps", start_ts, end_ts))
    if not swaps:
        return []
    by_mint: dict[str, list[dict]] = defaultdict(list)
    for s in swaps:
        by_mint[s["mint"]].append(s)
    for k in by_mint:
        by_mint[k].sort(key=lambda r: r["ts"])

    pump_starts: list[tuple[str, float]] = []
    for mint, rows in by_mint.items():
        if len(rows) < 5:
            continue
        # Detect pump: max(price)/first(price) over any 60min sliding window ≥ ratio_min
        for i, r in enumerate(rows):
            j = i
            base_price = r["price_sol"] if r["price_sol"] > 0 else None
            if base_price is None:
                continue
            best = base_price
            while j < len(rows) and rows[j]["ts"] - r["ts"] <= pump_window_s:
                p = rows[j]["price_sol"]
                if p > best:
                    best = p
                j += 1
            if best / max(base_price, 1e-12) >= pump_ratio_min:
                pump_starts.append((mint, r["ts"]))
                break

    if not pump_starts:
        return []

    def features_at(mint: str, t: float) -> dict[str, float]:
        rows = by_mint.get(mint, [])
        lo, hi = t - 60, t
        in_window = [r for r in rows if lo <= r["ts"] <= hi]
        buys = [r for r in in_window if int(r.get("side", 0)) == 0]
        n_buy = len(buys)
        inflow = sum(r["sol_amount"] for r in buys)
        unique = len({r["wallet"] for r in buys})
        return {
            "buy_count_60s": n_buy,
            "inflow_sol_60s": inflow,
            "unique_buyers_60s": unique,
        }

    pump_feats: list[dict[str, float]] = []
    for mint, ts in pump_starts:
        pump_feats.append(features_at(mint, ts - 90))   # 90s before pump

    # Baseline: random (mint, ts) pairs not in any pump window
    pump_set = {(m, math.floor(t / 60.0)) for m, t in pump_starts}
    base_feats: list[dict[str, float]] = []
    keys = list(by_mint.keys())
    if not keys:
        return []
    while len(base_feats) < baseline_samples:
        m = rng.choice(keys)
        rows = by_mint[m]
        if len(rows) < 2: continue
        ts = rng.uniform(rows[0]["ts"], rows[-1]["ts"])
        if (m, math.floor(ts / 60.0)) in pump_set:
            continue
        base_feats.append(features_at(m, ts))

    feature_names = list(pump_feats[0].keys()) if pump_feats else []
    out: list[PrecursorFeature] = []
    for fn in feature_names:
        pv = [f[fn] for f in pump_feats]
        bv = [f[fn] for f in base_feats]
        mu_p = sum(pv) / max(len(pv), 1)
        mu_b = sum(bv) / max(len(bv), 1)
        if len(bv) > 1:
            mb = mu_b
            sigma_b = math.sqrt(sum((x - mb) ** 2 for x in bv) / (len(bv) - 1))
        else:
            sigma_b = 1.0
        z = (mu_p - mu_b) / max(sigma_b, 1e-6)
        out.append(PrecursorFeature(name=fn, z_score=z, mu_pump=mu_p, mu_baseline=mu_b))
    out.sort(key=lambda x: -x.z_score)
    return [p for p in out if p.z_score >= 1.0]


@dataclass
class FingerprintBucket:
    fingerprint: str
    n_trades: int
    median_roi: float
    win_rate: float
    kind: str   # "profitable" | "losing"


def _fingerprint(payload: dict, regime: str = "NORMAL") -> str:
    fl = []
    fl.append("1" if payload.get("has_cluster") else "0")
    fl.append(str(payload.get("prepump_kind", 0)))
    fl.append("1" if payload.get("wallet_score_max", 0) > 0.7 else "0")
    fl.append({"NORMAL": "N", "MANIA": "M", "DEAD": "D", "VOLATILE": "V"}.get(regime, "N"))
    fl.append("1" if payload.get("entropy", 0) > 3.5 else "0")
    fl.append("1" if payload.get("route_compression") else "0")
    fl.append("1" if payload.get("lp_injection") else "0")
    fl.append("1" if payload.get("pumpfun_grad") else "0")
    return "".join(fl)


def cluster_fingerprints(run_db_path: str,
                         min_n: int = 10,
                         profitable_med_roi: float = 0.30,
                         losing_med_roi: float = -0.30,
                         min_wr_profitable: float = 0.55) -> list[FingerprintBucket]:
    """Group closed trades by 8-bit fingerprint (from trades.payload)."""
    db = sqlite3.connect(run_db_path)
    try:
        rows = db.execute(
            "SELECT roi, payload FROM trades WHERE ts_close IS NOT NULL"
        ).fetchall()
    finally:
        db.close()
    by_fp: dict[str, list[float]] = defaultdict(list)
    for roi, payload in rows:
        try:
            p = json.loads(payload) if payload else {}
        except Exception:
            p = {}
        fp = _fingerprint(p, regime=p.get("regime", "NORMAL"))
        by_fp[fp].append(float(roi if roi is not None else 0.0))
    out: list[FingerprintBucket] = []
    for fp, rs in by_fp.items():
        if len(rs) < min_n:
            continue
        rs_sorted = sorted(rs)
        median = rs_sorted[len(rs_sorted) // 2]
        wr = sum(1 for r in rs if r > 0) / len(rs)
        if median >= profitable_med_roi and wr >= min_wr_profitable:
            out.append(FingerprintBucket(fp, len(rs), median, wr, "profitable"))
        elif median <= losing_med_roi:
            out.append(FingerprintBucket(fp, len(rs), median, wr, "losing"))
    return out


def write_outputs(out_dir: Path,
                  wallet_candidates: list[WalletCandidate],
                  precursors: list[PrecursorFeature],
                  buckets: list[FingerprintBucket]) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    p1 = out_dir / "wallet_candidates.json"
    p1.write_text(json.dumps([asdict(c) for c in wallet_candidates], indent=2))
    p2 = out_dir / "precursors.json"
    p2.write_text(json.dumps([asdict(c) for c in precursors], indent=2))
    p3 = out_dir / "discovered_patterns.json"
    p3.write_text(json.dumps([asdict(b) for b in buckets if b.kind == "profitable"], indent=2))
    p4 = out_dir / "anti_patterns.json"
    p4.write_text(json.dumps([asdict(b) for b in buckets if b.kind == "losing"], indent=2))
    paths["wallet_candidates"] = str(p1)
    paths["precursors"] = str(p2)
    paths["discovered_patterns"] = str(p3)
    paths["anti_patterns"] = str(p4)
    return paths
