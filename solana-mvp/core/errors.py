"""Explicit exception hierarchy."""
from __future__ import annotations


class MvpError(Exception):
    """Base."""


class ConfigError(MvpError):
    """Bad/missing config."""


class TransientError(MvpError):
    """Retryable."""


class FatalError(MvpError):
    """Non-retryable; halt path."""


class QuoteError(TransientError):
    pass


class RouteError(MvpError):
    pass


class BuildError(MvpError):
    pass


class SignError(FatalError):
    pass


class SubmitError(TransientError):
    pass


class ConfirmTimeout(TransientError):
    pass


class HaltedError(FatalError):
    pass
