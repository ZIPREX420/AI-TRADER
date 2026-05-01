"""Execution-aware risk gates + watchdog. State persisted to data/state/risk_exec_state.json."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .state import JsonStore, atomic_write_json
from .types import Candidate, Decision, Mode, Side


_DEFAULT_STATE = {
    "consec_not_confirmed": 0,
    "slip_history_10": [],
    "sandwich_flags_1h": [],
    "critical_errors_60s": [],
    "submit_fail_history_10m": [],
    "quarantine": {},
    "blacklist": {},
    "size_dampener": {"factor": 1.0, "until_trade_n": 0},
    "trade_n": 0,
    "day_open_equity": 0.0,
    "loss_streak": 0,
}


@dataclass
class RiskCtx:
    sol_balance_lamports: int = 1_000_000_000
    token_balance_raw: int = 0
    expected_fee_lamports: int = 0
    expected_slip_bps: int = 0
    expected_size_lamports: int = 0
    last_latencies_ms: tuple[float, ...] = ()
    mode: Mode = Mode.LIVE
    sol_reserve_lamports: int = 50_000_000  # 0.05 SOL
    fee_threshold_pct: float = 0.015
    latency_p50_max_s: float = 1.5


class RiskExec:
    CRITICAL_ERRORS = {"balance_low", "halted", "sandwich_flagged"}

    def __init__(self, state_dir: str = "data/state"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = JsonStore(self.state_dir / "risk_exec_state.json", dict(_DEFAULT_STATE))
        self.pending_store = JsonStore(self.state_dir / "pending_submissions.json", {})
        self.halt_path = self.state_dir / "HALT"
        self._touch_halt_dir_only()

    def _touch_halt_dir_only(self) -> None:
        # Ensure parent dir exists; do NOT create HALT sentinel.
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # ───────────────────────────── pre-trade gates ─────────────────────────────
    def pre_trade_gates(self, candidate: Candidate, ctx: RiskCtx) -> Decision:
        if self.is_halted():
            return Decision(False, "halted")
        if ctx.mode in (Mode.HALT,):
            return Decision(False, "halted")
        if candidate.mint in self._active_quarantine():
            return Decision(False, "quarantined")
        if candidate.mint in self._active_blacklist():
            return Decision(False, "blacklisted")
        if candidate.side == Side.BUY:
            need = ctx.expected_size_lamports + ctx.sol_reserve_lamports
            if ctx.sol_balance_lamports < need:
                return Decision(False, "balance_low")
        else:
            if ctx.token_balance_raw <= 0:
                return Decision(False, "token_balance_zero")
        # fee threshold: (priority_fee + tip + slippage_loss_est) ≤ size · pct
        size = max(ctx.expected_size_lamports, 1)
        slip_loss = int(size * (ctx.expected_slip_bps / 10_000.0))
        total_friction = ctx.expected_fee_lamports + slip_loss
        if total_friction > size * ctx.fee_threshold_pct:
            return Decision(False, f"fee_threshold:{total_friction}>{size*ctx.fee_threshold_pct:.0f}")
        # latency gate: median of last 10 detect→submit ≤ 1.5s
        if ctx.last_latencies_ms:
            sorted_l = sorted(ctx.last_latencies_ms)
            p50 = sorted_l[len(sorted_l) // 2]
            if p50 > ctx.latency_p50_max_s * 1000.0:
                return Decision(False, f"latency_p50:{p50:.0f}ms")
        return Decision(True, "ok")

    # ───────────────────────────── outcome recording ─────────────────────────────
    def record_close(self, roi: float, realized_slip: float, expected_slip: float, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        s = self.store.load()
        # rolling slip history (last 10)
        s["slip_history_10"] = (s.get("slip_history_10", []) + [realized_slip])[-10:]
        # sandwich detection
        if expected_slip > 0 and realized_slip > 3 * expected_slip:
            s["sandwich_flags_1h"] = [t for t in s.get("sandwich_flags_1h", []) if now - t < 3600]
            s["sandwich_flags_1h"].append(now)
        # size dampener: if avg_slip_10 > 0.08 → factor 0.5 for 20 trades
        slips = s["slip_history_10"]
        if slips and (sum(slips) / len(slips)) > 0.08:
            s["size_dampener"] = {"factor": 0.5, "until_trade_n": s.get("trade_n", 0) + 20}
        # streak
        if roi < 0:
            s["loss_streak"] = s.get("loss_streak", 0) + 1
        else:
            s["loss_streak"] = 0
        s["trade_n"] = s.get("trade_n", 0) + 1
        self.store.save(s)

    def record_submit_outcome(self, ok: bool, error: Optional[str] = None, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        s = self.store.load()
        if ok:
            s["consec_not_confirmed"] = 0
        else:
            if error == "not_confirmed":
                s["consec_not_confirmed"] = s.get("consec_not_confirmed", 0) + 1
            # rolling submit-fail window 10min
            s["submit_fail_history_10m"] = [t for t in s.get("submit_fail_history_10m", []) if now - t < 600]
            s["submit_fail_history_10m"].append(now)
            if error in self.CRITICAL_ERRORS:
                s["critical_errors_60s"] = [t for t in s.get("critical_errors_60s", []) if now - t < 60]
                s["critical_errors_60s"].append(now)
                if len(s["critical_errors_60s"]) >= 3:
                    self.write_halt(reason="auto_emergency_3_critical_60s")
        self.store.save(s)

    # ───────────────────────────── quarantine / blacklist ─────────────────────────────
    def quarantine(self, mint: str, until_ts: float) -> None:
        s = self.store.load()
        s.setdefault("quarantine", {})[mint] = until_ts
        self.store.save(s)

    def blacklist(self, mint: str, until_ts: float) -> None:
        s = self.store.load()
        s.setdefault("blacklist", {})[mint] = until_ts
        self.store.save(s)

    def _active_quarantine(self) -> set[str]:
        now = time.time()
        s = self.store.load()
        return {m for m, until in s.get("quarantine", {}).items() if until > now}

    def _active_blacklist(self) -> set[str]:
        now = time.time()
        s = self.store.load()
        return {m for m, until in s.get("blacklist", {}).items() if until > now}

    # ───────────────────────────── pending tx watchdog ─────────────────────────────
    def add_pending(self, sig: str, mint: str, fee: int, mode: str = "live") -> None:
        st = self.pending_store.load() or {}
        st[sig] = {"submit_ts": time.time(), "fee": fee, "mint": mint, "mode": mode}
        self.pending_store.save(st)

    def remove_pending(self, sig: str) -> None:
        st = self.pending_store.load() or {}
        st.pop(sig, None)
        self.pending_store.save(st)

    def watchdog_tick(self, now: Optional[float] = None) -> dict:
        """Returns dict of {sig: action} where action in {ghost_cancel, abandon}."""
        now = now if now is not None else time.time()
        st = self.pending_store.load() or {}
        actions: dict[str, str] = {}
        for sig, info in list(st.items()):
            age = now - float(info.get("submit_ts", now))
            if age > 120:
                actions[sig] = "abandon"
                st.pop(sig, None)
            elif age > 60:
                actions[sig] = "ghost_cancel"
        self.pending_store.save(st)
        return actions

    # ───────────────────────────── halt ─────────────────────────────
    def is_halted(self) -> bool:
        return self.halt_path.exists()

    def write_halt(self, reason: str = "manual") -> None:
        self.halt_path.parent.mkdir(parents=True, exist_ok=True)
        self.halt_path.write_text(f"reason={reason}\nts={time.time()}\n")

    def clear_halt(self) -> None:
        try:
            self.halt_path.unlink()
        except FileNotFoundError:
            pass

    # ───────────────────────────── helpers ─────────────────────────────
    def size_dampener_factor(self) -> float:
        s = self.store.load()
        d = s.get("size_dampener", {"factor": 1.0, "until_trade_n": 0})
        if s.get("trade_n", 0) >= int(d.get("until_trade_n", 0)):
            return 1.0
        return float(d.get("factor", 1.0))

    def jito_only_mode(self) -> bool:
        """Active if ≥3 sandwich flags in last hour."""
        now = time.time()
        s = self.store.load()
        flags = [t for t in s.get("sandwich_flags_1h", []) if now - t < 3600]
        return len(flags) >= 3

    def consec_not_confirmed(self) -> int:
        return int(self.store.load().get("consec_not_confirmed", 0))

    def submit_fail_count_10m(self) -> int:
        now = time.time()
        s = self.store.load()
        recent = [t for t in s.get("submit_fail_history_10m", []) if now - t < 600]
        return len(recent)
