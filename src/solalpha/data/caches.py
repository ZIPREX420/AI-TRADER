"""On-chain metadata caches: mint, pool, ATA owner.

Each cache is a small in-memory LRU backed by the matching SQLite table:
  * `MintMetadataCache`  -> `cache_mint_metadata`
  * `PoolCache`          -> `cache_pool`
  * `AtaOwnerCache`      -> `cache_ata_owner`

The mint cache is the only fetch-through cache: a miss calls
`getAccountInfo` on the mint and parses the 82-byte SPL Token Mint layout to
extract `decimals`, `mint_authority`, and `freeze_authority`. The risk
engine consults `has_freeze_authority` / `has_mint_authority` before
approving a buy -- both are red flags for rug-able tokens.

`PoolCache` and `AtaOwnerCache` are write-through stores populated by the
decoder and execution plane as they observe new pools / ATAs.
"""

from __future__ import annotations

import base64
from collections import OrderedDict
from datetime import datetime
from typing import TYPE_CHECKING

import base58
from pydantic import BaseModel, ConfigDict

from solalpha.foundation.errors import DecodeError
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from solalpha.data.rpc_pool import RpcPool
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.state import SqliteStore

_log = get_logger(__name__)

# SPL Token Mint account layout: 82 bytes total.
#   0..4    mint_authority_option (u32 LE; 0=None, 1=Some)
#   4..36   mint_authority (32 bytes pubkey)
#   36..44  supply (u64 LE)
#   44      decimals (u8)
#   45      is_initialized (u8)
#   46..50  freeze_authority_option (u32 LE)
#   50..82  freeze_authority (32 bytes pubkey)
_MINT_LAYOUT_LEN = 82


class MintMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    mint: str
    decimals: int
    symbol: str | None = None
    has_mint_authority: bool
    has_freeze_authority: bool
    updated_at: datetime


class PoolMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    pool: str
    program: str
    token_a: str
    token_b: str
    updated_at: datetime


class AtaOwner(BaseModel):
    model_config = ConfigDict(frozen=True)

    ata: str
    owner: str
    mint: str
    updated_at: datetime


class _LRU:
    """Order-preserving size-capped dict."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._data: OrderedDict[str, object] = OrderedDict()

    def get(self, key: str) -> object | None:
        v = self._data.get(key)
        if v is not None:
            self._data.move_to_end(key)
        return v

    def put(self, key: str, value: object) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        if len(self._data) > self._capacity:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)


class MintMetadataCache:
    """Fetch-through cache for SPL Token mint metadata."""

    def __init__(
        self,
        store: SqliteStore,
        rpc: RpcPool,
        clock: Clock,
        *,
        capacity: int = 4096,
    ) -> None:
        self._store = store
        self._rpc = rpc
        self._clock = clock
        self._lru = _LRU(capacity)

    async def get(self, mint: str) -> MintMetadata:
        cached = self._lru.get(mint)
        if isinstance(cached, MintMetadata):
            return cached
        row = await self._store.fetch_one(
            "SELECT * FROM cache_mint_metadata WHERE mint = ?", (mint,)
        )
        if row is not None:
            md = MintMetadata(
                mint=str(row["mint"]),
                decimals=int(row["decimals"]),
                symbol=str(row["symbol"]) if row.get("symbol") else None,
                has_mint_authority=bool(row["has_mint_authority"]),
                has_freeze_authority=bool(row["has_freeze_authority"]),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            self._lru.put(mint, md)
            return md
        md = await self._fetch_through(mint)
        await self._persist(md)
        self._lru.put(mint, md)
        return md

    async def _fetch_through(self, mint: str) -> MintMetadata:
        result = await self._rpc.call(
            "getAccountInfo",
            [mint, {"encoding": "base64", "commitment": "confirmed"}],
        )
        if not isinstance(result, dict) or result.get("value") is None:
            raise DecodeError(f"mint account not found: {mint}")
        value = result["value"]
        data_field = value.get("data")
        if not isinstance(data_field, list) or len(data_field) < 2:
            raise DecodeError(f"mint {mint}: unexpected data shape: {data_field!r}")
        raw_b64, encoding = data_field[0], data_field[1]
        if encoding != "base64":
            raise DecodeError(f"mint {mint}: expected base64 encoding, got {encoding!r}")
        try:
            raw = base64.b64decode(raw_b64)
        except (ValueError, TypeError) as e:
            raise DecodeError(f"mint {mint}: base64 decode failed: {e}") from e
        if len(raw) < _MINT_LAYOUT_LEN:
            raise DecodeError(
                f"mint {mint}: account data {len(raw)} bytes < {_MINT_LAYOUT_LEN}"
            )
        return self._parse_mint(mint, raw)

    @staticmethod
    def _parse_mint(mint: str, raw: bytes) -> MintMetadata:
        has_mint_auth = int.from_bytes(raw[0:4], "little") == 1
        # 4..36 mint_authority (only used to confirm presence)
        decimals = raw[44]
        has_freeze_auth = int.from_bytes(raw[46:50], "little") == 1
        return MintMetadata(
            mint=mint,
            decimals=int(decimals),
            symbol=None,
            has_mint_authority=has_mint_auth,
            has_freeze_authority=has_freeze_auth,
            updated_at=datetime.fromtimestamp(0),  # filled in by _persist
        )

    async def _persist(self, md: MintMetadata) -> None:
        now = self._clock.now()
        # Re-bind the timestamp the caller will see.
        md_out = md.model_copy(update={"updated_at": now})
        await self._store.execute(
            """
            INSERT INTO cache_mint_metadata (
                mint, decimals, symbol, has_freeze_authority, has_mint_authority, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(mint) DO UPDATE SET
                decimals = excluded.decimals,
                symbol = excluded.symbol,
                has_freeze_authority = excluded.has_freeze_authority,
                has_mint_authority = excluded.has_mint_authority,
                updated_at = excluded.updated_at
            """,
            (
                md_out.mint,
                md_out.decimals,
                md_out.symbol,
                1 if md_out.has_freeze_authority else 0,
                1 if md_out.has_mint_authority else 0,
                now.isoformat(),
            ),
        )
        # Mutate the LRU entry the caller will get next time -- the model is
        # frozen, so we replace the slot.
        self._lru.put(md_out.mint, md_out)


class PoolCache:
    """Write-through cache for AMM pool metadata."""

    def __init__(self, store: SqliteStore, clock: Clock, *, capacity: int = 4096) -> None:
        self._store = store
        self._clock = clock
        self._lru = _LRU(capacity)

    async def get(self, pool: str) -> PoolMeta | None:
        cached = self._lru.get(pool)
        if isinstance(cached, PoolMeta):
            return cached
        row = await self._store.fetch_one(
            "SELECT * FROM cache_pool WHERE pool = ?", (pool,)
        )
        if row is None:
            return None
        pm = PoolMeta(
            pool=str(row["pool"]),
            program=str(row["program"]),
            token_a=str(row["token_a"]),
            token_b=str(row["token_b"]),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )
        self._lru.put(pool, pm)
        return pm

    async def put(self, *, pool: str, program: str, token_a: str, token_b: str) -> PoolMeta:
        now = self._clock.now()
        pm = PoolMeta(pool=pool, program=program, token_a=token_a, token_b=token_b, updated_at=now)
        await self._store.execute(
            """
            INSERT INTO cache_pool (pool, program, token_a, token_b, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(pool) DO UPDATE SET
                program = excluded.program,
                token_a = excluded.token_a,
                token_b = excluded.token_b,
                updated_at = excluded.updated_at
            """,
            (pool, program, token_a, token_b, now.isoformat()),
        )
        self._lru.put(pool, pm)
        return pm


class AtaOwnerCache:
    """Write-through cache for SPL ATA -> (owner, mint) lookups."""

    def __init__(self, store: SqliteStore, clock: Clock, *, capacity: int = 8192) -> None:
        self._store = store
        self._clock = clock
        self._lru = _LRU(capacity)

    async def get(self, ata: str) -> AtaOwner | None:
        cached = self._lru.get(ata)
        if isinstance(cached, AtaOwner):
            return cached
        row = await self._store.fetch_one(
            "SELECT * FROM cache_ata_owner WHERE ata = ?", (ata,)
        )
        if row is None:
            return None
        ao = AtaOwner(
            ata=str(row["ata"]),
            owner=str(row["owner"]),
            mint=str(row["mint"]),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )
        self._lru.put(ata, ao)
        return ao

    async def put(self, *, ata: str, owner: str, mint: str) -> AtaOwner:
        now = self._clock.now()
        ao = AtaOwner(ata=ata, owner=owner, mint=mint, updated_at=now)
        await self._store.execute(
            """
            INSERT INTO cache_ata_owner (ata, owner, mint, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ata) DO UPDATE SET
                owner = excluded.owner,
                mint = excluded.mint,
                updated_at = excluded.updated_at
            """,
            (ata, owner, mint, now.isoformat()),
        )
        self._lru.put(ata, ao)
        return ao


def encode_b58(raw: bytes) -> str:
    """Base58-encode 32-byte pubkey bytes. Convenience wrapper used by sub-decoders."""
    return base58.b58encode(raw).decode("ascii")


__all__ = [
    "AtaOwner",
    "AtaOwnerCache",
    "MintMetadata",
    "MintMetadataCache",
    "PoolCache",
    "PoolMeta",
    "encode_b58",
]
