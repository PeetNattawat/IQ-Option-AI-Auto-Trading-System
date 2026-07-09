"""
indicators_v2.py — spec-exact indicator engine (San's Architecture Notes §3, ADR-2).

Gap fixed vs the legacy IndicatorEngine (trading_engine.py): EMA now seeds from an
SMA of the first `period` values (spec §2) instead of pandas' `.ewm(adjust=False)`
implicit seed (recursion from the first raw value). The two converge after enough
bars, but backtest-vs-live parity (spec §10) requires the exact formula, not an
asymptotic approximation.

RSI(14) and ATR(14) use Wilder smoothing (`ewm(com=period-1, adjust=False)`), which
is mathematically the standard Wilder recursive formula — implemented directly here
(not imported from trading_engine.IndicatorEngine) so this module has zero
dependency on the IQ Option client / websockets import chain, and can be reused
standalone by backtest.py and data_downloader.py.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class IndicatorEngineV2:

    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        """SMA-seeded EMA per spec §2.
        First `period-1` values are NaN (not enough data).
        Value at index `period-1` is SMA(series[:period]).
        From `period` onward: recursive EMA using multiplier 2/(period+1)."""
        n = len(series)
        out = pd.Series(index=series.index, dtype=float)
        if n < period:
            out[:] = np.nan
            return out
        out.iloc[:period - 1] = np.nan
        seed = series.iloc[:period].mean()
        out.iloc[period - 1] = seed
        mult = 2 / (period + 1)
        prev = seed
        vals = series.values
        for i in range(period, n):
            prev = (vals[i] - prev) * mult + prev
            out.iloc[i] = prev
        return out

    @staticmethod
    def rsi(series: pd.Series, period: int = 14) -> pd.Series:
        """Wilder-smoothed RSI — unchanged from trading_engine.IndicatorEngine.rsi,
        reimplemented here to avoid importing the IQ Option client chain."""
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
        avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Wilder-smoothed ATR — unchanged formula from trading_engine.IndicatorEngine.atr."""
        high = df["high"]
        low = df["low"]
        close = df["close"].shift(1)
        tr = pd.concat([
            high - low,
            (high - close).abs(),
            (low - close).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(com=period - 1, adjust=False).mean()

    @staticmethod
    def compute_m15(df: pd.DataFrame) -> pd.DataFrame:
        """EMA20 + EMA50 + ATR14 on M15 candles (trend-filter inputs)."""
        df = df.copy()
        df["ema20"] = IndicatorEngineV2.ema(df["close"], 20)
        df["ema50"] = IndicatorEngineV2.ema(df["close"], 50)
        df["atr14"] = IndicatorEngineV2.atr(df, 14)
        return df

    @staticmethod
    def compute_m5(df: pd.DataFrame) -> pd.DataFrame:
        """EMA20 + RSI14 + ATR14 on M5 candles (entry-signal inputs)."""
        df = df.copy()
        df["ema20"] = IndicatorEngineV2.ema(df["close"], 20)
        df["rsi14"] = IndicatorEngineV2.rsi(df["close"], 14)
        df["atr14"] = IndicatorEngineV2.atr(df, 14)
        return df
