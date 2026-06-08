"""Replay determinism -- the headline contract.

The same recorded `NormalizedSwap` parquet session, replayed twice, must
produce bit-identical `signal_inputs_hash`es. If this test ever fails, a
non-`Clock` time source or unseeded randomness has leaked into the
signal path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from solalpha.foundation.config import AppConfig
from solalpha.research.replay import replay_session

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.replay

MINT = "Alpha111111111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"


def _write_session(path: Path, n_rounds: int = 15) -> int:
    """Write a synthetic NormalizedSwap parquet session; return row count."""
    smart = [f"SmartWallet{i:033d}" for i in range(4)]
    base = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
    rows = []
    for i in range(n_rounds):
        for wi, w in enumerate(smart):
            rows.append(
                {
                    "event_id": f"e-{i}-{wi:040d}",
                    "signature": f"sig-{i}-{wi}",
                    "slot": 1000 + i * 4 + wi,
                    "block_time": base + timedelta(seconds=i),
                    "venue": "jupiter",
                    "wallet": w,
                    "mint": MINT,
                    "side": "buy",
                    "input_mint": WSOL,
                    "output_mint": MINT,
                    "input_amount_raw": 100,
                    "output_amount_raw": 200,
                    "price": 0.0,
                    "usd_value": 50.0,
                    "pool": None,
                    "features": {"_": 0.0},  # parquet rejects empty structs
                    "received_at": base,
                }
            )
    pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")
    return len(rows)


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "profile": "test",
            "persistence": {"data_dir": str(tmp_path)},
            "kill_switch": {"file_path": str(tmp_path / ".kill")},
            "metrics": {"enabled": False},
            "signals": {
                "prepump": {
                    "window_s": 60,
                    "min_buy_pressure_ratio": 2.0,
                    "min_liquidity_slope_pct_per_min": -1.0,
                },
                "cluster": {"wallets_required": 3, "window_s": 60, "min_total_buy_usd": 100.0},
                "flow_anomaly": {"baseline_window_s": 120, "z_threshold": 2.0},
                "weights": {"prepump": 0.4, "cluster": 0.4, "flow_anomaly": 0.2},
            },
        }
    )


async def test_replay_is_deterministic(tmp_path: Path) -> None:
    session = tmp_path / "session.parquet"
    n = _write_session(session)

    # Two isolated data dirs: the second replay must not inherit ANY state
    # from the first (the determinism contract is purely a function of the
    # session input). It also sidesteps Windows holding the SQLite file
    # handle past `aclose()`, which on POSIX releases immediately.
    run1 = tmp_path / "run1"
    run2 = tmp_path / "run2"
    run1.mkdir()
    run2.mkdir()

    first = await replay_session(_cfg(run1), session)
    second = await replay_session(_cfg(run2), session)

    assert first["n_swaps"] == second["n_swaps"] == n
    assert first["signal_inputs_hashes"] == second["signal_inputs_hashes"], (
        "replay produced different inputs_hashes -- a non-deterministic "
        "input (wall clock / unseeded RNG) has leaked into the signal path"
    )
    # The metrics dict is identical too.
    assert first["metrics"] == second["metrics"]


async def test_replay_produces_signals(tmp_path: Path) -> None:
    session = tmp_path / "session.parquet"
    _write_session(session)
    cfg = _cfg(tmp_path)
    result = await replay_session(cfg, session)
    assert len(result["signal_inputs_hashes"]) >= 1


async def test_empty_session_is_handled(tmp_path: Path) -> None:
    session = tmp_path / "empty.parquet"
    pq.write_table(
        pa.Table.from_pylist([{"event_id": "x"}]),  # one junk row, no valid swaps
        session,
    )
    cfg = _cfg(tmp_path)
    result = await replay_session(cfg, session)
    # A junk row that fails NormalizedSwap validation is skipped.
    assert result["n_swaps"] == 0
