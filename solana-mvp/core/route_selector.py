"""Pure decision: Jupiter vs Raydium quote selection."""
from __future__ import annotations

from typing import Optional

from .types import Quote, Side


def choose(
    jupiter: Optional[Quote],
    raydium: Optional[Quote],
    side: Side,
    max_pi_buy: float = 0.04,
    max_pi_sell: float = 0.08,
) -> Optional[Quote]:
    """Pick the route with valid price_impact_pct ≤ threshold. Prefer Jupiter on tie."""
    cap = max_pi_buy if side == Side.BUY else max_pi_sell

    def _valid(q: Optional[Quote]) -> bool:
        return q is not None and q.out_amount > 0 and q.price_impact_pct <= cap

    j_ok = _valid(jupiter)
    r_ok = _valid(raydium)
    if j_ok and r_ok:
        # both valid → prefer Jupiter (better routing) unless raydium gives ≥5% more out
        assert jupiter is not None and raydium is not None
        if raydium.out_amount > jupiter.out_amount * 1.05:
            return raydium
        return jupiter
    if j_ok:
        return jupiter
    if r_ok:
        return raydium
    return None
