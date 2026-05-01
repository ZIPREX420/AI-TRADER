"""Transaction submission: Jito bundle (buys) or RPC sendRawTransaction (sells)."""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Optional

import base58
import httpx

log = logging.getLogger("submit")

DEFAULT_TIMEOUT = 8.0
MAX_RETRIES = 2


async def _backoff(attempt: int) -> None:
    await asyncio.sleep(0.4 * (1.6 ** attempt))


async def submit_jito_bundle(
    http: httpx.AsyncClient,
    jito_url: str,
    txs_b58: list[str],
    timeout_s: float = DEFAULT_TIMEOUT,
) -> Optional[str]:
    """Returns the Jito bundle id, or None on failure."""
    if not jito_url or not txs_b58:
        return None
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendBundle",
        "params": [txs_b58],
    }
    url = jito_url.rstrip("/") + "/api/v1/bundles"
    last_err: Optional[str] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = await http.post(url, json=payload, timeout=timeout_s)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = f"transport:{type(e).__name__}"
            if attempt < MAX_RETRIES:
                await _backoff(attempt)
                continue
            log.warning(f"jito bundle submit failed: {last_err}")
            return None
        if r.status_code == 200:
            try:
                data = r.json() or {}
                return data.get("result")
            except ValueError:
                return None
        if r.status_code in (429, 500, 502, 503, 504):
            last_err = f"http_{r.status_code}"
            if attempt < MAX_RETRIES:
                await _backoff(attempt)
                continue
        log.warning(f"jito bundle non-2xx {r.status_code}: {r.text[:200]}")
        return None
    return None


async def submit_rpc(
    http: httpx.AsyncClient,
    rpc_url: str,
    tx_bytes: bytes,
    *,
    skip_preflight: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT,
) -> Optional[str]:
    """Returns the transaction signature, or None on failure."""
    if not rpc_url or not tx_bytes:
        return None
    encoded = base58.b58encode(tx_bytes).decode("ascii")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            encoded,
            {
                "encoding": "base58",
                "skipPreflight": bool(skip_preflight),
                "maxRetries": 3,
                "preflightCommitment": "confirmed",
            },
        ],
    }
    last_err: Optional[str] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = await http.post(rpc_url, json=payload, timeout=timeout_s)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = f"transport:{type(e).__name__}"
            if attempt < MAX_RETRIES:
                await _backoff(attempt)
                continue
            log.warning(f"rpc submit failed: {last_err}")
            return None
        if r.status_code == 200:
            try:
                data = r.json() or {}
            except ValueError:
                return None
            if "error" in data:
                log.warning(f"rpc submit error: {data['error']}")
                return None
            return data.get("result")
        if r.status_code in (429, 500, 502, 503, 504):
            last_err = f"http_{r.status_code}"
            if attempt < MAX_RETRIES:
                await _backoff(attempt)
                continue
        log.warning(f"rpc submit non-2xx {r.status_code}: {r.text[:200]}")
        return None
    return None
