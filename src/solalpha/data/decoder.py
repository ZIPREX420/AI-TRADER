"""Transaction decoder: raw RPC tx response -> list[NormalizedSwap].

The decoder dispatches by the *outermost* program invocation in a tx:
  * Jupiter v6 routing -> `JupiterV6Decoder`
  * Direct Raydium v4   -> `RaydiumV4Decoder`
  * Direct Orca         -> `OrcaDecoder`
  * pump.fun bonding    -> `PumpFunDecoder`

All four sub-decoders share `BalanceDiffDecoder`: they derive the swap
quantities from the tx's `meta.preTokenBalances` / `meta.postTokenBalances`
diff for the signer wallet. This is venue-agnostic (works whether the swap
flows through Jupiter or hits the AMM directly) and degrades gracefully
when an inner-instruction layout we don't know about appears -- we still
get the wallet, mint, side, and amounts. Pool address extraction is
best-effort and may be None for Phase 2.

Stable-quote mints (WSOL / USDC / USDT) are recognized so `side` and the
"interesting" `mint` are assigned consistently.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from solalpha.data.program_ids import (
    DECODABLE_PROGRAMS,
    PROGRAM_JUPITER_V6,
    PROGRAM_ORCA_WHIRLPOOL,
    PROGRAM_PUMPFUN,
    PROGRAM_RAYDIUM_AMM_V4,
)
from solalpha.domain import NormalizedSwap, SwapVenue
from solalpha.foundation import metrics
from solalpha.foundation.errors import DecodeError, UnknownProgramError
from solalpha.foundation.ids import deterministic_event_id
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from solalpha.foundation.clock import Clock

_log = get_logger(__name__)

# Quote mints we recognise. Used to pick the *interesting* side of a swap.
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KConKy6XQVHCu6cLmExt"
QUOTE_MINTS: frozenset[str] = frozenset({WSOL_MINT, USDC_MINT, USDT_MINT})


class TransactionDecoder:
    """Dispatch a parsed `getTransaction` payload to a venue-specific decoder."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._sub: dict[str, _SubDecoder] = {
            PROGRAM_JUPITER_V6: _SubDecoder("jupiter", PROGRAM_JUPITER_V6),
            PROGRAM_RAYDIUM_AMM_V4: _SubDecoder("raydium", PROGRAM_RAYDIUM_AMM_V4),
            PROGRAM_ORCA_WHIRLPOOL: _SubDecoder("orca", PROGRAM_ORCA_WHIRLPOOL),
            PROGRAM_PUMPFUN: _SubDecoder("pumpfun", PROGRAM_PUMPFUN),
        }

    def decode(self, tx_response: dict[str, Any]) -> list[NormalizedSwap]:
        """Return zero or more swaps. Never raises on unknown programs."""
        try:
            signature, slot, block_time, programs, meta, signer = _extract_top_level(
                tx_response
            )
        except DecodeError as e:
            metrics.DECODER_ERRORS.labels(program="*").inc()
            _log.warning("decode_top_level_failed", exc=str(e))
            return []
        # Choose which sub-decoder runs based on the outermost
        # decodable program invoked. If none are present, we skip.
        chosen: _SubDecoder | None = None
        for pid in programs:
            sub = self._sub.get(pid)
            if sub is not None:
                chosen = sub
                break
            if pid not in DECODABLE_PROGRAMS:
                metrics.DECODER_UNKNOWN.labels(program_id=pid).inc()
        if chosen is None:
            return []
        try:
            diffs = _balance_diffs(meta, signer)
            swap = _diff_to_swap(
                venue=chosen.venue,
                signer=signer,
                signature=signature,
                slot=slot,
                block_time=block_time,
                diffs=diffs,
                clock=self._clock,
            )
        except UnknownProgramError:
            raise
        except DecodeError as e:
            metrics.DECODER_ERRORS.labels(program=chosen.venue).inc()
            _log.warning("decode_failed", program=chosen.venue, exc=str(e))
            return []
        if swap is None:
            return []
        metrics.SWAPS_NORMALIZED.labels(program=chosen.venue).inc()
        return [swap]


class _SubDecoder:
    """A program-id -> venue-label binding. Trivial today; richer later."""

    __slots__ = ("program_id", "venue")

    def __init__(self, venue: SwapVenue, program_id: str) -> None:
        self.venue = venue
        self.program_id = program_id


# ---- helpers ----


def _extract_top_level(
    tx: dict[str, Any],
) -> tuple[str, int, datetime, list[str], dict[str, Any], str]:
    """Pull the bits we need from a `getTransaction` result.

    Returns (signature, slot, block_time_utc, top_level_program_ids,
    meta_dict, signer_pubkey).
    """
    txn = tx.get("transaction")
    meta = tx.get("meta")
    slot = tx.get("slot")
    block_time = tx.get("blockTime")
    if not isinstance(txn, dict) or not isinstance(meta, dict):
        raise DecodeError("missing transaction or meta")
    if not isinstance(slot, int):
        raise DecodeError(f"missing or non-integer slot: {slot!r}")
    sigs = txn.get("signatures")
    if not isinstance(sigs, list) or not sigs:
        raise DecodeError("transaction has no signatures")
    signature = str(sigs[0])
    if block_time is None:
        bt = datetime.fromtimestamp(0, UTC)
    else:
        bt = datetime.fromtimestamp(int(block_time), UTC)
    msg = txn.get("message")
    if not isinstance(msg, dict):
        raise DecodeError("missing transaction.message")
    account_keys = msg.get("accountKeys")
    instructions = msg.get("instructions")
    if not isinstance(account_keys, list) or not isinstance(instructions, list):
        raise DecodeError("message.accountKeys or .instructions missing")
    if not account_keys:
        raise DecodeError("empty accountKeys")
    # `accountKeys` may be list[str] (jsonParsed `false`) or list[dict] (`true`).
    signer = _account_key(account_keys[0])
    keys = [_account_key(k) for k in account_keys]
    programs: list[str] = []
    for ix in instructions:
        if not isinstance(ix, dict):
            continue
        pid = ix.get("programId")
        if isinstance(pid, str) and pid:
            programs.append(pid)
            continue
        idx = ix.get("programIdIndex")
        if isinstance(idx, int) and 0 <= idx < len(keys):
            programs.append(keys[idx])
    return signature, slot, bt, programs, meta, signer


def _account_key(key: Any) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, dict):
        pk = key.get("pubkey")
        if isinstance(pk, str):
            return pk
    raise DecodeError(f"unexpected account key shape: {key!r}")


def _sum_owned_by_signer(entries: list[Any], signer: str) -> dict[str, int]:
    """Sum raw `uiTokenAmount`s per mint for the `signer`-owned entries.

    Shared by the pre- and post-balance passes in `_balance_diffs`: an entry
    contributes to its mint's running total only when it is a dict owned by
    `signer` with a string mint and a parseable amount. Anything else is
    skipped, so a malformed entry can never corrupt the diff.
    """
    totals: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("owner") != signer:
            continue
        mint = entry.get("mint")
        amount = _ui_amount_raw(entry)
        if isinstance(mint, str) and amount is not None:
            totals[mint] = totals.get(mint, 0) + amount
    return totals


def _balance_diffs(
    meta: dict[str, Any], signer: str
) -> dict[str, _Diff]:
    """Aggregate signer-owned token balance deltas by mint."""
    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []
    if not isinstance(pre, list) or not isinstance(post, list):
        return {}
    pre_map = _sum_owned_by_signer(pre, signer)
    post_map = _sum_owned_by_signer(post, signer)
    diffs: dict[str, _Diff] = {}
    for mint in set(pre_map) | set(post_map):
        diff = post_map.get(mint, 0) - pre_map.get(mint, 0)
        diffs[mint] = _Diff(mint=mint, delta=diff)
    return diffs


def _ui_amount_raw(entry: dict[str, Any]) -> int | None:
    ta = entry.get("uiTokenAmount")
    if not isinstance(ta, dict):
        return None
    amt = ta.get("amount")
    if isinstance(amt, str):
        try:
            return int(amt)
        except ValueError:
            return None
    if isinstance(amt, int):
        return amt
    return None


class _Diff:
    __slots__ = ("delta", "mint")

    def __init__(self, mint: str, delta: int) -> None:
        self.mint = mint
        self.delta = delta


def _diff_to_swap(
    *,
    venue: SwapVenue,
    signer: str,
    signature: str,
    slot: int,
    block_time: datetime,
    diffs: dict[str, _Diff],
    clock: Clock,
) -> NormalizedSwap | None:
    """Pick the alpha mint + side from the balance diff."""
    positives = [d for d in diffs.values() if d.delta > 0]
    negatives = [d for d in diffs.values() if d.delta < 0]
    if not positives or not negatives:
        return None
    # Pick the non-quote side as the "interesting" mint.
    pos_alpha = next((d for d in positives if d.mint not in QUOTE_MINTS), positives[0])
    neg_alpha = next((d for d in negatives if d.mint not in QUOTE_MINTS), negatives[0])
    # If both ends are alpha (i.e. a token-token trade), prefer the positive
    # side as the buy direction; this is unusual but well-defined.
    if pos_alpha.mint not in QUOTE_MINTS:
        mint = pos_alpha.mint
        side = "buy"
    elif neg_alpha.mint not in QUOTE_MINTS:
        mint = neg_alpha.mint
        side = "sell"
    else:
        # Pure quote-quote swap (e.g. SOL <-> USDC arbitrage). Skip --
        # nothing alpha-relevant.
        return None
    # The in/out legs are the same in both directions: the positive balance
    # delta is what the wallet received, the negative what it spent. Only the
    # `mint`/`side` labels above depend on which side is the alpha token.
    out_mint = pos_alpha.mint
    out_amount = pos_alpha.delta
    in_mint = neg_alpha.mint
    in_amount = -neg_alpha.delta
    if in_amount <= 0 or out_amount <= 0:
        return None
    return NormalizedSwap(
        event_id=deterministic_event_id(signature, slot, 0),
        signature=signature,
        slot=slot,
        block_time=block_time,
        venue=venue,
        wallet=signer,
        mint=mint,
        side=side,
        input_mint=in_mint,
        output_mint=out_mint,
        input_amount_raw=in_amount,
        output_amount_raw=out_amount,
        received_at=clock.now(),
    )


__all__ = [
    "QUOTE_MINTS",
    "USDC_MINT",
    "USDT_MINT",
    "WSOL_MINT",
    "TransactionDecoder",
]
