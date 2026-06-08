"""SnapshotManager + recover(): round-trip and corrupt-snapshot fallback."""

from __future__ import annotations

import json

import pytest

from solalpha.observability.recovery import RecoveryReport, recover
from solalpha.observability.snapshot import SnapshotManager

pytestmark = pytest.mark.integration


async def test_snapshot_writes_valid_file(store: object, clock: object, app_config: object) -> None:
    app_config.persistence.snapshot_root.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
    mgr = SnapshotManager(store, clock, app_config.persistence.snapshot_root)  # type: ignore[arg-type]
    path = await mgr.snapshot_now()
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert "last_journal_seq" in data
    assert "mode" in data
    assert "kill_switch" in data


async def test_snapshot_seq_advances_with_journal(
    store: object, clock: object, app_config: object
) -> None:
    app_config.persistence.snapshot_root.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
    mgr = SnapshotManager(store, clock, app_config.persistence.snapshot_root)  # type: ignore[arg-type]
    p1 = await mgr.snapshot_now()
    seq1 = json.loads(p1.read_text())["last_journal_seq"]
    await store.journal("evt", {"a": 1})  # type: ignore[attr-defined]
    clock.advance(2)  # type: ignore[attr-defined]
    p2 = await mgr.snapshot_now()
    seq2 = json.loads(p2.read_text())["last_journal_seq"]
    assert seq2 > seq1


async def test_recover_returns_report(store: object, clock: object, app_config: object) -> None:
    app_config.persistence.snapshot_root.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
    mgr = SnapshotManager(store, clock, app_config.persistence.snapshot_root)  # type: ignore[arg-type]
    await mgr.snapshot_now()
    report = await recover(app_config)
    assert isinstance(report, RecoveryReport)
    assert report.snapshot_path is not None
    assert report.journal_entries_replayed >= 0
    # The report round-trips to JSON for the CLI.
    report.model_dump(mode="json")


async def test_recover_falls_back_on_corrupt_snapshot(
    store: object, clock: object, app_config: object
) -> None:
    root = app_config.persistence.snapshot_root  # type: ignore[attr-defined]
    root.mkdir(parents=True, exist_ok=True)
    mgr = SnapshotManager(store, clock, app_config.persistence.snapshot_root)  # type: ignore[arg-type]
    # A good older snapshot.
    good = await mgr.snapshot_now()
    clock.advance(2)  # type: ignore[attr-defined]
    # A newer but corrupt snapshot.
    corrupt = root / "2099-01-01T00-00-00.snap"
    corrupt.write_text("{ this is not json")
    report = await recover(app_config)
    assert report.fallback_used is True
    assert report.snapshot_path is not None
    assert good.name in report.snapshot_path
