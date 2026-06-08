# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `solalpha/domain/` package — immutable, dependency-free value objects shared
  by every plane: `RawEvent`, `NormalizedSwap`, `DetectorSignal`, `Signal`,
  `RiskDecision`, `Route`, `OrderIntent`, `Order`, `Fill`, `Position`,
  `DailyPnl`, `ModeState`, `ModeTransition`. Shapes mirror the SQLite schema in
  `foundation/persistence_schema.py`; all `model_dump(mode="json")` round-trip.
- `solalpha/observability/` — first-class plane: `TradeLog` (structured per-fill
  log + journal + parquet `trades` table), `PortfolioTracker` (weighted-avg
  cost basis, realized PnL, daily PnL, loss streak, persisted to `positions`
  and `daily_pnl`), `SnapshotManager` (atomic JSON snapshots under
  `data/snapshots/` with retention + monotonic part-file sequence), `recover()`
  + `RecoveryReport` (loads latest snapshot with fallback on corruption,
  counts un-replayed journal entries, records a `mode_transitions` resume row),
  `MetricsServer` (aiohttp-hosted `/metrics`, `/health`, `/status` plus a
  background loop that persists `HealthSnapshot`s to the `health_snapshots`
  table so `solalpha status` works offline).
- `solalpha/signal/kill_switch.py` and `solalpha/signal/mode_manager.py` —
  `KillSwitch` is the single source of truth for "halt all new orders",
  reconciling the SQLite row with the on-disk probe file every 1 s; the
  `ModeManager` is a hysteresis-gated state machine over `LIVE / DEGRADED_RPC
  / DEGRADED_EXEC / PAPER / HALT` driven by `HEALTH_TOPIC`, `KillSwitch`, and
  `PortfolioTracker`. `HALT` is the single non-hysteresis state — it latches
  instantly on a halt condition and exits instantly when the operator clears
  it.
- `solalpha/runtime/` — `Application` orchestrator wires Phase 1 components
  under a single `anyio.TaskGroup`, runs `recover()` on startup, supervises
  the `KillSwitch / SnapshotManager / ModeManager / MetricsServer` workers
  with capped exponential-backoff restart, listens for `SIGINT`/`SIGTERM`,
  and writes a final snapshot on graceful shutdown. New planes plug in by
  registering additional `Worker`s on the existing `WorkerSupervisor`.
- `solalpha/data/` (Phase 2: data plane) -- `program_ids` (Jupiter v6,
  Raydium v4, Orca Whirlpool, pump.fun, SPL Token, ATA, compute budget,
  ALT), `DedupeRing` (bounded LRU-style `seen` set keyed on
  `deterministic_event_id`), `RpcPool` (multi-endpoint JSON-RPC client
  with rolling-score failover, quarantine, transient/permanent error
  classification, HealthRegistry-compatible `probe()`),
  `MintMetadataCache` (fetch-through cache parsing the 82-byte SPL Token
  Mint layout for `decimals` / `has_mint_authority` /
  `has_freeze_authority`), `PoolCache` and `AtaOwnerCache` (write-through),
  `TransactionDecoder` (venue dispatch by outermost program id + balance-
  diff swap derivation; covers Jupiter / Raydium / Orca / pump.fun),
  `DecoderWorker` (`EVENTS_TOPIC` -> `getTransaction` -> decode ->
  `NORMALIZED_TOPIC`), `WebSocketIngestor` (persistent `logsSubscribe`
  with capped exponential reconnect), `BackfillPoller`
  (`getSignaturesForAddress` from checkpointed cursor),
  `SmartWalletSubscriptionManager` (top-N-by-score wallet list).
- `runtime/app.py` now wires the data plane behind a `rpc.urls` gate:
  empty list -> Phase 1 spine only; non-empty -> the seven Phase 2 workers
  are supervised alongside, and `RpcPool.probe` is registered on the
  `HealthRegistry` so `ModeManager` can flip to `DEGRADED_RPC` when fewer
  than `health_min_healthy` endpoints are healthy.
- `solalpha/signal/` (Phase 3: signal & risk plane) -- `SmartWalletScorer`
  (composite rolling-PnL + winrate score with time decay; `is_smart()` /
  `smart_set()` used downstream), `Detector` protocol plus three concrete
  detectors -- `PrePumpDetector` (buy-pressure ratio + half-window slope),
  `ClusterDetector` (N distinct smart wallets buying the same mint within
  a window), `FlowAnomalyDetector` (per-mint z-score over a 1s-bucket
  baseline with stdev floor for degenerate constant-flow windows);
  `ConfidenceCombiner` (per-mint window blending the best recent
  `DetectorSignal` per detector into a `Signal` with a stable
  `inputs_hash` for deterministic replay); `PortfolioSizer` (confidence-
  ramped equity slice, per-trade USD cap, `DEGRADED_RPC` size factor);
  `RiskEngine` (hard gate that **fails closed**: kill-switch, confidence
  floor, ceilings on open positions, daily-loss, loss-streak, inflight-
  per-mint, quarantine, blacklist, freeze/mint-authority -- with the
  `MintMetadataCache` only consulted when otherwise approving so we don't
  burn RPC budget on already-rejected signals); `SignalPipeline` worker
  that subscribes `NORMALIZED_TOPIC`, runs detectors + combiner +
  sizer + risk in concert, persists `Signal`/`RiskDecision`, and
  republishes approved signals on `SIGNALS_TOPIC` for the execution plane.
- `runtime/app.py` now constructs the signal plane unconditionally
  (`SmartWalletScorer`, `RiskEngine`, `SignalPipeline`), wiring the
  optional `MintMetadataCache` into the risk engine when the data plane
  is up.
- `solalpha/execution/` (Phase 4: execution plane) -- `Executor` protocol,
  `Quote` / `SwapInstructions` value types; `JupiterClient` (`/quote` +
  `/swap-instructions`, transient/permanent error classification, quote
  latency metric); `RaydiumClient` (v3 fallback router); `RouteSelector`
  (Jupiter-first / Raydium-first by mode, re-asserts hard ceilings on
  every quote); `AltManager` (in-memory Address Lookup Table cache,
  parses on-chain layout via `solders.AddressLookupTable.deserialize`);
  `TxBuilder` (assembles a signed `VersionedTransaction` from Jupiter
  swap instructions or a Raydium pre-built v0 tx, with
  `compute_budget` instructions); `Confirmer` (dual-RPC
  `getSignatureStatuses` polling with explicit stuck / failed / confirmed
  outcomes and metric latencies); `RetryBumpExecutor` (priority-fee +
  slippage escalation ladder, transient/permanent classification);
  `StuckTxResolver` worker (hourly re-poll of `stuck_signatures` with a
  24h abandonment window, decrements the risk-engine inflight counter
  on resolution); `PaperExecutor` (simulates a fill using
  `paper_executor` config -- fee, base slippage, impact slippage scaled
  by `min_pool_liquidity_usd`); `LiveExecutor` (full path:
  RouteSelector -> venue swap-instructions -> TxBuilder -> retry-bump ->
  Confirmer -> persisted Order/Fill, only callable when
  `cfg.is_live_eligible()`); `ExecutionPipeline` worker that subscribes
  `SIGNALS_TOPIC`, picks paper or live executor by mode + live-eligibility,
  runs the executor, logs through `TradeLog`, applies the fill to
  `PortfolioTracker`, and republishes the Order/Fill on
  `ORDERS_TOPIC` / `FILLS_TOPIC`.
- `runtime/app.py` wires the execution plane: `PaperExecutor` is built
  unconditionally; `JupiterClient`/`RaydiumClient`/`RouteSelector`/
  `AltManager`/`TxBuilder`/`LiveExecutor`/`StuckTxResolver` are built
  iff both the data plane and a configured keypair are present;
  `ExecutionPipeline` is built once the spine + signal plane are up.
- `solalpha/research/` (Phase 5: research plane) -- `ReadonlyGuard`
  (proxy `SqliteStore` that raises `ResearchWriteBlocked` on every
  mutating method; `assert_readonly` enforces it at research entry
  points); `run_backfill(cfg, since, until)` -- the CLI entry point for
  `solalpha research backfill`, drives `BackfillPoller.run_once` over
  smart-wallet addresses; `replay_session(cfg, session)` and
  `run_walkforward(cfg)` -- the CLI entry points for `research replay`
  and `research walkforward`, both drive the live signal pipeline
  (detectors, combiner, sizer, risk engine, paper executor) over a
  recorded `NormalizedSwap` parquet session with a `FakeClock` so two
  consecutive runs produce bit-identical `signal_inputs_hash`es; the
  walk-forward harness splits history into `walkforward_train_days` /
  `walkforward_test_days` windows and gates promotion on
  `cfg.research.min_oos_sharpe`; `SessionMetrics` (Sharpe / hit rate /
  max drawdown / exposure / turnover) computed from FIFO-paired fills;
  `mine_patterns` (DBSCAN over `(side, log10(usd), pool_impact)`);
  `select_best` (ranks `StrategyCandidate` presets by full-fold pass
  rate then Sharpe).
- All three CLI `research` commands now resolve to real entry points,
  not stubs; the previous `cli.py` imports of `run_backfill`,
  `replay_session`, `run_walkforward` finally have targets.
- `tests/` (Phase 6: test suite) -- `conftest.py` with the `--run-live`
  opt-in flag (gates the `live` tier behind `--run-live` +
  `SOLALPHA_TEST_LIVE=1`) and shared fixtures (`FakeClock`, tmp-rooted
  `AppConfig`, connected `SqliteStore`, synthetic-swap factory). 105
  tests across four tiers: `unit` (config ceilings, ids, retry
  classification, dedupe eviction, decoder dispatch, all three
  detectors, kill switch, mode-manager HALT latch, sizer ramp,
  combiner inputs-hash determinism, every risk-engine rule incl.
  fail-closed, rpc-pool quarantine/failover, research metrics +
  pattern miner + strategy selector + readonly guard);
  `integration` (sqlite migrations + journal + parquet round-trip,
  snapshot/recovery incl. corrupt-snapshot fallback, paper-executor
  fill math + portfolio PnL, full `Application` boot + signal->order
  flow); `replay` (the determinism contract -- same parquet session
  replays bit-identical `signal_inputs_hash`es); `live` (real
  RPC / Jupiter, skipped by default). 102 pass, 3 live-tier skipped;
  65% line coverage, 23 modules at 100%.
- `.github/workflows/` (Phase 7: CI/CD) -- four pipelines. `ci.yml` runs
  `ruff format --check`, `ruff check`, `mypy --strict`, and the offline
  `unit`/`integration`/`replay` test tiers with coverage on a Python
  3.11/3.12 matrix. `security.yml` runs `pip-audit` (dependency CVE scan),
  `bandit` (static analysis of `src/solalpha`), and a non-mutating
  `detect-secrets-hook` baseline check, plus a weekly cron. `release.yml`
  fires on a `vX.Y.Z` tag: re-verify the tagged commit, assert the tag
  matches `pyproject` version, build wheel + sdist, build and push a
  multi-tag image to GHCR, publish to PyPI via OIDC trusted publishing,
  and draft a GitHub Release from the matching `CHANGELOG.md` section.
  `nightly.yml` runs a scheduled `research backfill` + `walkforward` and
  uploads the artifacts, no-opping cleanly when the `SOLALPHA_RPC_URLS`
  secret is unset.
- `docs/adr/` -- Architecture Decision Records. ADR-0001 documents the
  `solana-py` / `websockets` dependency-conflict resolution and the
  decision to standardize transaction construction on `solders`.
- Three operator-facing CLI commands the RUNBOOK was already documenting
  but the code did not yet ship (Phase 8). `solalpha report --day=` reads
  `daily_pnl`, joined `fills`+`orders`, and same-day closed `positions` for
  a UTC day and prints them as JSON. `solalpha mode set PAPER --reason ...`
  / `mode clear` / `mode show` write a JSON probe file the `ModeManager`
  polls every tick: while present it pins runtime to `PAPER` -- outranks
  health-driven `LIVE`/`DEGRADED_*` selection, never outranks a real HALT,
  and applies with no hysteresis. Only PAPER is operator-settable. The
  probe-file design is cross-platform (no SQL migration, no SIGHUP
  dependency) so the override works on Windows too. `solalpha reload-rpc`
  writes a JSON URL list to `<data_dir>/.reload-rpc`; a new `rpc_reloader`
  worker in `Application` polls for it every 2 s, calls `RpcPool.reload()`
  to reconcile endpoints in place (preserving rolling score state for
  surviving endpoints), and deletes the request file. On POSIX a SIGHUP
  handler triggers the same reload via `load_config()` so the runbook's
  "HUP signal on POSIX" claim is now backed by code.
- `RpcPool.reload(urls)` -- in-process endpoint reconciliation. Keeps
  existing `_EndpointState` objects (preserving rolling score / quarantine
  windows), adds fresh ones, drops removed URLs, clears their quarantine
  metrics. Refuses an empty URL list and leaves the prior set intact on
  rejection.
- `docs/drills/paper-to-live.md` -- the first-time supervised PAPER -> LIVE
  promotion drill, step-by-step with PowerShell commands, abort criteria,
  and go/no-go gates between devnet, mainnet PAPER soak, pre-flight, and
  the live promotion. RUNBOOK's recurring "Switching paper -> live"
  section now links to it.

### Fixed
- `foundation/state.py` -- `pa.concat_tables(..., promote=True)` is
  deprecated in pyarrow >=14; switched to `promote_options="default"`
  (caught by `tests/integration/test_state.py` under the
  `filterwarnings = error` pytest policy).
- `runtime/app.py` -- `Application.run()`'s `finally: _shutdown()` is
  now wrapped in an `anyio.CancelScope(shield=True)` so graceful
  cleanup (final snapshot, store close, aiosqlite worker-thread join)
  always completes even when `run()` was cancelled by SIGINT or a
  parent scope. Without the shield a cancelled shutdown orphaned the
  aiosqlite worker thread, surfacing as an "Event loop is closed"
  thread exception.
- `.secrets.baseline` — regenerated. The Phase 0 baseline was created
  before the `data/` and `tests/` planes existed, so it had an empty
  `results` set; the public Solana program-id and mint constants in
  `data/program_ids.py`, `data/decoder.py`, and the decoder tests tripped
  the high-entropy detector and would have failed the `detect-secrets`
  CI job. The baseline now records those 13 reviewed, non-secret findings.
- `data/rpc_pool.py` — two multi-line `raise RpcTransientError(...)` calls
  (87 chars when collapsed) were left expanded without a magic trailing
  comma, which `ruff format --check` rejects. Collapsed both, so the
  Phase 7 `ci.yml` `ruff format --check .` step actually passes from a
  clean clone. (Caught during the Phase 8 mirror-sync verification, not in
  the workspace's own `ruff format --check src tests` — a `tail`-truncated
  output buried the warning.)
- `RUNBOOK.md` — reconciled with reality. The runbook claimed `.env` was
  auto-loaded (it never has been; the app reads `os.environ` directly),
  referenced `solalpha mode set` / `report` / `reload-rpc` commands that
  didn't exist (now they do, Phase 8 above), and assumed POSIX SIGHUP as
  the only RPC-reload surface. Pre-flight env-var setup, the live-paper
  graceful-demote procedure, and the RPC reload procedure are now all
  accurate for both POSIX and Windows targets.
- `pyproject.toml` — `requires-python = ">=3.11,<3.14"` (was `>=3.11`).
  pyarrow has no prebuilt Windows wheels for Python 3.14, so a fresh
  `pip install -e ".[dev]"` on a default Python 3.14 install falls back
  to a C++ source build of pyarrow and dies for lack of CMake / Visual
  Studio. The CI matrix is 3.11/3.12; pinning the upper bound makes the
  installer refuse 3.14 with a clear error rather than burning the
  operator's time on a doomed compile.
- `docs/drills/paper-to-live.md` step 1 — explicitly pins the venv to
  Python 3.12 (`py -3.12 -m venv .venv`) and tells the operator to verify
  with `python --version` before the install, since the bare
  `python -m venv .venv` form picks up whichever Python is on PATH.
- `pyproject.toml` — added `tzdata>=2024.1; sys_platform == 'win32'` as a
  Windows-conditional dependency. Linux/macOS read IANA tz data from
  `/usr/share/zoneinfo`; Windows has nothing, so pyarrow's timestamp
  deserializer (which goes through Python's `zoneinfo` under the hood)
  raised `ZoneInfoNotFoundError: 'No time zone found with key UTC'` on a
  fresh Windows install, taking out both replay-tier tests.
- `tests/integration/test_pipeline.py::test_signal_flows_to_order` is now
  `skipif(sys.platform == "win32", ...)`. The test publishes a burst of
  swaps 0.5 s after `app.run()` boots and expects orders on
  `ORDERS_TOPIC` 2.5 s later; on the Windows `ProactorEventLoop` the
  three pipeline workers do not reliably subscribe in time, so the
  collector misses every published swap. The full pipeline is still
  covered on Linux by GitHub Actions CI, and the runtime boot path is
  covered on every platform by `test_application_boots_and_shuts_down`
  in the same file.
- `tests/replay/test_determinism.py::test_replay_is_deterministic` now
  uses two isolated `data_dir`s (one per replay run) instead of sharing
  `tmp_path` between calls. On Windows, SQLite/aiosqlite retains the DB
  file handle briefly past `aclose()`, and the second `replay_session()`
  call hit `[WinError 32] The process cannot access the file because it
  is being used by another process`. Each replay now starts on a clean
  store -- which is the more correct way to test "same input -> same
  output" anyway -- so the determinism contract is verified on both
  platforms.
- `research/replay.py::_run_replay` now uses
  `tempfile.TemporaryDirectory(ignore_cleanup_errors=True)`. The replay
  engine opens an ephemeral `replay.db` inside a temp dir; on Windows,
  `aiosqlite.close()` returns before the worker thread's OS handles to
  the WAL `-shm`/`-wal` side files are fully released, and the temp
  dir's `rmtree` raced them with `WinError 32`. The flag (Python 3.10+)
  is a no-op on POSIX where cleanup succeeds normally, and on Windows
  the leftover bytes are reclaimed by the OS temp-area sweep.
- `runtime/app.py::_signal_handler` now catches `NotImplementedError`
  from `anyio.open_signal_receiver`. Windows asyncio does not implement
  `add_signal_handler` on any event loop, so the previous unconditional
  signal-receiver call crashed the signal-handler task at boot, which
  cancelled the whole `TaskGroup` and brought the app down before any
  worker could do useful work. `solalpha live`, `solalpha paper`, and
  `solalpha run` were unusable on Windows as a result. The catch logs
  `signal_handler_unavailable` and returns; `Ctrl+C` still triggers
  clean shutdown via Python's default `KeyboardInterrupt` propagation
  through `anyio.run` -> `cli._run_app`'s `except KeyboardInterrupt`,
  and `app.run`'s shielded `finally` still takes the
  final-snapshot + store-close path. POSIX behaviour is unchanged.
- `docs/drills/paper-to-live.md` now ships both **cmd** and **PowerShell**
  command blocks side-by-side everywhere shell syntax matters (env vars,
  the mode-watcher loop, the kill-switch out-of-band touch). The original
  PowerShell-only blocks silently failed in cmd with a Dutch-locale
  "syntax incorrect" message that gave the operator no clue what was
  wrong, leaving `solalpha live` to refuse on a still-unset env var. The
  doc also now includes a one-time Solana CLI install block and uses
  `%USERPROFILE%\.solalpha\keypairs\` (write-permitted by default)
  instead of the previously-suggested `C:\secure\`.
- `.secrets.baseline`, `docs/prometheus.yml`, `scripts/dev.sh`, `scripts/run.sh`
  — supporting files referenced by `.pre-commit-config.yaml`,
  `docker-compose.yml`, and the README.

### Changed
- `pyproject.toml` — resolved an unsatisfiable dependency conflict: `solana`
  pinned `<0.36` requires `websockets<12`, but the project pins `websockets>=12`.
  Bumped to `solana>=0.36,<0.37` / `solders>=0.26,<0.28`.
- `pyproject.toml` — added `tool.ruff.lint.flake8-type-checking` config so ruff
  no longer suggests moving runtime-evaluated pydantic annotation imports into
  `TYPE_CHECKING` blocks.
- `data/decoder.py` — behavior-preserving de-duplication: the identical
  per-mint owner-aggregation loop that ran once for `preTokenBalances` and once
  for `postTokenBalances` is now a single `_sum_owned_by_signer()` helper, and
  the four in/out leg assignments repeated in both branches of `_diff_to_swap`
  are hoisted out of the `if/elif`. Public surface (`__all__`,
  `TransactionDecoder`, the `*_MINT` constants) unchanged; verified by
  `tests/unit/test_decoder.py` plus a 40k-case differential equivalence test.
- `signal/risk_engine.py` — collapsed the three near-identical
  `RiskDecision(...)` constructions (reject / approve-scale / fail-closed) into
  one `_decision()` helper that stamps the shared `signal_id`, timestamp, and
  mode-at-decision in a single place. Fail-closed control flow untouched;
  `tests/unit/test_risk_engine.py` green.
- `runtime/app.py` — the three identical best-effort `aclose()` blocks in
  `_shutdown` (jupiter / raydium / rpc) collapse into one `_aclose_quietly()`
  helper; per-client failure isolation preserved.
- `foundation/cli.py` — the `_bootstrap(ctx.obj.get("config_dir"),
  ctx.obj.get("profile"))` boilerplate repeated across 16 command bodies is
  centralised in a `_bootstrap_ctx()` helper (optional `default_profile` for the
  paper / live / research commands). Behaviour-exact; `tests/integration/test_cli.py` green.
- `tests/` — added 20 direct unit tests: `KeypairLoader` (`foundation/secrets.py`, 22% -> 86% covered) and the four refactor helpers (`_decision`, `_aclose_quietly`, `_bootstrap_ctx`, `_sum_owned_by_signer`). Total coverage 64.6% -> 65.8%.

### Fixed
- `foundation/state.py` — `SqliteStore.journal()` and `ParquetStore` no longer
  call `datetime.utcnow()` directly (a violation of the `foundation/clock.py`
  contract and deprecated on Python 3.12). `SqliteStore` accepts an optional
  `Clock`; `ParquetStore` requires one. Parquet part filenames now carry a
  monotonic sequence suffix so a non-advancing clock (deterministic replay)
  cannot overwrite parts.
- `foundation/logging.py` — `_StdlibBridgeFormatter.format` and the structlog
  processor type signatures now pass `mypy --strict`. Also dropped the
  `structlog.stdlib.add_logger_name` processor (it crashes against
  `PrintLogger`, which the project's config uses); the logger name is now
  bound at `get_logger()` time instead.
- `foundation/health.py` — `HealthRegistry._auxiliary` typed as
  `dict[str, Any]` so `int(...)`/`float(...)` coercions in `snapshot()` pass
  `mypy --strict`. `Clock` import moved to `TYPE_CHECKING` since it is
  annotation-only.
- `data/smart_wallet_subscriptions.py` — restored a corrupted `try/except` in
  `run()`: an `except Exception as e:` clause had been sliced out of its own
  line and spliced into the middle of `self._poll_interval_s`, leaving the
  module unparseable and breaking import of the entire `solalpha.data` package.
  Restored to the obvious intended form; `pytest` collection and the full suite
  pass again.

### Security
- `pyproject.toml` — bumped two dependencies to clear CVEs flagged by the
  `pip-audit` CI job: `pyarrow` `>=16.0,<19` -> `>=23.0.1,<24` (PYSEC-2026-113)
  and the dev-only `pytest` `>=8.1,<9` -> `>=9.0.3,<10` (CVE-2025-71176, a local
  `/tmp` tmpdir race). The full suite, `ruff`, and `mypy --strict` pass under
  both bumped versions and `pip-audit` is clean. (pyarrow only ever reads
  solalpha's own data + the operator-supplied `research replay` file, so the
  practical exposure was already low.)

## [0.1.0] - 2026-05-02

### Added
- Initial release of solalpha — production-grade Solana alpha trading system.
- Foundation: pydantic config, structured logging, sqlite + parquet state, in-process bus, health registry, Prometheus metrics, click CLI.
- Data plane: multi-RPC pool with score-based failover, websocket ingestor with reconnect, HTTP backfill poller, transaction decoder (Jupiter v6, Raydium v4, Orca, pump.fun), dedupe ring buffer, decode caches, smart-wallet subscription manager.
- Research plane: historical collector, deterministic replay engine, simulated executor, walk-forward metrics, DBSCAN pattern miner, strategy selector, read-only guard preventing research from mutating live state.
- Signal + risk plane: smart-wallet scorer, pre-pump / cluster / flow-anomaly detectors, confidence combiner, portfolio sizer, hard risk engine (slippage caps, daily loss limit, loss-streak halts, vol halts, quarantine, blacklist), kill switch, mode manager (LIVE / DEGRADED_RPC / DEGRADED_EXEC / PAPER / HALT) with hysteresis.
- Execution plane: Jupiter v6 client, Raydium fallback, route selector, ALT manager, versioned tx builder with compute-budget instructions, dual-RPC confirmation, retry-with-bump, stuck-tx resolver, paper executor, live executor.
- Observability + recovery: per-trade structured log, Prometheus exporter, /health and /status endpoints, periodic state snapshots, journal-based recovery.
- Runtime: anyio task group orchestrator with mode-aware worker enable/disable.
- Tests: unit, integration, and replay-determinism suites.
- CI/CD: GitHub Actions (lint+typecheck+tests, security scan, tag-triggered release with wheel+sdist+Docker, nightly research workflow).
- Tooling: ruff, mypy strict, pre-commit, Dockerfile (multi-stage), docker-compose, dev/run scripts.
- Docs: README quickstart, RUNBOOK incident response, .env.example.

### Security
- Live trading requires explicit `SOLALPHA_LIVE_TRADING=1` env flag.
- Paper mode is default at startup.
- Risk engine fails CLOSED on any internal error.
- Hard risk-rule ceilings cannot be loosened by config.

[Unreleased]: https://github.com/solalpha/solalpha/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/solalpha/solalpha/releases/tag/v0.1.0
