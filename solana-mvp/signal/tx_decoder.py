"""Parse Helius enhanced-tx JSON into a WalletEvent. LRU dedupe cache."""
from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Optional

from core.types import Side, WalletEvent

log = logging.getLogger("tx_decoder")

WSOL = "So11111111111111111111111111111111111111112"


class _LRU:
    def __init__(self, cap: int = 5_000):
        self.cap = cap
        self._d: OrderedDict[str, Any] = OrderedDict()

    def get(self, k: str) -> Any:
        v = self._d.get(k)
        if v is not None:
            self._d.move_to_end(k)
        return v

    def set(self, k: str, v: Any) -> None:
        self._d[k] = v
        self._d.move_to_end(k)
        if len(self._d) > self.cap:
            self._d.popitem(last=False)

    def __contains__(self, k: str) -> bool:
        return k in self._d


_decode_cache = _LRU(5_000)


def _amount_from_token_field(field: dict) -> float:
    if not isinstance(field, dict):
        return 0.0
    raw = field.get("rawTokenAmount") or {}
    try:
        amt = float(raw.get("tokenAmount", 0))
        decimals = int(raw.get("decimals", 9) or 9)
        return amt / (10 ** decimals) if decimals > 0 else amt
    except (TypeError, ValueError):
        return 0.0


def decode_swap(tx_json: dict, wallet: str) -> Optional[WalletEvent]:
    """Returns a WalletEvent if `wallet` performed a swap in this tx, else None."""
    if not tx_json or not isinstance(tx_json, dict):
        return None
    sig = tx_json.get("signature", "")
    cache_key = f"{sig}:{wallet}"
    cached = _decode_cache.get(cache_key)
    if cached is not None:
        return cached if isinstance(cached, WalletEvent) else None

    events = tx_json.get("events") or {}
    swap = events.get("swap") if isinstance(events, dict) else None
    if not swap:
        _decode_cache.set(cache_key, "miss")
        return None

    ts = float(tx_json.get("timestamp", 0) or 0)
    slot = int(tx_json.get("slot", 0) or 0)

    side: Optional[Side] = None
    mint: Optional[str] = None
    token_amount: float = 0.0
    sol_amount: float = 0.0

    token_outputs = swap.get("tokenOutputs") or []
    for tio in token_outputs:
        if not isinstance(tio, dict):
            continue
        if tio.get("userAccount") == wallet:
            m = tio.get("mint")
            if m and m != WSOL:
                side = Side.BUY
                mint = m
                token_amount = _amount_from_token_field(tio)
                break
    if side is None:
        token_inputs = swap.get("tokenInputs") or []
        for tii in token_inputs:
            if not isinstance(tii, dict):
                continue
            if tii.get("userAccount") == wallet:
                m = tii.get("mint")
                if m and m != WSOL:
                    side = Side.SELL
                    mint = m
                    token_amount = _amount_from_token_field(tii)
                    break

    if side is None or not mint:
        _decode_cache.set(cache_key, "miss")
        return None

    native_in = swap.get("nativeInput") or {}
    native_out = swap.get("nativeOutput") or {}
    if isinstance(native_in, dict) and native_in.get("account") == wallet:
        try:
            sol_amount = float(native_in.get("amount", 0)) / 1e9
        except (TypeError, ValueError):
            sol_amount = 0.0
    if isinstance(native_out, dict) and native_out.get("account") == wallet:
        try:
            sol_amount = float(native_out.get("amount", 0)) / 1e9
        except (TypeError, ValueError):
            sol_amount = 0.0

    price_sol = (sol_amount / token_amount) if token_amount > 0 else 0.0
    if sol_amount <= 0:
        _decode_cache.set(cache_key, "miss")
        return None

    ev = WalletEvent(
        ts=ts,
        slot=slot,
        signature=sig,
        wallet=wallet,
        mint=mint,
        side=side,
        sol_amount=sol_amount,
        token_amount=token_amount,
        price_sol=price_sol,
    )
    _decode_cache.set(cache_key, ev)
    return ev


def cache_size() -> int:
    return len(_decode_cache._d)


def cache_clear() -> None:
    _decode_cache._d.clear()
