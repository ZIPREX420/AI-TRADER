# V7 MVP Live Bridge — directly implementable

Minimal layer connecting v6 research outputs → v1 live execution. No v6 redesign. New code lives in `src/live/*` and reuses existing executor / rug_filter / risk_manager / position_manager / ingest verbatim.

---

## 1. LIVE_BRIDGE

V6 produces three proposal artifacts; live consumes via `src/live/bridge.py`:

| V6 file | Live consumer | Reload | Action |
|---|---|---|---|
| `data/research/promoted_patterns.json` | `signal_engine_live` | mtime ≤60s | adds `LivePattern(id, fingerprint, threshold, boot_alloc)` to active set |
| `data/research/wallet_candidates.json` | `wallet_scorer_live` | nightly | revalidates from swaps parquet; promotes to `smart_wallets_scored.json` if WR≥0.65 ∧ n≥30 |
| `data/research/proposed_lr_weights.json` | `signal_engine_live` | manual | applies only after `data/research/approvals.json[<run_id>]=true` OR `oos_sharpe ≥ live_sharpe·1.2 ∧ KL≤1.5` |

**Score → execution mapping** (per pattern):
```
exec_weight   = clip(rank_score, 0.05, 0.50)
boot_alloc    = exec_weight                          # capital share at first activation
size_mult     = 0.5 + exec_weight                    # 0.55..1.0
threshold_θ_p = clip(0.55 + 0.20·(1 − exec_weight), 0.55, 0.85)  # weaker → pickier
max_pos_sol_p = capital_sol · min(0.07, 0.04 + exec_weight·0.06)
```

Schema additions to `promoted_patterns.json`:
```json
[{"run_id":"…","pattern_id":"fp_10110100","fingerprint":"10110100",
  "rank_score":0.74,"metrics":{…},
  "boot_alloc":0.20,"threshold_θ":0.59,"max_pos_sol":0.052}]
```

API: `LiveBridge.load() → list[LivePattern]`; `LiveBridge.match(event_features) → LivePattern | None`.

---

## 2. SMART_WALLETS

**Source of truth:** `data/research/swaps/*.parquet` (already collected by v6).
**Job:** `src/live/wallet_scorer_live.py` — runs nightly (or on-demand `python -m src.live.wallet_scorer_live --window-days 30`).

Per-wallet metrics over rolling 30 d (FIFO buy→sell pair matching, max 7 d open per pair):
```
WR          = wins / closed_trades
ROI         = Σ realized_sol / Σ sol_invested
HOLD_MED    = median hold seconds
ENTRY_PCTL  = mean percentile of buy ts within mint's first-1h volume CDF
COORD       = max Jaccard(traded_mints) vs other tracked wallets

S = 0.35·WR + 0.25·tanh(ROI/2)
  + 0.15·(1 − min(HOLD_MED/3600, 1))
  + 0.15·(1 − ENTRY_PCTL)
  + 0.10·COORD
```

**Filter (AND):**
```
WR ≥ 0.55
ROI ≥ 0.40
n_closed ≥ 30
0.5 ≤ mean_pos_size_sol ≤ 50
last_trade_age ≤ 7d
max_single_trade_pnl / total_pnl ≤ 0.60
24h_drawdown ≥ −0.20
```

**Clustering:** undirected graph, edge if shared-mint Jaccard ≥ 0.40 over 30 d → connected components → `cluster_id`.

**Output:** `data/smart_wallets_scored.json`:
```json
[{"pubkey":"…","S":0.72,"cluster_id":"c3","metrics":{…},"scored_at":1714521600}]
```

**Live consumer:** `signal_engine_live.evaluate()` pulls `wallet.S` from this file (cached, mtime-reloaded). Wallets with `S < 0.55` ignored.

**Early-entry detection:** wallet whose buy precedes pump start by ≥30 s, ≥3 historic instances → flag `early_entry=True`; gives +0.05 bonus to W in scoring.

---

## 3. SIGNAL_ENGINE

**Inputs at decision time:**
```
W = wallet.S (max over triggering wallets), 0 if none
T = rug_filter pass margin in [0,1]
P = LiveBridge.match(features) ? matched.rank_score : 0
C = 1 if ≥3 distinct cluster_ids bought mint within last 90s, else 0
```

**Confidence:**
```
confidence = 0.40·W + 0.25·T + 0.20·P + 0.15·C
```

**Threshold:**
```
θ_dyn = max(0.55, matched_pattern.threshold_θ if matched else 0.55)
```

**Decision:**
```
trade iff (confidence ≥ θ_dyn) ∧ (T ≥ 0.7 [TOKEN_FILTER pass]) ∧ (¬halt)
        ∧ (mint not quarantined) ∧ (latency_gate_ok)
```

**Per-event fingerprint** (for pattern match):
```
[has_cluster, prepump_kind∈{0..4}, S>0.7, regime∈{N,M,D,V},
 entropy>3.5, route_compression, lp_injection, pumpfun_grad]
```

API: `signal_engine_live.evaluate(event, ctx) → Candidate | None`.
`Candidate(mint, confidence, pattern_id, fingerprint, source, max_size_hint)`.

---

## 4. EXECUTION_ENGINE

**Reuse v1 `src/executor.py` verbatim.** New wrapper `src/live/executor_live.py`:

```
slippage_bps     = clip(200 + (1 − confidence)·600, 200, 800)
priority_fee     = clip(p75_recent · (1 + 0.5·(1−H)), 100_000, 5_000_000)   # H = heat
jito_tip         = max(tip_floor_p50, 12_500)
buy_path         : Jito bundle (private mempool)
sell_path        : RPC, skip_preflight=False, priority +25%
preflight_check  : Jupiter quote.priceImpactPct ≤ 4% else abort
latency_gate     : last-10 detect→submit p50 ≤ 1.5s; if >2s halt entries 1m
retry            : 1 retry at 3s no-confirm with priority×1.5; abort after 2
quarantine       : 2 consecutive failures on a mint → quarantine 15 min
```

**Submission flow** (atomic):
```
1. quote() → assert priceImpact ≤ 4%
2. build_swap() (Jupiter v6)
3. sign + submit_jito_bundle (with tip)
4. confirm via dual-RPC race (Helius || QuickNode), first-confirm wins
5. on success: position_manager.open(); on fail: log+quarantine
```

Failures classified: `quote_failed | preflight_blocked | submit_failed | not_confirmed | slippage_exceeded | dropped`. All persisted to `trades` with `error` column.

---

## 5. RISK_LAYER

Hard constraints (no override). `src/live/risk_layer.py::allow(ctx) → (ok, reason)`.

| # | Limit | Action |
|---|-------|--------|
| 1 | `data/HALT` exists | full halt, closes-only |
| 2 | daily realized loss > 30% capital | halt 24h |
| 3 | drawdown > 50% from all-time peak | halt 7d |
| 4 | 3 consecutive losses | cooldown 4h, max_open=1 next 5 trades |
| 5 | last-20 WR < 0.30 | halt 2h |
| 6 | per-pattern daily loss > 15% × pattern_pool | disable pattern 24h |
| 7 | wallet SOL balance < 0.10 absolute | halt entries (sells continue) |
| 8 | SOL price drops > 20% in 60 min | halt 1h |
| 9 | RPC failure rate > 30% in last 5 min | switch to backup, halt 30s |
|10 | latency p50 > 2s over last 10 events | halt entries 1 min |

Per-trade hard caps:
```
max_position_sol   = capital_sol · 0.07
max_open           = 3
sol_reserve_floor  = 0.05
portfolio_cap_sol  = capital_sol · 0.30
fee_threshold      = trade rejected if (prio + tip + slip_loss_est) > size · 0.015
```

State: `data/live/risk_state.json` (atomic write). Sweep runs every 30 s in background task.

---

## 6. PORTFOLIO_LOGIC

```
size_sol = base · confidence_mult · pattern_alloc · liquidity_mult
base              = capital_sol · 0.05
confidence_mult   = 0.5 + confidence                    # 0.95..1.5
pattern_alloc     = pattern.boot_alloc                  # 0.05–0.20 from rank_score
liquidity_mult    = min(1.0, pool_tvl_usd / 80_000)
size_sol          = clip(size_sol, 0, max_position_sol)
remaining_room    = max(0, portfolio_cap_sol − Σ open_sol)
size_sol          = min(size_sol, remaining_room)
```

Concurrency caps:
- `max_open = 3`
- `≤ 1 position per pattern_id`
- `≤ 2 positions per cluster_id` (correlation cap)
- `≤ 1 position per mint`

Reject if `size_sol < 0.005 SOL` (too small to be fee-efficient).

---

## 7. RUNTIME_LOOP

Single async loop. `src/live/bot_mvp.py`:

```python
async def main():
    cfg     = load_config()
    bridge  = LiveBridge();        bridge.start_watcher()       # 60s mtime poll
    wallets = ScoredWalletsCache(); wallets.start_watcher()
    risk    = RiskLayer(cfg)
    sig     = SignalEngineLive(bridge, wallets, cfg)
    exec_   = ExecutorLive(cfg)
    pos     = PositionManagerLive(cfg)                          # 3s tick task
    log     = TradeLog(cfg.db_path)
    queue   = asyncio.Queue(maxsize=10_000)

    asyncio.create_task(ingest.run_streams(cfg, queue))         # v1 ingest
    asyncio.create_task(pos.loop())                             # v1 position mgr
    asyncio.create_task(risk.background_sweep())                # 30s

    while True:
        ev = await queue.get()
        if not risk.event_processing_ok(): continue
        cand = sig.evaluate(ev)
        if cand is None: continue
        ok, why = risk.allow(cand)
        if not ok: log.skip(cand, why); continue
        if not rug_filter.run(cand.mint): continue
        size = portfolio.size_for(cand)
        if size < 0.005: continue
        res = await exec_.submit_buy(cand.mint, size, cand)
        if res.ok:
            pos.open_from(res, cand)
            await log.entry(res, cand)
        else:
            await log.failure(res, cand)
```

Position manager (v1 reused) ticks every 3 s, applies TP/SL/mirror/time-stop, writes `pnl` rows + `live_outcomes.parquet` on close.

---

## 8. FEEDBACK_INJECTION

**Live → research, append-only Parquet** (consumed by v6 nightly):

`data/research/live_outcomes.parquet` (per closed trade):
```
trade_id, ts_open, ts_close, mint, pattern_id, fingerprint,
wallets[json], cluster_ids[json],
sol_in, sol_out, fees, roi, peak_ratio, hold_s, exit_kind,
confidence_at_entry, regime_at_entry
```

**Pattern weight update** (online, EWMA, per closed trade):
```
live_S' = 0.95·live_S + 0.05·clip(roi, -1, 1)
boot_alloc' = clip(boot_alloc · (1.5 if real_sharpe ≥ 0.7·sim_sharpe else 0.5), 0.05, 0.20)
n_real ≥ 30 ∧ real_sharpe < 0  →  pattern removed from promoted_patterns.json (via integrate.py)
```
Persisted in `data/live/pattern_state.json` (atomic).

**Wallet score update** (nightly via `wallet_scorer_live`):
- Re-computes from cumulative swaps + new live outcomes.
- Outputs new `smart_wallets_scored.json` (atomic replace).

**Proposed weight refresh:**
- Live tuner runs hourly; if calibration Brier > 0.30 over last 100 trades, calls `integrate.fit_lr_weights()` → writes `data/research/proposed_lr_weights.json`.
- Live system applies only after operator approval or auto-gate.

**Safety:** all live writes go to `data/live/*` or append-only `data/research/live_outcomes.parquet`. Live execution **never** modifies the canonical files in v6's `FORBIDDEN_LIVE_FILES` list — that protection is unchanged.

**Audit:** every state change appends to `data/evolution_log.jsonl`:
```json
{"ts":…,"source":"live_mvp","kind":"pattern_alloc|wallet_score|pattern_demote","before":…,"after":…}
```

---

## NEW MODULES (additive, no v1/v6 changes)

```
src/live/__init__.py
src/live/bridge.py             # LiveBridge: load + match patterns
src/live/wallet_scorer_live.py # CLI: rebuild smart_wallets_scored.json
src/live/scored_wallets_cache.py
src/live/signal_engine_live.py # extends v1 signal_engine; integrates bridge + scores
src/live/executor_live.py      # wraps v1 executor; adds slippage/fee/retry policy
src/live/position_manager_live.py  # 3s tick; appends live_outcomes on close
src/live/risk_layer.py         # 10 rules + state
src/live/portfolio.py          # size_for() + caps
src/live/feedback.py           # EWMA pattern updates + nightly trigger
src/live/bot_mvp.py            # single async loop
notebooks/live_mvp.ipynb       # boot + monitor + manual halt
```

**Tests:**
- `tests/test_bridge.py` — load promoted_patterns + match by fingerprint
- `tests/test_wallet_scorer_live.py` — fixture parquet → expected scores
- `tests/test_signal_engine_live.py` — channel inputs → expected confidence + decision
- `tests/test_risk_layer.py` — each rule fires + resets
- `tests/test_portfolio.py` — sizing formula edge cases
- `tests/test_feedback.py` — closed trade → expected EWMA update + parquet append

**Capital invariants (preserved):**
```
Σ open_sol ≤ capital_sol · 0.30
wallet_sol ≥ 0.10
daily realized loss ≤ 30%
max_position ≤ capital_sol · 0.07
no pattern > 0.20 alloc
```

**Implementation order:** `bridge.py` → `wallet_scorer_live.py` (offline first, no live effect) → `signal_engine_live.py` (gated by feature flag) → `risk_layer.py` + `portfolio.py` → `executor_live.py` → `bot_mvp.py` → `feedback.py`. Each behind `cfg.live_mvp_enabled` so v1 path stays untouched until ready.
