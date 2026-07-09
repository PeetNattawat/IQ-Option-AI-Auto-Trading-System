"""
data_downloader.py — historical M5+M15 downloader for backtest.py
(San's Architecture Notes §11; Peet's decision #3, brief).

Primary source: Dukascopy tick data (free, non-OTC/interbank feed, no auth/API key).
Ticks are decompressed (LZMA) from the public bi5 endpoint, aggregated to M1 OHLCV,
then resampled up to M5/M15 to match CandleStore's schema exactly.

KNOWN LIMITATION — verified this session, not a code bug:
  `datafeed.dukascopy.com` timed out on every attempt from this sandboxed dev
  environment (general internet works — google.com/dukascopy.com resolve fine —
  but the tick-data subdomain specifically times out/503s, likely IP-based
  rate-limiting or a network policy blocking that host). The fetch logic below is
  believed correct against Dukascopy's documented bi5 format, but could not be
  exercised end-to-end here. Must be re-run from a host with unrestricted network
  access before trusting the resulting parquet files for a spec-compliant
  backtest (spec §10/§16.3 open question).

Output: data/historical/<pair>_<tf>_<source>.parquet — same OHLCV column schema
CandleStore uses (ts, open, high, low, close, volume) so backtest.py can feed the
output straight into CandleStore.load_candles() / IndicatorEngineV2 unmodified.
"""

from __future__ import annotations

import lzma
import os
import struct
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

HISTORICAL_DIR = Path("data/historical")

# Dukascopy point value per pair (price = raw_int / POINT). Majors covered here;
# extend as needed for additional pairs.
POINT_VALUE = {
    "EURUSD": 100000, "GBPUSD": 100000, "AUDUSD": 100000, "NZDUSD": 100000,
    "USDCAD": 100000, "USDCHF": 100000, "EURGBP": 100000, "EURJPY": 1000,
    "USDJPY": 1000, "GBPJPY": 1000, "AUDJPY": 1000,
}

TICK_RECORD_FMT = ">IIIff"   # ms_offset, ask_raw, bid_raw, ask_vol, bid_vol (big-endian)
TICK_RECORD_SIZE = struct.calcsize(TICK_RECORD_FMT)


def _dukascopy_url(pair: str, dt_utc: datetime) -> str:
    # Dukascopy months are 0-indexed in the URL (January = 00).
    return (f"https://datafeed.dukascopy.com/datafeed/{pair}/"
            f"{dt_utc.year}/{dt_utc.month - 1:02d}/{dt_utc.day:02d}/{dt_utc.hour:02d}h_ticks.bi5")


def _fetch_hour_ticks(pair: str, dt_utc: datetime, retries: int = 3, timeout: int = 20) -> list[tuple]:
    url = _dukascopy_url(pair, dt_utc)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            if not raw:
                return []
            decompressed = lzma.decompress(raw)
            ticks = []
            point = POINT_VALUE.get(pair, 100000)
            for i in range(0, len(decompressed) - TICK_RECORD_SIZE + 1, TICK_RECORD_SIZE):
                ms_off, ask_raw, bid_raw, _ask_vol, _bid_vol = struct.unpack(
                    TICK_RECORD_FMT, decompressed[i:i + TICK_RECORD_SIZE])
                ts = dt_utc + timedelta(milliseconds=ms_off)
                mid = ((ask_raw + bid_raw) / 2.0) / point
                ticks.append((ts, mid))
            return ticks
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []  # market closed that hour (weekend) — not an error
            last_err = e
        except Exception as e:
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Dukascopy fetch failed for {pair} {dt_utc.isoformat()} after {retries} attempts: {last_err}")


def _ticks_to_m1(ticks: list[tuple]) -> pd.DataFrame:
    if not ticks:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(ticks, columns=["ts", "price"])
    df = df.set_index(pd.DatetimeIndex(df["ts"]))
    ohlc = df["price"].resample("1min").ohlc()
    ohlc["volume"] = df["price"].resample("1min").count()
    ohlc = ohlc.dropna(subset=["open"])
    ohlc = ohlc.reset_index().rename(columns={"index": "ts"})
    ohlc["ts"] = ohlc["ts"].astype("int64") // 10**9
    return ohlc[["ts", "open", "high", "low", "close", "volume"]]


def _resample(m1_df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if m1_df.empty:
        return m1_df
    idx = pd.to_datetime(m1_df["ts"], unit="s")
    df = m1_df.set_index(idx)
    out = pd.DataFrame({
        "open": df["open"].resample(rule).first(),
        "high": df["high"].resample(rule).max(),
        "low": df["low"].resample(rule).min(),
        "close": df["close"].resample(rule).last(),
        "volume": df["volume"].resample(rule).sum(),
    }).dropna(subset=["open"])
    out = out.reset_index().rename(columns={"index": "ts"})
    out["ts"] = out["ts"].astype("int64") // 10**9
    return out


def download_history(pair: str, months: int = 12, source: str = "dukascopy",
                      end: datetime | None = None) -> dict[str, Path]:
    """Fetch OHLCV history, normalize to CandleStore's Candle schema, save
    data/historical/<pair>_<tf>_<source>.parquet for tf in (m5, m15).
    Returns {"m5": Path, "m15": Path}."""
    if source != "dukascopy":
        raise NotImplementedError("Only the Dukascopy source is implemented; "
                                   "HistData fallback is an open question (San's notes §16.3)")

    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=months * 30)
    HISTORICAL_DIR.mkdir(parents=True, exist_ok=True)

    all_m1 = []
    cur = start.replace(minute=0, second=0, microsecond=0)
    hours_total = int((end - start).total_seconds() // 3600)
    fetched, failed = 0, 0
    while cur < end:
        if cur.weekday() < 5:  # skip weekend hours quickly (Dukascopy has no data anyway)
            try:
                ticks = _fetch_hour_ticks(pair, cur)
                if ticks:
                    all_m1.append(_ticks_to_m1(ticks))
                fetched += 1
            except RuntimeError:
                failed += 1
        cur += timedelta(hours=1)

    if not all_m1:
        raise RuntimeError(
            f"No candles retrieved for {pair} from Dukascopy ({fetched} hours attempted, "
            f"{failed} failed) — see module docstring: this host's network could not reach "
            f"datafeed.dukascopy.com in Titan's dev sandbox. Re-run from an unrestricted host."
        )

    m1 = pd.concat(all_m1, ignore_index=True).sort_values("ts").drop_duplicates("ts")
    out = {}
    for tf, rule in (("m5", "5min"), ("m15", "15min")):
        resampled = _resample(m1, rule)
        path = HISTORICAL_DIR / f"{pair}_{tf}_{source}.parquet"
        resampled.to_parquet(path, index=False)
        out[tf] = path
    return out


def load_history(pair: str, tf: str, source: str = "dukascopy") -> pd.DataFrame:
    path = HISTORICAL_DIR / f"{pair}_{tf}_{source}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"No historical data at {path} — run download_history() first")
    return pd.read_parquet(path)


if __name__ == "__main__":
    import sys
    pair = sys.argv[1] if len(sys.argv) > 1 else "EURUSD"
    print(f"Downloading {pair} — 12 months, M5+M15, source=dukascopy ...")
    try:
        paths = download_history(pair, months=12)
        print("Saved:", paths)
    except RuntimeError as e:
        print(f"FAILED: {e}")
        sys.exit(1)
