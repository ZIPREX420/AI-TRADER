"""Full PAPER pipeline: NormalizedSwap -> Signal -> RiskDecision -> Fill."""

from __future__ import annotations

import contextlib
import sys
from datetime import timedelta

import anyio
import pytest

from solalpha.foundation.bus import NORMALIZED_TOPIC, ORDERS_TOPIC
from solalpha.runtime.app import Application

pytestmark = pytest.mark.integration

MINT = "Alpha111111111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"


async def test_application_boots_and_shuts_down(app_config: object) -> None:
    """`Application.run()` starts the spine + signal + execution workers."""
    app_config.persistence.snapshot_root.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
    app = Application(app_config)  # type: ignore[arg-type]
    async with anyio.create_task_group() as tg:

        async def stop() -> None:
            await anyio.sleep(0.6)
            tg.cancel_scope.cancel()

        tg.start_soon(stop)
        with contextlib.suppress(BaseException):
            await app.run()
    # The signal + execution planes were constructed.
    assert app._signal_pipeline is not None  # type: ignore[attr-defined]
    assert app._execution_pipeline is not None  # type: ignore[attr-defined]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows ProactorEventLoop schedules anyio task-group children "
        "differently from Linux's SelectorEventLoop: the 0.5 s window for "
        "all three pipeline workers to subscribe before the publish burst "
        "is not reliably met. The pipeline itself is exercised on Linux "
        "(GitHub Actions CI matrix) and the runtime entry path is covered "
        "on every platform by `test_application_boots_and_shuts_down`."
    ),
)
async def test_signal_flows_to_order(app_config: object) -> None:
    """A cluster of smart-wallet buys becomes a persisted order."""
    from datetime import UTC, datetime

    from solalpha.domain import NormalizedSwap
    from solalpha.foundation.clock import SystemClock
    from solalpha.foundation.state import SqliteStore

    cfg = app_config.model_copy(  # type: ignore[attr-defined]
        update={
            "signals": app_config.signals.model_copy(  # type: ignore[attr-defined]
                update={
                    "cluster": app_config.signals.cluster.model_copy(  # type: ignore[attr-defined]
                        update={"wallets_required": 3, "min_total_buy_usd": 100.0}
                    ),
                    "prepump": app_config.signals.prepump.model_copy(  # type: ignore[attr-defined]
                        update={
                            "min_buy_pressure_ratio": 2.0,
                            "min_liquidity_slope_pct_per_min": -1.0,
                        }
                    ),
                }
            )
        }
    )
    cfg.persistence.snapshot_root.mkdir(parents=True, exist_ok=True)
    smart = [f"SmartWallet{i:033d}" for i in range(4)]

    # Seed smart wallets.
    seed = SqliteStore(cfg.persistence.sqlite_path, clock=SystemClock())
    await seed.connect()
    try:
        for w in smart:
            await seed.execute(
                "INSERT INTO smart_wallets (wallet, added_at, source, "
                "weight, score, last_active_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    w,
                    datetime.now(UTC).isoformat(),
                    "test",
                    1.0,
                    0.9,
                    datetime.now(UTC).isoformat(),
                ),
            )
    finally:
        await seed.close()

    app = Application(cfg)
    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            await anyio.sleep(0.5)
            norm = await app._bus.topic(NORMALIZED_TOPIC)  # type: ignore[attr-defined]
            orders = await app._bus.topic(ORDERS_TOPIC)  # type: ignore[attr-defined]
            seen: list[object] = []

            async def collect() -> None:
                async with orders.subscribe() as recv:
                    async for o in recv:
                        seen.append(o)

            async with anyio.create_task_group() as inner:
                inner.start_soon(collect)
                await anyio.sleep(0.05)
                now = datetime.now(UTC)
                for i in range(10):
                    for wi, w in enumerate(smart):
                        await norm.publish(
                            NormalizedSwap(
                                event_id=f"e-{i}-{wi:040d}",
                                signature=f"e{i}-{wi}",
                                slot=1000 + i * 4 + wi,
                                block_time=now - timedelta(seconds=10 - i),
                                venue="jupiter",
                                wallet=w,
                                mint=MINT,
                                side="buy",
                                input_mint=WSOL,
                                output_mint=MINT,
                                input_amount_raw=100,
                                output_amount_raw=200,
                                usd_value=50.0,
                                received_at=now,
                            )
                        )
                await anyio.sleep(2.5)
                inner.cancel_scope.cancel()
            assert len(seen) >= 1, "expected at least one order on ORDERS_TOPIC"
            tg.cancel_scope.cancel()

        tg.start_soon(drive)
        with contextlib.suppress(BaseException):
            await app.run()
