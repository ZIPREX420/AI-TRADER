# V8 — `solana-mvp/`: Live Bridge Project Layout

Standalone Python package that consumes v6 research outputs (read-only) and routes them into a live or simulated execution loop. Every module is independently testable. Live + sim share the same control flow via the `Executor` abstraction.

---

## 1. FINAL_FOLDER_STRUCTURE

```
solana-mvp/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── pyproject.toml                  # package metadata + ruff/pytest config
│
├── config/
│   ├── __init__.py
│   ├── settings.py                 # capital, thresholds, mode flags, paths (loads .env)
│   └── risk.py                     # RISK_RULES dict (hard limits, kill-switch sentinel)
│
├── core/
│   ├── __init__.py
│   ├── types.py                    # shared dataclasses: Event, Candidate, LivePattern,
│   │                               #   Position, ExecResult, Decision
│   ├── bridge.py                   # V6Bridge: loads promoted_patterns + scored wallets,
│   │                               #   mtime-watched, .match(features) → LivePattern|None
│   ├── wallet_intel.py             # WalletIntel: rescore from v6 swaps parquet → S, cluster_id;
│   │                               #   .get(pubkey) → Score; CLI: --rescore
│   ├── signal_engine.py            # SignalEngine: event → fingerprint → channels (W,T,P,C)
│   │                               #   → Candidate(mint, confidence, pattern_id, source)
│   ├── execution.py                # Executor (abstract) + LiveExecutor (Jupiter+Jito) +
│   │                               #   MockExecutor (deterministic sim from price feed)
│   ├── risk.py                     # RiskGate: 10 hard rules + halt sentinel; .allow()
│   └── portfolio.py                # Portfolio: open positions, sizing, concurrency caps;
│                                   #   .size_for(candidate) / .open() / .close()
│
├── runtime/
│   ├── __init__.py
│   ├── loop.py                     # async main() — wires everything, spawns tasks
│   ├── feedback.py                 # append live_outcomes.parquet + EWMA pattern updates
│   └── logger.py                   # TradeLog: SQLite (trades/signals/skips) + JSONL telemetry
│
├── connectors/
│   ├── __init__.py
│   ├── v6_adapter.py               # read-only loader for v6 research outputs
│   │                               #   (promoted_patterns, wallet_candidates,
│   │                               #    smart_wallets_scored, swaps parquet)
│   └── solana_rpc.py               # SolanaRPC: WS logsSubscribe + HTTP RPC (dual-endpoint
│                                   #   race) + Jupiter quote/swap + Jito bundle submit
│
├── notebooks/
│   └── mvp_live.ipynb              # boot, view bridge state, start loop, live PnL panel
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                 # fixtures: mock v6 outputs, fake RPC, fake price feed
│   ├── test_bridge.py
│   ├── test_wallet_intel.py
│   ├── test_signal_engine.py
│   ├── test_risk.py
│   ├── test_execution.py           # MockExecutor determinism + LiveExecutor (mocked HTTP)
│   ├── test_portfolio.py
│   └── test_feedback.py
│
├── secrets/                        # .gitignored
│   └── trader.json                 # Solana keypair (created via solana-keygen)
│
└── data/
    ├── research_in/                # symlink/path → v6 ../INSTANT-AI-TRADER/data/research/
    ├── state/                      # mvp-owned mutable state (atomic JSON writes)
    │   ├── pattern_state.json      # EWMA live_S, boot_alloc per pattern
    │   ├── risk_state.json         # streak counters, halt timers
    │   └── HALT                    # presence ⇒ kill-switch
    ├── logs/
    │   ├── mvp.db                  # SQLite (trades, signals, skips, pnl)
    │   └── telemetry.jsonl         # event/skip/error stream
    └── signals/                    # debug dumps of recent signals
```

---

## 2. MODULE_RESPONSIBILITIES

| Module | Responsibility |
|---|---|
| `config/settings.py` | Loads `.env` → typed `Settings` (CAPITAL_SOL, MAX_POSITION_PCT=0.07, MAX_OPEN=3, RESERVE=0.05, FEE_THRESHOLD_PCT=0.015, MODE∈{live,sim}, paths). Single source for all params. |
| `config/risk.py` | Frozen `RISK_RULES` table: 10 entries (daily_loss_pct=−0.30, dd_pct=−0.50, loss_streak=3, wr_min=0.30, sol_crash_pct=−0.20, rpc_fail_rate=0.30, latency_p50_max_s=2.0, balance_floor=0.10, per_pattern_daily_loss_pct=0.15, halt_path="data/state/HALT"). |
| `core/types.py` | Pure dataclasses; no I/O. `Event`, `Candidate`, `LivePattern`, `Position`, `ExecResult`, `Decision`, `Score`. |
| `core/bridge.py` | `V6Bridge.from_disk()` → loads `promoted_patterns.json`. `start_watcher()` polls mtime every 60 s. `.match(features) → LivePattern\|None`. Caches by fingerprint. |
| `core/wallet_intel.py` | `WalletIntel.from_disk()` → reads `smart_wallets_scored.json` (built by CLI from v6 swaps parquet). `.score(pubkey) → Score{S, cluster_id, early_entry}`. CLI: `python -m core.wallet_intel --rescore --window-days 30`. |
| `core/signal_engine.py` | Stateful per-mint EWMA + 90 s cluster window. `evaluate(event) → Candidate\|None` with `confidence = 0.40·W + 0.25·T + 0.20·P + 0.15·C`. Computes 8-bit fingerprint, queries `bridge.match`. Threshold `θ_dyn = max(0.55, matched.threshold_θ)`. |
| `core/execution.py` | `Executor` ABC: `quote / submit_buy / submit_sell / confirm`. `LiveExecutor` (Jupiter v6 + Jito bundle, dual-RPC race, retry-with-bump). `MockExecutor` (deterministic, seeded RNG, fed by price feed). Both return identical `ExecResult`. |
| `core/risk.py` | `RiskGate(cfg).allow(candidate, ctx) → Decision(ok, reason)`. Implements 10 rules from `config/risk.py`. `is_halted()` checks `HALT` sentinel + halt timers in `risk_state.json`. `background_sweep()` task refreshes streak counters + auto-clears expired halts. |
| `core/portfolio.py` | Owns open positions in-memory + on disk. `size_for(candidate) → SOL` per v7 formula. Enforces `max_open=3`, `≤1 per pattern`, `≤2 per cluster`, `≤1 per mint`, portfolio_cap=30%. `position_loop(executor, log, feedback, tick_s=3)` evaluates exits (TP/SL/mirror/time-stop). |
| `runtime/loop.py` | Async `main(mode)` — wires modules, spawns tasks (`rpc.stream_events`, `portfolio.position_loop`, `risk.background_sweep`, `bridge.watcher`, `intel.watcher`), drains queue, dispatches each event. |
| `runtime/feedback.py` | On position close: appends row to `data/research_in/live_outcomes.parquet`; updates `data/state/pattern_state.json` with EWMA `live_S' = 0.95·live_S + 0.05·clip(roi,−1,1)` and adjusts `boot_alloc`. **Never** writes canonical v6 state. |
| `runtime/logger.py` | `TradeLog`: SQLite schema for `trades, signals, skips, errors`; helpers `entry/exit/skip/failure`; JSONL telemetry tail. Optional Telegram sender on entry/exit/halt. |
| `connectors/v6_adapter.py` | Read-only loader. Exposes `load_promoted_patterns()`, `load_wallet_candidates()`, `load_scored_wallets()`, `iter_swaps(start, end)`. No writes. |
| `connectors/solana_rpc.py` | Single class `SolanaRPC`: `stream_events(queue)` (logsSubscribe), `get_token_supply`, `get_account_info_json_parsed`, `get_recent_priority_fees`, `submit_jito_bundle`, `submit_rpc`, `confirm`. Dual-endpoint failover. |
| `notebooks/mvp_live.ipynb` | 5 cells: env preview · bridge/intel snapshot · `task = asyncio.create_task(loop.main(mode))` · live tail of `mvp.db` · graceful stop. |

---

## 3. EXECUTION_FLOW

```
                    ┌──────────────────────────┐
                    │  v6 research outputs     │     (read-only mount)
                    │  data/research/...       │
                    └────────────┬─────────────┘
                                 │
                ┌────────────────┴───────────────┐
                ▼                                ▼
       connectors.v6_adapter            connectors.v6_adapter
                │                                │
                ▼                                ▼
        core.bridge.V6Bridge          core.wallet_intel.WalletIntel
        (mtime-watched)               (mtime-watched, rescored nightly)
                │                                │
                └────────────────┬───────────────┘
                                 │
[Solana WS] → connectors.solana_rpc.stream_events → asyncio.Queue
                                 │
                                 ▼
                      core.signal_engine.SignalEngine
                          .evaluate(event)
                          → fingerprint
                          → channels {W, T, P, C}
                          → Candidate(mint, confidence, pattern_id)
                                 │
                                 ▼
                      core.risk.RiskGate.allow()  ──► skip + log
                                 │ (ok)
                                 ▼
                      core.portfolio.size_for()   ──► skip if size < min
                                 │
                                 ▼
                core.execution.{Live|Mock}Executor.submit_buy()
                                 │
                          ┌──────┴──────┐
                          ▼             ▼
                       on ok          on fail → quarantine + log
                          │
                          ▼
                core.portfolio.open(position)
                          │
                          ▼
        portfolio.position_loop (3 s tick)
        ├─ TP ladder, hard SL, mirror, time-stop
        ├─ partial / full sells via executor.submit_sell
        │
        ▼ on close
        runtime.feedback.write_close()
                ├─► append data/research_in/live_outcomes.parquet  (consumed by v6 next nightly)
                ├─► update data/state/pattern_state.json           (EWMA per pattern)
                └─► append data/evolution_log.jsonl                (audit)

Background tasks (parallel):
  • core.risk.background_sweep   every 30 s   → refreshes risk_state.json
  • core.bridge.watcher          every 60 s   → reload on mtime change
  • core.wallet_intel.watcher    every 60 s   → reload on mtime change
  • portfolio.position_loop      every  3 s   → exit decisions
  • runtime.logger.flush         every 10 s   → batch SQLite commits

Halt sources (any one halts entries; existing positions still managed):
  • data/state/HALT exists
  • daily_loss > 30% capital
  • drawdown > 50% peak
  • loss_streak ≥ 3 (cooldown 4 h)
  • last-20 WR < 0.30 (halt 2 h)
  • SOL price −20% in 60 min (halt 1 h)
  • RPC failure rate > 30% in 5 min
  • latency p50 > 2 s over last 10 events (halt 1 min)
  • wallet SOL balance < 0.10
```

**Pseudocode** (`runtime/loop.py`):
```python
async def main(mode: str = "live"):
    cfg     = settings.load()
    rpc     = SolanaRPC(cfg)
    bridge  = V6Bridge.from_disk(); bridge.start_watcher()
    intel   = WalletIntel.from_disk(); intel.start_watcher()
    risk    = RiskGate(cfg)
    sig     = SignalEngine(bridge, intel, cfg)
    port    = Portfolio(cfg)
    log     = TradeLog(cfg)
    feed    = Feedback(cfg)
    exec_   = LiveExecutor(rpc, cfg) if mode == "live" else MockExecutor(rpc, cfg)

    queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
    asyncio.create_task(rpc.stream_events(queue))
    asyncio.create_task(port.position_loop(exec_, log, feed, tick_s=3))
    asyncio.create_task(risk.background_sweep(period_s=30))

    async for ev in _queue_iter(queue):
        if not risk.event_processing_ok():
            continue
        cand = sig.evaluate(ev)
        if cand is None:
            continue
        decision = risk.allow(cand)
        if not decision.ok:
            log.skip(cand, decision.reason); continue
        size = port.size_for(cand)
        if size < cfg.MIN_TRADE_SOL:
            log.skip(cand, "size_below_min"); continue
        res = await exec_.submit_buy(cand.mint, size, cand)
        if res.ok:
            port.open(res, cand); log.entry(res, cand)
        else:
            log.failure(res, cand)
```

**Capital invariants enforced:** `Σ open_sol ≤ capital·0.30` · `wallet_sol ≥ 0.10` · `daily_loss ≤ 30%` · `max_pos ≤ capital·0.07` · `max_open=3` · no pattern > 20% boot_alloc.

**Test surface (per module, ≥1 test file each):** unit-level logic only — no live network calls in CI. Mocks: `FakeRPC`, `FakeV6Adapter`, `FakePriceFeed`, deterministic `MockExecutor`.

**Implementation order:** `config/*` → `core/types.py` → `connectors/v6_adapter.py` → `core/bridge.py` + `core/wallet_intel.py` → `core/risk.py` + `core/portfolio.py` → `core/signal_engine.py` → `connectors/solana_rpc.py` → `core/execution.py` → `runtime/{logger, feedback, loop}.py` → notebook → tests in lockstep with each module.
