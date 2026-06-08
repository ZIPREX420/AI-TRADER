"""Decoder worker: EVENTS_TOPIC -> NORMALIZED_TOPIC.

Subscribes to `RawEvent`s coming off the websocket ingestor and the backfill
poller, fetches the full transaction via the `RpcPool`, runs
`TransactionDecoder.decode`, and publishes any resulting `NormalizedSwap`s
to `NORMALIZED_TOPIC` for the signal plane.

We do the fetch + decode here (not in the ws hot path) so a slow RPC can
stall decoding without backing up websocket consumption. The dedupe ring
is consulted at the EVENTS layer too (in the ingestor / backfill); the
decoder worker just trusts that filter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from solalpha.data.decoder import TransactionDecoder
from solalpha.foundation.bus import EVENTS_TOPIC, NORMALIZED_TOPIC
from solalpha.foundation.errors import RpcError
from solalpha.foundation.logging import bind_trace_id, get_logger

if TYPE_CHECKING:
    from solalpha.data.rpc_pool import RpcPool
    from solalpha.domain import RawEvent
    from solalpha.foundation.bus import Bus
    from solalpha.foundation.clock import Clock

_log = get_logger(__name__)


class DecoderWorker:
    """Long-running coroutine that drives the EVENTS -> NORMALIZED pipeline."""

    name = "decoder_worker"
    modes: tuple[str, ...] = ()  # always-on

    def __init__(
        self,
        bus: Bus,
        rpc: RpcPool,
        clock: Clock,
        *,
        commitment: str = "confirmed",
    ) -> None:
        self._bus = bus
        self._rpc = rpc
        self._clock = clock
        self._decoder = TransactionDecoder(clock)
        self._commitment = commitment

    async def run(self) -> None:
        in_topic = await self._bus.topic(EVENTS_TOPIC)
        out_topic = await self._bus.topic(NORMALIZED_TOPIC)
        async with in_topic.subscribe() as recv:
            async for event in recv:
                with bind_trace_id(event.signature[:16]):
                    await self._handle(event, out_topic)

    async def _handle(self, event: RawEvent, out_topic: object) -> None:
        try:
            tx = await self._rpc.call(
                "getTransaction",
                [
                    event.signature,
                    {
                        "encoding": "jsonParsed",
                        "commitment": self._commitment,
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
        except RpcError as e:
            _log.warning(
                "decoder_fetch_failed",
                signature=event.signature,
                exc=str(e),
                exc_type=type(e).__name__,
            )
            return
        if not isinstance(tx, dict):
            return
        swaps = self._decoder.decode(tx)
        if not swaps:
            return
        # `out_topic` is `Topic[NormalizedSwap]`; the bus is duck-typed.
        publish = getattr(out_topic, "publish", None)
        if publish is None:
            _log.error("decoder_out_topic_invalid", type=type(out_topic).__name__)
            return
        for swap in swaps:
            await publish(swap)


__all__ = ["DecoderWorker"]
