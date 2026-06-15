"""
VM-ready diagnostic: where is REAL (non-OTC) forex open RIGHT NOW?
Hang-proof — get_all_open_time() is wrapped in a thread with a hard timeout
(its internal digital probe can block forever on some api versions).
Read-only. Run on the VM:  python -u diag_open.py
"""
import os, time, threading
from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option

load_dotenv()
iq = IQ_Option(os.getenv("IQ_EMAIL"), os.getenv("IQ_PASSWORD"))
ok, reason = iq.connect()
print("connected:", ok, reason, "| account:", os.getenv("IQ_ACCOUNT", "PRACTICE"), flush=True)
iq.change_balance(os.getenv("IQ_ACCOUNT", "PRACTICE"))

majors = {"EUR", "USD", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"}
def is_fx(n):                      # any non-OTC 6-letter forex pair
    return not n.endswith("-OTC") and len(n) == 6 and n[:3].isalpha() and n[3:].isalpha()
def is_major_fx(n):                # the bot's CURRENT strict filter (majors only)
    return is_fx(n) and n[:3] in majors and n[3:] in majors

def call_with_timeout(fn, timeout=20):
    box = {}
    def run():
        try: box["v"] = fn()
        except Exception as e: box["e"] = e
    t = threading.Thread(target=run, daemon=True); t.start(); t.join(timeout)
    if t.is_alive(): return None, "TIMEOUT"
    return box.get("v"), box.get("e")

# ---- 1) init_v2 (what resolve_assets currently relies on) ----
init = None
for i in range(4):
    data, err = call_with_timeout(iq.get_all_init_v2, 25)
    if data: init = data; break
    print(f"  init_v2 attempt {i+1}: {err or 'empty'}", flush=True)
    time.sleep(1.5)

print("\n=== init_v2 binary/turbo : open NON-OTC forex ===", flush=True)
if init:
    for option in ("binary", "turbo"):
        actives = (init.get(option) or {}).get("actives") or {}
        fx_open, major_open, otc_open = [], [], []
        for aid, a in actives.items():
            clean = str(a.get("name", "")).split(".")[-1]
            is_open = a.get("enabled", True) and not a.get("is_suspended", False)
            if not is_open: continue
            if clean.endswith("-OTC"): otc_open.append(clean)
            elif is_fx(clean):
                fx_open.append(clean)
                if is_major_fx(clean): major_open.append(clean)
        print(f"  {option:7s} non-OTC fx open({len(fx_open)}): {sorted(fx_open) or 'NONE'}", flush=True)
        print(f"          of which majors-only({len(major_open)}): {sorted(major_open) or 'NONE'}  | otc open: {len(otc_open)}", flush=True)
else:
    print("  init_v2 unavailable", flush=True)

# ---- 2) get_all_open_time (authoritative 'open now'), hang-guarded ----
print("\n=== get_all_open_time : open NON-OTC forex (per kind) ===", flush=True)
ot, err = call_with_timeout(iq.get_all_open_time, 25)
if ot:
    for kind in ("binary", "turbo", "digital"):
        items = ot.get(kind) or {}
        fx_open = sorted(n for n, info in items.items() if info.get("open") and is_fx(n))
        otc_open = [n for n, info in items.items() if info.get("open") and n.endswith("-OTC")]
        print(f"  {kind:8s} non-OTC fx open({len(fx_open)}): {fx_open or 'NONE'}  | otc open: {len(otc_open)}", flush=True)
else:
    print(f"  get_all_open_time unavailable: {err}", flush=True)

# ---- 3) payout for a few real majors ----
print("\n=== payout (get_all_profit) for sample majors ===", flush=True)
profits, err = call_with_timeout(iq.get_all_profit, 20)
if profits:
    for n in ("EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "EURGBP"):
        print(f"  {n:8s} -> {profits.get(n)}", flush=True)
else:
    print(f"  get_all_profit unavailable: {err}", flush=True)

print("\nDone.", flush=True)
