"""Runtime: application orchestrator, lifecycle, mode-aware worker supervision.

The CLI's `paper`, `live`, and `run` commands all construct an `Application`
and run it under `anyio.run`. Phases 2-5 register additional `Worker`s on the
existing `WorkerSupervisor` rather than building parallel orchestrators.
"""

from __future__ import annotations

from solalpha.runtime.app import Application
from solalpha.runtime.workers import Worker, WorkerSupervisor

__all__ = ["Application", "Worker", "WorkerSupervisor"]
