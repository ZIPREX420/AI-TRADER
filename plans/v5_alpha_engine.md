# V5 Predictive Alpha Engine — directly implementable

Layered atop v4: anomaly detection sidecar → log-odds aggregation → conviction tiers → continuous evolution.

---

## 1. ALPHA_SOURCES

| ID | Class | Definition |
|----|-------|------------|
| **A1** | graph | Smart wallet receives ≥0.5 SOL from another tracked smart wallet within 60 min, then buys mint X |
| **A2** | graph | Known dev/insider wallet sends to a fresh wallet (<24h, no prior trades) which buys mint X |
| **A3** | graph | ≥3 wallets funded from same CEX deposit addr in 60 min all buy same mint within 30 min |
| **A4** | lifecycle | Pump.fun mint graduates in <30 min from creation AND ≥0.5 SOL net inflow in next 60 s |
| **A5** | lifecycle | Established mint (>7d age, last 24h vol < $5k) suddenly has unique-buyers/min Z ≥ 3 |
| **A6** | lifecycle | LP migration Raydium→Meteora/Orca with concentrated LP, depth ≥ $30k |
| **A7** | velocity | buyer/seller count ratio over 60 s ≥ 4× rolling 30 min baseline AND ≥ 8 buyers |
| **A8** | velocity | 2nd-derivative of cumulative SOL inflow > 0 across 3 consecutive 30 s windows |
| **A9** | velocity | unique-buyer-velocity Z-score ≥ 3 vs 1 h baseline AND ≥ 5 absolute |
| **A10** | liquidity | LP add ≥ 0.5 SOL by non-creator wallet |
| **A11** | liquidity | ANY LP remove event → emergency-exit signal (downside, not alpha) |
| **A12** | liquidity | Jupiter best-route hop count drops from ≥3 to 1 (organic depth growth) |

Output: `AlphaSignal(kind, mint, magnitude, ts, payload)` from `src/anomaly.py` → signal_engine.

---

## 2. ANOMALY_RULES

Per-mint EWMA (Welford) stats kept in dict; α=0.05 (~20-sample memory); evict after 6 h idle.

```
update m_t for each metric:
  μ_t = (1−α)·μ_{t-1} + α·m_t
  σ²_t = (1−α)·σ²_{t-1} + α·(m_t − μ_t)²
  Z = (m_t − μ_t) / max(σ_t, ε)         ε = 1e-6
```

Specific triggers (all require m_t ≥ floor to suppress noise):

| Trigger | Z gate | Floor |
|---|---|---|
| `buy_burst` (buy_count_60s) | Z ≥ 3 | count ≥ 8 |
| `inflow_burst` (sol_inflow_60s) | Z ≥ 3 | sol ≥ 1.5 |
| `buyer_velocity` (unique_buyers_60s) | Z ≥ 3 | uniq ≥ 5 |
| `cluster_burst` (distinct_clusters_90s) | ≥ μ + 2σ | clusters ≥ 3 |
| `liq_injection` (LP add by non-creator) | n/a | sol ≥ 0.5 |
| `liq_removal` (LP remove) | n/a | any → forced exit |
| `graph_anomaly` (A1/A2/A3) | n/a | event-triggered |

Composite: `anomaly_score = max(min(Z/3, 1.0), bin_indicator)` per channel, fed to §3.

---

## 3. SIGNAL_MODEL

Probabilistic aggregation via log-odds (Naive-Bayes-lite, no ML training):

```
log_odds = logit(P_strategy_prior[id])
for channel i (e_i ∈ [0,1], weight w_i, baseline b_i):
    log_odds += w_i · (e_i − b_i)
P_pump = σ(log_odds) = 1 / (1 + exp(−log_odds))
```

| Channel | e_i source | w_i (default) | b_i |
|---|---|---:|---:|
| W_quality | wallet score | 1.6 | 0.5 |
| T_safety | filter pass margin | 1.2 | 0.5 |
| E_timing | latency+anti-FOMO | 0.8 | 0.5 |
| cluster_z | min(z_clusters/3, 1) | 1.4 | 0.0 |
| inflow_z | min(z_inflow/3, 1) | 1.2 | 0.0 |
| buyer_z | min(z_buyers/3, 1) | 1.0 | 0.0 |
| graph_hit | A1∨A2∨A3 → 0/1 | 1.8 | 0.0 |
| lp_injection | A10 → 0/1 | 1.0 | 0.0 |
| route_compress | A12 → 0/1 | 0.6 | 0.0 |
| regime_fit | R_regime[s] | 0.6 | 0.5 |
| token_history | H_token[t] | 0.6 | 0.0 |

Decision: `P_pump ≥ θ_s` (per strategy, θ in probability space, default 0.55).
Persist channel weights in `data/lr_weights.json` (atomic write).

---

## 4. PREDICTIVE_LAYER

Conviction tiers applied to size and exits:

| Tier | P_pump | size_mult | TP1 adj | SL adj | Concurrency cap |
|------|-------:|----------:|--------:|-------:|----------------:|
| LOW | 0.45–0.60 | 0.5 | +0.10 (later) | tighter +0.05 | normal |
| MED | 0.60–0.75 | 1.0 | 0 | 0 | normal |
| HIGH | 0.75–0.85 | 1.5 | −0.10 (faster) | −0.05 (looser) | normal |
| ULTRA | ≥ 0.85 | 2.0 | −0.20 | −0.10 | max_open=1 in band |

Total cap stays ≤ `0.12 × capital_sol` per trade after composing tier × regime × allocator.

Below 0.45 → reject live, still shadowed.

**Calibration check (nightly)**: bin executed trades into 10 P_pump deciles; compute realized WR per bin. If `mean(|WR_bin − bin_center|) > 0.10` → re-fit LR weights via closed-form logistic least squares on logged `(channels, outcome_binary)`. Logged in `data/calibration.jsonl`.

---

## 5. SHADOW_EVOLUTION

Pattern = `(strategy_id, regime, dominant_channel_mix_bucket)`.

**Promotion path** (shadow → live, alloc 0.05 probe):
```
sim_n ≥ 50 AND sim_sharpe ≥ 1.0 AND sim_expectancy ≥ 0.10 AND sim_pf ≥ 1.5
```
After 20 real trades on a probe pattern:
```
real_sharpe ≥ 0.7·sim_sharpe → expand alloc toward target
real_sharpe < 0.5·sim_sharpe → drift_demote: alloc ×0.5
```

**Demotion** (active patterns):
```
real_sharpe < 0 over rolling 30 trades  → freeze 24 h
real_expectancy < 0 over 50 trades      → suspend 7 d
real_n ≥ 100 AND real_sharpe < 0.5·bootstrap → permanent demote (floor alloc 0.05)
```

**Hypothesis generator (nightly)**: bucket numeric features of last-30d trades into winners vs losers (no ML — just per-feature mean+stdev split). Feature axes where `|μ_win − μ_lose| > 1·σ_pooled` become candidate rules. Auto-spawn a shadow pattern combining top-3 such axes. Track for 50 trades before allowing promotion eligibility.

State: `data/live_patterns.json`, `data/shadow_patterns.json`.

---

## 6. ALPHA_DECAY

**Per-channel Information Coefficient (IC)** over rolling N=100 closed trades:
```
IC_i = corr(e_i_at_decision, roi_realized)
baseline_IC_i = 30d EWMA of IC_i
```

Actions:
```
IC_i − baseline_IC_i < −0.20  →  w_i ← w_i × 0.7
IC_i < 0.05 over last 100      →  w_i ← max(0.1, w_i × 0.5)
IC_i < 0 over last 50          →  w_i = 0  (deactivate; flag for review)
```

**Per-pattern decay rate** over rolling 30 trades:
```
edge_t = expectancy_30
decay_rate = (edge_t − edge_{t-7d}) / max(|edge_{t-7d}|, 0.05)
decay_rate < −0.30 → alloc ×0.5
edge_t < 0 over 30  → disable for 24 h
```

**Wallet decay**:
```
wallet_S ← wallet_S × 0.99 per inactive day  (cap 7 d)
inactive ≥ 7 d → S floor 0.20, remove from active stream subscription
```

**Token-type decay**:
```
H_token[t] halved when expectancy_30d crosses zero downward (latched until 10-trade recovery)
```

State: `data/decay_state.json` updated hourly.

---

## 7. EXECUTION_OPTIMIZATION

```
# Adaptive priority fee
heat_inv = 1 − H                                  # H from §risk
priority_fee = clip(p75_recent · (1 + 0.5·heat_inv), 100_000, 5_000_000)
if regime == MANIA: priority_fee = max(priority_fee, p90_recent)

# Adaptive Jito tip
jito_tip = max(tip_floor_p50, conviction_mult · 12_500)

# Dynamic slippage (bps)
slippage_bps = clip(50 + 2·z_inflow + 30·conviction_tier_index, 100, 800)
   # tier_index: LOW=0, MED=1, HIGH=2, ULTRA=3

# Strategy-aware micro-delay (avoid front-running the leader on copies)
micro_delay_ms = {
  COPY:    clip(median_p50_latency_last100 − 200, 0, 300),  # let leader land first
  CLUSTER: 0,
  NEW:     0,
  DIP:     0,
  SCALP:   0,
}

# Retry ladder
not_landed_in_3s → re-quote, fee +50%, retry once
not_landed_in_4s → abort + log
slippage_failure → re-quote slippage·1.3, retry once

# Confirmation optimism
buys:  require commitment="confirmed"   (avoid reorg double-fill)
sells: accept commitment="processed"    (free capital faster)

# Route warming
quote every 20 s for: watched smart-wallet held mints + own open positions
cache ComputeBudget+SetComputeUnit ix per mint for 60 s
```

State: `data/exec_params.json`; refreshed every 10 min by tuner.

---

## 8. SYSTEM_EVOLUTION

Nightly `src/evolver.py` at 03:00 UTC:

1. **Wallet discovery**
   - For each profitable real or shadow trade in last 7d: find wallets that bought ≥30 s pre-entry and held ≥ time-to-TP1.
   - Score via `wallet_scorer`; require `S ≥ 0.55` and ≥3 winning instances → watchlist.
   - 5 winning instances over 14 d → auto-promote into active stream subscriptions.

2. **Cluster rebuild**
   - Re-cluster active wallets via shared-mint Jaccard ≥ 0.40 over last 30 d.
   - Sunset clusters with no activity in last 7 d.

3. **Pattern mining**
   - Bucket each trade by binary fingerprint: `[has_cluster, prepump_kind, wallet_S>0.7, regime, entropy>3.5]`.
   - Fingerprints with ≥10 trades, expectancy ≥ 0.20, WR ≥ 0.55 → register `candidate_pattern` → auto-spawn shadow.

4. **Obsolete phase-out**
   - Channels with `IC < 0.05` for 30 d AND ≥100 samples → `data/disabled_channels.json`.
   - Wallets inactive 14 d → drop completely.

5. **Calibration**
   - Brier score over last 100 P_pump predictions.
   - If `Brier > 0.30` → re-fit LR weights via logistic-LS on `(channels, outcome_binary)` (closed-form, no SGD). Persist `data/lr_weights.json`.

Output append-only: `data/evolution_log.jsonl` `{ts, action, target, before, after, rationale}`.

---

## INTEGRATION OVERVIEW

**New modules:**
- `src/anomaly.py` — per-mint EWMA stats, alpha-signal emitter
- `src/predictive.py` — log-odds aggregation, conviction tiers, calibration
- `src/decay.py` — IC tracker + nightly weight decay
- `src/evolver.py` — nightly discovery + pattern mining + LR re-fit
- `src/exec_optim.py` — adaptive fees, slippage, micro-delay

**Modified:**
- `signal_engine.py` — replaces `final_score` with `P_pump` from `predictive.score()`; consumes anomaly signals
- `executor.py` — reads `exec_params.json` per submit; applies micro-delay per strategy
- `tuner.py` — adds calibration-trigger path; recomputes channel weights when Brier > 0.30
- `bot.py` — spawns: `anomaly_task` (sidecar to ingest), `decay_task` (hourly), `evolver_task` (nightly), `exec_param_task` (10 min)

**State (data/):**
- `lr_weights.json` — channel weights
- `decay_state.json` — IC + baselines per channel and pattern
- `live_patterns.json`, `shadow_patterns.json` — pattern registry
- `exec_params.json` — adaptive execution params
- `evolution_log.jsonl` — append-only audit
- `disabled_channels.json` — phased-out signals
- `calibration.jsonl` — nightly calibration snapshots

**Pipeline:**
```
ingest_q
   ▼─────────────────► anomaly.py (sidecar) → AlphaSignal(s) into signal_engine
signal_engine.fan_out → strategies.match()  → Candidate
   ▼ for each candidate:
   features dict + alpha signals → predictive.score() → P_pump → tier
   ▼
risk_engine → regime → allocator → risk_manager (heat, corr, fee_threshold)
   ▼
rug_filter → exec_optim.params() → exec_race
   ▼
position_manager (3s, strategy.decide_exit, tier-adjusted TP/SL)
   ▼ on close
attribution → trade_close_q
   ├── feedback (priors, weights, wallet_overrides)
   ├── decay_task hourly (IC, channel weights, pattern alloc)
   ├── tuner_task hourly (params, calibration check)
   └── evolver_task nightly (new wallets, patterns, clusters, LR re-fit)
```

**Capital invariants (unchanged from v4):**
- `Σ open_sol ≤ capital_sol·0.30`
- `wallet_balance_sol ≥ 0.10`
- daily realized loss ≤ 30%
- no strategy > 50% capital
- ULTRA tier capped at 1 concurrent position
- conviction × regime × allocator size composition capped at `0.12·capital_sol`

**Tests:**
- `test_anomaly.py` — synthetic time-series → expected Z-trigger fires
- `test_predictive.py` — channel vector → expected P_pump; calibration smoke
- `test_decay.py` — IC drift fixture → weight reduction
- `test_evolver.py` — synthetic trade history → expected pattern + wallet promotions
- `test_exec_optim.py` — heat/regime inputs → expected fee/slippage params
