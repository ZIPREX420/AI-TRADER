"""Confirmer: poll getSignatureStatuses until confirmed / failed / stuck."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from solalpha.execution.confirmation import Confirmer
from solalpha.foundation.errors import ExecutionFailed, RpcError, StuckTransaction

pytestmark = pytest.mark.unit


class _AutoClock:
    """Test clock whose sleep() advances monotonic time instead of blocking."""

    def __init__(self) -> None:
        self._mono = 0.0

    def monotonic(self) -> float:
        return self._mono

    def now(self) -> datetime:
        return datetime(2026, 5, 15, 12, 0, tzinfo=UTC)

    async def sleep(self, seconds: float) -> None:
        self._mono += max(0.0, seconds)


class _SeqRpc:
    """Fake RpcPool.call returning queued responses; repeats the last one."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def call(self, method: str, params: Any) -> Any:
        self.calls += 1
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def _status(confirmation: str | None = None, err: object | None = None) -> dict[str, Any]:
    return {"value": [{"confirmationStatus": confirmation, "err": err}]}


@pytest.mark.parametrize("confirmation", ["confirmed", "finalized"])
async def test_confirm_returns_on_terminal_status(confirmation: str) -> None:
    rpc = _SeqRpc([_status(confirmation)])
    entry = await Confirmer(rpc, _AutoClock(), timeout_s=5, poll_interval_s=1).confirm("sig")
    assert entry["confirmationStatus"] == confirmation
    assert rpc.calls == 1  # success path does not sleep/re-poll


async def test_confirm_err_raises_execution_failed() -> None:
    rpc = _SeqRpc([_status("finalized", err={"InstructionError": [0, "Custom"]})])
    with pytest.raises(ExecutionFailed):
        await Confirmer(rpc, _AutoClock(), timeout_s=5).confirm("sig")


async def test_confirm_err_null_string_is_not_failure() -> None:
    # The literal string "null" is treated as no error.
    rpc = _SeqRpc([_status("confirmed", err="null")])
    entry = await Confirmer(rpc, _AutoClock(), timeout_s=5).confirm("sig")
    assert entry["confirmationStatus"] == "confirmed"


async def test_confirm_pending_then_confirmed() -> None:
    rpc = _SeqRpc([_status("processed"), _status("confirmed")])
    entry = await Confirmer(rpc, _AutoClock(), timeout_s=10, poll_interval_s=1).confirm("sig")
    assert entry["confirmationStatus"] == "confirmed"
    assert rpc.calls == 2


async def test_confirm_rpc_error_then_confirmed() -> None:
    rpc = _SeqRpc([RpcError("rpc down"), _status("finalized")])
    entry = await Confirmer(rpc, _AutoClock(), timeout_s=10, poll_interval_s=1).confirm("sig")
    assert entry["confirmationStatus"] == "finalized"
    assert rpc.calls == 2


async def test_confirm_empty_value_then_confirmed() -> None:
    rpc = _SeqRpc([{"value": []}, _status("confirmed")])
    entry = await Confirmer(rpc, _AutoClock(), timeout_s=10, poll_interval_s=1).confirm("sig")
    assert entry["confirmationStatus"] == "confirmed"


async def test_confirm_non_dict_result_then_confirmed() -> None:
    rpc = _SeqRpc([["not", "a", "dict"], _status("confirmed")])
    entry = await Confirmer(rpc, _AutoClock(), timeout_s=10, poll_interval_s=1).confirm("sig")
    assert entry["confirmationStatus"] == "confirmed"


async def test_confirm_first_entry_not_dict_then_confirmed() -> None:
    rpc = _SeqRpc([{"value": ["scalar"]}, _status("confirmed")])
    entry = await Confirmer(rpc, _AutoClock(), timeout_s=10, poll_interval_s=1).confirm("sig")
    assert entry["confirmationStatus"] == "confirmed"


async def test_confirm_timeout_zero_raises_stuck_immediately() -> None:
    rpc = _SeqRpc([_status("processed")])
    with pytest.raises(StuckTransaction) as ei:
        await Confirmer(rpc, _AutoClock(), timeout_s=0.0).confirm("sigX")
    assert ei.value.signature == "sigX"
    assert rpc.calls == 0  # deadline already passed; never polled


async def test_confirm_persistent_pending_raises_stuck() -> None:
    rpc = _SeqRpc([_status("processed")])
    with pytest.raises(StuckTransaction):
        await Confirmer(rpc, _AutoClock(), timeout_s=3, poll_interval_s=1).confirm("sig")
    assert rpc.calls >= 3  # polled across the whole window via auto-advancing sleep
