"""SqliteStore: migrations, journal, integrity, ParquetStore round-trip."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from solalpha.foundation.errors import PersistenceError
from solalpha.foundation.state import ParquetStore, SqliteStore

if TYPE_CHECKING:
    from pathlib import Path

    from solalpha.foundation.clock import FakeClock

pytestmark = pytest.mark.integration


async def test_migrations_create_tables(store: object) -> None:
    rows = await store.fetch_all(  # type: ignore[attr-defined]
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    names = {r["name"] for r in rows}
    for expected in ("signals", "orders", "fills", "positions", "kill_switch", "journal"):
        assert expected in names


async def test_integrity_check_passes(store: object) -> None:
    assert await store.integrity_check() is True  # type: ignore[attr-defined]


async def test_journal_requires_clock_or_ts(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "noclock.db")  # no clock
    await s.connect()
    try:
        with pytest.raises(PersistenceError, match="Clock"):
            await s.journal("k", {"a": 1})
    finally:
        await s.close()


async def test_journal_with_clock(store: object, clock: FakeClock) -> None:
    await store.journal("test_event", {"value": 42})  # type: ignore[attr-defined]
    rows = await store.fetch_all("SELECT kind, payload_json FROM journal")  # type: ignore[attr-defined]
    assert len(rows) == 1
    assert rows[0]["kind"] == "test_event"


async def test_parquet_round_trip(tmp_path: Path, clock: FakeClock) -> None:
    pstore = ParquetStore(tmp_path / "pq", clock)
    rows = [
        {"ts": "2026-05-15T00:00:00", "mint": "M1", "value": 1.0},
        {"ts": "2026-05-15T00:00:01", "mint": "M2", "value": 2.0},
    ]
    path = await pstore.append("events", rows)
    assert path is not None and path.exists()
    table = await pstore.read_partition("events", "2026-05-15")
    assert table is not None
    assert table.num_rows == 2


async def test_parquet_seq_avoids_overwrite(tmp_path: Path, clock: FakeClock) -> None:
    """Two appends in the same clock instant must not overwrite each other."""
    pstore = ParquetStore(tmp_path / "pq", clock)
    row = [{"ts": "2026-05-15T00:00:00", "v": 1}]
    p1 = await pstore.append("t", row)
    p2 = await pstore.append("t", row)  # clock did not advance
    assert p1 != p2
