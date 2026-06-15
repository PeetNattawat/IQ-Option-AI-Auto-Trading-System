"""
Diagnostic: resolve what active_id 1880 really is, straight from IQ Option's
LIVE instrument list (get_all_init_v2). OTC instruments are named "front.XXX-OTC".
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
init = iq.get_all_init_v2()

found = False
open_real, open_otc = [], []
for option in ("binary", "turbo"):
    actives = (init.get(option) or {}).get("actives") or {}
    for aid, active in actives.items():
        clean = str(active.get("name", "")).split(".")[-1]
        is_open = active.get("enabled", True) and not active.get("is_suspended", False)
        if str(aid) == str(TARGET):
            found = True
            print(f"\n[FOUND] active_id {aid} in {option}")
            print(f"  name      = {active.get('name')}  (clean: {clean})")
            print(f"  is OTC?   = {clean.endswith('-OTC')}")
            print(f"  enabled   = {active.get('enabled')}  suspended = {active.get('is_suspended')}")
        if is_open and len(clean) >= 6:
            (open_otc if clean.endswith("-OTC") else open_real).append(clean)

if not found:
    print(f"\n[NOT FOUND] active_id {TARGET} not in binary/turbo actives")
    try:
        print("  live name lookup ->", iq.get_name_by_activeId(TARGET))
    except Exception as e:
        print("  name lookup failed:", e)

print("\n── open REAL (non-OTC) pairs ──")
print(sorted(set(open_real)) or "NONE")
print("\n── open OTC pairs count ──", len(set(open_otc)))
