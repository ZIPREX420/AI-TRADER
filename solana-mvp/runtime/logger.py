"""SQLite trade/signal/skip log + JSONL telemetry."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

from core.types import Candidate, ExecResult

log = logging.getLogger("runtime.logger")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_open REAL NOT NULL,
    ts_close REAL,
    mint TEXT NOT NULL,
    side TEXT NOT NULL,
    sol_in REAL,
    sol_out REAL,
    fees REAL,
    roi REAL,
    sig TEXT,
    error TEXT,
    mode TEXT,
    candidate_json TEXT,
    exec_result_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_mint ON trades(mint);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts_open);

CREATE TABLE IF NOT EXISTS signals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mint TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_mint ON signals(mint);

CREATE TABLE IF NOT EXISTS skips(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mint TEXT,
    reason TEXT,
    candidate_json TEXT
);
"""


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if is_dataclass(v):
        return asdict(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    try:
        return str(v)
    except Exception:
        return None


def _json(v: Any) -> str:
    try:
        return json.dumps(_jsonable(v), default=str)
    except Exception:
        return "{}"


class TradeLog:
    def __init__(self, db_path: str | Path, telemetry_path: Optional[str | Path] = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.telemetry_path = Path(telemetry_path) if telemetry_path else self.db_path.parent / "telemetry.jsonl"
        self._init_schema()

    def _init_schema(self) -> None:
        db = sqlite3.connect(str(self.db_path))
        try:
            db.executescript(_SCHEMA)
            db.commit()
        finally:
            db.close()

    # ───── trades ─────
    def entry(self, candidate: Candidate, exec_result: ExecResult, mode: str) -> int:
        db = sqlite3.connect(str(self.db_path))
        try:
            cur = db.execute(
                """INSERT INTO trades(ts_open, mint, side, sol_in, sig, error, mode,
                                      candidate_json, exec_result_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    exec_result.submit_ts or time.time(),
                    candidate.mint,
                    str(candidate.side.value if hasattr(candidate.side, "value") else candidate.side),
                    exec_result.in_amount / 1e9,
                    exec_result.sig,
                    exec_result.error,
                    mode,
                    _json(candidate),
                    _json(exec_result),
                ),
            )
            db.commit()
            return int(cur.lastrowid or 0)
        finally:
            db.close()

    def exit(self, trade_id: int, mint: str, sol_out: float, fees: float,
             roi: float, sig: Optional[str], error: Optional[str], mode: str,
             exec_result: Optional[ExecResult] = None) -> None:
        db = sqlite3.connect(str(self.db_path))
        try:
            db.execute(
                """UPDATE trades SET ts_close=?, sol_out=?, fees=?, roi=?,
                                     sig=COALESCE(?, sig), error=COALESCE(?, error),
                                     mode=?, exec_result_json=COALESCE(?, exec_result_json)
                   WHERE id=?""",
                (
                    time.time(), sol_out, fees, roi, sig, error, mode,
                    _json(exec_result) if exec_result else None,
                    trade_id,
                ),
            )
            db.commit()
        finally:
            db.close()

    def skip(self, candidate: Optional[Candidate], reason: str) -> None:
        db = sqlite3.connect(str(self.db_path))
        try:
            db.execute(
                "INSERT INTO skips(ts, mint, reason, candidate_json) VALUES(?,?,?,?)",
                (time.time(), getattr(candidate, "mint", None), reason, _json(candidate)),
            )
            db.commit()
        finally:
            db.close()

    def failure(self, candidate: Candidate, exec_result: ExecResult) -> None:
        db = sqlite3.connect(str(self.db_path))
        try:
            db.execute(
                """INSERT INTO trades(ts_open, mint, side, sol_in, sig, error, mode,
                                      candidate_json, exec_result_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    exec_result.submit_ts or time.time(),
                    candidate.mint,
                    str(candidate.side.value if hasattr(candidate.side, "value") else candidate.side),
                    exec_result.in_amount / 1e9,
                    exec_result.sig,
                    exec_result.error or "unknown",
                    exec_result.mode or "live",
                    _json(candidate),
                    _json(exec_result),
                ),
            )
            db.commit()
        finally:
            db.close()

    def signal(self, mint: str, kind: str, payload: Any = None) -> None:
        db = sqlite3.connect(str(self.db_path))
        try:
            db.execute(
                "INSERT INTO signals(ts, mint, kind, payload_json) VALUES(?,?,?,?)",
                (time.time(), mint, kind, _json(payload)),
            )
            db.commit()
        finally:
            db.close()

    # ───── reads ─────
    def tail(self, n: int = 20) -> list[dict]:
        db = sqlite3.connect(str(self.db_path))
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT id, ts_open, ts_close, mint, side, sol_in, sol_out, roi, sig, "
                "error, mode FROM trades ORDER BY id DESC LIMIT ?",
                (int(n),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()

    # ───── telemetry ─────
    def telemetry(self, kind: str, **fields: Any) -> None:
        line = {"ts": time.time(), "kind": kind, **{k: _jsonable(v) for k, v in fields.items()}}
        try:
            self.telemetry_path.parent.mkdir(parents=True, exist_ok=True)
            with self.telemetry_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, default=str) + "\n")
        except Exception:
            log.debug("telemetry write failed", exc_info=True)
