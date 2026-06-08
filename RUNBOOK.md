# solalpha Runbook

Operational procedures for incident response and recovery.

## Status check

```bash
solalpha status                             # last persisted health snapshot
curl -s http://127.0.0.1:9464/health        # if running, live snapshot
curl -s http://127.0.0.1:9464/status        # full status report
curl -s http://127.0.0.1:9464/metrics       # Prometheus metrics
```

A healthy snapshot has `overall: ok`, `mode: LIVE` (or `PAPER` if intentional), and `kill_switch_armed: false`.

## Kill switch

Stops all new orders, transitions mode to `HALT`. In-flight orders are allowed to confirm.

```bash
solalpha kill arm --reason "manual halt"
solalpha kill disarm                         # only after investigation
```

Or out-of-band (works even if the CLI is unavailable):

```bash
touch ./data/.kill                           # arm by file existence
rm ./data/.kill && solalpha kill disarm      # remove file + clear persisted state
```

The kill switch state is persisted in sqlite (`kill_switch` table) and restored on restart. The file probe is polled every 1 s.

## Stuck transaction

A transaction submitted but never confirmed within 30 s.

1. Check `stuck_signatures` table:

   ```bash
   sqlite3 data/solalpha.db "select * from stuck_signatures order by created_at desc limit 20;"
   ```

2. The `StuckTxResolver` worker polls hourly for up to 24 h. The position state for the parent order is `unknown`; risk engine treats as full exposure.

3. Manual reconciliation:

   ```bash
   solalpha recover --reconcile-stuck
   ```

   This calls `getSignatureStatuses` for each stuck signature on every healthy RPC and finalizes.

## RPC failover

Mode manager flips to `DEGRADED_RPC` when fewer than 2 endpoints are healthy.

- Check pool state: `curl -s http://127.0.0.1:9464/status | jq .components.rpc_pool`
- Endpoint scores live in memory; recent quarantines logged with reason.
- Add a new endpoint at runtime: update `rpc.urls` in `config/<profile>.yaml` (or export an updated `SOLALPHA_RPC_URLS`), then run `solalpha reload-rpc`. The CLI writes the new endpoint list to `data/.reload-rpc`, and the running process reconciles its pool within ~2 s (in-flight calls keep their existing endpoint). On POSIX, sending `SIGHUP` to the running process triggers the same reload via the app's own config re-load.
- If all RPCs are down for >2 minutes, mode auto-falls to `PAPER`.

## Mode flapping

If the system oscillates between LIVE ↔ DEGRADED, hysteresis windows are too tight or upstream is genuinely unstable. Increase:

```yaml
# config/<profile>.yaml
mode_manager:
  hysteresis_live_to_degraded_rpc_s: 30.0
  hysteresis_degraded_rpc_to_live_s: 90.0
```

`mode_transitions` table records every transition with reason and timestamp.

## Daily loss limit hit

Mode auto-transitions to `HALT` and persists. Trading resumes at next UTC midnight unless operator intervenes earlier:

```bash
solalpha kill disarm                         # operator override (records actor)
```

Investigate: `solalpha report --day=YYYY-MM-DD` or `select * from fills where date(block_time)='YYYY-MM-DD';`

## Loss streak hit

5 consecutive losing closes → `HALT`. Same recovery as daily-loss-limit. Review last losers:

```bash
sqlite3 data/solalpha.db "select position_id, mint, opened_at, closed_at, realized_pnl_usd from positions where state='closed' order by closed_at desc limit 10;"
```

## Crash recovery

1. Process exits unexpectedly → on next start, `recovery.recover()` runs automatically:
   - Loads latest snapshot from `data/snapshots/`
   - Replays journaled events since snapshot timestamp
   - Reconciles open positions by querying SPL balances
   - Logs a `RecoveryReport`
   - Resumes in PAPER until operator promotes

2. If snapshot is corrupt, the system falls back to the second-latest and alerts.

3. Manual recovery:

   ```bash
   solalpha recover --snapshot=data/snapshots/2026-05-02T12-00-00.snap
   ```

## Corrupted SQLite

```bash
sqlite3 data/solalpha.db "PRAGMA integrity_check;"
```

If corruption confirmed:

1. Stop the process.
2. `cp data/solalpha.db data/solalpha.db.broken`
3. `sqlite3 data/solalpha.db.broken ".dump" | sqlite3 data/solalpha.db.recovered`
4. Replace and restart with `solalpha recover --reconcile-positions`.

## Switching paper → live

> **First time?** Follow the supervised drill in
> [`docs/drills/paper-to-live.md`](docs/drills/paper-to-live.md) end-to-end
> before relying on this short recurring procedure.

Pre-flight:

1. In the shell that will launch `solalpha`, export:
   `SOLALPHA_LIVE_TRADING=1`, `SOLALPHA_KEYPAIR_PATH=/secure/path/keypair.json`,
   `SOLALPHA_RPC_URLS=<comma-separated mainnet endpoints>`. (The app reads env
   vars directly; no `.env` auto-loading.)
2. Keypair file: `chmod 600` on POSIX (Windows: ACL the file to your user
   only). Never commit; `.env*` and `*.json` keypairs are gitignored.
3. `solalpha status` → `overall: ok`, ≥2 RPCs healthy, `kill_switch_armed: false`.
4. `SOLALPHA_PROFILE=live solalpha live` (refuses without `SOLALPHA_LIVE_TRADING=1`).
5. Watch logs for at least 5 min; mode promotes PAPER → LIVE only after
   `mode_manager.paper_to_live_health_s` of sustained health (default 5 min).
   First-trade `risk.per_trade_usd_cap` should be conservative for the drill.

## Switching live → paper (graceful)

```bash
solalpha mode set PAPER --reason "investigating signal X"   # pin PAPER (no HALT)
solalpha mode show                                          # inspect the override
solalpha mode clear                                         # release back to the health gate
```

Only PAPER may be operator-pinned (LIVE/DEGRADED_* are health-driven; use
`solalpha kill arm` to force HALT). A running instance picks up the override
on its next mode-manager tick (~1 s).

Or stop the process; default-on-restart is PAPER.

## Release procedure

1. Update `CHANGELOG.md` with a new dated section.
2. `git tag vX.Y.Z && git push --tags`
3. `release.yml` runs lint+tests → build wheel+sdist → build+push Docker → publish to PyPI (OIDC) → create GitHub Release.

If the workflow fails after the Docker push, you can re-run only the publish steps from the Actions UI (idempotent). Wheel/sdist publish via OIDC is also idempotent (PyPI rejects duplicate uploads cleanly).
