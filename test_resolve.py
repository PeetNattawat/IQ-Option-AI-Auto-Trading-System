"""
Local test harness: run the REAL TradingBot.resolve_assets() end-to-end.
Connects, resolves tradable assets (binary/turbo/digital), prints what it picked,
then tries to fetch candles for the top pick. NO trades are placed.
Run:  python -u test_resolve.py
"""
import os, time
from dotenv import load_dotenv
from trading_engine import TradingConfig, TradingBot

load_dotenv()
cfg = TradingConfig()
print(f"account={cfg.account_type} trade_digital={cfg.trade_digital} min_payout={cfg.min_payout}", flush=True)

bot = TradingBot(cfg)
if not bot.connect():
    raise SystemExit("connect failed")

print("\n--- resolve_assets() ---", flush=True)
t0 = time.time()
bot.resolve_assets()
print(f"resolved in {time.time()-t0:.1f}s", flush=True)
print("assets    :", cfg.assets, flush=True)
print("asset_kind:", cfg.asset_kind, flush=True)
print("resolved? :", bot._assets_resolved, flush=True)

# sanity: can we pull candles for the top pick? (proves the name->id patch works)
if cfg.assets:
    top = cfg.assets[0]
    print(f"\n--- get_candles({top}) [{cfg.asset_kind.get(top)}] ---", flush=True)
    try:
        df = bot.get_candles(top)
        if df is None or len(df) == 0:
            print("  candles: NONE (id patch / market issue)", flush=True)
        else:
            print(f"  candles OK: {len(df)} rows, last close={df['close'].iloc[-1]}", flush=True)
    except Exception as e:
        print(f"  candles error: {e}", flush=True)
else:
    print("\nNo real (non-OTC) assets resolved — nothing to fetch.", flush=True)

print("\nDone (no trades placed).", flush=True)
