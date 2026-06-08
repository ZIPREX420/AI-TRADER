"""Solana websocket ingestor.

Opens a persistent websocket to one Solana RPC and runs `logsSubscribe`
against the configured set of program ids (Jupiter v6, Raydium v4, Orca,
pump.fun by default) plus any tracked smart-wallet addresses. Each
notification is normalized to a `RawEvent`, deduplicated, and published on
the `events` topic.

Reconnects with capped exponential backoff (`ws_reconnect_max_s`) on any
transport-level error; heartbeat pings keep the connection alive. The
ingestor is mode-aware via the runtime supervisor: it runs in every mode
where the data plane is allowed, including `DEGRADED_RPC` and `PAPER`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import websockets
from websockets.exceptions import (
    ConnectionClosedError,
    InvalidHandshake,
    InvalidStatus,
    WebSocketException,
)

from solalpha.domain import RawEvent
from solalpha.foundation import metrics
from solalpha.foundation.bus import EVENTS_TOPIC
from solalpha.foundation.ids import deterministic_event_id
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    from solalpha.data.dedupe import DedupeRing
    from solalpha.foundation.bus import Bus
    from solalpha.foundation.clock import Clock

_log = get_logger(__name__)


class WebSocketIngestor:
    """Owns a single ws connection; resubscribes on reconnect."""

    name = "ws_ingestor"
    modes: tuple[str, ...] = ()  # always-on

    def __init__(
        self,
        ws_url: str,
        bus: Bus,
        dedupe: DedupeRing,
        clock: Clock,
        *,
        program_ids: Iterable[str],
        smart_wallets: Iterable[str] = (),
        heartbeat_s: float = 15.0,
        reconnect_max_s: float = 30.0,
        commitment: str = "confirmed",
    ) -> None:
        if not ws_url:
            raise ValueError("WebSocketIngestor requires a non-empty ws_url")
        self._ws_url = ws_url
        self._bus = bus
        self._dedupe = dedupe
        self._clock = clock
        self._program_ids = list(program_ids)
        self._smart_wallets = list(smart_wallets)
        self._heartbeat_s = heartbeat_s
        self._reconnect_max_s = reconnect_max_s
        self._commitment = commitment

    async def run(self) -> None:
        """Reconnect loop. Cancellation-safe."""
        backoff = 1.0
        while True:
            try:
                await self._run_once()
                # Clean exit (peer closed gracefully) -- reset backoff.
                backoff = 1.0
            except (ConnectionClosedError, InvalidHandshake, InvalidStatus) as e:
                _log.warning(
                    "ws_disconnected",
                    exc=str(e),
                    exc_type=type(e).__name__,
                    backoff_s=backoff,
                )
            except WebSocketException as e:
                _log.warning(
                    "ws_error",
                    exc=str(e),
                    exc_type=type(e).__name__,
                    backoff_s=backoff,
                )
            except OSError as e:
                _log.warning("ws_oserror", exc=str(e), backoff_s=backoff)
            await self._clock.sleep(backoff)
            backoff = min(self._reconnect_max_s, backoff * 2.0)

    async def _run_once(self) -> None:
        async with websockets.connect(
            self._ws_url,
            ping_interval=self._heartbeat_s,
            ping_timeout=self._heartbeat_s,
            max_size=4 * 1024 * 1024,
        ) as ws:
            _log.info("ws_connected", url=self._ws_url)
            # Inline subscription so we don't have to carry the websocket
            # type across the API boundary -- the concrete type differs
            # between websockets 12 and 13.
            subs = [
                self._sub_request(idx, [pid])
                for idx, pid in enumerate(self._program_ids)
            ]
            for offset, addr in enumerate(self._smart_wallets, start=len(subs)):
                subs.append(self._sub_request(offset, [addr]))
            for req in subs:
                await ws.send(json.dumps(req))
            topic = await self._bus.topic(EVENTS_TOPIC)
            async for raw in ws:
                msg = self._decode_message(raw)
                if msg is None:
                    continue
                event = self._notification_to_event(msg)
                if event is None:
                    continue
                metrics.EVENTS_INGESTED.labels(source="ws").inc()
                if not self._dedupe.add(event.event_id):
                    continue
                await topic.publish(event)

    def _sub_request(self, idx: int, mentions: list[str]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": idx,
            "method": "logsSubscribe",
            "params": [
                {"mentions": mentions},
                {"commitment": self._commitment},
            ],
        }

    @staticmethod
    def _decode_message(raw: str | bytes) -> dict[str, Any] | None:
        text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        return obj

    def _notification_to_event(self, msg: dict[str, Any]) -> RawEvent | None:
        if msg.get("method") != "logsNotification":
            return None
        params = msg.get("params")
        if not isinstance(params, dict):
            return None
        result = params.get("result")
        if not isinstance(result, dict):
            return None
        ctx = result.get("context")
        value = result.get("value")
        if not isinstance(ctx, dict) or not isinstance(value, dict):
            return None
        slot = ctx.get("slot")
        signature = value.get("signature")
        if not isinstance(slot, int) or not isinstance(signature, str):
            return None
        logs_raw = value.get("logs")
        logs = (
            tuple(str(s) for s in logs_raw) if isinstance(logs_raw, list) else ()
        )
        now = self._clock.now()
        return RawEvent(
            event_id=deterministic_event_id(signature, slot, 0),
            signature=signature,
            slot=slot,
            block_time=now,  # block_time is unknown until getTransaction; ws gives ingest time
            program_id="<ws>",
            accounts=(),
            logs=logs,
            data="",
            source="ws",
            received_at=now,
        )


__all__ = ["WebSocketIngestor"]
