"""RpcPool: successful call, quarantine on failure, error classification."""

from __future__ import annotations

import pytest
import respx

from solalpha.data.rpc_pool import RpcPool
from solalpha.foundation.clock import FakeClock
from solalpha.foundation.errors import NoHealthyRpcError, RpcPermanentError

pytestmark = pytest.mark.unit


async def test_successful_call_returns_result() -> None:
    url = "https://rpc-ok.example/"
    async with respx.mock(assert_all_called=False) as router:
        router.post(url).respond(200, json={"jsonrpc": "2.0", "id": 1, "result": {"slot": 7}})
        pool = RpcPool([url], FakeClock())
        try:
            result = await pool.call("getSlot")
            assert result == {"slot": 7}
            assert pool.healthy_count() == 1
        finally:
            await pool.aclose()


async def test_probe_reports_ok() -> None:
    url = "https://rpc-ok.example/"
    pool = RpcPool([url], FakeClock())
    try:
        probe = await pool.probe()
        assert probe.status == "ok"
    finally:
        await pool.aclose()


async def test_500_quarantines_and_exhausts() -> None:
    url = "https://rpc-bad.example/"
    async with respx.mock(assert_all_called=False) as router:
        router.post(url).respond(500, text="server error")
        pool = RpcPool([url], FakeClock(), health_quarantine_s=60.0)
        try:
            with pytest.raises(NoHealthyRpcError):
                await pool.call("getSlot", retries=0)
            assert pool.healthy_count() == 0  # quarantined
        finally:
            await pool.aclose()


async def test_4xx_is_permanent() -> None:
    url = "https://rpc-4xx.example/"
    async with respx.mock(assert_all_called=False) as router:
        router.post(url).respond(404, text="not found")
        pool = RpcPool([url], FakeClock())
        try:
            with pytest.raises(RpcPermanentError):
                await pool.call("getSlot")
        finally:
            await pool.aclose()


async def test_failover_to_second_endpoint() -> None:
    bad = "https://rpc-bad.example/"
    good = "https://rpc-good.example/"
    async with respx.mock(assert_all_called=False) as router:
        router.post(bad).respond(503, text="down")
        router.post(good).respond(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})
        pool = RpcPool([bad, good], FakeClock())
        try:
            result = await pool.call("getSlot", retries=2)
            assert result == "ok"
        finally:
            await pool.aclose()


async def test_empty_urls_rejected() -> None:
    with pytest.raises(ValueError, match="at least one URL"):
        RpcPool([], FakeClock())


async def test_reload_adds_and_drops_endpoints() -> None:
    pool = RpcPool(["https://a.example/", "https://b.example/"], FakeClock())
    try:
        assert pool.urls == ["https://a.example/", "https://b.example/"]
        count = pool.reload(["https://b.example/", "https://c.example/"])
        assert count == 2
        assert pool.urls == ["https://b.example/", "https://c.example/"]
    finally:
        await pool.aclose()


async def test_reload_preserves_kept_endpoint_identity() -> None:
    # An endpoint kept across a reload keeps its `_EndpointState` object, and
    # therefore its rolling score / quarantine window.
    pool = RpcPool(["https://keep.example/", "https://drop.example/"], FakeClock())
    try:
        before = {ep.url: ep for ep in pool._endpoints}
        pool.reload(["https://keep.example/", "https://new.example/"])
        after = {ep.url: ep for ep in pool._endpoints}
        assert after["https://keep.example/"] is before["https://keep.example/"]
        assert "https://new.example/" in after
        assert "https://drop.example/" not in after
    finally:
        await pool.aclose()


async def test_reload_empty_rejected_keeps_previous_set() -> None:
    pool = RpcPool(["https://a.example/"], FakeClock())
    try:
        with pytest.raises(ValueError, match="at least one URL"):
            pool.reload([])
        # A rejected reload must not strip the pool of its endpoints.
        assert pool.urls == ["https://a.example/"]
    finally:
        await pool.aclose()


async def test_reload_dedupes_and_strips() -> None:
    pool = RpcPool(["https://a.example/"], FakeClock())
    try:
        count = pool.reload(["https://x.example/", "https://x.example/", "  "])
        assert count == 1
        assert pool.urls == ["https://x.example/"]
    finally:
        await pool.aclose()
