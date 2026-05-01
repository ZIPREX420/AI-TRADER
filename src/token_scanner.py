"""Detect new token launches: Raydium initialize2 + Pump.fun bonding-curve migrations."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from solders.signature import Signature

from .constants import (
    PUMPFUN,
    PUMPFUN_AMM,
    PUMPFUN_MIGRATE_MARKERS,
    RAYDIUM_AMM_V4,
    RAYDIUM_INIT_LOG_MARKERS,
    SOL_MINT,
)
from .ingest import LogEvent

log = logging.getLogger("token_scanner")


@dataclass
class NewTokenSignal:
    kind: str  # "new"
    mint: str
    source: str  # "raydium" | "pumpfun"
    sig: str
    slot: int
    detected_at: float
    age_ms: float


def _is_raydium_init(evt: LogEvent) -> bool:
    if RAYDIUM_AMM_V4 not in evt.mention and RAYDIUM_AMM_V4 not in (evt.logs[0] if evt.logs else ""):
        # mention should match by chunk subscription
        pass
    return any(any(m in l for m in RAYDIUM_INIT_LOG_MARKERS) for l in evt.logs)


def _is_pumpfun_migrate(evt: LogEvent) -> bool:
    return any(any(m in l for m in PUMPFUN_MIGRATE_MARKERS) for l in evt.logs)


async def parse_new_token(client, evt: LogEvent) -> Optional[NewTokenSignal]:
    if RAYDIUM_AMM_V4 in evt.mention:
        if not _is_raydium_init(evt):
            return None
        source = "raydium"
    elif PUMPFUN in evt.mention or PUMPFUN_AMM in evt.mention:
        if not _is_pumpfun_migrate(evt):
            return None
        source = "pumpfun"
    else:
        return None

    try:
        sig = Signature.from_string(evt.signature)
        from solana.rpc.commitment import Confirmed
        resp = await client.get_transaction(
            sig,
            commitment=Confirmed,
            max_supported_transaction_version=0,
            encoding="jsonParsed",
        )
    except Exception as e:
        log.debug(f"get_transaction failed for {evt.signature}: {e!r}")
        return None

    tx = getattr(resp, "value", None)
    if tx is None or tx.transaction.meta is None or tx.transaction.meta.err is not None:
        return None

    # The newly introduced mint is the non-SOL mint that appears in post_token_balances but not pre.
    meta = tx.transaction.meta
    pre_mints = {b.mint for b in (meta.pre_token_balances or [])}
    post_mints = {b.mint for b in (meta.post_token_balances or [])}
    candidates = [m for m in post_mints if m != SOL_MINT]
    # Prefer mints that are *new* in this tx
    new_mints = [m for m in candidates if m not in pre_mints]
    chosen = new_mints[0] if new_mints else (candidates[0] if candidates else None)
    if not chosen:
        return None

    return NewTokenSignal(
        kind="new",
        mint=chosen,
        source=source,
        sig=evt.signature,
        slot=evt.slot,
        detected_at=time.time(),
        age_ms=(time.time() - evt.received_at) * 1000.0,
    )
