"""Structured logging via structlog with a stdlib bridge.

Every record carries `service`, `version`, `trace_id` (from contextvar) and any
context bound by `bind_contextvars`. JSON output for files; key=value pretty
output when stderr is a tty.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterator

    from structlog.types import EventDict, WrappedLogger

_TRACE_ID: ContextVar[str | None] = ContextVar("trace_id", default=None)
_CONFIGURED: bool = False


def _add_trace_id(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    tid = _TRACE_ID.get()
    if tid is not None:
        event_dict.setdefault("trace_id", tid)
    return event_dict


def _add_service_metadata(service: str, version: str) -> structlog.types.Processor:
    def proc(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        event_dict.setdefault("version", version)
        return event_dict

    return proc


def configure_logging(
    *,
    level: str = "info",
    fmt: str = "json",
    service: str = "solalpha",
    version: str = "0.0.0",
    stream: Any = None,
) -> None:
    """Configure structlog + stdlib logging. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = getattr(logging, level.upper(), logging.INFO)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts")

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        timestamper,
        _add_trace_id,
        _add_service_metadata(service, version),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if fmt == "console" or (fmt == "auto" and sys.stderr.isatty()):
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(colors=False)
    else:
        renderer = structlog.processors.JSONRenderer(sort_keys=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib logs through structlog so libraries' logs are JSON too.
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(_StdlibBridgeFormatter(processors=[*shared_processors, renderer]))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)
    # Quiet very noisy libraries unless explicitly debug.
    for noisy in ("websockets", "httpx", "httpcore", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(max(log_level, logging.WARNING))

    _CONFIGURED = True


class _StdlibBridgeFormatter(logging.Formatter):
    def __init__(self, processors: list[structlog.types.Processor]) -> None:
        super().__init__()
        self._processors = processors

    def format(self, record: logging.LogRecord) -> str:
        event_dict: dict[str, Any] = {
            "event": record.getMessage(),
            "logger": record.name,
            "level": record.levelname.lower(),
        }
        if record.exc_info:
            event_dict["exc_info"] = record.exc_info
        # The processor chain ends in a renderer that returns a `str`; mypy
        # cannot narrow that across the loop, so `rendered` is intentionally Any.
        rendered: Any = event_dict
        for proc in self._processors:
            rendered = proc(None, record.levelname.lower(), rendered)
        return rendered if isinstance(rendered, str) else str(rendered)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name).bind(logger=name)  # type: ignore[no-any-return]


@contextmanager
def bind_trace_id(trace_id: str | None) -> Iterator[None]:
    token = _TRACE_ID.set(trace_id)
    try:
        yield
    finally:
        _TRACE_ID.reset(token)


def current_trace_id() -> str | None:
    return _TRACE_ID.get()


__all__ = [
    "bind_trace_id",
    "configure_logging",
    "current_trace_id",
    "get_logger",
]
