"""SQLite trade log + Telegram alerter."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import aiosqlite
import httpx

log = logging.getLogger("log_store")


SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    mint TEXT NOT NULL,
    source TEXT,
    score REAL,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_mint ON signals(mint);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    side TEXT NOT NULL,
    mint TEXT NOT NULL,
    sol_amount REAL,
    token_amount REAL,
    price_sol REAL,
    signature TEXT,
    source TEXT,
    dry_run INTEGER,
    success INTEGER,
    error TEXT,
    elapsed_ms REAL,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_mint ON trades(mint);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);

CREATE TABLE IF NOT EXISTS pnl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mint TEXT NOT NULL,
    realized_sol REAL,
    realized_usd REAL,
    note TEXT
);
"""


class TradeLog:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def log_signal(self, kind: str, mint: str, source: str, score: float, payload: dict):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO signals(ts,kind,mint,source,score,payload) VALUES(?,?,?,?,?,?)",
                (time.time(), kind, mint, source, score, json.dumps(payload, default=str)),
            )
            await db.commit()

    async def log_trade(
        self,
        *,
        side: str,
        mint: str,
        sol_amount: float,
        token_amount: float,
        price_sol: float,
        signature: str | None,
        source: str,
        dry_run: bool,
        success: bool,
        error: str | None,
        elapsed_ms: float,
        payload: dict | None = None,
    ):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO trades(ts,side,mint,sol_amount,token_amount,price_sol,signature,source,
                                      dry_run,success,error,elapsed_ms,payload)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(), side, mint, sol_amount, token_amount, price_sol,
                    signature, source, int(dry_run), int(success), error, elapsed_ms,
                    json.dumps(payload or {}, default=str),
                ),
            )
            await db.commit()

    async def log_pnl(self, mint: str, realized_sol: float, sol_price_usd: float, note: str = ""):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO pnl(ts,mint,realized_sol,realized_usd,note) VALUES(?,?,?,?,?)",
                (time.time(), mint, realized_sol, realized_sol * sol_price_usd, note),
            )
            await db.commit()

    async def realized_pnl_today_sol(self) -> float:
        async with aiosqlite.connect(self.db_path) as db:
            cutoff = time.time() - 86_400
            cur = await db.execute(
                "SELECT COALESCE(SUM(realized_sol),0) FROM pnl WHERE ts > ?", (cutoff,)
            )
            row = await cur.fetchone()
            return float(row[0]) if row else 0.0

    async def tail(self, n: int = 20) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT ts,side,mint,sol_amount,token_amount,signature,success,error,source "
                "FROM trades ORDER BY id DESC LIMIT ?", (n,)
            )
            return [dict(r) for r in await cur.fetchall()]


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)

    async def send(self, text: str):
        if not self.enabled:
            return
        try:
            async with httpx.AsyncClient(timeout=5.0) as h:
                await h.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
                )
        except Exception as e:
            log.debug(f"telegram error: {e!r}")
