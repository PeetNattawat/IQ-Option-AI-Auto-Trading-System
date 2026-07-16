"""
test_bug181_candle_store_seeding.py — bug-181 regression suite (Titan).

Root cause (confirmed via 3 rounds of production journalctl analysis, 100% reproduction,
280/280 ticks in a 3-hour in-window sample on 2026-07-15): `_ensure_candle_stores()`
(main.py) is the ONLY code that creates a `CandleStore` per resolved spec_v1 asset. It was
wired into exactly two places, both legacy-path: `run_cycle()`'s scan body (after
`resolve_assets()`) and `main_loop()`'s startup. Neither `spec_v1_m5_loop()` nor
`spec_v1_m15_loop()` ever called it, and `_spec_v1_append_new_candle()` only appends to an
EXISTING store (`if not store: return`) — it never creates one. Result: in spec_v1 mode,
`self._candle_stores` stayed empty for every resolved "-op" asset -> every M5 tick died at
state_machine.py's "no candle store" gate, every M15 trend refresh died the same way ->
trend stayed NO_TRADE/None forever -> zero orders since the 13-Jul switch to spec_v1.

Fix: call `self._ensure_candle_stores()` inside BOTH `spec_v1_m5_loop()` and
`spec_v1_m15_loop()`, after `_sync_state_machine_v1()` and before the per-asset `for` loop
(mirrors the legacy call site). `_ensure_candle_stores()` is idempotent (skips assets that
already have a store — §4.1 docstring), so calling it every tick from both loops is safe
and does not depend on the legacy scan loop ever running (it doesn't, if the bot is
paused/reconnecting/weekend-halted before reaching its own `_ensure_candle_stores()` call).

This suite proves:
  A. (static) Both spec_v1_m5_loop() and spec_v1_m15_loop() call self._ensure_candle_stores()
     before their per-asset `for` loop.
  B. (functional) After _ensure_candle_stores() runs against spec_v1-resolved assets,
     self._candle_stores contains a real, populated CandleStore for each — and is
     idempotent (second call does not replace/duplicate existing stores).
  C. (functional) Once seeded, BotStateMachine.on_m5_close()/on_m15_close() no longer hit
     "no candle store" and actually reach trend/AND-gate evaluation with real (non-"n/a")
     diagnostic values — proven via the real CandleStore + real TrendFilter/EntrySignal
     code paths (IndicatorEngineV2 monkeypatched only for deterministic output, same
     convention as test_spec_v1_signal_logging.py).
  D. (regression guard) Before the fix, on the SAME seeded-then-unseeded setup, calling
     on_m5_close()/on_m15_close() WITHOUT ever calling _ensure_candle_stores() first
     reproduces the original bug ("no candle store" on every tick) — proving this suite
     would have caught the real production bug.

Run: python test_bug181_candle_store_seeding.py
No network access required.
"""

import logging
import os
import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.chdir(os.path.dirname(os.path.abspath(__file__)))


# ── Stub out heavy network dependency before importing main.py (same pattern as
# test_overhaul.py / test_spec_v1_live_wiring.py) — main.py imports trading_engine, which
# imports iqoptionapi.
def _make_fake_iqoption():
    if "iqoptionapi" in sys.modules:
        return
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

import pandas as pd

import entry_signal as es_mod
import trend_filter as tf_mod
import main
from candle_store import CandleStore
from entry_signal import EntrySignal
from risk_manager import RiskConfig, RiskManager
from state_machine import BotStateMachine
from time_filter import TimeFilter
from trend_filter import TrendFilter, TrendState

BANGKOK = ZoneInfo("Asia/Bangkok")
PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


with open("main.py", encoding="utf-8") as _f:
    MAIN_SRC = _f.read()


# ─────────────────────────────────────────
# A. Static: both spec_v1 loops seed candle stores before iterating assets
# ─────────────────────────────────────────
def test_m5_loop_calls_ensure_candle_stores_before_asset_loop():
    idx = MAIN_SRC.index("async def spec_v1_m5_loop(self):")
    idx_end = MAIN_SRC.index("async def spec_v1_m15_loop(self):", idx)
    body = MAIN_SRC[idx:idx_end]
    for_idx = body.index("for asset in self.cfg.assets:")
    before_loop = body[:for_idx]
    check("bug-181: spec_v1_m5_loop() calls self._ensure_candle_stores() before the per-asset loop",
          "self._ensure_candle_stores()" in before_loop, before_loop)


def test_m15_loop_calls_ensure_candle_stores_before_asset_loop():
    idx = MAIN_SRC.index("async def spec_v1_m15_loop(self):")
    idx_end = MAIN_SRC.index("def build_martingale_warning", idx)
    body = MAIN_SRC[idx:idx_end]
    for_idx = body.index("for asset in self.cfg.assets:")
    before_loop = body[:for_idx]
    check("bug-181: spec_v1_m15_loop() calls self._ensure_candle_stores() before the per-asset loop",
          "self._ensure_candle_stores()" in before_loop, before_loop)


# ─────────────────────────────────────────
# B. Functional: _ensure_candle_stores() populates self._candle_stores for spec_v1 assets
# ─────────────────────────────────────────
def _raw_candles(n: int, start_ts: int, seconds: int, start_price: float, step: float):
    """Synthetic strictly-increasing OHLCV candles, IQ Option raw shape ('from'/'max'/'min').
    Includes one extra still-forming candle at the end (bootstrap/_normalize drops it)."""
    out = []
    price = start_price
    for i in range(n + 1):  # +1 so drop_forming still leaves n closed candles
        o = price
        c = price + step
        out.append({
            "from": start_ts + i * seconds, "open": o, "close": c,
            "max": max(o, c) + step * 0.1, "min": min(o, c) - step * 0.1, "volume": 100,
        })
        price = c
    return out


class _FakeIQForBootstrap:
    """get_candles(active, seconds, count, endtime) — bootstrap()/_spec_v1_append_new_candle()
    call this exact signature. Returns enough closed candles for both TFs to pass their
    respective min_needed gates (m5 EntrySignal=22, m15 TrendFilter=53)."""

    def __init__(self):
        self.calls = []

    def get_candles(self, active, seconds, count, endtime):
        self.calls.append((active, seconds, count))
        if seconds == 900:
            return _raw_candles(60, 1_700_000_000, 900, 1.1000, 0.0002)
        return _raw_candles(30, 1_700_000_000, 300, 1.1000, 0.0002)


def _fake_bot_for_ensure(assets):
    """Duck-typed stand-in for FullTradingBot — only the attributes _ensure_candle_stores()
    itself touches (self.cfg.assets, self._candle_stores, self.iq)."""
    return types.SimpleNamespace(
        cfg=types.SimpleNamespace(assets=list(assets)),
        _candle_stores={},
        iq=_FakeIQForBootstrap(),
    )


def test_ensure_candle_stores_populates_dict_for_resolved_assets():
    bot = _fake_bot_for_ensure(["EURUSD-op", "GBPUSD-op"])
    main.FullTradingBot._ensure_candle_stores(bot)
    check("bug-181: self._candle_stores contains an entry per resolved spec_v1 asset",
          set(bot._candle_stores.keys()) == {"EURUSD-op", "GBPUSD-op"}, bot._candle_stores)
    check("bug-181: each seeded store is a real CandleStore with bootstrapped M5 candles",
          all(isinstance(s, CandleStore) and len(s.m5) > 0 for s in bot._candle_stores.values()))
    check("bug-181: each seeded store is a real CandleStore with bootstrapped M15 candles",
          all(len(s.m15) > 0 for s in bot._candle_stores.values()))


def test_ensure_candle_stores_is_idempotent_second_call_keeps_existing_store():
    bot = _fake_bot_for_ensure(["EURUSD-op"])
    main.FullTradingBot._ensure_candle_stores(bot)
    first_store = bot._candle_stores["EURUSD-op"]
    first_store.m5.append(first_store.m5[-1])  # mutate so we can detect a replace vs a keep
    marker_len = len(first_store.m5)
    main.FullTradingBot._ensure_candle_stores(bot)
    check("bug-181: calling _ensure_candle_stores() again does not replace an existing store "
          "(§4.1 — keeps history instead of re-bootstrapping)",
          bot._candle_stores["EURUSD-op"] is first_store
          and len(bot._candle_stores["EURUSD-op"].m5) == marker_len)


def test_ensure_candle_stores_noop_on_empty_assets():
    bot = _fake_bot_for_ensure([])
    main.FullTradingBot._ensure_candle_stores(bot)
    check("bug-181: _ensure_candle_stores() is a no-op when cfg.assets is empty (overnight/weekend)",
          bot._candle_stores == {})


# ─────────────────────────────────────────
# C. Functional: seeded store -> on_m5_close()/on_m15_close() bypass "no candle store" and
#    reach real (non-"n/a") diagnostic evaluation
# ─────────────────────────────────────────
class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(self.format(record))


def _capture(logger_name: str, fn):
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


def _fresh_risk_manager(suffix: str) -> RiskManager:
    state_path = f"data/_test_risk_bug181_{suffix}.json"
    snapshot_path = f"data/_test_snap_bug181_{suffix}.json"
    for p in (state_path, snapshot_path):
        if os.path.exists(p):
            os.remove(p)
    return RiskManager(RiskConfig(), state_path=state_path, snapshot_path=snapshot_path)


def _seeded_store() -> CandleStore:
    store = CandleStore("EURUSD-op", persist_dir="data/_test_candles_bug181")
    store.bootstrap(_FakeIQForBootstrap())
    return store


def _make_sm_with_real_store(store, entry_signal, trend_filter, suffix) -> BotStateMachine:
    asset = "EURUSD-op"
    return BotStateMachine(
        assets=[asset],
        candle_stores={asset: store},
        trend_filter=trend_filter,
        entry_signal=entry_signal,
        time_filter=TimeFilter(),
        risk_manager=_fresh_risk_manager(suffix),
        place_order_fn=lambda asset, direction, stake: {"id": "T1", "asset": asset, "direction": direction},
        get_balance_fn=lambda: 1000.0,
        now_fn=lambda: datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK),  # Thursday, inside 14:00-17:00 window
    )


def test_on_m15_close_bypasses_no_candle_store_once_seeded_and_computes_real_trend():
    """Real TrendFilter.evaluate() against a real bootstrapped M15 buffer — proves the
    seeded store is genuinely usable, not just present-but-empty."""
    store = _seeded_store()
    sm = _make_sm_with_real_store(store, EntrySignal(), TrendFilter(), suffix="m15real")
    lines = _capture("state_machine", lambda: sm.on_m15_close("EURUSD-op"))
    check("bug-181: on_m15_close() never logs 'no candle store' once seeded",
          not any("no candle store" in l for l in lines), lines)
    trend = sm.trend_states.get("EURUSD-op")
    check("bug-181: on_m15_close() actually computed a TrendState (not left unset)",
          trend is not None and isinstance(trend, TrendState))
    check("bug-181: computed TrendState has REAL (non-None) ema20/ema50/atr14 values — "
          "proves it reached genuine indicator math, not a stub",
          trend.ema20 is not None and trend.ema50 is not None and trend.atr14 is not None, trend)


def test_on_m5_close_bypasses_no_candle_store_once_seeded_and_reaches_and_gate_with_real_diag():
    """Monkeypatch IndicatorEngineV2.compute_m5 only (deterministic full-pass row), same
    convention as test_spec_v1_signal_logging.py — the CandleStore/on_m5_close/EntrySignal
    wiring itself is exercised for real; only the indicator MATH is stubbed for determinism."""
    store = _seeded_store()
    sm = _make_sm_with_real_store(store, EntrySignal(), TrendFilter(), suffix="m5real")
    # seed a real UPTREND so on_m5_close doesn't short-circuit at the trend gate
    sm.trend_states["EURUSD-op"] = TrendState(
        asset="EURUSD-op", status="UPTREND", ema20=1.1, ema50=1.09, atr14=0.001, computed_at="x")

    def _fake_m5_df(df):
        # entry_signal.evaluate() (called with no atr_history_100 override, exactly as
        # state_machine.on_m5_close() calls it) derives the volatility-check median from
        # df["atr14"].tail(100) — needs >=20 rows of real history, not just the 2 rows the
        # older signal-logging suite used, or the AND-gate dies at "insufficient history"
        # instead of reaching a genuine OK/FAIL vol verdict.
        cols = ["open", "high", "low", "close", "ema20", "rsi14", "atr14"]
        filler = {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "ema20": 1.0,
                  "rsi14": 50.0, "atr14": 0.001}
        prev = {"open": 1.0995, "close": 1.0990, "high": 1.0995, "low": 1.0985,
                "ema20": 1.0995, "rsi14": 50.0, "atr14": 0.001}
        row = {"open": 1.0990, "high": 1.1015, "low": 1.0985, "close": 1.1010,
               "ema20": 1.1000, "rsi14": 55.0, "atr14": 0.001}
        rows = [filler] * 23 + [prev, row]
        data = {c: [r.get(c, 0.0) for r in rows] for c in cols}
        return pd.DataFrame(data)

    orig_compute_m5 = es_mod.IndicatorEngineV2.compute_m5
    es_mod.IndicatorEngineV2.compute_m5 = staticmethod(_fake_m5_df)
    try:
        lines = _capture("state_machine", lambda: sm.on_m5_close("EURUSD-op"))
    finally:
        es_mod.IndicatorEngineV2.compute_m5 = orig_compute_m5

    check("bug-181: on_m5_close() never logs 'no candle store' once seeded", not any("no candle store" in l for l in lines), lines)
    line = next((l for l in lines if "[SPEC_V1_SIGNAL]" in l), None)
    check("bug-181: on_m5_close() reaches the AND-gate and logs real (non-'n/a') diagnostic "
          "values (pullback/pattern/rsi/vol), not the pre-fix silent 'no candle store' HOLD",
          line is not None and "pullback=OK" in line and "pattern=engulfing" in line
          and "rsi=55.0" in line and "vol=" in line and "n/a" not in line
          and "decision=ENTER" in line, line)


# ─────────────────────────────────────────
# D. Regression guard: reproduce the ORIGINAL bug on an unseeded store dict
# ─────────────────────────────────────────
def test_original_bug_reproduced_when_never_seeded():
    """Proves this suite is a real regression guard: without ever calling
    _ensure_candle_stores(), a spec_v1-resolved asset's on_m5_close()/on_m15_close() hit
    exactly the production symptom — 'no candle store' on every tick."""
    asset = "EURUSD-op"
    sm = BotStateMachine(
        assets=[asset], candle_stores={},  # <- never seeded, reproduces the bug
        trend_filter=TrendFilter(), entry_signal=EntrySignal(), time_filter=TimeFilter(),
        risk_manager=_fresh_risk_manager("unseeded"),
        place_order_fn=lambda asset, direction, stake: {"id": "T1"},
        get_balance_fn=lambda: 1000.0,
        now_fn=lambda: datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK),
    )
    sm.trend_states[asset] = TrendState(asset=asset, status="UPTREND", ema20=1.1, ema50=1.09,
                                         atr14=0.001, computed_at="x")
    lines_m5 = _capture("state_machine", lambda: sm.on_m5_close(asset))
    lines_m15 = _capture("state_machine", lambda: sm.on_m15_close(asset))
    check("bug-181 regression guard: unseeded on_m5_close() reproduces 'no candle store' "
          "(the exact production symptom — proves the suite would have caught this)",
          any("no candle store" in l for l in lines_m5), lines_m5)
    check("bug-181 regression guard: unseeded on_m15_close() reproduces "
          "'M15 trend refresh skipped — no candle store'",
          any("no candle store" in l for l in lines_m15), lines_m15)


def run_all():
    tests = [
        test_m5_loop_calls_ensure_candle_stores_before_asset_loop,
        test_m15_loop_calls_ensure_candle_stores_before_asset_loop,
        test_ensure_candle_stores_populates_dict_for_resolved_assets,
        test_ensure_candle_stores_is_idempotent_second_call_keeps_existing_store,
        test_ensure_candle_stores_noop_on_empty_assets,
        test_on_m15_close_bypasses_no_candle_store_once_seeded_and_computes_real_trend,
        test_on_m5_close_bypasses_no_candle_store_once_seeded_and_reaches_and_gate_with_real_diag,
        test_original_bug_reproduced_when_never_seeded,
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
