"""
time_filter.py — Trading window + blackout + weekend rule (San's Architecture Notes §6).

All times Asia/Bangkok. Replaces main.py's ad-hoc `_compute_weekend_halt()` /
`_handle_weekend_transition()` — same TZ pattern, folded into one filter together
with the trading-window + blackout rules.

News-calendar blackout is wired but not fetched yet (open question — see San's
notes §16.1): `news_blackout_until` can be set externally by a future calendar
fetcher; is_tradeable() already checks it so the wiring is ready.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

BANGKOK = ZoneInfo("Asia/Bangkok")


class TimeFilter:
    WINDOWS = [(time(14, 0), time(17, 0)), (time(19, 30), time(22, 30))]
    BLACKOUT = [(time(19, 0), time(19, 30))]  # NY open spike, inside window 2, carved out

    def __init__(self):
        self.news_blackout_until: Optional[datetime] = None

    def is_tradeable(self, now_th: Optional[datetime] = None) -> tuple[bool, str]:
        if now_th is None:
            now_th = datetime.now(BANGKOK)
        elif now_th.tzinfo is None:
            now_th = now_th.replace(tzinfo=BANGKOK)

        wd, t = now_th.weekday(), now_th.time()

        if self.news_blackout_until is not None and now_th < self.news_blackout_until:
            return False, f"news blackout จนถึง {self.news_blackout_until.isoformat()}"
        if wd == 4 and t >= time(21, 0):
            return False, "ศุกร์หลัง 21:00 — ปิดเทรดถึงจันทร์"
        if wd == 0 and t < time(15, 0):
            return False, "จันทร์ก่อน 15:00 — รอ"
        if wd in (5, 6):
            return False, "วันหยุดสุดสัปดาห์"
        if any(b[0] <= t < b[1] for b in self.BLACKOUT):
            return False, "blackout 19:00-19:30 (NY open)"
        if any(w[0] <= t < w[1] for w in self.WINDOWS):
            return True, "in window"
        return False, "นอกหน้าต่างเทรด"
