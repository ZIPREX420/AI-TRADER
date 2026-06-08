"""Persistent state stores.

`SqliteStore` is the authoritative store for state (positions, orders, fills,
mode transitions, kill switch, quarantine, blacklist, checkpoints, journal).
All writes serialize through one async lock to keep behaviour deterministic
under WAL mode without surprising the journaling implementation.

`ParquetStore` is the analytical/cold store. Hot ingestion writes events here
in append-only fashion partitioned by `dt=YYYY-MM-DD`; the live trading path
never reads from parquet.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING

import aiosqlite
import anyio
import pyarrow as pa
import pyarrow.parquet as pq

from solalpha.foundation.errors import PersistenceError, StateCorruptionError
from solalpha.foundation.logging import get_logger
from solalpha.foundation.persistence_schema import apply_migrations

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
    from pathlib import Path
    from typing import Any

    from solalpha.foundation.clock import Clock

_log = get_logger(__name__)


class SqliteStore:
    """Async sqlite store with WAL, single-writer serialization, and migrations."""

    def __init__(self, db_path: Path, clock: Clock | None = None) -> None:
        self.db_path = db_path
        self._clock = clock
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = anyio.Lock()

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Apply migrations synchronously first so subsequent async usage is safe.
        await anyio.to_thread.run_sync(self._sync_migrate)
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.commit()
        _log.info("sqlite_connected", path=str(self.db_path))

    def _sync_migrate(self) -> None:
        with sqlite3.connect(str(self.db_path)) as raw:
            raw.execute("PRAGMA journal_mode=WAL")
            try:
                apply_migrations(raw)
            except sqlite3.DatabaseError as e:
                raise StateCorruptionError(f"sqlite migration failed: {e}") from e

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        if self._conn is None:
            raise PersistenceError("SqliteStore not connected")
        async with self._write_lock:
            try:
                yield self._conn
                await self._conn.commit()
            except Exception:
                await self._conn.rollback()
                raise

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        if self._conn is None:
            raise PersistenceError("SqliteStore not connected")
        async with self._write_lock:
            cur = await self._conn.execute(sql, params)
            await self._conn.commit()
            try:
                return cur.rowcount
            finally:
                await cur.close()

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        if self._conn is None:
            raise PersistenceError("SqliteStore not connected")
        async with self._write_lock:
            cur = await self._conn.executemany(sql, rows)
            await self._conn.commit()
            try:
                return cur.rowcount
            finally:
                await cur.close()

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Mapping[str, Any] | None:
        if self._conn is None:
            raise PersistenceError("SqliteStore not connected")
        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            return dict(row)

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[Mapping[str, Any]]:
        if self._conn is None:
            raise PersistenceError("SqliteStore not connected")
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def journal(
        self, kind: str, payload: dict[str, Any], *, ts: datetime | None = None
    ) -> None:
        """Append an event to the recovery journal.

        The timestamp comes from the injected `Clock` (or an explicit `ts`); no
        code in solalpha reads wall-time directly -- see `foundation/clock.py`.
        """
        if ts is None:
            if self._clock is None:
                raise PersistenceError(
                    "SqliteStore.journal needs a Clock (pass clock= to __init__) or explicit ts"
                )
            ts = self._clock.now()
        await self.execute(
            "INSERT INTO journal (ts, kind, payload_json) VALUES (?, ?, ?)",
            (ts.isoformat(timespec="microseconds"), kind, json.dumps(payload, default=str)),
        )

    async def integrity_check(self) -> bool:
        row = await self.fetch_one("PRAGMA integrity_check")
        return bool(row and next(iter(row.values())) == "ok")


class ParquetStore:
    """Append-only parquet store partitioned by `dt=YYYY-MM-DD`.

    Each call to `append(table, rows)` writes a new part file under
    `<root>/<table>/dt=YYYY-MM-DD/part-<epoch_ms>-<seq>.parquet`. Compaction
    rewrites part files into a single file per day.
    """

    def __init__(self, root: Path, clock: Clock) -> None:
        self.root = root
        self._clock = clock
        self._lock = anyio.Lock()
        self._seq = 0

    async def append(self, table: str, rows: Sequence[Mapping[str, Any]]) -> Path | None:
        if not rows:
            return None
        async with self._lock:
            return await anyio.to_thread.run_sync(self._sync_append, table, list(rows))

    def _sync_append(self, table: str, rows: list[Mapping[str, Any]]) -> Path:
        if not rows:
            raise PersistenceError("no rows to append")
        # All rows in one append must share the same date partition.
        first_dt = self._date_for_row(rows[0])
        partition_dir = self.root / table / f"dt={first_dt}"
        partition_dir.mkdir(parents=True, exist_ok=True)
        epoch_ms = int(self._clock.now().timestamp() * 1000)
        # `_seq` guarantees uniqueness even when the clock does not advance
        # between appends (e.g. a FakeClock under deterministic replay).
        self._seq += 1
        target = partition_dir / f"part-{epoch_ms:013d}-{self._seq:06d}.parquet"
        try:
            table_obj = pa.Table.from_pylist([dict(r) for r in rows])
            pq.write_table(table_obj, target, compression="zstd")  # type: ignore[no-untyped-call]
        except Exception as e:
            raise PersistenceError(f"parquet append failed: {e}") from e
        return target

    def _date_for_row(self, row: Mapping[str, Any]) -> str:
        # Try common timestamp fields.
        for k in ("ts", "block_time", "received_at", "created_at"):
            v = row.get(k)
            if isinstance(v, datetime):
                return v.strftime("%Y-%m-%d")
            if isinstance(v, str) and len(v) >= 10:
                return v[:10]
        return self._clock.now().strftime("%Y-%m-%d")

    async def compact(self, table: str, day: str) -> Path | None:
        async with self._lock:
            return await anyio.to_thread.run_sync(self._sync_compact, table, day)

    def _sync_compact(self, table: str, day: str) -> Path | None:
        partition_dir = self.root / table / f"dt={day}"
        if not partition_dir.exists():
            return None
        parts = sorted(partition_dir.glob("part-*.parquet"))
        if len(parts) <= 1:
            return parts[0] if parts else None
        try:
            tables = [pq.read_table(p) for p in parts]  # type: ignore[no-untyped-call]
            merged = pa.concat_tables(tables, promote_options="default")
            target = partition_dir / "compacted.parquet"
            pq.write_table(merged, target, compression="zstd")  # type: ignore[no-untyped-call]
            for p in parts:
                p.unlink(missing_ok=True)
            target.rename(partition_dir / "part-00000000000.parquet")
            return partition_dir / "part-00000000000.parquet"
        except Exception as e:
            raise PersistenceError(f"parquet compact failed: {e}") from e

    async def read_partition(self, table: str, day: str) -> pa.Table | None:
        return await anyio.to_thread.run_sync(self._sync_read, table, day)

    def _sync_read(self, table: str, day: str) -> pa.Table | None:
        partition_dir = self.root / table / f"dt={day}"
        if not partition_dir.exists():
            return None
        parts = sorted(partition_dir.glob("part-*.parquet"))
        if not parts:
            return None
        tables = [pq.read_table(p) for p in parts]  # type: ignore[no-untyped-call]
        return pa.concat_tables(tables, promote_options="default")


__all__ = ["ParquetStore", "SqliteStore"]
