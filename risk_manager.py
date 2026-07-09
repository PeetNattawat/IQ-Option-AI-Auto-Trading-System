"""
risk_manager.py — always-on hard risk rules (San's Architecture Notes §7.1).

Spec-exact numbers, all persisted so a restart doesn't silently reset the day's
risk state. Martingale is intentionally NOT here — see martingale.py (§7.2, opt-in,
isolated, server-enforced two-flag gate).

Two independent loss-response mechanisms (ADR-5, do not conflate):
  1. max_consecutive_losses (3) -> HARD stop for the rest of the CALENDAR DAY.
  2. signal_cooldown_minutes (15) -> SOFT cooldown after ANY single loss, time-based.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

BANGKOK = ZoneInfo("Asia/Bangkok")


@dataclass
class RiskConfig:
    stake_pct: float = 1.5
    max_trades_per_day: int = 5
    max_consecutive_losses: int = 3
    daily_loss_limit_pct: float = 4.0
    weekly_loss_limit_pct: float = 10.0
    signal_cooldown_minutes: int = 15
    auto_stop_enabled: bool = True
    auto_stop_drawdown_pct: float = 30.0


def _week_start(d: datetime) -> str:
    """Monday 00:00 Asia/Bangkok, as an ISO date string — used as the week boundary key."""
    monday = d - timedelta(days=d.weekday())
    return monday.date().isoformat()


class RiskManager:

    def __init__(self, cfg: RiskConfig, state_path: str = "data/risk_state.json",
                 snapshot_path: str = "data/equity_snapshots.json",
                 on_event=None):
        self.cfg = cfg
        self.state_path = state_path
        self.snapshot_path = snapshot_path
        self.on_event = on_event  # optional callable(event_type: str, detail: dict) -> None

        self._trades_today = 0
        self._consecutive_losses = 0
        self._daily_pnl = 0.0
        self._weekly_pnl = 0.0
        self._open_positions = 0
        self._signal_cooldown_until: Optional[datetime] = None
        self._day_hard_stop = False
        self._week_hard_stop = False
        self._day_hard_stop_reason: Optional[str] = None
        self._week_hard_stop_reason: Optional[str] = None

        self._current_day_key: Optional[str] = None
        self._current_week_key: Optional[str] = None
        self._balance_start_of_day: Optional[float] = None
        self._balance_start_of_week: Optional[float] = None
        self._equity_baseline: Optional[float] = None
        self._auto_stop_triggered = False

        self._load_state()
        self._load_snapshots()

    # ── persistence ──
    def _load_state(self):
        try:
            with open(self.state_path) as f:
                d = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        self._trades_today = d.get("trades_today", 0)
        self._consecutive_losses = d.get("consecutive_losses", 0)
        self._daily_pnl = d.get("daily_pnl", 0.0)
        self._weekly_pnl = d.get("weekly_pnl", 0.0)
        self._day_hard_stop = d.get("day_hard_stop", False)
        self._week_hard_stop = d.get("week_hard_stop", False)
        self._day_hard_stop_reason = d.get("day_hard_stop_reason")
        self._week_hard_stop_reason = d.get("week_hard_stop_reason")
        self._current_day_key = d.get("current_day_key")
        self._current_week_key = d.get("current_week_key")
        cd = d.get("signal_cooldown_until")
        self._signal_cooldown_until = datetime.fromisoformat(cd) if cd else None
        self._auto_stop_triggered = d.get("auto_stop_triggered", False)

    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump({
                "trades_today": self._trades_today,
                "consecutive_losses": self._consecutive_losses,
                "daily_pnl": self._daily_pnl,
                "weekly_pnl": self._weekly_pnl,
                "day_hard_stop": self._day_hard_stop,
                "week_hard_stop": self._week_hard_stop,
                "day_hard_stop_reason": self._day_hard_stop_reason,
                "week_hard_stop_reason": self._week_hard_stop_reason,
                "current_day_key": self._current_day_key,
                "current_week_key": self._current_week_key,
                "signal_cooldown_until": self._signal_cooldown_until.isoformat() if self._signal_cooldown_until else None,
                "auto_stop_triggered": self._auto_stop_triggered,
            }, f, indent=2)

    def _load_snapshots(self):
        try:
            with open(self.snapshot_path) as f:
                d = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            d = {}
        self._balance_start_of_day = d.get("balance_start_of_day")
        self._balance_start_of_week = d.get("balance_start_of_week")
        self._equity_baseline = d.get("equity_baseline")

    def _save_snapshots(self):
        os.makedirs(os.path.dirname(self.snapshot_path) or ".", exist_ok=True)
        with open(self.snapshot_path, "w") as f:
            json.dump({
                "balance_start_of_day": self._balance_start_of_day,
                "balance_start_of_week": self._balance_start_of_week,
                "equity_baseline": self._equity_baseline,
            }, f, indent=2)

    def _emit(self, event_type: str, detail: dict):
        if self.on_event:
            try:
                self.on_event(event_type, detail)
            except Exception:
                pass

    # ── boundary rollover — call once per cycle (cheap) ──
    def roll_boundaries(self, now_th: Optional[datetime] = None, balance: Optional[float] = None):
        if now_th is None:
            now_th = datetime.now(BANGKOK)
        day_key = now_th.date().isoformat()
        week_key = _week_start(now_th)

        if self._current_day_key != day_key:
            self._current_day_key = day_key
            self._trades_today = 0
            self._consecutive_losses = 0
            self._daily_pnl = 0.0
            self._day_hard_stop = False
            self._day_hard_stop_reason = None
            if balance is not None:
                self._balance_start_of_day = balance
                self._save_snapshots()
            self._emit("CONFIG_CHANGED", {"reason": "day_boundary", "day": day_key})

        if self._current_week_key != week_key:
            self._current_week_key = week_key
            self._weekly_pnl = 0.0
            self._week_hard_stop = False
            self._week_hard_stop_reason = None
            if balance is not None:
                self._balance_start_of_week = balance
                self._save_snapshots()
            self._emit("CONFIG_CHANGED", {"reason": "week_boundary", "week": week_key})

        if self._equity_baseline is None and balance is not None:
            self._equity_baseline = balance
            self._save_snapshots()
            self._emit("CONFIG_CHANGED", {"reason": "equity_baseline_seeded", "baseline": balance})

        self._save_state()

    # ── stake ──
    def stake_amount(self, current_balance: float) -> float:
        return round(current_balance * self.cfg.stake_pct / 100, 2)

    # ── gate ──
    def can_trade(self, now_th: Optional[datetime] = None, balance: Optional[float] = None) -> tuple[bool, str]:
        if now_th is None:
            now_th = datetime.now(BANGKOK)

        if self._open_positions > 0:
            return False, "มีไม้เปิดอยู่ — ห้ามซ้อน"
        if self._trades_today >= self.cfg.max_trades_per_day:
            return False, f"ครบ {self.cfg.max_trades_per_day} ไม้/วันแล้ว"
        if self._consecutive_losses >= self.cfg.max_consecutive_losses:
            return False, f"แพ้ติดกัน {self._consecutive_losses} ไม้ — หยุดถึงวันถัดไป"
        if self._day_hard_stop:
            return False, self._day_hard_stop_reason or "หยุดเทรดถึงวันถัดไป"
        if self._week_hard_stop:
            return False, self._week_hard_stop_reason or "หยุดเทรดถึงสัปดาห์ถัดไป"
        if self._signal_cooldown_until and now_th < self._signal_cooldown_until:
            remaining = int((self._signal_cooldown_until - now_th).total_seconds() / 60) + 1
            return False, f"cooldown หลังแพ้ — เหลือ {remaining} นาที"
        if self._balance_start_of_day and self._balance_start_of_day > 0:
            if -self._daily_pnl >= self._balance_start_of_day * self.cfg.daily_loss_limit_pct / 100:
                return False, f"ครบ daily loss limit {self.cfg.daily_loss_limit_pct}% — หยุดถึงวันถัดไป"
        if self._balance_start_of_week and self._balance_start_of_week > 0:
            if -self._weekly_pnl >= self._balance_start_of_week * self.cfg.weekly_loss_limit_pct / 100:
                return False, f"ครบ weekly loss limit {self.cfg.weekly_loss_limit_pct}% — หยุดถึงสัปดาห์ถัดไป"
        if self.cfg.auto_stop_enabled and self._equity_baseline and balance is not None:
            floor = self._equity_baseline * (1 - self.cfg.auto_stop_drawdown_pct / 100)
            if balance <= floor:
                if not self._auto_stop_triggered:
                    self._auto_stop_triggered = True
                    self._save_state()
                    self._emit("AUTO_STOP_TRIGGERED", {"balance": balance, "baseline": self._equity_baseline,
                                                        "drawdown_pct": self.cfg.auto_stop_drawdown_pct})
                return False, "AUTO-STOP: equity ลดเกิน threshold — ต้องรีเซ็ตจาก dashboard"
        return True, "OK"

    # ── lifecycle hooks ──
    def record_order_placed(self):
        self._open_positions += 1
        self._trades_today += 1
        self._save_state()

    def record_order_result(self, pnl: float, result: str, now_th: Optional[datetime] = None,
                             count_streak: bool = True):
        """count_streak=False lets a caller feed non-bot trades (manual/web) into the P&L
        and open-position counters (which legacy tracks for ALL sources) without polluting
        the consecutive-losses hard-stop counter (which legacy's trading_engine.py only
        ever increments for source == "auto" — see _apply_close). The soft signal-cooldown
        timer (ADR-5, mechanism 2) is intentionally NOT gated by count_streak: spec says it
        fires after ANY single loss regardless of who placed the trade."""
        if now_th is None:
            now_th = datetime.now(BANGKOK)
        self._open_positions = max(0, self._open_positions - 1)
        self._daily_pnl += pnl
        self._weekly_pnl += pnl

        if result == "LOSS":
            self._signal_cooldown_until = now_th + timedelta(minutes=self.cfg.signal_cooldown_minutes)
            if count_streak:
                self._consecutive_losses += 1
                if self._consecutive_losses >= self.cfg.max_consecutive_losses:
                    # can_trade() checks self._consecutive_losses directly (its own branch,
                    # ahead of _day_hard_stop) — this event is emitted for audit/system_events
                    # only, it does not itself gate anything beyond the counter check.
                    self._emit("STATE_TRANSITION", {"reason": "max_consecutive_losses_hard_stop",
                                                     "consecutive_losses": self._consecutive_losses})
        elif result == "WIN":
            if count_streak:
                self._consecutive_losses = 0

        if self._balance_start_of_day and -self._daily_pnl >= self._balance_start_of_day * self.cfg.daily_loss_limit_pct / 100:
            self._day_hard_stop = True
            self._day_hard_stop_reason = f"ครบ daily loss limit {self.cfg.daily_loss_limit_pct}% — หยุดถึงวันถัดไป"
        if self._balance_start_of_week and -self._weekly_pnl >= self._balance_start_of_week * self.cfg.weekly_loss_limit_pct / 100:
            self._week_hard_stop = True
            self._week_hard_stop_reason = f"ครบ weekly loss limit {self.cfg.weekly_loss_limit_pct}% — หยุดถึงสัปดาห์ถัดไป"

        self._save_state()

    # ── dashboard actions (§13.2) ──
    def set_auto_stop(self, enabled: bool, drawdown_pct: Optional[float] = None):
        self.cfg.auto_stop_enabled = bool(enabled)
        if drawdown_pct is not None:
            self.cfg.auto_stop_drawdown_pct = max(5.0, min(50.0, float(drawdown_pct)))
        self._emit("CONFIG_CHANGED", {"auto_stop_enabled": self.cfg.auto_stop_enabled,
                                       "auto_stop_drawdown_pct": self.cfg.auto_stop_drawdown_pct})

    def reset_equity_baseline(self, balance: float):
        self._equity_baseline = balance
        self._auto_stop_triggered = False
        self._save_snapshots()
        self._save_state()
        self._emit("AUTO_STOP_RESET", {"new_baseline": balance})

    # ── snapshot for §13.1 WS payload ──
    def to_state_dict(self, balance: Optional[float] = None) -> dict:
        daily_pnl_pct = (self._daily_pnl / self._balance_start_of_day * 100) if self._balance_start_of_day else 0.0
        weekly_pnl_pct = (self._weekly_pnl / self._balance_start_of_week * 100) if self._balance_start_of_week else 0.0
        current_drawdown_pct = 0.0
        if self._equity_baseline and balance is not None and self._equity_baseline > 0:
            current_drawdown_pct = round((1 - balance / self._equity_baseline) * 100, 2)
        hard_stop_until = "next_day" if self._day_hard_stop else ("next_week" if self._week_hard_stop else None)
        return {
            "open_positions": self._open_positions,
            "trades_today": self._trades_today,
            "max_trades_per_day": self.cfg.max_trades_per_day,
            "consecutive_losses": self._consecutive_losses,
            "max_consecutive_losses": self.cfg.max_consecutive_losses,
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "daily_loss_limit_pct": self.cfg.daily_loss_limit_pct,
            "weekly_pnl_pct": round(weekly_pnl_pct, 2),
            "weekly_loss_limit_pct": self.cfg.weekly_loss_limit_pct,
            "signal_cooldown_until": self._signal_cooldown_until.isoformat() if self._signal_cooldown_until else None,
            "hard_stop_until": hard_stop_until,
            "auto_stop": {
                "enabled": self.cfg.auto_stop_enabled,
                "drawdown_pct": self.cfg.auto_stop_drawdown_pct,
                "baseline_balance": self._equity_baseline,
                "current_drawdown_pct": current_drawdown_pct,
                "triggered": self._auto_stop_triggered,
            },
        }
