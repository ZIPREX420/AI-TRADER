"""Shared pytest fixtures + the `--run-live` opt-in flag.

Markers (declared in `pyproject.toml`):
  * `unit`        -- pure logic, no I/O
  * `integration` -- I/O against fakes / fixtures / tempdirs, no network
  * `replay`      -- deterministic replay engine tests
  * `live`        -- hits real RPCs; skipped unless `--run-live` and
                     `SOLALPHA_TEST_LIVE=1` are both set
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from solalpha.foundation.clock import FakeClock
from solalpha.foundation.config import AppConfig
from solalpha.foundation.state import SqliteStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# ---- CLI option ----


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Run the `live` test tier against real RPC / Jupiter endpoints. "
        "Requires SOLALPHA_TEST_LIVE=1 too.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every `live` test unless both gates are set."""
    if config.getoption("--run-live") and os.environ.get("SOLALPHA_TEST_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(
        reason="live tier disabled (pass --run-live and SOLALPHA_TEST_LIVE=1 to enable)"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


# ---- common fixtures ----


@pytest.fixture
def clock() -> FakeClock:
    """A `FakeClock` anchored at 2026-05-15 12:00:00 UTC."""
    return FakeClock(start=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    """A test-flavoured `AppConfig` rooted at `tmp_path` and PAPER-locked."""
    return AppConfig.model_validate(
        {
            "profile": "test",
            "persistence": {"data_dir": str(tmp_path)},
            "kill_switch": {"file_path": str(tmp_path / ".kill")},
            "metrics": {"enabled": False},
        }
    )


@pytest.fixture
async def store(app_config: AppConfig, clock: FakeClock) -> SqliteStore:
    """A connected `SqliteStore` at `app_config.persistence.sqlite_path`."""
    s = SqliteStore(app_config.persistence.sqlite_path, clock=clock)
    await s.connect()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def make_swap(
    clock: FakeClock,
) -> Callable[..., object]:
    """Factory for synthetic `NormalizedSwap`s with sane defaults."""
    from solalpha.domain import NormalizedSwap

    def _make(
        *,
        mint: str = "M1",
        wallet: str = "W1",
        side: str = "buy",
        usd_value: float = 50.0,
        seconds_offset: float = 0.0,
        signature: str = "sig",
        slot: int = 1,
    ) -> NormalizedSwap:
        bt = clock.now() + timedelta(seconds=seconds_offset)
        return NormalizedSwap(
            event_id=f"ev-{signature}-{slot}",
            signature=signature,
            slot=slot,
            block_time=bt,
            venue="jupiter",
            wallet=wallet,
            mint=mint,
            side=side,  # type: ignore[arg-type]
            input_mint="So11111111111111111111111111111111111111112",
            output_mint=mint,
            input_amount_raw=100,
            output_amount_raw=200,
            usd_value=usd_value,
            received_at=bt,
        )

    return _make
