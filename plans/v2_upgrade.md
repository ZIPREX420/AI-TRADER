# V2 Upgrade Spec — directly executable

## 1. WEAKNESSES
- Smart wallets are curator-trusted; no on-chain re-validation → silent decay.
- Single-wallet copy only; misses coordinated cluster entries (highest-alpha pre-pump signal).
- `getTransaction` JSON-parse on every event = 200–500ms latency penalty; outpaced by snipers.
- Filter is one-shot at signal time; no holder-growth, dev-wallet, or wash-graph check; 60s cache absent.
- Risk is per-trade; no portfolio cap, no correlation cap, no fee-aware threshold.

## 2. WALLET_INTELLIGENCE

**Per-wallet metrics (rolling 30d, derived nightly from on-chain history):**
- `WR` = wins / trades
- `ROI` = Σrealized_sol / Σsol_invested
- `HOLD_MED` = median hold duration (s)
- `ENTRY_PCTL` = mean percentile of buy timestamp within token's first-1h volume CDF (lower = earlier)
- `COORD` = max Jaccard(traded_mints) vs other tracked wallets
- `LAST_TRADE_AGE` (s)

**Score formula (clamped to [0,1]):**
```
S = 0.35*WR + 0.25*tanh(ROI/2) + 0.15*(1 - min(HOLD_MED/3600, 1))
  + 0.15*(1 - ENTRY_PCTL) + 0.10*COORD
```

**Filter pipeline (AND):**
- WR ≥ 0.55, ROI ≥ 0.40, trades ≥ 30
- 0.5 SOL ≤ mean_pos_size ≤ 50 SOL (kill whales+dust)
- LAST_TRADE_AGE ≤ 7d
- max_single_trade_pnl / total_pnl ≤ 0.60 (no outlier dependency)
- 24h_drawdown ≥ −0.20

**Clustering:** wallets with shared-mint Jaccard ≥ 0.40 → `cluster_id`. Tracked across all signals.

**Output:** `data/smart_wallets_scored.json`: `[{pubkey, score, cluster_id, metrics{...}}]`. Refresh every 24h.

## 3. TOKEN_FILTER

Strict gate, fail fast, cache result 60s per mint:

1. mint_authority == None ∧ freeze_authority == None
2. LP token: ≥90% to burn-addr OR known locker (Streamflow `strmRq…`, Bonkfun lock vault)
3. Holder entropy `H = -Σ p_i log p_i` over top-50 ≥ 3.0
4. Top-10 holders (excluding LP+burn) ≤ 25% supply
5. Dev wallet (mint authority OR pre-pool funder) holds ≤ 15% AND no out-transfers in last 5 min
6. Jupiter sell quote exists for 0.1 SOL probe
7. Jupiter buy quote price impact ≤ 25% on 0.3 SOL
8. Wash-graph: max wallet-pair bidirectional volume ratio ≤ 0.6 over last 100 swaps
9. Holder count ≥ 50 AND grew by ≥ 15 in last 5 min
10. No mint instructions executed in last 60s

ALL must pass. Single fail → reject + cache reason.

## 4. PRE_PUMP_SIGNALS

Maintain rolling 90s buy-event window per mint. Trigger any:

- **A. CLUSTER_HIT**: ≥3 distinct smart-wallet `cluster_id`s buy within 90s, total ≥ 5 SOL
- **B. EARLY_FLOCK**: ≥5 wallets with `first_tx_age ≤ 24h` buy within 60s, each 0.05–1 SOL
- **C. STAIR**: ≥4 buys, monotonically increasing size, span ≤120s, total ≥2 SOL, size-stride σ > 0.1 (anti-bot)
- **D. PRE_INFLOW**: ≥3 buys of 0.1–0.5 SOL within 30s, then a single buy ≥5 SOL within next 60s → enter on the small-buy cluster (front-run the front-runners)

**Anti-bot guards (reject signal):**
- All buyer txs share same instruction-byte hash → deterministic bundle bot
- All txs same fee_payer → one entity faking diversity

## 5. ENTRY_RULES

Enter iff: `TOKEN_FILTER == pass` AND (`copy_score ≥ 0.6` OR `CLUSTER_HIT` OR `PRE_INFLOW`).

- **Anti-FOMO**: if mint price ↑ ≥ 50% in last 60s, wait until next 1m candle pulls back ≤ 20% from local high OR another smart wallet enters; abort after 90s.
- **Liquidity floor**: pool TVL ≥ $25k (SOL-denominated).
- **Slippage budget**: abort if Jupiter quote priceImpact > 4%.
- **Latency gate**: `event_received → submit` p50 ≤ 1.5s; if exceeded, mark `STALE`, skip.
- **Confirmation**: at least 1 of triggering smart wallets still holding (no sells from them in last 30s).

## 6. EXIT_RULES

Tick every **3s** per open position:

- **HARD SL**: ratio ≤ 0.65 (−35%) → exit 100%
- **SOFT SL**: ratio ∈ [0.90, 1.00] AND no new on-chain buys for 5 min → exit 100%
- **TP1**: ratio ≥ 1.80 → sell 35%
- **TP2**: ratio ≥ 3.00 → sell 35% more
- **TP3**: ratio ≥ 6.00 → sell 20% more (10% moonbag)
- **Trailing on moonbag**: ATR-14m × 1.5 below high-water, OR 25% drawdown from HW, whichever tighter
- **Mirror exit**: ≥2 origin smart wallets sell ≥50% of their bag → exit 100%
- **Time stop**: t > 30 min AND ratio < 1.30 → exit
- **Fee-aware trim guard**: skip partial sell if (sol_out − fees) < cost_basis × 0.05 for that slice (no churning)

## 7. POSITION_RULES

```
size_sol = base × score_mult × cluster_mult × liquidity_mult
base           = capital_sol * 0.05
score_mult     = clamp(1 + 1.5*(score - 0.5), 0.5, 2.0)
cluster_mult   = 1.0 / 1.4 / 1.7   for clusters 1 / ≥3 / ≥5
liquidity_mult = min(1.0, pool_tvl_usd / 80_000)
size_sol       = clamp(size_sol, 0, capital_sol * 0.12)
```

Constraints:
- `Σ open_position_sol ≤ capital_sol × 0.30` (portfolio cap)
- `max_open = 4`
- `≤ 2 positions per source cluster_id` (correlation cap)
- Reject trade if `(priority_fee + jito_tip + slippage_loss_est) > size_sol × 0.015`

## 8. EXECUTION_RULES

- **Dual-RPC race**: build once, sign once, submit to Helius primary AND QuickNode/Triton secondary; first confirmation wins; cancel/ignore the other.
- **Priority fee** μLamports = clamp(p75 of `getRecentPrioritizationFees` last 150 slots, 100_000, 5_000_000).
- **Jito tip** lamports = max(p50 of last 100 landed-bundle tips via Jito `tip-floor` API, 10_000).
- **Buys**: always Jito bundle (private mempool, anti-sandwich).
- **Sells**: RPC direct (need confirmation, not bundling); priority fee +25%.
- **Retry**: if unconfirmed at 4s, re-quote (price moved), bump priority fee +50%, resubmit once. Abort after 2 attempts → log and skip.
- **Preflight**: skip on buys (saves ~300ms), enable on sells.
- **CU limit**: dynamic from Jupiter, cap 600_000.
- **Route warming**: background task quotes watched/held mints every 30s — warms Jupiter route cache.

## 9. SYSTEM_UPGRADE

**New modules:**
- `src/wallet_scorer.py` — nightly: walks `getSignaturesForAddress` per wallet, parses swaps, computes metrics + `S`, writes `data/smart_wallets_scored.json`. Standalone CLI: `python -m src.wallet_scorer`.
- `src/cluster_detector.py` — rolling 90s window per mint; consumes CopySignals + small new-token buys; emits `ClusterHit(mint, cluster_ids, total_sol, kind∈{A,B,C,D})`.
- `src/exec_race.py` — `submit_race(signed_tx, [endpoints]) → (sig, winner)`; uses `asyncio.wait(FIRST_COMPLETED)`.
- `src/fee_oracle.py` — caches `priority_fee_p75` + `jito_tip_floor`; refresh every 10s; in-process singleton.

**Modified:**
- `constants.py` — add `SOL_INCINERATOR`, `STREAMFLOW_LOCK_PROGRAM`, `BONKFUN_LOCK`, `JITO_TIP_FLOOR_URL = "https://bundles.jito.wtf/api/v1/bundles/tip_floor"`.
- `config.py` — load `smart_wallets_scored.json` if present, else fallback to `smart_wallets.json` with default score 0.6.
- `rug_filter.py` — add `_check_lp_lock`, `_check_holder_entropy`, `_check_holder_growth`, `_check_wash_graph`, `_check_dev_wallet`, `_check_recent_mint_ix`; 60s LRU cache keyed on mint.
- `signal_engine.py` — consume `ClusterHit`; score = max(copy_score × wallet_S, cluster_score, prepump_score); emit `TradeOrder` with `score`, `cluster_id_set`.
- `executor.py` — split `submit_buy()` (Jito only) vs `submit_sell()` (RPC race); both use `fee_oracle`; retry-with-bump.
- `risk_manager.py` — add `portfolio_cap_sol`, `correlation_cap`, `fee_threshold_check(order, fee_oracle)`.
- `position_manager.py` — 3s tick; new TP/SL constants; ATR-14m trailing (compute from 5s price probes); cache mint decimals.
- `bot.py` — wire `cluster_detector`, `fee_oracle`, `exec_race`; route `CopySignal` through cluster detector before signal_engine.

**Updated pipeline:**
```
WS → ingest_q
       ├→ wallet_tracker → CopySignal ──► cluster_detector ──► ClusterHit ─┐
       └→ token_scanner  → NewSignal ───────────────────────────────────────┤
                                                                            ▼
                                                                 signal_engine (uses wallet_score)
                                                                            ▼
                                                                 risk_manager (incl. portfolio + correlation + fee)
                                                                            ▼
                                                                 rug_filter (60s cache, expanded checks)
                                                                            ▼
                                                                 exec_race (Helius || QuickNode, fee_oracle)
                                                                            ▼
                                                                 position_manager (3s tick, ATR trail, mirror)
```

**Tests to add:**
- `test_wallet_scorer.py` — fixture trade history → expected metrics + score.
- `test_cluster_detector.py` — synthetic event streams hitting A/B/C/D rules.
- `test_token_filter_v2.py` — entropy, growth, wash-graph fixtures.
- `test_position_v2.py` — new TP ladder, SOFT SL, ATR trail.

**Config additions (.env):**
```
QUICKNODE_RPC_URL=
TRITON_RPC_URL=
WALLET_SCORE_MIN=0.55
PORTFOLIO_CAP_PCT=0.30
CORRELATION_CAP=2
EXIT_TICK_S=3
```
