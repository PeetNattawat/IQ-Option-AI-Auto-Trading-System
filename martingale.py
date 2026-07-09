"""
martingale.py — opt-in Martingale ladder (San's Architecture Notes §7.2, ADR-4).

Structurally isolated from RiskManager.stake_amount() (the default path). Only
consulted by the state machine's PLACE_ORDER step when BOTH cfg.martingale_enabled
AND cfg.martingale_ack_risk are True — checked together, every cycle, not just at
toggle time, so a partial/corrupted config can never silently activate it.

Peet's decision #2 (project brief): server enforces the two-flag gate independently
of the dashboard, because client-side-only gating is bypassable via raw WS messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field


WARNING_TEXT = (
    "⚠️ Martingale เปิดอยู่ — สเปคของโปรเจกต์นี้ระบุห้ามใช้ทุกรูปแบบ เพราะ payout ต่ำกว่า 100% "
    "ทำให้ล้างพอร์ตเร็วกว่า Forex ปกติ การเปิดใช้เป็นการตัดสินใจของผู้ใช้เอง ไม่ใช่ค่าที่แนะนำ"
)


@dataclass
class MartingaleConfig:
    base: float = 50.0
    multiplier: float = 2.0
    max_steps: int = 4


class MartingaleModule:

    def __init__(self, cfg: MartingaleConfig):
        self.cfg = cfg
        self.current_step: int = 0

    @staticmethod
    def is_active(martingale_enabled: bool, martingale_ack_risk: bool) -> bool:
        """The ONLY place that decides whether the ladder may be consulted this cycle.
        Both flags required together — never one without the other."""
        return bool(martingale_enabled) and bool(martingale_ack_risk)

    @staticmethod
    def validate_toggle(enabled: bool, ack_risk: bool) -> tuple[bool, str]:
        """Server-side gate for the `set_martingale` / `update_settings` cmd (§13.2).
        Rejects enabled=true without ack_risk=true in the SAME payload."""
        if enabled and not ack_risk:
            return False, "martingale_enabled=true requires martingale_ack_risk=true in the same payload"
        return True, "OK"

    def sequence(self) -> list[float]:
        return [round(self.cfg.base * (self.cfg.multiplier ** i), 2) for i in range(self.cfg.max_steps)]

    def next_stake(self, base_stake: float | None = None) -> float:
        """Same ladder math as trading_engine.TradeManager.next_auto_stake() — unchanged,
        just relocated behind the opt-in gate."""
        seq = self.sequence()
        step = min(self.current_step, len(seq) - 1)
        return seq[step]

    def advance(self, result: str) -> None:
        """Same as trading_engine.TradeManager._advance_martingale() — unchanged."""
        if result == "LOSS":
            self.current_step += 1
            if self.current_step >= self.cfg.max_steps:
                self.current_step = 0
        elif result == "WIN":
            self.current_step = 0
        # EQUAL: no-op, keep the same step
