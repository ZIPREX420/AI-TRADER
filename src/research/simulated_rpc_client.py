"""Minimal RPC stand-in that serves token decimals + get_token_supply from a snapshot dict.

Lets v1 modules (rug_filter, position_manager) run inside replay without hitting Solana.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Resp:
    value: Any


@dataclass
class SnapshotMint:
    decimals: int = 9
    supply_raw: int = 1_000_000_000_000
    mint_authority: str | None = None
    freeze_authority: str | None = None
    top10_pct: float = 0.10
    largest_accounts: list[Any] = field(default_factory=list)


class SimulatedRpcClient:
    def __init__(self, mints: dict[str, SnapshotMint] | None = None):
        self.mints: dict[str, SnapshotMint] = mints or {}

    def add_mint(self, mint: str, snap: SnapshotMint):
        self.mints[mint] = snap

    async def get_token_supply(self, pubkey):
        m = self.mints.get(str(pubkey))
        if m is None:
            return _Resp(value=type("S", (), {"amount": "0", "decimals": 9})())
        return _Resp(value=type("S", (), {"amount": str(m.supply_raw), "decimals": m.decimals})())

    async def get_account_info_json_parsed(self, pubkey):
        m = self.mints.get(str(pubkey))
        if m is None:
            return _Resp(value=None)
        info = {"info": {
            "mintAuthority": m.mint_authority, "freezeAuthority": m.freeze_authority,
            "supply": str(m.supply_raw), "decimals": m.decimals,
        }}
        return _Resp(value=type("V", (), {"data": type("D", (), {"parsed": info})()})())

    async def get_token_largest_accounts(self, pubkey):
        m = self.mints.get(str(pubkey))
        accs = m.largest_accounts if m else []
        return _Resp(value=accs)

    async def get_balance(self, pubkey):
        return _Resp(value=int(0.5 * 1e9))   # default 0.5 SOL

    async def get_signature_statuses(self, *args, **kwargs):
        # Confirm immediately in sim
        return _Resp(value=[type("S", (), {"confirmation_status": "confirmed", "err": None})()])

    async def get_latest_blockhash(self):
        return _Resp(value=type("V", (), {"blockhash": "0" * 44})())

    async def close(self):
        pass

    async def get_version(self):
        return _Resp(value=type("V", (), {"solana_core": "sim"})())

    async def get_slot(self):
        return _Resp(value=0)
