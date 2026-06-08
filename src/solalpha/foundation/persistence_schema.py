"""SQLite schema (DDL) and migration runner.

Schema is versioned in `schema_version`; on connect the store applies any
missing migrations in order. Migrations are append-only — never edit existing
ones, only add new versions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

# Each migration is (version, description, sql).
# SQL may contain multiple statements separated by `;`.
MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial schema",
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS smart_wallets (
            wallet                  TEXT PRIMARY KEY,
            added_at                TEXT NOT NULL,
            source                  TEXT NOT NULL,
            weight                  REAL NOT NULL DEFAULT 1.0,
            score                   REAL NOT NULL DEFAULT 0.0,
            rolling_pnl_usd_30d     REAL NOT NULL DEFAULT 0.0,
            rolling_winrate_30d     REAL NOT NULL DEFAULT 0.0,
            last_active_at          TEXT,
            quarantined_until       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_smart_wallets_score ON smart_wallets(score DESC);

        CREATE TABLE IF NOT EXISTS signals (
            signal_id      TEXT PRIMARY KEY,
            created_at     TEXT NOT NULL,
            mint           TEXT NOT NULL,
            direction      TEXT NOT NULL,
            detectors_json TEXT NOT NULL,
            confidence     REAL NOT NULL,
            suggested_usd  REAL NOT NULL,
            rationale      TEXT NOT NULL,
            inputs_hash    TEXT NOT NULL,
            trace_id       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at);
        CREATE INDEX IF NOT EXISTS idx_signals_mint ON signals(mint);

        CREATE TABLE IF NOT EXISTS risk_decisions (
            signal_id        TEXT PRIMARY KEY,
            decision         TEXT NOT NULL,
            approved_usd     REAL NOT NULL,
            reasons_json     TEXT NOT NULL,
            ts               TEXT NOT NULL,
            mode_at_decision TEXT NOT NULL,
            FOREIGN KEY (signal_id) REFERENCES signals(signal_id)
        );

        CREATE TABLE IF NOT EXISTS orders (
            order_id                  TEXT PRIMARY KEY,
            signal_id                 TEXT,
            created_at                TEXT NOT NULL,
            mint                      TEXT NOT NULL,
            direction                 TEXT NOT NULL,
            intended_usd              REAL NOT NULL,
            intended_input_amount_raw INTEGER NOT NULL,
            max_slippage_bps          INTEGER NOT NULL,
            status                    TEXT NOT NULL,
            last_attempt              INTEGER NOT NULL DEFAULT 0,
            last_signature            TEXT,
            trace_id                  TEXT NOT NULL,
            FOREIGN KEY (signal_id) REFERENCES signals(signal_id)
        );
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_orders_mint   ON orders(mint);

        CREATE TABLE IF NOT EXISTS fills (
            fill_id                 TEXT PRIMARY KEY,
            order_id                TEXT NOT NULL,
            signature               TEXT NOT NULL UNIQUE,
            slot                    INTEGER NOT NULL,
            block_time              TEXT NOT NULL,
            input_amount_raw        INTEGER NOT NULL,
            output_amount_raw       INTEGER NOT NULL,
            realized_slippage_bps   INTEGER NOT NULL,
            fee_lamports            INTEGER NOT NULL,
            priority_fee_lamports   INTEGER NOT NULL,
            route_json              TEXT NOT NULL,
            usd_value               REAL,
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        );
        CREATE INDEX IF NOT EXISTS idx_fills_block_time ON fills(block_time);

        CREATE TABLE IF NOT EXISTS positions (
            position_id       TEXT PRIMARY KEY,
            mint              TEXT NOT NULL,
            opened_at         TEXT NOT NULL,
            closed_at         TEXT,
            cost_basis_usd    REAL NOT NULL DEFAULT 0.0,
            quantity_raw      INTEGER NOT NULL DEFAULT 0,
            quantity_ui       REAL NOT NULL DEFAULT 0.0,
            realized_pnl_usd  REAL NOT NULL DEFAULT 0.0,
            fills_json        TEXT NOT NULL DEFAULT '[]',
            state             TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_positions_state ON positions(state);
        CREATE INDEX IF NOT EXISTS idx_positions_mint  ON positions(mint);

        CREATE TABLE IF NOT EXISTS mode_transitions (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            from_mode TEXT NOT NULL,
            to_mode   TEXT NOT NULL,
            reason    TEXT NOT NULL,
            ts        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS quarantine (
            key    TEXT PRIMARY KEY,
            kind   TEXT NOT NULL,
            until  TEXT NOT NULL,
            reason TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS blacklist (
            key      TEXT PRIMARY KEY,
            kind     TEXT NOT NULL,
            added_at TEXT NOT NULL,
            reason   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS kill_switch (
            id     INTEGER PRIMARY KEY CHECK (id = 1),
            armed  INTEGER NOT NULL,
            reason TEXT,
            since  TEXT,
            by_who TEXT
        );
        INSERT OR IGNORE INTO kill_switch (id, armed) VALUES (1, 0);

        CREATE TABLE IF NOT EXISTS checkpoints (
            stream         TEXT PRIMARY KEY,
            last_slot      INTEGER NOT NULL DEFAULT 0,
            last_signature TEXT,
            ts             TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS journal (
            seq           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT NOT NULL,
            kind          TEXT NOT NULL,
            payload_json  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal(ts);

        CREATE TABLE IF NOT EXISTS health_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT NOT NULL,
            snapshot_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stuck_signatures (
            signature      TEXT PRIMARY KEY,
            order_id       TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            last_polled_at TEXT,
            attempts       INTEGER NOT NULL DEFAULT 0,
            resolved       INTEGER NOT NULL DEFAULT 0,
            resolution     TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_pnl (
            day         TEXT PRIMARY KEY,
            pnl_usd     REAL NOT NULL DEFAULT 0.0,
            wins        INTEGER NOT NULL DEFAULT 0,
            losses      INTEGER NOT NULL DEFAULT 0,
            loss_streak INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS cache_mint_metadata (
            mint                  TEXT PRIMARY KEY,
            decimals              INTEGER NOT NULL,
            symbol                TEXT,
            has_freeze_authority  INTEGER NOT NULL DEFAULT 0,
            has_mint_authority    INTEGER NOT NULL DEFAULT 0,
            updated_at            TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cache_pool (
            pool       TEXT PRIMARY KEY,
            program    TEXT NOT NULL,
            token_a    TEXT NOT NULL,
            token_b    TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cache_ata_owner (
            ata        TEXT PRIMARY KEY,
            owner      TEXT NOT NULL,
            mint       TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
]


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Run any unapplied migrations against `conn`. Returns the resulting version."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
    )
    conn.commit()
    cur = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
    current = int(cur.fetchone()[0])
    for version, _, sql in MIGRATIONS:
        if version <= current:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, applied_at) "
            "VALUES (?, datetime('now'))",
            (version,),
        )
        conn.commit()
        current = version
    return current


__all__ = ["MIGRATIONS", "apply_migrations"]
