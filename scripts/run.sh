#!/usr/bin/env bash
# Convenience launcher for solalpha.
#
# Defaults to PAPER mode, which is always safe (simulated executor, no signing).
# Pass "live" to attempt LIVE mode -- it is still refused unless the operator
# has exported SOLALPHA_LIVE_TRADING=1 (see README.md / RUNBOOK.md).
#
#   scripts/run.sh                # paper mode
#   scripts/run.sh paper          # paper mode (explicit)
#   scripts/run.sh status         # print last health snapshot
#   SOLALPHA_LIVE_TRADING=1 scripts/run.sh live
set -euo pipefail

cd "$(dirname "$0")/.."

MODE="${1:-paper}"
shift || true

exec solalpha "$MODE" "$@"
