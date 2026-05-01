"""Helius endpoint factory."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HeliusEndpoint:
    name: str
    http_url: str
    ws_url: str
    priority: int = 1
    max_subs_per_ws: int = 50
    enhanced_tx_url: str = ""


def make(api_key: str, name: str = "helius", priority: int = 1) -> HeliusEndpoint:
    if not api_key:
        return HeliusEndpoint(name=name, http_url="", ws_url="", priority=priority,
                              max_subs_per_ws=50, enhanced_tx_url="")
    return HeliusEndpoint(
        name=name,
        http_url=f"https://mainnet.helius-rpc.com/?api-key={api_key}",
        ws_url=f"wss://mainnet.helius-rpc.com/?api-key={api_key}",
        priority=priority,
        max_subs_per_ws=50,
        enhanced_tx_url=f"https://api.helius.xyz/v0/addresses/{{address}}/transactions?api-key={api_key}",
    )
