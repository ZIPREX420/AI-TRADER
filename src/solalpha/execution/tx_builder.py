"""Versioned-tx builder for the live executor.

Two input shapes:
  * Jupiter `/swap-instructions` -- a JSON list of `{programId, accounts, data}`
    descriptors. We decode each one into a `solders.Instruction`, prepend
    `compute_budget` instructions (limit + price), compile a `MessageV0`,
    sign with the keypair, and return the serialized base64 transaction.
  * Raydium `/transaction/swap-base-in` -- a pre-built v0 transaction. We
    deserialize, optionally replace the blockhash, sign, and re-serialize.

Both paths return a `BuiltTx` carrying the base64-encoded signed wire bytes
and the latest blockhash + last-valid-slot so the confirmer can detect
expiry.

The builder never logs key material; the only thing that touches the
keypair bytes is `KeypairLoader.load_keypair()` (see
`foundation/secrets.py`).
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from solalpha.foundation.errors import (
    BlockhashExpired,
    ExecutionFailed,
    RpcError,
)
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from solalpha.data.rpc_pool import RpcPool
    from solalpha.execution.alt_manager import AltManager
    from solalpha.execution.base import SwapInstructions
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.secrets import KeypairLoader


_log = get_logger(__name__)


class BuiltTx(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    wire_b64: str
    blockhash: str
    last_valid_block_height: int
    compute_unit_limit: int
    compute_unit_price_microlamports: int


class TxBuilder:
    """Build + sign a `VersionedTransaction` from venue swap instructions."""

    def __init__(
        self,
        rpc: RpcPool,
        clock: Clock,
        keypair_loader: KeypairLoader,
        alt_manager: AltManager,
        *,
        compute_unit_limit: int = 400_000,
    ) -> None:
        self._rpc = rpc
        self._clock = clock
        self._keypair_loader = keypair_loader
        self._alt = alt_manager
        self._cu_limit = compute_unit_limit

    async def build(
        self,
        *,
        ix: SwapInstructions,
        priority_fee_microlamports: int,
    ) -> BuiltTx:
        keypair = self._load_keypair()
        blockhash_hex, last_valid = await self._latest_blockhash()
        if ix.venue == "jupiter":
            wire = await self._build_jupiter(
                ix=ix,
                keypair=keypair,
                blockhash=blockhash_hex,
                priority_fee=priority_fee_microlamports,
            )
        else:
            wire = await self._build_raydium(
                ix=ix,
                keypair=keypair,
                priority_fee=priority_fee_microlamports,
            )
        return BuiltTx(
            wire_b64=wire,
            blockhash=blockhash_hex,
            last_valid_block_height=last_valid,
            compute_unit_limit=ix.compute_unit_limit or self._cu_limit,
            compute_unit_price_microlamports=priority_fee_microlamports,
        )

    # ---- jupiter path ----

    async def _build_jupiter(
        self,
        *,
        ix: SwapInstructions,
        keypair: Keypair,
        blockhash: str,
        priority_fee: int,
    ) -> str:
        instructions: list[Instruction] = [
            set_compute_unit_limit(ix.compute_unit_limit or self._cu_limit),
            set_compute_unit_price(priority_fee),
        ]
        for raw in ix.setup_instructions:
            instructions.append(_decode_jupiter_ix(raw))
        instructions.append(_decode_jupiter_ix(ix.swap_instruction))
        for raw in ix.cleanup_instructions:
            instructions.append(_decode_jupiter_ix(raw))
        alts = await self._alt.get_many(list(ix.address_lookup_tables))
        try:
            msg = MessageV0.try_compile(
                keypair.pubkey(),
                instructions,
                alts,
                Hash.from_string(blockhash),
            )
        except Exception as e:
            raise ExecutionFailed(f"message compile failed: {e}") from e
        tx = VersionedTransaction(msg, [keypair])
        return base64.b64encode(bytes(tx)).decode("ascii")

    # ---- raydium path ----

    async def _build_raydium(
        self,
        *,
        ix: SwapInstructions,
        keypair: Keypair,
        priority_fee: int,
    ) -> str:
        # Raydium returns a fully built (unsigned) v0 transaction. We
        # deserialize, replace the signer, sign, and re-serialize. The
        # compute-budget instructions are already embedded by Raydium when
        # we set `computeUnitPriceMicroLamports`.
        del priority_fee  # already embedded in the raydium response
        try:
            raw = base64.b64decode(ix.swap_instruction)
            tx_unsigned = VersionedTransaction.from_bytes(raw)
        except Exception as e:
            raise ExecutionFailed(f"raydium tx decode failed: {e}") from e
        try:
            tx_signed = VersionedTransaction(tx_unsigned.message, [keypair])
        except Exception as e:
            raise ExecutionFailed(f"raydium tx sign failed: {e}") from e
        return base64.b64encode(bytes(tx_signed)).decode("ascii")

    # ---- helpers ----

    def _load_keypair(self) -> Keypair:
        # `KeypairLoader.load_keypair()` returns a `solders.keypair.Keypair`
        # under our `foundation/secrets.py` contract.
        kp = self._keypair_loader.load_keypair()
        if not isinstance(kp, Keypair):
            raise ExecutionFailed(f"keypair loader returned non-Keypair: {type(kp).__name__}")
        return kp

    async def _latest_blockhash(self) -> tuple[str, int]:
        try:
            res = await self._rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])
        except RpcError as e:
            raise BlockhashExpired(f"latest blockhash unavailable: {e}") from e
        if not isinstance(res, dict):
            raise BlockhashExpired(f"latest blockhash bad shape: {res!r}")
        value = res.get("value")
        if not isinstance(value, dict):
            raise BlockhashExpired(f"latest blockhash no value: {res!r}")
        bh = value.get("blockhash")
        last_valid = value.get("lastValidBlockHeight")
        if not isinstance(bh, str) or not isinstance(last_valid, int):
            raise BlockhashExpired(f"latest blockhash fields: {value!r}")
        return bh, last_valid


def _decode_jupiter_ix(raw: str) -> Instruction:
    """Decode one Jupiter `{programId, accounts, data}` JSON descriptor."""
    try:
        obj = json.loads(raw) if raw.startswith("{") else None
    except json.JSONDecodeError as e:
        raise ExecutionFailed(f"jupiter ix non-JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ExecutionFailed("jupiter ix not a dict")
    pid = obj.get("programId")
    accounts_raw = obj.get("accounts", []) or []
    data_b64 = obj.get("data", "")
    if not isinstance(pid, str) or not isinstance(accounts_raw, list):
        raise ExecutionFailed(f"jupiter ix shape: {obj!r}")
    metas: list[AccountMeta] = []
    for a in accounts_raw:
        if not isinstance(a, dict):
            continue
        pk = a.get("pubkey")
        if not isinstance(pk, str):
            continue
        metas.append(
            AccountMeta(
                pubkey=Pubkey.from_string(pk),
                is_signer=bool(a.get("isSigner", False)),
                is_writable=bool(a.get("isWritable", False)),
            )
        )
    try:
        data = base64.b64decode(data_b64) if data_b64 else b""
    except (ValueError, TypeError) as e:
        raise ExecutionFailed(f"jupiter ix data b64: {e}") from e
    return Instruction(program_id=Pubkey.from_string(pid), accounts=metas, data=data)


__all__ = ["BuiltTx", "TxBuilder"]
