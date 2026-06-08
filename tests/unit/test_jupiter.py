"""JupiterClient: quote + swap-instructions over respx-mocked HTTP."""

from __future__ import annotations

import httpx
import pytest
import respx

from solalpha.execution.base import Quote
from solalpha.execution.jupiter import JupiterClient
from solalpha.foundation.clock import FakeClock
from solalpha.foundation.errors import JupiterError, JupiterPermanentError

pytestmark = pytest.mark.unit

BASE = "https://jupiter.test"
WSOL = "So11111111111111111111111111111111111111112"
MINT = "Mint1111111111111111111111111111111111111111"


def _client() -> JupiterClient:
    return JupiterClient(BASE, FakeClock(), quote_timeout_s=1.0, swap_timeout_s=1.0)


def _quote() -> Quote:
    return Quote(
        venue="jupiter",
        input_mint=WSOL,
        output_mint=MINT,
        in_amount_raw=1,
        out_amount_raw=1,
        other_amount_threshold=1,
        price_impact_pct=0.0,
        slippage_bps=1,
        raw_response={"x": 1},
    )


async def test_quote_ok() -> None:
    with respx.mock:
        respx.get(f"{BASE}/quote").mock(
            return_value=httpx.Response(
                200,
                json={
                    "inAmount": "1000000000",
                    "outAmount": "5000000",
                    "otherAmountThreshold": "4950000",
                    "priceImpactPct": "0.012",
                    "routePlan": [
                        {"swapInfo": {"label": "Orca"}},
                        {"swapInfo": {"label": "Raydium"}},
                    ],
                },
            )
        )
        jc = _client()
        q = await jc.quote(
            input_mint=WSOL, output_mint=MINT, amount_raw=1_000_000_000, slippage_bps=50
        )
        assert q.venue == "jupiter"
        assert (q.in_amount_raw, q.out_amount_raw, q.other_amount_threshold) == (
            1_000_000_000,
            5_000_000,
            4_950_000,
        )
        assert abs(q.price_impact_pct - 0.012) < 1e-9
        assert q.route_plan == ("Orca", "Raydium")
        assert q.raw_response["inAmount"] == "1000000000"
        await jc.aclose()


async def test_quote_defaults() -> None:
    with respx.mock:
        respx.get(f"{BASE}/quote").mock(
            return_value=httpx.Response(200, json={"inAmount": "10", "outAmount": "20"})
        )
        q = await _client().quote(input_mint=WSOL, output_mint=MINT, amount_raw=10, slippage_bps=10)
        assert q.other_amount_threshold == 20  # defaults to outAmount
        assert q.price_impact_pct == 0.0
        assert q.route_plan == ()


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_quote_5xx_429_transient(status: int) -> None:
    with respx.mock:
        respx.get(f"{BASE}/quote").mock(return_value=httpx.Response(status, text="x"))
        with pytest.raises(JupiterError):
            await _client().quote(input_mint=WSOL, output_mint=MINT, amount_raw=1, slippage_bps=1)


@pytest.mark.parametrize("status", [400, 404, 422])
async def test_quote_4xx_permanent(status: int) -> None:
    with respx.mock:
        respx.get(f"{BASE}/quote").mock(return_value=httpx.Response(status, text="bad"))
        with pytest.raises(JupiterPermanentError):
            await _client().quote(input_mint=WSOL, output_mint=MINT, amount_raw=1, slippage_bps=1)


async def test_quote_non_json_permanent() -> None:
    with respx.mock:
        respx.get(f"{BASE}/quote").mock(return_value=httpx.Response(200, text="not json"))
        with pytest.raises(JupiterPermanentError):
            await _client().quote(input_mint=WSOL, output_mint=MINT, amount_raw=1, slippage_bps=1)


async def test_quote_non_object_permanent() -> None:
    with respx.mock:
        respx.get(f"{BASE}/quote").mock(return_value=httpx.Response(200, json=[1, 2]))
        with pytest.raises(JupiterPermanentError):
            await _client().quote(input_mint=WSOL, output_mint=MINT, amount_raw=1, slippage_bps=1)


async def test_quote_missing_fields_permanent() -> None:
    with respx.mock:
        respx.get(f"{BASE}/quote").mock(return_value=httpx.Response(200, json={"outAmount": "5"}))
        with pytest.raises(JupiterPermanentError):
            await _client().quote(input_mint=WSOL, output_mint=MINT, amount_raw=1, slippage_bps=1)


async def test_quote_transport_error_transient() -> None:
    with respx.mock:
        respx.get(f"{BASE}/quote").mock(side_effect=httpx.ConnectError("no route"))
        with pytest.raises(JupiterError):
            await _client().quote(input_mint=WSOL, output_mint=MINT, amount_raw=1, slippage_bps=1)


async def test_swap_instructions_ok() -> None:
    with respx.mock:
        respx.post(f"{BASE}/swap-instructions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "setupInstructions": ["c2V0dXA="],
                    "swapInstruction": "c3dhcA==",
                    "cleanupInstructions": ["Y2xlYW51cA=="],
                    "addressLookupTableAddresses": ["ALT1", "ALT2"],
                    "computeUnitLimit": 200000,
                },
            )
        )
        si = await _client().swap_instructions(
            quote=_quote(), user_public_key="USER", priority_fee_lamports=1000
        )
        assert si.venue == "jupiter"
        assert si.setup_instructions == ("c2V0dXA=",)
        assert si.swap_instruction == "c3dhcA=="
        assert si.cleanup_instructions == ("Y2xlYW51cA==",)
        assert si.address_lookup_tables == ("ALT1", "ALT2")
        assert si.compute_unit_limit == 200000


async def test_swap_instructions_dict_swapix_serialized() -> None:
    with respx.mock:
        respx.post(f"{BASE}/swap-instructions").mock(
            return_value=httpx.Response(
                200, json={"swapInstruction": {"programId": "P", "data": "D"}}
            )
        )
        si = await _client().swap_instructions(
            quote=_quote(), user_public_key="U", priority_fee_lamports=1
        )
        assert '"programId": "P"' in si.swap_instruction


async def test_swap_instructions_500_transient() -> None:
    with respx.mock:
        respx.post(f"{BASE}/swap-instructions").mock(return_value=httpx.Response(502))
        with pytest.raises(JupiterError):
            await _client().swap_instructions(
                quote=_quote(), user_public_key="U", priority_fee_lamports=1
            )


async def test_swap_instructions_transport_error_transient() -> None:
    with respx.mock:
        respx.post(f"{BASE}/swap-instructions").mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(JupiterError):
            await _client().swap_instructions(
                quote=_quote(), user_public_key="U", priority_fee_lamports=1
            )
