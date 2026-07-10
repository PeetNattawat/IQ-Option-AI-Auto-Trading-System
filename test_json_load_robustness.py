"""
test_json_load_robustness.py — regression tests for bug-XXX (production crash):

    json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
    at trading_engine.py:540 TradeManager._load_learning_rules()

Root cause: data/learning_rules.json existed on the VM but was 0 bytes. The
loader only caught FileNotFoundError, not json.JSONDecodeError, so the whole
process crashed at startup (TradeManager.__init__ -> connect() ->
systemd restart loop).

This suite proves, for every "load a JSON state file with a safe fallback"
call site in trading_engine.py / main.py:
  1. File missing            -> fallback (pre-existing, unaffected behavior)
  2. File exists but empty   -> fallback, no crash (THE BUG)
  3. File exists but corrupt -> fallback, no crash
  4. File has valid JSON     -> real value loaded (no regression)

Uses the same "mock iqoptionapi before import" convention already established
in test_spec_v1_live_wiring.py, and the custom check()/PASS-FAIL harness used
by all 4 existing suites (test_spec_v1.py, test_spec_v1_live_wiring.py,
test_risk_v2_live_sync.py, test_overhaul.py).

Run: python test_json_load_robustness.py
No network access required.
"""

import json
import os
import sys
import shutil
import tempfile
import types

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ── Mock iqoptionapi before importing trading_engine/main (same pattern as
#    test_spec_v1_live_wiring.py) so this suite has zero network dependency.
if "iqoptionapi" not in sys.modules:
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

import trading_engine  # noqa: E402
import main  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


class _TempCwd:
    """Runs a block inside a fresh temp directory (with an empty data/ subfolder)
    and restores the original cwd afterwards. All the loaders under test use
    relative paths like "data/learning_rules.json", so this is the simplest
    way to isolate each scenario without touching the real data/ directory."""

    def __enter__(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.mkdtemp(prefix="iqoption_json_test_")
        os.makedirs(os.path.join(self._tmp, "data"), exist_ok=True)
        os.chdir(self._tmp)
        return self._tmp

    def __exit__(self, *exc):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)


def _new_trade_manager():
    """TradeManager.__init__ eagerly calls _load_trades/_load_learning_rules/
    _load_step, which is exactly the code path we're testing — so we
    instantiate the real class (not object.__new__) inside the isolated cwd."""
    cfg = trading_engine.TradingConfig()
    return trading_engine.TradeManager(cfg, iq=None)


# ─────────────────────────────────────────
# 1. TradeManager._load_learning_rules()
# ─────────────────────────────────────────
def test_load_learning_rules_missing_file():
    with _TempCwd():
        tm = _new_trade_manager()
        check("_load_learning_rules: missing file -> []", tm.learning_rules == [])


def test_load_learning_rules_empty_file():
    with _TempCwd():
        with open("data/learning_rules.json", "w") as f:
            pass  # 0-byte file, exactly what broke production
        tm = _new_trade_manager()
        check("_load_learning_rules: empty (0-byte) file -> [] no crash",
              tm.learning_rules == [])


def test_load_learning_rules_corrupt_file():
    with _TempCwd():
        with open("data/learning_rules.json", "w") as f:
            f.write("{invalid")
        tm = _new_trade_manager()
        check("_load_learning_rules: malformed JSON -> [] no crash",
              tm.learning_rules == [])


def test_load_learning_rules_valid_file():
    with _TempCwd():
        rules = [{"id": "veto_call_rsi_75", "disabled": True}]
        with open("data/learning_rules.json", "w") as f:
            json.dump(rules, f)
        tm = _new_trade_manager()
        check("_load_learning_rules: valid JSON -> loaded as-is (no regression)",
              tm.learning_rules == rules, tm.learning_rules)


# ─────────────────────────────────────────
# 2. LearningEngine._merge_rules() (same file, write path — no __init__ args)
# ─────────────────────────────────────────
def test_merge_rules_corrupt_existing_file():
    with _TempCwd():
        with open("data/learning_rules.json", "w") as f:
            f.write("")  # empty, same as production bug
        le = trading_engine.LearningEngine()
        try:
            le._merge_rules([{"id": "new_rule_1", "disabled": True}])
            ok = True
        except Exception as e:
            ok = False
            print(f"        unexpected exception: {e}")
        check("_merge_rules: empty existing file -> no crash, merges fresh", ok)
        with open("data/learning_rules.json") as f:
            saved = json.load(f)
        check("_merge_rules: new rule persisted after empty-file recovery",
              any(r["id"] == "new_rule_1" for r in saved), saved)


# ─────────────────────────────────────────
# 3. TradeManager._load_step() (martingale_state.json) — was already fixed
#    pre-session; regression-guard so it can't silently regress back.
# ─────────────────────────────────────────
def test_load_step_missing_file():
    with _TempCwd():
        tm = _new_trade_manager()
        check("_load_step: missing file -> 0", tm.current_step == 0)


def test_load_step_empty_file():
    with _TempCwd():
        with open("data/martingale_state.json", "w") as f:
            pass
        tm = _new_trade_manager()
        check("_load_step: empty (0-byte) file -> 0 no crash", tm.current_step == 0)


def test_load_step_corrupt_file():
    with _TempCwd():
        with open("data/martingale_state.json", "w") as f:
            f.write("not json at all {{{")
        tm = _new_trade_manager()
        check("_load_step: malformed JSON -> 0 no crash", tm.current_step == 0)


def test_load_step_valid_file():
    with _TempCwd():
        with open("data/martingale_state.json", "w") as f:
            json.dump({"step": 3}, f)
        tm = _new_trade_manager()
        check("_load_step: valid JSON -> real value loaded (no regression)",
              tm.current_step == 3, tm.current_step)


# ─────────────────────────────────────────
# 4. TradeManager._load_trades() (trades.json) — already handled both
#    exceptions pre-session; regression-guard.
# ─────────────────────────────────────────
def test_load_trades_empty_file():
    with _TempCwd():
        with open("data/trades.json", "w") as f:
            pass
        tm = _new_trade_manager()
        check("_load_trades: empty (0-byte) file -> [] no crash", tm.trades == [])


def test_load_trades_valid_file():
    with _TempCwd():
        trades = [{"id": "t1", "status": "open"}]
        with open("data/trades.json", "w") as f:
            json.dump(trades, f)
        tm = _new_trade_manager()
        check("_load_trades: valid JSON -> loaded as-is (no regression)",
              tm.trades == trades, tm.trades)


# ─────────────────────────────────────────
# 5. main.load_runtime_config() (config.json) — already handled both
#    exceptions pre-session; regression-guard.
# ─────────────────────────────────────────
def test_load_runtime_config_missing_file():
    with _TempCwd():
        orig_path = main.RUNTIME_CONFIG_PATH
        main.RUNTIME_CONFIG_PATH = "data/config.json"
        try:
            cfg = main.load_runtime_config()
            check("load_runtime_config: missing file -> {}", cfg == {})
        finally:
            main.RUNTIME_CONFIG_PATH = orig_path


def test_load_runtime_config_empty_file():
    with _TempCwd():
        orig_path = main.RUNTIME_CONFIG_PATH
        main.RUNTIME_CONFIG_PATH = "data/config.json"
        try:
            with open("data/config.json", "w") as f:
                pass
            cfg = main.load_runtime_config()
            check("load_runtime_config: empty (0-byte) file -> {} no crash", cfg == {})
        finally:
            main.RUNTIME_CONFIG_PATH = orig_path


def test_load_runtime_config_corrupt_file():
    with _TempCwd():
        orig_path = main.RUNTIME_CONFIG_PATH
        main.RUNTIME_CONFIG_PATH = "data/config.json"
        try:
            with open("data/config.json", "w") as f:
                f.write("{not valid json")
            cfg = main.load_runtime_config()
            check("load_runtime_config: malformed JSON -> {} no crash", cfg == {})
        finally:
            main.RUNTIME_CONFIG_PATH = orig_path


def test_load_runtime_config_valid_file():
    with _TempCwd():
        orig_path = main.RUNTIME_CONFIG_PATH
        main.RUNTIME_CONFIG_PATH = "data/config.json"
        try:
            data = {"account_type": "PRACTICE", "trade_amount": 25.0}
            with open("data/config.json", "w") as f:
                json.dump(data, f)
            cfg = main.load_runtime_config()
            check("load_runtime_config: valid JSON -> loaded as-is (no regression)",
                  cfg == data, cfg)
        finally:
            main.RUNTIME_CONFIG_PATH = orig_path


if __name__ == "__main__":
    print("=" * 70)
    print("JSON load robustness — regression suite (0-byte / corrupt state files)")
    print("=" * 70)

    print("\n[1] TradeManager._load_learning_rules()")
    test_load_learning_rules_missing_file()
    test_load_learning_rules_empty_file()
    test_load_learning_rules_corrupt_file()
    test_load_learning_rules_valid_file()

    print("\n[2] TradeManager._merge_rules()")
    test_merge_rules_corrupt_existing_file()

    print("\n[3] TradeManager._load_step()")
    test_load_step_missing_file()
    test_load_step_empty_file()
    test_load_step_corrupt_file()
    test_load_step_valid_file()

    print("\n[4] TradeManager._load_trades()")
    test_load_trades_empty_file()
    test_load_trades_valid_file()

    print("\n[5] main.load_runtime_config()")
    test_load_runtime_config_missing_file()
    test_load_runtime_config_empty_file()
    test_load_runtime_config_corrupt_file()
    test_load_runtime_config_valid_file()

    print("\n" + "=" * 70)
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed (of {len(PASS) + len(FAIL)})")
    print("=" * 70)
    if FAIL:
        print("\nFAILED:")
        for name in FAIL:
            print(f"  - {name}")
        sys.exit(1)
    sys.exit(0)
