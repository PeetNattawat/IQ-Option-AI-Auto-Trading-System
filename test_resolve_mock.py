"""
Offline unit test for resolve_assets() — no network. Feeds a FAKE IQ client whose
get_digital_underlying_list_data() returns exactly the shape the real server sends on
the VM, then asserts: real (non-OTC) forex is picked as 'digital', OTC is excluded,
and binary/turbo real FX (closed) is ignored. Proves the selection + no-OTC logic.
Run:  python test_resolve_mock.py
"""
import time
from trading_engine import TradingConfig, TradingBot

NOW = time.time()
OPEN = [{"open": NOW - 3600, "close": NOW + 3600}]   # a window covering 'now'
SHUT = [{"open": NOW - 7200, "close": NOW - 3600}]   # a past window -> closed


class FakeIQ:
    def get_all_init_v2(self):
        # binary/turbo: only OTC open + a real pair that is CLOSED here (mirrors live)
        actives = {
            "1": {"name": "front.EURUSD-OTC", "enabled": True, "is_suspended": False},
            "2": {"name": "front.EURUSD",     "enabled": True, "is_suspended": True},   # real but closed on binary
        }
        return {"binary": {"actives": actives}, "turbo": {"actives": {}}}

    def get_digital_underlying_list_data(self):
        return {"underlying": [
            {"underlying": "EURUSD",     "active_id": 1,  "schedule": OPEN},   # real, open  -> pick
            {"underlying": "GBPUSD",     "active_id": 2,  "schedule": OPEN},   # real, open  -> pick
            {"underlying": "USDTRY",     "active_id": 3,  "schedule": OPEN},   # real exotic, open -> pick (non-major now allowed)
            {"underlying": "EURUSD-OTC", "active_id": 4,  "schedule": OPEN},   # OTC, open   -> EXCLUDE
            {"underlying": "AUDUSD",     "active_id": 5,  "schedule": SHUT},   # real, closed-> skip
        ]}

    def get_all_profit(self):
        return {}

    def get_digital_payout(self, name, seconds=0):
        return {"EURUSD": 88, "GBPUSD": 84, "USDTRY": 91}.get(name, 0)


cfg = TradingConfig(min_payout=0.70, max_assets=12)
bot = TradingBot(cfg)
bot.iq = FakeIQ()
bot.trade_manager = None

bot.resolve_assets()

print("assets     :", cfg.assets)
print("asset_kind :", cfg.asset_kind)

picked = set(cfg.assets)
assert "EURUSD" in picked and "GBPUSD" in picked and "USDTRY" in picked, "should pick open real FX"
assert not any(a.endswith("-OTC") for a in picked), "must NEVER pick OTC"
assert "AUDUSD" not in picked, "closed pair must be skipped"
assert all(cfg.asset_kind[a] == "digital" for a in picked), "real FX should resolve as digital here"
# ranked by digital payout desc: USDTRY(91) > EURUSD(88) > GBPUSD(84)
assert cfg.assets == ["USDTRY", "EURUSD", "GBPUSD"], f"payout ranking wrong: {cfg.assets}"

print("\nALL ASSERTIONS PASSED ✓  (no-OTC respected, digital pairs selected & ranked)")
