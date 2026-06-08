"""Address Lookup Table (ALT) cache.

Versioned transactions reference ALTs by address; the network expects the
sender to also provide the actual table contents (a vector of addresses)
so it can resolve the compact references in the message. We fetch each
ALT account once via `getAccountInfo` and cache the parsed
`AddressLookupTableAccount` in memory until process exit.

The ALT account layout is fixed; solders provides the parser.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from solders.address_lookup_table_account import (
    AddressLookupTable,
    AddressLookupTableAccount,
)
from solders.pubkey import Pubkey

from solalpha.foundation.errors import DecodeError, RpcError
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from solalpha.data.rpc_pool import RpcPool

_log = get_logger(__name__)


class AltManager:
    """In-memory ALT cache keyed by base58 address."""

    def __init__(self, rpc: RpcPool) -> None:
        self._rpc = rpc
        self._cache: dict[str, AddressLookupTableAccount] = {}

    async def get(self, address: str) -> AddressLookupTableAccount:
        cached = self._cache.get(address)
        if cached is not None:
            return cached
        result = await self._rpc.call(
            "getAccountInfo",
            [address, {"encoding": "base64", "commitment": "confirmed"}],
        )
        if not isinstance(result, dict):
            raise DecodeError(f"ALT {address}: unexpected getAccountInfo result")
        value = result.get("value")
        if value is None:
            raise DecodeError(f"ALT {address}: not found on-chain")
        data_field = value.get("data") if isinstance(value, dict) else None
        if not isinstance(data_field, list) or len(data_field) < 2:
            raise DecodeError(f"ALT {address}: unexpected data shape: {data_field!r}")
        try:
            raw = base64.b64decode(data_field[0])
        except (ValueError, TypeError) as e:
            raise DecodeError(f"ALT {address}: base64 decode: {e}") from e
        try:
            key = Pubkey.from_string(address)
            table = AddressLookupTable.deserialize(raw)
            account = AddressLookupTableAccount(key=key, addresses=list(table.addresses))
        except Exception as e:
            raise DecodeError(f"ALT {address}: parse failed: {e}") from e
        self._cache[address] = account
        return account

    async def get_many(self, addresses: list[str]) -> list[AddressLookupTableAccount]:
        out: list[AddressLookupTableAccount] = []
        for a in addresses:
            try:
                out.append(await self.get(a))
            except (DecodeError, RpcError) as e:
                _log.warning("alt_skip", address=a, exc=str(e))
        return out
