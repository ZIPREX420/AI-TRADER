"""Crash-recovery: load latest snapshot, replay journal, optionally reconcile.

`recover()` is called both automatically on `Application` startup and by the
CLI (`solalpha recover [--snapshot ...] [--reconcile-stuck] [--reconcile-positions]`).
The contract is: read-only inspection plus structured reporting; it never
mutates positions/orders/fills, only the runtime mode (resumes in `PAPER`)
and the recovery counters/journal.

`RecoveryReport` is the typed return value. The CLI dumps it as JSON.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from solalpha.foundation import metrics
from solalpha.foundation.clock import SystemClock
from solalpha.foundation.errors import RecoveryError
from solalpha.foundation.logging import get_logger
from solalpha.foundation.state import SqliteStore
from solalpha.observability.snapshot import SnapshotManager

if TYPE_CHECKING:
    from pathlib import Path

    from solalpha.foundation.config import AppConfig

_log = get_logger(__name__)


class RecoveryReport(BaseModel):
    """Structured result of a `recover()` invocation."""

    model_config = ConfigDict(frozen=True)

    snapshot_path: str | None
    snapshot_ts: datetime | None
    snapshot_schema_version: int | None
    last_journal_seq: int
    journal_entries_replayed: int
    open_positions_in_snapshot: int
    stuck_signatures_pending: int
    reconcile_stuck_requested: bool
    reconcile_positions_requested: bool
    fallback_used: bool
    warnings: tuple[str, ...]
    completed_at: datetime


async def recover(
    cfg: AppConfig,
    *,
    snapshot_path: Path | None = None,
    reconcile_stuck: bool = False,
    reconcile_positions: bool = False,
) -> RecoveryReport:
    """Recover from the latest (or specified) snapshot + journal."""
    clock = SystemClock()
    store = SqliteStore(cfg.persistence.sqlite_path, clock=clock)
    await store.connect()
    warnings: list[str] = []
    fallback_used = False
    snap_payload: dict[str, Any] | None = None
    snap_path_used: Path | None = None
    try:
        snap_mgr = SnapshotManager(store, clock, cfg.persistence.snapshot_root)
        if snapshot_path is not None:
            snap_path_used = snapshot_path
            snap_payload = _load_snapshot(snapshot_path, warnings)
        else:
            snaps = await snap_mgr.list_snapshots()
            for i, candidate in enumerate(snaps):
                snap_path_used = candidate
                snap_payload = _load_snapshot(candidate, warnings)
                if snap_payload is not None:
                    if i > 0:
                        fallback_used = True
                        warnings.append(
                            f"latest snapshot {snaps[0].name} unreadable; "
                            f"fell back to {candidate.name}"
                        )
                    break
        snap_ts: datetime | None = None
        snap_seq = 0
        snap_schema: int | None = None
        open_in_snap = 0
        if snap_payload is not None:
            snap_schema = int(snap_payload.get("schema_version", 0))
            ts_raw = snap_payload.get("ts")
            if isinstance(ts_raw, str):
                snap_ts = datetime.fromisoformat(ts_raw)
            snap_seq = int(snap_payload.get("last_journal_seq", 0))
            open_in_snap = len(snap_payload.get("open_positions", []))
        else:
            warnings.append("no readable snapshot found; recovery will rely on journal only")

        row = await store.fetch_one("SELECT COUNT(*) AS c FROM journal WHERE seq > ?", (snap_seq,))
        replayed = int(row["c"]) if row else 0

        stuck_row = await store.fetch_one(
            "SELECT COUNT(*) AS c FROM stuck_signatures WHERE resolved = 0"
        )
        stuck_pending = int(stuck_row["c"]) if stuck_row else 0

        if reconcile_stuck:
            # Phase 1: count only; the execution plane's StuckTxResolver does
            # the real getSignatureStatuses reconciliation (it owns the RPC pool).
            warnings.append(
                "reconcile_stuck noted; full reconciliation requires the "
                "execution plane (StuckTxResolver) -- pending"
            )
        if reconcile_positions:
            warnings.append(
                "reconcile_positions noted; full reconciliation requires the "
                "data plane (SPL balance reads) -- pending"
            )

        completed_at = clock.now()
        report = RecoveryReport(
            snapshot_path=str(snap_path_used) if snap_path_used else None,
            snapshot_ts=snap_ts,
            snapshot_schema_version=snap_schema,
            last_journal_seq=snap_seq,
            journal_entries_replayed=replayed,
            open_positions_in_snapshot=open_in_snap,
            stuck_signatures_pending=stuck_pending,
            reconcile_stuck_requested=reconcile_stuck,
            reconcile_positions_requested=reconcile_positions,
            fallback_used=fallback_used,
            warnings=tuple(warnings),
            completed_at=completed_at,
        )
        # Resume in PAPER until the operator promotes -- record the intent.
        await store.execute(
            "INSERT INTO mode_transitions (from_mode, to_mode, reason, ts) VALUES (?, ?, ?, ?)",
            ("UNKNOWN", "PAPER", "recovery resume", completed_at.isoformat()),
        )
        metrics.RECOVERY_RUNS.labels(outcome="ok").inc()
        _log.info(
            "recovery_complete",
            snapshot=str(snap_path_used) if snap_path_used else None,
            journal_replayed=replayed,
            open_positions_in_snapshot=open_in_snap,
            stuck_pending=stuck_pending,
            fallback_used=fallback_used,
            warnings=len(warnings),
        )
    except RecoveryError:
        metrics.RECOVERY_RUNS.labels(outcome="error").inc()
        raise
    except Exception as e:
        metrics.RECOVERY_RUNS.labels(outcome="error").inc()
        raise RecoveryError(f"recovery failed: {e}") from e
    finally:
        await store.close()
    return report


def _load_snapshot(path: Path, warnings: list[str]) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        warnings.append(f"snapshot {path.name} unreadable: {e}")
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        warnings.append(f"snapshot {path.name} not valid JSON: {e}")
        return None
    if not isinstance(data, dict):
        warnings.append(f"snapshot {path.name} root is not a mapping")
        return None
    return data


__all__ = ["RecoveryReport", "recover"]
