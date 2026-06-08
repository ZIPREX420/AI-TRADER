"""Periodic full-state snapshots for crash recovery.

`SnapshotManager` dumps a JSON document under `data/snapshots/` capturing the
state needed to bootstrap from a cold start: schema version, latest journal
sequence, current mode, kill-switch state, every open position, and today's
`daily_pnl` row. `recover()` (in `observability/recovery.py`) loads the latest
snapshot, replays journal entries since `last_journal_seq`, and resumes.

Snapshots are write-only here -- the loader lives in `recovery.py`.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import anyio

from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from solalpha.foundation.clock import Clock
    from solalpha.foundation.state import SqliteStore

_log = get_logger(__name__)

SNAPSHOT_SCHEMA_VERSION = 1


class SnapshotManager:
    """Owns the `data/snapshots/` directory.

    Construct with the runtime store and clock; `run()` is the periodic loop
    that takes a snapshot every `interval_s` seconds and prunes files older
    than `retention_days`. CLI's `solalpha snapshot` calls `snapshot_now()`
    directly without `run()`.
    """

    def __init__(
        self,
        store: SqliteStore,
        clock: Clock,
        snapshot_root: Path,
        *,
        interval_s: float = 60.0,
        retention_days: int = 14,
    ) -> None:
        self._store = store
        self._clock = clock
        self._root = snapshot_root
        self._interval_s = interval_s
        self._retention_days = retention_days

    # ---- public ----

    async def snapshot_now(self) -> Path:
        """Dump current state to a new snapshot file and return its path."""
        payload = await self._build_payload()
        self._root.mkdir(parents=True, exist_ok=True)
        now = self._clock.now()
        # Filename is ISO-ish but colon-free so Windows tolerates it.
        stamp = now.strftime("%Y-%m-%dT%H-%M-%S")
        target = self._root / f"{stamp}.snap"
        # Write atomically: temp file then rename so a crash mid-write never
        # leaves a half-written snapshot at the canonical name.
        tmp = target.with_suffix(".snap.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
        _log.info("snapshot_written", path=str(target), journal_seq=payload["last_journal_seq"])
        return target

    async def run(self) -> None:
        """Periodic snapshot + prune loop. Cancellation-safe."""
        while True:
            try:
                await self.snapshot_now()
                await self._prune_old()
            except Exception as e:  # log and keep going
                _log.error("snapshot_loop_error", exc=str(e), exc_type=type(e).__name__)
            await self._clock.sleep(self._interval_s)

    async def latest(self) -> Path | None:
        snaps = await self.list_snapshots()
        return snaps[0] if snaps else None

    async def list_snapshots(self) -> list[Path]:
        """All snapshots, newest first."""
        return await anyio.to_thread.run_sync(self._sync_list)

    # ---- private ----

    def _sync_list(self) -> list[Path]:
        if not self._root.exists():
            return []
        return sorted(self._root.glob("*.snap"), key=lambda p: p.name, reverse=True)

    async def _prune_old(self) -> None:
        snaps = await self.list_snapshots()
        if not snaps:
            return
        cutoff = self._clock.now() - timedelta(days=self._retention_days)
        # Always retain the newest two regardless of age (fallback for recovery).
        keep = set(snaps[:2])
        removed = 0
        for path in snaps:
            if path in keep:
                continue
            try:
                # Filename starts with %Y-%m-%dT%H-%M-%S; compare prefix to cutoff.
                if path.name[:19] >= cutoff.strftime("%Y-%m-%dT%H-%M-%S"):
                    continue
                path.unlink(missing_ok=True)
                removed += 1
            except OSError as e:
                _log.warning("snapshot_prune_failed", path=str(path), exc=str(e))
        if removed:
            _log.info("snapshot_pruned", removed=removed, retained=len(snaps) - removed)

    async def _build_payload(self) -> dict[str, Any]:
        now = self._clock.now()
        last_seq_row = await self._store.fetch_one(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM journal"
        )
        last_seq = int(last_seq_row["seq"]) if last_seq_row else 0
        kill_row = await self._store.fetch_one(
            "SELECT armed, reason, since, by_who FROM kill_switch WHERE id = 1"
        )
        kill = {
            "armed": bool(kill_row["armed"]) if kill_row else False,
            "reason": kill_row["reason"] if kill_row else None,
            "since": kill_row["since"] if kill_row else None,
            "by_who": kill_row["by_who"] if kill_row else None,
        }
        mode_row = await self._store.fetch_one(
            "SELECT to_mode, reason, ts FROM mode_transitions ORDER BY id DESC LIMIT 1"
        )
        mode = {
            "mode": str(mode_row["to_mode"]) if mode_row else "PAPER",
            "reason": str(mode_row["reason"]) if mode_row else "startup",
            "since": str(mode_row["ts"]) if mode_row else now.isoformat(),
        }
        positions = await self._store.fetch_all(
            "SELECT * FROM positions WHERE state = 'open' ORDER BY opened_at"
        )
        day = now.strftime("%Y-%m-%d")
        pnl_row = await self._store.fetch_one("SELECT * FROM daily_pnl WHERE day = ?", (day,))
        daily = {
            "day": day,
            "pnl_usd": float(pnl_row["pnl_usd"]) if pnl_row else 0.0,
            "wins": int(pnl_row["wins"]) if pnl_row else 0,
            "losses": int(pnl_row["losses"]) if pnl_row else 0,
            "loss_streak": int(pnl_row["loss_streak"]) if pnl_row else 0,
        }
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "ts": now.isoformat(),
            "last_journal_seq": last_seq,
            "mode": mode,
            "kill_switch": kill,
            "open_positions": [dict(p) for p in positions],
            "daily_pnl_today": daily,
        }


__all__ = ["SNAPSHOT_SCHEMA_VERSION", "SnapshotManager"]
