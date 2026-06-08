"""RaydiumClient: quote + swap-transaction over respx-mocked HTTP."""

from __future__ import annotations

import httpx
import pytest
import respx

from solalpha.execution.base import Quote
from solalpha.execution.raydium import RaydiumClient
from solalpha.foundation.clock import FakeClock
from solalpha.foundation.errors import RaydiumError

pytestmark = pytest.mark.unit

BASE = "https://raydium.test"
WSOL = "So11111111111111111111111111111111111111112"
MINT = "Mint1111111111111111111111111111111111111111"
COMPUTE = f"{BASE}/compute/swap-base-in"
TXN = f"{BASE}/transaction/swap-base-in"


def _client() -> RaydiumClient:
    return RaydiumClient(BASE, FakeClock(), request_timeout_s=1.0)


def _quote() -> Quote:
    return Quote(
        venue="raydium",
        input_mint=WSOL,
        output_mint=MINT,
        in_amount_raw=1,
        out_amount_raw=1,
        other_amount_threshold=1,
        price_impact_pct=0.0,
        slippage_bps=1,
        raw_response={"data": {"id": "q"}},
    )


async def test_quote_ok() -> None:
    with respx.mock:
        respx.get(COMPUTE).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "inputAmount": "1000000000",
                        "outputAmount": "7000000",
                        "otherAmountThreshold": "6900000",
                        "priceImpactPct": 0.02,
                    }
                },
            )
        )
        q = await _client().quote(
            input_mint=WSOL, output_mint=MINT, amount_raw=1_000_000_000, slippage_bps=50
        )
        assert q.venue == "raydium"
        assert (q.in_amount_raw, q.out_amount_raw, q.other_amount_threshold) == (
            1_000_000_000,
            7_000_000,
            6_900_000,
        )
        assert abs(q.price_impact_pct - 0.02) < 1e-9
        assert q.raw_response["data"]["inputAmount"] == "1000000000"


async def test_quote_threshold_and_impact_default() -> None:
    with respx.mock:
        respx.get(COMPUTE).mock(
            return_value=httpx.Response(
                200, json={"data": {"inputAmount": "10", "outputAmount": "20"}}
            )
        )
        q = await _client().quote(input_mint=WSOL, output_mint=MINT, amount_raw=10, slippage_bps=10)
        assert q.other_amount_threshold == 20  # defaults to outputAmount
        assert q.price_impact_pct == 0.0


async def test_quote_bad_impact_falls_back_to_zero() -> None:
    with respx.mock:
        respx.get(COMPUTE).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "inputAmount": "10",
                        "outputAmount": "20",
                        "priceImpactPct": "not-a-number",
                    }
                },
            )
        )
        q = await _client().quote(input_mint=WSOL, output_mint=MINT, amount_raw=10, slippage_bps=10)
        assert q.price_impact_pct == 0.0


@pytest.mark.parametrize("status", [400, 429, 500, 503])
async def test_quote_http_error(status: int) -> None:
    with respx.mock:
        respx.get(COMPUTE).mock(return_value=httpx.Response(status, text="boom"))
        with pytest.raises(RaydiumError):
            await _client().quote(input_mint=WSOL, output_mint=MINT, amount_raw=1, slippage_bps=1)


async def test_quote_non_json() -> None:
    with respx.mock:
        respx.get(COMPUTE).mock(return_value=httpx.Response(200, text="not json"))
        with pytest.raises(RaydiumError):
            await _client().quote(input_mint=WSOL, output_mint=MINT, amount_raw=1, slippage_bps=1)


async def test_quote_non_object() -> None:
    with respx.mock:
        respx.get(COMPUTE).mock(return_value=httpx.Response(200, json=[1, 2]))
        with pytest.raises(RaydiumError):
            await _client().quote(input_mint=WSOL, output_mint=MINT, amount_raw=1, slippage_bps=1)


async def test_quote_data_not_dict() -> None:
    with respx.mock:
        respx.get(COMPUTE).mock(return_value=httpx.Response(200, json={"data": "nope"}))
        with pytest.raises(RaydiumError):
            await _client().quote(input_mint=WSOL, output_mint=MINT, amount_raw=1, slippage_bps=1)


async def test_quote_missing_fields() -> None:
    with respx.mock:
        respx.get(COMPUTE).mock(
            return_value=httpx.Response(200, json={"data": {"outputAmount": "5"}})
        )
        with pytest.raises(RaydiumError):
            await _client().quote(input_mint=WSOL, output_mint=MINT, amount_raw=1, slippage_bps=1)


async def test_quote_transport_error() -> None:
    with respx.mock:
        respx.get(COMPUTE).mock(side_effect=httpx.ConnectError("no route"))
        with pytest.raises(RaydiumError):
            await _client().quote(input_mint=WSOL, output_mint=MINT, amount_raw=1, slippage_bps=1)


async def test_swap_transaction_ok() -> None:
    with respx.mock:
        respx.post(TXN).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "transaction": "QkFTRTY0VFg=",
                            "addressLookupTableAddresses": ["ALT1", "ALT2"],
                        }
                    ]
                },
            )
        )
        si = await _client().swap_transaction(
            quote=_quote(), user_public_key="USER", priority_fee_lamports=1000
        )
        assert si.venue == "raydium"
        assert si.swap_instruction == "QkFTRTY0VFg="
        assert si.address_lookup_tables == ("ALT1", "ALT2")


async def test_swap_transaction_no_alts() -> None:
    with respx.mock:
        respx.post(TXN).mock(
            return_value=httpx.Response(200, json={"data": [{"transaction": "VFg="}]})
        )
        si = await _client().swap_transaction(
            quote=_quote(), user_public_key="U", priority_fee_lamports=1
        )
        assert si.address_lookup_tables == ()


@pytest.mark.parametrize("status", [400, 500])
async def test_swap_transaction_http_error(status: int) -> None:
    with respx.mock:
        respx.post(TXN).mock(return_value=httpx.Response(status, text="bad"))
        with pytest.raises(RaydiumError):
            await _client().swap_transaction(
                quote=_quote(), user_public_key="U", priority_fee_lamports=1
            )


async def test_swap_transaction_empty_data() -> None:
    with respx.mock:
        respx.post(TXN).mock(return_value=httpx.Response(200, json={"data": []}))
        with pytest.raises(RaydiumError):
            await _client().swap_transaction(
                quote=_quote(), user_public_key="U", priority_fee_lamports=1
            )


async def test_swap_transaction_data_not_list() -> None:
    with respx.mock:
        respx.post(TXN).mock(return_value=httpx.Response(200, json={"data": {"k": "v"}}))
        with pytest.raises(RaydiumError):
            await _client().swap_transaction(
                quote=_quote(), user_public_key="U", priority_fee_lamports=1
            )


async def test_swap_transaction_first_not_dict() -> None:
    with respx.mock:
        respx.post(TXN).mock(return_value=httpx.Response(200, json={"data": ["x"]}))
        with pytest.raises(RaydiumError):
            await _client().swap_transaction(
                quote=_quote(), user_public_key="U", priority_fee_lamports=1
            )


async def test_swap_transaction_transport_error() -> None:
    with respx.mock:
        respx.post(TXN).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(RaydiumError):
            await _client().swap_transaction(
                quote=_quote(), user_public_key="U", priority_fee_lamports=1
            )


async def test_aclose_owns_client_closes() -> None:
    c = _client()
    await c.aclose()
    assert c._client.is_closed


async def test_aclose_injected_client_not_closed() -> None:
    injected = httpx.AsyncClient()
    c = RaydiumClient(BASE, FakeClock(), client=injected)
    await c.aclose()
    assert not injected.is_closed
    await injected.aclose()
