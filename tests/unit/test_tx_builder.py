"""TxBuilder: compile + sign versioned transactions (jupiter + raydium paths).

Uses real ``solders`` primitives (ephemeral keypairs) with a mocked RPC for
``getLatestBlockhash`` and a fake ALT manager. No network, no on-chain state.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from solders.compute_budget import set_compute_unit_limit
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

from solalpha.execution.base import SwapInstructions
from solalpha.execution.tx_builder import TxBuilder
from solalpha.foundation.clock import FakeClock
from solalpha.foundation.errors import BlockhashExpired, ExecutionFailed, RpcError

pytestmark = pytest.mark.unit

# A pubkey is a valid base58 32-byte value -> usable as a blockhash string.
VALID_BLOCKHASH = str(Keypair().pubkey())


class _Rpc:
    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc
        self.calls: list[tuple[str, Any]] = []

    async def call(self, method: str, params: Any) -> Any:
        self.calls.append((method, params))
        if self._exc is not None:
            raise self._exc
        return self._result


def _ok_rpc(blockhash: str = VALID_BLOCKHASH, last_valid: int = 1234) -> _Rpc:
    return _Rpc(result={"value": {"blockhash": blockhash, "lastValidBlockHeight": last_valid}})


class _Loader:
    def __init__(self, kp: Any) -> None:
        self._kp = kp

    def load_keypair(self) -> Any:
        return self._kp


class _Alt:
    def __init__(self, tables: list[Any] | None = None) -> None:
        self._tables = tables or []
        self.requested: list[str] | None = None

    async def get_many(self, addrs: list[str]) -> list[Any]:
        self.requested = list(addrs)
        return list(self._tables)


def _jupiter_ix(data: bytes = b"\x01\x02\x03") -> str:
    return json.dumps(
        {
            "programId": str(Keypair().pubkey()),
            "accounts": [
                {"pubkey": str(Keypair().pubkey()), "isSigner": False, "isWritable": True}
            ],
            "data": base64.b64encode(data).decode("ascii"),
        }
    )


def _builder(kp: Any, rpc: _Rpc, alt: _Alt | None = None) -> TxBuilder:
    return TxBuilder(rpc, FakeClock(), _Loader(kp), alt or _Alt())


async def test_build_jupiter_signs_and_carries_metadata() -> None:
    kp = Keypair()
    alt = _Alt()
    ix = SwapInstructions(
        venue="jupiter",
        setup_instructions=(_jupiter_ix(),),
        swap_instruction=_jupiter_ix(),
        cleanup_instructions=(_jupiter_ix(),),
        address_lookup_tables=(),
        compute_unit_limit=250_000,
    )
    built = await _builder(kp, _ok_rpc(), alt).build(ix=ix, priority_fee_microlamports=5_000)
    assert built.blockhash == VALID_BLOCKHASH
    assert built.last_valid_block_height == 1234
    assert built.compute_unit_limit == 250_000
    assert built.compute_unit_price_microlamports == 5_000
    tx = VersionedTransaction.from_bytes(base64.b64decode(built.wire_b64))
    assert tx.message.recent_blockhash == Hash.from_string(VALID_BLOCKHASH)
    assert len(tx.signatures) >= 1
    assert alt.requested == []


async def test_build_jupiter_requests_declared_alts() -> None:
    kp = Keypair()
    alt = _Alt()
    ix = SwapInstructions(
        venue="jupiter",
        swap_instruction=_jupiter_ix(),
        address_lookup_tables=("ALTaddr1", "ALTaddr2"),
    )
    await _builder(kp, _ok_rpc(), alt).build(ix=ix, priority_fee_microlamports=1)
    assert alt.requested == ["ALTaddr1", "ALTaddr2"]


async def test_build_jupiter_default_cu_limit_when_none() -> None:
    kp = Keypair()
    ix = SwapInstructions(venue="jupiter", swap_instruction=_jupiter_ix(), compute_unit_limit=None)
    built = await _builder(kp, _ok_rpc()).build(ix=ix, priority_fee_microlamports=1)
    assert built.compute_unit_limit == 400_000  # configured default


async def test_build_raydium_resigns_prebuilt_tx() -> None:
    kp = Keypair()
    bh = Hash.from_string(VALID_BLOCKHASH)
    msg = MessageV0.try_compile(kp.pubkey(), [set_compute_unit_limit(123_456)], [], bh)
    prebuilt = VersionedTransaction(msg, [kp])
    wire = base64.b64encode(bytes(prebuilt)).decode("ascii")
    ix = SwapInstructions(venue="raydium", swap_instruction=wire, compute_unit_limit=None)
    built = await _builder(kp, _ok_rpc()).build(ix=ix, priority_fee_microlamports=9_999)
    tx = VersionedTransaction.from_bytes(base64.b64decode(built.wire_b64))
    assert tx.message.recent_blockhash == bh
    assert built.compute_unit_limit == 400_000


async def test_build_raydium_decode_failure() -> None:
    kp = Keypair()
    ix = SwapInstructions(venue="raydium", swap_instruction="@@@not-base64@@@")
    with pytest.raises(ExecutionFailed):
        await _builder(kp, _ok_rpc()).build(ix=ix, priority_fee_microlamports=1)


async def test_build_raydium_sign_failure_wrong_signer() -> None:
    payer = Keypair()
    other = Keypair()
    bh = Hash.from_string(VALID_BLOCKHASH)
    msg = MessageV0.try_compile(payer.pubkey(), [set_compute_unit_limit(1)], [], bh)
    wire = base64.b64encode(bytes(VersionedTransaction(msg, [payer]))).decode("ascii")
    ix = SwapInstructions(venue="raydium", swap_instruction=wire)
    with pytest.raises(ExecutionFailed):
        await _builder(other, _ok_rpc()).build(ix=ix, priority_fee_microlamports=1)


async def test_load_keypair_non_keypair_raises() -> None:
    ix = SwapInstructions(venue="jupiter", swap_instruction=_jupiter_ix())
    with pytest.raises(ExecutionFailed):
        await _builder("i-am-not-a-keypair", _ok_rpc()).build(ix=ix, priority_fee_microlamports=1)


async def test_blockhash_rpc_error_raises_blockhash_expired() -> None:
    kp = Keypair()
    ix = SwapInstructions(venue="jupiter", swap_instruction=_jupiter_ix())
    with pytest.raises(BlockhashExpired):
        await _builder(kp, _Rpc(exc=RpcError("rpc down"))).build(
            ix=ix, priority_fee_microlamports=1
        )


@pytest.mark.parametrize(
    "result",
    [
        "not-a-dict",
        {"value": "not-a-dict"},
        {"value": {"blockhash": 123, "lastValidBlockHeight": 5}},
        {"value": {"blockhash": "abc", "lastValidBlockHeight": "not-int"}},
        {"value": {}},
    ],
)
async def test_blockhash_bad_shape_raises(result: Any) -> None:
    kp = Keypair()
    ix = SwapInstructions(venue="jupiter", swap_instruction=_jupiter_ix())
    with pytest.raises(BlockhashExpired):
        await _builder(kp, _Rpc(result=result)).build(ix=ix, priority_fee_microlamports=1)


async def test_jupiter_ix_non_json_descriptor_raises() -> None:
    kp = Keypair()
    ix = SwapInstructions(venue="jupiter", swap_instruction="bm90LWpzb24=")  # no leading '{'
    with pytest.raises(ExecutionFailed):
        await _builder(kp, _ok_rpc()).build(ix=ix, priority_fee_microlamports=1)


async def test_jupiter_ix_malformed_json_raises() -> None:
    kp = Keypair()
    ix = SwapInstructions(venue="jupiter", swap_instruction='{"programId": "x", ')
    with pytest.raises(ExecutionFailed):
        await _builder(kp, _ok_rpc()).build(ix=ix, priority_fee_microlamports=1)


async def test_jupiter_ix_bad_program_id_raises() -> None:
    kp = Keypair()
    ix = SwapInstructions(
        venue="jupiter",
        swap_instruction=json.dumps({"programId": 123, "accounts": [], "data": ""}),
    )
    with pytest.raises(ExecutionFailed):
        await _builder(kp, _ok_rpc()).build(ix=ix, priority_fee_microlamports=1)
