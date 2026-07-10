"""
test_spec_v1_live_wiring.py — Titan's pre-QA pass for wiring spec_v1 into main.py's live
loop (San's Architecture Notes, outputs/10_san-iqoption-spec-v1-live-wiring.md).

Covers exactly the 6 decision points + 1 gap San flagged:
  1. Strategy switch = replace, not parallel (legacy auto-execute loop guarded)
  2. Hard safety gate (3 call sites): apply_runtime_config, switch_account, scheduler loop
  3. source="spec_v1" tag + no risk_v2 double-count on close
  4. Telegram tag ("spec_v1 (demo)") on alert_trade_open/alert_result + _src_label
  5. Logging: trades.json (free via _place_order) + SQLite via TradeLogger (structural check)
  6. Rollback: strategy_mode is a live RUNTIME_FIELDS switch; _sync_state_machine_v1()
     always (re)creates a fresh BotStateMachine on re-activation (no stale KILL carry-over)
  7. KILL/AUTO_STOP on_event wiring (previously never connected in main.py)

Static/textual assertions on main.py's source are used wherever a full live IQ Option
connection would otherwise be required (FullTradingBot subclasses TradingBot, which
opens a real broker session in .connect() — not appropriate for an offline unit test).
Functional assertions are used everywhere the logic can be exercised directly against
RiskManager / BotStateMachine / TelegramBot without a live network call.

Run: python test_spec_v1_live_wiring.py
No network access required.
"""

import asyncio
import math
import os
import sys
import types
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ── Stub out heavy network dependency before importing main.py (same pattern as
# test_overhaul.py) — main.py imports trading_engine, which imports iqoptionapi.
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

import main
from state_machine import BotStateMachine
from risk_manager import RiskConfig, RiskManager
from telegram_bot import TelegramBot, TelegramConfig
from trading_engine import TradingConfig

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
# §2 — Hard safety gate: 3 call sites, all forcing fallback to legacy
# ─────────────────────────────────────────
def test_safety_gate_forces_fallback_on_non_practice():
    cfg = TradingConfig(strategy_mode="spec_v1", account_type="REAL")
    tripped = main._enforce_spec_v1_practice_gate(cfg)
    check("safety-gate: trips when spec_v1 + REAL", tripped)
    check("safety-gate: forces strategy_mode back to legacy", cfg.strategy_mode == "legacy",
          cfg.strategy_mode)


def test_safety_gate_noop_on_practice():
    cfg = TradingConfig(strategy_mode="spec_v1", account_type="PRACTICE")
    tripped = main._enforce_spec_v1_practice_gate(cfg)
    check("safety-gate: does NOT trip on PRACTICE", not tripped)
    check("safety-gate: strategy_mode left untouched on PRACTICE", cfg.strategy_mode == "spec_v1",
          cfg.strategy_mode)


def test_safety_gate_noop_on_legacy_mode():
    cfg = TradingConfig(strategy_mode="legacy", account_type="REAL")
    tripped = main._enforce_spec_v1_practice_gate(cfg)
    check("safety-gate: does not trip when strategy_mode is already legacy", not tripped)


def test_safety_gate_call_site_1_apply_runtime_config():
    idx = MAIN_SRC.index("def apply_runtime_config(")
    end = MAIN_SRC.index("\n\n\n", idx)  # next top-level blank-blank separator
    body = MAIN_SRC[idx:end]
    check("safety-gate site 1/3: apply_runtime_config() calls the gate",
          "_enforce_spec_v1_practice_gate(cfg, tg, trade_logger)" in body)


def test_safety_gate_call_site_2_switch_account():
    idx = MAIN_SRC.index('elif cmd == "switch_account":')
    idx2 = MAIN_SRC.index('elif cmd ==', idx + 10)  # next command branch = end of this one
    body = MAIN_SRC[idx:idx2]
    check("safety-gate site 2/3: switch_account handler calls the gate (San's gap finding)",
          "_enforce_spec_v1_practice_gate(self.cfg, self.tg, self._trade_logger)" in body, body)


def test_safety_gate_call_site_3_scheduler_loops():
    count = MAIN_SRC.count(
        "_enforce_spec_v1_practice_gate(self.cfg, self.tg, self._trade_logger)")
    # site 2 (switch_account) + site 3 x2 (m5 loop, m15 loop) == 3 occurrences of this exact form
    check("safety-gate site 3/3: called from spec_v1_m5_loop AND spec_v1_m15_loop "
          "(belt-and-suspenders, defense-in-depth)", count >= 3, f"found {count}")


def test_safety_gate_never_crashes_on_trip():
    """Explicit non-negotiable from the brief: force-fallback + log, never crash/throw."""
    cfg = TradingConfig(strategy_mode="spec_v1", account_type="REAL")
    try:
        main._enforce_spec_v1_practice_gate(cfg)
        ok = True
    except Exception as e:
        ok = False
        print(f"        unexpected exception: {e}")
    check("safety-gate: never raises on trip", ok)


def test_safety_gate_scheduler_loop_rechecks_gate_per_asset_not_once_per_tick():
    """bug-155 (Iris, CRITICAL) static regression guard: the per-asset `for` loop body must
    recheck the gate/mode immediately before EVERY on_m5_close()/on_m15_close() call, not
    just once at the top of the tick. Without this, a switch_account(REAL) landing mid-tick
    (during the `await self._spec_v1_append_new_candle(...)` yield point) can still let a
    broker order fire for a later asset in the same tick, even though the gate already
    tripped and forced strategy_mode back to 'legacy'. See adversarial_race_test.py /
    adversarial_race_test_post_fix.py and outputs/12_iris-iqoption-spec-v1-live-wiring-qa.md."""
    idx5 = MAIN_SRC.index("async def spec_v1_m5_loop(self):")
    idx5_end = MAIN_SRC.index("async def spec_v1_m15_loop(self):", idx5)
    body5 = MAIN_SRC[idx5:idx5_end]
    for_idx5 = body5.index("for asset in self.cfg.assets:")
    per_asset_body5 = body5[for_idx5:]
    check("bug-155: spec_v1_m5_loop's per-asset loop rechecks strategy_mode before on_m5_close()",
          'self.cfg.strategy_mode != "spec_v1"' in per_asset_body5)
    check("bug-155: spec_v1_m5_loop's per-asset loop rechecks the gate before on_m5_close()",
          per_asset_body5.count("_enforce_spec_v1_practice_gate(self.cfg, self.tg, self._trade_logger)") >= 1)
    check("bug-155: spec_v1_m5_loop aborts (break) the remaining assets on a mid-tick trip, "
          "not skip-and-continue",
          "break" in per_asset_body5.split("on_m5_close(asset)")[0])

    idx15_end = MAIN_SRC.index("def build_martingale_warning", idx5_end)
    body15 = MAIN_SRC[idx5_end:idx15_end]
    for_idx15 = body15.index("for asset in self.cfg.assets:")
    per_asset_body15 = body15[for_idx15:]
    check("bug-155: spec_v1_m15_loop's per-asset loop rechecks strategy_mode before on_m15_close()",
          'self.cfg.strategy_mode != "spec_v1"' in per_asset_body15)
    check("bug-155: spec_v1_m15_loop's per-asset loop rechecks the gate before on_m15_close()",
          per_asset_body15.count("_enforce_spec_v1_practice_gate(self.cfg, self.tg, self._trade_logger)") >= 1)
    check("bug-155: spec_v1_m15_loop aborts (break) the remaining assets on a mid-tick trip",
          "break" in per_asset_body15.split("on_m15_close(asset)")[0])


def test_safety_gate_scheduler_loop_survives_self_healed_strategy_mode():
    """bug-155 regression (functional, not just static): after a gate trip, cfg.strategy_mode
    is forced to 'legacy' by _enforce_spec_v1_practice_gate() itself — so calling the gate a
    SECOND time returns False (its condition requires strategy_mode=="spec_v1"). A fix that
    only reruns the gate call (without independently rechecking strategy_mode) would silently
    let a second, already-fallen-back tick continue placing orders. This must recheck
    strategy_mode independently, not just the gate's return value."""
    cfg = TradingConfig(strategy_mode="spec_v1", account_type="PRACTICE")
    tripped_once = main._enforce_spec_v1_practice_gate(cfg)
    check("bug-155 precondition: gate does not trip while still PRACTICE", not tripped_once)
    cfg.account_type = "REAL"
    tripped_first_call = main._enforce_spec_v1_practice_gate(cfg)
    check("bug-155: first gate call after account flips to REAL trips and self-heals mode",
          tripped_first_call and cfg.strategy_mode == "legacy")
    # The critical assertion: a second call to the SAME gate function, with strategy_mode
    # already self-healed to "legacy", returns False — proving that a per-asset recheck must
    # ALSO test `cfg.strategy_mode != "spec_v1"` directly, not rely on the gate's return value
    # alone to detect "this tick should stop".
    tripped_second_call = main._enforce_spec_v1_practice_gate(cfg)
    check("bug-155: gate alone canNOT detect an already-self-healed trip on a second call "
          "(this is exactly why the per-asset recheck must ALSO test strategy_mode directly)",
          not tripped_second_call)
    check("bug-155: independently, strategy_mode itself correctly reflects the fallback",
          cfg.strategy_mode == "legacy")


async def _run_adversarial_race_scenario(interleave_on_asset_index: int):
    """Faithful reproduction of the FIXED spec_v1_m5_loop() body (post bug-155 fix), reused
    by two regression tests below. Fires a simulated switch_account(REAL) mid-await on the
    asset at `interleave_on_asset_index`, then asserts no order reaches the broker on/after
    that asset once the account has flipped to REAL."""
    calls = {"orders": 0, "account_type_at_order_time": []}

    def place_order_fn(asset, direction, stake):
        calls["orders"] += 1
        calls["account_type_at_order_time"].append(cfg.account_type)
        return {"id": f"ORDER-{calls['orders']}", "asset": asset, "direction": direction, "amount": stake}

    cfg = TradingConfig(strategy_mode="spec_v1", account_type="PRACTICE",
                         assets=["EURUSD-op", "GBPUSD-op"])

    class _FakeSM:
        def __init__(self, place_order_fn=None, **kwargs):
            self.place_order_fn = place_order_fn
            self.global_state = "RUNNING"

        def on_m5_close(self, asset):
            return self.place_order_fn(asset, "CALL", 10.0)

    state_path = f"data/_test_race_{interleave_on_asset_index}_state.json"
    snap_path = f"data/_test_race_{interleave_on_asset_index}_snap.json"
    fake = SimpleNamespace(
        cfg=cfg, tg=None, _trade_logger=None,
        _risk_v2=RiskManager(RiskConfig(), state_path=state_path, snapshot_path=snap_path),
        _state_machine_v1=None, _spec_v1_was_active=False,
        _candle_stores={}, _trend_filter_v1=None, _entry_signal_v1=None, _time_filter_v1=None,
        _spec_v1_place_order=place_order_fn, _spec_v1_on_event=lambda *a: None,
    )
    real_bsm = main.BotStateMachine
    main.BotStateMachine = _FakeSM
    try:
        main.FullTradingBot._sync_state_machine_v1(fake)
    finally:
        main.BotStateMachine = real_bsm

    seen = {"n": 0}

    async def racing_append_new_candle(asset, tf):
        if seen["n"] == interleave_on_asset_index:
            fake.cfg.account_type = "REAL"  # what switch_account's handler does first
            main._enforce_spec_v1_practice_gate(fake.cfg, fake.tg, fake._trade_logger)
        seen["n"] += 1
        await asyncio.sleep(0)

    fake._spec_v1_append_new_candle = racing_append_new_candle

    # verbatim control flow of the FIXED spec_v1_m5_loop() per-asset body
    for asset in fake.cfg.assets:
        await fake._spec_v1_append_new_candle(asset, "m5")
        if (fake.cfg.strategy_mode != "spec_v1"
                or main._enforce_spec_v1_practice_gate(fake.cfg, fake.tg, fake._trade_logger)):
            break
        fake._state_machine_v1.on_m5_close(asset)

    for p in (state_path, snap_path):
        if os.path.exists(p):
            os.remove(p)
    return calls


def test_bug155_adversarial_race_no_order_placed_on_real_after_mid_tick_switch():
    """bug-155 (Iris, CRITICAL) — the exact adversarial scenario Iris proved with a runnable
    script: switch_account(REAL) lands during asset 1's in-flight `await`. Must result in
    ZERO orders placed with account_type=='REAL'."""
    calls = asyncio.run(_run_adversarial_race_scenario(interleave_on_asset_index=0))
    check("bug-155 adversarial: zero orders placed once account_type flips to REAL mid-tick",
          calls["orders"] == 0, calls)
    check("bug-155 adversarial: no 'REAL' ever recorded at order-placement time",
          "REAL" not in calls["account_type_at_order_time"], calls["account_type_at_order_time"])


def test_bug155_legit_practice_trades_still_fire_when_no_race_occurs():
    """Regression guard for over-blocking: the bug-155 fix must not block legitimate PRACTICE
    trades when no account switch ever happens mid-tick."""
    calls = asyncio.run(_run_adversarial_race_scenario(interleave_on_asset_index=99))  # never fires
    check("bug-155 fix does not over-block: both PRACTICE assets still place orders normally",
          calls["orders"] == 2, calls)
    check("bug-155 fix does not over-block: account_type stays PRACTICE throughout",
          calls["account_type_at_order_time"] == ["PRACTICE", "PRACTICE"], calls)


# ─────────────────────────────────────────
# §1 — legacy auto-execute loop is guarded by strategy_mode
# ─────────────────────────────────────────
def test_legacy_execute_loop_guarded_by_strategy_mode():
    idx = MAIN_SRC.index("candidates.sort(key=lambda s: s.confidence, reverse=True)")
    idx_end = MAIN_SRC.index("# Summarize the decision", idx)
    block = MAIN_SRC[idx:idx_end]
    check('legacy §1: auto-execute loop is wrapped in `if self.cfg.strategy_mode != "spec_v1":`',
          'if self.cfg.strategy_mode != "spec_v1":' in block)
    check("legacy §1: execute_trade() call still lives inside that guarded block",
          "self.trade_manager.execute_trade" in block)


# ─────────────────────────────────────────
# §3/§5 — no risk_v2 double-count on spec_v1 close; SQLite + trades.json both written
# ─────────────────────────────────────────
def test_spec_v1_close_branch_structurally_exclusive():
    marker = 'if t.get("source") == "spec_v1":'
    idx = MAIN_SRC.index(marker)
    branch = MAIN_SRC[idx: idx + 900]
    if_block, sep, else_block = branch.partition("\n                    else:")
    check("no-double-count: `else:` branch exists immediately after the spec_v1 branch", sep != "")
    check("no-double-count: spec_v1 branch calls on_trade_closed()",
          "on_trade_closed(" in if_block, if_block)
    check("no-double-count: spec_v1 branch logs to SQLite via _spec_v1_log_trade_to_sqlite",
          "_spec_v1_log_trade_to_sqlite(t)" in if_block, if_block)
    check("no-double-count: spec_v1 branch NEVER calls self._risk_v2.record_order_result "
          "directly (BotStateMachine.on_trade_closed already does so internally)",
          "record_order_result" not in if_block, if_block)
    check("no-double-count: else branch (legacy/manual/web) DOES call record_order_result",
          "record_order_result" in else_block, else_block[:300])


def test_spec_v1_place_order_uses_tag_not_auto():
    idx = MAIN_SRC.index("def _spec_v1_place_order(")
    end = MAIN_SRC.index("\n    def ", idx + 10)
    body = MAIN_SRC[idx:end]
    check('spec_v1 order tag: meta uses source="spec_v1" (not "auto")',
          '"source": "spec_v1"' in body, body)
    check("spec_v1 order tag: routes through TradeManager._place_order (no new broker call)",
          "self.trade_manager._place_order(" in body)


def test_spec_v1_close_path_does_not_double_count_pnl():
    """Functional proxy: on_trade_closed() is the ONLY thing main.py's spec_v1 branch calls
    (per the structural test above) — verify it applies pnl exactly once to the shared
    RiskManager, mirroring the exact call sequence main.py performs."""
    state_path = "data/_test_wiring_risk_state.json"
    snap_path = "data/_test_wiring_risk_snap.json"
    for p in (state_path, snap_path):
        if os.path.exists(p):
            os.remove(p)
    rm = RiskManager(RiskConfig(max_trades_per_day=99, max_consecutive_losses=99),
                      state_path=state_path, snapshot_path=snap_path)
    now = datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK)
    rm.roll_boundaries(now, 1000)
    sm = BotStateMachine(
        assets=["EURUSD-op"], candle_stores={}, trend_filter=None, entry_signal=None,
        time_filter=None, risk_manager=rm, place_order_fn=lambda *a: None,
        get_balance_fn=lambda: 1000, now_fn=lambda: now,
    )
    rm.record_order_placed()
    sm.on_trade_closed("EURUSD-op", -20.0, "LOSS")   # exactly what the spec_v1 branch calls
    state = rm.to_state_dict(balance=980)
    check("no-double-count (functional): exactly one -20 applied to daily_pnl (-2.0%)",
          math.isclose(state["daily_pnl_pct"], -2.0), state)
    for p in (state_path, snap_path):
        if os.path.exists(p):
            os.remove(p)


# ─────────────────────────────────────────
# §4 — Telegram tag
# ─────────────────────────────────────────
class _CapturingTelegramBot(TelegramBot):
    def __init__(self):
        super().__init__(TelegramConfig(bot_token="", chat_id="", enabled=False))
        self.captured = None

    async def send(self, text):
        self.captured = text
        return True


def test_telegram_alert_trade_open_tags_spec_v1():
    bot = _CapturingTelegramBot()
    trade = {"asset": "EURUSD-op", "direction": "CALL", "amount": 50, "confidence": None,
              "expiry": 15, "source": "spec_v1"}
    asyncio.run(bot.alert_trade_open(trade))
    check("telegram §4: alert_trade_open tags spec_v1 trades with 'spec_v1 (demo)'",
          "spec_v1 (demo)" in (bot.captured or ""), bot.captured)


def test_telegram_alert_trade_open_no_tag_for_auto():
    bot = _CapturingTelegramBot()
    trade = {"asset": "EURUSD-op", "direction": "CALL", "amount": 50, "confidence": 80,
              "expiry": 15, "source": "auto"}
    asyncio.run(bot.alert_trade_open(trade))
    check("telegram §4: legacy auto trades are NOT tagged spec_v1 (no false-positive tag)",
          "spec_v1 (demo)" not in (bot.captured or ""), bot.captured)


def test_telegram_alert_result_tags_spec_v1():
    bot = _CapturingTelegramBot()
    trade = {"asset": "EURUSD-op", "direction": "CALL", "amount": 50, "pnl": 10,
              "result": "WIN", "source": "spec_v1"}
    asyncio.run(bot.alert_result(trade, {}))
    check("telegram §4: alert_result tags spec_v1 trades with 'spec_v1 (demo)'",
          "spec_v1 (demo)" in (bot.captured or ""), bot.captured)


def test_telegram_src_label_distinguishes_spec_v1():
    check("telegram §4: _src_label('spec_v1') differs from _src_label('manual')",
          TelegramBot._src_label("spec_v1") != TelegramBot._src_label("manual"))
    check("telegram §4: _src_label('spec_v1') differs from _src_label('auto')",
          TelegramBot._src_label("spec_v1") != TelegramBot._src_label("auto"))


# ─────────────────────────────────────────
# §7 — KILL / AUTO_STOP on_event wiring (San's gap finding: main.py never passed on_event)
# ─────────────────────────────────────────
def test_main_py_wires_on_event_to_risk_v2_and_state_machine():
    check("§7: self._risk_v2.on_event is wired in __init__",
          "self._risk_v2.on_event = self._spec_v1_on_event" in MAIN_SRC)
    check("§7: BotStateMachine(...) is constructed with on_event=self._spec_v1_on_event",
          "on_event=self._spec_v1_on_event" in MAIN_SRC)
    check("§7: _spec_v1_on_event() audit-logs every event to SQLite system_events",
          "self._trade_logger.write_system_event(ts, event_type, detail)" in MAIN_SRC)
    check("§7: _spec_v1_on_event() alerts Telegram on KILL",
          '"🛑 <b>spec_v1 KILLED</b>' in MAIN_SRC)
    check("§7: _spec_v1_on_event() alerts Telegram on AUTO_STOP_TRIGGERED",
          "AUTO_STOP_TRIGGERED" in MAIN_SRC and "spec_v1 AUTO-STOP triggered" in MAIN_SRC)


def test_kill_event_fires_via_callback_after_3_process_errors():
    events = []
    rm = RiskManager(RiskConfig(), state_path="data/_test_wiring_kill_state.json",
                      snapshot_path="data/_test_wiring_kill_snap.json")
    sm = BotStateMachine(
        assets=["EURUSD-op"], candle_stores={}, trend_filter=None, entry_signal=None,
        time_filter=None, risk_manager=rm, place_order_fn=lambda *a: None,
        get_balance_fn=lambda: 1000, on_event=lambda et, d: events.append((et, d)),
    )
    for _ in range(3):
        sm.record_process_error("simulated broker error")
    check("§7: KILL event fires via on_event callback after 3 consecutive process errors",
          any(et == "KILL" for et, _ in events), events)
    check("§7: global_state is KILLED after the callback fires", sm.global_state == "KILLED")
    for p in ("data/_test_wiring_kill_state.json", "data/_test_wiring_kill_snap.json"):
        if os.path.exists(p):
            os.remove(p)


def test_auto_stop_event_fires_via_callback():
    events = []
    state_path, snap_path = "data/_test_wiring_autostop_state.json", "data/_test_wiring_autostop_snap.json"
    for p in (state_path, snap_path):
        if os.path.exists(p):
            os.remove(p)
    rm = RiskManager(RiskConfig(auto_stop_enabled=True, auto_stop_drawdown_pct=30.0),
                      state_path=state_path, snapshot_path=snap_path,
                      on_event=lambda et, d: events.append((et, d)))
    now = datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK)
    rm.roll_boundaries(now, 1000)   # seeds equity_baseline = 1000
    rm.can_trade(now, balance=650)  # -35% drawdown -> past 30% threshold
    check("§7: AUTO_STOP_TRIGGERED fires via on_event callback past drawdown threshold",
          any(et == "AUTO_STOP_TRIGGERED" for et, _ in events), events)
    for p in (state_path, snap_path):
        if os.path.exists(p):
            os.remove(p)


def test_kill_state_blocks_further_order_placement():
    """KILL must stop BOTH engines from opening new orders — spec_v1's own on_m5_close()
    already short-circuits to a no-op once global_state == 'KILLED' (state_machine.py), and
    legacy is already gated off entirely whenever strategy_mode == 'spec_v1' (§1). This test
    exercises the spec_v1 half of that guarantee directly."""
    calls = {"n": 0}

    def _counting_place_order(asset, side, stake):
        calls["n"] += 1
        return {"id": "SHOULD_NOT_HAPPEN"}

    rm = RiskManager(RiskConfig(), state_path="data/_test_wiring_kill2_state.json",
                      snapshot_path="data/_test_wiring_kill2_snap.json")
    sm = BotStateMachine(
        assets=["EURUSD-op"], candle_stores={}, trend_filter=None, entry_signal=None,
        time_filter=None, risk_manager=rm, place_order_fn=_counting_place_order,
        get_balance_fn=lambda: 1000,
    )
    sm.global_state = "KILLED"
    result = sm.on_m5_close("EURUSD-op")
    check("§7: on_m5_close() is a no-op once KILLED — broker never called", calls["n"] == 0)
    check("§7: on_m5_close() returns the asset's (unchanged) state, not an exception",
          result is sm.states["EURUSD-op"])
    for p in ("data/_test_wiring_kill2_state.json", "data/_test_wiring_kill2_snap.json"):
        if os.path.exists(p):
            os.remove(p)


# ─────────────────────────────────────────
# §6 — Rollback: live strategy_mode switch never carries over stale KILLED state
# ─────────────────────────────────────────
def _fake_bot_for_sync(strategy_mode: str, was_active: bool, existing_sm=None):
    """Duck-typed stand-in exercising FullTradingBot._sync_state_machine_v1 as an unbound
    method — avoids constructing a real FullTradingBot (which subclasses TradingBot and
    would otherwise require a live IQ Option connection in __init__/connect())."""
    fake = SimpleNamespace(
        cfg=SimpleNamespace(strategy_mode=strategy_mode, assets=["EURUSD-op"]),
        _spec_v1_was_active=was_active,
        _state_machine_v1=existing_sm,
        _candle_stores={},
        _trend_filter_v1=None,
        _entry_signal_v1=None,
        _time_filter_v1=None,
        _risk_v2=RiskManager(RiskConfig(), state_path="data/_test_wiring_sync_state.json",
                              snapshot_path="data/_test_wiring_sync_snap.json"),
        _spec_v1_place_order=lambda *a: None,
        _spec_v1_on_event=lambda *a: None,
    )
    return fake


def test_sync_state_machine_creates_instance_on_activation():
    fake = _fake_bot_for_sync("spec_v1", was_active=False, existing_sm=None)
    main.FullTradingBot._sync_state_machine_v1(fake)
    check("rollback §6: activating spec_v1 creates a BotStateMachine instance",
          fake._state_machine_v1 is not None)
    check("rollback §6: freshly created instance starts RUNNING (not stale KILLED)",
          fake._state_machine_v1.global_state == "RUNNING")
    check("rollback §6: _spec_v1_was_active flag flips True after activation",
          fake._spec_v1_was_active is True)


def test_sync_state_machine_does_not_recreate_while_still_active():
    fake = _fake_bot_for_sync("spec_v1", was_active=False, existing_sm=None)
    main.FullTradingBot._sync_state_machine_v1(fake)
    first_instance = fake._state_machine_v1
    # simulate a KILL happening mid-run
    first_instance.global_state = "KILLED"
    main.FullTradingBot._sync_state_machine_v1(fake)  # still spec_v1, still "active" -> no recreate
    check("rollback §6: instance is NOT recreated on every tick while still active "
          "(state persists mid-run, e.g. KILLED stays KILLED until an actual mode switch)",
          fake._state_machine_v1 is first_instance and fake._state_machine_v1.global_state == "KILLED")


def test_sync_state_machine_recreates_fresh_after_legacy_then_spec_v1_again():
    """The actual rollback scenario: spec_v1 KILLs -> Peet switches to legacy -> Peet switches
    back to spec_v1 later. The new activation must NOT carry over the old KILLED state."""
    fake = _fake_bot_for_sync("spec_v1", was_active=False, existing_sm=None)
    main.FullTradingBot._sync_state_machine_v1(fake)
    fake._state_machine_v1.global_state = "KILLED"
    fake._state_machine_v1.error_streak = 3

    # switch to legacy — was_active flips False, no new instance created (want_active=False)
    fake.cfg.strategy_mode = "legacy"
    main.FullTradingBot._sync_state_machine_v1(fake)
    check("rollback §6: switching to legacy flips was_active=False (no instance change)",
          fake._spec_v1_was_active is False)

    # switch back to spec_v1 — must create a brand-new instance, not reuse the KILLED one
    fake.cfg.strategy_mode = "spec_v1"
    main.FullTradingBot._sync_state_machine_v1(fake)
    check("rollback §6: re-activating spec_v1 after legacy creates a FRESH instance",
          fake._state_machine_v1.global_state == "RUNNING", fake._state_machine_v1.global_state)
    check("rollback §6: fresh instance has error_streak reset to 0 (no stale KILL carry-over)",
          fake._state_machine_v1.error_streak == 0)
    for p in ("data/_test_wiring_sync_state.json", "data/_test_wiring_sync_snap.json"):
        if os.path.exists(p):
            os.remove(p)


# ─────────────────────────────────────────
# Event-loop wiring — on_m5_close/on_m15_close called from a real M5/M15 scheduler,
# not ad-hoc polling (structural check: main.py owns dedicated loops, not a shared
# arbitrary-interval poll)
# ─────────────────────────────────────────
def test_dedicated_m5_m15_scheduler_loops_exist():
    check("scheduler: spec_v1_m5_loop() exists and calls on_m5_close",
          "async def spec_v1_m5_loop(self):" in MAIN_SRC and "on_m5_close(asset)" in MAIN_SRC)
    check("scheduler: spec_v1_m15_loop() exists and calls on_m15_close",
          "async def spec_v1_m15_loop(self):" in MAIN_SRC and "on_m15_close(asset)" in MAIN_SRC)
    check("scheduler: M5 loop aligns to 300s candle close (same pattern as legacy main_loop)",
          "wait = 300 - (now % 300) + 8" in MAIN_SRC)
    check("scheduler: M15 loop aligns to 900s candle close", "wait = 900 - (now % 900) + 8" in MAIN_SRC)
    check("scheduler: both loops are wired into main()'s asyncio.gather()",
          "bot.spec_v1_m5_loop()" in MAIN_SRC and "bot.spec_v1_m15_loop()" in MAIN_SRC)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{'='*50}\nTOTAL: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        raise SystemExit(1)
