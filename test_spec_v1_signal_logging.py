"""
test_spec_v1_signal_logging.py — bug-160 regression suite (Titan).

Peet reported ~1 hour of complete silence from spec_v1 on the production VM while
strategy_mode=spec_v1 was active inside its allowed trading window — no [SPEC_V1]/
[TRADE]/error lines at all, making it impossible to tell "evaluating candles but never
finding a qualifying signal" (entry_signal.py's AND-gated conditions are much stricter
than legacy) apart from "on_m5_close()/on_m15_close() silently never running".

This suite proves the new unconditional [SPEC_V1_SIGNAL] INFO log
(state_machine.py's `_log_signal()`) actually fires on EVERY on_m5_close()/
on_m15_close() call, in EVERY branch, with the right per-condition breakdown:

  A. entry_signal.py — EntryResult.diag is populated correctly at each AND-gate
     (pullback / pattern / RSI / volatility), using IndicatorEngineV2.compute_m5
     monkeypatched to return fully-controlled indicator rows (deterministic, no
     reliance on random-walk data settling into the right zone by luck).
  B. state_machine.py — BotStateMachine.on_m5_close()/on_m15_close() emit
     "[SPEC_V1_SIGNAL]" via the real Python logging module on every branch:
     trend NO_TRADE, no candle store, entry_signal HOLD (with diag), and the
     full-pass ENTER path — captured with a logging.Handler, not just inspected
     via return value, so a future refactor that silently removes a log call on
     one branch would be caught here.

Run: python test_spec_v1_signal_logging.py
No network access required.
"""

import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import pandas as pd

import entry_signal as es_mod
from entry_signal import EntryResult, EntrySignal
from risk_manager import RiskConfig, RiskManager
from state_machine import BotStateMachine
from time_filter import TimeFilter
from trend_filter import TrendState

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
# Log capture helper
# ─────────────────────────────────────────
class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(self.format(record))


def _capture(logger_name: str, fn):
    """Run fn() with a capture handler attached to logger_name at INFO level;
    return the list of formatted log lines emitted during the call."""
    target = logging.getLogger(logger_name)
    handler = _CaptureHandler()
    handler.setLevel(logging.INFO)
    old_level = target.level
    target.addHandler(handler)
    target.setLevel(logging.INFO)
    try:
        fn()
    finally:
        target.removeHandler(handler)
        target.setLevel(old_level)
    return handler.records


# ─────────────────────────────────────────
# A. entry_signal.py — EntryResult.diag correctness per AND-gate
# ─────────────────────────────────────────
_orig_compute_m5 = es_mod.IndicatorEngineV2.compute_m5


def _fake_indicator_df(row_vals: dict, prev_vals: dict) -> pd.DataFrame:
    cols = ["open", "high", "low", "close", "ema20", "rsi14", "atr14"]
    data = {c: [prev_vals.get(c, 0.0), row_vals.get(c, 0.0)] for c in cols}
    return pd.DataFrame(data)


def _with_fixed_indicators(row_vals: dict, prev_vals: dict):
    es_mod.IndicatorEngineV2.compute_m5 = staticmethod(lambda df: _fake_indicator_df(row_vals, prev_vals))


def _restore_indicators():
    es_mod.IndicatorEngineV2.compute_m5 = _orig_compute_m5


_DUMMY_M5 = pd.DataFrame({"x": [0] * 22})  # only needs len >= 22 (min_needed gate)
_UPTREND = TrendState(asset="EURUSD", status="UPTREND", ema20=1.1, ema50=1.09, atr14=0.001, computed_at="x")


def test_diag_no_trade_short_circuit():
    es = EntrySignal()
    trend = TrendState(asset="EURUSD", status="NO_TRADE", ema20=None, ema50=None, atr14=None, computed_at=None)
    result = es.evaluate("EURUSD", trend, pd.DataFrame())
    check("diag: NO_TRADE trend -> all diag fields stay 'n/a' (never reached)",
          result.diag == {"pullback": "n/a", "pattern": "n/a", "rsi": "n/a", "vol": "n/a"}, result.diag)


def test_diag_pullback_fail():
    es = EntrySignal()
    row = {"open": 1.1005, "high": 1.1010, "low": 1.1004, "close": 1.1006,
           "ema20": 1.1000, "rsi14": 55.0, "atr14": 0.001}
    prev = {"open": 1.0995, "close": 1.0990}
    try:
        _with_fixed_indicators(row, prev)
        result = es.evaluate("EURUSD", _UPTREND, _DUMMY_M5)
    finally:
        _restore_indicators()
    check("diag: pullback FAIL -> pattern/rsi/vol stay 'n/a' (short-circuit)",
          result.signal == "HOLD" and result.diag["pullback"] == "FAIL"
          and result.diag["pattern"] == "n/a" and result.diag["rsi"] == "n/a" and result.diag["vol"] == "n/a",
          result.diag)


def test_diag_pattern_none():
    es = EntrySignal()
    row = {"open": 1.1000, "high": 1.1002, "low": 1.0999, "close": 1.1001,
           "ema20": 1.1000, "rsi14": 55.0, "atr14": 0.001}
    prev = {"open": 1.1000, "close": 1.1001}
    try:
        _with_fixed_indicators(row, prev)
        result = es.evaluate("EURUSD", _UPTREND, _DUMMY_M5)
    finally:
        _restore_indicators()
    check("diag: pullback OK, no candlestick pattern -> pattern='none', rsi/vol stay 'n/a'",
          result.signal == "HOLD" and result.diag["pullback"] == "OK"
          and result.diag["pattern"] == "none" and result.diag["rsi"] == "n/a" and result.diag["vol"] == "n/a",
          result.diag)


def _engulf_row_prev():
    # Bullish engulfing, pullback-OK, body=0.0020 >= 0.3xATR14(0.001)
    row = {"open": 1.0990, "high": 1.1015, "low": 1.0985, "close": 1.1010,
           "ema20": 1.1000, "atr14": 0.001}
    prev = {"open": 1.0995, "close": 1.0990}
    return row, prev


def test_diag_rsi_out_of_zone():
    es = EntrySignal()
    row, prev = _engulf_row_prev()
    row["rsi14"] = 80.0  # outside CALL zone [45, 65]
    try:
        _with_fixed_indicators(row, prev)
        result = es.evaluate("EURUSD", _UPTREND, _DUMMY_M5)
    finally:
        _restore_indicators()
    check("diag: pullback+pattern OK, RSI outside zone -> rsi shows value+zone+FAIL, vol stays 'n/a'",
          result.signal == "HOLD" and result.diag["pullback"] == "OK"
          and result.diag["pattern"] == "engulfing"
          and result.diag["rsi"] == "80.0(zone 45-65 FAIL)" and result.diag["vol"] == "n/a",
          result.diag)


def test_diag_volatility_fail():
    es = EntrySignal()
    row, prev = _engulf_row_prev()
    row["rsi14"] = 55.0  # inside zone
    history = pd.Series([0.01] * 30)  # median 0.01 -> valid range [0.005, 0.025]; row atr14=0.001 too low
    try:
        _with_fixed_indicators(row, prev)
        result = es.evaluate("EURUSD", _UPTREND, _DUMMY_M5, atr_history_100=history)
    finally:
        _restore_indicators()
    check("diag: pullback+pattern+RSI OK, volatility outside median band -> vol='FAIL'",
          result.signal == "HOLD" and result.diag["pullback"] == "OK"
          and result.diag["pattern"] == "engulfing"
          and result.diag["rsi"] == "55.0(zone 45-65 OK)" and result.diag["vol"] == "FAIL",
          result.diag)


def test_diag_full_pass_actionable():
    es = EntrySignal()
    row, prev = _engulf_row_prev()
    row["rsi14"] = 55.0
    history = pd.Series([0.001] * 30)  # median 0.001 -> valid range [0.0005, 0.0025]; row atr14=0.001 OK
    try:
        _with_fixed_indicators(row, prev)
        result = es.evaluate("EURUSD", _UPTREND, _DUMMY_M5, atr_history_100=history)
    finally:
        _restore_indicators()
    check("diag: all 4 gates pass -> signal=CALL, diag all OK/engulfing",
          result.signal == "CALL" and result.diag == {
              "pullback": "OK", "pattern": "engulfing",
              "rsi": "55.0(zone 45-65 OK)", "vol": "OK",
          }, (result.signal, result.diag))


# ─────────────────────────────────────────
# B. state_machine.py — _log_signal() fires on every on_m5_close()/on_m15_close() branch
# ─────────────────────────────────────────
class _FakeStore:
    def m5_df(self):
        return pd.DataFrame({"x": [0] * 22})

    def m15_df(self):
        return pd.DataFrame({"x": [0] * 60})


class _FakeTrendFilter:
    """Only exercised by on_m15_close(); on_m5_close() reads the trend_states cache
    directly and never calls trend_filter itself."""

    def __init__(self, status: str = "UPTREND"):
        self._status = status

    def evaluate(self, m15_df, asset):
        return TrendState(asset=asset, status=self._status, ema20=1.1, ema50=1.09,
                           atr14=0.001, computed_at="x")


class _FakeEntrySignal:
    """Duck-typed stand-in so the state_machine tests exercise wiring/logging in
    isolation from entry_signal's real indicator math (that's covered in section A)."""

    def __init__(self, result: EntryResult):
        self._result = result

    def evaluate(self, asset, trend, m5_df, atr_history_100=None):
        return self._result


def _fresh_risk_manager(suffix: str) -> RiskManager:
    import os
    state_path = f"data/_test_risk_sigloG_{suffix}.json"
    snapshot_path = f"data/_test_snap_sigloG_{suffix}.json"
    for p in (state_path, snapshot_path):
        if os.path.exists(p):
            os.remove(p)
    return RiskManager(RiskConfig(), state_path=state_path, snapshot_path=snapshot_path)


def _make_sm(entry_signal, assets=("EURUSD-op",), suffix="a") -> BotStateMachine:
    asset = assets[0]
    return BotStateMachine(
        assets=list(assets),
        candle_stores={asset: _FakeStore()},
        trend_filter=_FakeTrendFilter(),  # only used by on_m15_close(); on_m5_close reads trend_states cache
        entry_signal=entry_signal,
        time_filter=TimeFilter(),
        risk_manager=_fresh_risk_manager(suffix),
        place_order_fn=lambda asset, direction, stake: {"id": "T1", "asset": asset, "direction": direction},
        get_balance_fn=lambda: 1000.0,
        now_fn=lambda: datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK),  # Thursday, inside 14:00-17:00 window
    )


def test_log_fires_on_trend_no_trade():
    sm = _make_sm(_FakeEntrySignal(EntryResult.hold("EURUSD-op", "unused")), suffix="trendnotrade")
    sm.trend_states["EURUSD-op"] = TrendState(asset="EURUSD-op", status="NO_TRADE",
                                               ema20=None, ema50=None, atr14=None, computed_at=None)
    lines = _capture("state_machine", lambda: sm.on_m5_close("EURUSD-op"))
    check("state_machine: trend NO_TRADE branch still emits [SPEC_V1_SIGNAL]",
          any("[SPEC_V1_SIGNAL]" in l and "trend NO_TRADE" in l for l in lines), lines)


def test_log_fires_on_no_candle_store():
    sm = _make_sm(_FakeEntrySignal(EntryResult.hold("EURUSD-op", "unused")), suffix="nostore")
    sm.candle_stores.pop("EURUSD-op")
    sm.trend_states["EURUSD-op"] = TrendState(asset="EURUSD-op", status="UPTREND",
                                               ema20=1.1, ema50=1.09, atr14=0.001, computed_at="x")
    lines = _capture("state_machine", lambda: sm.on_m5_close("EURUSD-op"))
    check("state_machine: no candle store branch still emits [SPEC_V1_SIGNAL]",
          any("[SPEC_V1_SIGNAL]" in l and "no candle store" in l for l in lines), lines)


def test_log_fires_on_entry_signal_hold_with_diag():
    diag = {"pullback": "OK", "pattern": "none", "rsi": "n/a", "vol": "n/a"}
    result = EntryResult.hold("EURUSD-op", "ไม่พบแท่งยืนยัน (engulfing/pinbar)", diag)
    sm = _make_sm(_FakeEntrySignal(result), suffix="holddiag")
    sm.trend_states["EURUSD-op"] = TrendState(asset="EURUSD-op", status="UPTREND",
                                               ema20=1.1, ema50=1.09, atr14=0.001, computed_at="x")
    lines = _capture("state_machine", lambda: sm.on_m5_close("EURUSD-op"))
    line = next((l for l in lines if "[SPEC_V1_SIGNAL]" in l), None)
    check("state_machine: entry_signal HOLD branch logs full diag breakdown",
          line is not None and "trend=UPTREND" in line and "pullback=OK" in line
          and "pattern=none" in line and "rsi=n/a" in line and "vol=n/a" in line
          and "HOLD (ไม่พบแท่งยืนยัน" in line, line)


def test_log_fires_on_enter_signal():
    diag = {"pullback": "OK", "pattern": "engulfing", "rsi": "55.0(zone 45-65 OK)", "vol": "OK"}
    row = pd.Series({"close": 1.1, "ema20": 1.1, "rsi14": 55.0, "atr14": 0.001, "ts": 1})
    result = EntryResult.actionable("EURUSD-op", "CALL", "engulfing", row,
                                     TrendState(asset="EURUSD-op", status="UPTREND", ema20=1.1,
                                                ema50=1.09, atr14=0.001, computed_at="x"), diag)
    sm = _make_sm(_FakeEntrySignal(result), suffix="enter")
    sm.trend_states["EURUSD-op"] = TrendState(asset="EURUSD-op", status="UPTREND",
                                               ema20=1.1, ema50=1.09, atr14=0.001, computed_at="x")
    calls = {"n": 0}
    orig_place = sm.place_order_fn
    sm.place_order_fn = lambda *a, **kw: (calls.__setitem__("n", calls["n"] + 1), orig_place(*a, **kw))[1]
    lines = _capture("state_machine", lambda: sm.on_m5_close("EURUSD-op"))
    line = next((l for l in lines if "[SPEC_V1_SIGNAL]" in l), None)
    check("state_machine: full-pass ENTER branch logs ENTER CALL (engulfing) with all diag OK",
          line is not None and "decision=ENTER CALL (engulfing)" in line
          and "pullback=OK" in line and "pattern=engulfing" in line
          and "rsi=55.0(zone 45-65 OK)" in line and "vol=OK" in line, line)
    check("state_machine: ENTER branch actually calls place_order_fn (broker call reached)",
          calls["n"] == 1)


def test_log_fires_on_m15_close():
    sm = _make_sm(_FakeEntrySignal(EntryResult.hold("EURUSD-op", "unused")), suffix="m15")
    lines = _capture("state_machine", lambda: sm.on_m15_close("EURUSD-op"))
    check("state_machine: on_m15_close() also emits [SPEC_V1_SIGNAL] every call",
          any("[SPEC_V1_SIGNAL]" in l and "M15 trend refresh" in l for l in lines), lines)


def test_log_fires_even_when_killed():
    sm = _make_sm(_FakeEntrySignal(EntryResult.hold("EURUSD-op", "unused")), suffix="killed")
    sm.global_state = "KILLED"
    lines = _capture("state_machine", lambda: sm.on_m5_close("EURUSD-op"))
    check("state_machine: KILLED global_state still emits [SPEC_V1_SIGNAL] (no truly silent branch)",
          any("[SPEC_V1_SIGNAL]" in l and "KILLED" in l for l in lines), lines)


def run_all():
    tests = [
        test_diag_no_trade_short_circuit, test_diag_pullback_fail, test_diag_pattern_none,
        test_diag_rsi_out_of_zone, test_diag_volatility_fail, test_diag_full_pass_actionable,
        test_log_fires_on_trend_no_trade, test_log_fires_on_no_candle_store,
        test_log_fires_on_entry_signal_hold_with_diag, test_log_fires_on_enter_signal,
        test_log_fires_on_m15_close, test_log_fires_even_when_killed,
    ]
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        t()
    print(f"\n{'='*60}\nTOTAL: {len(PASS)} passed, {len(FAIL)} failed\n{'='*60}")
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print(f"  - {f}")
        sys.exit(1)


if __name__ == "__main__":
    run_all()
