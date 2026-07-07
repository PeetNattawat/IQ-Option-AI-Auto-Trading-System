"""
IQ Option Auto-Trading Engine
EMA + RSI + ATR + Volume Strategy
Runs locally on Python, serves dashboard via WebSocket
"""

import asyncio
import json
import logging
import time
import os
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, asdict
from collections import deque

import pandas as pd
import numpy as np
from iqoptionapi.stable_api import IQ_Option
from iqoptionapi.constants import ACTIVES

# Numeric active_id -> asset name (e.g. 76 -> "EURUSD-OTC"), built once from the static table
ACTIVE_NAMES = {v: k for k, v in ACTIVES.items()}
import websockets
import threading
from dotenv import load_dotenv

load_dotenv()
# Ensure runtime dirs exist before logging is configured (fresh clones have no logs/ data/),
# and use UTF-8 so Thai text + emoji don't crash the handlers on any platform.
os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)
try:
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/trading.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Sentinel returned by _place_order when the broker rejects an order because the pair
# is "not available at the moment" — caller uses this to skip to the next signal
# without counting the attempt as a loss or affecting Martingale.
_ORDER_UNAVAILABLE = object()

# ─────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────
@dataclass
class TradingConfig:
    email: str = os.getenv("IQ_EMAIL", "")
    password: str = os.getenv("IQ_PASSWORD", "")
    account_type: str = os.getenv("IQ_ACCOUNT", "PRACTICE")  # PRACTICE or REAL

    # Assets to monitor
    assets: list = None
    timeframe: int = 60          # seconds: 60=M1, 300=M5, 900=M15, 1800=M30
    # A/B candidate: M15 (timeframe=900) with expiry_minutes=30-45 for slower trend signals
    candles_history: int = 250   # how many candles to load (must be >= ema_trend + 10)

    # Signal thresholds
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 50
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    # Tightened RSI zones (item 5): narrow the scoring window to reduce chop entries
    rsi_call_min: float = 52.0   # was 50
    rsi_call_max: float = 63.0   # was 70
    rsi_put_min: float = 37.0    # was 30
    rsi_put_max: float = 48.0    # was 50
    atr_period: int = 14
    atr_multiplier: float = 1.5  # ATR must be > avg * multiplier for high vol
    # ATR hard gate — normalized as % of close price (works across all pairs)
    # atr_pct = ATR(14) / close
    # FLOOR: ตลาดต้องมีแรงพอ (กรอง dead market/no-move)
    # CEILING: ตลาดต้องไม่บ้าเกิน (กรอง news spike / blow-out candle)
    atr_floor_pct: float = 0.0003   # 0.03% — ต่ำกว่านี้ตลาดนิ่งเกินไป
    atr_ceiling_pct: float = 0.0035  # 0.35% — สูงกว่านี้ผันผวนเกินจะคาดเดาได้
    volume_period: int = 20
    volume_multiplier: float = 1.2  # Volume must be > avg * multiplier

    # Direction filters (anti-chop / anti-bias)
    adx_min: float = 28.0        # raised back to 28: requires a developing trend and filters choppy
                                 # low-ADX setups that were losing Martingale recovery stakes.
    dir_margin: float = 30.0     # was 15 — dominant side must beat the other by this many points (item 4)

    # Trade settings
    trade_amount: float = 50.0   # base stake per trade (account currency)
    expiry_minutes: int = 15     # binary — snaps to the next :00/:15/:30/:45 quarter-hour
    max_trades_per_hour: int = 12
    max_consecutive_losses: int = 4
    loss_cooldown_minutes: int = 30  # after max_consecutive_losses: pause new entries this long, then auto-resume
    confidence_threshold: float = 70.0  # min score to trade
    # Deadlock breaker: if a trade can't be resolved (e.g. IQ connection dropped) by this many
    # minutes past its expiry, force-expire it so it stops occupying a max_open_positions slot.
    stale_open_minutes: int = 30

    # Martingale money management
    # When off, every auto trade uses the flat trade_amount stake.
    martingale_enabled: bool = True
    martingale_base: float = 50.0       # step 1 stake
    martingale_multiplier: float = 2.0  # each loss multiplies the next stake
    martingale_max_steps: int = 4       # 50 -> 100 -> 200 -> 400, then reset

    # Risk limits (dashboard counters)
    max_open_positions: int = 1   # one open auto trade at a time (max reliability; was 3 concurrent lanes)
    max_trades_per_day: int = 20
    daily_profit_target: float = 200.0
    daily_loss_limit: float = 150.0   # 3 × $50 stake (item 3); 0 = disabled

    # Asset discovery
    auto_discover_assets: bool = False  # scan all open forex pairs (binary/turbo/digital)
    max_assets: int = 999               # cap when auto-discovering (999 = effectively no cap)
    trade_otc: bool = False             # NEVER trade OTC (synthetic) pairs — real forex only
    trade_digital: bool = True          # also scan/trade DIGITAL options — real forex usually lives here
    min_payout: float = 0.70            # only trade pairs paying at least this (e.g. 0.70 = 70%)

    def __post_init__(self):
        if self.assets is None:
            self.assets = ["EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "EURGBP"]
        # name -> instrument kind ("digital" | "binary" | "turbo"), filled by resolve_assets
        self.asset_kind = {}


# ─────────────────────────────────────────
#  BAND HELPERS  (items 6 & 7)
# ─────────────────────────────────────────
def adx_band(adx: float) -> str:
    """Classify ADX into coarse bands for bucket logging and win-rate veto.
    Trades are only placed when adx >= adx_min (30), so the first band won't appear
    in live trade records — it exists so the band function is complete for analysis."""
    if adx < 28:
        return "lt28"
    if adx < 34:
        return "28-34"
    if adx < 40:
        return "34-40"
    return "40+"


def rsi_band(rsi: float) -> str:
    """Classify RSI into 5-point bands (e.g. 50.0-54.9 -> '50-55').
    Coarse enough to accumulate samples quickly as the trade count grows."""
    bucket = int(rsi // 5) * 5
    return f"{bucket}-{bucket + 5}"


def signal_bucket_key(asset: str, side: str, adx: float, rsi: float) -> str:
    """Unique string key for the (asset, direction, ADX band, RSI band) bucket."""
    return f"{asset}|{side}|{adx_band(adx)}|{rsi_band(rsi)}"


def compute_bucket_winrates(trades: list) -> dict:
    """Compute realized win-rate per setup bucket from closed AUTO trades.
    Returns {bucket_key: {"wins": int, "total": int, "winrate": float}}."""
    buckets: dict = {}
    for t in trades:
        if t.get("source") != "auto" or t.get("status") != "closed":
            continue
        adx = t.get("adx")
        rsi = t.get("rsi")
        if adx is None or rsi is None:
            continue
        key = signal_bucket_key(
            t.get("asset", ""),
            t.get("direction", ""),
            float(adx),
            float(rsi),
        )
        rec = buckets.setdefault(key, {"wins": 0, "total": 0})
        rec["total"] += 1
        if t.get("result") == "WIN":
            rec["wins"] += 1
    for rec in buckets.values():
        rec["winrate"] = round(rec["wins"] / rec["total"], 4) if rec["total"] else 0.0
    return buckets


# ─────────────────────────────────────────
#  INDICATOR CALCULATIONS
# ─────────────────────────────────────────
class IndicatorEngine:
    """Calculate all technical indicators from OHLCV data"""

    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def rsi(series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
        avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high = df["high"]
        low = df["low"]
        close = df["close"].shift(1)
        tr = pd.concat([
            high - low,
            (high - close).abs(),
            (low - close).abs()
        ], axis=1).max(axis=1)
        return tr.ewm(com=period - 1, adjust=False).mean()

    @staticmethod
    def macd(series: pd.Series, fast=12, slow=26, signal=9):
        ema_fast = series.ewm(span=fast, adjust=False).mean()
        ema_slow = series.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    @staticmethod
    def bollinger_bands(series: pd.Series, period=20, std_dev=2):
        sma = series.rolling(period).mean()
        std = series.rolling(period).std()
        upper = sma + (std * std_dev)
        lower = sma - (std * std_dev)
        return upper, sma, lower

    @staticmethod
    def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high = df["high"]
        low = df["low"]
        close = df["close"]
        plus_dm = high.diff()
        minus_dm = -low.diff()
        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
        tr = IndicatorEngine.atr(df, period)
        plus_di = 100 * (plus_dm.ewm(com=period - 1).mean() / tr)
        minus_di = 100 * (minus_dm.ewm(com=period - 1).mean() / tr)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
        return dx.ewm(com=period - 1).mean()

    @staticmethod
    def compute_all(df: pd.DataFrame, cfg: TradingConfig) -> pd.DataFrame:
        df = df.copy()
        close = df["close"]
        volume = df["volume"] if "volume" in df.columns else pd.Series(np.ones(len(df)), index=df.index)

        df["ema_fast"] = IndicatorEngine.ema(close, cfg.ema_fast)
        df["ema_slow"] = IndicatorEngine.ema(close, cfg.ema_slow)
        df["ema_trend"] = IndicatorEngine.ema(close, cfg.ema_trend)
        df["rsi"] = IndicatorEngine.rsi(close, cfg.rsi_period)
        df["atr"] = IndicatorEngine.atr(df, cfg.atr_period)
        df["atr_avg"] = df["atr"].rolling(50).mean()
        df["volume"] = volume
        df["volume_avg"] = volume.rolling(cfg.volume_period).mean()
        df["macd"], df["macd_signal"], df["macd_hist"] = IndicatorEngine.macd(close)
        df["bb_upper"], df["bb_mid"], df["bb_lower"] = IndicatorEngine.bollinger_bands(close)
        df["adx"] = IndicatorEngine.adx(df)
        return df


# ─────────────────────────────────────────
#  SIGNAL SCORING ENGINE
# ─────────────────────────────────────────
@dataclass
class SignalResult:
    asset: str
    timeframe: int
    signal: str            # CALL / PUT / HOLD
    confidence: float      # 0-100
    score_breakdown: dict
    reasons: list
    entry_price: float
    rsi: float
    atr: float
    ema_fast: float
    ema_slow: float
    ema_trend: float
    adx: float
    macd_hist: float
    timestamp: str


class SignalEngine:
    """Score-based signal generator: each condition contributes to total score"""

    def __init__(self, cfg: TradingConfig):
        self.cfg = cfg

    def evaluate(self, df: pd.DataFrame, asset: str) -> SignalResult:
        if len(df) < self.cfg.ema_trend + 10:
            return self._no_signal(asset, "Not enough data")

        row = df.iloc[-1]
        prev = df.iloc[-2]

        # Directional points only (max 45): EMA + RSI only.
        # ATR / ADX / Volume are quality gates (not scored to a side).
        # MACD and Bollinger Bands removed — strategy is EMA + RSI + ATR only.
        MAX_DIR = 45
        call = 0
        put = 0
        call_reasons = []
        put_reasons = []
        breakdown_call = {}
        breakdown_put = {}

        # ── EMA TREND ALIGNMENT (25 / partial 10) ──
        ema_bull = row["ema_fast"] > row["ema_slow"] > row["ema_trend"]
        ema_bear = row["ema_fast"] < row["ema_slow"] < row["ema_trend"]
        if ema_bull:
            call += 25; breakdown_call["ema_alignment"] = 25
            call_reasons.append(f"EMA เรียงขาขึ้น {row['ema_fast']:.5f} > {row['ema_slow']:.5f} > {row['ema_trend']:.5f}")
        elif row["ema_fast"] > row["ema_slow"]:
            call += 10; breakdown_call["ema_partial"] = 10
            call_reasons.append("EMA fast > slow (ขาขึ้นบางส่วน)")
        if ema_bear:
            put += 25; breakdown_put["ema_alignment"] = 25
            put_reasons.append(f"EMA เรียงขาลง {row['ema_fast']:.5f} < {row['ema_slow']:.5f} < {row['ema_trend']:.5f}")
        elif row["ema_fast"] < row["ema_slow"]:
            put += 10; breakdown_put["ema_partial"] = 10
            put_reasons.append("EMA fast < slow (ขาลงบางส่วน)")

        # ── RSI (20 / extreme reversal 10) ──
        # Reversal (extreme RSI) อนุญาตเฉพาะเมื่อ EMA ไม่ full aligned เท่านั้น
        # ถ้า EMA full aligned ขาขึ้น → ห้าม PUT reversal (สวนเทรนด์)
        # ถ้า EMA full aligned ขาลง → ห้าม CALL reversal (สวนเทรนด์)
        rsi = row["rsi"]
        if self.cfg.rsi_call_min <= rsi <= self.cfg.rsi_call_max:
            call += 20; breakdown_call["rsi"] = 20
            call_reasons.append(f"RSI {rsi:.1f} โซนกระทิง [{self.cfg.rsi_call_min:.0f}-{self.cfg.rsi_call_max:.0f}]")
        elif self.cfg.rsi_put_min <= rsi <= self.cfg.rsi_put_max:
            put += 20; breakdown_put["rsi"] = 20
            put_reasons.append(f"RSI {rsi:.1f} โซนหมี [{self.cfg.rsi_put_min:.0f}-{self.cfg.rsi_put_max:.0f}]")
        elif rsi > self.cfg.rsi_overbought and not ema_bull:
            # RSI overbought reversal → PUT ได้เฉพาะตอนที่ EMA ไม่ได้เรียงขาขึ้นสมบูรณ์
            put += 10; breakdown_put["rsi_extreme"] = 10
            put_reasons.append(f"RSI {rsi:.1f} overbought — ลุ้นกลับตัวลง (EMA ไม่ full bull)")
        elif rsi < self.cfg.rsi_oversold and not ema_bear:
            # RSI oversold reversal → CALL ได้เฉพาะตอนที่ EMA ไม่ได้เรียงขาลงสมบูรณ์
            call += 10; breakdown_call["rsi_extreme"] = 10
            call_reasons.append(f"RSI {rsi:.1f} oversold — ลุ้นเด้งขึ้น (EMA ไม่ full bear)")

        macd_hist = row["macd_hist"]
        prev_macd = prev["macd_hist"]
        price = row["close"]

        # ── ATR HARD GATE — normalized % of close (cross-pair safe) ──
        atr = row["atr"]
        atr_avg = row["atr_avg"] if not pd.isna(row["atr_avg"]) else atr
        atr_high = atr > atr_avg * self.cfg.atr_multiplier
        atr_pct = atr / price if price > 0 else 0
        atr_floor_ok = atr_pct >= self.cfg.atr_floor_pct
        atr_ceiling_ok = atr_pct <= self.cfg.atr_ceiling_pct
        vol = row["volume"]
        vol_avg = row["volume_avg"] if not pd.isna(row["volume_avg"]) else vol
        vol_high = vol > vol_avg * self.cfg.volume_multiplier
        adx = row["adx"]
        adx_ok = adx >= self.cfg.adx_min

        # ATR gate ตรวจก่อน decision — ถ้าไม่ผ่าน ไม่ต้องคำนวณต่อ
        if not atr_floor_ok:
            return self._hold(asset, row, rsi, atr, adx, macd_hist,
                              f"ATR {atr_pct*100:.4f}% < floor {self.cfg.atr_floor_pct*100:.4f}% — ตลาดนิ่งเกินไป ไม่เทรด")
        if not atr_ceiling_ok:
            return self._hold(asset, row, rsi, atr, adx, macd_hist,
                              f"ATR {atr_pct*100:.4f}% > ceiling {self.cfg.atr_ceiling_pct*100:.4f}% — ผันผวนเกินไป ไม่เทรด")

        # ── DECISION ──
        # 1) need a clear directional winner (no CALL-on-tie bias)
        if call > put:
            side, dom, reasons, breakdown = "CALL", call, call_reasons, breakdown_call
        elif put > call:
            side, dom, reasons, breakdown = "PUT", put, put_reasons, breakdown_put
        else:
            return self._hold(asset, row, rsi, atr, adx, macd_hist, "สองทิศคะแนนเท่ากัน — ไม่เทรด")

        confidence = round(dom / MAX_DIR * 100, 1)   # directional edge, 0-100
        margin = dom - min(call, put)
        side_label = side

        # 2) hard filters — any failure => HOLD
        gates = []
        if not adx_ok:
            gates.append(f"ADX {adx:.1f} < {self.cfg.adx_min:.0f} — เทรนด์ไม่แรงพอ ไม่เทรด")
        if margin < self.cfg.dir_margin:
            gates.append(f"ทิศไม่ชัด (นำแค่ {margin} แต้ม)")
        if confidence < self.cfg.confidence_threshold:
            gates.append(f"confidence {confidence:.0f}% < {self.cfg.confidence_threshold:.0f}%")

        # ── RSI HARD VETO: extremes that contradict the direction ──
        # CALL vetoed when rsi > 66 (too overbought for a new long entry)
        # PUT  vetoed when rsi < 34 (too oversold for a new short entry)
        if side == "CALL" and rsi > 66:
            gates.append(f"RSI {rsi:.1f} > 66 — veto CALL (overbought)")
        elif side == "PUT" and rsi < 34:
            gates.append(f"RSI {rsi:.1f} < 34 — veto PUT (oversold)")

        if gates:
            return self._hold(asset, row, rsi, atr, adx, macd_hist,
                              reasons[:2] + [f"⛔ {side_label} ถูกกรอง: " + " · ".join(gates)], confidence)

        # passed — annotate quality
        reasons.append(f"ATR {atr_pct*100:.4f}% ✅ ผ่าน gate [{self.cfg.atr_floor_pct*100:.4f}%–{self.cfg.atr_ceiling_pct*100:.4f}%]")
        if vol_high: reasons.append("Volume สูงกว่าค่าเฉลี่ย — ยืนยันแรงซื้อขาย")

        return SignalResult(
            asset=asset, timeframe=self.cfg.timeframe,
            signal=side, confidence=min(confidence, 100),
            score_breakdown=breakdown, reasons=reasons,
            entry_price=price, rsi=rsi, atr=atr,
            ema_fast=row["ema_fast"], ema_slow=row["ema_slow"],
            ema_trend=row["ema_trend"], adx=adx, macd_hist=macd_hist,
            timestamp=datetime.now().isoformat()
        )

    def _hold(self, asset, row, rsi, atr, adx, macd_hist, reasons, confidence=0) -> SignalResult:
        if isinstance(reasons, str):
            reasons = [reasons]
        return SignalResult(
            asset=asset, timeframe=self.cfg.timeframe, signal="HOLD", confidence=confidence,
            score_breakdown={}, reasons=reasons[:4], entry_price=row["close"],
            rsi=rsi, atr=atr, ema_fast=row["ema_fast"], ema_slow=row["ema_slow"],
            ema_trend=row["ema_trend"], adx=adx, macd_hist=macd_hist,
            timestamp=datetime.now().isoformat()
        )

    def _no_signal(self, asset, reason):
        return SignalResult(
            asset=asset, timeframe=0, signal="HOLD", confidence=0,
            score_breakdown={}, reasons=[reason], entry_price=0,
            rsi=0, atr=0, ema_fast=0, ema_slow=0, ema_trend=0, adx=0, macd_hist=0,
            timestamp=datetime.now().isoformat()
        )


# ─────────────────────────────────────────
#  TRADE MANAGER
# ─────────────────────────────────────────
class TradeManager:
    """Manages trade execution, risk rules, and result tracking"""

    # Minimum closed samples before the bucket veto activates for a given bucket (item 6)
    BUCKET_MIN_SAMPLES = 10
    BUCKET_MIN_WINRATE = 0.50  # veto if winrate is below this threshold

    def __init__(self, cfg: TradingConfig, iq: IQ_Option):
        self.cfg = cfg
        self.iq = iq
        self.trades = self._load_trades()   # list of trade dicts (persisted across restarts)
        self.active_orders = {t["id"]: t for t in self.trades if t.get("status") == "open" and t.get("id")}
        self.consecutive_losses = 0
        self.hourly_trades = deque()
        self.learning_rules = self._load_learning_rules()
        self._lock = threading.RLock()  # guards trades/active_orders across the trading & sync threads
        # Trades that just closed and still need a Telegram alert. ANY close path (the 5-min
        # trading cycle, the 15s sync loop, external/web sync) appends here; the alert sender
        # drains it. This decouples alerting from which loop closed the trade — the old
        # snapshot-diff approach missed alerts when run_cycle resolved a trade before the
        # 15s loop's old_results snapshot was taken.
        self.pending_alerts = []  # list of closed trade dicts awaiting a Telegram alert
        # Global Martingale ladder: one shared step counter across all assets.
        # Persisted to disk so restart doesn't reset mid-recovery.
        self.current_step: int = self._load_step()
        # Live active_id -> name map (filled by resolve_assets from get_all_init_v2),
        # so platform-opened trades show real pair names even for new ids the static table lacks.
        self._live_active_names = {}
        # Cached bucket win-rates; rebuilt each time execute_trade is called (cheap at <1k trades)
        self._bucket_cache: dict = {}
        # Hard expiry lock: epoch time before which NO new auto trade is allowed.
        # Independent of active_orders — survives API quirks that prematurely clear a position.
        self._auto_locked_until: float = 0.0

    def resolve_active_name(self, active_id) -> str:
        """Best-effort active_id -> readable pair name. Tries the live map, then the
        static table, then a live server lookup, finally the raw id."""
        if active_id is None:
            return "?"
        live = self._live_active_names or {}
        name = live.get(active_id) or live.get(str(active_id)) or ACTIVE_NAMES.get(active_id)
        if name:
            return name
        try:
            n = self.iq.get_name_by_activeId(active_id)
            if n:
                return str(n).split(".")[-1]
        except Exception:
            pass
        return str(active_id)

    def _load_trades(self) -> list:
        try:
            with open("data/trades.json") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _load_learning_rules(self) -> list:
        try:
            with open("data/learning_rules.json") as f:
                return json.load(f)
        except FileNotFoundError:
            return []

    def _load_step(self) -> int:
        try:
            with open("data/martingale_state.json") as f:
                data = json.load(f)
                # New format: {"step": N}
                if "step" in data:
                    step = int(data["step"])
                    logger.info(f"[MARTINGALE] Loaded global step: {step}")
                    return step
                # Legacy per-asset format: {"EURUSD": 1, ...} — take the max active step
                if isinstance(data, dict) and data:
                    step = max(int(v) for v in data.values())
                    logger.info(f"[MARTINGALE] Migrated legacy per-asset state → global step: {step}")
                    return step
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return 0

    def _save_step(self):
        os.makedirs("data", exist_ok=True)
        with open("data/martingale_state.json", "w") as f:
            json.dump({"step": self.current_step}, f, indent=2)

    def today_trades(self) -> list:
        today = datetime.now().date().isoformat()
        return [t for t in self.trades if str(t.get("open_time", ""))[:10] == today]

    def today_pnl(self) -> float:
        return round(sum(t.get("pnl") or 0 for t in self.today_trades() if t.get("status") == "closed"), 2)

    # ── Martingale (BOT/auto trades only) — global single ladder ──
    def martingale_sequence(self) -> list:
        base, mult, steps = self.cfg.martingale_base, self.cfg.martingale_multiplier, self.cfg.martingale_max_steps
        return [round(base * (mult ** i), 2) for i in range(steps)]

    def next_auto_stake(self, asset: str = "") -> float:
        """Stake for the next bot trade.
        When martingale is OFF, always returns the flat base stake (trade_amount).
        When ON, returns the current global ladder step amount."""
        if not self.cfg.martingale_enabled:
            return self.cfg.trade_amount
        seq = self.martingale_sequence()
        step = min(self.current_step, len(seq) - 1)
        return seq[step]

    def _advance_martingale(self, asset: str, result: str):
        """After a BOT trade closes: advance global ladder on loss, reset on win.
        Reaching the final step then losing resets to base (accept the drawdown).
        No-op when martingale is disabled."""
        if not self.cfg.martingale_enabled:
            return
        if result == "LOSS":
            self.current_step += 1
            if self.current_step >= self.cfg.martingale_max_steps:
                logger.warning(f"[MARTINGALE] {asset}: final step lost — reset ladder to base")
                self.current_step = 0
            else:
                logger.info(f"[MARTINGALE] {asset}: loss — step → {self.current_step + 1}/{self.cfg.martingale_max_steps}")
        elif result == "WIN":
            if self.current_step:
                logger.info(f"[MARTINGALE] {asset}: win — reset ladder to base")
            self.current_step = 0
        # EQUAL: keep the same step (stake returned, no progression)
        self._save_step()

    def open_auto_count(self) -> int:
        return sum(1 for t in self.active_orders.values() if t.get("source") == "auto")

    def open_auto_assets(self) -> set:
        return {t.get("asset") for t in self.active_orders.values() if t.get("source") == "auto"}

    def can_trade(self) -> tuple[bool, str]:
        now = time.time()
        # Hard expiry lock — blocks new entries until the last auto trade's expiry has passed.
        # Survives API quirks that prematurely remove a trade from active_orders.
        if now < self._auto_locked_until:
            remaining = int((self._auto_locked_until - now) / 60) + 1
            return False, f"รอไม้ล่าสุดหมดอายุก่อน (~{remaining} นาที)"
        # Clear old hourly records
        while self.hourly_trades and now - self.hourly_trades[0] > 3600:
            self.hourly_trades.popleft()
        if len(self.hourly_trades) >= self.cfg.max_trades_per_hour:
            return False, f"Max {self.cfg.max_trades_per_hour} trades/hour reached"
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            return False, f"Consecutive losses {self.consecutive_losses} — cooling down"
        if len(self.active_orders) >= self.cfg.max_open_positions:
            return False, f"Max {self.cfg.max_open_positions} open positions reached"
        if self.cfg.daily_loss_limit > 0 and -self.today_pnl() >= self.cfg.daily_loss_limit:
            return False, f"Daily loss limit {self.cfg.daily_loss_limit} reached"
        return True, "OK"

    def apply_learning_veto(self, signal: SignalResult) -> tuple[bool, str]:
        """Check if any learned rules veto this trade"""
        for rule in self.learning_rules:
            if not rule.get("active", True):
                continue
            condition = rule.get("condition", {})
            # Example: {"signal": "CALL", "rsi_gt": 80} -> veto
            if condition.get("signal") and condition["signal"] != signal.signal:
                continue
            if condition.get("rsi_gt") and signal.rsi <= condition["rsi_gt"]:
                continue
            if condition.get("rsi_lt") and signal.rsi >= condition["rsi_lt"]:
                continue
            if condition.get("adx_lt") and signal.adx >= condition["adx_lt"]:
                continue
            if condition.get("atr_low") and signal.atr > signal.atr * 0.8:
                continue
            return True, f"Vetoed by learning rule: {rule.get('reason', 'unknown')}"
        return False, ""

    def apply_bucket_veto(self, signal: SignalResult) -> tuple[bool, str]:
        """Setup-bucket realized win-rate veto (item 6).
        Vetoes a trade when the (asset, side, ADX-band, RSI-band) bucket has >= 10 closed
        AUTO samples AND a win-rate below 50%.  With sparse data (<10 per bucket) this is
        inert by design — it only activates once we have meaningful evidence."""
        key = signal_bucket_key(signal.asset, signal.signal, signal.adx, signal.rsi)
        rec = self._bucket_cache.get(key)
        if rec is None:
            return False, ""
        if rec["total"] >= self.BUCKET_MIN_SAMPLES and rec["winrate"] < self.BUCKET_MIN_WINRATE:
            return True, (
                f"Bucket veto [{key}]: {rec['wins']}/{rec['total']} wins "
                f"({rec['winrate']*100:.0f}% < {self.BUCKET_MIN_WINRATE*100:.0f}%)"
            )
        return False, ""

    def _refresh_bucket_cache(self):
        """Recompute bucket win-rates from trade history.  Called before each execute_trade."""
        self._bucket_cache = compute_bucket_winrates(self.trades)

    def _place_order(self, asset: str, direction: str, amount: float, meta: dict):
        """Place an order on IQ Option and record it. direction: CALL/PUT.
        Returns trade dict on success, _ORDER_UNAVAILABLE sentinel when the broker
        rejects because the pair is unavailable, or None for all other failures.
        Routes to digital-spot for assets resolved as 'digital' (real forex usually trades
        only on digital), otherwise to the binary endpoint.
        Persists adx_band and rsi_band for bucket analysis (item 7)."""
        kind = (getattr(self.cfg, "asset_kind", None) or {}).get(asset, "binary")
        action = "call" if direction.upper() == "CALL" else "put"
        duration = self.cfg.expiry_minutes
        logger.info(f"[TRADE] Placing {action.upper()} on {asset} [{kind}] | amount {amount} | source {meta.get('source')}")
        def _attempt_buy() -> tuple:
            """Submit a single buy call and wait up to 30s. Returns (check, order_id) or raises.
            Does NOT use ThreadPoolExecutor as context manager — shutdown(wait=True) would block
            forever if buy_digital_spot hangs after the timeout fires."""
            import concurrent.futures as _cf
            _ex = _cf.ThreadPoolExecutor(max_workers=1)
            try:
                if kind == "digital":
                    _fut = _ex.submit(self.iq.buy_digital_spot, asset, amount, action, duration)
                else:
                    _fut = _ex.submit(self.iq.buy, amount, asset, action, duration)
                result = _fut.result(timeout=30)
                _ex.shutdown(wait=False)
                return result
            except _cf.TimeoutError:
                _ex.shutdown(wait=False)  # release immediately — hung thread runs in background
                raise

        try:
            import concurrent.futures as _cf
            try:
                check, order_id = _attempt_buy()
            except _cf.TimeoutError:
                logger.warning(
                    f"[TRADE] TIMEOUT placing {asset} [{kind}] after 30s — attempting reconnect and retry"
                )
                # Reconnect once
                try:
                    _rc, _rr = self.iq.connect()
                    if _rc:
                        try:
                            self.iq.change_balance(self.cfg.account_type)
                        except Exception:
                            pass
                        logger.info("[TRADE] Reconnected successfully — retrying order")
                    else:
                        logger.error(f"[TRADE] Reconnect failed ({_rr}) — skipping {asset}")
                        return None
                except Exception as _re:
                    logger.error(f"[TRADE] Reconnect error: {_re} — skipping {asset}")
                    return None
                # Single retry
                try:
                    check, order_id = _attempt_buy()
                except _cf.TimeoutError:
                    logger.error(
                        f"[TRADE] TIMEOUT on retry placing {asset} [{kind}] after 30s — skipping"
                    )
                    return None
            if not check:
                _rej_msg = str(order_id).lower() if order_id else ""
                if "not available" in _rej_msg:
                    logger.warning(
                        f"[TRADE] {asset} [{kind}] not available at the moment — "
                        "will try next signal this cycle"
                    )
                    return _ORDER_UNAVAILABLE
                logger.error(f"[TRADE] Order failed for {asset} [{kind}] (broker rejected: {order_id})")
                return None
          # fall through to record under lock
        except Exception as e:
            logger.error(f"[TRADE] Exception: {e}")
            return None

        # Enrich meta with band dimensions for bucket logging (item 7)
        adx_val = meta.get("adx")
        rsi_val = meta.get("rsi")
        if adx_val is not None and rsi_val is not None:
            meta["adx_band"] = adx_band(float(adx_val))
            meta["rsi_band"] = rsi_band(float(rsi_val))

        with self._lock:
            trade = {
                "id": order_id,
                "asset": asset,
                "kind": kind,
                "direction": direction.upper(),
                "amount": amount,
                "open_time": datetime.now().isoformat(),
                "expiry": duration,
                "status": "open",
                "pnl": None,
                "result": None,
                **meta,
            }
            self.active_orders[order_id] = trade
            self.trades.append(trade)
            self.hourly_trades.append(time.time())
            # Set hard expiry lock when this is an auto trade.
            # Prevents a new auto entry until expiry + 60s buffer,
            # even if active_orders is cleared early by an API quirk.
            if meta.get("source") == "auto":
                # Buffer 5 min accounts for IQ Option's quarter-hour snap (:00/:15/:30/:45)
                # which can make the actual expiry up to ~15 min later than placement time.
                self._auto_locked_until = time.time() + (duration * 60) + 300
                logger.info(f"[LOCK] Auto trade lock set — ไม่เปิดไม้ใหม่จนกว่าจะครบ {duration} นาที (+5 นาที buffer)")
            self._save_trades()
            logger.info(f"[TRADE] Order placed: ID {order_id}")
            return trade

    def execute_trade(self, signal: SignalResult, source: str = "auto"):
        """Place an auto trade for the given signal.
        Returns:
          - trade dict on success
          - _ORDER_UNAVAILABLE sentinel if the broker rejects the pair as unavailable
            (caller should skip to next signal, no Martingale / loss counters affected)
          - None for all other failures (risk block, veto, order error)
        """
        can, reason = self.can_trade()
        if not can:
            logger.warning(f"[RISK] Trade blocked: {reason}")
            return None

        # One open auto trade per asset at a time (each asset = its own Martingale lane)
        if signal.asset in self.open_auto_assets():
            return None

        vetoed, veto_reason = self.apply_learning_veto(signal)
        if vetoed:
            logger.warning(f"[LEARNING] {veto_reason}")
            return None

        # Bucket win-rate veto (item 6) — refresh cache from current trade history first
        self._refresh_bucket_cache()
        bucket_vetoed, bucket_reason = self.apply_bucket_veto(signal)
        if bucket_vetoed:
            logger.warning(f"[BUCKET-VETO] {bucket_reason}")
            return None

        step = self.current_step
        stake = self.next_auto_stake(signal.asset)
        step_info = (f" | Martingale step {step + 1}/{self.cfg.martingale_max_steps}"
                     if self.cfg.martingale_enabled else "")
        logger.info(f"[TRADE] Auto stake {stake}{step_info}")
        return self._place_order(signal.asset, signal.signal, stake, {
            "entry": signal.entry_price,
            "confidence": signal.confidence,
            "reasons": signal.reasons,
            "rsi": signal.rsi,
            "atr": signal.atr,
            "adx": signal.adx,
            "source": source,
            "mg_step": (step + 1) if self.cfg.martingale_enabled else None,
        })

    def execute_manual(self, asset: str, direction: str, amount: float = None) -> Optional[dict]:
        """Manual trade from dashboard — bypasses signal confidence but respects open-position cap"""
        if direction.upper() not in ("CALL", "PUT"):
            logger.warning(f"[TRADE] Invalid manual direction: {direction}")
            return None
        if len(self.active_orders) >= self.cfg.max_open_positions:
            logger.warning(f"[RISK] Manual trade blocked: max {self.cfg.max_open_positions} open positions")
            return None
        return self._place_order(asset, direction, amount or self.cfg.trade_amount, {
            "entry": 0,
            "confidence": None,
            "reasons": ["manual trade from dashboard"],
            "source": "manual",
        })

    def _fetch_closed_positions(self) -> dict:
        """Closed option positions from the portfolio history, keyed by order id (external_id)."""
        by_id = {}
        instrument_types = {"turbo-option"}
        if any((t.get("expiry") or self.cfg.expiry_minutes) > 5 for t in self.active_orders.values()):
            instrument_types.add("binary-option")
        if any(t.get("kind") == "digital" for t in self.active_orders.values()):
            instrument_types.add("digital-option")
        import concurrent.futures as _cf
        for itype in instrument_types:
            try:
                with _cf.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(self.iq.get_position_history_v2, itype, 50, 0, 0, 0)
                    try:
                        check, msg = fut.result(timeout=8)
                    except _cf.TimeoutError:
                        logger.warning(f"[RESULT] position history ({itype}) timed out — skipping")
                        continue
                if not check:
                    continue
                for p in (msg or {}).get("positions", []):
                    ext = p.get("external_id")
                    if ext is not None:
                        by_id[str(ext)] = p
            except Exception as e:
                logger.warning(f"[RESULT] position history ({itype}) failed: {e}")
        return by_id

    def check_results(self):
        """Poll IQ Option for results of active orders via portfolio position history.
        (The library's check_win_v3 / get_optioninfo_v2 rely on a deprecated endpoint
        that the server no longer answers, so they block forever.)"""
        # Only bother once at least one order is past its expiry
        now = datetime.now()
        with self._lock:
            due = []
            for order_id, trade in self.active_orders.items():
                try:
                    opened = datetime.fromisoformat(str(trade["open_time"]))
                except ValueError:
                    due.append(order_id)
                    continue
                if now >= opened + timedelta(minutes=trade.get("expiry") or self.cfg.expiry_minutes, seconds=10):
                    due.append(order_id)
        if not due:
            return

        by_id = self._fetch_closed_positions()  # network — kept outside the lock

        with self._lock:
            closed = []
            expired = []
            for order_id in due:
                trade = self.active_orders.get(order_id)
                if not trade:
                    continue  # closed by the sync loop meanwhile
                p = by_id.get(str(order_id))
                if not p or p.get("status") != "closed":
                    # Not in closed history yet. If it's stuck far past expiry (e.g. IQ
                    # connection was down when it should have resolved), force-expire it so
                    # it stops deadlocking the open-position slot. Otherwise retry next cycle.
                    if self.cfg.stale_open_minutes > 0:
                        try:
                            opened = datetime.fromisoformat(str(trade["open_time"]))
                        except (ValueError, KeyError):
                            opened = None
                        expiry = trade.get("expiry") or self.cfg.expiry_minutes
                        if opened and now >= opened + timedelta(minutes=expiry + self.cfg.stale_open_minutes):
                            self._expire_stale(trade)
                            expired.append(order_id)
                    continue  # not in closed history yet — try next cycle
                self._apply_close(trade, p.get("close_reason"), p.get("pnl_net", p.get("pnl")))
                closed.append(order_id)

            if closed or expired:
                self._save_trades()
            for oid in closed + expired:
                self.active_orders.pop(oid, None)

    def _apply_close(self, trade: dict, close_reason: str, pnl_raw) -> None:
        """Mark a trade closed from a 'win'/'loose'/'equal' reason + raw pnl value."""
        try:
            pnl = round(float(pnl_raw or 0), 2)
        except (TypeError, ValueError):
            pnl = 0.0
        trade["pnl"] = pnl
        trade["result"] = "WIN" if close_reason == "win" else ("EQUAL" if close_reason == "equal" else "LOSS")
        trade["close_time"] = datetime.now().isoformat()
        trade["status"] = "closed"
        # Risk counter + Martingale ladder track BOT trades only (manual/web don't affect them)
        if trade.get("source") == "auto":
            if trade["result"] == "LOSS":
                self.consecutive_losses += 1
            elif trade["result"] == "WIN":
                self.consecutive_losses = 0
            self._advance_martingale(trade["asset"], trade["result"])
            # Release expiry lock immediately — result is confirmed, next trade can enter now
            self._auto_locked_until = 0.0
            logger.info(f"[LOCK] ปลด lock แล้ว — {trade['asset']} ปิดด้วย {trade['result']}")
        logger.info(f"[RESULT] {trade['asset']} {trade['direction']} -> {trade['result']} | PnL: {pnl:+.2f}")
        self.pending_alerts.append(trade)

    def drain_pending_alerts(self) -> list:
        """Atomically take and clear the queue of just-closed trades awaiting a Telegram
        alert. Called by the alert sender; safe regardless of which loop closed the trade."""
        with self._lock:
            pending = self.pending_alerts
            self.pending_alerts = []
        return pending

    def _expire_stale(self, trade: dict) -> None:
        """Force-close a trade that never resolved (IQ connection drop / missing history).
        Marked 'expired' with no PnL so it doesn't pollute win/loss stats, but it IS removed
        from active_orders so it stops blocking new entries (max_open_positions deadlock)."""
        trade["status"] = "expired"
        trade["result"] = None
        trade["pnl"] = 0.0
        trade["close_time"] = datetime.now().isoformat()
        trade["close_reason"] = "stale_unresolved"
        logger.warning(
            f"[RESULT] {trade.get('asset')} {trade.get('direction')} -> EXPIRED "
            f"(unresolved {self.cfg.stale_open_minutes}m past expiry — slot freed)")
        self.pending_alerts.append(trade)

    def sync_external_trades(self) -> list:
        """Discover option trades opened directly on the IQ Option website/app from the
        realtime portfolio buffer (api.order_async) and merge them into our trade list
        with source='web'. Also refreshes live status of trades we already track.
        Returns the list of newly discovered trades."""
        try:
            order_async = dict(self.iq.api.order_async)
        except Exception:
            return []

        added = []
        with self._lock:
            by_id = {str(t.get("id")): t for t in self.trades if t.get("id")}
            for raw_oid, events in order_async.items():
                pc = events.get("position-changed") if isinstance(events, dict) else None
                if not pc:
                    continue
                msg = pc.get("msg", {}) or {}
                if msg.get("instrument_type") not in ("turbo-option", "binary-option", "digital-option"):
                    continue
                raw = msg.get("raw_event", {}) or {}
                ext = msg.get("external_id") or raw_oid
                is_closed = msg.get("status") == "closed"

                existing = by_id.get(str(ext))
                if existing:
                    # finalize a trade we already track if the platform shows it closed
                    if existing.get("status") == "open" and is_closed:
                        self._apply_close(existing, msg.get("close_reason"), msg.get("pnl_net", msg.get("pnl")))
                        self.active_orders.pop(ext, None)
                        self._save_trades()
                    continue

                # New trade opened outside the bot
                asset = self.resolve_active_name(msg.get("active_id"))
                direction = (raw.get("direction") or "").upper()
                amount = float(msg.get("invest") or raw.get("amount") or 0)
                open_ms = msg.get("open_time") or raw.get("open_time_millisecond")
                try:
                    open_time = datetime.fromtimestamp(open_ms / 1000).isoformat() if open_ms else datetime.now().isoformat()
                except (TypeError, ValueError, OSError):
                    open_time = datetime.now().isoformat()
                exp_sec = raw.get("expiration_time")
                expiry_min = self.cfg.expiry_minutes
                if exp_sec and open_ms:
                    expiry_min = max(1, round((exp_sec - open_ms / 1000) / 60))

                trade = {
                    "id": ext,
                    "asset": asset,
                    "direction": direction if direction in ("CALL", "PUT") else (direction or "?"),
                    "amount": round(amount, 2),
                    "open_time": open_time,
                    "expiry": expiry_min,
                    "status": "open",
                    "pnl": None,
                    "result": None,
                    "entry": 0,
                    "confidence": None,
                    "reasons": ["opened on IQ Option platform"],
                    "source": "web",
                }
                if is_closed:
                    self._apply_close(trade, msg.get("close_reason"), msg.get("pnl_net", msg.get("pnl")))
                else:
                    self.active_orders[ext] = trade
                self.trades.append(trade)
                by_id[str(ext)] = trade
                added.append(trade)
                logger.info(f"[SYNC] External trade {asset} {direction} ${amount:.2f} (id {ext}) status={trade['status']}")

            if added:
                self._save_trades()
        return added

    @staticmethod
    def _stat_block(closed: list) -> dict:
        wins = sum(1 for t in closed if t["result"] == "WIN")
        losses = sum(1 for t in closed if t["result"] == "LOSS")
        equals = sum(1 for t in closed if t["result"] == "EQUAL")
        decided = wins + losses
        pnl = sum(t["pnl"] for t in closed if t["pnl"] is not None)
        return {
            "total": len(closed),
            "wins": wins,
            "losses": losses,
            "equals": equals,
            "win_rate": round(wins / decided * 100, 1) if decided else 0,
            "pnl": round(pnl, 2),
        }

    @staticmethod
    def _is_bot(t: dict) -> bool:
        return t.get("source") == "auto"

    def _stats_by_source(self, closed: list) -> dict:
        """Overall stat block + Bot/Manual split (manual = dashboard manual + web/app trades)."""
        block = self._stat_block(closed)
        block["bot"] = self._stat_block([t for t in closed if self._is_bot(t)])
        block["manual"] = self._stat_block([t for t in closed if not self._is_bot(t)])
        return block

    def get_stats(self) -> dict:
        closed = [t for t in self.trades if t["status"] == "closed"]
        today_closed = [t for t in self.today_trades() if t["status"] == "closed"]
        stats = self._stats_by_source(closed)
        stats["today"] = self._stats_by_source(today_closed)
        stats["consecutive_losses"] = self.consecutive_losses
        stats["active_orders"] = len(self.active_orders)
        stats["martingale"] = {
            "enabled": self.cfg.martingale_enabled,
            "max_steps": self.cfg.martingale_max_steps,
            "sequence": self.martingale_sequence(),
            "base": self.cfg.martingale_base,
            "current_step": self.current_step + 1,  # 1-indexed for display
            "next_stake": self.next_auto_stake(),
        }
        return stats

    def _save_trades(self):
        os.makedirs("data", exist_ok=True)
        with open("data/trades.json", "w") as f:
            json.dump(self.trades, f, indent=2, default=str)


# ─────────────────────────────────────────
#  LEARNING ENGINE
# ─────────────────────────────────────────
class LearningEngine:
    """Analyze trade history and generate/disable rules"""

    MIN_SAMPLE = 8  # minimum trades before learning kicks in

    def analyze(self, trades: list) -> dict:
        closed = [t for t in trades if t.get("status") == "closed"]
        if len(closed) < self.MIN_SAMPLE:
            return {"rules": [], "message": f"Need {self.MIN_SAMPLE} trades, have {len(closed)}"}

        new_rules = []
        disabled = []
        warnings = []

        # Rule: RSI overbought CALL
        rsi_high_calls = [t for t in closed if t.get("direction") == "CALL" and t.get("rsi", 0) > 75]
        if len(rsi_high_calls) >= 5:
            wins = sum(1 for t in rsi_high_calls if t.get("result") == "WIN")
            wr = wins / len(rsi_high_calls)
            if wr < 0.4:
                disabled.append({
                    "id": "veto_call_rsi_75",
                    "reason": f"CALL with RSI > 75 wins only {wr:.0%} ({len(rsi_high_calls)} trades)",
                    "condition": {"signal": "CALL", "rsi_gt": 75},
                    "active": True,
                    "created": datetime.now().isoformat()
                })
                warnings.append(f"RSI > 75 CALL has {wr:.0%} win rate — rule added to block it")

        # Rule: Low ADX
        low_adx_trades = [t for t in closed if t.get("adx", 99) < 20]
        if len(low_adx_trades) >= 5:
            wins = sum(1 for t in low_adx_trades if t.get("result") == "WIN")
            wr = wins / len(low_adx_trades)
            if wr < 0.45:
                disabled.append({
                    "id": "veto_low_adx",
                    "reason": f"ADX < 20 trades win only {wr:.0%} ({len(low_adx_trades)} trades) — sideways market trap",
                    "condition": {"adx_lt": 20},
                    "active": True,
                    "created": datetime.now().isoformat()
                })

        # High performance patterns
        high_conf = []
        for t in closed:
            conf = t.get("confidence")
            if conf is None:
                continue
            try:
                conf_val = float(conf)
            except (TypeError, ValueError):
                continue
            if conf_val >= 80:
                high_conf.append(t)
        if high_conf:
            hc_wins = sum(1 for t in high_conf if t.get("result") == "WIN")
            hc_wr = hc_wins / len(high_conf)
            new_rules.append({
                "type": "insight",
                "message": f"Confidence >= 80% → {hc_wr:.0%} win rate ({len(high_conf)} trades)"
            })

        result = {
            "new_rules": new_rules,
            "disabled_rules": disabled,
            "warnings": warnings,
            "analyzed_trades": len(closed),
            "timestamp": datetime.now().isoformat()
        }

        # Save disabled rules
        self._merge_rules(disabled)
        return result

    def _merge_rules(self, new_rules):
        os.makedirs("data", exist_ok=True)
        try:
            with open("data/learning_rules.json") as f:
                existing = {r["id"]: r for r in json.load(f)}
        except FileNotFoundError:
            existing = {}
        for r in new_rules:
            existing[r["id"]] = r
        with open("data/learning_rules.json", "w") as f:
            json.dump(list(existing.values()), f, indent=2)


# ─────────────────────────────────────────
#  WEBSOCKET SERVER (for dashboard)
# ─────────────────────────────────────────
connected_clients = set()
state_store = {
    "signals": [],
    "trades": [],
    "stats": {},
    "balance": 0,
    "status": "stopped",
    "learning": {},
    "candles": {},
    "risk": {},
    "config": {},
    "account_type": "PRACTICE",
    "activity": {},        # current one-line heartbeat: what the bot is doing right now
    "activity_log": [],    # rolling timeline of activity + per-pair decisions
}


async def ws_handler(websocket):
    connected_clients.add(websocket)
    try:
        # Send current state immediately on connect
        await websocket.send(json.dumps({"type": "state", "data": state_store}))
        async for msg in websocket:
            data = json.loads(msg)
            if data.get("type") == "ping":
                await websocket.send(json.dumps({"type": "pong"}))
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        connected_clients.discard(websocket)


async def broadcast(payload: dict):
    if connected_clients:
        msg = json.dumps(payload)
        await asyncio.gather(*[ws.send(msg) for ws in connected_clients], return_exceptions=True)


async def ws_server():
    async with websockets.serve(ws_handler, "localhost", 8765):
        logger.info("[WS] Dashboard WebSocket server on ws://localhost:8765")
        await asyncio.Future()


# ─────────────────────────────────────────
#  MAIN TRADING LOOP
# ─────────────────────────────────────────
class TradingBot:
    def __init__(self, cfg: TradingConfig):
        self.cfg = cfg
        self.iq: Optional[IQ_Option] = None
        self.signal_engine = SignalEngine(cfg)
        self.indicator_engine = IndicatorEngine()
        self.learning_engine = LearningEngine()
        self.trade_manager: Optional[TradeManager] = None
        self.running = False
        self.loop_count = 0
        self._assets_resolved = False  # True once we have a confirmed tradable (OTC) asset list

    def connect(self) -> bool:
        logger.info(f"[IQ] Connecting as {self.cfg.email} ({self.cfg.account_type})")
        self.iq = IQ_Option(self.cfg.email, self.cfg.password)
        check, reason = self.iq.connect()
        if check:
            self.iq.change_balance(self.cfg.account_type)
            logger.info(f"[IQ] Connected. Balance: {self.iq.get_balance():.2f}")
            self.trade_manager = TradeManager(self.cfg, self.iq)
            # Asset resolution happens in the first run_cycle (non-blocking) so a slow/degraded
            # IQ market-list endpoint can't stall startup or the dashboard balance.
            return True
        logger.error(f"[IQ] Connection failed: {reason}")
        return False

    def ensure_connected(self) -> bool:
        """True if the IQ socket is alive; otherwise try to reconnect once.
        Without this the loop stalls forever if IQ drops the connection."""
        try:
            if self.iq and self.iq.check_connect():
                return True
        except Exception:
            pass
        logger.warning("[IQ] Connection lost — reconnecting...")
        try:
            check, reason = self.iq.connect()
            if check:
                self.iq.change_balance(self.cfg.account_type)
                logger.info("[IQ] Reconnected successfully")
                return True
            logger.error(f"[IQ] Reconnect failed: {reason}")
        except Exception as e:
            logger.error(f"[IQ] Reconnect error: {e}")
        return False

    def resolve_assets(self):
        """Pick open REAL (non-OTC) forex pairs across binary / turbo / DIGITAL, ranked by payout.

        On current IQ Option, real forex is usually open only as *digital* options while
        binary/turbo carry mostly OTC — so scanning binary/turbo alone finds nothing real.
        We therefore also read the digital underlying list (authoritative open windows via
        each pair's `schedule`). OTC is never selected (policy: real forex only)."""

        # Fiat-currency whitelist: IQ-tradeable majors covering all real forex crosses.
        # Excludes crypto (BTC/ETH), gold (XAU), silver (XAG), oil (USO), etc. by construction.
        FIAT = {"EUR", "USD", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"}

        def is_fx(name):
            # real (non-OTC) FIAT forex only. IQ names the REAL binary/turbo option "XXXXXX-op",
            # the OTC (synthetic) one "XXXXXX-OTC", and older listings are bare 6-letter.
            # Strip a trailing "-op" before checking, then require BOTH 3-char halves to be
            # whitelisted fiat currencies (rejects crypto/gold/oil/silver pairs).
            if name.endswith("-OTC"):              # never trade OTC (synthetic) pairs
                return False
            base = name[:-3] if name.endswith("-op") else name
            return len(base) == 6 and base[:3] in FIAT and base[3:] in FIAT

        open_kind = {}        # name -> "digital" | "binary" | "turbo"  (digital preferred for real FX)
        name_by_id = {}       # active_id -> name (for readable alert names)
        digital_open = []
        otc_count = 0
        repatched = 0

        # ── 1) binary/turbo via init_v2 (also re-patches the stale name->id table) ──
        init = None
        for attempt in range(3):
            try:
                data = self.iq.get_all_init_v2()
                if data:
                    init = data
                    break
            except Exception as e:
                logger.warning(f"[ASSET] get_all_init_v2 attempt {attempt + 1} failed: {e}")
            time.sleep(1.5)

        if init:
            for option in ("binary", "turbo"):
                actives = (init.get(option) or {}).get("actives") or {}
                for aid, active in actives.items():
                    clean = str(active.get("name", "")).split(".")[-1]
                    if not clean:
                        continue
                    try:
                        aid_key = int(aid)
                    except (TypeError, ValueError):
                        continue
                    name_by_id[aid_key] = clean
                    # Patch name->id with the LIVE id (static table goes stale, e.g.
                    # GBPNZD 947 -> 1880); otherwise get_candles()/buy() hit a dead id.
                    if ACTIVES.get(clean) != aid_key:
                        ACTIVES[clean] = aid_key
                        repatched += 1
                    is_open = active.get("enabled", True) and not active.get("is_suspended", False)
                    if not is_open:
                        continue
                    if clean.endswith("-OTC"):
                        otc_count += 1
                    elif is_fx(clean):
                        open_kind.setdefault(clean, option)
        else:
            logger.warning("[ASSET] init_v2 unavailable — relying on digital list this cycle")

        # ── 2) digital underlyings (where real forex actually trades) ──
        if self.cfg.trade_digital:
            try:
                # The first call only subscribes; the list arrives async and a slow link can
                # miss the library's 30s wait. Retry — a follow-up call usually returns at once.
                dl = None
                for attempt in range(3):
                    dl = self.iq.get_digital_underlying_list_data()
                    if dl and dl.get("underlying"):
                        break
                    logger.warning(f"[ASSET] digital list attempt {attempt + 1} empty — retrying")
                    time.sleep(2)
                underlying = (dl or {}).get("underlying") or []
                now = time.time()
                for d in underlying:
                    name = str(d.get("underlying") or "")
                    if not name:
                        continue
                    aid = d.get("active_id")
                    if isinstance(aid, int):
                        name_by_id[aid] = name
                        if ACTIVES.get(name) != aid:
                            ACTIVES[name] = aid
                            repatched += 1
                    is_open = any(s.get("open", 0) < now < s.get("close", 0)
                                  for s in (d.get("schedule") or []))
                    if not is_open:
                        continue
                    if name.endswith("-OTC"):
                        otc_count += 1
                    elif is_fx(name):
                        open_kind[name] = "digital"   # digital wins over binary/turbo for real FX
                        digital_open.append(name)
            except Exception as e:
                logger.warning(f"[ASSET] digital underlying list failed: {e}")

        # cache id -> name so alerts show real pair names
        if self.trade_manager:
            self.trade_manager._live_active_names = name_by_id
        if repatched:
            logger.info(f"[ASSET] Synced {repatched} live active ids into the name->id table")

        open_real = sorted(open_kind)
        logger.info(f"[ASSET] open real-FX: {open_real or 'none'} "
                    f"(digital: {sorted(digital_open) or 'none'}) | OTC open: {otc_count}")

        if not open_real:
            if self.cfg.assets:
                logger.info("[ASSET] No real (non-OTC) forex open on binary/turbo/digital "
                            "— waiting (OTC disabled by policy)")
            self.cfg.assets = []
            self.cfg.asset_kind = {}
            self._assets_resolved = False
            return

        # ── 3) rank by payout (digital payout for digital pairs, binary/turbo profit otherwise) ──
        try:
            profits = self.iq.get_all_profit()
        except Exception as e:
            logger.warning(f"[ASSET] get_all_profit failed: {e} — selecting without binary payout")
            profits = {}

        digital_payout = {}
        for name in open_real:
            if open_kind[name] == "digital":
                try:
                    p = self.iq.get_digital_payout(name)
                    digital_payout[name] = (float(p) / 100.0) if p else 0.0
                except Exception:
                    digital_payout[name] = 0.0

        def payout(name):
            if open_kind[name] == "digital":
                return digital_payout.get(name, 0.0)
            p = profits.get(name, {}) if profits else {}
            return float(p.get("binary") or p.get("turbo") or 0)

        ranked = sorted(open_real, key=payout, reverse=True)
        if any(payout(a) > 0 for a in ranked):
            filtered = [a for a in ranked if payout(a) >= self.cfg.min_payout]
            chosen = (filtered or ranked)[: self.cfg.max_assets]
        else:
            chosen = ranked[: self.cfg.max_assets]

        if chosen != self.cfg.assets:
            label = ", ".join(f"{a}[{open_kind[a]}] {payout(a)*100:.0f}%" for a in chosen)
            logger.info(f"[ASSET] Tradable (by payout): {label}")
        self.cfg.assets = chosen
        self.cfg.asset_kind = {a: open_kind[a] for a in chosen}
        self._assets_resolved = True

    def get_candles(self, asset: str) -> Optional[pd.DataFrame]:
        try:
            candles = self.iq.get_candles(asset, self.cfg.timeframe, self.cfg.candles_history, time.time())
            if not candles:
                return None
            df = pd.DataFrame(candles)
            df = df.rename(columns={"open": "open", "close": "close", "max": "high", "min": "low", "volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].astype(float)
            # Drop the last candle — it's the current still-forming one. We decide only on
            # the most recent CLOSED candle (so entries happen on confirmed data, not mid-candle).
            if len(df) > 1:
                df = df.iloc[:-1].reset_index(drop=True)
            return df
        except Exception as e:
            logger.error(f"[CANDLE] {asset}: {e}")
            return None

    async def run_cycle(self):
        """Single analysis + trade cycle"""
        self.loop_count += 1
        signals_this_cycle = []
        balance = self.iq.get_balance()
        state_store["balance"] = round(balance, 2)

        for asset in self.cfg.assets:
            df = self.get_candles(asset)
            if df is None or len(df) < 50:
                continue
            df = self.indicator_engine.compute_all(df, self.cfg)
            signal = self.signal_engine.evaluate(df, asset)

            signals_this_cycle.append(asdict(signal))
            logger.info(f"[SIGNAL] {asset}: {signal.signal} | Score: {signal.confidence:.1f}% | RSI: {signal.rsi:.1f}")

            # Execute trade if signal is actionable
            if signal.signal in ("CALL", "PUT") and signal.confidence >= self.cfg.confidence_threshold:
                trade = self.trade_manager.execute_trade(signal)
                if trade:
                    state_store["trades"] = self.trade_manager.trades
                    await broadcast({"type": "new_trade", "data": trade})

            # Store last candles for chart (last 50)
            state_store["candles"][asset] = df.tail(50)[["open", "high", "low", "close", "volume", "ema_fast", "ema_slow", "rsi", "atr", "macd_hist"]].to_dict("records")

            await asyncio.sleep(0.5)

        # Check completed trades
        self.trade_manager.check_results()
        stats = self.trade_manager.get_stats()
        state_store["signals"] = signals_this_cycle
        state_store["stats"] = stats
        state_store["status"] = "running"

        # Run learning every 30 cycles
        if self.loop_count % 30 == 0:
            learning_result = self.learning_engine.analyze(self.trade_manager.trades)
            state_store["learning"] = learning_result
            logger.info(f"[LEARNING] {learning_result}")
            # Reload rules
            self.trade_manager.learning_rules = self.trade_manager._load_learning_rules()

        await broadcast({"type": "update", "data": {
            "signals": signals_this_cycle,
            "stats": stats,
            "balance": state_store["balance"],
            "candles": state_store["candles"],
            "learning": state_store["learning"],
        }})

    async def main_loop(self):
        if not self.connect():
            logger.error("Cannot connect to IQ Option — exiting")
            return
        self.running = True
        loop_interval = self.cfg.timeframe  # analyze every candle close
        logger.info(f"[BOT] Starting main loop every {loop_interval}s")
        while self.running:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"[LOOP] Error: {e}")
                state_store["status"] = f"error: {e}"
            await asyncio.sleep(loop_interval)


# ─────────────────────────────────────────
#  ENTRYPOINT
# ─────────────────────────────────────────
async def main():
    cfg = TradingConfig()
    bot = TradingBot(cfg)
    await asyncio.gather(
        ws_server(),
        bot.main_loop()
    )


if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    os.makedirs("data", exist_ok=True)
    asyncio.run(main())
