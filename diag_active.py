"""
Diagnostic: resolve what active_id 1880 really is, straight from IQ Option's
LIVE server data (not the outdated static library table).
Run on the VM:  python3 diag_active.py
"""
import os
from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option

load_dotenv()
iq = IQ_Option(os.getenv("IQ_EMAIL"), os.getenv("IQ_PASSWORD"))
ok, reason = iq.connect()
print("connected:", ok, reason)
iq.change_balance(os.getenv("IQ_ACCOUNT", "PRACTICE"))

TARGET = 1880

# ── 1) Live instrument list from the server (binary + turbo) ──
try:
    init = iq.get_all_init_v2()
except Exception as e:
    init = None
    print("get_all_init_v2 failed:", e)

if init:
    for kind in ("binary", "turbo"):
        actives = (init.get(kind) or {}).get("actives") or {}
        for aid, info in actives.items():
            if str(aid) == str(TARGET):
                name = info.get("name")          # e.g. "front.GBPNZD"
                ticker = info.get("ticker")
                is_otc = info.get("is_otc")
                susp = info.get("is_suspended")
                print(f"\n[FOUND in {kind}] id={aid}")
                print(f"  name      = {name}")
                print(f"  ticker    = {ticker}")
                print(f"  is_otc    = {is_otc}")
                print(f"  suspended = {susp}")

# ── 2) Cross-check via open_time: is this pair listed as -OTC? ──
print("\n── get_all_open_time: pairs containing GBPNZD/1880 ──")
ot = iq.get_all_open_time()
for kind in ("binary", "turbo"):
    for n, i in (ot.get(kind) or {}).items():
        if "GBPNZD" in n.upper():
            print(f"  {kind}: {n:15s} open={i.get('open')}")

# ── 3) What real (non-OTC) forex IS open right now ──
print("\n── real (non-OTC) forex open right now ──")
for kind in ("binary", "turbo"):
    real = [n for n, i in (ot.get(kind) or {}).items()
            if i.get("open") and not n.endswith("-OTC") and len(n) == 6]
    print(f"  {kind}: {real}")
