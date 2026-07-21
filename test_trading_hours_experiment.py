"""
test_trading_hours_experiment.py — 2026-07-21 24h PRACTICE-only trading-hours experiment
(Psycho-approved, Peet-approved). Zero orders in ~36h since 20-Jul was diagnosed (see
outputs/02_emma-brief-jul21.md) as a strict AND-gate entry funnel simply not aligning, not a
code bug. Psycho recommended a phased, instrumented 24h experiment in PRACTICE mode only —
this ticket implements exactly that: a flaggable time-window bypass + per-session trade
tagging, nothing else (pattern/pullback/RSI/vol thresholds are untouched, spec-locked).

Covers:
  - TradingConfig.trading_hours_experiment defaults to False (real-money-safe by default)
  - config.json ships the flag, defaulted False
  - main.py: RUNTIME_FIELDS includes the flag (dashboard/config.json settable)
  - main.py: _enforce_trading_hours_experiment_practice_gate() forces the flag back to False
    off a non-PRACTICE account_type, mirrors the existing spec_v1 gate exactly
  - main.py: the gate is wired at all 3 call sites (apply_runtime_config, switch_account,
    both spec_v1 schedulers) — static/textual proof, same pattern test_risk_v2_live_sync.py
    established for defense-in-depth gates
  - main.py: _spec_v1_place_order() tags every ENTER's meta with a "session" key
  - trade_logger.py: SQLite schema has a `session` column, write_trade()/get_trades()
    round-trip it correctly, and a migration adds the column to a pre-existing (pre-ticket)
    DB file without touching existing rows

Run: python test_trading_hours_experiment.py
No network access required.
"""

import os
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.chdir(os.path.dirname(os.path.abspath(__file__)))

BANGKOK = ZoneInfo("Asia/Bangkok")
PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


# ─────────────────────────────────────────
# 1. TradingConfig default + config.json
# ─────────────────────────────────────────
def test_trading_config_default_is_false():
    from trading_engine import TradingConfig
    cfg = TradingConfig()
    check("TradingConfig(): trading_hours_experiment defaults to False",
          cfg.trading_hours_experiment is False, cfg.trading_hours_experiment)


def test_config_json_ships_the_flag_defaulted_false():
    import json
    with open("data/config.json") as f:
        rt = json.load(f)
    check("data/config.json: trading_hours_experiment key present", "trading_hours_experiment" in rt, rt)
    check("data/config.json: trading_hours_experiment defaults to false", rt.get("trading_hours_experiment") is False, rt)


# ─────────────────────────────────────────
# 2. main.py wiring — static/textual proof (mirrors test_risk_v2_live_sync.py's pattern)
# ─────────────────────────────────────────
import main  # noqa: E402  (import after os.chdir so main.py's relative data/ paths resolve)
from trading_engine import TradingConfig  # noqa: E402


def test_runtime_fields_includes_the_flag():
    check("main.py: RUNTIME_FIELDS includes trading_hours_experiment (dashboard/config.json settable)",
          "trading_hours_experiment" in main.RUNTIME_FIELDS, main.RUNTIME_FIELDS)


def test_gate_function_exists_and_wired_at_all_call_sites():
    with open("main.py", encoding="utf-8") as f:
        src = f.read()
    calls = src.count("_enforce_trading_hours_experiment_practice_gate(")
    # 1 def + >=4 call sites (apply_runtime_config, switch_account, spec_v1_m5_loop top,
    # spec_v1_m15_loop top) = >=5 occurrences of the name in source.
    check("main.py: _enforce_trading_hours_experiment_practice_gate defined",
          "def _enforce_trading_hours_experiment_practice_gate(" in src)
    check("main.py: gate called inside apply_runtime_config()",
          "_enforce_trading_hours_experiment_practice_gate(cfg, tg, trade_logger)" in src)
    check("main.py: gate called inside switch_account command handler",
          src.count('_enforce_trading_hours_experiment_practice_gate(self.cfg, self.tg, self._trade_logger)') >= 3,
          f"found {src.count('_enforce_trading_hours_experiment_practice_gate(self.cfg, self.tg, self._trade_logger)')} self.cfg call sites")
    check("main.py: _enforce_trading_hours_experiment_practice_gate referenced >=5 times total "
          "(1 def + >=4 call sites)", calls >= 5, f"found {calls}")


def test_time_filter_v1_seeded_from_cfg_and_resynced():
    with open("main.py", encoding="utf-8") as f:
        src = f.read()
    check("main.py: TimeFilter(trading_hours_experiment=cfg.trading_hours_experiment) at construction",
          "TimeFilter(trading_hours_experiment=cfg.trading_hours_experiment)" in src)
    check("main.py: _sync_time_filter_v1_config() defined",
          "def _sync_time_filter_v1_config(self):" in src)
    check("main.py: _sync_time_filter_v1_config() called after every apply_runtime_config() edit "
          "and on every scheduler tick",
          src.count("self._sync_time_filter_v1_config()") >= 3,
          f"found {src.count('self._sync_time_filter_v1_config()')}")


def test_gate_forces_flag_off_on_non_practice_account():
    """Functional proof, not just static grep: the gate actually mutates cfg."""
    cfg = TradingConfig(trading_hours_experiment=True, account_type="REAL")
    tripped = main._enforce_trading_hours_experiment_practice_gate(cfg, tg=None, trade_logger=None)
    check("gate: trips (returns True) when trading_hours_experiment=True + account_type=REAL", tripped is True)
    check("gate: forces trading_hours_experiment back to False on REAL account",
          cfg.trading_hours_experiment is False, cfg.trading_hours_experiment)


def test_gate_is_a_no_op_on_practice_account():
    cfg = TradingConfig(trading_hours_experiment=True, account_type="PRACTICE")
    tripped = main._enforce_trading_hours_experiment_practice_gate(cfg, tg=None, trade_logger=None)
    check("gate: does NOT trip on PRACTICE account", tripped is False)
    check("gate: trading_hours_experiment stays True on PRACTICE account",
          cfg.trading_hours_experiment is True, cfg.trading_hours_experiment)


def test_gate_is_a_no_op_when_flag_already_false():
    cfg = TradingConfig(trading_hours_experiment=False, account_type="REAL")
    tripped = main._enforce_trading_hours_experiment_practice_gate(cfg, tg=None, trade_logger=None)
    check("gate: no-op when the flag is already False (nothing to force)", tripped is False)


# ─────────────────────────────────────────
# 3. main.py — every ENTER's meta is tagged with a session
# ─────────────────────────────────────────
def test_spec_v1_place_order_tags_session_in_meta():
    with open("main.py", encoding="utf-8") as f:
        src = f.read()
    check('main.py: _spec_v1_place_order() meta includes "session": self._time_filter_v1.session_tag()',
          '"session": self._time_filter_v1.session_tag()' in src)
    check('main.py: _spec_v1_log_trade_to_sqlite() forwards t.get("session") to the session column',
          '"session": t.get("session")' in src)


# ─────────────────────────────────────────
# 4. trade_logger.py — schema, round-trip, migration
# ─────────────────────────────────────────
_TEST_DB = "data/_test_trade_logger_session.db"


def _cleanup():
    if os.path.exists(_TEST_DB):
        os.remove(_TEST_DB)


def test_trade_logger_schema_has_session_column():
    from trade_logger import TradeLogger
    _cleanup()
    tl = TradeLogger(db_path=_TEST_DB)
    conn = sqlite3.connect(_TEST_DB)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    conn.close()
    check("trade_logger: trades table has a session column", "session" in cols, cols)
    _cleanup()


def test_write_trade_round_trips_session():
    from trade_logger import TradeLogger
    _cleanup()
    tl = TradeLogger(db_path=_TEST_DB)
    tl.write_trade({
        "order_id": "abc123", "timestamp": datetime.now(BANGKOK).isoformat(), "pair": "EURUSD-op",
        "direction": "CALL", "stake": 50.0, "result": "WIN", "pnl": 42.5,
        "source": "spec_v1", "session": "experiment_extended_hours",
    })
    rows = tl.get_trades(limit=5)
    check("trade_logger: write_trade + get_trades round-trips the session tag",
          len(rows) == 1 and rows[0]["session"] == "experiment_extended_hours", rows)
    _cleanup()


def test_migration_adds_session_column_to_preexisting_db_without_losing_data():
    """Simulates a live VM's data/trades.db from BEFORE this ticket (no session column) —
    the migration must add the column additively and preserve every existing row."""
    _cleanup()
    conn = sqlite3.connect(_TEST_DB)
    # Full pre-ticket schema (every TRADE_COLUMNS entry EXCEPT the new `session` column) —
    # realistic stand-in for a live VM's data/trades.db from before this ticket shipped.
    conn.executescript("""
        CREATE TABLE trades (
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
    """)
    conn.execute(
        "INSERT INTO trades (order_id, timestamp, pair, direction, stake, result, pnl, source) "
        "VALUES ('legacy-1', '2026-07-01T00:00:00', 'EURUSD-op', 'CALL', 50.0, 'WIN', 42.5, 'spec_v1')"
    )
    conn.commit()
    conn.close()

    from trade_logger import TradeLogger
    tl = TradeLogger(db_path=_TEST_DB)  # __init__ runs the migration

    conn = sqlite3.connect(_TEST_DB)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    row = conn.execute("SELECT order_id, pnl FROM trades WHERE order_id='legacy-1'").fetchone()
    conn.close()
    check("migration: session column added to a pre-existing (no-session) trades table",
          "session" in cols, cols)
    check("migration: pre-existing row survives untouched (order_id/pnl intact)",
          row is not None and row[0] == "legacy-1" and row[1] == 42.5, row)

    # New writes after migration must also work correctly (column usable, not just present)
    tl.write_trade({
        "order_id": "post-migration-1", "timestamp": datetime.now(BANGKOK).isoformat(),
        "pair": "GBPUSD-op", "direction": "PUT", "stake": 50.0, "result": "LOSS", "pnl": -50.0,
        "source": "spec_v1", "session": "london_ny_window",
    })
    rows = tl.get_trades(limit=5)
    tagged = [r for r in rows if r["order_id"] == "post-migration-1"]
    check("migration: a write AFTER migration correctly stores the session tag",
          len(tagged) == 1 and tagged[0]["session"] == "london_ny_window", tagged)
    _cleanup()


def test_migration_is_idempotent_across_repeated_construction():
    """Constructing TradeLogger twice against the same db_path (e.g. process restart) must
    not raise (sqlite3.OperationalError: duplicate column name) on the second construction."""
    _cleanup()
    from trade_logger import TradeLogger
    TradeLogger(db_path=_TEST_DB)
    try:
        TradeLogger(db_path=_TEST_DB)  # second construction — migration must be a no-op, not crash
        ok = True
    except sqlite3.OperationalError as e:
        ok = False
        print(f"    unexpected OperationalError: {e}")
    check("migration: re-constructing TradeLogger against the same db_path does not raise "
          "(idempotent ALTER TABLE)", ok)
    _cleanup()


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{'='*50}\nTOTAL: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        raise SystemExit(1)
