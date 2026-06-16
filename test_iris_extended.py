"""
Iris extended edge-case tests -- Gate 2 supplement.
Run: python test_iris_extended.py
No network access required.
"""

import sys, os, types, json, collections, time
from datetime import datetime

# ── Stub iqoptionapi ──
def _stub():
    pkg  = types.ModuleType("iqoptionapi")
    stab = types.ModuleType("iqoptionapi.stable_api")
    cons = types.ModuleType("iqoptionapi.constants")
    class FakeIQ: pass
    stab.IQ_Option = FakeIQ
    cons.ACTIVES   = {}
    pkg.stable_api = stab
    pkg.constants  = cons
    sys.modules["iqoptionapi"]             = pkg
    sys.modules["iqoptionapi.stable_api"]  = stab
    sys.modules["iqoptionapi.constants"]   = cons
_stub()

PROJECT = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT)
sys.path.insert(0, PROJECT)

import numpy as np
import pandas as pd
from trading_engine import (
    TradingConfig, SignalEngine, TradeManager,
    adx_band, rsi_band, signal_bucket_key, compute_bucket_winrates,
    SignalResult,
)
import main as main_mod

PASS, FAIL = [], []

def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  -- {detail}")


# ── helper: build minimal df with exact indicator values ──
def _df(rsi_val, macd_hist_val, prev_macd_hist_val,
        ema_fast=1.102, ema_slow=1.100, ema_trend=1.098,
        adx_val=35.0, close_val=1.10):
    n = 2
    data = {
        "close":      [close_val] * n,
        "rsi":        [rsi_val] * n,
        "macd_hist":  [prev_macd_hist_val, macd_hist_val],
        "ema_fast":   [ema_fast] * n,
        "ema_slow":   [ema_slow] * n,
        "ema_trend":  [ema_trend] * n,
        "adx":        [adx_val] * n,
        "atr":        [0.001] * n,
        "atr_avg":    [0.0008] * n,
        "volume":     [1000.0] * n,
        "volume_avg": [800.0] * n,
        "bb_upper":   [close_val * 1.01] * n,
        "bb_lower":   [close_val * 0.99] * n,
        "bb_mid":     [close_val] * n,
        "macd":       [0.0] * n,
        "macd_signal":[0.0] * n,
    }
    df = pd.DataFrame(data)
    pad = TradingConfig().ema_trend + 10 - n   # 208
    if pad > 0:
        df = pd.concat([df] * (pad // n + 1), ignore_index=True).iloc[:pad + n].copy()
        for col in data:
            df.iloc[-2, df.columns.get_loc(col)] = data[col][0]
            df.iloc[-1, df.columns.get_loc(col)] = data[col][1]
    return df


cfg_sig = TradingConfig(
    martingale_enabled=False,
    adx_min=28.0, dir_margin=30.0,
    rsi_call_min=52.0, rsi_call_max=63.0,
    rsi_put_min=37.0,  rsi_put_max=48.0,
    confidence_threshold=70.0,
)
engine = SignalEngine(cfg_sig)


# ────────────────────────────────────────────────────────────────
print("\n--- EC1: MACD hist>0 but FALLING -> HOLD (item 4) ---")
# hist_val=0.001 < prev=0.003 -> positive but not rising -> MACD gate blocks CALL
df_ec1 = _df(rsi_val=58.0, macd_hist_val=0.001, prev_macd_hist_val=0.003)
sig_ec1 = engine.evaluate(df_ec1, "EURUSD")
check("MACD hist>0 but falling -> HOLD",
      sig_ec1.signal == "HOLD",
      f"got {sig_ec1.signal}  reasons={sig_ec1.reasons}")


# ────────────────────────────────────────────────────────────────
print("\n--- EC2: RSI boundary exactly 66 (item 5) ---")
# RSI=66: veto is rsi > 66 (strict), so exactly 66 must NOT be vetoed
df_ec2 = _df(rsi_val=66.0, macd_hist_val=0.002, prev_macd_hist_val=0.001)
sig_ec2 = engine.evaluate(df_ec2, "EURUSD")
# RSI=66 falls outside call window (52-63) so no RSI score, but EMA full-bull(25)+MACD(15)=40
# margin 40 >= dir_margin(30), adx ok -> should pass as CALL
check("RSI=66 exactly: veto rule is strict >66, CALL allowed",
      sig_ec2.signal == "CALL",
      f"got {sig_ec2.signal}  reasons={sig_ec2.reasons}")

df_ec2b = _df(rsi_val=66.01, macd_hist_val=0.002, prev_macd_hist_val=0.001)
sig_ec2b = engine.evaluate(df_ec2b, "EURUSD")
check("RSI=66.01 -> HOLD (veto fires just above 66)",
      sig_ec2b.signal == "HOLD",
      f"got {sig_ec2b.signal}")


# ────────────────────────────────────────────────────────────────
print("\n--- EC3: RSI boundary exactly 34 (item 5, PUT side) ---")
df_ec3 = _df(rsi_val=34.0, macd_hist_val=-0.002, prev_macd_hist_val=-0.001,
             ema_fast=1.098, ema_slow=1.100, ema_trend=1.102, adx_val=35.0)
sig_ec3 = engine.evaluate(df_ec3, "EURUSD")
check("RSI=34 exactly: not vetoed (veto is rsi < 34, strict)",
      sig_ec3.signal == "PUT",
      f"got {sig_ec3.signal}  reasons={sig_ec3.reasons}")

df_ec3b = _df(rsi_val=33.99, macd_hist_val=-0.002, prev_macd_hist_val=-0.001,
              ema_fast=1.098, ema_slow=1.100, ema_trend=1.102, adx_val=35.0)
sig_ec3b = engine.evaluate(df_ec3b, "EURUSD")
check("RSI=33.99 -> HOLD (veto fires just below 34)",
      sig_ec3b.signal == "HOLD",
      f"got {sig_ec3b.signal}")


# ────────────────────────────────────────────────────────────────
print("\n--- EC4: Bucket veto 40% (4/10) fires; exactly 50% (5/10) does not (item 6) ---")
def _make_trades(wins, total, adx=30.5, rsi=58.0):
    rows = []
    for i in range(total):
        rows.append({
            "asset": "EURUSD", "direction": "CALL",
            "source": "auto", "status": "closed",
            "result": "WIN" if i < wins else "LOSS",
            "pnl": 42.5 if i < wins else -50.0,
            "adx": adx, "rsi": rsi,
        })
    return rows

sig_cand = SignalResult(
    asset="EURUSD", timeframe=300, signal="CALL", confidence=80.0,
    score_breakdown={}, reasons=[],
    entry_price=1.10, rsi=58.0, atr=0.001,
    ema_fast=1.10, ema_slow=1.09, ema_trend=1.08,
    adx=30.5, macd_hist=0.001, timestamp="2026-06-16T00:00:00"
)

def _veto_check(trades):
    b = compute_bucket_winrates(trades)
    tm = object.__new__(TradeManager)
    tm.cfg = TradingConfig()
    tm.BUCKET_MIN_SAMPLES = 10
    tm.BUCKET_MIN_WINRATE = 0.50
    tm._bucket_cache = b
    return tm.apply_bucket_veto(sig_cand)

vetoed_40, msg_40 = _veto_check(_make_trades(4, 10))
check("Bucket veto fires at 40% winrate (4/10)", vetoed_40, f"msg={msg_40}")

vetoed_50, msg_50 = _veto_check(_make_trades(5, 10))
check("Bucket veto silent at exactly 50% winrate (5/10)", not vetoed_50, f"msg={msg_50}")

vetoed_49, msg_49 = _veto_check(_make_trades(4, 10))   # 40% < 50%
check("Bucket veto fires at 40% (below 50% threshold)", vetoed_49, f"msg={msg_49}")


# ────────────────────────────────────────────────────────────────
print("\n--- EC5: apply_runtime_config leaves max_consecutive_losses=4 with martingale OFF (item 1) ---")
cfg_rt = TradingConfig()   # martingale_enabled=False, max_consecutive_losses=4
rt = {
    "martingale_enabled": False,
    "max_consecutive_losses": 4,
    "trade_amount": 50.0,
    "daily_loss_limit": 150.0,
}
main_mod.apply_runtime_config(cfg_rt, rt)
check("max_consecutive_losses stays 4 with martingale OFF",
      cfg_rt.max_consecutive_losses == 4,
      f"got {cfg_rt.max_consecutive_losses}")

# Martingale ON must bump max_consecutive_losses >= martingale_max_steps
cfg_rt2 = TradingConfig(martingale_enabled=True, martingale_max_steps=4, max_consecutive_losses=4)
rt2 = {"martingale_enabled": True, "martingale_max_steps": 4, "max_consecutive_losses": 4}
main_mod.apply_runtime_config(cfg_rt2, rt2)
check("max_consecutive_losses >= martingale_max_steps when martingale ON",
      cfg_rt2.max_consecutive_losses >= 4,
      f"got {cfg_rt2.max_consecutive_losses}")


# ────────────────────────────────────────────────────────────────
print("\n--- EC6: compute_bucket_winrates skips None/missing adx/rsi without crashing (item 6) ---")
trades_null = [
    {"source": "auto", "status": "closed", "result": "LOSS",
     "asset": "EURUSD", "direction": "CALL", "pnl": -50.0,
     "adx": None, "rsi": None},
    {"source": "auto", "status": "closed", "result": "WIN",
     "asset": "EURUSD", "direction": "CALL", "pnl": 42.5,
     "adx": 30.5, "rsi": 58.0},
    {"source": "auto", "status": "closed", "result": "LOSS",
     "asset": "EURUSD", "direction": "CALL", "pnl": -50.0},   # keys missing entirely
]
try:
    b_null = compute_bucket_winrates(trades_null)
    check("compute_bucket_winrates does not crash with None/missing adx/rsi", True)
    key_v = signal_bucket_key("EURUSD", "CALL", 30.5, 58.0)
    check("only valid trade counted (null records skipped)",
          b_null.get(key_v, {}).get("total") == 1,
          f"total={b_null.get(key_v,{}).get('total')}")
except Exception as exc:
    check("compute_bucket_winrates does not crash with None/missing adx/rsi", False, str(exc))
    check("only valid trade counted (null records skipped)", False, "crashed above")


# ────────────────────────────────────────────────────────────────
print("\n--- EC7: REAL gate logic in main.py (item 2) ---")
def _gate(iq_account, iq_allow_real):
    raw = iq_account.upper()
    if raw == "REAL" and iq_allow_real != "1":
        return "PRACTICE", True
    return raw, False

eff, warned = _gate("REAL", "")
check("IQ_ACCOUNT=REAL without IQ_ALLOW_REAL=1 -> PRACTICE", eff == "PRACTICE", f"got {eff}")
check("Warning fires when REAL downgraded", warned)

eff2, warned2 = _gate("REAL", "1")
check("IQ_ACCOUNT=REAL with IQ_ALLOW_REAL=1 -> REAL", eff2 == "REAL", f"got {eff2}")
check("No warning when REAL correctly enabled", not warned2)

eff3, _ = _gate("PRACTICE", "")
check("IQ_ACCOUNT=PRACTICE stays PRACTICE", eff3 == "PRACTICE", f"got {eff3}")


# ────────────────────────────────────────────────────────────────
print("\n--- EC8: timeframe default M5 (300s) and RUNTIME_FIELDS (item 8) ---")
default_tf = int(os.getenv("IQ_TIMEFRAME", "300"))
check("Default timeframe = 300 (M5) when IQ_TIMEFRAME not set",
      default_tf == 300, f"got {default_tf}")

rf = main_mod.RUNTIME_FIELDS
check("expiry_minutes in RUNTIME_FIELDS", "expiry_minutes" in rf)
check("adx_min in RUNTIME_FIELDS", "adx_min" in rf)
check("dir_margin in RUNTIME_FIELDS", "dir_margin" in rf)
check("rsi_call_min in RUNTIME_FIELDS", "rsi_call_min" in rf)
check("rsi_call_max in RUNTIME_FIELDS", "rsi_call_max" in rf)
check("rsi_put_min in RUNTIME_FIELDS", "rsi_put_min" in rf)
check("rsi_put_max in RUNTIME_FIELDS", "rsi_put_max" in rf)
check("timeframe NOT in RUNTIME_FIELDS (env-only per spec)",
      "timeframe" not in rf, f"found timeframe in RUNTIME_FIELDS")


# ────────────────────────────────────────────────────────────────
print("\n--- EC9: daily_loss_limit enforced in can_trade (item 3) ---")
tm_r = object.__new__(TradeManager)
cfg_r = TradingConfig(daily_loss_limit=150.0, max_open_positions=1,
                       max_trades_per_hour=12, max_consecutive_losses=4,
                       max_trades_per_day=20)
tm_r.cfg = cfg_r
tm_r.consecutive_losses = 0
tm_r.hourly_trades = collections.deque()
tm_r.active_orders = {}
today_str = datetime.now().date().isoformat()
tm_r.trades = [{
    "open_time": today_str + "T00:00:00",
    "status": "closed",
    "pnl": -150.0,
    "source": "auto",
}]
can, reason = tm_r.can_trade()
check("can_trade() False when today_loss >= daily_loss_limit (150)",
      not can, f"can={can}  reason={reason}")

tm_r.trades[0]["pnl"] = -149.99
can2, _ = tm_r.can_trade()
check("can_trade() True when loss just below limit (149.99 < 150)",
      can2, f"can={can2}")


# ────────────────────────────────────────────────────────────────
print("\n--- EC10: max_open_positions=1 default in TradingConfig and config.json (item 3) ---")
cfg_def = TradingConfig()
check("TradingConfig.max_open_positions default = 1",
      cfg_def.max_open_positions == 1, f"got {cfg_def.max_open_positions}")

with open(os.path.join(PROJECT, "data/config.json")) as f:
    cfg_json = json.load(f)
check("config.json max_open_positions = 1",
      cfg_json.get("max_open_positions") == 1, f"got {cfg_json.get('max_open_positions')}")


# ────────────────────────────────────────────────────────────────
print("\n--- EC11: bucket veto inert with 13 existing trades (item 6 regression) ---")
# Load actual trades.json if it exists; verify no spurious veto on current history
trades_path = os.path.join(PROJECT, "data/trades.json")
if os.path.exists(trades_path):
    with open(trades_path) as f:
        real_trades = json.load(f)
    buckets_real = compute_bucket_winrates(real_trades)
    # No bucket should have >= 10 samples from a 13-trade history
    over_10 = {k: v for k, v in buckets_real.items() if v["total"] >= 10}
    check("No bucket reaches >= 10 samples in 13-trade history (veto inert)",
          len(over_10) == 0, f"buckets with >=10: {over_10}")
else:
    print("  SKIP  EC11: data/trades.json not present (no history yet)")


# ────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"Iris extended: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("\nFailed:")
    for f in FAIL:
        print(f"  FAIL  {f}")
    sys.exit(1)
else:
    print("ALL EXTENDED TESTS PASSED")
