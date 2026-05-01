"""Mode state machine: LIVE → DEGRADED_RPC → DEGRADED_EXEC → PAPER, plus HALT override."""
from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from core.state import JsonStore
from core.types import Mode, ModeState


_DEFAULT = {
    "mode": Mode.LIVE.value,
    "since_ts": 0.0,
    "reason": "boot",
    "manual_override": False,
    "history": [],
}


class ModeManager:
    def __init__(self, state_dir: str = "data/state", initial: Mode | str = Mode.LIVE,
                 paper_exits_via_live: bool = True, halt_path: Optional[str] = None):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = JsonStore(self.state_dir / "mode.json", dict(_DEFAULT))
        self.paper_exits_via_live = paper_exits_via_live
        self.halt_path = Path(halt_path) if halt_path else self.state_dir / "HALT"
        self._live_executor = None
        self._mock_executor = None
        # health counters
        self._stream_silent_since: dict[str, float] = {}
        self._stream_last_ok: dict[str, float] = {}
        self._consec_not_confirmed = 0
        self._submit_fail_history: list[float] = []
        self._stable_since: float = time.time()
        # initial state
        s = self.store.load()
        if not s.get("history") or s.get("mode") != Mode(initial).value:
            s["mode"] = Mode(initial).value
            s["since_ts"] = time.time()
            s["reason"] = "boot"
            s.setdefault("history", []).append({
                "from": "init", "to": s["mode"], "ts": time.time(), "reason": "boot"
            })
            self.store.save(s)

    # ───── executor binding ─────
    def bind_executors(self, live, mock) -> None:
        self._live_executor = live
        self._mock_executor = mock

    def executor(self):
        m = self.current().mode
        if m in (Mode.LIVE, Mode.DEGRADED_RPC):
            return self._live_executor or self._mock_executor
        return self._mock_executor or self._live_executor

    def is_halted(self) -> bool:
        return self.halt_path.exists() or self.current().mode == Mode.HALT

    # ───── reads ─────
    def current(self) -> ModeState:
        s = self.store.load()
        return ModeState(
            mode=Mode(s.get("mode", Mode.LIVE.value)),
            since_ts=float(s.get("since_ts", 0.0)),
            reason=str(s.get("reason", "")),
            manual_override=bool(s.get("manual_override", False)),
            history=list(s.get("history", [])),
        )

    # ───── reports ─────
    def report_stream_health(self, endpoint: str, ok: bool, latency_ms: float = 0.0) -> None:
        now = time.time()
        if ok:
            self._stream_last_ok[endpoint] = now
            self._stream_silent_since.pop(endpoint, None)
        else:
            self._stream_silent_since.setdefault(endpoint, now)

    def report_exec_result(self, ok: bool, error: Optional[str] = None) -> None:
        now = time.time()
        if ok:
            self._consec_not_confirmed = 0
            return
        if error == "not_confirmed":
            self._consec_not_confirmed += 1
        self._submit_fail_history = [t for t in self._submit_fail_history if now - t < 600]
        self._submit_fail_history.append(now)

    # ───── transitions ─────
    def write_halt(self, reason: str = "manual") -> None:
        self.halt_path.parent.mkdir(parents=True, exist_ok=True)
        self.halt_path.write_text(f"reason={reason}\nts={time.time()}\n")
        self._set(Mode.HALT, reason=f"halt_file:{reason}")

    def clear_halt(self) -> None:
        try:
            self.halt_path.unlink()
        except FileNotFoundError:
            pass
        # On clear, return to LIVE (operator decision)
        self._set(Mode.LIVE, reason="halt_cleared")

    def manual_paper(self, reason: str = "manual") -> None:
        s = self.store.load()
        s["manual_override"] = True
        self.store.save(s)
        self._set(Mode.PAPER, reason=f"manual:{reason}")

    def clear_manual(self) -> None:
        s = self.store.load()
        s["manual_override"] = False
        self.store.save(s)
        self._set(Mode.LIVE, reason="manual_cleared")

    def evaluate(self, now: Optional[float] = None) -> Mode:
        """Recompute mode based on health metrics. Should be called every 30s + on errors."""
        now = now if now is not None else time.time()
        cur = self.current().mode

        # 1. HALT sentinel always wins
        if self.halt_path.exists():
            if cur != Mode.HALT:
                self._set(Mode.HALT, reason="halt_file_present")
            return Mode.HALT

        # 2. Manual override: stay in PAPER until cleared
        s = self.store.load()
        if s.get("manual_override", False):
            if cur != Mode.PAPER:
                self._set(Mode.PAPER, reason="manual_override")
            return Mode.PAPER

        # 3. Health-based transitions
        all_silent_60 = self._all_streams_silent(60.0, now) if self._stream_last_ok else False
        consec_not_conf = self._consec_not_confirmed
        submit_fail_10m = len(self._submit_fail_history)

        if consec_not_conf >= 3 or submit_fail_10m > 5:
            if cur != Mode.DEGRADED_EXEC:
                self._set(Mode.DEGRADED_EXEC, reason=f"exec_fail:not_conf={consec_not_conf} fails={submit_fail_10m}")
                self._stable_since = now
            # escalate to PAPER if degraded for 60 min
            if cur == Mode.DEGRADED_EXEC and now - self.current().since_ts > 3600:
                self._set(Mode.PAPER, reason="degraded_exec_sustained_60min")
            return self.current().mode

        if all_silent_60:
            if cur != Mode.DEGRADED_RPC:
                self._set(Mode.DEGRADED_RPC, reason="all_streams_silent_60s")
                self._stable_since = now
            # escalate after 5 min unrecovered
            if cur == Mode.DEGRADED_RPC and now - self.current().since_ts > 300:
                self._set(Mode.DEGRADED_EXEC, reason="rpc_unrecovered_5min")
            return self.current().mode

        # Recovery
        if cur == Mode.DEGRADED_RPC and (now - self._stable_since) > 300:
            self._set(Mode.LIVE, reason="rpc_recovered")
            return Mode.LIVE
        if cur == Mode.DEGRADED_EXEC and consec_not_conf == 0 and submit_fail_10m == 0 \
                and (now - self._stable_since) > 1800:
            self._set(Mode.LIVE, reason="exec_recovered")
            return Mode.LIVE

        return cur

    def _all_streams_silent(self, seconds: float, now: float) -> bool:
        if not self._stream_last_ok:
            return False
        return all(now - last > seconds for last in self._stream_last_ok.values())

    def _set(self, new_mode: Mode, reason: str = "") -> None:
        s = self.store.load()
        prev = s.get("mode", Mode.LIVE.value)
        if prev == new_mode.value:
            return
        s["mode"] = new_mode.value
        s["since_ts"] = time.time()
        s["reason"] = reason
        s.setdefault("history", []).append({
            "from": prev, "to": new_mode.value, "ts": time.time(), "reason": reason
        })
        # keep history bounded
        s["history"] = s["history"][-200:]
        self.store.save(s)
        self._stable_since = time.time()
