"""StuckTxResolver.tick(): re-poll stuck signatures and settle parent orders."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from solalpha.execution.stuck_resolver import StuckTxResolver
from solalpha.foundation.errors import RpcError

pytestmark = pytest.mark.unit


class _MapRpc:
    """Fake RpcPool.call keyed by signature (params=[[sig], {...}])."""

    def __init__(self, by_sig: dict[str, Any]) -> None:
        self._by_sig = dict(by_sig)
        self.calls: list[str] = []

    async def call(self, method: str, params: Any) -> Any:
        sig = params[0][0]
        self.calls.append(sig)
        item = self._by_sig[sig]
        if isinstance(item, Exception):
            raise item
        return item


def _status(confirmation: str | None = None, err: object | None = None) -> dict[str, Any]:
    return {"value": [{"confirmationStatus": confirmation, "err": err}]}


async def _seed(store: Any, *, sig: str, order_id: str, mint: str, created: datetime) -> None:
    await store.execute(
        "INSERT INTO orders (order_id, created_at, mint, direction, intended_usd, "
        "intended_input_amount_raw, max_slippage_bps, status, trace_id) "
        "VALUES (?, ?, ?, 'buy', 50.0, 1000, 50, 'stuck', ?)",
        (order_id, created.isoformat(), mint, f"trace-{order_id}"),
    )
    await store.execute(
        "INSERT INTO stuck_signatures (signature, order_id, created_at, attempts, resolved) "
        "VALUES (?, ?, ?, 0, 0)",
        (sig, order_id, created.isoformat()),
    )


async def _order_status(store: Any, order_id: str) -> str:
    row = await store.fetch_one("SELECT status FROM orders WHERE order_id = ?", (order_id,))
    assert row is not None
    return str(row["status"])


async def _stuck_row(store: Any, sig: str) -> Any:
    return await store.fetch_one(
        "SELECT resolved, resolution, attempts FROM stuck_signatures WHERE signature = ?", (sig,)
    )


async def test_tick_confirmed_settles_order(store: Any, clock: Any) -> None:
    await _seed(store, sig="s1", order_id="o1", mint="M1", created=clock.now())
    seen: list[str] = []
    resolver = StuckTxResolver(
        _MapRpc({"s1": _status("finalized")}), store, clock, on_resolved=seen.append
    )
    n = await resolver.tick()
    assert n == 1
    assert await _order_status(store, "o1") == "confirmed"
    row = await _stuck_row(store, "s1")
    assert row["resolved"] == 1 and row["resolution"] == "confirmed"
    assert seen == ["M1"]


async def test_tick_failed_settles_order(store: Any, clock: Any) -> None:
    await _seed(store, sig="s2", order_id="o2", mint="M2", created=clock.now())
    seen: list[str] = []
    resolver = StuckTxResolver(
        _MapRpc({"s2": _status("finalized", err={"x": 1})}), store, clock, on_resolved=seen.append
    )
    n = await resolver.tick()
    assert n == 1
    assert await _order_status(store, "o2") == "failed"
    row = await _stuck_row(store, "s2")
    assert row["resolved"] == 1 and row["resolution"] == "failed"
    assert seen == ["M2"]


async def test_tick_pending_recent_increments_attempts(store: Any, clock: Any) -> None:
    await _seed(store, sig="s3", order_id="o3", mint="M3", created=clock.now())
    resolver = StuckTxResolver(_MapRpc({"s3": _status("processed")}), store, clock)
    n = await resolver.tick()
    assert n == 0
    row = await _stuck_row(store, "s3")
    assert row["resolved"] == 0 and row["attempts"] == 1
    assert await _order_status(store, "o3") == "stuck"


async def test_tick_pending_old_is_abandoned(store: Any, clock: Any) -> None:
    old = clock.now() - timedelta(hours=25)
    await _seed(store, sig="s4", order_id="o4", mint="M4", created=old)
    seen: list[str] = []
    resolver = StuckTxResolver(
        _MapRpc({"s4": _status("processed")}), store, clock, on_resolved=seen.append
    )
    n = await resolver.tick()
    assert n == 1
    row = await _stuck_row(store, "s4")
    assert row["resolved"] == 1 and row["resolution"] == "abandoned"
    assert await _order_status(store, "o4") == "failed"
    assert seen == ["M4"]


async def test_tick_rpc_error_skips_row(store: Any, clock: Any) -> None:
    await _seed(store, sig="s5", order_id="o5", mint="M5", created=clock.now())
    resolver = StuckTxResolver(_MapRpc({"s5": RpcError("rpc down")}), store, clock)
    n = await resolver.tick()
    assert n == 0
    row = await _stuck_row(store, "s5")
    assert row["resolved"] == 0  # left for the next tick


async def test_tick_non_dict_result_is_pending(store: Any, clock: Any) -> None:
    await _seed(store, sig="s6", order_id="o6", mint="M6", created=clock.now())
    resolver = StuckTxResolver(_MapRpc({"s6": ["not-a-dict"]}), store, clock)
    n = await resolver.tick()
    assert n == 0
    assert (await _stuck_row(store, "s6"))["attempts"] == 1


async def test_tick_mixed_rows(store: Any, clock: Any) -> None:
    await _seed(store, sig="a", order_id="oa", mint="MA", created=clock.now())
    await _seed(store, sig="b", order_id="ob", mint="MB", created=clock.now())
    rpc = _MapRpc({"a": _status("confirmed"), "b": _status("processed")})
    n = await StuckTxResolver(rpc, store, clock).tick()
    assert n == 1
    assert await _order_status(store, "oa") == "confirmed"
    assert await _order_status(store, "ob") == "stuck"


async def test_tick_empty_table_returns_zero(store: Any, clock: Any) -> None:
    n = await StuckTxResolver(_MapRpc({}), store, clock).tick()
    assert n == 0
