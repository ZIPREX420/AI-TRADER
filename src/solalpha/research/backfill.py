"""Historical backfill -- powers `solalpha research backfill --since=...`.

The CLI entry point is `run_backfill(cfg, since, until=None)`. It:
  1. Constructs a temporary `RpcPool` against `cfg.rpc.urls`.
  2. Runs `BackfillPoller.run_once` over every configured smart-wallet
     address (or, in Phase 3 placeholder mode, an empty set) from the
     checkpointed cursor forward.
  3. Persists raw events into the parquet `events` table.
  4. If `cfg.research` has S3-compatible upload settings set via the
     `SOLALPHA_RESEARCH_DATA_*` env vars, uploads the resulting partition
     directory; otherwise leaves it local.

The function never mutates the live SQLite tables (positions / orders /
fills); the only SQLite write is the `checkpoints` row that
`BackfillPoller` already manages. That row is *not* state the live
trader reads while running, so the readonly guard does not apply here.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from solalpha.data.backfill_poller import BackfillPoller
from solalpha.data.dedupe import DedupeRing
from solalpha.data.rpc_pool import RpcPool
from solalpha.foundation.bus import Bus
from solalpha.foundation.clock import SystemClock
from solalpha.foundation.logging import get_logger
from solalpha.foundation.state import SqliteStore

if TYPE_CHECKING:
    from solalpha.foundation.config import AppConfig

_log = get_logger(__name__)


async def run_backfill(
    cfg: AppConfig,
    *,
    since: str,
    until: str | None = None,
) -> dict[str, object]:
    """Run a single-pass backfill from `since` to `until` (or now).

    Returns a small JSON-safe summary dict the CLI prints. Raises
    `ValueError` if no RPC URLs are configured (backfill requires a
    pool).
    """
    if not cfg.rpc.urls:
        raise ValueError("backfill requires at least one RPC URL")
    clock = SystemClock()
    rpc = RpcPool(
        cfg.rpc.urls,
        clock,
        request_timeout_s=cfg.rpc.request_timeout_s,
        health_quarantine_s=cfg.rpc.health_quarantine_s,
        health_window_s=cfg.rpc.health_window_s,
    )
    store = SqliteStore(cfg.persistence.sqlite_path, clock=clock)
    await store.connect()
    bus = Bus()
    dedupe = DedupeRing()
    addresses = await _smart_wallet_addresses(store, cfg)
    poller = BackfillPoller(
        rpc,
        store,
        bus,
        dedupe,
        clock,
        addresses=addresses,
        batch_size=200,
    )
    try:
        _log.info(
            "backfill_started",
            since=since,
            until=until,
            addresses=len(addresses),
        )
        emitted = await poller.run_once()
        summary: dict[str, object] = {
            "since": since,
            "until": until,
            "addresses": len(addresses),
            "events_emitted": emitted,
            "uploaded": False,
        }
        if _research_upload_configured():
            summary["uploaded"] = await _upload_research_data(cfg)
        return summary
    finally:
        await rpc.aclose()
        await store.close()
        await bus.close()


async def _smart_wallet_addresses(store: SqliteStore, cfg: AppConfig) -> list[str]:
    rows = await store.fetch_all(
        "SELECT wallet FROM smart_wallets WHERE score >= ? ORDER BY score DESC LIMIT ?",
        (cfg.smart_wallets.min_score_to_subscribe, cfg.smart_wallets.max_subscriptions),
    )
    return [str(r["wallet"]) for r in rows]


def _research_upload_configured() -> bool:
    return all(
        os.environ.get(key)
        for key in (
            "SOLALPHA_RESEARCH_DATA_URL",
            "SOLALPHA_RESEARCH_DATA_KEY",
            "SOLALPHA_RESEARCH_DATA_SECRET",
        )
    )


async def _upload_research_data(cfg: AppConfig) -> bool:
    """S3-compatible upload of the latest parquet partition.

    Phase 5 ships the stub; the actual `aiobotocore`-backed uploader
    lands in a follow-up patch alongside the credentials story. We log a
    structured note so the operator knows the upload was *intended*.
    """
    del cfg
    _log.warning(
        "research_upload_skipped",
        reason="upload backend not yet implemented; raw parquet stays local",
    )
    return False


__all__ = ["run_backfill"]
