"""Convert smart-wallet log events into copy Signals by parsing transaction balance deltas."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from solders.signature import Signature

from .constants import SOL_MINT, SWAP_PROGRAMS
from .ingest import LogEvent

log = logging.getLogger("wallet_tracker")


@dataclass
class CopySignal:
    kind: str  # "copy"
    wallet: str
    mint: str
    direction: str  # "buy" | "sell"
    sol_amount: float
    token_amount: float
    sig: str
    slot: int
    detected_at: float
    age_ms: float  # ms from event received → signal emitted


def _is_swap_event(logs: list[str]) -> bool:
    return any(any(p in l for p in SWAP_PROGRAMS) for l in logs)


async def parse_swap(client, evt: LogEvent, wallet: str) -> Optional[CopySignal]:
    """Fetch tx, diff pre/post token balances for `wallet`, infer mint+direction.

    Returns CopySignal on a buy of a non-SOL token by `wallet`, else None.
    Sells are returned too (used for mirror-exit), but only when direction='sell'.
    """
    if not _is_swap_event(evt.logs):
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
    if tx is None:
        return None

    meta = tx.transaction.meta
    if meta is None or meta.err is not None:
        return None

    # Account keys (json parsed)
    msg = tx.transaction.transaction.message
    account_keys = [str(k.pubkey) if hasattr(k, "pubkey") else str(k) for k in msg.account_keys]
    try:
        wallet_idx = account_keys.index(wallet)
    except ValueError:
        return None

    pre_sol = (meta.pre_balances[wallet_idx] or 0) / 1e9
    post_sol = (meta.post_balances[wallet_idx] or 0) / 1e9
    sol_delta = post_sol - pre_sol  # negative = spent SOL = buy

    # Token balance diffs for this wallet
    pre_t = {(b.mint, b.owner): float(b.ui_token_amount.ui_amount or 0) for b in (meta.pre_token_balances or [])}
    post_t = {(b.mint, b.owner): float(b.ui_token_amount.ui_amount or 0) for b in (meta.post_token_balances or [])}
    keys = set(pre_t) | set(post_t)
    deltas: list[tuple[str, float]] = []
    for k in keys:
        if k[1] != wallet:
            continue
        d = post_t.get(k, 0.0) - pre_t.get(k, 0.0)
        if abs(d) > 1e-9 and k[0] != SOL_MINT:
            deltas.append((k[0], d))

    if not deltas:
        return None

    # Pick the largest absolute delta (multi-hop swaps may show multiple)
    mint, dt = max(deltas, key=lambda x: abs(x[1]))
    direction = "buy" if dt > 0 else "sell"
    # Filter: only fire on meaningful swaps
    if abs(sol_delta) < 0.005:  # < 0.005 SOL is dust / fee noise
        return None

    return CopySignal(
        kind="copy",
        wallet=wallet,
        mint=mint,
        direction=direction,
        sol_amount=abs(sol_delta),
        token_amount=abs(dt),
        sig=evt.signature,
        slot=evt.slot,
        detected_at=time.time(),
        age_ms=(time.time() - evt.received_at) * 1000.0,
    )
