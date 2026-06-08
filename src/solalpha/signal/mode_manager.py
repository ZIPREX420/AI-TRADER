"""Runtime mode state machine with hysteresis.

The manager subscribes to `HEALTH_TOPIC` (and consults `KillSwitch` plus the
`PortfolioTracker` daily PnL / loss streak) on every tick, then chooses a
target mode and only transitions once that target has been the preferred
choice for the configured hysteresis window.

`HALT` is the one exception: it has **no hysteresis** -- the moment a halt
condition is observed (kill switch armed, daily-loss limit hit, or loss
streak hit) the manager latches HALT immediately. Leaving HALT requires all
halt conditions to clear (e.g. operator `solalpha kill disarm`, or a fresh
UTC day for daily PnL); the next tick re-evaluates.

An **operator override** (`solalpha mode set PAPER`) writes a small JSON
file the manager consults every tick. While present it pins the runtime to
`PAPER` -- it outranks health-driven `LIVE`/`DEGRADED_*` selection but never
a real `HALT` condition, and like `HALT` it applies with no hysteresis so
the operator's "stop live trading now" intent takes effect immediately.
`solalpha mode clear` removes the file and hands control back to the health
gate.

Every transition is published on `MODE_TOPIC` and persisted to the
`mode_transitions` table.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from solalpha.domain import ModeState, ModeTransition
from solalpha.foundation import metrics
from solalpha.foundation.bus import MODE_TOPIC
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from solalpha.domain import ModeStr
    from solalpha.foundation.bus import Bus
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.config import AppConfig
    from solalpha.foundation.health import HealthRegistry, HealthSnapshot
    from solalpha.foundation.state import SqliteStore
    from solalpha.observability.portfolio import PortfolioTracker
    from solalpha.signal.kill_switch import KillSwitch

_log = get_logger(__name__)

# Names of the probes the manager inspects (registered by the data /
# execution planes when they wire themselves up; absent in Phase 1).
_RPC_PROBE = "rpc_pool"
_EXEC_PROBE = "jupiter"


class ModeManager:
    """Owns the runtime mode -- read by every worker, written only here."""

    def __init__(
        self,
        cfg: AppConfig,
        bus: Bus,
        store: SqliteStore,
        clock: Clock,
        kill_switch: KillSwitch,
        portfolio: PortfolioTracker,
        health_registry: HealthRegistry,
        *,
        tick_interval_s: float = 1.0,
    ) -> None:
        self._cfg = cfg
        self._bus = bus
        self._store = store
        self._clock = clock
        self._kill = kill_switch
        self._portfolio = portfolio
        self._health = health_registry
        self._tick_interval_s = tick_interval_s
        # Operator override probe file (written by `solalpha mode set`).
        self._operator_mode_path = cfg.persistence.data_dir / ".operator_mode"

        # Initial mode: from config (clamped to PAPER if live env-flag missing).
        self._mode: ModeStr = cfg.mode
        self._reason: str = "startup"
        self._since = clock.now()
        # `_pending[(from, to)] = monotonic_seconds_first_observed`.
        self._pending: dict[tuple[ModeStr, ModeStr], float] = {}
        metrics.MODE_GAUGE.labels(mode=self._mode).set(1)
        self._health.set_aux("mode", self._mode)

    # ---- public read ----

    def state(self) -> ModeState:
        return ModeState(mode=self._mode, reason=self._reason, since=self._since)

    @property
    def mode(self) -> ModeStr:
        return self._mode

    # ---- lifecycle ----

    async def run(self) -> None:
        """Periodic tick loop; subscribes to MODE_TOPIC consumers via Bus.publish."""
        # Publish initial state so subscribers see a value even before any tick.
        await self._publish(self.state())
        while True:
            try:
                snap = await self._health.snapshot()
                await self.tick(snap)
            except Exception as e:
                _log.warning("mode_manager_tick_error", exc=str(e), exc_type=type(e).__name__)
            await self._clock.sleep(self._tick_interval_s)

    async def tick(self, snapshot: HealthSnapshot) -> None:
        """Evaluate the current observations and possibly transition."""
        override = self._operator_override()
        target, reason = await self._evaluate_target(snapshot, override)
        if target == self._mode:
            # Clear any pending transitions out of `_mode` since we're stable.
            self._pending = {k: v for k, v in self._pending.items() if k[0] != self._mode}
            return

        # No hysteresis when HALT is involved (either direction) or when an
        # operator override is active -- both express an explicit, immediate
        # "change now" intent (the RUNBOOK's "solalpha kill disarm" /
        # "solalpha mode set" must take effect promptly).
        if target == "HALT" or self._mode == "HALT" or override is not None:
            await self._transition(target, reason)
            return

        key = (self._mode, target)
        now_mono = self._clock.monotonic()
        first = self._pending.get(key)
        if first is None:
            self._pending[key] = now_mono
            return
        threshold = self._hysteresis_window(self._mode, target)
        if now_mono - first >= threshold:
            await self._transition(target, reason)

    # ---- decision ----

    def _operator_override(self) -> tuple[ModeStr, str] | None:
        """Read the operator-mode probe file, if present and valid.

        Returns `(mode, reason)` for a recognised operator mode, else `None`.
        Any read/parse error is swallowed (and logged) so a corrupt probe
        file can never wedge the mode loop -- a missing or unreadable file
        simply means "no override".
        """
        path = self._operator_mode_path
        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _log.warning("operator_mode_read_failed", exc=str(e))
            return None
        if not isinstance(data, dict):
            _log.warning("operator_mode_malformed", note="probe file is not a JSON object")
            return None
        mode = data.get("mode")
        reason = str(data.get("reason", "")) or "no-reason"
        # Only PAPER may be pinned by an operator: LIVE / DEGRADED_* are
        # health-driven, and HALT has its own surface (`solalpha kill arm`).
        if mode == "PAPER":
            return "PAPER", reason
        _log.warning("operator_mode_invalid", mode=mode)
        return None

    async def _evaluate_target(
        self, snapshot: HealthSnapshot, override: tuple[ModeStr, str] | None
    ) -> tuple[ModeStr, str]:
        # 1. HALT conditions take absolute precedence.
        if self._kill.armed():
            return "HALT", f"kill switch armed: {self._kill.reason() or 'no-reason'}"
        if self._portfolio.loss_streak() >= self._cfg.risk.loss_streak_max:
            return "HALT", f"loss streak hit ({self._portfolio.loss_streak()})"
        daily = await self._portfolio.daily_pnl()
        equity = self._cfg.risk.starting_equity_usd
        if equity > 0 and daily.pnl_usd <= -equity * self._cfg.risk.daily_loss_pct:
            return (
                "HALT",
                f"daily loss limit hit (pnl={daily.pnl_usd:.2f} on equity={equity:.2f})",
            )

        # 2. Operator override outranks health-driven selection (it can never
        #    outrank a real HALT condition -- those are handled above).
        if override is not None:
            ov_mode, ov_reason = override
            return ov_mode, f"operator override: {ov_reason}"

        # 3. If we're in HALT and no halt condition fires, fall back to PAPER.
        if self._mode == "HALT":
            return "PAPER", "halt conditions cleared"

        # 4. Component-driven degradation.
        rpc = snapshot.components.get(_RPC_PROBE)
        exec_probe = snapshot.components.get(_EXEC_PROBE)
        rpc_down = rpc is not None and rpc.status == "down"
        rpc_degraded = rpc is not None and rpc.status != "ok"
        exec_down = exec_probe is not None and exec_probe.status == "down"

        if rpc_down and exec_down:
            return "PAPER", "all upstreams down"
        if rpc_degraded:
            return "DEGRADED_RPC", "rpc pool degraded"
        if exec_down:
            return "DEGRADED_EXEC", "jupiter probe down"

        # 5. Healthy: PAPER if not live-eligible, otherwise LIVE.
        if not self._cfg.is_live_eligible():
            return "PAPER", "live trading not enabled"
        return "LIVE", "healthy and live-eligible"

    def _hysteresis_window(self, frm: ModeStr, to: ModeStr) -> float:
        mm = self._cfg.mode_manager
        # PAPER -> LIVE requires the long "sustained health" window.
        if frm == "PAPER" and to == "LIVE":
            return mm.paper_to_live_health_s
        if frm == "LIVE" and to == "DEGRADED_RPC":
            return mm.hysteresis_live_to_degraded_rpc_s
        if frm == "DEGRADED_RPC" and to == "LIVE":
            return mm.hysteresis_degraded_rpc_to_live_s
        if frm == "LIVE" and to == "DEGRADED_EXEC":
            return mm.hysteresis_live_to_degraded_exec_s
        if frm == "DEGRADED_EXEC" and to == "LIVE":
            return mm.hysteresis_degraded_exec_to_live_s
        if to == "PAPER":
            return mm.hysteresis_to_paper_s
        # Conservative default for unspecified pairs -- match the slowest window.
        return mm.hysteresis_to_paper_s

    # ---- mutation ----

    async def _transition(self, target: ModeStr, reason: str) -> None:
        previous = self._mode
        now = self._clock.now()
        await self._store.execute(
            "INSERT INTO mode_transitions (from_mode, to_mode, reason, ts) VALUES (?, ?, ?, ?)",
            (previous, target, reason, now.isoformat()),
        )
        # Update Prometheus gauge: clear all labels then set the new one.
        for m in ("LIVE", "DEGRADED_RPC", "DEGRADED_EXEC", "PAPER", "HALT"):
            metrics.MODE_GAUGE.labels(mode=m).set(1 if m == target else 0)
        self._health.set_aux("mode", target)
        self._mode = target
        self._reason = reason
        self._since = now
        self._pending.clear()
        state = ModeState(mode=target, reason=reason, since=now)
        await self._publish(state)
        transition = ModeTransition(from_mode=previous, to_mode=target, reason=reason, ts=now)
        _log.warning(
            "mode_transition",
            **transition.model_dump(mode="json"),
        )

    async def _publish(self, state: ModeState) -> None:
        topic = await self._bus.topic(MODE_TOPIC)
        await topic.publish(state)


__all__ = ["ModeManager"]
