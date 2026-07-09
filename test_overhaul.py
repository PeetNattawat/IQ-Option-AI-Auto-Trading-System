"""
Offline self-test for the strategy + risk overhaul (items 1-8).
Tests: flat stake, RSI veto, MACD gate, bucket veto.
Run:  python test_overhaul.py
No network access required.
"""

import sys
import os
import types

# ── Stub out heavy network dependencies before importing trading_engine ──
# We replace iqoptionapi with a thin shim so the import doesn't fail offline.
def _make_fake_iqoption():
    pkg = types.ModuleType("iqoptionapi")
    stable = types.ModuleType("iqoptionapi.stable_api")
    constants = types.ModuleType("iqoptionapi.constants")
    class FakeIQ:
        pass
    stable.IQ_Option = FakeIQ
    constants.ACTIVES = {}
    pkg.stable_api = stable
    pkg.constants = constants
    sys.modules["iqoptionapi"] = pkg
    sys.modules["iqoptionapi.stable_api"] = stable
    sys.modules["iqoptionapi.constants"] = constants

_make_fake_iqoption()

import numpy as np
import pandas as pd

# Change cwd so data/ and logs/ paths in trading_engine resolve correctly
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from trading_engine import (
    TradingConfig, SignalEngine, TradeManager,
    adx_band, rsi_band, signal_bucket_key, compute_bucket_winrates,
    SignalResult,
)

PASS = []
FAIL = []

def check(name: str, condition: bool, detail: str = ""):
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


# ══════════════════════════════════════════════
#  ITEM 1 — Flat stake with martingale OFF
# ══════════════════════════════════════════════
print("\n--- Item 1: Flat stake (martingale OFF) ---")

class _FakeTradeManagerBase:
    """Minimal stand-in — only tests next_auto_stake / _advance_martingale"""
    pass

cfg_flat = TradingConfig(martingale_enabled=False, trade_amount=50.0, martingale_base=50.0)

# Mimic TradeManager methods without connecting to IQ
tm = object.__new__(TradeManager)
tm.cfg = cfg_flat
tm.current_step = 0

# Step 0 — flat
s0 = tm.next_auto_stake("EURUSD")
check("stake at step 0 is 50", s0 == 50.0, f"got {s0}")

# Simulate a loss — with martingale OFF, step should NOT advance
tm._advance_martingale("EURUSD", "LOSS")
s1 = tm.next_auto_stake("EURUSD")
check("stake after LOSS still 50 (no ladder)", s1 == 50.0, f"got {s1}")

tm._advance_martingale("EURUSD", "LOSS")
tm._advance_martingale("EURUSD", "LOSS")
s3 = tm.next_auto_stake("EURUSD")
check("stake after 3 losses still 50", s3 == 50.0, f"got {s3}")

# Sanity: with martingale ON the ladder WOULD progress (global step)
cfg_mg = TradingConfig(martingale_enabled=True, martingale_base=50.0,
                       martingale_multiplier=2.0, martingale_max_steps=4)
tm2 = object.__new__(TradeManager)
tm2.cfg = cfg_mg
tm2.current_step = 0
tm2._advance_martingale("EURUSD", "LOSS")
s_mg = tm2.next_auto_stake("EURUSD")
check("martingale ON step 1 gives 100", s_mg == 100.0, f"got {s_mg}")


# ══════════════════════════════════════════════
#  Helper: build minimal df for SignalEngine
# ══════════════════════════════════════════════
def _build_df(rsi_val, macd_hist_val, prev_macd_hist_val,
              ema_fast=1.10, ema_slow=1.09, ema_trend=1.08,
              adx_val=30.0, atr_val=0.001, close_val=1.10):
    """Build a two-row dataframe with pre-cooked indicator values.
    SignalEngine.evaluate() reads row = df.iloc[-1], prev = df.iloc[-2]."""
    n = 2
    data = {
        "close": [close_val] * n,
        "rsi": [rsi_val] * n,
        "macd_hist": [prev_macd_hist_val, macd_hist_val],
        "ema_fast": [ema_fast] * n,
        "ema_slow": [ema_slow] * n,
        "ema_trend": [ema_trend] * n,
        "adx": [adx_val] * n,
        "atr": [atr_val] * n,
        "atr_avg": [atr_val * 0.8] * n,
        "volume": [1000.0] * n,
        "volume_avg": [800.0] * n,
        "bb_upper": [close_val * 1.01] * n,
        "bb_lower": [close_val * 0.99] * n,
        "bb_mid": [close_val] * n,
        "macd": [0.0] * n,
        "macd_signal": [0.0] * n,
    }
    df = pd.DataFrame(data)
    # Pad to ema_trend + 10 rows so the length check passes
    pad = TradingConfig().ema_trend + 10 - n  # 210 - 2 = 208 extra rows
    if pad > 0:
        df = pd.concat([df] * (pad // n + 1), ignore_index=True).iloc[:pad + n].copy()
        # Restore the last two rows with our values
        for col in data:
            df.iloc[-2][col]  # trigger copy
        for col in data:
            df[col] = df[col].values  # ensure writable
            df.iloc[-2, df.columns.get_loc(col)] = data[col][0]
            df.iloc[-1, df.columns.get_loc(col)] = data[col][1]
    return df


# ══════════════════════════════════════════════
#  ITEM 5 — RSI veto: CALL blocked when rsi > 66
# ══════════════════════════════════════════════
print("\n--- Item 5: RSI hard veto ---")

cfg_sig = TradingConfig(
    martingale_enabled=False,
    adx_min=28.0,
    dir_margin=30.0,
    rsi_call_min=52.0, rsi_call_max=63.0,
    rsi_put_min=37.0,  rsi_put_max=48.0,
    confidence_threshold=70.0,
)
engine = SignalEngine(cfg_sig)

# CALL with RSI=68 — should be vetoed (> 66 veto threshold)
# EMA bull, MACD confirms CALL, ADX ok, margin >30, but RSI too high
df_rsi68 = _build_df(
    rsi_val=68.0,
    macd_hist_val=0.002, prev_macd_hist_val=0.001,   # MACD confirms CALL
    ema_fast=1.102, ema_slow=1.100, ema_trend=1.098, # full bull EMA (25 pts)
    adx_val=35.0,
)
sig_rsi68 = engine.evaluate(df_rsi68, "EURUSD")
check("CALL with RSI=68 is HOLD (RSI veto)", sig_rsi68.signal == "HOLD",
      f"got signal={sig_rsi68.signal}, reasons={sig_rsi68.reasons}")

# CALL with RSI=58 — should NOT be vetoed
df_rsi58 = _build_df(
    rsi_val=58.0,
    macd_hist_val=0.002, prev_macd_hist_val=0.001,
    ema_fast=1.102, ema_slow=1.100, ema_trend=1.098,
    adx_val=35.0,
)
sig_rsi58 = engine.evaluate(df_rsi58, "EURUSD")
check("CALL with RSI=58 is CALL (no veto)", sig_rsi58.signal == "CALL",
      f"got signal={sig_rsi58.signal}, reasons={sig_rsi58.reasons}")


# ══════════════════════════════════════════════
#  ITEM 4 — MACD hard gate
# ══════════════════════════════════════════════
print("\n--- Item 4: MACD hard gate ---")

# CALL signal but MACD does not confirm (hist < 0)
df_macd_no = _build_df(
    rsi_val=58.0,
    macd_hist_val=-0.001, prev_macd_hist_val=-0.002,  # MACD negative — no CALL confirm
    ema_fast=1.102, ema_slow=1.100, ema_trend=1.098,
    adx_val=35.0,
)
sig_macd_no = engine.evaluate(df_macd_no, "EURUSD")
check("CALL with MACD < 0 is HOLD (MACD gate)", sig_macd_no.signal == "HOLD",
      f"got signal={sig_macd_no.signal}")

# CALL signal with MACD confirming (hist > 0 and rising)
df_macd_ok = _build_df(
    rsi_val=58.0,
    macd_hist_val=0.003, prev_macd_hist_val=0.001,
    ema_fast=1.102, ema_slow=1.100, ema_trend=1.098,
    adx_val=35.0,
)
sig_macd_ok = engine.evaluate(df_macd_ok, "EURUSD")
check("CALL with MACD confirming is CALL", sig_macd_ok.signal == "CALL",
      f"got signal={sig_macd_ok.signal}")

# PUT signal but MACD does not confirm (hist > 0)
df_put_macd_no = _build_df(
    rsi_val=42.0,
    macd_hist_val=0.002, prev_macd_hist_val=0.001,  # MACD positive — no PUT confirm
    ema_fast=1.098, ema_slow=1.100, ema_trend=1.102, # bear EMA
    adx_val=35.0,
)
sig_put_macd_no = engine.evaluate(df_put_macd_no, "EURUSD")
check("PUT with MACD > 0 is HOLD (MACD gate)", sig_put_macd_no.signal == "HOLD",
      f"got signal={sig_put_macd_no.signal}")


# ══════════════════════════════════════════════
#  ITEM 6 — Bucket veto activates on >= 10 losing samples
# ══════════════════════════════════════════════
print("\n--- Item 6: Bucket veto ---")

# Build 12 closed AUTO trades in the same bucket: EURUSD CALL, ADX=30 (band 28-34), RSI=58 (band 55-60)
# All LOSS — winrate = 0% → should trigger veto
losing_bucket_trades = []
for i in range(12):
    losing_bucket_trades.append({
        "id": 9000000 + i,
        "asset": "EURUSD",
        "direction": "CALL",
        "source": "auto",
        "status": "closed",
        "result": "LOSS",
        "pnl": -50.0,
        "adx": 30.5,   # adx_band -> "28-34"
        "rsi": 58.0,   # rsi_band -> "55-60"
    })

buckets = compute_bucket_winrates(losing_bucket_trades)
key = signal_bucket_key("EURUSD", "CALL", 30.5, 58.0)
check("losing bucket key exists", key in buckets, f"buckets: {list(buckets.keys())}")
rec = buckets.get(key, {})
check("losing bucket has 12 samples", rec.get("total") == 12, f"total={rec.get('total')}")
check("losing bucket winrate = 0.0", rec.get("winrate") == 0.0, f"wr={rec.get('winrate')}")

# Now wire a TradeManager-like veto check
tm_veto = object.__new__(TradeManager)
tm_veto.cfg = TradingConfig()
tm_veto.BUCKET_MIN_SAMPLES = 10
tm_veto.BUCKET_MIN_WINRATE = 0.50
tm_veto._bucket_cache = buckets

sig_veto_candidate = SignalResult(
    asset="EURUSD", timeframe=300, signal="CALL", confidence=80.0,
    score_breakdown={}, reasons=[],
    entry_price=1.10, rsi=58.0, atr=0.001,
    ema_fast=1.10, ema_slow=1.09, ema_trend=1.08,
    adx=30.5, macd_hist=0.001, timestamp="2026-06-16T00:00:00"
)
bucket_vetoed, bucket_msg = tm_veto.apply_bucket_veto(sig_veto_candidate)
check("bucket veto fires on all-losing bucket (>=10 samples)", bucket_vetoed,
      f"vetoed={bucket_vetoed}, msg={bucket_msg}")
print(f"         veto msg: {bucket_msg}")

# With only 9 samples — veto should NOT fire
nine_trades = losing_bucket_trades[:9]
buckets9 = compute_bucket_winrates(nine_trades)
tm_veto._bucket_cache = buckets9
vetoed9, _ = tm_veto.apply_bucket_veto(sig_veto_candidate)
check("bucket veto silent with only 9 samples", not vetoed9)

# With 10 samples but 60% winrate — should NOT fire
mixed_trades = []
for i in range(10):
    mixed_trades.append({
        "asset": "EURUSD", "direction": "CALL",
        "source": "auto", "status": "closed",
        "result": "WIN" if i < 6 else "LOSS",
        "pnl": 42.5 if i < 6 else -50.0,
        "adx": 30.5, "rsi": 58.0,
    })
buckets_mixed = compute_bucket_winrates(mixed_trades)
tm_veto._bucket_cache = buckets_mixed
vetoed_mixed, _ = tm_veto.apply_bucket_veto(sig_veto_candidate)
check("bucket veto silent when winrate >= 50% (6/10)", not vetoed_mixed)


# ══════════════════════════════════════════════
#  ITEM 7 — Band helpers produce correct labels
# ══════════════════════════════════════════════
print("\n--- Item 7: Band classification helpers ---")
check("adx_band(25) = 'lt28'", adx_band(25.0) == "lt28")
check("adx_band(30) = '28-34'", adx_band(30.0) == "28-34")
check("adx_band(36) = '34-40'", adx_band(36.0) == "34-40")
check("adx_band(42) = '40+'", adx_band(42.0) == "40+")
check("rsi_band(58) = '55-60'", rsi_band(58.0) == "55-60")
check("rsi_band(50) = '50-55'", rsi_band(50.0) == "50-55")
check("rsi_band(42) = '40-45'", rsi_band(42.0) == "40-45")
check("rsi_band(30) = '30-35'", rsi_band(30.0) == "30-35")


# ══════════════════════════════════════════════
#  ITEM 3 — Config.json verification
# ══════════════════════════════════════════════
print("\n--- Item 3: config.json ---")
import json
with open("data/config.json") as f:
    cfg_json = json.load(f)
check("martingale_enabled=false in config.json", cfg_json.get("martingale_enabled") == False,
      f"got {cfg_json.get('martingale_enabled')}")
check("daily_loss_limit=150 in config.json", cfg_json.get("daily_loss_limit") == 150.0,
      f"got {cfg_json.get('daily_loss_limit')}")
check("max_open_positions=1 in config.json", cfg_json.get("max_open_positions") == 1,
      f"got {cfg_json.get('max_open_positions')}")


# ══════════════════════════════════════════════
#  ITEM 1 check: TradingConfig default
# ══════════════════════════════════════════════
print("\n--- Item 1: TradingConfig defaults ---")
cfg_default = TradingConfig()
check("TradingConfig.martingale_enabled default = False", cfg_default.martingale_enabled == False)
check("TradingConfig.daily_loss_limit default = 150", cfg_default.daily_loss_limit == 150.0)
check("TradingConfig.adx_min default = 28", cfg_default.adx_min == 28.0)
check("TradingConfig.dir_margin default = 30", cfg_default.dir_margin == 30.0)
check("TradingConfig.rsi_call_min default = 52", cfg_default.rsi_call_min == 52.0)
check("TradingConfig.rsi_call_max default = 63", cfg_default.rsi_call_max == 63.0)


# ══════════════════════════════════════════════
#  BUG-143 regression — martingale must default OFF even when the config
#  dict/file is missing the key entirely (not just when the dataclass is
#  constructed bare). This is the exact "config.json missing/corrupt" case
#  Iris's QA report flagged as the real-world risk.
# ══════════════════════════════════════════════
print("\n--- Bug-143: martingale_enabled fallback when config key is absent ---")
from main import apply_runtime_config

cfg_no_key = TradingConfig()
apply_runtime_config(cfg_no_key, {})  # empty dict == config.json missing / key absent
check("apply_runtime_config({}) leaves martingale_enabled = False",
      cfg_no_key.martingale_enabled == False, f"got {cfg_no_key.martingale_enabled}")

cfg_other_keys = TradingConfig()
apply_runtime_config(cfg_other_keys, {"trade_amount": 100.0, "max_open_positions": 2})
check("apply_runtime_config(dict without martingale_enabled key) leaves martingale_enabled = False",
      cfg_other_keys.martingale_enabled == False, f"got {cfg_other_keys.martingale_enabled}")


# ══════════════════════════════════════════════
#  BUG-144 regression — PLACE_ORDER latency check must run BEFORE the broker
#  call, and a trade that WAS actually placed must never be silently
#  un-tracked (no-overlap / PnL / trade_logger bookkeeping must always match
#  broker reality). See .wolf/buglog.json bug-144, Iris QA report BUG-3.
# ══════════════════════════════════════════════
print("\n--- Bug-144: PLACE_ORDER latency check happens before the broker call ---")
import time
import time as _time
from datetime import datetime as _datetime
from zoneinfo import ZoneInfo as _ZoneInfo
from state_machine import BotStateMachine, LATENCY_BUDGET_SECONDS
from entry_signal import EntryResult
from trend_filter import TrendState

_BANGKOK = _ZoneInfo("Asia/Bangkok")


class _FakeTimeFilter:
    def is_tradeable(self, now):
        return True, ""


class _FakeRiskManager:
    def __init__(self):
        self._open_positions = 0
        self.results_recorded = []

    def roll_boundaries(self, now, balance):
        pass

    def can_trade(self, now, balance):
        return True, ""

    def stake_amount(self, balance):
        return 10.0

    def record_order_placed(self):
        self._open_positions += 1

    def record_order_result(self, pnl, result, now_th=None):
        self._open_positions = max(0, self._open_positions - 1)
        self.results_recorded.append((pnl, result))


class _FakeEntrySignal:
    def evaluate(self, asset, trend, df):
        return EntryResult(asset=asset, signal="CALL", reason="test signal", pattern="engulfing")


class _FakeCandleStore:
    def m5_df(self):
        return None

    def m15_df(self):
        return None


def _make_sm(place_order_fn):
    asset = "EURUSD-op"
    rm = _FakeRiskManager()
    sm = BotStateMachine(
        assets=[asset],
        candle_stores={asset: _FakeCandleStore()},
        trend_filter=None,
        entry_signal=_FakeEntrySignal(),
        time_filter=_FakeTimeFilter(),
        risk_manager=rm,
        place_order_fn=place_order_fn,
        get_balance_fn=lambda: 1000.0,
        now_fn=lambda: _datetime.now(_BANGKOK),
    )
    sm.trend_states[asset] = TrendState(asset=asset, status="UPTREND", ema20=1.0, ema50=0.9,
                                         atr14=0.001, computed_at="2026-07-09T00:00:00")
    return sm, rm, asset


# Case 1: broker call is instant (well under budget) -> order placed & tracked normally.
def _instant_broker(asset, side, stake):
    return {"id": "T1", "asset": asset, "side": side, "stake": stake}

sm1, rm1, asset1 = _make_sm(_instant_broker)
state1 = sm1.on_m5_close(asset1)
check("fast broker call -> state IN_TRADE", state1.state == "IN_TRADE", f"got {state1.state}")
check("fast broker call -> open_positions == 1", rm1._open_positions == 1, f"got {rm1._open_positions}")


# Case 2: our OWN pre-broker-call processing already blew the latency budget
# (simulated by monkey-patching time.time to jump forward before place_order_fn
# would be called). The broker must NEVER be called in this case.
_broker_called = {"count": 0}

def _should_never_be_called(asset, side, stake):
    _broker_called["count"] += 1
    return {"id": "SHOULD_NOT_HAPPEN", "asset": asset, "side": side, "stake": stake}

sm2, rm2, asset2 = _make_sm(_should_never_be_called)
_real_time = time.time
_slow_start = {"t0": None}

def _fake_time_slow():
    if _slow_start["t0"] is None:
        _slow_start["t0"] = _real_time()
        return _slow_start["t0"]
    # every subsequent call (including the pre-call latency check) is already
    # past the budget, simulating slow internal processing before submission.
    return _slow_start["t0"] + LATENCY_BUDGET_SECONDS + 1.0

time.time = _fake_time_slow
try:
    state2 = sm2.on_m5_close(asset2)
finally:
    time.time = _real_time

check("pre-call latency exceeded -> broker NEVER called", _broker_called["count"] == 0,
      f"got {_broker_called['count']} calls")
check("pre-call latency exceeded -> state IDLE (signal cancelled)", state2.state == "IDLE",
      f"got {state2.state}")
check("pre-call latency exceeded -> open_positions untouched (never incremented)",
      rm2._open_positions == 0, f"got {rm2._open_positions}")


# Case 3: the broker call ITSELF is slow (round-trip > budget) but DOES succeed —
# a real position now exists at the broker. This must be tracked normally
# (IN_TRADE, open_positions incremented, trade dict flagged latency_violation),
# never silently discarded like the pre-fix bug did.
def _slow_but_successful_broker(asset, side, stake):
    _time.sleep(0)  # no real sleep needed; latency is injected via fake clock below
    return {"id": "T3", "asset": asset, "side": side, "stake": stake}

sm3, rm3, asset3 = _make_sm(_slow_but_successful_broker)
_call_count = {"n": 0}
_t0 = _real_time()

def _fake_time_roundtrip():
    _call_count["n"] += 1
    # 1st call = candle_close_event_time, 2nd = pre-call check (still fast),
    # 3rd+ = post-broker-call latency measurement (now slow).
    if _call_count["n"] <= 2:
        return _t0
    return _t0 + LATENCY_BUDGET_SECONDS + 1.0

time.time = _fake_time_roundtrip
try:
    state3 = sm3.on_m5_close(asset3)
finally:
    time.time = _real_time

check("post-broker-call latency exceeded -> trade STILL tracked as IN_TRADE",
      state3.state == "IN_TRADE", f"got {state3.state}")
check("post-broker-call latency exceeded -> open_positions == 1 (not discarded)",
      rm3._open_positions == 1, f"got {rm3._open_positions}")


# ══════════════════════════════════════════════
#  SUMMARY
# ══════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"RESULTS: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print(f"\nFailed tests:")
    for f in FAIL:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("\nALL TESTS PASSED")
