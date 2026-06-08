"""Exception taxonomy for solalpha.

Every internal error derives from `SolalphaError`. The classifier
distinguishes transient from permanent failures so retry logic can
make safe decisions without parsing strings.
"""

from __future__ import annotations


class SolalphaError(Exception):
    """Base for every solalpha-raised exception."""

    transient: bool = False


class ConfigError(SolalphaError):
    """Configuration is missing, invalid, or violates a hard ceiling."""


class TransientError(SolalphaError):
    """Marker base for retryable failures."""

    transient = True


class PermanentError(SolalphaError):
    """Marker base for failures that should not be retried."""

    transient = False


# ---- Network / RPC ----


class RpcError(SolalphaError):
    def __init__(self, message: str, *, endpoint: str | None = None, code: int | None = None):
        super().__init__(message)
        self.endpoint = endpoint
        self.code = code


class RpcTransientError(RpcError, TransientError):
    """Connection reset, timeout, 5xx, etc."""


class RpcPermanentError(RpcError, PermanentError):
    """4xx (other than 429), explicit method-not-found, etc."""


class NoHealthyRpcError(RpcTransientError):
    """All endpoints are quarantined or down."""


class WebsocketError(RpcTransientError):
    """Websocket connection failed or dropped."""


# ---- Decoder ----


class DecodeError(PermanentError):
    """Transaction or instruction could not be parsed."""


class UnknownProgramError(DecodeError):
    """No registered decoder for the program id."""


# ---- Persistence ----


class PersistenceError(SolalphaError):
    pass


class StateCorruptionError(PersistenceError, PermanentError):
    pass


# ---- Risk / signal ----


class RiskBlocked(PermanentError):
    """Risk engine refused an intent. Carries the rule(s) that blocked."""

    def __init__(self, message: str, *, reasons: list[str] | None = None):
        super().__init__(message)
        self.reasons = list(reasons or [])


class RiskInternalError(PermanentError):
    """Risk engine itself raised — fail CLOSED."""


class KillSwitchArmed(RiskBlocked):
    """Kill switch is armed; all new orders rejected."""


# ---- Execution ----


class ExecutionError(SolalphaError):
    pass


class JupiterError(ExecutionError, TransientError):
    pass


class JupiterPermanentError(ExecutionError, PermanentError):
    pass


class RaydiumError(ExecutionError, TransientError):
    pass


class RouteUnavailable(ExecutionError, TransientError):
    pass


class BlockhashExpired(ExecutionError, TransientError):
    pass


class ExecutionFailed(ExecutionError, PermanentError):
    """Confirmed failure on chain or both RPCs reported error."""


class StuckTransaction(ExecutionError, TransientError):
    """Tx submitted but not confirmed within the timeout."""

    def __init__(self, message: str, *, signature: str | None = None):
        super().__init__(message)
        self.signature = signature


# ---- Research ----


class ResearchWriteBlocked(PermanentError):
    """Research code attempted to mutate live state."""


class ReplayDataError(PermanentError):
    """Replay session is missing data or has invalid ordering."""


# ---- Recovery ----


class RecoveryError(PermanentError):
    pass


__all__ = [
    "BlockhashExpired",
    "ConfigError",
    "DecodeError",
    "ExecutionError",
    "ExecutionFailed",
    "JupiterError",
    "JupiterPermanentError",
    "KillSwitchArmed",
    "NoHealthyRpcError",
    "PermanentError",
    "PersistenceError",
    "RaydiumError",
    "RecoveryError",
    "ReplayDataError",
    "ResearchWriteBlocked",
    "RiskBlocked",
    "RiskInternalError",
    "RouteUnavailable",
    "RpcError",
    "RpcPermanentError",
    "RpcTransientError",
    "SolalphaError",
    "StateCorruptionError",
    "StuckTransaction",
    "TransientError",
    "UnknownProgramError",
    "WebsocketError",
]
