"""
Where is REAL (non-OTC) forex actually tradable right now?
Checks binary / turbo / digital + payout for a few real majors.
Run on the VM:  python3 diag_where.py
"""
import os, time
from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option

load_dotenv()
iq = IQ_Option(os.getenv("IQ_EMAIL"), os.getenv("IQ_PASSWORD"))
ok, reason = iq.connect()
print("connected:", ok, reason, "| account:", os.getenv("IQ_ACCOUNT", "PRACTICE"))
iq.change_balance(os.getenv("IQ_ACCOUNT", "PRACTICE"))

TARGETS = ["AUDUSD", "EURUSD", "GBPNZD", "GBPUSD"]

# 1) init_v2 enabled/suspended per binary/turbo
print("\n=== init_v2 (binary/turbo) enabled/suspended ===")
init = iq.get_all_init_v2()
for option in ("binary", "turbo"):
    actives = (init.get(option) or {}).get("actives") or {}
    for aid, a in actives.items():
        clean = str(a.get("name", "")).split(".")[-1]
        if clean in TARGETS:
            print(f"  {option:7s} {clean:8s} id={aid:>5} enabled={a.get('enabled')} suspended={a.get('is_suspended')}")

# 2) get_all_open_time open flags across binary/turbo/digital
print("\n=== get_all_open_time open flags ===")
ot = iq.get_all_open_time()
for kind in ("binary", "turbo", "digital"):
    for name in TARGETS:
        info = (ot.get(kind) or {}).get(name)
        if info is not None:
            print(f"  {kind:8s} {name:8s} open={info.get('open')}")

# 3) payout from get_all_profit
print("\n=== get_all_profit (payout) ===")
try:
    profits = iq.get_all_profit()
    for name in TARGETS:
        p = profits.get(name)
        print(f"  {name:8s} -> {p}")
except Exception as e:
    print("  get_all_profit failed:", e)

# 4) which digital underlyings (real, non-OTC forex) are open now
print("\n=== open DIGITAL real-forex underlyings ===")
try:
    digital_open = [n for n, i in (ot.get("digital") or {}).items()
                    if i.get("open") and not n.endswith("-OTC") and len(n) == 6]
    print(" ", sorted(digital_open) or "NONE")
except Exception as e:
    print("  failed:", e)
