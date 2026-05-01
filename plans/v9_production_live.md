# V9 Production Live Execution — directly implementable

Hardens the v8 MVP `LiveExecutor` and `solana_rpc` connector. Reuses v6 research path, v8 module shapes, and `ExecResult` contract so `MockExecutor` remains drop-in compatible. Adds explicit mode-state machine for safe fallback.

---

## 1. LIVE_EXECUTION_ARCHITECTURE

```
core/execution.py
  ├─ LiveExecutor (orchestrator, implements Executor ABC from v8)
  └─ MockExecutor (unchanged, used in PAPER mode)

core/execution_jupiter.py    Jupiter v6 quote + swap-tx builder
core/execution_raydium.py    Raydium AMM v4 direct-swap fallback
core/tx_builder.py           VersionedTransaction sign, ALT resolve, CB/CU ix
core/submit.py               Jito bundle (buys) + RPC race submit (sells)
core/confirm.py              Dual-endpoint signature poll
core/route_selector.py       chooses Jupiter vs Raydium per quote
```

**Decision flow inside `LiveExecutor.submit_buy(mint, sol_lamports, candidate)`:**

```
1. pre_check()                     # balance ≥ size+reserve, mint not quarantined
2. quote_primary  = Jupiter.quote()
   if quote_primary is None or no_route or priceImpact > 0.04:
       quote_alt = Raydium.quote()
       chosen    = better_of(quote_primary, quote_alt)   # by realized impact
   else:
       chosen    = quote_primary
   if chosen is None: return ExecResult(ok=False, error="no_route")
3. tx = tx_builder.build(chosen, kp)
   tx = tx_builder.attach_priority(tx, fee_lamports)
   tx = tx_builder.attach_compute_unit_limit(tx, max_cu=600_000)
   signed = tx_builder.sign(tx, kp)
4. sig  = submit.jito_bundle([signed, tip_tx])           # buys: private mempool
5. ok   = await confirm.dual_race(sig, timeout_s=30)
   if not ok:
       (sig2, ok2) = retry_with_bump(chosen, fee×1.5)    # 1 retry
       if not ok2: return ExecResult(ok=False, error="not_confirmed", sig=sig)
6. return ExecResult(ok=True, sig=sig, in=..., out=..., realized_slippage=..., elapsed_ms=...)
```

`submit_sell` is symmetric except: route Jupiter only, RPC submit (preflight=True), priority×1.25.

All errors classified into a fixed enum:
`{quote_failed, no_route, price_impact_exceeded, build_failed, sign_failed, submit_failed, dropped, not_confirmed, slippage_exceeded, balance_low, quarantined, halted}`.

---

## 2. JUPITER_SWAP_FLOW

`core/execution_jupiter.py`:

```python
async def quote(http, in_mint, out_mint, amount, slippage_bps) -> dict | None:
    params = {
      "inputMint": in_mint, "outputMint": out_mint, "amount": amount,
      "slippageBps": slippage_bps, "swapMode": "ExactIn",
      "onlyDirectRoutes": "false", "asLegacyTransaction": "false",
      "maxAccounts": 64,
    }
    r = await http.get("https://quote-api.jup.ag/v6/quote", params=params, timeout=5)
    if r.status_code != 200: return None
    q = r.json()
    if not q.get("routePlan") or int(q.get("outAmount", 0)) == 0: return None
    return q

async def swap_tx(http, quote, user_pubkey, prio_lamports="auto") -> str | None:
    body = {
      "quoteResponse": quote, "userPublicKey": user_pubkey,
      "wrapAndUnwrapSol": True, "useSharedAccounts": True,
      "asLegacyTransaction": False, "dynamicComputeUnitLimit": True,
      "prioritizationFeeLamports": prio_lamports,
    }
    r = await http.post("https://quote-api.jup.ag/v6/swap", json=body, timeout=8)
    return r.json().get("swapTransaction") if r.status_code == 200 else None
```

**Slippage matrix:**
```
bps = clip(200 + (1 − confidence)·600, 200, 800)
buys  : abort if priceImpactPct > 0.04
sells : abort if priceImpactPct > 0.08
```

**Submit (buys via Jito):**
```
tip_tx = build_transfer(kp → random JITO_TIP_ACCOUNT, jito_tip_lamports, recent_bh)
bundle = [signed_swap, tip_tx]
POST {jito_url}/api/v1/bundles  → bundle_id
poll signature_statuses(swap_sig) on Helius + QuickNode every 800ms
```

**Submit (sells via RPC):**
```
sendRawTransaction(skip_preflight=False, preflight_commitment=Confirmed, max_retries=3)
poll signature_statuses every 800ms, timeout 30s
```

**Retry-with-bump (one attempt):**
```
on "not_confirmed" at 30s:
   re-quote (price may have moved)
   priority_fee ← priority_fee · 1.5  (capped 5_000_000 µL)
   re-submit
abort after 2 total attempts
```

**Fallback to Raydium (`core/execution_raydium.py`)** triggers when:
- Jupiter quote returns no route, OR
- Jupiter swap-tx endpoint returns 5xx three times in 60s, OR
- last 5 Jupiter submissions all `not_confirmed`

Raydium direct path: build `swapBaseIn` ix manually against AMM v4 program; pool keys cached per mint. Lower coverage but no aggregator dependency.

---

## 3. RPC_STREAMING_SYSTEM

`connectors/solana_rpc.py` — single coordinator over multiple endpoints:

```
EndpointPool([
  Endpoint("helius",    ws=..., http=..., priority=1),
  Endpoint("quicknode", ws=..., http=..., priority=2),
  Endpoint("triton",    ws=..., http=..., priority=3),  # optional
])
```

**Streaming:**
```
async def stream_events(queue):
    for ep in pool.live_endpoints():
        asyncio.create_task(_ws_consume(ep, queue))
async def _ws_consume(ep, queue):
    while True:
        try:
            async with websockets.connect(ep.ws, ping_interval=20) as ws:
                await subscribe(ws, mentions)
                async for msg in ws:
                    ev = normalize(msg, source=ep.name)
                    if not dedupe.seen(ev.signature):
                        await queue.put(ev)
                    health.record(ep, ok=True, latency=now()-ev.block_time)
        except Exception as e:
            health.record(ep, ok=False)
            await asyncio.sleep(min(30, 1.5 ** ep.consecutive_failures))
```

**Dedupe:** LRU set of `(signature, slot)` over rolling 60 s window.

**Health tracking** (per endpoint, persisted to `data/state/rpc_health.json`):
```
{
  endpoint: {
    latency_p50_ms, latency_p95_ms,
    error_rate_5m, consecutive_failures,
    last_event_ts, last_failure_ts
  }
}
```

**Selection policy:**
- **Streaming**: subscribe to ALL live endpoints in parallel; first to deliver each event wins.
- **Submission**: choose endpoint with lowest `error_rate_5m`; ties broken by `latency_p50_ms`.
- **Demote** an endpoint if `error_rate_5m > 0.30`: skip for 10 min cooldown.

**Health events emitted into queue:**
```
StreamHealth(degraded=True, reason="all_silent_60s")  → mode_manager picks up
```

**Watchdog:** if all endpoints silent 60 s → publish `StreamHealth(degraded=True)` and trigger `mode_manager` transition to `DEGRADED_RPC`.

**Normalized event shape** (consumed by signal engine):
```python
@dataclass
class Event:
    ts: float
    slot: int
    signature: str
    kind: str          # "log" | "swap" | "pool" | "transfer"
    source: str        # endpoint name
    mention: str       # subscription that matched
    raw_logs: list[str]
    block_time: float | None
```

---

## 4. LIVE_WALLET_TRACKER

`runtime/wallet_tracker_live.py` — async pipeline.

**Subscription manager:**
```
- top-N wallets by S (default N=100) read from smart_wallets_scored.json
- chunked into N/50 WS connections (Helius mention-filter cap)
- rotates membership every 5min: drop wallets whose S dropped below 0.55, add new high-S
- mtime-watch on smart_wallets_scored.json → re-subscribe on change
```

**Event flow:**
```
log_event ─► is_swap(SWAP_PROGRAMS in logs) ─► fetch get_transaction(sig)
   │             │ no
   │             └─► drop
   ▼
core/tx_decoder.decode(tx, wallet)
   - extract pre/post token+SOL balances
   - infer side: buy if (sol_delta < 0 AND token_delta > 0)
   - return Decoded(wallet, mint, side, sol_amount, token_amount, ts, slot, sig)
   │
   ▼
emit WalletEvent → asyncio.Queue
   │
   ▼
core/cluster_detector — per-mint deque(maxlen=200) of (ts, wallet, cluster_id)
   evict entries older than 90s
   pattern checks (run on every insert):
     CLUSTER_HIT  : ≥3 distinct cluster_ids ∧ Σsol ≥ 5 SOL within 90s
     EARLY_FLOCK  : ≥5 wallets first_tx_age<24h, 0.05–1 SOL each, within 60s
     STAIR        : ≥4 monotonic-size buys in 120s, total ≥2 SOL, σ_stride>0.1
     PRE_INFLOW   : ≥3 buys 0.1–0.5 SOL within 30s
   on hit: emit PrePumpSignal(kind, mint, magnitude, wallets) → main queue
```

**Anti-bot guard** (drop signal if):
- all triggering txs share same instruction-byte hash (deterministic bundle), OR
- all txs share fee_payer (single entity faking diversity)

**Tx decoder cache:** dict `signature → Decoded` capped 5_000 entries (LRU). Avoids redundant `get_transaction` calls when same sig matched by multiple subscriptions.

---

## 5. RISK_HARDENING_LAYER

Augments v8 `core/risk.py` with execution-aware checks. New module: `core/risk_exec.py`.

**Pre-trade gates** (each entry):
```
1. balance_check   : SOL_balance ≥ size + reserve(0.05)
2. token_balance   : (sells only) tokens_available ≥ tokens_to_sell
3. quarantine      : mint not in quarantine list
4. fee_threshold   : (priority_fee + jito_tip + slip_loss_est) ≤ size · 0.015
5. latency_gate    : last-10 detect→submit p50 ≤ 1.5 s
6. mode_check      : mode_manager.current() ∈ {LIVE, DEGRADED_RPC}
```

**Stuck-tx watchdog** (background task, 10 s tick):
```
for sig in pending_submissions:
    age = now() − submit_ts
    if age > 60 and not confirmed:
        ghost_cancel(sig)         # send empty tx with same blockhash + higher fee
        log("stuck_cancel", sig)
    if age > 120 and not confirmed:
        abandon(sig)              # remove from pending; tx may still land harmlessly
        log("abandoned", sig)
```

**Post-trade slippage policing** (rolling 10 closes):
```
realized_slip = (expected_out − actual_out) / expected_out
avg_slip_10  = mean over last 10
if avg_slip_10 > 0.08 → global size · 0.5 for next 20 trades
```

**Sandwich detection** (rolling 1 h):
```
realized_slip > 3 × expected_slip → flag(sig)
3 flags in 1 h → enter "jito_only_mode" for 4 h (no RPC fallback for buys)
```

**Volatility halts:**
```
SOL 1m return ≤ −0.05 in last 60 s             → halt entries 1 min
SOL stdev_60m > 3 × median_60d                  → all sizes · 0.5
mint price drop > 50% in last 5 min             → blacklist mint 1 h (still allow exits)
```

**Auto-emergency kill-switch:**
```
3 critical errors in 60 s → write data/state/HALT, alert
critical = {balance_low, halted, sandwich_flagged}
```

**Capital protection floor:**
```
if equity < 0.80 · day_open_equity → halt 24 h
```

**State persisted:** `data/state/risk_exec_state.json`
```
{
  consecutive_not_confirmed, slip_history_10, sandwich_flags_1h,
  pending_submissions: {sig: {submit_ts, fee, mint}},
  quarantine: {mint: until_ts},
  blacklist:  {mint: until_ts},
  size_dampener: {factor, until_trade_n}
}
```

---

## 6. SIGNAL_EXECUTION_PIPELINE

End-to-end (single async loop in `runtime/loop.py`, with parallel position manager and watchdogs):

```
RPC stream → asyncio.Queue
    │
    ▼
[A] wallet_tracker_live           [B] token_scanner            [C] anomaly detector
    │ (WalletEvent)                     │ (NewTokenSignal)            │ (PrePumpSignal)
    └────────────────┬──────────────────┴──────────┬─────────────────┘
                     ▼                              ▼
              core.signal_engine.evaluate(event, ctx)
                  fingerprint → channels (W,T,P,C)
                  → Candidate(mint, confidence, pattern_id, source_kinds, wallets)
                     │
                     ▼
              core.risk.RiskGate.allow(candidate)        ──► reject + log
                     │ ok
                     ▼
              core.risk_exec.pre_trade_gates()           ──► reject + log
                     │ ok
                     ▼
              core.portfolio.size_for(candidate)         ──► reject if size < min
                     │
                     ▼
              core.rug_filter.run(mint)  (60s cache)     ──► reject + log
                     │ ok
                     ▼
              mode_manager.executor() → LiveExecutor or MockExecutor
                     │
                     ▼
              executor.submit_buy(mint, size, candidate)
                  Jupiter quote → (Raydium fallback if needed)
                  build → sign → submit (Jito bundle for buys)
                  confirm via dual-RPC race; retry-with-bump 1×
                     │
                     ▼
              ExecResult
                     │
            ┌────────┴────────┐
            ▼                 ▼
         on ok            on fail
            │                 │
            ▼                 ▼
     portfolio.open    quarantine + log
     logger.entry           │
     feedback.append_pending│
            │               (no further action)
            ▼
   portfolio.position_loop /3 s
            │
            ▼ on close
     executor.submit_sell  (RPC, Jupiter route)
            │
            ▼
     portfolio.close
     logger.exit
     feedback.write_close()
            │
            ▼
   data/research_in/live_outcomes.parquet (append, mode-tagged)
   data/state/pattern_state.json (EWMA per pattern)
   data/evolution_log.jsonl (append-only)
```

**Latency budget (target p50):**
```
ingest WS         ≤  50 ms
parse + decode    ≤ 300 ms
signal_engine     ≤  50 ms
risk + portfolio  ≤  20 ms
rug_filter        ≤   1 ms (cached) / ≤ 800 ms (cold)
quote + build     ≤ 400 ms
submit + confirm  ≤ 1500 ms
total detect→sub  ≤ 1500 ms p50, ≤ 3000 ms p95
```

---

## 7. FALLBACK_SIMULATION_MODE

`runtime/mode_manager.py` — explicit state machine. Single writer; readers consult via `mode_manager.current() → ModeState`.

```
States: LIVE → DEGRADED_RPC → DEGRADED_EXEC → PAPER

LIVE:
  all systems green; LiveExecutor active
  → DEGRADED_RPC if (all WS endpoints silent 60s)
                  OR (rpc_error_rate > 30% in 5min)
  → DEGRADED_EXEC if (3 consecutive not_confirmed)
                   OR (submit failures > 5 in 10 min)

DEGRADED_RPC:
  - keep tracking via remaining endpoint or HTTP polling fallback
  - halt new entries for 5 min; existing positions managed normally
  - if streams recover for 5 min sustained → LIVE
  - if 5-min escalation timer expires unrecovered → DEGRADED_EXEC

DEGRADED_EXEC:
  - new entries route to MockExecutor (paper)
  - real positions still close via LiveExecutor (RPC ok required)
  - if 30 min sustained stable → LIVE
  - if persists 60 min → PAPER

PAPER:
  - all execution via MockExecutor
  - ingest unchanged (real market signals captured)
  - manual flag clear required to resume → LIVE
```

**Trigger sources:**
```
RPC failure   → DEGRADED_RPC
exec failure  → DEGRADED_EXEC
manual file   data/state/mode.json {"mode":"PAPER"} → PAPER
auto-emergency: 3 critical errors / 60 s → write HALT (independent kill-switch)
```

**Logging consistency:**
- Single SQLite `mvp.db` schema for all trades regardless of mode.
- All trade rows tagged with `mode` column (`live | paper`).
- All telemetry to `telemetry.jsonl` includes `{mode, endpoint, latency_ms}`.
- `live_outcomes.parquet` carries `mode` column so v6 selector can filter (default: train only on `mode=live`).

**State file `data/state/mode.json`:**
```json
{
  "mode": "LIVE",
  "since_ts": 1714521600,
  "reason": "boot",
  "manual_override": false,
  "history": [{"from":"LIVE","to":"DEGRADED_RPC","ts":...,"reason":"all_silent_60s"}]
}
```

**Implementation:**
```python
class ModeManager:
    async def evaluate(self):
        # called every 30 s by background sweep + on every error event
        if self._halt_present(): return self.set("PAPER", "HALT_file")
        if self._all_streams_silent(60): return self.set("DEGRADED_RPC", "all_silent")
        if self._rpc_err_rate(5) > 0.30: return self.set("DEGRADED_RPC", "rpc_err")
        if self._consec_not_confirmed >= 3: return self.set("DEGRADED_EXEC", "exec_fail")
        if self._submit_fail_rate(10) > 5: return self.set("DEGRADED_EXEC", "submit_fail")
        if self._stable_for(min_seconds=...): return self.set("LIVE", "recovered")

    def executor(self) -> Executor:
        return self.live_executor if self.current().mode in ("LIVE","DEGRADED_RPC") \
               else self.mock_executor
```

**Exit-side override:** even in PAPER mode, if a real position is open and RPC is healthy, exits attempt LiveExecutor.submit_sell (config flag `paper_exits_via_live=True`). If that flag is False, exits also paper-trade and the real on-chain position remains open until manual intervention. Default: True (close real positions for safety).

---

## NEW MODULES

```
core/execution_jupiter.py     # quote + swap-tx
core/execution_raydium.py     # direct AMM fallback
core/tx_builder.py            # versioned tx, ALT, CB/CU
core/submit.py                # Jito bundle / RPC submit
core/confirm.py               # dual-poll signature confirm
core/route_selector.py        # Jupiter vs Raydium decision
core/risk_exec.py             # execution-aware risk gates + watchdog
core/cluster_detector.py      # 90s window, CLUSTER_HIT/EARLY_FLOCK/STAIR/PRE_INFLOW
core/tx_decoder.py            # parse pre/post balances → Decoded swap
runtime/mode_manager.py       # state machine + transitions
runtime/wallet_tracker_live.py# subscription mgr + decode pipeline
```

**Modified:**
- `core/execution.py::LiveExecutor` — wired to new modules
- `connectors/solana_rpc.py` — multi-endpoint pool + dedupe + health
- `runtime/loop.py` — adds tracker/scanner/anomaly tasks; consults `mode_manager.executor()`
- `runtime/feedback.py` — adds `mode` column to outputs

**State files (data/state/):**
- `rpc_health.json`, `risk_exec_state.json`, `mode.json`, `pending_submissions.json`

**Tests:**
- `test_jupiter_swap.py` (mocked HTTP)
- `test_raydium_fallback.py`
- `test_tx_builder.py`
- `test_submit.py` (mock RPC)
- `test_confirm.py` (race semantics)
- `test_mode_manager.py` (state transitions)
- `test_risk_exec.py` (each gate fires + clears)
- `test_cluster_detector.py`
- `test_pipeline_integration.py` (event → quoted → mock-submitted → logged)
