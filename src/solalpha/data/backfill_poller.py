"""HTTP backfill poller -- closes ws gaps and powers `solalpha research backfill`.

For each tracked address, calls `getSignaturesForAddress` from the persisted
checkpoint forward (in batches of `batch_size`) and emits one `RawEvent`
per signature into `EVENTS_TOPIC` (subject to the dedupe ring). The
decoder worker then fetches each tx body and produces `NormalizedSwap`s
exactly as for the websocket path -- dedupe by `deterministic_event_id`
means a backfill that races a live websocket notification is a no-op.

The poller persists progress in the `checkpoints` table after each
successful batch, so a crash mid-backfill resumes at the last batch
boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from solalpha.domain import RawEvent
from solalpha.foundation import metrics
from solalpha.foundation.bus import EVENTS_TOPIC
from solalpha.foundation.errors import RpcError
from solalpha.foundation.ids import deterministic_event_id
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    from solalpha.data.dedupe import DedupeRing
    from solalpha.data.rpc_pool import RpcPool
    from solalpha.foundation.bus import Bus
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.state import SqliteStore

_log = get_logger(__name__)


class BackfillPoller:
    """Drives `getSignaturesForAddress` for each tracked address."""

    name = "backfill_poller"
    modes: tuple[str, ...] = ()  # always-on

    def __init__(
        self,
        rpc: RpcPool,
        store: SqliteStore,
        bus: Bus,
        dedupe: DedupeRing,
        clock: Clock,
        *,
        addresses: Iterable[str],
        batch_size: int = 200,
        poll_interval_s: float = 30.0,
        commitment: str = "confirmed",
    ) -> None:
        self._rpc = rpc
        self._store = store
        self._bus = bus
        self._dedupe = dedupe
        self._clock = clock
        self._addresses = list(addresses)
        self._batch_size = batch_size
        self._poll_interval_s = poll_interval_s
        self._commitment = commitment

    async def run(self) -> None:
        while True:
            for address in self._addresses:
                try:
                    await self._poll_address(address)
                except RpcError as e:
                    _log.warning(
                        "backfill_rpc_error",
                        address=address,
                        exc=str(e),
                        exc_type=type(e).__name__,
                    )
                except Exception as e:
                    _log.error(
                        "backfill_unexpected_error",
                        address=address,
                        exc=str(e),
                        exc_type=type(e).__name__,
                    )
            await self._clock.sleep(self._poll_interval_s)

    async def run_once(self, *, until_signature: str | None = None) -> int:
        """Run a single pass for every address. Returns total signatures emitted.

        Used by `solalpha research backfill` (no loop), and by tests.
        """
        total = 0
        for address in self._addresses:
            total += await self._poll_address(address, stop_at=until_signature)
        return total

    async def _poll_address(self, address: str, *, stop_at: str | None = None) -> int:
        checkpoint = await self._load_checkpoint(address)
        before: str | None = None
        emitted = 0
        topic = await self._bus.topic(EVENTS_TOPIC)
        # Walk back from "now" until we hit the checkpoint signature.
        latest_seen: str | None = None
        latest_slot: int | None = None
        while True:
            result = await self._rpc.call(
                "getSignaturesForAddress",
                [
                    address,
                    {
                        "limit": self._batch_size,
                        "before": before,
                        "until": checkpoint,
                        "commitment": self._commitment,
                    },
                ],
            )
            if not isinstance(result, list) or not result:
                break
            for entry in result:
                if not isinstance(entry, dict):
                    continue
                sig = entry.get("signature")
                slot = entry.get("slot")
                if not isinstance(sig, str) or not isinstance(slot, int):
                    continue
                if stop_at is not None and sig == stop_at:
                    return emitted
                event = RawEvent(
                    event_id=deterministic_event_id(sig, slot, 0),
                    signature=sig,
                    slot=slot,
                    block_time=self._clock.now(),
                    program_id="<backfill>",
                    accounts=(address,),
                    source="backfill",
                    received_at=self._clock.now(),
                )
                metrics.EVENTS_INGESTED.labels(source="backfill").inc()
                if self._dedupe.add(event.event_id):
                    await topic.publish(event)
                    emitted += 1
                if latest_seen is None:
                    latest_seen = sig
                    latest_slot = slot
                before = sig
            if len(result) < self._batch_size:
                break
        if latest_seen is not None and latest_slot is not None:
            await self._save_checkpoint(address, latest_seen, latest_slot)
        return emitted

    async def _load_checkpoint(self, address: str) -> str | None:
        row = await self._store.fetch_one(
            "SELECT last_signature FROM checkpoints WHERE stream = ?", (address,)
        )
        if row is None:
            return None
        sig = row.get("last_signature")
        return str(sig) if isinstance(sig, str) and sig else None

    async def _save_checkpoint(
        self, address: str, signature: str, slot: int
    ) -> None:
        await self._store.execute(
            """
            INSERT INTO checkpoints (stream, last_slot, last_signature, ts)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(stream) DO UPDATE SET
                last_slot = excluded.last_slot,
                last_signature = excluded.last_signature,
                ts = excluded.ts
            """,
            (address, slot, signature, self._clock.now().isoformat()),
        )


__all__ = ["BackfillPoller"]
