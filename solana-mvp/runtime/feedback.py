"""Append live_outcomes parquet rows + EWMA pattern_state.json. Refuses canonical writes."""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

from core.state import JsonStore

log = logging.getLogger("runtime.feedback")

# Live writes are confined to these paths. Canonical v6 state files are forbidden.
ALLOWED_OUTPUT_DIRS = {
    "data/research_in/live_outcomes",
    "data/state",
    "data/logs",
}
FORBIDDEN_LIVE_FILES = {
    "data/lr_weights.json",
    "data/source_priors.json",
    "data/strategy_priors.json",
    "data/wallet_overrides.json",
    "data/disabled_sources.json",
    "data/params.json",
    "data/regime.json",
    "data/allocations.json",
    "data/smart_wallets.json",
    "data/smart_wallets_scored.json",
}


def _is_allowed(path: Path) -> bool:
    norm = str(path).replace("\\", "/")
    if norm in FORBIDDEN_LIVE_FILES:
        return False
    return any(norm.startswith(prefix) for prefix in ALLOWED_OUTPUT_DIRS)


def _to_jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if is_dataclass(v):
        return asdict(v)
    if isinstance(v, dict):
        return {k: _to_jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_to_jsonable(x) for x in v]
    try:
        return str(v)
    except Exception:
        return None


class Feedback:
    def __init__(self, base_dir: str | Path = "data/research_in/live_outcomes",
                 pattern_state_path: str | Path = "data/state/pattern_state.json"):
        self.base_dir = Path(base_dir)
        self.pattern_state_path = Path(pattern_state_path)
        if not _is_allowed(self.base_dir):
            raise PermissionError(f"feedback base_dir not allowed: {base_dir}")
        if not _is_allowed(self.pattern_state_path):
            raise PermissionError(f"pattern_state_path not allowed: {pattern_state_path}")
        self.pattern_store = JsonStore(self.pattern_state_path, default={})

    # ───── parquet append ─────
    def write_close(
        self,
        *,
        trade_id: int,
        mint: str,
        pattern_id: Optional[str],
        sol_in: float,
        sol_out: float,
        fees: float,
        roi: float,
        peak_ratio: float,
        hold_s: float,
        exit_kind: str,
        confidence_at_entry: float,
        regime_at_entry: str,
        wallets: list[str],
        cluster_ids: list[str],
        fingerprint: str,
        mode: str,
    ) -> Path:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except Exception as e:
            raise RuntimeError(f"pyarrow not available: {e}")
        row = {
            "trade_id": int(trade_id),
            "ts_open": time.time() - max(hold_s, 0.0),
            "ts_close": time.time(),
            "mint": str(mint),
            "pattern_id": str(pattern_id or ""),
            "fingerprint": str(fingerprint or ""),
            "wallets_json": json.dumps(_to_jsonable(wallets)),
            "cluster_ids_json": json.dumps(_to_jsonable(cluster_ids)),
            "sol_in": float(sol_in),
            "sol_out": float(sol_out),
            "fees": float(fees),
            "roi": float(roi),
            "peak_ratio": float(peak_ratio),
            "hold_s": float(hold_s),
            "exit_kind": str(exit_kind),
            "confidence_at_entry": float(confidence_at_entry),
            "regime_at_entry": str(regime_at_entry or "NORMAL"),
            "mode": str(mode or "live"),
        }
        date_part = dt.datetime.utcfromtimestamp(row["ts_close"]).strftime("%Y-%m-%d")
        out_dir = self.base_dir / f"dt={date_part}"
        out_dir.mkdir(parents=True, exist_ok=True)
        n_existing = sum(1 for _ in out_dir.glob("part-*.parquet"))
        out_path = out_dir / f"part-{n_existing:05d}.parquet"
        if not _is_allowed(out_path):
            raise PermissionError(f"output path not allowed: {out_path}")
        table = pa.Table.from_pylist([row])
        pq.write_table(table, out_path, compression="zstd")
        return out_path

    # ───── pattern state EWMA ─────
    def update_pattern_state(self, pattern_id: Optional[str], roi: float,
                             alpha: float = 0.05) -> dict:
        if not pattern_id:
            return {}
        state = self.pattern_store.load() or {}
        rec = state.get(pattern_id) or {"live_S": 0.0, "n": 0, "boot_alloc": 0.05}
        clipped = max(-1.0, min(1.0, float(roi)))
        rec["live_S"] = (1.0 - alpha) * float(rec.get("live_S", 0.0)) + alpha * clipped
        rec["n"] = int(rec.get("n", 0)) + 1
        # Boot allocation adjustment based on EWMA quality
        if rec["n"] >= 10:
            if rec["live_S"] > 0.10:
                rec["boot_alloc"] = min(0.20, float(rec.get("boot_alloc", 0.05)) * 1.20)
            elif rec["live_S"] < 0.0:
                rec["boot_alloc"] = max(0.05, float(rec.get("boot_alloc", 0.05)) * 0.50)
        state[pattern_id] = rec
        self.pattern_store.save(state)
        return rec
