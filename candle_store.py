"""
candle_store.py — Dual-timeframe (M15 + M5) rolling candle buffer.

Spec reference: San's Architecture Notes §2 (outputs/01_san-iqoption-spec-overhaul.md).

One CandleStore instance per configured asset. Holds two independent rolling
deques (M15 trend / M5 entry). Only CLOSED candles ever enter a buffer — the
currently-forming candle is discarded at the single point where candles enter
the system (bootstrap() and append_if_new()), so no downstream module
(TrendFilter, EntrySignal, indicators) needs its own no-repaint check.

Buffer max length = 200 in memory per timeframe. When the deque is full and a
new candle arrives, the candle about to be evicted is first appended to an
append-only JSONL file under data/candles/ so history is not lost, while RAM
never exceeds MAX_IN_MEMORY.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd


@dataclass
class Candle:
    ts: int        # epoch seconds, candle OPEN time (IQ Option convention)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


TF_SECONDS = {"m5": 300, "m15": 900}


class CandleStore:
    """Dual-TF rolling buffer + append-only overflow persistence for one asset."""

    MAX_IN_MEMORY = 200

    def __init__(self, asset: str, persist_dir: str = "data/candles"):
        self.asset = asset
        self.persist_dir = persist_dir
        self.m15: deque[Candle] = deque(maxlen=self.MAX_IN_MEMORY)
        self.m5: deque[Candle] = deque(maxlen=self.MAX_IN_MEMORY)
        self._last_m15_ts: Optional[int] = None
        self._last_m5_ts: Optional[int] = None
        os.makedirs(self.persist_dir, exist_ok=True)

    # ── persistence paths ──
    def _overflow_path(self, tf: str) -> str:
        safe_asset = self.asset.replace("/", "_")
        return os.path.join(self.persist_dir, f"{safe_asset}_{tf}.jsonl")

    def _persist_one(self, tf: str, candle: Candle) -> None:
        with open(self._overflow_path(tf), "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(candle)) + "\n")

    def _read_tail(self, tf: str, n: int) -> list[Candle]:
        """Read the last n persisted candles for restart continuity."""
        path = self._overflow_path(tf)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        out = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                out.append(Candle(**d))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    # ── raw candle normalization (shared by bootstrap + live IQ fetch) ──
    @staticmethod
    def _normalize_iq_candles(raw_candles: list[dict], drop_forming: bool = True) -> list[Candle]:
        """IQ Option's get_candles() always returns the still-forming candle as the
        last element. Drop it here — this is the ONE place raw broker candles enter
        the system, so every consumer downstream only ever sees closed candles."""
        out = []
        for c in raw_candles:
            try:
                out.append(Candle(
                    ts=int(c.get("from") or c.get("id") or c.get("at") or 0),
                    open=float(c["open"]),
                    high=float(c.get("max", c.get("high"))),
                    low=float(c.get("min", c.get("low"))),
                    close=float(c["close"]),
                    volume=float(c.get("volume", 0) or 0),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda c: c.ts)
        if drop_forming and len(out) > 1:
            out = out[:-1]
        return out

    def bootstrap(self, iq) -> None:
        """One-time startup fetch: last MAX_IN_MEMORY CLOSED candles for both TFs.
        Also seeds continuity from any persisted overflow tail if present."""
        for tf, seconds in TF_SECONDS.items():
            try:
                raw = iq.get_candles(self.asset, seconds, self.MAX_IN_MEMORY, time.time())
            except Exception:
                raw = None
            candles = self._normalize_iq_candles(raw or [])
            buf = self.m15 if tf == "m15" else self.m5
            buf.clear()
            for c in candles:
                buf.append(c)
            if buf:
                if tf == "m15":
                    self._last_m15_ts = buf[-1].ts
                else:
                    self._last_m5_ts = buf[-1].ts

    def load_candles(self, tf: str, raw_candles: list[dict], drop_forming: bool = True) -> int:
        """Testable/backtest-friendly bootstrap path — load from an already-fetched
        list of raw candle dicts (broker shape or plain OHLCV dicts) instead of a
        live iq client. Returns number of candles loaded."""
        candles = self._normalize_iq_candles(raw_candles, drop_forming=drop_forming)
        buf = self.m15 if tf == "m15" else self.m5
        buf.clear()
        for c in candles:
            buf.append(c)
        if buf:
            if tf == "m15":
                self._last_m15_ts = buf[-1].ts
            else:
                self._last_m5_ts = buf[-1].ts
        return len(buf)

    def append_if_new(self, tf: str, candle: Candle) -> bool:
        """Append a single newly-closed candle (from the M5/M15 close scheduler event).
        Dedup guard: only appends if candle.ts is newer than the last stored ts —
        satisfies "ห้ามดึง 200 แท่งใหม่ทุกรอบ" (never re-fetch 200 candles every cycle).
        Persists the candle that's about to be evicted (if the buffer is full) BEFORE
        the new candle pushes it out."""
        buf = self.m15 if tf == "m15" else self.m5
        last_ts = self._last_m15_ts if tf == "m15" else self._last_m5_ts
        if last_ts is not None and candle.ts <= last_ts:
            return False
        if len(buf) >= self.MAX_IN_MEMORY:
            evicted = buf[0]
            self._persist_one(tf, evicted)
        buf.append(candle)
        if tf == "m15":
            self._last_m15_ts = candle.ts
        else:
            self._last_m5_ts = candle.ts
        return True

    # ── DataFrame views for indicators/filters ──
    @staticmethod
    def _to_df(buf: deque[Candle]) -> pd.DataFrame:
        if not buf:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        return pd.DataFrame([asdict(c) for c in buf])

    def m15_df(self) -> pd.DataFrame:
        return self._to_df(self.m15)

    def m5_df(self) -> pd.DataFrame:
        return self._to_df(self.m5)
