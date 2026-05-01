# V3 Self-Improvement Spec — directly implementable

Core principle: every closed trade emits an attribution record; offline-style updaters re-derive weights, priors, thresholds. No ML, only EWMA/sliding-window arithmetic. All adaptive state in JSON files; reload on each tick.

---

## 1. METRICS

Window: last `N=50` closed trades unless stated otherwise.

| Metric | Formula |
|---|---|
| `roi_t` | `(sol_out_t − sol_in_t − fees_t) / sol_in_t` |
| `WR` | `Σ 1[roi_t>0] / N` |
| `avg_win` | `mean(roi_t | roi_t>0)` |
| `avg_loss` | `mean(roi_t | roi_t<0)` (≤0) |
| `pf` (profit factor) | `Σ_+roi / |Σ_−roi|` |
| `expectancy` | `WR·avg_win + (1−WR)·avg_loss` |
| `ttp` (time-to-profit) | median seconds from `ts_open` to first ratio≥1.10 (winners only) |
| `mdd` (max drawdown) | `max_over_t( max_{u≤t}cum_roi_u − cum_roi_t )` |
| `sharpe_lite` | `mean(roi)/std(roi)` over N |
| `edge_k` | per-source-kind expectancy (copy / cluster / A / B / C / D) |

Computed in `src/metrics.py`. Read-only over SQLite.

---

## 2. LOG_SCHEMA

New SQLite table `attribution`, one row per trade lifecycle (write on entry, update on close):

```sql
CREATE TABLE IF NOT EXISTS attribution (
  trade_id        INTEGER PRIMARY KEY,
  ts_open         REAL,
  ts_close        REAL,
  mint            TEXT,
  source_kinds    TEXT,   -- JSON list e.g. ["copy","cluster_A"]
  wallets         TEXT,   -- JSON list of triggering wallet pubkeys
  cluster_ids     TEXT,   -- JSON list of cluster_ids
  copy_score      REAL,
  wallet_score_max REAL,
  prepump_kind    TEXT,   -- A/B/C/D or NULL
  features        TEXT,   -- JSON {filter+entry+exec+context}
  outcome         TEXT,   -- JSON {sol_in, sol_out, fees, roi, peak_ratio, hold_s, exit_kind, partials}
  score_pred      REAL,   -- final_score at decision time
  weights_used    TEXT    -- JSON snapshot of weights at decision time
);
CREATE INDEX idx_attr_ts ON attribution(ts_close);
CREATE INDEX idx_attr_kind ON attribution(source_kinds);
```

Canonical `features` JSON:
```json
{
 "filter":{"top10":0.18,"entropy":3.4,"holders_5m_growth":22,"wash_ratio":0.31,"dev_pct":0.07,"lp_locked":true,"has_sell":true,"price_impact_buy":0.018,"holders_total":120},
 "entry":{"sol_in":0.043,"expected_slippage":0.012,"latency_ms":820,"anti_fomo_waited_s":0,"smart_holds_active":2,"price_60s_jump":0.18},
 "exec":{"priority_fee":250000,"jito_tip":15000,"retries":0,"used_jito":true,"rpc_winner":"helius","confirm_ms":1300},
 "context":{"sol_price_usd":154.2,"slot":287654321,"hour_utc":14}
}
```

`partials` (in `outcome`): `[{ts, fraction, sol_out, reason}]`.

---

## 3. SCORING_SYSTEM

Three subscores in [0,1], plus per-source prior:

```
W_quality = max(wallet_S over triggering wallets)              # 0..1, 0 if no copy
T_safety  = mean([
   clip(entropy/3.5,0,1),
   clip(1−top10/0.25,0,1),
   clip(holders_5m_growth/30,0,1),
   clip(1−wash_ratio/0.6,0,1),
   1.0 if lp_locked else 0.0
])
E_timing  = 0.40*clip(1 − price_60s_jump/0.50, 0, 1)
          + 0.30*clip(1 − latency_ms/2000, 0, 1)
          + 0.30*(1 if smart_holds_active≥1 else 0)

final_score = w1·W_quality + w2·T_safety + w3·E_timing + b·P_src[kind]
```

Defaults (loaded from `data/weights.json`): `w1=0.40, w2=0.30, w3=0.20, b=0.10` (sum=1).
`P_src` defaults: `{copy:0.55, cluster:0.65, prepump_A:0.60, B:0.45, C:0.40, D:0.55}`.

Trade gate: `final_score ≥ θ` (θ from `data/params.json`, default 0.60).

Implementation: pure function `score(features, weights, priors) → float` in `src/scoring.py`.

---

## 4. FEEDBACK_RULES

After each trade close, run `src/feedback.py::update(attr_row)`:

**Per-source prior (Bayesian-lite, EWMA):**
```
delta = sign(roi) * min(|roi|, 1.0)         # bounded reward
P_src[kind] ← clip(P_src[kind] + 0.02 * delta, 0.05, 0.95)
```

**Per-wallet override:**
```
wallet_S' ← clip(0.95*wallet_S + 0.05*tanh(2*roi), 0.05, 1.0)
```
Persist in `data/wallet_overrides.json`. Loader merges over scorer output.

**Sub-weights (simple gradient toward observed quality):**
```
realized_q = clip((roi+0.5)/2.0, 0, 1)         # roi=−0.5 → 0, roi=1.5 → 1
err = realized_q − final_score_predicted
for i in (W,T,E):
    w_i ← w_i + 0.005 * subscore_i * err
renormalize so w1+w2+w3 = 1 − b
```
Only apply if `N_window ≥ 30`. Hard bounds: each `w_i ∈ [0.05, 0.70]`.

**Atomic write:** write to `*.tmp` then `os.replace()`.

---

## 5. PARAMETER_TUNING

`src/tuner.py` runs every 60 min over last `N=50`. Outputs `data/params.json`:

```
θ           ← clip(θ + 0.01*(0.45 − WR), 0.50, 0.85)         # higher θ if losing
TP1         ← clip(TP1 + 0.05*(median_peak_ratio_TP1 − TP1), 1.50, 2.20)
HARD_SL     ← clip(HARD_SL + 0.02*(median_winners_min_ratio − HARD_SL), 0.50, 0.75)
TIME_STOP_S ← clip(2 * median_ttp_s, 1200, 3600)
BASE_PCT    ← clip(BASE_PCT * (1 + 0.10*sign(sharpe_lite − 1.0)), 0.025, 0.07)
                                                              # rises with sharpe>1, falls below
```
Skip update if any input has fewer than 20 samples.

Consumers:
- `signal_engine` reads `θ` per evaluation
- `position_manager` reads `TP1, HARD_SL, TIME_STOP_S` each tick
- `risk_manager` reads `BASE_PCT`

---

## 6. STRATEGY_EVOLUTION

Per source_kind k, evaluated on every close and at hourly tuner pass:

| Condition (rolling last 30 of k) | Action |
|---|---|
| `E_k < 0 ∧ trades_k ≥ 15` | `P_src[k] ← max(0.10, P_src[k]*0.5)` |
| `E_k > 0.30 ∧ WR_k > 0.50` | `P_src[k] ← min(0.95, P_src[k]*1.2)` |
| `E_k < −0.20 over last 20 of k` | `disabled_until[k] = now + 24h` |
| First trade after cooldown | size = base × 0.5 (probe) |

Per-wallet:
| Condition | Action |
|---|---|
| cumulative copy-PnL of wallet < −1 SOL over 30 trades | `wallet_S ← min(wallet_S, 0.20)` for 7 days |
| wallet inactive ≥ 7d | drop from active set |

State files: `data/source_priors.json`, `data/disabled_sources.json`, `data/wallet_overrides.json`.

---

## 7. RISK_ADAPTATION

**Heat (defensive scalar) computed each `risk.allow()`:**
```
H = clip(0.5 − rolling_pnl_24h / capital_sol, 0.0, 1.0)
```
- `H=0` → no losses 24h; `H=1` → −50% capital lost.

**Adaptive caps:**
```
adj_max_pct  = base_max_pct  * (1 − 0.5*H)         # 0.07 → 0.035 at H=1
adj_max_open = max(1, base_max_open − floor(2*H))   # 4 → 2 at H=1
```

**Streak control (last 5 closes):**
| Streak | Override |
|---|---|
| 3+ losses in a row | `adj_max_open = 1` for next 5 trades |
| 3+ wins in a row | `adj_max_open = min(5, base+1)` for next 5 trades |

**Volatility scale:** if 24h SOL stdev > 1.5× 60d median → multiply size by 0.7.

Hard floors unchanged: daily loss halt −30%; SOL reserve 0.05.

Implementation: `src/heat.py::current(state) → {H, adj_max_pct, adj_max_open, vol_scale}`. Pure read of SQLite.

---

## 8. SYSTEM_INTEGRATION

**New modules:**
- `src/metrics.py` — stat functions over `trades`/`pnl`/`attribution`.
- `src/attribution.py` — `write_open(...)`, `write_close(...)`; called from `bot.py` on entry, `position_manager.py` on close.
- `src/scoring.py` — pure `score(features, weights, priors) → float`; loaders for JSON state.
- `src/feedback.py` — `update(attr_row)`; called from a `trade_close_q` consumer task.
- `src/tuner.py` — async loop, 1h cadence; writes `data/params.json`.
- `src/heat.py` — heat + caps; queried per `allow()`.

**State files (atomic write, all under `data/`):**
- `weights.json` — `{w1,w2,w3,b}`
- `source_priors.json` — `{copy, cluster, prepump_A, B, C, D}`
- `wallet_overrides.json` — `{<pubkey>: {score, expires_ts}}`
- `disabled_sources.json` — `{<kind>: until_ts}`
- `params.json` — `{theta, TP1, HARD_SL, TIME_STOP_S, BASE_PCT}`

**Modified:**
- `signal_engine.py` → import `scoring.score`; reload state files if mtime changed; gate on `final_score ≥ θ`; respect `disabled_sources`.
- `risk_manager.py` → call `heat.current()` to get `adj_max_pct`/`adj_max_open`/`vol_scale`; multiply size by `vol_scale`.
- `position_manager.py` → reload `params.json` on each tick; use dynamic TP1, HARD_SL, TIME_STOP_S; on close push `trade_id` to `trade_close_q`.
- `bot.py` → spawn `tuner_task` (1h tick) and `feedback_task` (consumes `trade_close_q`); pass `trade_close_q` into `position_manager`.
- `logger.py` → add `attribution` table to schema; helpers `write_attribution_open`, `update_attribution_close`.

**Updated pipeline:**
```
ingest_q → signal_engine (uses weights+priors+θ) → risk_manager (heat) → rug_filter
        → exec_race → position_manager (reads params.json each 3s)
                                │
                          on close: write_attribution_close → trade_close_q
                                                                 │
                          ┌──────────────────────────────────────┘
                          ▼
                   feedback_task: update weights, priors, wallet_overrides
                          ▼
                   tuner_task (hourly): writes params.json
                          ▼
                   signal_engine, risk_manager, position_manager auto-reload
```

**Bootstrapping:** if any state file missing on boot, write defaults. Updates apply only after `N_window ≥ 30` for global weights and ≥ 15 per-source for evolution.

**Tests:**
- `test_metrics.py` — synthetic trades → expected WR/PF/expectancy/MDD.
- `test_scoring.py` — known features+weights → expected score.
- `test_feedback.py` — sequence of attr rows → expected weight/prior trajectories.
- `test_tuner.py` — synthetic trade history → expected param deltas.
- `test_heat.py` — pnl scenarios → expected H and caps.

**Operational:** all JSON files human-readable; can be hand-edited mid-run (next reload picks up). Add `data/state.lock` if concurrent writers ever needed; for now single-writer (feedback_task) is sufficient.
