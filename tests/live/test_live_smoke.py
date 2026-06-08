"""Live tier -- hits real RPC / Jupiter endpoints.

Every test here is marked `live` and is SKIPPED by default. Enable with::

    SOLALPHA_TEST_LIVE=1 pytest --run-live tests/live

The runner gate lives in `tests/conftest.py:pytest_collection_modifyitems`.
These tests need `SOLALPHA_RPC_URLS` exported to at least one healthy
mainnet endpoint.
"""

from __future__ import annotations

import os

import pytest

from solalpha.data.rpc_pool import RpcPool
from solalpha.execution.jupiter import JupiterClient
from solalpha.foundation.clock import SystemClock

pytestmark = pytest.mark.live

# Well-known mainnet mints used for a sanity quote.
_WSOL = "So11111111111111111111111111111111111111112"
_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _rpc_urls() -> list[str]:
    raw = os.environ.get("SOLALPHA_RPC_URLS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


async def test_rpc_pool_get_slot() -> None:
    """A real RPC endpoint answers `getSlot` with a positive integer."""
    urls = _rpc_urls()
    if not urls:
        pytest.skip("SOLALPHA_RPC_URLS not set")
    pool = RpcPool(urls, SystemClock())
    try:
        slot = await pool.call("getSlot")
        assert isinstance(slot, int) and slot > 0
    finally:
        await pool.aclose()


async def test_rpc_pool_probe_healthy() -> None:
    urls = _rpc_urls()
    if not urls:
        pytest.skip("SOLALPHA_RPC_URLS not set")
    pool = RpcPool(urls, SystemClock())
    try:
        # Issue one call so the probe has a fresh sample.
        await pool.call("getSlot")
        probe = await pool.probe()
        assert probe.status in ("ok", "degraded")
    finally:
        await pool.aclose()


async def test_jupiter_quote_wsol_usdc() -> None:
    """Jupiter v6 returns a quote for 0.1 WSOL -> USDC."""
    client = JupiterClient(
        os.environ.get("SOLALPHA_JUPITER_BASE_URL", "https://quote-api.jup.ag/v6"),
        SystemClock(),
    )
    try:
        quote = await client.quote(
            input_mint=_WSOL,
            output_mint=_USDC,
            amount_raw=100_000_000,  # 0.1 SOL
            slippage_bps=100,
        )
        assert quote.venue == "jupiter"
        assert quote.out_amount_raw > 0
    finally:
        await client.aclose()
