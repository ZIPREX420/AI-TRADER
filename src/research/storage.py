"""Parquet-based append-only storage + SQLite index."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    import pyarrow.dataset as pads
    _HAVE_PYARROW = True
except ImportError:
    _HAVE_PYARROW = False


ROOT = Path("data/research")

SCHEMAS = {
    "swaps": ["ts:double", "slot:int64", "sig:string", "mint:string", "wallet:string",
              "side:int8", "sol_amount:double", "token_amount:double", "price_sol:double",
              "dex:string"],
    "pools": ["ts:double", "slot:int64", "sig:string", "mint:string", "event_kind:string",
              "source_program:string", "sol_in_pool:double", "lp_holder:string",
              "sol_amount:double", "signer:string"],
    "prices": ["ts:double", "mint:string", "sol_per_token:double", "source:string",
               "bar_seconds:int32"],
    "transfers": ["ts:double", "sig:string", "src:string", "dst:string", "sol_amount:double"],
    "wallets": ["pubkey:string", "first_tx_ts:double", "label:string",
                "cluster_id:string", "last_seen_ts:double"],
}


def _arrow_type(t: str):
    return {
        "double": pa.float64(), "int64": pa.int64(), "int32": pa.int32(),
        "int8": pa.int8(), "string": pa.string(),
    }[t]


def _schema(table: str) -> "pa.Schema":
    if not _HAVE_PYARROW:
        raise RuntimeError("pyarrow not installed; pip install pyarrow")
    fields = []
    for spec in SCHEMAS[table]:
        name, t = spec.split(":")
        fields.append(pa.field(name, _arrow_type(t)))
    return pa.schema(fields)


def _index_db(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(root / "index.db"))
    db.executescript("""
    CREATE TABLE IF NOT EXISTS collected(
        table_name TEXT, dt TEXT, source TEXT, rows INT, status TEXT,
        sha256 TEXT, created_ts REAL,
        PRIMARY KEY(table_name, dt, source)
    );
    CREATE TABLE IF NOT EXISTS cursors(
        source TEXT PRIMARY KEY, last_signature TEXT, last_slot INT, last_ts REAL
    );
    """)
    db.commit()
    return db


def _date_str(ts: float) -> str:
    return dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


@dataclass
class WriteResult:
    table: str
    dt: str
    rows: int
    path: str
    sha256: str


def append_rows(table: str, rows: list[dict], source: str, root: Path = ROOT) -> list[WriteResult]:
    """Append rows to per-day partitions. Returns one WriteResult per partition written."""
    if not rows:
        return []
    if not _HAVE_PYARROW:
        raise RuntimeError("pyarrow not installed")
    schema = _schema(table)
    by_dt: dict[str, list[dict]] = {}
    for r in rows:
        by_dt.setdefault(_date_str(r["ts"]), []).append(r)

    out: list[WriteResult] = []
    db = _index_db(root)
    try:
        for dt_, batch in by_dt.items():
            partition_dir = root / table / f"dt={dt_}"
            partition_dir.mkdir(parents=True, exist_ok=True)
            n_existing = sum(1 for _ in partition_dir.glob("part-*.parquet"))
            path = partition_dir / f"part-{n_existing:05d}.parquet"
            tbl = pa.Table.from_pylist(batch, schema=schema)
            pq.write_table(tbl, path, compression="zstd")
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            db.execute(
                "INSERT OR REPLACE INTO collected VALUES(?,?,?,?,?,?,?)",
                (table, dt_, source, len(batch), "ok", sha, _now()),
            )
            out.append(WriteResult(table, dt_, len(batch), str(path), sha))
        db.commit()
    finally:
        db.close()
    return out


def read_partition(table: str, start_ts: float, end_ts: float, root: Path = ROOT):
    """Lazy scan via pyarrow.dataset, filtered to [start_ts, end_ts]."""
    if not _HAVE_PYARROW:
        raise RuntimeError("pyarrow not installed")
    base = root / table
    if not base.exists():
        return None
    ds = pads.dataset(str(base), format="parquet", partitioning="hive")
    expr = (pads.field("ts") >= start_ts) & (pads.field("ts") <= end_ts)
    return ds.to_table(filter=expr).sort_by([("ts", "ascending"), ("slot", "ascending")])


def read_partition_iter(table: str, start_ts: float, end_ts: float, root: Path = ROOT, batch_size: int = 50_000):
    """Iterator over rows (dict) sorted by ts."""
    tbl = read_partition(table, start_ts, end_ts, root)
    if tbl is None or tbl.num_rows == 0:
        return iter(())
    return iter(tbl.to_pylist())


def get_cursor(source: str, root: Path = ROOT) -> dict:
    db = _index_db(root)
    try:
        cur = db.execute(
            "SELECT last_signature, last_slot, last_ts FROM cursors WHERE source=?",
            (source,),
        ).fetchone()
        if cur is None:
            return {"last_signature": None, "last_slot": 0, "last_ts": 0.0}
        return {"last_signature": cur[0], "last_slot": cur[1], "last_ts": cur[2]}
    finally:
        db.close()


def set_cursor(source: str, last_signature: str | None, last_slot: int, last_ts: float, root: Path = ROOT) -> None:
    db = _index_db(root)
    try:
        db.execute(
            "INSERT OR REPLACE INTO cursors VALUES(?,?,?,?)",
            (source, last_signature, last_slot, last_ts),
        )
        db.commit()
    finally:
        db.close()


def _now() -> float:
    import time as _t
    return _t.time()
