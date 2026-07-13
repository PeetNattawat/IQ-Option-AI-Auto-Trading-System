"""
test_risk_v2_live_sync.py — verifies the bugfix for the gap Pixel found while fixing
bug-143: S.risk_v2 (main.py's self._risk_v2, backed by risk_manager.RiskManager) was
never fed real trade events, so open_positions / trades_today / consecutive_losses
stayed at their zero defaults forever even while the live legacy engine traded for real.

Fix: main.py now calls self._risk_v2.record_order_placed() / record_order_result()
at every place a real trade opens/closes (auto entry, manual dashboard trade, web/app
trade discovered by sync_external_trades, and every close drained via
drain_pending_alerts()). record_order_result() gained a `count_streak` kwarg so the
consecutive_losses counter mirrors legacy's own semantics exactly: trading_engine.py's
_apply_close() only ever increments/resets TradeManager.consecutive_losses for
source == "auto" — manual/web trades affect the shared P&L and open-position counters
but must NOT trip the auto-only consecutive-loss hard stop.

This file exercises RiskManager the same way main.py's new hook points do (open/close
events driven by mock trade dicts shaped exactly like trading_engine.py's real trade
dicts — same keys, same "source"/"result"/"pnl" semantics) so the assertions are a
faithful proxy for "the legacy engine opens/closes a trade -> risk_v2 state updates".
A second guard (test_main_py_wires_the_hooks) statically confirms the call sites still
exist in main.py, so a future refactor can't silently remove the wiring again.

Run: python test_risk_v2_live_sync.py
No network access required.
"""

import os
import sys
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from risk_manager import RiskConfig, RiskManager

BANGKOK = ZoneInfo("Asia/Bangkok")
PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def _fresh_risk_manager(tag: str, **overrides) -> RiskManager:
    state_path = f"data/_test_livesync_{tag}_state.json"
    snapshot_path = f"data/_test_livesync_{tag}_snap.json"
    for p in (state_path, snapshot_path):
        if os.path.exists(p):
            os.remove(p)
    cfg = RiskConfig(**overrides)
    return RiskManager(cfg, state_path=state_path, snapshot_path=snapshot_path)


def _feed(rm: RiskManager, trade: dict, now):
    """Mirrors main.py's new hook points exactly:
    open  -> record_order_placed() (any source: auto / manual / web)
    close -> record_order_result(pnl, result, now, count_streak=(source == "auto"))
    """
    rm.record_order_placed()
    rm.record_order_result(trade["pnl"], trade["result"], now,
                            count_streak=(trade.get("source") == "auto"))


# ─────────────────────────────────────────
# 1. Auto trade open -> close updates all 3 gap-report fields
# ─────────────────────────────────────────
def test_auto_open_close_updates_counters():
    rm = _fresh_risk_manager("auto_basic", max_trades_per_day=99, max_consecutive_losses=99)
    now = datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK)
    rm.roll_boundaries(now, 1000)
    before = rm.to_state_dict(balance=1000)
    check("live-sync: fresh state starts at zero (pre-fix stale defaults)",
          before["open_positions"] == 0 and before["trades_today"] == 0 and before["consecutive_losses"] == 0)

    trade = {"asset": "EURUSD-op", "source": "auto", "pnl": -5.0, "result": "LOSS"}
    _feed(rm, trade, now)
    after = rm.to_state_dict(balance=995)
    check("live-sync: open_positions increments on placed, decrements back on close",
          after["open_positions"] == 0, after)
    check("live-sync: trades_today reflects the real trade", after["trades_today"] == 1, after)
    check("live-sync: consecutive_losses reflects the real LOSS (auto source)",
          after["consecutive_losses"] == 1, after)


def test_open_position_visible_before_close():
    rm = _fresh_risk_manager("auto_open_visible", max_trades_per_day=99, max_consecutive_losses=99)
    now = datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK)
    rm.roll_boundaries(now, 1000)
    rm.record_order_placed()
    mid = rm.to_state_dict(balance=1000)
    check("live-sync: open_positions == 1 while the order is still live (not stuck at 0)",
          mid["open_positions"] == 1, mid)


# ─────────────────────────────────────────
# 2. Manual/web trades must NOT trip the auto-only consecutive-loss hard stop
#    (mirrors trading_engine.py's _apply_close: source == "auto" gate)
# ─────────────────────────────────────────
def test_manual_and_web_losses_do_not_count_streak():
    rm = _fresh_risk_manager("manual_web", max_trades_per_day=99, max_consecutive_losses=99)
    now = datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK)
    rm.roll_boundaries(now, 1000)

    _feed(rm, {"source": "manual", "pnl": -5.0, "result": "LOSS"}, now)
    _feed(rm, {"source": "web", "pnl": -5.0, "result": "LOSS"}, now)
    state = rm.to_state_dict(balance=990)
    check("live-sync: manual/web LOSS updates trades_today (legacy counts all sources)",
          state["trades_today"] == 2, state)
    check("live-sync: manual/web LOSS does NOT bump consecutive_losses (legacy is auto-only)",
          state["consecutive_losses"] == 0, state)
    check("live-sync: manual/web LOSS still reduces daily P&L (legacy today_pnl counts all sources)",
          state["daily_pnl_pct"] < 0, state)


def test_mixed_sequence_matches_legacy_semantics():
    rm = _fresh_risk_manager("mixed", max_trades_per_day=99, max_consecutive_losses=99)
    now = datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK)
    rm.roll_boundaries(now, 1000)

    _feed(rm, {"source": "auto", "pnl": 5.0, "result": "WIN"}, now)     # streak: 0
    _feed(rm, {"source": "manual", "pnl": -5.0, "result": "LOSS"}, now)  # streak: still 0 (not auto)
    _feed(rm, {"source": "web", "pnl": -5.0, "result": "LOSS"}, now)     # streak: still 0 (not auto)
    _feed(rm, {"source": "auto", "pnl": -5.0, "result": "LOSS"}, now)    # streak: 1 (first auto loss)
    state = rm.to_state_dict(balance=990)
    check("live-sync: mixed-source sequence — trades_today counts every trade",
          state["trades_today"] == 4, state)
    check("live-sync: mixed-source sequence — consecutive_losses only reflects auto-sourced losses",
          state["consecutive_losses"] == 1, state)
    check("live-sync: mixed-source sequence — open_positions settles back to 0",
          state["open_positions"] == 0, state)


# ─────────────────────────────────────────
# 3. The 4 risk_v2-exclusive fields (daily/weekly P&L%, cooldown, hard_stop) also start
#    moving once real events are fed — this was silently broken by the same root cause.
# ─────────────────────────────────────────
def test_cooldown_and_pnl_pct_now_move_with_real_trades():
    rm = _fresh_risk_manager("pnlpct", max_trades_per_day=99, max_consecutive_losses=99,
                              signal_cooldown_minutes=15)
    now = datetime(2026, 7, 9, 14, 5, tzinfo=BANGKOK)
    rm.roll_boundaries(now, 1000)  # balance_start_of_day = 1000
    _feed(rm, {"source": "auto", "pnl": -50.0, "result": "LOSS"}, now)
    state = rm.to_state_dict(balance=950)
    check("live-sync: daily_pnl_pct reflects the real -50 on a 1000 balance-start-of-day",
          abs(state["daily_pnl_pct"] - (-5.0)) < 0.01, state)
    check("live-sync: signal_cooldown_until is now set after a real loss (was always null pre-fix)",
          state["signal_cooldown_until"] is not None, state)


# ─────────────────────────────────────────
# 4. Static guard — confirm main.py still actually calls the hooks (prevents this gap
#    from silently regressing if someone refactors run_cycle / external_sync_loop later).
# ─────────────────────────────────────────
def test_main_py_wires_the_hooks():
    with open("main.py", encoding="utf-8") as f:
        src = f.read()
    placed_calls = src.count("self._risk_v2.record_order_placed()")
    result_calls = src.count("self._risk_v2.record_order_result(")
    check("live-sync: main.py calls record_order_placed() at >=3 sites (auto/manual/web open)",
          placed_calls >= 3, f"found {placed_calls}")
    check("live-sync: main.py calls record_order_result(...) at >=1 site (unified close drain)",
          result_calls >= 1, f"found {result_calls}")
    check("live-sync: record_order_result call passes count_streak keyed off trade source",
          'count_streak=(t.get("source") == "auto")' in src)


# ─────────────────────────────────────────
# 5. bug-16x — max_trades_per_day / max_consecutive_losses were missing from the field list
#    that main.py's __init__ used to build self._risk_v2's RiskConfig, so RiskConfig's
#    hardcoded dataclass defaults (5 / 3) silently governed spec_v1 forever regardless of
#    what config.json's max_trades_per_day/max_consecutive_losses said. Fixed via a single
#    RISK_V2_SYNC_FIELDS tuple used both at construction and at re-sync time.
# ─────────────────────────────────────────
import main  # noqa: E402  (import after os.chdir so main.py's relative data/ paths resolve)
from trading_engine import TradingConfig


def test_risk_v2_sync_fields_include_the_two_previously_missing_fields():
    check("bug-16x: RISK_V2_SYNC_FIELDS includes max_trades_per_day",
          "max_trades_per_day" in main.RISK_V2_SYNC_FIELDS, main.RISK_V2_SYNC_FIELDS)
    check("bug-16x: RISK_V2_SYNC_FIELDS includes max_consecutive_losses",
          "max_consecutive_losses" in main.RISK_V2_SYNC_FIELDS, main.RISK_V2_SYNC_FIELDS)


def test_construction_honors_custom_max_trades_per_day_not_the_riskconfig_default():
    """Reproduces Peet's exact production report: cfg.max_trades_per_day=50 in config.json
    must reach risk_v2, not silently stay at RiskConfig's dataclass default of 5."""
    cfg = TradingConfig(max_trades_per_day=50, max_consecutive_losses=9)
    risk_cfg = RiskConfig(**{f: getattr(cfg, f) for f in main.RISK_V2_SYNC_FIELDS})
    check("bug-16x: constructed RiskConfig.max_trades_per_day reflects cfg (50), not the default (5)",
          risk_cfg.max_trades_per_day == 50, risk_cfg)
    check("bug-16x: constructed RiskConfig.max_consecutive_losses reflects cfg (9), not the default (3)",
          risk_cfg.max_consecutive_losses == 9, risk_cfg)


def test_construction_effective_gate_uses_the_custom_value():
    """Functional proof, not just a field-equality check: can_trade() must actually allow
    a 10th trade when max_trades_per_day=50 (would have been hard-blocked at 5 pre-fix)."""
    cfg = TradingConfig(max_trades_per_day=50, max_consecutive_losses=9)
    rm = _fresh_risk_manager("construction_gate", **{f: getattr(cfg, f) for f in main.RISK_V2_SYNC_FIELDS})
    now = datetime(2026, 7, 13, 10, 0, tzinfo=BANGKOK)
    rm.roll_boundaries(now, 1000)
    for _ in range(10):
        rm.record_order_placed()
        rm.record_order_result(1.0, "WIN", now, count_streak=True)
    ok, reason = rm.can_trade(now, balance=1010)
    check("bug-16x: 10 trades done, max=50 -> can_trade() still allows more (pre-fix would block at 5)",
          ok, reason)


def test_apply_runtime_config_reload_syncs_into_risk_v2():
    """Reproduces the 2nd half of Peet's report: editing config.json + restart/reload must
    reach the ALREADY-CONSTRUCTED self._risk_v2.cfg too, not just a fresh instance.
    Exercises FullTradingBot._sync_risk_v2_config as an unbound method against a duck-typed
    stand-in (same technique test_spec_v1_live_wiring.py already uses for
    _sync_state_machine_v1) — avoids constructing a real FullTradingBot / IQ connection."""
    fake = SimpleNamespace(
        cfg=TradingConfig(),  # defaults: max_trades_per_day=20, max_consecutive_losses=4
        _risk_v2=RiskManager(RiskConfig()),  # RiskConfig defaults: 5 / 3 — deliberately stale
    )
    before = fake._risk_v2.cfg
    check("bug-16x: sanity — risk_v2 starts on RiskConfig's stale hardcoded defaults",
          before.max_trades_per_day == 5 and before.max_consecutive_losses == 3, before)

    # Simulate what apply_runtime_config() does on a config.json reload / dashboard edit:
    fake.cfg.max_trades_per_day = 50
    fake.cfg.max_consecutive_losses = 9
    main.FullTradingBot._sync_risk_v2_config(fake)

    after = fake._risk_v2.cfg
    check("bug-16x: after re-sync, risk_v2.cfg.max_trades_per_day reflects the reloaded value (50)",
          after.max_trades_per_day == 50, after)
    check("bug-16x: after re-sync, risk_v2.cfg.max_consecutive_losses reflects the reloaded value (9)",
          after.max_consecutive_losses == 9, after)


def test_main_py_calls_sync_after_update_settings_apply_runtime_config():
    """Static guard — confirms the dashboard update_settings call site actually re-syncs
    risk_v2 right after apply_runtime_config(), so this can't silently regress later."""
    with open("main.py", encoding="utf-8") as f:
        src = f.read()
    idx = src.index('elif cmd == "update_settings"')
    snippet = src[idx: idx + 1200]
    check("bug-16x: update_settings handler calls apply_runtime_config(self.cfg, settings, ...)",
          "apply_runtime_config(self.cfg, settings" in snippet)
    check("bug-16x: update_settings handler calls self._sync_risk_v2_config() right after",
          "self._sync_risk_v2_config()" in snippet)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{'='*50}\nTOTAL: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        raise SystemExit(1)
