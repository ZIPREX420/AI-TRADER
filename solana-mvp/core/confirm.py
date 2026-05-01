"""Dual-RPC signature confirmation. First confirmer wins."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable, Optional

import httpx

log = logging.getLogger("confirm")


async def _poll_one(http: httpx.AsyncClient, rpc_url: str, signature: str,
                    deadline: float, poll_ms: int) -> bool:
    payload_template = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignatureStatuses",
        "params": [[signature], {"searchTransactionHistory": True}],
    }
    while time.time() < deadline:
        try:
            r = await http.post(rpc_url, json=payload_template, timeout=4.0)
        except (httpx.TimeoutException, httpx.TransportError):
            await asyncio.sleep(poll_ms / 1000.0)
            continue
        if r.status_code != 200:
            await asyncio.sleep(poll_ms / 1000.0)
            continue
        try:
            data = r.json() or {}
        except ValueError:
            await asyncio.sleep(poll_ms / 1000.0)
            continue
        result = (data.get("result") or {}).get("value") or []
        if result:
            entry = result[0]
            if entry is None:
                await asyncio.sleep(poll_ms / 1000.0)
                continue
            err = entry.get("err")
            status = entry.get("confirmationStatus")
            if err is not None:
                return False
            if status in ("confirmed", "finalized"):
                return True
        await asyncio.sleep(poll_ms / 1000.0)
    return False


async def confirm_signature(
    http: httpx.AsyncClient,
    rpc_urls: Iterable[str],
    signature: str,
    timeout_s: float = 30.0,
    poll_ms: int = 800,
) -> bool:
    urls = [u for u in rpc_urls if u]
    if not urls:
        return False
    deadline = time.time() + timeout_s
    tasks = [asyncio.create_task(_poll_one(http, u, signature, deadline, poll_ms)) for u in urls]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED, timeout=timeout_s)
        for t in done:
            try:
                res = t.result()
            except Exception:
                res = False
            if res:
                for p in pending:
                    p.cancel()
                return True
        # First completed returned False — still wait for any other to confirm before deadline
        for p in pending:
            try:
                res = await asyncio.wait_for(p, timeout=max(0.0, deadline - time.time()))
                if res:
                    return True
            except (asyncio.TimeoutError, asyncio.CancelledError):
                continue
        return False
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
