# V4 Institutional Multi-Strategy Spec — directly implementable

Layered atop v3: each event fans to N strategies, each strategy scored independently, regime gates, allocator caps, shadow tracker mirrors all decisions.

---

## 1. STRATEGY_SET

| ID | Name | Trigger | Hold | TP ladder | SL | Max hold |
|----|------|---------|------|-----------|----|---------:|
| **S1** | COPY | wallet_S ≥ 0.65 buy detected | 15–60 min | 1.80 / 3.00 / 6.00 (35/35/20) | −35% | 60 min |
| **S2** | CLUSTER | CLUSTER_HIT or PRE_INFLOW (v2 §4 A or D) | 5–15 min | 1.50 / 2.50 (50/40) | −25% | 15 min |
| **S3** | NEW_LAUNCH | Raydium init OR Pump.fun graduate, no copy | 3–10 min | 1.40 / 2.00 (50/40) | −20% | 10 min |
| **S4** | DIP_BUY | mint in top-100 30m volume AND ≥35% pullback from 5m local high | 10–30 min | retake_high → 50%, 2.0× → 30%, 3.0× → 20% | −20% | 30 min |
| **S5** | SCALP | price ≥1.30× in 3 min AND smart wallet bought within ±60s | 1–5 min | 1.20 / 1.40 (60/30, 10% runner) | −10% | 5 min |

Each strategy implements:
```python
class Strategy:
    id: str; default_alloc: float; thetas_min: float
    def match(event, ctx) -> StrategySignal | None
    def size(score, ctx) -> float          # SOL
    def decide_exit(pos, market, now) -> ExitDecision
```

---

## 2. REGIME_RULES

Recompute every 5 min, persist `data/regime.json`. Inputs (rolling 60 min):

```
launches_per_hr  = count(NewTokenSignal in last 60min)
median_tvl_new   = median(pool_tvl_usd at detection) over last 60min
own_wr_50        = WR over last 50 closed real trades
sol_sigma_60m    = stdev(1m SOL log-returns last 60min)
sol_sigma_60d    = median 60min stdev over last 60 days
```

Classification (priority top-down):
```
VOLATILE if sol_sigma_60m > 2.0 × sol_sigma_60d
DEAD     if launches_per_hr < 8  OR median_tvl_new < 15_000
MANIA    if launches_per_hr ≥ 30 AND median_tvl_new ≥ 30_000
NORMAL   otherwise
```

Add hysteresis: stay in current regime for ≥ 15 min before switching unless VOLATILE triggers.

---

## 3. STRATEGY_MAPPING

| Regime   | S1 COPY | S2 CLUSTER | S3 NEW | S4 DIP | S5 SCALP |
|----------|---------|------------|--------|--------|----------|
| NORMAL   | 1.0×    | 1.0×       | 0.7×   | 1.0×   | 1.0×     |
| MANIA    | 1.0×    | 1.3×       | 1.0×   | 0.0× (off) | 1.2× |
| DEAD     | 1.0×    | 0.0× (off) | 0.0× (off) | 1.0× | 0.0× (off) |
| VOLATILE | 0.5×    | 0.0× (off) | 0.0× (off) | 0.0× (off) | 0.0× (off) |

`size_after_regime = size_pre_regime × multiplier`. Strategy with multiplier 0 → suppressed (signals still scored + shadowed but not executed).

---

## 4. CAPITAL_ALLOCATION

Per-strategy share `a_s`, sum = 1.0. State: `data/allocations.json`. Default:
```
a = {S1:0.30, S2:0.25, S3:0.15, S4:0.15, S5:0.15}
```

Update hourly (only strategies with ≥10 closed trades in last 7d):
```
sharpe_s  = mean(roi_s) / max(std(roi_s), 0.05)
score_s   = clip(sharpe_s, 0, 3.0)
target_s  = score_s / Σ_k score_k    (k where trades_k ≥ 10)
a_s       = 0.7·a_s_prev + 0.3·target_s          # EMA smoothing
clip a_s ∈ [0.05, 0.50] then renormalize Σa = 1
if sharpe_s < 0 over last 30 trades: a_s = 0.05 (floor)
```

Effective per-trade size: `size = size_v3 × (a_s / a_s_default) × regime_mult_s`.
Per-strategy capital pool: `pool_s = capital_sol × a_s`. Reject entry if `Σ open_s ≥ pool_s`.

---

## 5. SHADOW_SYSTEM

Always-on parallel ledger. For every event passing TOKEN_FILTER:
- For every strategy, even disabled or off-regime: compute `match()` + score.
- If would-have-traded: insert `shadow_trades` row with entry quote price.
- Background `shadow_task` (every 30s) re-quotes price for open shadow rows; applies that strategy's `decide_exit()`; closes shadow row when exit triggered or after `max_hold`.

Schema:
```sql
CREATE TABLE shadow_trades(
  id INTEGER PK, ts_open REAL, ts_close REAL,
  strategy TEXT, mint TEXT, regime TEXT,
  score_pred REAL, executed INT,           -- 1 if real trade also opened
  entry_price_sol REAL, exit_price_sol REAL,
  sim_roi REAL, peak_ratio REAL, exit_reason TEXT
);
```

Promotion rule: if a currently-suppressed strategy accumulates ≥50 shadow trades with `sim_sharpe ≥ 1.0 AND sim_expectancy ≥ 0.10` → enable in NORMAL regime (mapping update).

Drift report (hourly): `Δ = |real_sharpe_s − shadow_sharpe_s|`. If `Δ > 0.5` → log slippage warning, downsize that strategy by 20% until aligned.

---

## 6. SIGNAL_MODEL

Unified score (per candidate signal):
```
final = w1·W_quality                 # wallet
      + w2·T_safety                  # token filter pass-margin (v3 §3)
      + w3·E_timing                  # latency + anti-FOMO + holds_active
      + b ·P_strategy[id]            # learned strategy prior
      + r ·R_regime[id, regime]      # 1.0 if mapping≥1, 0.5 if 0.5–1.0, 0.0 if off
      + h ·H_token[type]             # rolling expectancy by token type
```
Defaults: `w1=0.30, w2=0.25, w3=0.15, b=0.15, r=0.10, h=0.05` (sum=1).
Token types (for `H_token`): `pumpfun_grad`, `raydium_new`, `meme_established` (TVL>$200k), `low_cap_old` (mint>7d, TVL<$50k).

Decision: enter iff
```
final ≥ θ_strategy
  AND pool_s ≥ required_size
  AND regime_mult_s > 0
  AND not in risk_halt
  AND TOKEN_FILTER == pass
```

Per-strategy θ defaults: `S1=0.55, S2=0.60, S3=0.65, S4=0.55, S5=0.65`.
Threshold also adapts via v3 tuner: `θ_s ← clip(θ_s + 0.01·(target_WR_s − WR_s), 0.50, 0.85)`.

---

## 7. RISK_ENGINE

Priority-ordered hard checks (run on every entry attempt, every 30s background sweep):

| # | Trigger | Action |
|---|---------|--------|
| 1 | `data/HALT` file exists | full halt; closes-only |
| 2 | Daily loss > 30% capital | halt 24 h |
| 3 | Cumulative DD > 50% from all-time peak | halt 7 d |
| 4 | 5 consecutive losing real trades | halt 4 h |
| 5 | Last-20 WR < 0.30 | halt 2 h |
| 6 | Per-strategy daily loss > 15% × pool_s | disable strategy 24 h |
| 7 | Avg realized slippage > 8% over last 10 trades | global size × 0.5 for next 20 |
| 8 | Wallet SOL balance < 0.10 absolute | halt entries (sells continue) |
| 9 | SOL price drops > 20% in 60 min | halt 1 h |
|10 | RPC failure rate > 30% in 5 min | switch to backup RPC + alert |

State persisted in `data/risk_state.json`. `risk_engine.allow(ctx) → (ok, reason)` consulted before `risk_manager`.

Halt = entries blocked; `position_manager` continues to manage open positions.

---

## 8. PERFORMANCE_TRACKING

Read-only views computed on-demand from `attribution` + `trades` + `pnl` + `shadow_trades`:

```
perf_strategy(id) → {
  trades, WR, avg_win, avg_loss, expectancy, sharpe,
  allocation_now, pool_used_pct,
  pnl_24h_sol, pnl_7d_sol, drift_vs_shadow
}
perf_wallet(pubkey) → {trades_copied, pnl_sol, last_active_ts, current_S}
perf_token_type(type) → {trades, expectancy, best_strategy}
perf_regime(regime) → {trades, WR, sharpe, dominant_strategy}
perf_hour_utc(h)    → {trades, sharpe}                # find best windows
perf_dd_curve()     → list[(ts, cum_pnl, peak, dd)]
```

All exposed via `src/dashboard.py::print_dashboard(db_path)` printing DataFrames; called from a notebook cell.

---

## 9. ORCHESTRATION_FLOW

**New modules:**
- `src/strategies/__init__.py` — registry
- `src/strategies/s1_copy.py`, `s2_cluster.py`, `s3_new.py`, `s4_dip.py`, `s5_scalp.py`
- `src/regime.py` — classifier + 5-min loop
- `src/allocator.py` — hourly weight updates
- `src/shadow.py` — parallel sim ledger
- `src/risk_engine.py` — hard-limit gate
- `src/dashboard.py` — perf views

**State files (data/):**
- `regime.json`, `allocations.json`, `strategy_priors.json`, `risk_state.json`, `HALT` (sentinel)

**Modified:**
- `signal_engine.py` → fan event to all strategies; for each `match()`, compute unified score; emit one or more `Candidate(strategy, mint, score, size_pre)`. Sort by score desc, take first that passes all gates.
- `risk_manager.py` → calls `risk_engine.allow()` first; then heat caps; then strategy pool check.
- `position_manager.py` → each `Position` carries `strategy_id`; calls strategy's `decide_exit()` instead of global rules.
- `attribution.py` → add `strategy`, `regime_at_open`, `allocation_at_open` columns.
- `bot.py` → spawn: `regime_task(300s)`, `allocator_task(3600s)`, `shadow_task(30s)`, `risk_engine_task(30s)`.

**Decision hierarchy (top first; failure short-circuits):**
1. `risk_engine` hard limits
2. regime gate (`regime_mult_s == 0` → reject)
3. capital allocator (`pool_s < required_size` → reject)
4. portfolio cap + correlation cap
5. `final_score ≥ θ_strategy`
6. `TOKEN_FILTER` (cached 60s)
7. latency gate (p50 ≤ 1.5s)

**Final pipeline:**
```
ingest_q
   ▼
signal_engine ── fan-out ─► [S1..S5].match()  → Candidate(strategy, score)
   ▼ rank by score
risk_engine.allow()        ─► halt? per-strategy off? balance ok?
   ▼
regime gate                ─► regime_mult_s > 0?
   ▼
allocator.allow()          ─► pool_s available?
   ▼
risk_manager.allow()       ─► heat, portfolio, correlation, fee_threshold
   ▼
rug_filter (cached 60s)
   ▼
exec_race  ──┬──────────────► position_manager (strategy-specific exits, 3s tick)
             │                       ▼ on close
             │             attribution.write_close → trade_close_q
             │                       ▼
             └────► shadow.fork()    feedback (priors, weights, allocations)
                    every signal
                    regardless                tuner (1h)  → params.json
                                                regime (5m) → regime.json
                                                allocator(1h)→ allocations.json
                                                risk_engine (30s sweep)
```

**Bootstrapping:** all state files default-init on missing; updates gated on N≥10 per-strategy and N≥30 global. Real and shadow ledgers run in lockstep from boot.

**Tests:**
- `test_strategies.py` — each strategy `match`/`decide_exit` against fixtures.
- `test_regime.py` — input vectors → expected regime + hysteresis.
- `test_allocator.py` — sequence of trade outcomes → expected allocations.
- `test_shadow.py` — simulated price tick stream → expected sim_roi.
- `test_risk_engine.py` — each rule fires and resets.

**Capital preservation invariants (must always hold):**
- `Σ open_position_sol ≤ capital_sol × 0.30`
- `wallet_balance_sol ≥ 0.10` post-trade
- daily realized loss never exceeds 30% of starting-day capital → enforced by check #2
- no single strategy ever holds > 50% of capital → enforced by `clip(a_s, 0.05, 0.50)`
