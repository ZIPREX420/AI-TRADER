"""Runtime-mode domain models.

`ModeState` is what the mode manager publishes on the `mode` topic; every
transition is also persisted to the `mode_transitions` table. `ModeStr` is the
canonical mode enumeration, re-exported here from `foundation.config` so the
domain package is the single import surface for mode types.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from solalpha.foundation.config import ModeStr


class ModeState(BaseModel):
    """The current runtime mode, with the reason and time it was entered."""

    model_config = ConfigDict(frozen=True)

    mode: ModeStr
    reason: str
    since: datetime


class ModeTransition(BaseModel):
    """A recorded mode change (mirrors the `mode_transitions` table)."""

    model_config = ConfigDict(frozen=True)

    from_mode: ModeStr
    to_mode: ModeStr
    reason: str
    ts: datetime


__all__ = [
    "ModeState",
    "ModeStr",
    "ModeTransition",
]
