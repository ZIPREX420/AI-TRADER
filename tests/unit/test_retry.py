"""retry: transient errors retried, permanent errors not."""

from __future__ import annotations

import pytest

from solalpha.foundation.errors import PermanentError, TransientError
from solalpha.foundation.retry import retry_async

pytestmark = pytest.mark.unit


async def test_transient_is_retried() -> None:
    attempts = 0

    @retry_async(attempts=3, base=0.0, cap=0.0)
    async def fn() -> int:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TransientError("boom")
        return 42

    assert await fn() == 42
    assert attempts == 3


async def test_permanent_not_retried() -> None:
    attempts = 0

    @retry_async(attempts=5, base=0.0, cap=0.0)
    async def fn() -> int:
        nonlocal attempts
        attempts += 1
        raise PermanentError("nope")

    with pytest.raises(PermanentError):
        await fn()
    assert attempts == 1


async def test_exhausted_transient_raises_last() -> None:
    @retry_async(attempts=2, base=0.0, cap=0.0)
    async def fn() -> int:
        raise TransientError("still bad")

    with pytest.raises(TransientError):
        await fn()
