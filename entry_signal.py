"""
entry_signal.py — M5 entry signal (San's Architecture Notes §5).

Runs on every M5 candle close, only for assets whose TrendState.status != NO_TRADE.
All sub-checks are AND'ed hard gates — no scoring, no partial credit. Direction is
locked by the M15 trend (UPTREND -> only CALL setups considered, DOWNTREND -> PUT).

Candlestick pattern formulas (§4.2) verified byte-for-byte against the original
source spec doc (binary-options-bot-spec.md §4.2) per Iris's bug-146 QA finding
(2026-07-09) and corrected accordingly:
  Engulfing: body(C0) >= 0.3 * ATR14(M5) minimum-size filter added (was missing).
  Pin bar/Hammer: dominant_wick >= 0.5 * ATR14(M5) minimum-size filter added (was
  missing); close-condition changed from `c > (h+l)/2` to the spec-literal
  `close > open` (CALL) / `close < open` (PUT); the opposite-wick-max constraint
  that was NOT in the spec has been removed (it was stricter than spec and could
  reject spec-valid pin bars).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from indicators_v2 import IndicatorEngineV2
from trend_filter import TrendState

RSI_CALL_RANGE = (45.0, 65.0)
RSI_PUT_RANGE = (35.0, 55.0)
ATR_VOL_LOW_MULT = 0.5
ATR_VOL_HIGH_MULT = 2.5
ATR_HISTORY_WINDOW = 100

PULLBACK_TOUCH_ATR_MULT = 0.25
PULLBACK_CLOSE_ATR_MULT = 0.5

ENGULF_MIN_RATIO = 1.0          # current body must fully engulf previous body
ENGULF_MIN_BODY_ATR_MULT = 0.3  # spec §4.2: body(C0) >= 0.3 x ATR14(M5) — filters tiny candles
PINBAR_WICK_BODY_RATIO = 2.0    # dominant wick >= 2x body
PINBAR_MIN_WICK_ATR_MULT = 0.5  # spec §4.2: dominant wick >= 0.5 x ATR14(M5)


@dataclass
class EntryResult:
    asset: str
    signal: str                 # "CALL" | "PUT" | "HOLD"
    reason: str
    pattern: Optional[str] = None      # "engulfing" | "pinbar" | None
    trend_status: Optional[str] = None
    row: dict = field(default_factory=dict)   # snapshot of indicator values at signal time

    @classmethod
    def hold(cls, asset: str, reason: str) -> "EntryResult":
        return cls(asset=asset, signal="HOLD", reason=reason)

    @classmethod
    def actionable(cls, asset: str, side: str, pattern: str, row: pd.Series, trend: TrendState) -> "EntryResult":
        return cls(
            asset=asset, signal=side, reason=f"entry ok ({pattern})", pattern=pattern,
            trend_status=trend.status,
            row={
                "close": float(row["close"]), "ema20": float(row["ema20"]),
                "rsi14": float(row["rsi14"]), "atr14": float(row["atr14"]),
                "ts": row.get("ts"),
            },
        )


class EntrySignal:

    def _detect_pattern(self, row: pd.Series, prev: pd.Series, side: str) -> Optional[str]:
        """Returns 'engulfing' | 'pinbar' | None. side is 'CALL' or 'PUT' — the
        candlestick confirmation must point the same direction as the trend."""
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        po, pc = prev["open"], prev["close"]
        atr = row["atr14"]
        body = abs(c - o)
        rng = h - l
        if rng <= 0:
            return None

        if side == "CALL":
            # Bullish engulfing: prev bearish, current bullish, body engulfs prev body,
            # body >= 0.3xATR14 (spec §4.2 — filters out insignificant/tiny candles)
            prev_bear = pc < po
            cur_bull = c > o
            if prev_bear and cur_bull and o <= pc and c >= po \
                    and body >= ENGULF_MIN_BODY_ATR_MULT * atr:
                return "engulfing"
            # Hammer / bullish pin bar (spec §4.2): lower_wick >= 2xbody AND
            # lower_wick >= 0.5xATR14 AND close > open
            lower_wick = min(o, c) - l
            if body > 0 and lower_wick >= PINBAR_WICK_BODY_RATIO * body \
                    and lower_wick >= PINBAR_MIN_WICK_ATR_MULT * atr \
                    and c > o:
                return "pinbar"
            return None
        else:  # PUT — exact mirror of CALL
            prev_bull = pc > po
            cur_bear = c < o
            if prev_bull and cur_bear and o >= pc and c <= po \
                    and body >= ENGULF_MIN_BODY_ATR_MULT * atr:
                return "engulfing"
            # Shooting star / bearish pin bar (spec §4.2 mirror): upper_wick >= 2xbody
            # AND upper_wick >= 0.5xATR14 AND close < open
            upper_wick = h - max(o, c)
            if body > 0 and upper_wick >= PINBAR_WICK_BODY_RATIO * body \
                    and upper_wick >= PINBAR_MIN_WICK_ATR_MULT * atr \
                    and c < o:
                return "pinbar"
            return None

    def evaluate(self, asset: str, trend: TrendState, m5_df: pd.DataFrame,
                 atr_history_100: Optional[pd.Series] = None) -> EntryResult:
        if trend.status == "NO_TRADE":
            return EntryResult.hold(asset, "trend NO_TRADE (M15) — short-circuit")

        min_needed = 22  # EMA20 seed(20) + prev candle
        if m5_df is None or len(m5_df) < min_needed:
            return EntryResult.hold(asset, "M5 ข้อมูลไม่พอ")

        df = IndicatorEngineV2.compute_m5(m5_df)
        row, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(row.ema20) or pd.isna(row.rsi14) or pd.isna(row.atr14) or pd.isna(row.atr14):
            return EntryResult.hold(asset, "indicator ยังไม่ settle")

        side = "CALL" if trend.status == "UPTREND" else "PUT"

        # 4.1 Pullback-to-EMA20
        if side == "CALL":
            pullback_ok = (row.low <= row.ema20 + PULLBACK_TOUCH_ATR_MULT * row.atr14) and \
                          (row.close > row.ema20 - PULLBACK_CLOSE_ATR_MULT * row.atr14)
        else:
            pullback_ok = (row.high >= row.ema20 - PULLBACK_TOUCH_ATR_MULT * row.atr14) and \
                          (row.close < row.ema20 + PULLBACK_CLOSE_ATR_MULT * row.atr14)
        if not pullback_ok:
            return EntryResult.hold(asset, "pullback ไม่เข้าเงื่อนไข")

        # 4.2 Candlestick pattern — logged separately (engulfing vs pinbar)
        pattern = self._detect_pattern(row, prev, side)
        if pattern is None:
            return EntryResult.hold(asset, "ไม่พบแท่งยืนยัน (engulfing/pinbar)")

        # 4.3 RSI filter
        lo, hi = RSI_CALL_RANGE if side == "CALL" else RSI_PUT_RANGE
        if not (lo <= row.rsi14 <= hi):
            return EntryResult.hold(asset, f"RSI {row.rsi14:.1f} นอกโซน [{lo}-{hi}]")

        # 4.4 Volatility sanity — median of trailing 100 ATR values (not mean)
        if atr_history_100 is None:
            atr_history_100 = df["atr14"].tail(ATR_HISTORY_WINDOW)
        hist = atr_history_100.dropna()
        if len(hist) < 20:
            return EntryResult.hold(asset, "ATR history ไม่พอสำหรับ volatility check")
        median_atr = float(hist.median())
        if median_atr <= 0:
            return EntryResult.hold(asset, "median ATR = 0 — ข้อมูลผิดปกติ")
        vol_ok = (ATR_VOL_LOW_MULT * median_atr) <= row.atr14 <= (ATR_VOL_HIGH_MULT * median_atr)
        if not vol_ok:
            return EntryResult.hold(asset, f"ATR {row.atr14:.5f} นอกช่วง {ATR_VOL_LOW_MULT}x-{ATR_VOL_HIGH_MULT}x median100 ({median_atr:.5f})")

        return EntryResult.actionable(asset, side, pattern, row, trend)
