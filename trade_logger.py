"""
trade_logger.py — SQLite writer (San's Architecture Notes §10, ADR-3).

data/trades.json stays as the live dashboard cache (unchanged). SQLite is the
analytical / backtest-comparable system of record. This module writes to SQLite;
callers write to trades.json separately (unchanged existing path in trading_engine).
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Optional

DEFAULT_DB_PATH = "data/trades.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          TEXT,
    timestamp         TEXT NOT NULL,
    pair              TEXT NOT NULL,
    direction         TEXT NOT NULL,
    stake             REAL NOT NULL,
    entry_price       REAL,
    expiry_price      REAL,
    result            TEXT,
    pnl               REAL,
    ema20_m15         REAL,
    ema50_m15         REAL,
    ema20_m5          REAL,
    rsi_m5            REAL,
    atr_m5            REAL,
    pattern_type      TEXT,
    trend_status      TEXT,
    latency_ms        INTEGER,
    balance_before    REAL,
    balance_after     REAL,
    source            TEXT NOT NULL,
    martingale_step   INTEGER,
    state_trace       TEXT
);

CREATE TABLE IF NOT EXISTS system_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_type         TEXT NOT NULL,
    balance               REAL NOT NULL,
    captured_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    param_hash    TEXT NOT NULL,
    sample_type   TEXT NOT NULL,      -- in_sample | out_of_sample
    timestamp     TEXT NOT NULL,
    result_summary TEXT
);
"""

TRADE_COLUMNS = [
    "order_id", "timestamp", "pair", "direction", "stake", "entry_price", "expiry_price",
    "result", "pnl", "ema20_m15", "ema50_m15", "ema20_m5", "rsi_m5", "atr_m5",
    "pattern_type", "trend_status", "latency_ms", "balance_before", "balance_after",
    "source", "martingale_step", "state_trace",
]


class TradeLogger:

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def write_trade(self, trade: dict) -> int:
        """trade: dict with keys matching (a subset of) TRADE_COLUMNS.
        Missing keys are stored as NULL. state_trace (list) is JSON-encoded."""
        row = {}
        for col in TRADE_COLUMNS:
            v = trade.get(col)
            if col == "state_trace" and isinstance(v, (list, dict)):
                v = json.dumps(v)
            row[col] = v
        placeholders = ", ".join(["?"] * len(TRADE_COLUMNS))
        cols = ", ".join(TRADE_COLUMNS)
        with self._conn() as conn:
            cur = conn.execute(f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
                                [row[c] for c in TRADE_COLUMNS])
            return cur.lastrowid

    def write_system_event(self, timestamp: str, event_type: str, detail: Optional[dict] = None) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO system_events (timestamp, event_type, detail) VALUES (?, ?, ?)",
                (timestamp, event_type, json.dumps(detail or {})),
            )
            return cur.lastrowid

    def write_equity_snapshot(self, snapshot_type: str, balance: float, captured_at: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO equity_snapshots (snapshot_type, balance, captured_at) VALUES (?, ?, ?)",
                (snapshot_type, balance, captured_at),
            )
            return cur.lastrowid

    def write_backtest_run(self, run_id: str, param_hash: str, sample_type: str,
                            timestamp: str, result_summary: dict) -> int:
        """Append-only log of every parameter set run against out-of-sample data —
        lets Iris/Vector mechanically detect look-ahead tuning (spec §10 overfitting
        guard, San's Architecture Notes §11)."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO backtest_runs (run_id, param_hash, sample_type, timestamp, result_summary) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, param_hash, sample_type, timestamp, json.dumps(result_summary)),
            )
            return cur.lastrowid

    def has_run_out_of_sample(self, param_hash: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM backtest_runs WHERE param_hash = ? AND sample_type = 'out_of_sample'",
                (param_hash,),
            )
            return cur.fetchone()[0] > 0

    def get_trades(self, limit: int = 1000) -> list[dict]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]
