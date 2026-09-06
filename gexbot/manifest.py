"""Pipeline manifest — tracks per (dataset, date) state so runs are resumable.

States: downloaded -> converted (raw deleted). A crashed run re-does at most
one day. The same DuckDB file later becomes the live engine's state store.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_manifest (
    dataset     TEXT NOT NULL,          -- 'opra_trades' | 'opra_quotes' | 'index_values'
    day         DATE NOT NULL,
    state       TEXT NOT NULL,          -- 'converted' | 'empty' | 'failed'
    rows_kept   BIGINT,
    raw_bytes   BIGINT,
    parquet_bytes BIGINT,
    finished_at TIMESTAMP DEFAULT current_timestamp,
    error       TEXT,
    PRIMARY KEY (dataset, day)
);
"""


class Manifest:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(db_path))
        self.con.execute(_SCHEMA)

    def done(self, dataset: str, day: dt.date) -> bool:
        row = self.con.execute(
            "SELECT state FROM pipeline_manifest WHERE dataset=? AND day=?",
            [dataset, day],
        ).fetchone()
        return row is not None and row[0] in ("converted", "empty")

    def mark(
        self,
        dataset: str,
        day: dt.date,
        state: str,
        rows_kept: int = 0,
        raw_bytes: int = 0,
        parquet_bytes: int = 0,
        error: str | None = None,
    ) -> None:
        self.con.execute(
            """INSERT OR REPLACE INTO pipeline_manifest
               (dataset, day, state, rows_kept, raw_bytes, parquet_bytes, error)
               VALUES (?,?,?,?,?,?,?)""",
            [dataset, day, state, rows_kept, raw_bytes, parquet_bytes, error],
        )

    def summary(self) -> list[tuple]:
        return self.con.execute(
            """SELECT dataset, state, count(*) AS days,
                      sum(rows_kept) AS rows, sum(parquet_bytes)/1e9 AS gb
               FROM pipeline_manifest GROUP BY dataset, state ORDER BY dataset"""
        ).fetchall()
