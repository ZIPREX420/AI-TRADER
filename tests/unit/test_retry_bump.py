"""RetryBumpExecutor: priority-fee + slippage escalation across attempts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from solalpha.execution.retry_bump import RetryBumpExecutor
from solalpha.foundation.config import ExecutionConfig
from solalpha.foundation.errors import (
    BlockhashExpired,
    ExecutionFailed,
    JupiterError,
    PermanentError,
    RaydiumError,
    StuckTransaction,
)

pytestmark = pytest.mark.unit


def _fns(fees: list[tuple[int, int]], *, fail_confirm: list[Exception | None] | None = None):
    confirm_seq = list(fail_confirm or [])

    async def build_fn(fee: int, slip: int) -> Any:
        fees.append((fee, slip))
        return ("BUILT", fee, slip)

    async def submit_fn(built: Any) -> str:
        return "sigX"

    async def confirm_fn(signature: str) -> dict[str, object]:
        if confirm_seq:
            exc = confirm_seq.pop(0)
            if exc is not None:
                raise exc
        return {"confirmationStatus": "confirmed"}

    return build_fn, submit_fn, confirm_fn


async def test_success_first_attempt() -> None:
    fees: list[tuple[int, int]] = []
    build_fn, submit_fn, confirm_fn = _fns(fees)
    _built, sig, status = await RetryBumpExecutor(ExecutionConfig()).run(
        base_slippage_bps=100, build_fn=build_fn, submit_fn=submit_fn, confirm_fn=confirm_fn
    )
    assert sig == "sigX"
    assert status["confirmationStatus"] == "confirmed"
    assert fees == [(5000, 100)]  # attempt 0: ladder[0]=5000, slip=100+0


async def test_success_after_transient_escalates() -> None:
    fees: list[tuple[int, int]] = []
    build_fn, submit_fn, confirm_fn = _fns(fees, fail_confirm=[JupiterError("5xx"), None])
    _, sig, _ = await RetryBumpExecutor(ExecutionConfig()).run(
        base_slippage_bps=100, build_fn=build_fn, submit_fn=submit_fn, confirm_fn=confirm_fn
    )
    assert sig == "sigX"
    # attempt 0: (5000, 100+0); attempt 1: (25000, 100+50)
    assert fees == [(5000, 100), (25000, 150)]


async def test_permanent_error_raises_immediately() -> None:
    fees: list[tuple[int, int]] = []
    build_fn, submit_fn, confirm_fn = _fns(fees, fail_confirm=[ExecutionFailed("on-chain fail")])
    with pytest.raises(ExecutionFailed):
        await RetryBumpExecutor(ExecutionConfig()).run(
            base_slippage_bps=50, build_fn=build_fn, submit_fn=submit_fn, confirm_fn=confirm_fn
        )
    assert len(fees) == 1  # no retry on permanent


async def test_bare_permanent_error_raises() -> None:
    fees: list[tuple[int, int]] = []
    build_fn, submit_fn, confirm_fn = _fns(fees, fail_confirm=[PermanentError("nope")])
    with pytest.raises(PermanentError):
        await RetryBumpExecutor(ExecutionConfig()).run(
            base_slippage_bps=50, build_fn=build_fn, submit_fn=submit_fn, confirm_fn=confirm_fn
        )


async def test_stuck_transaction_retries_then_raises() -> None:
    fees: list[tuple[int, int]] = []
    stuck = StuckTransaction("not confirmed", signature="s1")
    build_fn, submit_fn, confirm_fn = _fns(fees, fail_confirm=[stuck, stuck, stuck])
    with pytest.raises(StuckTransaction):
        await RetryBumpExecutor(ExecutionConfig()).run(
            base_slippage_bps=50, build_fn=build_fn, submit_fn=submit_fn, confirm_fn=confirm_fn
        )
    assert len(fees) == 3  # max_attempts


async def test_blockhash_expired_from_build_retries() -> None:
    fees: list[tuple[int, int]] = []
    calls = {"n": 0}

    async def build_fn(fee: int, slip: int) -> Any:
        fees.append((fee, slip))
        calls["n"] += 1
        if calls["n"] == 1:
            raise BlockhashExpired("stale")
        return ("BUILT", fee, slip)

    async def submit_fn(built: Any) -> str:
        return "sig"

    async def confirm_fn(signature: str) -> dict[str, object]:
        return {"confirmationStatus": "finalized"}

    _, sig, _ = await RetryBumpExecutor(ExecutionConfig()).run(
        base_slippage_bps=10, build_fn=build_fn, submit_fn=submit_fn, confirm_fn=confirm_fn
    )
    assert sig == "sig"
    assert fees == [(5000, 10), (25000, 60)]


async def test_generic_transient_retries() -> None:
    fees: list[tuple[int, int]] = []
    build_fn, submit_fn, confirm_fn = _fns(fees, fail_confirm=[RaydiumError("blip"), None])
    _, sig, _ = await RetryBumpExecutor(ExecutionConfig()).run(
        base_slippage_bps=0, build_fn=build_fn, submit_fn=submit_fn, confirm_fn=confirm_fn
    )
    assert sig == "sigX"
    assert len(fees) == 2


async def test_exhaustion_raises_last_exception() -> None:
    fees: list[tuple[int, int]] = []
    build_fn, submit_fn, confirm_fn = _fns(
        fees, fail_confirm=[JupiterError("a"), JupiterError("b"), JupiterError("c")]
    )
    with pytest.raises(JupiterError):
        await RetryBumpExecutor(ExecutionConfig()).run(
            base_slippage_bps=0, build_fn=build_fn, submit_fn=submit_fn, confirm_fn=confirm_fn
        )
    assert len(fees) == 3


def test_priority_fee_ladder_clamps() -> None:
    cfg = SimpleNamespace(
        max_attempts=5,
        bump_priority_fee_lamports=[5000, 25000, 100000],
        bump_slippage_bps=[0, 50, 100],
        default_priority_fee_lamports=5000,
    )
    ex = RetryBumpExecutor(cfg)  # type: ignore[arg-type]
    assert ex._priority_fee(0) == 5000
    assert ex._priority_fee(2) == 100000
    assert ex._priority_fee(4) == 100000  # clamped to last rung


def test_priority_fee_empty_ladder_uses_default() -> None:
    cfg = SimpleNamespace(
        max_attempts=3,
        bump_priority_fee_lamports=[],
        bump_slippage_bps=[],
        default_priority_fee_lamports=777,
    )
    ex = RetryBumpExecutor(cfg)  # type: ignore[arg-type]
    assert ex._priority_fee(0) == 777
    assert ex._slippage(120, 2) == 120  # no bumps -> base unchanged


def test_slippage_bump_indexing() -> None:
    cfg = SimpleNamespace(
        max_attempts=3,
        bump_priority_fee_lamports=[1],
        bump_slippage_bps=[0, 50, 100],
        default_priority_fee_lamports=1,
    )
    ex = RetryBumpExecutor(cfg)  # type: ignore[arg-type]
    assert ex._slippage(100, 0) == 100
    assert ex._slippage(100, 1) == 150
    assert ex._slippage(100, 9) == 200  # clamped to last rung
