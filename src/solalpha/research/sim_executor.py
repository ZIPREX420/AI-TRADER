"""Simulated executor for replay.

Thin pass-through over `PaperExecutor`: replay runs always use the paper
executor (never the live one) so a replay session is bit-for-bit
deterministic, hardware-aside. This module exists so the research plane
can be discovered + tested without importing the entire execution
package's live dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from solalpha.execution.paper_executor import PaperExecutor

if TYPE_CHECKING:
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.config import AppConfig


def build_sim_executor(cfg: AppConfig, clock: Clock) -> PaperExecutor:
    """Construct a `PaperExecutor` configured for deterministic replay."""
    return PaperExecutor(cfg, clock)


__all__ = ["build_sim_executor"]
