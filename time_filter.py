"""
time_filter.py — Trading window + blackout + weekend rule (San's Architecture Notes §6).

All times Asia/Bangkok. Replaces main.py's ad-hoc `_compute_weekend_halt()` /
`_handle_weekend_transition()` — same TZ pattern, folded into one filter together
with the trading-window + blackout rules.

News-calendar blackout is wired but not fetched yet (open question — see San's
notes §16.1): `news_blackout_until` can be set externally by a future calendar
fetcher; is_tradeable() already checks it so the wiring is ready.

── 2026-07-21 24h PRACTICE-only trading-hours experiment (Psycho-approved, Peet-approved) ──
`trading_hours_experiment` (default False, opt-in via config.json/dashboard) bypasses ONLY
the two `WINDOWS` entries below (14:00-17:00 / 19:30-22:30). It does NOT bypass:
  - the weekend halt (Fri >=21:00 -> Mon <15:00) — Psycho was explicit that weekend gaps
    must never be included in this experiment's data, they would pollute the win-rate
    comparison with illiquid/closed-market conditions.
  - the Monday-before-15:00 gate, or the 19:00-19:30 NY-open blackout — DESIGN DECISION
    (Titan, 2026-07-21): Psycho's brief only asked to widen the *main* two-window
    restriction; it did not say whether the narrower NY-open blackout or the Monday
    settle-in gate should also open up. Both are pre-existing, narrower, deliberate
    "even worse than average" carve-outs (spike risk / weekend-gap settling), so the
    conservative default is to leave them as normal always-on gates even under the
    experiment flag. Peet/Psycho can override this by asking Titan to also gate these
    two behind the same flag — flagging here so the decision is visible, not buried.
Wiring: main.py's `_enforce_trading_hours_experiment_practice_gate()` refuses to let this
flag be True unless `account_type == "PRACTICE"` (mirrors the existing spec_v1 gate).
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

BANGKOK = ZoneInfo("Asia/Bangkok")

# Session tags for trade-record tagging (win-rate-by-session comparison after
# ~2 weeks / 50-100 resolved trades — see outputs/ report for this ticket).
SESSION_LONDON_NY_WINDOW = "london_ny_window"
SESSION_EXTENDED_HOURS = "experiment_extended_hours"


class TimeFilter:
    WINDOWS = [(time(14, 0), time(17, 0)), (time(19, 30), time(22, 30))]
    BLACKOUT = [(time(19, 0), time(19, 30))]  # NY open spike, inside window 2, carved out

    def __init__(self, trading_hours_experiment: bool = False):
        self.news_blackout_until: Optional[datetime] = None
        # Opt-in 24h PRACTICE experiment flag — see module docstring. Default False
        # keeps real-money/legacy behavior byte-for-byte unchanged when unset.
        self.trading_hours_experiment = trading_hours_experiment

    def is_tradeable(self, now_th: Optional[datetime] = None) -> tuple[bool, str]:
        if now_th is None:
            now_th = datetime.now(BANGKOK)
        elif now_th.tzinfo is None:
            now_th = now_th.replace(tzinfo=BANGKOK)

        wd, t = now_th.weekday(), now_th.time()

        if self.news_blackout_until is not None and now_th < self.news_blackout_until:
            return False, f"news blackout จนถึง {self.news_blackout_until.isoformat()}"
        # Weekend halt — ALWAYS enforced, regardless of trading_hours_experiment. Do not
        # move this below the experiment bypass; weekend data must never leak into the
        # experiment's win-rate-by-session comparison.
        if wd == 4 and t >= time(21, 0):
            return False, "ศุกร์หลัง 21:00 — ปิดเทรดถึงจันทร์"
        if wd == 0 and t < time(15, 0):
            return False, "จันทร์ก่อน 15:00 — รอ"
        if wd in (5, 6):
            return False, "วันหยุดสุดสัปดาห์"
        # NY-open blackout — ALWAYS enforced (see module docstring design note); the
        # experiment flag widens the WINDOWS check below, not this narrower carve-out.
        if any(b[0] <= t < b[1] for b in self.BLACKOUT):
            return False, "blackout 19:00-19:30 (NY open)"
        if self.trading_hours_experiment:
            return True, "in window (24h trading-hours experiment, PRACTICE-only)"
        if any(w[0] <= t < w[1] for w in self.WINDOWS):
            return True, "in window"
        return False, "นอกหน้าต่างเทรด"

    def session_tag(self, now_th: Optional[datetime] = None) -> str:
        """Classify a (tradeable) tick's session for trade-record tagging. Independent of
        is_tradeable()'s gating logic — purely a label so ENTER/WIN/LOSS/EXPIRED records
        can later be grouped by session to compare win rate and see whether off-hours
        trades are cannibalizing the max_trades_per_day budget before the core
        london_ny_window sessions arrive."""
        if now_th is None:
            now_th = datetime.now(BANGKOK)
        elif now_th.tzinfo is None:
            now_th = now_th.replace(tzinfo=BANGKOK)
        t = now_th.time()
        if any(w[0] <= t < w[1] for w in self.WINDOWS):
            return SESSION_LONDON_NY_WINDOW
        return SESSION_EXTENDED_HOURS
