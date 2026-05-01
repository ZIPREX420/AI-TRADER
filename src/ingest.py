"""Helius logsSubscribe WebSocket client with reconnect + watchdog."""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator

import websockets

log = logging.getLogger("ingest")

_id_counter = itertools.count(1)


@dataclass
class LogEvent:
    received_at: float
    signature: str
    slot: int
    err: object | None
    logs: list[str]
    mention: str  # the mentions filter that produced this event (wallet or program)


def _next_id() -> int:
    return next(_id_counter)


async def _subscribe(ws, mention: str) -> int:
    sub_id = _next_id()
    await ws.send(json.dumps({
        "jsonrpc": "2.0",
        "id": sub_id,
        "method": "logsSubscribe",
        "params": [
            {"mentions": [mention]},
            {"commitment": "processed"},
        ],
    }))
    return sub_id


async def stream_logs(
    uri: str,
    mentions: list[str],
    queue: asyncio.Queue[LogEvent],
    *,
    name: str = "ws",
    watchdog_seconds: float = 60.0,
) -> None:
    """Subscribe to logs for each `mentions` entry, push LogEvent into queue.

    Reconnects with exponential backoff. Bails out only on cancellation.
    Sub-id → mention map allows tagging which filter produced each event.
    """
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                log.info(f"[{name}] connected, subscribing {len(mentions)} mentions")
                sub_to_mention: dict[int, str] = {}
                pending: dict[int, str] = {}
                for m in mentions:
                    req_id = await _subscribe(ws, m)
                    pending[req_id] = m
                last_msg = time.time()
                async for raw in ws:
                    last_msg = time.time()
                    msg = json.loads(raw)
                    # subscription confirmation
                    if "result" in msg and "id" in msg and msg["id"] in pending:
                        sub_to_mention[msg["result"]] = pending.pop(msg["id"])
                        continue
                    params = msg.get("params")
                    if not params:
                        continue
                    sub_id = params.get("subscription")
                    result = params.get("result", {})
                    value = result.get("value", {})
                    if not value:
                        continue
                    evt = LogEvent(
                        received_at=time.time(),
                        signature=value.get("signature", ""),
                        slot=result.get("context", {}).get("slot", 0),
                        err=value.get("err"),
                        logs=value.get("logs", []) or [],
                        mention=sub_to_mention.get(sub_id, ""),
                    )
                    if evt.err is not None:
                        continue
                    await queue.put(evt)
                    if time.time() - last_msg > watchdog_seconds:
                        log.warning(f"[{name}] watchdog stale, reconnecting")
                        break
                backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"[{name}] WS error: {e!r}; backoff={backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def stream_chunks(
    uri: str,
    mentions: list[str],
    queue: asyncio.Queue[LogEvent],
    *,
    chunk_size: int = 50,
    name: str = "ws",
) -> list[asyncio.Task]:
    """Helius caps subs per connection; spawn N tasks, each handling chunk_size mentions."""
    tasks: list[asyncio.Task] = []
    for i in range(0, len(mentions), chunk_size):
        chunk = mentions[i : i + chunk_size]
        t = asyncio.create_task(
            stream_logs(uri, chunk, queue, name=f"{name}#{i // chunk_size}"),
            name=f"{name}#{i // chunk_size}",
        )
        tasks.append(t)
    return tasks
