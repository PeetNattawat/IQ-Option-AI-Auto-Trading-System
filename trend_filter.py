"""
trend_filter.py — M15 trend filter, 4-condition (San's Architecture Notes §4).

Runs once per M15 candle close, per configured asset. Result is a TrendState that
EntrySignal (M5) reads on every M5 close to decide which side (CALL/PUT) it is even
allowed to look for, or to short-circuit to HOLD when NO_TRADE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from indicators_v2 import IndicatorEngineV2

EMA_TREND_PERIOD = 50
SLOPE_LOOKBACK = 3        # compare current EMA20 vs 3 closed candles back
SEPARATION_ATR_MULT = 0.15


@dataclass
class TrendState:
    asset: str
    status: str            # "UPTREND" | "DOWNTREND" | "NO_TRADE"
    ema20: Optional[float]
    ema50: Optional[float]
    atr14: Optional[float]
    computed_at: Optional[str]   # ISO ts of the M15 candle this was computed from


class TrendFilter:

    def evaluate(self, m15_df: pd.DataFrame, asset: str) -> TrendState:
        min_needed = EMA_TREND_PERIOD + SLOPE_LOOKBACK
        if m15_df is None or len(m15_df) < min_needed:
            return TrendState(asset=asset, status="NO_TRADE", ema20=None, ema50=None,
                               atr14=None, computed_at=None)

        df = IndicatorEngineV2.compute_m15(m15_df)
        row = df.iloc[-1]
        row_3back = df.iloc[-1 - SLOPE_LOOKBACK]

        if pd.isna(row.ema20) or pd.isna(row.ema50) or pd.isna(row.atr14) or pd.isna(row_3back.ema20):
            return TrendState(asset=asset, status="NO_TRADE", ema20=None, ema50=None,
                               atr14=None, computed_at=None)

        ema20, ema50, atr14 = float(row.ema20), float(row.ema50), float(row.atr14)
        close = float(row.close)
        ema20_3back = float(row_3back.ema20)

        up = (
            ema20 > ema50
            and (ema20 - ema50) >= SEPARATION_ATR_MULT * atr14
            and close > ema20
            and ema20 > ema20_3back
        )
        down = (
            ema20 < ema50
            and (ema50 - ema20) >= SEPARATION_ATR_MULT * atr14
            and close < ema20
            and ema20 < ema20_3back
        )

        status = "UPTREND" if up else ("DOWNTREND" if down else "NO_TRADE")
        computed_at = row.get("ts") if "ts" in df.columns else None
        return TrendState(
            asset=asset, status=status, ema20=ema20, ema50=ema50, atr14=atr14,
            computed_at=str(computed_at) if computed_at is not None else None,
        )
