# solalpha

Production-grade Solana alpha trading system.

- **Default mode:** PAPER. Live execution requires `SOLALPHA_LIVE_TRADING=1`.
- **Stack:** Python 3.11+, anyio, pydantic v2, httpx, websockets, solders + solana-py, aiosqlite, pyarrow, structlog, Prometheus.
- **Layout:** `foundation` → `data` → `research` → `signal` → `execution` → `observability` → `runtime`.
- **Hard risk controls:** slippage caps, daily loss limit, loss-streak halts, vol halts, quarantine, kill switch.

## Quickstart

```bash
# Install
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env: at minimum set SOLALPHA_RPC_URLS to two healthy endpoints.

# Paper mode (default, safe)
solalpha paper

# Status / health
solalpha status

# Live (refuses without env flag)
SOLALPHA_LIVE_TRADING=1 solalpha live
```

## CLI

| Command | Purpose |
|---|---|
| `solalpha version` | Print package version |
| `solalpha status` | Print last health snapshot as JSON |
| `solalpha paper` | Run in PAPER mode |
| `solalpha live` | Run in LIVE mode (requires `SOLALPHA_LIVE_TRADING=1`) |
| `solalpha research backfill --since=YYYY-MM-DD` | Pull historical events |
| `solalpha research replay <session>` | Deterministic replay of a recorded session |
| `solalpha research walkforward` | Out-of-sample evaluation |
| `solalpha snapshot` | Dump full state to a snapshot file |
| `solalpha recover` | Recover from latest snapshot + journal |
| `solalpha kill arm --reason=...` | Arm the kill switch (rejects all new orders) |
| `solalpha kill disarm` | Disarm the kill switch |

## Configuration

Three layers, last wins:
1. `config/default.yaml` — conservative defaults
2. `config/<profile>.yaml` — selected via `SOLALPHA_PROFILE` (`paper`, `live`, `research`)
3. Env vars prefixed `SOLALPHA_` (double underscore for nesting: `SOLALPHA_RISK__PER_TRADE_USD_CAP=100`)

The hard risk ceilings (`hard_slippage_ceiling_bps`, `max_open_positions_ceiling`, `max_price_impact_ceiling_pct`) **cannot** be loosened by config.

## Modes

`PAPER` → `LIVE` ↔ `DEGRADED_RPC` ↔ `DEGRADED_EXEC` → `PAPER` → `HALT`

- **PAPER** — sim executor only. Default at startup.
- **LIVE** — full pipeline. Requires env flag + healthy stack.
- **DEGRADED_RPC** — fewer than 2 healthy RPCs. Confidence threshold +0.15, size ×0.5.
- **DEGRADED_EXEC** — Jupiter unhealthy. Sells/exits only.
- **HALT** — kill switch armed, daily loss limit hit, or loss streak hit. Operator clears.

See [`RUNBOOK.md`](RUNBOOK.md) for incident response.

## Development

```bash
ruff format . && ruff check .
mypy --strict src/solalpha
pytest -m unit
pytest -m integration
pytest tests/replay
```

`tests/live` is skipped by default; pass `--run-live` and `SOLALPHA_TEST_LIVE=1` to enable.

## Release

Push a `v*` tag; `.github/workflows/release.yml` re-runs tests, builds wheel + sdist, builds and pushes the Docker image to GHCR, publishes to PyPI via OIDC, and drafts a GitHub Release from the matching `CHANGELOG.md` section.

## License

[MIT](LICENSE).
