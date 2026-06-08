"""TransactionDecoder: venue dispatch + balance-diff swap derivation."""

from __future__ import annotations

import pytest

from solalpha.data.decoder import WSOL_MINT, TransactionDecoder
from solalpha.foundation.clock import FakeClock

pytestmark = pytest.mark.unit

ALPHA = "Alpha111111111111111111111111111111111111111"
USER = "User1111111111111111111111111111111111111111"
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _tx(
    *,
    program: str,
    pre_wsol: int,
    post_wsol: int,
    pre_alpha: int,
    post_alpha: int,
    signature: str = "sig",
    slot: int = 100,
) -> dict[str, object]:
    return {
        "slot": slot,
        "blockTime": 1715000000,
        "transaction": {
            "signatures": [signature],
            "message": {
                "accountKeys": [USER, ALPHA, WSOL_MINT, program],
                "instructions": [{"programId": program}],
            },
        },
        "meta": {
            "preTokenBalances": [
                {"owner": USER, "mint": WSOL_MINT, "uiTokenAmount": {"amount": str(pre_wsol)}},
                {"owner": USER, "mint": ALPHA, "uiTokenAmount": {"amount": str(pre_alpha)}},
            ],
            "postTokenBalances": [
                {"owner": USER, "mint": WSOL_MINT, "uiTokenAmount": {"amount": str(post_wsol)}},
                {"owner": USER, "mint": ALPHA, "uiTokenAmount": {"amount": str(post_alpha)}},
            ],
        },
    }


def test_jupiter_buy() -> None:
    dec = TransactionDecoder(FakeClock())
    swaps = dec.decode(
        _tx(program=JUP, pre_wsol=1_000_000_000, post_wsol=0, pre_alpha=0, post_alpha=5_000_000)
    )
    assert len(swaps) == 1
    s = swaps[0]
    assert s.venue == "jupiter"
    assert s.side == "buy"
    assert s.mint == ALPHA
    assert s.input_mint == WSOL_MINT
    assert s.output_mint == ALPHA
    assert s.input_amount_raw == 1_000_000_000
    assert s.output_amount_raw == 5_000_000
    assert s.wallet == USER


def test_jupiter_sell() -> None:
    dec = TransactionDecoder(FakeClock())
    swaps = dec.decode(
        _tx(program=JUP, pre_wsol=0, post_wsol=1_500_000_000, pre_alpha=5_000_000, post_alpha=0)
    )
    assert len(swaps) == 1
    assert swaps[0].side == "sell"
    assert swaps[0].mint == ALPHA


def test_unknown_program_yields_nothing() -> None:
    dec = TransactionDecoder(FakeClock())
    swaps = dec.decode(
        _tx(
            program="UnknownProgram111111111111111111111111111",
            pre_wsol=100,
            post_wsol=0,
            pre_alpha=0,
            post_alpha=50,
        )
    )
    assert swaps == []


def test_pure_quote_quote_yields_nothing() -> None:
    """USDC <-> WSOL arbitrage should not produce an alpha swap."""
    dec = TransactionDecoder(FakeClock())
    tx = {
        "slot": 1,
        "blockTime": 1715000000,
        "transaction": {
            "signatures": ["s"],
            "message": {
                "accountKeys": [USER, _USDC, WSOL_MINT, JUP],
                "instructions": [{"programId": JUP}],
            },
        },
        "meta": {
            "preTokenBalances": [
                {"owner": USER, "mint": WSOL_MINT, "uiTokenAmount": {"amount": "100"}},
                {"owner": USER, "mint": _USDC, "uiTokenAmount": {"amount": "0"}},
            ],
            "postTokenBalances": [
                {"owner": USER, "mint": WSOL_MINT, "uiTokenAmount": {"amount": "0"}},
                {"owner": USER, "mint": _USDC, "uiTokenAmount": {"amount": "100"}},
            ],
        },
    }
    assert dec.decode(tx) == []


def test_malformed_tx_yields_nothing() -> None:
    dec = TransactionDecoder(FakeClock())
    assert dec.decode({}) == []


def test_sum_owned_by_signer_filters_and_aggregates() -> None:
    """Extracted helper: owner filter + per-mint aggregation + skip malformed."""
    from solalpha.data.decoder import _sum_owned_by_signer

    entries = [
        {"owner": "S", "mint": "M", "uiTokenAmount": {"amount": "10"}},
        {"owner": "S", "mint": "M", "uiTokenAmount": {"amount": "5"}},
        {"owner": "OTHER", "mint": "M", "uiTokenAmount": {"amount": "99"}},
        "not-a-dict",
        {"owner": "S", "uiTokenAmount": {"amount": "7"}},
        {"owner": "S", "mint": "M2", "uiTokenAmount": {"amount": "notanumber"}},
        {"owner": "S", "mint": "M3", "uiTokenAmount": {"amount": "3"}},
    ]
    assert _sum_owned_by_signer(entries, "S") == {"M": 15, "M3": 3}
