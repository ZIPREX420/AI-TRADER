# V6 Offline Research Engine — directly implementable

Replay historical Solana on-chain data through the **same** v5 strategy code with a simulated executor, mine patterns, validate out-of-sample, then feed proposals back into the live system. Goal: every promotion to live is statistically grounded.

---

## 1. DATA_SCHEMA

Five Parquet datasets under `data/research/{table}/dt=YYYY-MM-DD/part-N.parquet`:

```
swaps      ts, slot, sig, mint, wallet, side(0/1), sol_amount, token_amount,
           price_sol, dex(raydium|pumpfun|orca|meteora|jupiter)
pools      ts, slot, sig, mint, event_kind(init|migrate|lp_add|lp_remove),
           source_program, sol_in_pool, lp_holder, sol_amount, signer
prices     ts, mint, sol_per_token, source(jup|dex), bar_seconds
transfers  ts, sig, src, dst, sol_amount   # only smart-wallet-relevant
wallets    pubkey, first_tx_ts, label, cluster_id, last_seen_ts
```

Index DB `data/research/index.db`:
```sql
collected(table, dt, source, rows, status, sha256, created_ts)
cursors(source, last_signature, last_slot, last_ts)
```

Minimum viable per row ~32–48 bytes. 30 d corpus ≈ 25 MB/day = 750 MB. Feasible on laptop.

---

## 2. DATA_PIPELINE

```
src/research/collectors/
  helius_collector.py   # pulls /v0/addresses/{addr}/transactions paginated
  price_collector.py    # Birdeye /defi/history_price 1m bars
  pool_collector.py     # parses program logs from Raydium/Pumpfun signatures
  graph_collector.py    # SOL transfers between known smart wallets (A1/A2/A3)
storage.py              # parquet IO via pyarrow; append-only per dt= partition
```

Sources (free tier first):
- Helius Enhanced Txn API (parsed) for swaps/pools (free 100k req/mo)
- Helius `getSignaturesForAddress` to discover sigs by program
- Birdeye public OHLCV (1-min bars)
- Optional paid: Helius archival, SolanaFM

Operational:
- Resumable: `data/research/cursor.json` per source = last processed `(slot, sig)`
- Rate limit: token bucket per host; exponential backoff on HTTP 429
- Idempotency: sha256 over (sig|slot) deduped at insert
- Run as CLI: `python -m src.research.collect --source helius --start 2026-04-01 --end 2026-04-30`
- Daily cron task: `python -m src.research.collect --since-cursor`

Storage: Parquet (zstd compression). 10–50× smaller than CSV; columnar scan via `pyarrow.dataset` is fast.

---

## 3. REPLAY_ENGINE

```python
# src/research/clock.py
class Clock:                # abstract
    def now(self) -> float: ...

class WallClock(Clock):     # live system
    def now(self): return time.time()

class ReplayClock(Clock):
    def __init__(self, t0): self._now = t0
    def now(self): return self._now
    def advance_to(self, t): self._now = t
```

```python
# src/research/replay.py
class Replay:
    def __init__(self, start_ts, end_ts, tables=("swaps","pools","transfers"), seed=42):
        self.clock  = ReplayClock(start_ts)
        self.end    = end_ts
        self.heap   = merge_sorted_streams_from_parquet(tables, start_ts, end_ts)
        self.rng    = random.Random(seed)
        self.prices = PriceProvider(start_ts, end_ts)   # interpolated 1m bars
    def __iter__(self):
        for ev in self.heap:
            if ev.ts > self.end: break
            self.clock.advance_to(ev.ts)
            yield ev
```

Determinism: heap-sort by `(ts, slot, sig)`; seeded RNG for any stochastic friction (slippage jitter, tx-drop bool).

Speeds:
- `mode="instant"` (no sleeps; full throughput)
- `mode="realtime_x{N}"` (advances clock relative to wallclock; for visual debugging)
- `mode="batch"` (parameter sweep over many runs)

The **same** `signal_engine`, `strategies/*`, `risk_engine`, `risk_manager`, `position_manager` consume `Event` objects emitted by the heap; they only need `Clock` injected (already abstracted at v3+).

---

## 4. SIMULATION_FLOW

`src/research/simulated_executor.py`:

```python
class SimulatedExecutor:
    def __init__(self, clock, prices, slippage_model, fee_model, latency_dist, drop_rate=0.05):
        ...
    def execute_swap(self, in_mint, out_mint, amount, slippage_bps, ctx):
        # Add realistic latency before action lands
        clock.advance_to(clock.now() + sample(latency_dist))
        if rng.random() < drop_rate:
            return SwapResult(ok=False, error="dropped", elapsed_ms=...)
        price = prices.at(clock.now(), out_mint)
        tvl   = pools.tvl_at(clock.now(), out_mint)
        slip  = 0.5*(slippage_bps/10000) + 1.5*(amount / max(tvl, 1))
        fee   = fee_model(ctx)              # priority + jito + jup
        out   = (amount / price) * (1 - slip) - fee/price
        return SwapResult(ok=True, in_amount=amount, out_amount=out_lamports, ...)
```

Calibration of friction models from live data (`attribution.features.exec`):
```
slippage_model: linear regression on (sol_amount, tvl) → realized slippage
latency_dist:   ECDF of attribution.entry.latency_ms
drop_rate:      1 − confirmed/total over last 1000 live submits
fee_model:      mean priority+tip from live + 0.25% Jupiter
```

Driver: `src/research/run.py`
```python
def run(replay_cfg, strategies_cfg, capital_sol):
    clock   = ReplayClock(replay_cfg.start)
    rpc     = ReplayRpcClient(clock)
    exec_   = SimulatedExecutor(clock, ...)
    bot_ctx = build_v5_bot_with(clock=clock, executor=exec_, rpc=rpc,
                                strategies=strategies_cfg)
    rep     = Replay(...)
    for ev in rep: bot_ctx.dispatch(ev)
    return bot_ctx.run_db_path     # data/research/runs/{run_id}/run.db
```

Output: identical schema as live (`attribution`, `trades`, `pnl`, `shadow_trades`) but in run-scoped SQLite. Reuses live `metrics.py`, `dashboard.py`.

---

## 5. PATTERN_RULES

Run after replay, on `run.db` + raw parquets:

**A. Wallet predictive mining**
For each winning trade `T` (roi ≥ 0.10):
```
candidates = wallets that bought T.mint within [T.ts_open − 300s, T.ts_open − 30s]
             AND still held at T.ts_open + 60s
hits[w] += 1; total[w] = total buys-then-hold by w in window
```
Promote `w` to `data/research/wallet_candidates.json` if `hits[w] ≥ 3 ∧ hits[w]/total[w] ≥ 0.30`.

**B. Pre-pump signature mining**
For each pumped mint (`max(ratio_60min) ≥ 2.0`):
```
features at t_pump - 90s = {buy_count_60s, inflow_60s, unique_buyers_60s,
                            cluster_count_90s, holders_growth_5m,
                            has_graph_anomaly, route_hops}
```
Compute `(μ_pump_i − μ_baseline_i) / σ_baseline_i` per feature i over N pumps and N matched-random baseline windows. Features with z ≥ 1.0 → confirmed precursors → registered in `data/research/precursors.json`.

**C. Trade fingerprint clustering** (no ML)
8-bit fingerprint per closed trade:
`[has_cluster, prepump_kind∈{0..4}, S>0.7, regime, entropy>3.5, route_compression, lp_injection, pumpfun_grad]`.
Bucket trades by fingerprint:
```
profitable bucket: n ≥ 10 ∧ median_roi ≥ 0.30 ∧ WR ≥ 0.55 → discovered_patterns.json
losing bucket   : n ≥ 10 ∧ median_roi ≤ −0.30           → anti_patterns.json
```

**D. Anti-pattern filters**
For every entry condition, compute conditional WR. If `WR(condition) < 0.30 ∧ n ≥ 30` → recommend exclusion rule in `anti_patterns.json`.

Outputs (atomic JSON):
`data/research/{wallet_candidates, precursors, discovered_patterns, anti_patterns}.json`.

---

## 6. EVALUATION

Per run, computed by `src/research/evaluation.py` from `run.db`:

```
total_roi      = (final_capital − initial_capital) / initial_capital
WR             = wins / n
expectancy     = WR·avg_win + (1−WR)·avg_loss
profit_factor  = Σ_+roi / |Σ_−roi|
sharpe_lite    = mean(roi) / max(std(roi), 1e-3)
mdd            = max( max_{u≤t}cum_pnl_u − cum_pnl_t )
recovery_days  = time(peak → next new high) — median across drawdowns
tail_p95_gain  = quantile(roi, 0.95)
tail_p05_loss  = quantile(roi, 0.05)
trades_per_day = n / span_days
alpha_per_trade= total_roi / n
consistency    = std(monthly_sharpe) / max(|mean(monthly_sharpe)|, 1e-3)   # lower = stable
```

**Robustness checks:**
```
bootstrap_5_95   = quantile(mean(resample(roi)) for _ in 1000, [0.05, 0.95])
oos_sharpe_ratio = sharpe(test_30%) / sharpe(train_70%)   # >0.6 acceptable
walk_forward     = rolling 30-day windows; report mean+std of window sharpes
overlap_with_anti= count of trades whose fingerprint matches anti_patterns
```

Report: `runs/{id}/report.json` + `report.html` (simple Jinja template).

---

## 7. STRATEGY_SELECTION

A pattern P is **promotable** iff ALL hold:

```
n_trades_in_backtest      ≥ 100
sharpe_lite               ≥ 1.20
profit_factor             ≥ 1.60
max_dd / capital          ≤ 0.30
oos_sharpe / is_sharpe    ≥ 0.60
bootstrap_5th_percentile_roi > 0
consistency               ≤ 1.50      # stability
overlap_with_anti_patterns/ n_trades < 0.05
```

Composite ranking score:
```
rank_score = 0.40·min(sharpe_lite/2.0, 1)
           + 0.30·min(oos_sharpe/is_sharpe, 1)
           + 0.20·max(0, 1 − consistency/2.0)
           + 0.10·max(0, 1 − max_dd/0.30)
```

Top-N (default N=3 per run) → `data/research/promoted_patterns.json`.

Patterns failing checks → `rejected_patterns.json` with reason. Never re-tested for 7 days unless backtest dataset extended.

---

## 8. LIVE_INTEGRATION

Backtest **never** writes directly to live execution state. Three proposal channels:

**A. New shadow patterns**
Write `promoted_patterns.json` → live `evolver` reads on next nightly pass; entries spawn shadow patterns at zero alloc. Live shadow-evolution rules (v5 §5) handle promotion to probe → live.

**B. Updated channel weights (LR re-fit)**
```
X = decision-time channel matrix from run.db.attribution
y = (roi > 0)  binary outcome
W = ridge_logistic_LS(X, y, λ=0.5)               # closed-form
```
Write to `data/research/proposed_lr_weights.json`. Live tuner consumes after either:
- Operator approval (manual flag in `data/research/approvals.json`)
- Auto-approve if `proposed_oos_sharpe ≥ live_sharpe × 1.2 ∧ KL(old, new) ≤ 1.5`

**C. Wallet candidates**
Append to `data/smart_wallets_candidate.json`. Live `wallet_scorer` revalidates next pass; auto-promote if backtest WR ≥ 0.65 ∧ n ≥ 30; else operator review.

Audit: every change appends to `data/evolution_log.jsonl`:
```json
{"ts":..., "source":"research_run_{id}", "kind":"weight_update|pattern|wallet",
 "before":{...}, "after":{...}, "rationale":"..."}
```

**Safety invariant:** any state file ending `_proposed.json` is read-only by the live execution path. Live consumers only ever read the canonical name. The promotion step *renames* atomically after validation gates.

---

## 9. SCALING_PLAN

| Phase | Horizon | Volume | Storage | Compute | Replay time |
|------|---------|-------|---------|---------|-------------|
| 0 | 30 d | ~1M swaps | local Parquet | laptop | <10 min |
| 1 | 90 d | ~5M swaps | local Parquet, day-partitioned | laptop + `pyarrow.dataset` lazy scan | <30 min |
| 2 | 6+ mo | ~30M swaps | DuckDB over Parquet (still local, no server) | `multiprocessing.Pool` for sweeps; one CPU per param set | <2 h full sweep |
| 3 | 1y+ | ~100M swaps | Parquet on S3/R2; DuckDB `httpfs` | optional ClickHouse for ad-hoc; daily cron refresh | runs in cloud worker |

Scaling invariants:
- Strategy code unchanged across phases (only `collectors/` + `storage.py` evolve).
- Replay determinism preserved (same seed → same trades).
- All phases run on commodity hardware (≤16 GB RAM, no GPU).
- Backtest loop is single-threaded inside one run; parallelism is **across** runs.

Optional accelerations (future, not v6):
- Numba JIT on hot loops (heap merge, EWMA stats)
- Polars instead of pandas for aggregation
- DuckDB columnar joins instead of pandas merges

Triggers for next phase: `phase_n+1` activated when `replay_run_seconds > 1800` for phase n.

---

## INTEGRATION OVERVIEW

**New modules (`src/research/`):**
```
__init__.py
clock.py                 # Clock abstraction
collectors/              # helius_collector, price_collector, pool_collector, graph_collector
storage.py               # parquet IO + index.db
replay.py                # event heap iterator
simulated_executor.py
simulated_rpc_client.py  # serves get_account_info etc. from snapshots
run.py                   # CLI: --start --end --strategies --capital --seed
patterns.py              # mining (A/B/C/D in §5)
evaluation.py            # metrics + bootstrap + OOS + walk-forward
selector.py              # promotion gate + ranking
integrate.py             # writes proposed_*.json + updates evolution_log
notebooks/research.ipynb # interactive runner with charts
```

**Modified live modules** (minimal — most already time-agnostic):
- All time-reading code uses `clock.now()` instead of `time.time()`.
- `executor` accepts injected fee/latency models for replay parity.
- `bot.py` exposes a `build(...)` factory taking `clock`, `executor`, `rpc_client` → reusable in `run.py`.

**State (data/research/):**
```
collected_index.db
swaps/ pools/ prices/ transfers/ wallets/   # parquet partitions
runs/{run_id}/run.db, report.json, report.html
discovered_patterns.json
anti_patterns.json
promoted_patterns.json
rejected_patterns.json
wallet_candidates.json
precursors.json
proposed_lr_weights.json
approvals.json
cursors.json
```

**Pipeline:**
```
collectors (daily cron) ──► parquet datasets
                               │
notebooks/research.ipynb ──► research.run.run(start, end, strategies, capital)
                               │
                          replay heap ──► v5 bot (sim executor, sim rpc, replay clock)
                               │
                          run.db (attribution, trades, pnl, shadow)
                               │
              ┌────────────────┼─────────────────┐
              ▼                ▼                 ▼
        evaluation.py     patterns.py        selector.py
              │                │                 │
              └────────────────┼─────────────────┘
                               ▼
                      integrate.py writes proposed_* files
                               ▼
              live evolver (nightly) consumes promoted_patterns
              live tuner (calibration trigger) consumes proposed_lr_weights
              live wallet_scorer consumes wallet_candidates
                               ▼
                        evolution_log.jsonl (append-only audit)
```

**Tests:**
- `test_replay_determinism.py` — same seed + same data → identical trade list
- `test_simulated_executor.py` — slippage/fee model returns expected SOL out
- `test_patterns.py` — synthetic pumps → expected wallet/precursor outputs
- `test_evaluation.py` — known PnL stream → expected metrics + bootstrap CI
- `test_selector.py` — fixture metrics → correct promotion verdict
- `test_integrate_safety.py` — backtest cannot write canonical live state files
