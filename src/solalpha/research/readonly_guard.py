"""Readonly guard for research code.

`ReadonlyGuard.wrap(store)` returns a proxy `SqliteStore` whose mutating
methods raise `ResearchWriteBlocked`. Research code passes the guarded
store everywhere it touches persistent state; this guarantees that a
buggy backfill / replay / walk-forward run cannot corrupt the live
trading database.

Reads (`fetch_one`, `fetch_all`, `integrity_check`, `transaction` for
read-only access patterns) are forwarded unchanged.

The guard is enforced *defensively* in addition to the `ResearchConfig`
validator that already refuses `allow_live_writes=True`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from solalpha.foundation.errors import ResearchWriteBlocked
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from contextlib import _AsyncGeneratorContextManager
    from typing import Any

    import aiosqlite

    from solalpha.foundation.state import SqliteStore

_log = get_logger(__name__)


class ReadonlyGuard:
    """Wraps a `SqliteStore` and routes mutations to `ResearchWriteBlocked`."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @property
    def db_path(self) -> object:
        return self._store.db_path

    async def connect(self) -> None:
        await self._store.connect()

    async def close(self) -> None:
        await self._store.close()

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Mapping[str, Any] | None:
        return await self._store.fetch_one(sql, params)

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[Mapping[str, Any]]:
        return await self._store.fetch_all(sql, params)

    async def integrity_check(self) -> bool:
        return await self._store.integrity_check()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        del sql, params
        raise ResearchWriteBlocked(
            "research code attempted store.execute(); use ParquetStore instead"
        )

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        del sql, rows
        raise ResearchWriteBlocked("research code attempted store.executemany()")

    async def journal(self, kind: str, payload: dict[str, Any], **_: Any) -> None:
        del kind, payload
        raise ResearchWriteBlocked("research code attempted store.journal()")

    def transaction(self) -> _AsyncGeneratorContextManager[aiosqlite.Connection]:
        raise ResearchWriteBlocked(
            "research code attempted store.transaction(); reads should use fetch_*"
        )

    is_readonly_guard: bool = True


def assert_readonly(store: object) -> None:
    """Raise `ResearchWriteBlocked` if `store` is not a `ReadonlyGuard`."""
    if not getattr(store, "is_readonly_guard", False):
        raise ResearchWriteBlocked(
            "research entrypoint received a non-readonly store; refusing to proceed"
        )


__all__ = ["ReadonlyGuard", "assert_readonly"]
