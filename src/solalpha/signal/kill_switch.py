"""Kill switch -- single source of truth for "halt all new orders".

Two arming surfaces, ORed together:
  * the SQLite `kill_switch` row (`armed=1`) -- set by `solalpha kill arm`
  * the on-disk probe file (`cfg.kill_switch.file_path`) -- works even when
    the CLI is unavailable (`touch ./data/.kill`)

State is persisted in SQLite and survives restarts. The `run()` coroutine
polls the file every `poll_interval_s` and reflects changes into both
SQLite and the in-process state. Other planes read `armed()` (cheap, in-
memory) and react via the mode manager, which latches `HALT` on any arm.

The kill switch never lets orders through when armed: the risk engine and
the executor must both consult `armed()` before any side effect.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from solalpha.foundation import metrics
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from solalpha.foundation.clock import Clock
    from solalpha.foundation.state import SqliteStore

_log = get_logger(__name__)


class KillSwitch:
    """Read/write authority over the kill-switch row + file probe."""

    def __init__(
        self,
        store: SqliteStore,
        clock: Clock,
        file_path: Path,
        *,
        poll_interval_s: float = 1.0,
    ) -> None:
        self._store = store
        self._clock = clock
        self._file_path = file_path
        self._poll_interval_s = poll_interval_s
        # Cached state -- updated by `load()`, `arm()`, `disarm()`, and `run()`.
        self._armed: bool = False
        self._reason: str | None = None
        self._by_who: str | None = None

    # ---- public read ----

    def armed(self) -> bool:
        """In-memory read; do not block the hot path on the database."""
        return self._armed

    def reason(self) -> str | None:
        return self._reason

    def by_who(self) -> str | None:
        return self._by_who

    # ---- lifecycle ----

    async def load(self) -> None:
        """Reconcile in-memory state with the persisted row + file probe."""
        row = await self._store.fetch_one(
            "SELECT armed, reason, by_who FROM kill_switch WHERE id = 1"
        )
        db_armed = bool(row["armed"]) if row else False
        file_armed = self._file_path.exists()
        armed = db_armed or file_armed
        # If only one source reports armed, normalize both so they stay in sync.
        if armed and not db_armed:
            await self._persist_armed(reason="file-probe", by_who="filesystem")
        if armed and not file_armed:
            await self._touch_file()
        self._armed = armed
        self._reason = str(row["reason"]) if row and row.get("reason") else None
        self._by_who = str(row["by_who"]) if row and row.get("by_who") else None
        metrics.KILL_SWITCH_ARMED.set(1 if self._armed else 0)
        _log.info(
            "kill_switch_loaded",
            armed=self._armed,
            reason=self._reason,
            by_who=self._by_who,
        )

    async def arm(self, reason: str, by_who: str = "system") -> None:
        await self._persist_armed(reason=reason, by_who=by_who)
        await self._touch_file()
        self._armed = True
        self._reason = reason
        self._by_who = by_who
        metrics.KILL_SWITCH_ARMED.set(1)
        _log.warning("kill_switch_armed", reason=reason, by_who=by_who)

    async def disarm(self, by_who: str = "system") -> None:
        await self._store.execute(
            "UPDATE kill_switch SET armed=0, reason=NULL, since=NULL, by_who=? WHERE id=1",
            (by_who,),
        )
        with contextlib.suppress(FileNotFoundError):
            self._file_path.unlink()
        self._armed = False
        self._reason = None
        self._by_who = by_who
        metrics.KILL_SWITCH_ARMED.set(0)
        _log.warning("kill_switch_disarmed", by_who=by_who)

    async def run(self) -> None:
        """Polling loop reflecting on-disk probe into the cached state.

        SQL-driven arming (e.g. `arm()`) is observed immediately. File-probe
        arming is picked up at the next poll tick.
        """
        while True:
            try:
                file_armed = self._file_path.exists()
                if file_armed and not self._armed:
                    await self.arm(reason="file-probe", by_who="filesystem")
                elif not file_armed and self._armed and self._reason == "file-probe":
                    await self.disarm(by_who="filesystem")
            except Exception as e:
                _log.warning("kill_switch_poll_error", exc=str(e), exc_type=type(e).__name__)
            await self._clock.sleep(self._poll_interval_s)

    # ---- private ----

    async def _persist_armed(self, *, reason: str, by_who: str) -> None:
        await self._store.execute(
            "UPDATE kill_switch SET armed=1, reason=?, since=?, by_who=? WHERE id=1",
            (reason, self._clock.now().isoformat(), by_who),
        )

    async def _touch_file(self) -> None:
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        # `touch` semantics: create-or-update; do not error if the file exists.
        self._file_path.touch(exist_ok=True)


__all__ = ["KillSwitch"]
