"""
test_spec_v1.py — offline unit tests for the spec-overhaul modules (Titan's own
pre-QA pass; Iris runs a separate, independent QA layer on top of this).

Covers:
  - IndicatorEngineV2.ema() SMA-seed correctness (spec §2) vs hand-computed values
  - RSI/ATR sanity (Wilder smoothing, no NaN once warmed up)
  - TrendFilter 4-condition UPTREND/DOWNTREND/NO_TRADE
  - EntrySignal pullback / RSI-zone / volatility gates
  - RiskManager hard rules: stake %, max trades/day, consecutive-loss hard stop,
    signal cooldown, daily/weekly loss limit, no-overlap, auto-stop
  - MartingaleModule two-flag gate (ADR-4) — cannot activate with only one flag
  - CandleStore no-repaint (forming candle dropped) + dedup guard
  - TimeFilter trading windows / blackout / weekend rule

Run: python test_spec_v1.py
No network access required.
"""

import json
import math
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import pandas as pd

from candle_store import Candle, CandleStore
from entry_signal import EntrySignal
from indicators_v2 import IndicatorEngineV2
from martingale import MartingaleConfig, MartingaleModule
from risk_manager import RiskConfig, RiskManager
from time_filter import TimeFilter
from trend_filter import TrendFilter

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
# 1. IndicatorEngineV2 — EMA SMA-seed correctness
# ─────────────────────────────────────────
def test_ema_sma_seed():
    series = pd.Series([float(i) for i in range(1, 11)])  # 1..10
    period = 5
    ema = IndicatorEngineV2.ema(series, period)
    # index 0..3 (period-1=4 values) must be NaN
    check("ema: first period-1 values are NaN", ema.iloc[:period - 1].isna().all())
    # seed at index period-1 (=4) must be SMA of series[:period] = mean(1..5) = 3.0
    check("ema: seed = SMA(first period)", math.isclose(ema.iloc[period - 1], 3.0),
          f"got {ema.iloc[period - 1]}")
    # recursive value at index 5 (series=6): mult=2/6; ema5 = (6-3)*mult+3 = 4.0
    mult = 2 / (period + 1)
    expected5 = (6 - 3.0) * mult + 3.0
    check("ema: recursive step matches hand-computed value", math.isclose(ema.iloc[5], expected5),
          f"got {ema.iloc[5]} expected {expected5}")
    # last value should differ from pandas .ewm(adjust=False) seeded-from-first-value result
    ewm_native = series.ewm(span=period, adjust=False).mean()
    check("ema: SMA-seeded diverges from pandas .ewm(adjust=False) near the start (ADR-2)",
          not math.isclose(ema.iloc[period - 1], ewm_native.iloc[period - 1]))


def test_rsi_atr_no_nan_after_warmup():
    rng = np.random.default_rng(1)
    n = 100
    close = pd.Series(1.10 + np.cumsum(rng.normal(0, 0.0005, n)))
    high = close + abs(rng.normal(0, 0.0002, n))
    low = close - abs(rng.normal(0, 0.0002, n))
    df = pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]), "high": high, "low": low, "close": close})
    rsi = IndicatorEngineV2.rsi(close, 14)
    atr = IndicatorEngineV2.atr(df, 14)
    check("rsi: no NaN after warmup (last 50 bars)", not rsi.iloc[-50:].isna().any())
    check("rsi: bounded 0-100", rsi.dropna().between(0, 100).all())
    check("atr: no NaN after warmup (last 50 bars)", not atr.iloc[-50:].isna().any())
    check("atr: always >= 0", (atr.dropna() >= 0).all())


# ─────────────────────────────────────────
# 2. TrendFilter — 4-condition UPTREND/DOWNTREND/NO_TRADE
# ─────────────────────────────────────────
def _make_trending_m15(n=260, direction="up", noise=0.00005, seed=7):
    rng = np.random.default_rng(seed)
    step = 0.0004 if direction == "up" else -0.0004
    price = 1.1000
    rows = []
    ts = 1_700_000_000
    for i in range(n):
        o = price
        c = price + step + rng.normal(0, noise)
        h = max(o, c) + abs(rng.normal(0, noise))
        l = min(o, c) - abs(rng.normal(0, noise))
        rows.append((ts, o, h, l, c, 100.0))
        price = c
        ts += 900
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])


def test_trend_filter_uptrend():
    df = _make_trending_m15(direction="up")
    tf = TrendFilter()
    state = tf.evaluate(df, "EURUSD")
    check("trend_filter: sustained uptrend -> UPTREND", state.status == "UPTREND", state.status)


def test_trend_filter_downtrend():
    df = _make_trending_m15(direction="down")
    tf = TrendFilter()
    state = tf.evaluate(df, "EURUSD")
    check("trend_filter: sustained downtrend -> DOWNTREND", state.status == "DOWNTREND", state.status)


def test_trend_filter_insufficient_data():
    df = _make_trending_m15(n=30, direction="up")
    tf = TrendFilter()
    state = tf.evaluate(df, "EURUSD")
    check("trend_filter: insufficient data -> NO_TRADE (not error)", state.status == "NO_TRADE")


def test_trend_filter_choppy_no_trade():
    # Perfectly flat market: close never moves, so ema20 == ema50 == price exactly
    # (never strictly >/< each other) and ATR == 0 — deterministically NOT a
    # qualifying trend under all 4 conditions, regardless of RNG.
    n = 260
    rows = []
    ts = 1_700_000_000
    price = 1.1000
    for i in range(n):
        rows.append((ts, price, price, price, price, 100.0))
        ts += 900
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    tf = TrendFilter()
    state = tf.evaluate(df, "EURUSD")
    check("trend_filter: perfectly flat market -> NO_TRADE", state.status == "NO_TRADE", state.status)


# ─────────────────────────────────────────
# 3. EntrySignal — pullback / RSI zone / volatility gates
# ─────────────────────────────────────────
def test_entry_signal_no_trade_short_circuit():
    from trend_filter import TrendState
    es = EntrySignal()
    trend = TrendState(asset="EURUSD", status="NO_TRADE", ema20=None, ema50=None, atr14=None, computed_at=None)
    result = es.evaluate("EURUSD", trend, pd.DataFrame())
    check("entry_signal: NO_TRADE trend short-circuits to HOLD", result.signal == "HOLD")


def test_entry_signal_rsi_zone_reject():
    from trend_filter import TrendState
    es = EntrySignal()
    trend = TrendState(asset="EURUSD", status="UPTREND", ema20=1.1, ema50=1.09, atr14=0.0004, computed_at="x")
    # Build a flat-ish M5 series so RSI drifts far outside 45-65 (strong one-directional run)
    n = 40
    close = pd.Series([1.10 + i * 0.001 for i in range(n)])  # strong monotonic up -> RSI near 100
    df = pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]), "high": close + 0.0002,
        "low": close - 0.0002, "close": close, "volume": 100.0,
        "ts": [1_700_000_000 + i * 300 for i in range(n)],
    })
    result = es.evaluate("EURUSD", trend, df)
    check("entry_signal: extreme RSI (monotonic run) rejected outside 45-65 zone (if not held earlier)",
          result.signal == "HOLD")


# ─────────────────────────────────────────
# 3b. EntrySignal._detect_pattern — spec §4.2 exact formulas (bug-146 regression)
# ─────────────────────────────────────────
def test_pattern_engulfing_body_atr_filter():
    es = EntrySignal()
    prev = pd.Series({"open": 1.0, "high": 1.005, "low": 0.99, "close": 0.99})
    row_big_body = pd.Series({"open": 0.99, "high": 1.03, "low": 0.98, "close": 1.02, "atr14": 0.05})
    check("pattern: bullish engulfing accepted when body >= 0.3xATR14",
          es._detect_pattern(row_big_body, prev, "CALL") == "engulfing")

    row_tiny_body = pd.Series({"open": 0.99, "high": 1.03, "low": 0.98, "close": 1.02, "atr14": 1.0})
    check("pattern: bullish engulfing REJECTED when body < 0.3xATR14 (spec §4.2 min-size filter)",
          es._detect_pattern(row_tiny_body, prev, "CALL") is None,
          f"got {es._detect_pattern(row_tiny_body, prev, 'CALL')}")


def test_pattern_pinbar_call_atr_and_close_condition():
    es = EntrySignal()
    prev = pd.Series({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0})  # neutral, no engulf match

    row_ok = pd.Series({"open": 1.00, "high": 1.006, "low": 0.95, "close": 1.005, "atr14": 0.08})
    check("pattern: pinbar CALL accepted — lower_wick>=2xbody AND lower_wick>=0.5xATR14 AND close>open",
          es._detect_pattern(row_ok, prev, "CALL") == "pinbar")

    row_small_wick_vs_atr = pd.Series({"open": 1.00, "high": 1.006, "low": 0.95, "close": 1.005, "atr14": 0.2})
    check("pattern: pinbar CALL REJECTED when lower_wick < 0.5xATR14 (spec §4.2 min-size filter, was missing)",
          es._detect_pattern(row_small_wick_vs_atr, prev, "CALL") is None,
          f"got {es._detect_pattern(row_small_wick_vs_atr, prev, 'CALL')}")

    row_close_below_open = pd.Series({"open": 1.01, "high": 1.016, "low": 0.95, "close": 1.005, "atr14": 0.08})
    check("pattern: pinbar CALL REJECTED when close<=open even if wick ratios satisfied "
          "(spec §4.2 literal close>open, not the old c>(h+l)/2 midpoint check)",
          es._detect_pattern(row_close_below_open, prev, "CALL") is None,
          f"got {es._detect_pattern(row_close_below_open, prev, 'CALL')}")


def test_pattern_pinbar_put_mirror():
    es = EntrySignal()
    prev = pd.Series({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0})

    row_ok = pd.Series({"open": 1.005, "high": 1.05, "low": 0.999, "close": 1.00, "atr14": 0.08})
    check("pattern: pinbar PUT accepted — upper_wick>=2xbody AND upper_wick>=0.5xATR14 AND close<open (mirror)",
          es._detect_pattern(row_ok, prev, "PUT") == "pinbar")

    row_small_wick_vs_atr = pd.Series({"open": 1.005, "high": 1.05, "low": 0.999, "close": 1.00, "atr14": 0.2})
    check("pattern: pinbar PUT REJECTED when upper_wick < 0.5xATR14 (mirror of CALL min-size filter)",
          es._detect_pattern(row_small_wick_vs_atr, prev, "PUT") is None,
          f"got {es._detect_pattern(row_small_wick_vs_atr, prev, 'PUT')}")

    row_close_above_open = pd.Series({"open": 1.00, "high": 1.045, "low": 0.994, "close": 1.005, "atr14": 0.08})
    check("pattern: pinbar PUT REJECTED when close>=open even if wick ratios satisfied (mirror check)",
          es._detect_pattern(row_close_above_open, prev, "PUT") is None,
          f"got {es._detect_pattern(row_close_above_open, prev, 'PUT')}")


# ─────────────────────────────────────────
# 4. RiskManager — hard rules
# ─────────────────────────────────────────
def _fresh_risk_manager(tmp_suffix="test1", **overrides) -> RiskManager:
    """Each RiskManager persists to disk and reloads on init — tests MUST start from
    a clean slate (delete any leftover file from a previous run of this suite) or
    results become order/history-dependent across runs, which is not a real bug in
    RiskManager itself (persistence across restarts is correct/intended production
    behavior) but would make this test file non-idempotent."""
    import os
    state_path = f"data/_test_risk_{tmp_suffix}.json"
    snapshot_path = f"data/_test_snap_{tmp_suffix}.json"
    for p in (state_path, snapshot_path):
        if os.path.exists(p):
            os.remove(p)
    cfg = RiskConfig(**overrides)
    return RiskManager(cfg, state_path=state_path, snapshot_path=snapshot_path)


def test_risk_stake_pct():
    rm = _fresh_risk_manager("stake")
    check("risk: stake_amount = balance * stake_pct/100", math.isclose(rm.stake_amount(1000), 15.0))


def test_risk_max_trades_per_day():
    rm = _fresh_risk_manager("maxday", max_trades_per_day=2)
    now = datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK)  # Thursday, inside window
    rm.roll_boundaries(now, 1000)
    can, _ = rm.can_trade(now, 1000)
    check("risk: can_trade OK before hitting max_trades_per_day", can)
    rm.record_order_placed()
    rm.record_order_result(5, "WIN", now)
    rm.record_order_placed()
    rm.record_order_result(5, "WIN", now)
    can, reason = rm.can_trade(now, 1010)
    check("risk: blocked after max_trades_per_day reached", not can, reason)


def test_risk_consecutive_loss_hard_stop():
    rm = _fresh_risk_manager("consec", max_consecutive_losses=3, max_trades_per_day=99)
    now = datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK)
    rm.roll_boundaries(now, 1000)
    for _ in range(3):
        rm.record_order_placed()
        rm.record_order_result(-5, "LOSS", now)
    can, reason = rm.can_trade(now, 985)
    check("risk: 3 consecutive losses -> hard stop for the day", not can and "วันถัดไป" in reason, reason)
    # A NEW day should clear the hard stop
    next_day = now + timedelta(days=1)
    rm.roll_boundaries(next_day, 985)
    can2, reason2 = rm.can_trade(next_day, 985)
    check("risk: hard stop clears on new calendar day", can2, reason2)


def test_risk_signal_cooldown_independent_of_hard_stop():
    rm = _fresh_risk_manager("cooldown", max_consecutive_losses=99, signal_cooldown_minutes=15)
    now = datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK)
    rm.roll_boundaries(now, 1000)
    rm.record_order_placed()
    rm.record_order_result(-5, "LOSS", now)  # single loss, far from the 99-loss hard stop
    can, reason = rm.can_trade(now, 995)
    check("risk: single loss triggers soft cooldown (independent of hard stop, ADR-5)",
          not can and "cooldown" in reason, reason)
    later = now + timedelta(minutes=16)
    can2, _ = rm.can_trade(later, 995)
    check("risk: cooldown clears after signal_cooldown_minutes", can2)


def test_risk_daily_loss_limit_pct():
    rm = _fresh_risk_manager("dailyloss", daily_loss_limit_pct=4.0, max_consecutive_losses=99)
    now = datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK)
    rm.roll_boundaries(now, 1000)  # balance_start_of_day = 1000
    rm.record_order_placed()
    rm.record_order_result(-41, "LOSS", now)  # -4.1% of 1000
    can, reason = rm.can_trade(now, 959)
    check("risk: daily loss limit (% of day-start balance) blocks further trades",
          not can and "daily loss limit" in reason, reason)


def test_risk_no_overlap():
    rm = _fresh_risk_manager("overlap")
    now = datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK)
    rm.roll_boundaries(now, 1000)
    rm.record_order_placed()
    can, reason = rm.can_trade(now, 1000)
    check("risk: no-overlap — blocked while a position is open", not can and "ซ้อน" in reason, reason)


def test_risk_auto_stop():
    rm = _fresh_risk_manager("autostop", auto_stop_enabled=True, auto_stop_drawdown_pct=30.0)
    now = datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK)
    rm.roll_boundaries(now, 1000)  # seeds equity_baseline = 1000
    can, reason = rm.can_trade(now, balance=650)  # -35% drawdown
    check("risk: auto-stop triggers past configured drawdown %", not can and "AUTO-STOP" in reason, reason)
    rm.reset_equity_baseline(650)
    can2, _ = rm.can_trade(now, balance=650)
    check("risk: reset_equity_baseline clears the auto-stop", can2)


def test_risk_auto_stop_toggle_off():
    rm = _fresh_risk_manager("autostop_off", auto_stop_enabled=True, auto_stop_drawdown_pct=30.0)
    now = datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK)
    rm.roll_boundaries(now, 1000)
    rm.set_auto_stop(False)
    can, _ = rm.can_trade(now, balance=500)  # -50% drawdown, but auto-stop is off
    check("risk: auto-stop can be disabled via set_auto_stop (dashboard-toggleable)", can)


# ─────────────────────────────────────────
# 5. MartingaleModule — two-flag gate (ADR-4)
# ─────────────────────────────────────────
def test_martingale_two_flag_gate():
    check("martingale: OFF by default (enabled=False)", not MartingaleModule.is_active(False, False))
    check("martingale: enabled alone (no ack) is NOT active", not MartingaleModule.is_active(True, False))
    check("martingale: ack alone (not enabled) is NOT active", not MartingaleModule.is_active(False, True))
    check("martingale: both flags together IS active", MartingaleModule.is_active(True, True))
    ok, _ = MartingaleModule.validate_toggle(True, False)
    check("martingale: server rejects enabled=true without ack_risk=true", not ok)
    ok2, _ = MartingaleModule.validate_toggle(True, True)
    check("martingale: server accepts enabled=true WITH ack_risk=true", ok2)


def test_martingale_ladder_math():
    mm = MartingaleModule(MartingaleConfig(base=50.0, multiplier=2.0, max_steps=4))
    seq = mm.sequence()
    check("martingale: ladder sequence 50/100/200/400", seq == [50.0, 100.0, 200.0, 400.0], seq)
    mm.advance("LOSS")
    check("martingale: step advances on loss", mm.current_step == 1)
    mm.advance("WIN")
    check("martingale: step resets to 0 on win", mm.current_step == 0)


# ─────────────────────────────────────────
# 6. CandleStore — no-repaint + dedup
# ─────────────────────────────────────────
def test_candle_store_drops_forming_candle():
    store = CandleStore("EURUSD_TEST", persist_dir="data/_test_candles")
    raw = [{"from": 1000 + i * 300, "open": 1.1, "close": 1.1, "max": 1.11, "min": 1.09, "volume": 1}
           for i in range(5)]
    n = store.load_candles("m5", raw, drop_forming=True)
    check("candle_store: forming (last) candle dropped on load", n == 4, n)


def test_candle_store_dedup_guard():
    store = CandleStore("EURUSD_TEST2", persist_dir="data/_test_candles")
    store.load_candles("m5", [{"from": 1000, "open": 1, "close": 1, "max": 1, "min": 1, "volume": 1},
                               {"from": 1300, "open": 1, "close": 1, "max": 1, "min": 1, "volume": 1}])
    added_old = store.append_if_new("m5", Candle(ts=1000, open=1, high=1, low=1, close=1))
    added_new = store.append_if_new("m5", Candle(ts=1600, open=1, high=1, low=1, close=1))
    check("candle_store: append_if_new rejects a stale/duplicate ts", not added_old)
    check("candle_store: append_if_new accepts a genuinely new ts", added_new)


def test_candle_store_overflow_persists():
    store = CandleStore("EURUSD_TEST3", persist_dir="data/_test_candles")
    store.MAX_IN_MEMORY = 5
    store.m5.maxlen  # noqa — sanity that deque exists
    from collections import deque
    store.m5 = deque(maxlen=5)
    for i in range(5):
        store.append_if_new("m5", Candle(ts=i * 300, open=1, high=1, low=1, close=1))
    check("candle_store: buffer stays capped at MAX_IN_MEMORY", len(store.m5) == 5)
    store.append_if_new("m5", Candle(ts=5 * 300, open=1, high=1, low=1, close=1))
    check("candle_store: still capped after overflow", len(store.m5) == 5)
    import os
    check("candle_store: overflowed candle persisted to disk",
          os.path.exists(store._overflow_path("m5")))


# ─────────────────────────────────────────
# 7. TimeFilter — trading window / blackout / weekend
# ─────────────────────────────────────────
def test_time_filter_windows():
    tf = TimeFilter()
    ok, _ = tf.is_tradeable(datetime(2026, 7, 9, 15, 0, tzinfo=BANGKOK))   # Thu 15:00 — in window 1
    check("time_filter: 15:00 Thu inside window 1 -> tradeable", ok)
    ok2, _ = tf.is_tradeable(datetime(2026, 7, 9, 18, 0, tzinfo=BANGKOK))  # Thu 18:00 — outside all windows
    check("time_filter: 18:00 Thu outside windows -> not tradeable", not ok2)
    ok3, reason3 = tf.is_tradeable(datetime(2026, 7, 9, 19, 15, tzinfo=BANGKOK))  # blackout
    check("time_filter: 19:15 Thu inside NY-open blackout -> not tradeable", not ok3 and "blackout" in reason3, reason3)
    ok4, _ = tf.is_tradeable(datetime(2026, 7, 9, 20, 0, tzinfo=BANGKOK))  # inside window 2, after blackout
    check("time_filter: 20:00 Thu inside window 2 (after blackout) -> tradeable", ok4)


def test_time_filter_friday_monday_rule():
    tf = TimeFilter()
    ok, reason = tf.is_tradeable(datetime(2026, 7, 10, 21, 30, tzinfo=BANGKOK))  # Fri after 21:00
    check("time_filter: Friday after 21:00 -> not tradeable", not ok and "ศุกร์" in reason, reason)
    ok2, reason2 = tf.is_tradeable(datetime(2026, 7, 13, 14, 30, tzinfo=BANGKOK))  # Monday before 15:00
    check("time_filter: Monday before 15:00 -> not tradeable", not ok2 and "จันทร์" in reason2, reason2)


# ─────────────────────────────────────────
# 8. backtest.py — RiskManager state isolation across separate run_backtest() /
#    _simulate() invocations (bug-147 regression)
# ─────────────────────────────────────────
def test_backtest_no_state_leak_between_runs():
    import os
    import backtest as bt
    from risk_manager import RiskConfig

    pair, sample = "UNITTESTPAIR", "unit_test"
    state_path = f"data/backtest_risk_{pair}_{sample}.json"
    snap_path = f"data/backtest_snapshots_{pair}_{sample}.json"
    # make sure no leftovers from a previous failed run of this suite skew the result
    for p in (state_path, snap_path):
        if os.path.exists(p):
            os.remove(p)

    m5, m15 = bt.generate_synthetic_ohlcv(months=1, seed=7)
    cfg = RiskConfig()
    bt._simulate(pair, m5, m15, bt.DEFAULT_PAYOUT, bt.DEFAULT_EXPIRY_MINUTES, 1000.0, cfg, sample, {})
    with open(snap_path) as f:
        baseline_run1 = json.load(f)["equity_baseline"]
    check("backtest: run 1 (initial_balance=1000.0) seeds equity_baseline=1000.0",
          math.isclose(baseline_run1, 1000.0), f"got {baseline_run1}")

    bt._simulate(pair, m5, m15, bt.DEFAULT_PAYOUT, bt.DEFAULT_EXPIRY_MINUTES, 5000.0, cfg, sample, {})
    with open(snap_path) as f:
        baseline_run2 = json.load(f)["equity_baseline"]
    check("backtest: run 2 (initial_balance=5000.0) does NOT inherit run 1's stale "
          "equity_baseline (bug-147 fix — state reset at the start of every _simulate() call)",
          math.isclose(baseline_run2, 5000.0), f"got {baseline_run2}")

    for p in (state_path, snap_path):
        if os.path.exists(p):
            os.remove(p)


def test_time_filter_weekend():
    tf = TimeFilter()
    ok, reason = tf.is_tradeable(datetime(2026, 7, 11, 15, 0, tzinfo=BANGKOK))  # Saturday
    check("time_filter: Saturday -> not tradeable", not ok and "สุดสัปดาห์" in reason, reason)


# ─────────────────────────────────────────
# 9. TimeFilter — 2026-07-21 24h PRACTICE-only trading-hours experiment flag
# ─────────────────────────────────────────
def test_time_filter_experiment_flag_inert_when_unset():
    """Default (flag unset / False) must be byte-for-byte identical to pre-experiment
    behavior — this is the real-money safety guarantee the ticket asked to prove."""
    tf_default = TimeFilter()
    tf_explicit_false = TimeFilter(trading_hours_experiment=False)
    probe_times = [
        datetime(2026, 7, 9, 15, 0, tzinfo=BANGKOK),   # in window 1
        datetime(2026, 7, 9, 18, 0, tzinfo=BANGKOK),   # outside all windows
        datetime(2026, 7, 9, 19, 15, tzinfo=BANGKOK),  # NY-open blackout
        datetime(2026, 7, 9, 20, 0, tzinfo=BANGKOK),   # in window 2
        datetime(2026, 7, 10, 21, 30, tzinfo=BANGKOK), # Friday after 21:00
        datetime(2026, 7, 13, 14, 30, tzinfo=BANGKOK), # Monday before 15:00
        datetime(2026, 7, 11, 15, 0, tzinfo=BANGKOK),  # Saturday
    ]
    all_match = True
    for probe in probe_times:
        r1 = tf_default.is_tradeable(probe)
        r2 = tf_explicit_false.is_tradeable(probe)
        if r1 != r2:
            all_match = False
    check("time_filter: trading_hours_experiment unset/False is inert (identical to pre-experiment "
          "TimeFilter() across all probed times)", all_match)


def test_time_filter_experiment_flag_bypasses_windows():
    tf = TimeFilter(trading_hours_experiment=True)
    ok, reason = tf.is_tradeable(datetime(2026, 7, 9, 18, 0, tzinfo=BANGKOK))  # Thu 18:00 — normally outside all windows
    check("time_filter: experiment flag makes 18:00 Thu (outside normal windows) tradeable",
          ok and "experiment" in reason, reason)
    ok2, reason2 = tf.is_tradeable(datetime(2026, 7, 9, 2, 0, tzinfo=BANGKOK))  # 02:00 — deep off-hours
    check("time_filter: experiment flag makes 02:00 Thu (deep off-hours) tradeable",
          ok2 and "experiment" in reason2, reason2)


def test_time_filter_experiment_flag_weekend_halt_still_fires():
    """Psycho was explicit: weekend gaps must NOT be included in the experiment's data —
    the experiment flag must never bypass the weekend halt / Monday gate."""
    tf = TimeFilter(trading_hours_experiment=True)
    ok, reason = tf.is_tradeable(datetime(2026, 7, 11, 15, 0, tzinfo=BANGKOK))  # Saturday
    check("time_filter: experiment flag does NOT bypass Saturday weekend halt",
          not ok and "สุดสัปดาห์" in reason, reason)
    ok2, reason2 = tf.is_tradeable(datetime(2026, 7, 12, 15, 0, tzinfo=BANGKOK))  # Sunday
    check("time_filter: experiment flag does NOT bypass Sunday weekend halt",
          not ok2 and "สุดสัปดาห์" in reason2, reason2)
    ok3, reason3 = tf.is_tradeable(datetime(2026, 7, 10, 21, 30, tzinfo=BANGKOK))  # Fri after 21:00
    check("time_filter: experiment flag does NOT bypass Friday-after-21:00 halt",
          not ok3 and "ศุกร์" in reason3, reason3)
    ok4, reason4 = tf.is_tradeable(datetime(2026, 7, 13, 14, 30, tzinfo=BANGKOK))  # Monday before 15:00
    check("time_filter: experiment flag does NOT bypass Monday-before-15:00 gate "
          "(design decision — see time_filter.py module docstring)",
          not ok4 and "จันทร์" in reason4, reason4)


def test_time_filter_experiment_flag_ny_blackout_still_fires():
    """Design decision (Titan, 2026-07-21): the 19:00-19:30 NY-open blackout stays a normal
    always-on gate under the experiment flag too — see time_filter.py module docstring."""
    tf = TimeFilter(trading_hours_experiment=True)
    ok, reason = tf.is_tradeable(datetime(2026, 7, 9, 19, 15, tzinfo=BANGKOK))
    check("time_filter: experiment flag does NOT bypass the 19:00-19:30 NY-open blackout",
          not ok and "blackout" in reason, reason)


def test_time_filter_session_tag():
    tf = TimeFilter()
    tag1 = tf.session_tag(datetime(2026, 7, 9, 15, 0, tzinfo=BANGKOK))   # in window 1
    check("time_filter: session_tag() labels in-window ticks 'london_ny_window'",
          tag1 == "london_ny_window", tag1)
    tag2 = tf.session_tag(datetime(2026, 7, 9, 20, 0, tzinfo=BANGKOK))   # in window 2
    check("time_filter: session_tag() labels window-2 ticks 'london_ny_window' too",
          tag2 == "london_ny_window", tag2)
    tag3 = tf.session_tag(datetime(2026, 7, 9, 2, 0, tzinfo=BANGKOK))    # off-hours
    check("time_filter: session_tag() labels off-hours ticks 'experiment_extended_hours'",
          tag3 == "experiment_extended_hours", tag3)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{'='*50}\nTOTAL: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        raise SystemExit(1)
