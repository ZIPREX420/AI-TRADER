# Supervised PAPER → LIVE Drill (first-time go-live)

This is the **first-time** promotion procedure. Once you have done it once
end-to-end, the recurring "Switching paper → live" section in `RUNBOOK.md`
is enough.

The procedure is split into stages and **must not be rushed**. Every stage
has a clear go/no-go gate. Commands are shown for both **PowerShell** and
**cmd** (Windows) where they differ, plus the POSIX equivalent where
relevant. **Check your prompt before running**: `PS C:\...>` is PowerShell,
`C:\...>` (no `PS` prefix) is cmd. They use different syntax for env vars
and the two are NOT interchangeable.

| Shell      | Set env var                       | Activate venv                       |
|------------|-----------------------------------|-------------------------------------|
| PowerShell | `$env:NAME = "value"`             | `.\.venv\Scripts\Activate.ps1`      |
| cmd        | `set NAME=value`                  | `.venv\Scripts\activate.bat`        |
| POSIX      | `export NAME=value`               | `source .venv/bin/activate`         |

## 0. Prerequisites

Before you begin, have these in hand:

| Item | What | How |
|------|------|-----|
| Solana CLI | `solana` + `solana-keygen` on PATH | One-time install (see below). |
| RPC endpoints | Two healthy mainnet HTTPS endpoints | A paid provider (Helius / QuickNode / Triton) **plus** a fallback. The public `api.mainnet-beta.solana.com` alone is too rate-limited. |
| Keypair | A dedicated hot wallet, **never your main wallet** | `solana-keygen new -o %USERPROFILE%\.solalpha\keypairs\mainnet.json --no-bip39-passphrase`. Restrict the file ACL to your user. |
| Funding | Small SOL balance | 0.1 – 0.3 SOL for the first drill. Treat it as money you can lose entirely. |
| First-trade cap | Conservative per-trade USD cap | `risk.per_trade_usd_cap: 10` for the first drill (override `config/live.yaml`'s default of 250). |
| Time | At least 2 hours of attention | Babysit the promotion; do not walk away. |

### Installing the Solana CLI (Windows, one-time)

In a cmd window:

```cmd
mkdir C:\solana-install-tmp
curl https://release.anza.xyz/stable/solana-install-init-x86_64-pc-windows-msvc.exe ^
  --output C:\solana-install-tmp\solana-install-init.exe
C:\solana-install-tmp\solana-install-init.exe stable
```

Add `%USERPROFILE%\.local\share\solana\install\active_release\bin` to your
user `PATH` (System Properties → Environment Variables → User PATH → New).
Close and reopen any shell, then verify: `solana --version`.

## 1. Install + baseline checks

The CI matrix is Python 3.11 / 3.12; do not use 3.13 or 3.14. Newer Pythons
do not yet have prebuilt `pyarrow` wheels on Windows and pip will fall back
to a C++ source build that needs CMake + Visual Studio.

```powershell
py -0                                      # list installed Pythons; need 3.11 or 3.12
# If neither is present:
#   winget install Python.Python.3.12
git pull
py -3.12 -m venv .venv                     # pin the venv interpreter explicitly
.\.venv\Scripts\Activate.ps1               # cmd: .venv\Scripts\activate.bat
                                            # POSIX: source .venv/bin/activate
python --version                           # must say "Python 3.12.x"
python -m pip install --upgrade pip
pip install -e ".[dev]"
solalpha --version
```

Run the offline test suite as a sanity check that the install is healthy:

```powershell
pytest -m "unit or integration or replay"
```

**Go gate:** All tests pass.

## 2. Devnet dry-run (no money at risk)

The goal is to exercise the full transaction-build path against the Solana
devnet without ever paying mainnet fees. Devnet has free airdrops.

Create the keypair and fund it (cmd or PowerShell -- the `solana` CLI is
shell-agnostic):

```cmd
mkdir %USERPROFILE%\.solalpha\keypairs 2>nul
solana-keygen new -o %USERPROFILE%\.solalpha\keypairs\devnet.json --no-bip39-passphrase
solana airdrop 2 --keypair %USERPROFILE%\.solalpha\keypairs\devnet.json --url devnet
```

Then set env vars and launch -- pick the block that matches your shell:

**cmd:**
```cmd
set SOLALPHA_PROFILE=live
set SOLALPHA_LIVE_TRADING=1
set SOLALPHA_KEYPAIR_PATH=%USERPROFILE%\.solalpha\keypairs\devnet.json
set SOLALPHA_RPC_URLS=https://api.devnet.solana.com
set SOLALPHA_DRY_RUN=1
set SOLALPHA_                       :: sanity-check the vars stuck
solalpha live --dry-run
```

**PowerShell:**
```powershell
$env:SOLALPHA_PROFILE      = "live"
$env:SOLALPHA_LIVE_TRADING = "1"
$env:SOLALPHA_KEYPAIR_PATH = "$env:USERPROFILE\.solalpha\keypairs\devnet.json"
$env:SOLALPHA_RPC_URLS     = "https://api.devnet.solana.com"
$env:SOLALPHA_DRY_RUN      = "1"            # builds + signs but blocks at submit
solalpha live --dry-run
```

Let it run for ~10 minutes. In a second shell:

```powershell
solalpha status                               # last persisted snapshot
curl http://127.0.0.1:9464/health             # live health
```

**Go gate:** `/health` reports `overall: ok`; logs show `keypair_loaded`,
transaction builds (`solalpha_tx_build_*` metrics), no submits (dry-run is
honoured), no unhandled exceptions. Stop the process with `Ctrl+C`. Note that
Jupiter has limited devnet support, so failed quotes are expected; what you
are validating is that the *build path* (`TxBuilder`, `KeypairLoader`,
`AltManager`) executes cleanly with a real keypair.

## 3. Mainnet PAPER soak (24 h)

Now point at mainnet but stay in PAPER. This proves your RPC providers are
healthy, the ws ingestor stays connected, the decoder handles real swaps,
and signals/risk decisions flow.

**cmd:**
```cmd
set SOLALPHA_PROFILE=paper
set SOLALPHA_LIVE_TRADING=0
set SOLALPHA_KEYPAIR_PATH=
set SOLALPHA_RPC_URLS=https://your-helius-url,https://your-fallback-url
solalpha paper
```

**PowerShell:**
```powershell
$env:SOLALPHA_PROFILE      = "paper"
$env:SOLALPHA_LIVE_TRADING = "0"
$env:SOLALPHA_KEYPAIR_PATH = ""             # not needed in PAPER
$env:SOLALPHA_RPC_URLS     = "https://your-helius-url,https://your-fallback-url"
solalpha paper
```

After 24 h (or at minimum overnight), inspect (replace `YYYY-MM-DD` with
the UTC day):

```
solalpha status
solalpha report --day YYYY-MM-DD
curl http://127.0.0.1:9464/status         :: full status (PowerShell: pipe to ConvertFrom-Json)
```

**Go gate:** no `mode_transition` to `DEGRADED_RPC` lasting >5 min, no
unhandled-exception worker restarts in logs, the paper executor has filled
at least a handful of synthetic trades, `daily_pnl` is recorded.

If a transition to `HALT` happened, investigate before continuing. The
`mode_transitions` SQLite table records the reason.

## 4. Pre-flight (immediately before live)

In the shell you will launch `solalpha live` from -- pick the block that
matches your shell:

**cmd:**
```cmd
set SOLALPHA_PROFILE=live
set SOLALPHA_LIVE_TRADING=1
set SOLALPHA_KEYPAIR_PATH=%USERPROFILE%\.solalpha\keypairs\mainnet.json
set SOLALPHA_RPC_URLS=https://your-helius-url,https://your-fallback-url
set SOLALPHA_                       :: confirm all four are set, no typos
```

**PowerShell:**
```powershell
$env:SOLALPHA_PROFILE      = "live"
$env:SOLALPHA_LIVE_TRADING = "1"
$env:SOLALPHA_KEYPAIR_PATH = "$env:USERPROFILE\.solalpha\keypairs\mainnet.json"
$env:SOLALPHA_RPC_URLS     = "https://your-helius-url,https://your-fallback-url"
```

Lower the per-trade cap for the drill by editing `config/live.yaml`:

```yaml
risk:
  per_trade_usd_cap: 10        # was 250 -- supervised first-trade cap
  max_open_positions: 2        # was 6 -- only allow two parallel positions
```

Re-confirm:

```powershell
solalpha status                                # overall: ok, kill_switch_armed: false
curl http://127.0.0.1:9464/health
```

Have a second terminal ready with the abort command primed but not run:

```powershell
solalpha kill arm --reason "operator abort"
```

**Go gate:** `overall: ok`, ≥2 RPCs healthy, kill switch disarmed, keypair
file `chmod`/ACL restricted, you are awake and at the keyboard, and the
account balance is exactly what you intend to put at risk.

## 5. Promote to LIVE

```powershell
solalpha live
```

The process boots in `PAPER` and only promotes after `paper_to_live_health_s`
of sustained health (5 min default). Watch the mode transitions in a second
terminal:

**PowerShell** (the watcher really wants PowerShell -- cmd's loop syntax
is awkward; one PS window for the watcher even if you launched in cmd):
```powershell
while ($true) {
  Get-Date -Format HH:mm:ss
  curl -s http://127.0.0.1:9464/status | ConvertFrom-Json | Select-Object -ExpandProperty mode
  Start-Sleep -Seconds 10
}
```

**cmd:** open a second window and just re-run `solalpha status` every
~10 s, or `for /L %i in (1,1,9999) do @(echo. & solalpha status & timeout /t 10 > nul)`.

When you see the transition `PAPER → LIVE` in the logs, the system is now
trading real money against your configured cap. Stay at the keyboard for
**at least 30 minutes** after the first live fill.

**Abort criteria** -- arm the kill switch *immediately* if any of these:

- A `mode_transition` to `HALT` you did not understand
- A `risk_internal_error` in logs
- An unexpected `fill` larger than `risk.per_trade_usd_cap`
- Anything in the logs you do not recognise

```powershell
solalpha kill arm --reason "<one-line reason>"
```

Or out-of-band (works even if the CLI is unavailable):

```cmd
type nul > data\.kill                         :: cmd: touch the probe file
```
```powershell
New-Item -Path .\data\.kill -ItemType File   # PowerShell equivalent
```

The mode latches `HALT` on the next 1 s tick; in-flight orders are allowed
to confirm; no new orders are placed.

## 6. After-action

Within an hour of the first session ending (replace `YYYY-MM-DD` with the
UTC day -- `%date%` in cmd is locale-dependent and brittle; just type the
date):

```
solalpha kill arm --reason "session over"
solalpha report --day YYYY-MM-DD > drill-report.json
solalpha snapshot
```

Review:

- `drill-report.json` — every fill, sizing, realized PnL.
- `data/snapshots/` — the final snapshot for crash recovery if needed.
- The `logs/` directory — any warnings.

If the drill went well, you can raise `risk.per_trade_usd_cap` toward the
configured default in subsequent sessions. If anything surprised you, keep
the cap small and investigate before raising it.

## 7. Rollback

To return to PAPER without halting:

```powershell
solalpha mode set PAPER --reason "drill complete"
solalpha mode show                            # confirm the override is recorded
```

To resume normal operation later, `solalpha mode clear` releases the
override; the health gate then re-evaluates against `paper_to_live_health_s`
before promoting back to LIVE.

To stop entirely: `Ctrl+C` the running process. The default-on-restart is
PAPER regardless of profile.
