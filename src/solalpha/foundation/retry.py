"""Retry helpers built on tenacity.

Standard policy:
- 3 attempts
- exponential backoff: 0.25s → 1s → 4s, capped at 5s
- only retries `TransientError` subclasses
- structured-log every retry with attempt number and exception class
"""

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from tenacity import (
    AsyncRetrying,
    RetryCallState,
    RetryError,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from solalpha.foundation.errors import TransientError
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

P = ParamSpec("P")
R = TypeVar("R")

_log = get_logger(__name__)


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, TransientError):
        return True
    # Some libraries raise their own; we conservatively retry only the typed ones.
    return getattr(exc, "transient", False) is True


def _log_attempt(retry_state: RetryCallState) -> None:
    if retry_state.outcome and retry_state.outcome.failed:
        exc = retry_state.outcome.exception()
        _log.warning(
            "retrying",
            attempt=retry_state.attempt_number,
            fn=getattr(retry_state.fn, "__name__", str(retry_state.fn)),
            exc_type=type(exc).__name__ if exc else None,
            exc=str(exc) if exc else None,
        )


def retry_async(
    *,
    attempts: int = 3,
    base: float = 0.25,
    cap: float = 5.0,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator: retry an async callable on TransientError."""

    def deco(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(attempts),
                    wait=wait_exponential(multiplier=base, max=cap),
                    retry=retry_if_exception(_is_transient),
                    reraise=True,
                    before_sleep=_log_attempt,
                ):
                    with attempt:
                        return await fn(*args, **kwargs)
            except RetryError as e:  # pragma: no cover — reraise=True normally bypasses
                raise e.last_attempt.exception() or e from e
            raise RuntimeError("unreachable")  # pragma: no cover

        return wrapper

    return deco


def retry_sync(
    *,
    attempts: int = 3,
    base: float = 0.25,
    cap: float = 5.0,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def deco(fn: Callable[P, R]) -> Callable[P, R]:
        @wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                for attempt in Retrying(
                    stop=stop_after_attempt(attempts),
                    wait=wait_exponential(multiplier=base, max=cap),
                    retry=retry_if_exception(_is_transient),
                    reraise=True,
                    before_sleep=_log_attempt,
                ):
                    with attempt:
                        return fn(*args, **kwargs)
            except RetryError as e:  # pragma: no cover
                raise e.last_attempt.exception() or e from e
            raise RuntimeError("unreachable")  # pragma: no cover

        return wrapper

    return deco


__all__: list[str] = ["retry_async", "retry_sync"]


# Tenacity stub for AsyncRetrying.__call__ in case Any escapes mypy.
def _appease_unused_import(_: Any) -> None:  # pragma: no cover
    return None
