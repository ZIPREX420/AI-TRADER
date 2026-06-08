#!/usr/bin/env bash
# Local development gate.
#
# Runs the same checks as .github/workflows/ci.yml so that "green locally"
# means "green in CI": format, lint, strict type-check, and the three offline
# test tiers. The `live` tier is intentionally excluded -- it needs network
# and SOLALPHA_TEST_LIVE=1 (see RUNBOOK.md / pyproject.toml markers).
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== ruff format =="
ruff format .

echo "== ruff check =="
ruff check .

echo "== mypy --strict =="
mypy --strict src/solalpha

echo "== pytest -m unit =="
pytest -m unit

echo "== pytest -m integration =="
pytest -m integration

echo "== pytest tests/replay =="
pytest tests/replay

echo "all checks passed"
