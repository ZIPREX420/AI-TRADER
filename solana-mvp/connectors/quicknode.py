"""QuickNode endpoint factory."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class QuickNodeEndpoint:
    name: str
    http_url: str
    ws_url: str
    priority: int = 2
    max_subs_per_ws: int = 25


def make(http_url: str, ws_url: str, name: str = "quicknode", priority: int = 2) -> QuickNodeEndpoint:
    return QuickNodeEndpoint(
        name=name,
        http_url=http_url or "",
        ws_url=ws_url or "",
        priority=priority,
        max_subs_per_ws=25,
    )
