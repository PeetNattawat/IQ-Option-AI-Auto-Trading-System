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
import websockets
import threading
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/trading.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

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
    candles_history: int = 200   # how many candles to load

    # Signal thresholds
    ema_fast: int = 20
    ema_slow: int = 50
    ema_trend: int = 200
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    rsi_call_min: float = 50.0
    rsi_call_max: float = 70.0
    rsi_put_min: float = 30.0
    rsi_put_max: float = 50.0
    atr_period: int = 14
    atr_multiplier: float = 1.5  # ATR must be > avg * multiplier for high vol
    volume_period: int = 20
    volume_multiplier: float = 1.2  # Volume must be > avg * multiplier

    # Trade settings
    trade_amount: float = 1.0    # USD per trade
    expiry_minutes: int = 5
    max_trades_per_hour: int = 6
    max_consecutive_losses: int = 3
    confidence_threshold: float = 70.0  # min score to trade

    def __post_init__(self):
        if self.assets is None:
            self.assets = ["EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "EURGBP"]


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

        call_score = 0
        put_score = 0
        call_reasons = []
        put_reasons = []
        breakdown_call = {}
        breakdown_put = {}

        # ── EMA TREND ALIGNMENT (25 pts) ──
        ema_bull = row["ema_fast"] > row["ema_slow"] > row["ema_trend"]
        ema_bear = row["ema_fast"] < row["ema_slow"] < row["ema_trend"]
        if ema_bull:
            call_score += 25; breakdown_call["ema_alignment"] = 25
            call_reasons.append(f"EMA bull stack: {row['ema_fast']:.5f} > {row['ema_slow']:.5f} > {row['ema_trend']:.5f}")
        if ema_bear:
            put_score += 25; breakdown_put["ema_alignment"] = 25
            put_reasons.append(f"EMA bear stack: {row['ema_fast']:.5f} < {row['ema_slow']:.5f} < {row['ema_trend']:.5f}")

        # Partial EMA (10 pts if fast > slow only)
        if not ema_bull and row["ema_fast"] > row["ema_slow"]:
            call_score += 10; breakdown_call["ema_partial"] = 10
            call_reasons.append("EMA fast > slow (partial bullish)")
        if not ema_bear and row["ema_fast"] < row["ema_slow"]:
            put_score += 10; breakdown_put["ema_partial"] = 10
            put_reasons.append("EMA fast < slow (partial bearish)")

        # ── RSI (20 pts) ──
        rsi = row["rsi"]
        if self.cfg.rsi_call_min <= rsi <= self.cfg.rsi_call_max:
            call_score += 20; breakdown_call["rsi"] = 20
            call_reasons.append(f"RSI {rsi:.1f} in bullish zone [{self.cfg.rsi_call_min}-{self.cfg.rsi_call_max}]")
        elif self.cfg.rsi_put_min <= rsi <= self.cfg.rsi_put_max:
            put_score += 20; breakdown_put["rsi"] = 20
            put_reasons.append(f"RSI {rsi:.1f} in bearish zone [{self.cfg.rsi_put_min}-{self.cfg.rsi_put_max}]")
        elif rsi > self.cfg.rsi_overbought:
            put_score += 10; breakdown_put["rsi_extreme"] = 10
            put_reasons.append(f"RSI {rsi:.1f} overbought — reversal bias")
        elif rsi < self.cfg.rsi_oversold:
            call_score += 10; breakdown_call["rsi_extreme"] = 10
            call_reasons.append(f"RSI {rsi:.1f} oversold — bounce bias")

        # ── ATR VOLATILITY FILTER (15 pts) ──
        atr = row["atr"]
        atr_avg = row["atr_avg"] if not pd.isna(row["atr_avg"]) else atr
        atr_high = atr > atr_avg * self.cfg.atr_multiplier
        if atr_high:
            call_score += 15; put_score += 15
            breakdown_call["atr"] = 15; breakdown_put["atr"] = 15
            call_reasons.append(f"ATR {atr:.5f} above avg ({atr_avg:.5f}) — good volatility")
            put_reasons.append(f"ATR {atr:.5f} above avg — good volatility")
        else:
            call_reasons.append(f"ATR {atr:.5f} low volatility — caution")
            put_reasons.append(f"ATR {atr:.5f} low volatility — caution")

        # ── MACD (15 pts) ──
        macd_hist = row["macd_hist"]
        prev_macd = prev["macd_hist"]
        macd_bull = macd_hist > 0 and macd_hist > prev_macd
        macd_bear = macd_hist < 0 and macd_hist < prev_macd
        if macd_bull:
            call_score += 15; breakdown_call["macd"] = 15
            call_reasons.append(f"MACD histogram bullish {macd_hist:.5f}")
        if macd_bear:
            put_score += 15; breakdown_put["macd"] = 15
            put_reasons.append(f"MACD histogram bearish {macd_hist:.5f}")

        # ── VOLUME CONFIRMATION (10 pts) ──
        vol = row["volume"]
        vol_avg = row["volume_avg"] if not pd.isna(row["volume_avg"]) else vol
        if vol > vol_avg * self.cfg.volume_multiplier:
            call_score += 10; put_score += 10
            breakdown_call["volume"] = 10; breakdown_put["volume"] = 10
            call_reasons.append(f"Volume {vol:.0f} above avg ({vol_avg:.0f}) — confirmed")
            put_reasons.append(f"Volume above avg — confirmed")

        # ── ADX TREND STRENGTH (10 pts) ──
        adx = row["adx"]
        if adx > 25:
            call_score += 10; put_score += 10
            breakdown_call["adx"] = 10; breakdown_put["adx"] = 10
            call_reasons.append(f"ADX {adx:.1f} — strong trend")
            put_reasons.append(f"ADX {adx:.1f} — strong trend")
        else:
            call_reasons.append(f"ADX {adx:.1f} — weak trend, caution")
            put_reasons.append(f"ADX {adx:.1f} — weak trend, caution")

        # ── BOLLINGER BAND CONTEXT (5 pts) ──
        price = row["close"]
        if price < row["bb_lower"] * 1.001:
            call_score += 5; breakdown_call["bollinger"] = 5
            call_reasons.append("Price near BB lower — potential bounce")
        if price > row["bb_upper"] * 0.999:
            put_score += 5; breakdown_put["bollinger"] = 5
            put_reasons.append("Price near BB upper — potential rejection")

        # ── DECISION ──
        if call_score >= put_score and call_score >= self.cfg.confidence_threshold:
            return SignalResult(
                asset=asset, timeframe=self.cfg.timeframe,
                signal="CALL", confidence=min(call_score, 100),
                score_breakdown=breakdown_call, reasons=call_reasons,
                entry_price=price, rsi=rsi, atr=atr,
                ema_fast=row["ema_fast"], ema_slow=row["ema_slow"],
                ema_trend=row["ema_trend"], adx=adx, macd_hist=macd_hist,
                timestamp=datetime.now().isoformat()
            )
        elif put_score > call_score and put_score >= self.cfg.confidence_threshold:
            return SignalResult(
                asset=asset, timeframe=self.cfg.timeframe,
                signal="PUT", confidence=min(put_score, 100),
                score_breakdown=breakdown_put, reasons=put_reasons,
                entry_price=price, rsi=rsi, atr=atr,
                ema_fast=row["ema_fast"], ema_slow=row["ema_slow"],
                ema_trend=row["ema_trend"], adx=adx, macd_hist=macd_hist,
                timestamp=datetime.now().isoformat()
            )
        else:
            max_score = max(call_score, put_score)
            reasons = call_reasons if call_score >= put_score else put_reasons
            return SignalResult(
                asset=asset, timeframe=self.cfg.timeframe,
                signal="HOLD", confidence=max_score,
                score_breakdown={}, reasons=reasons[:3],
                entry_price=price, rsi=rsi, atr=atr,
                ema_fast=row["ema_fast"], ema_slow=row["ema_slow"],
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

    def __init__(self, cfg: TradingConfig, iq: IQ_Option):
        self.cfg = cfg
        self.iq = iq
        self.trades = []          # list of trade dicts
        self.active_orders = {}   # order_id -> trade_info
        self.consecutive_losses = 0
        self.hourly_trades = deque()
        self.learning_rules = self._load_learning_rules()

    def _load_learning_rules(self) -> list:
        try:
            with open("data/learning_rules.json") as f:
                return json.load(f)
        except FileNotFoundError:
            return []

    def can_trade(self) -> tuple[bool, str]:
        now = time.time()
        # Clear old hourly records
        while self.hourly_trades and now - self.hourly_trades[0] > 3600:
            self.hourly_trades.popleft()
        if len(self.hourly_trades) >= self.cfg.max_trades_per_hour:
            return False, f"Max {self.cfg.max_trades_per_hour} trades/hour reached"
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            return False, f"Consecutive losses {self.consecutive_losses} — cooling down"
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

    def execute_trade(self, signal: SignalResult) -> Optional[dict]:
        can, reason = self.can_trade()
        if not can:
            logger.warning(f"[RISK] Trade blocked: {reason}")
            return None

        vetoed, veto_reason = self.apply_learning_veto(signal)
        if vetoed:
            logger.warning(f"[LEARNING] {veto_reason}")
            return None

        direction = "call" if signal.signal == "CALL" else "put"
        asset_fmt = signal.asset  # IQ Option format

        logger.info(f"[TRADE] Placing {direction.upper()} on {asset_fmt} @ {signal.entry_price} | Confidence: {signal.confidence:.1f}%")

        try:
            check, order_id = self.iq.buy(
                self.cfg.trade_amount,
                asset_fmt,
                direction,
                self.cfg.expiry_minutes
            )
            if check:
                trade = {
                    "id": order_id,
                    "asset": signal.asset,
                    "direction": signal.signal,
                    "amount": self.cfg.trade_amount,
                    "entry": signal.entry_price,
                    "confidence": signal.confidence,
                    "reasons": signal.reasons,
                    "rsi": signal.rsi,
                    "atr": signal.atr,
                    "adx": signal.adx,
                    "open_time": datetime.now().isoformat(),
                    "expiry": self.cfg.expiry_minutes,
                    "status": "open",
                    "pnl": None,
                    "result": None
                }
                self.active_orders[order_id] = trade
                self.trades.append(trade)
                self.hourly_trades.append(time.time())
                self._save_trades()
                logger.info(f"[TRADE] Order placed: ID {order_id}")
                return trade
            else:
                logger.error(f"[TRADE] Order failed for {asset_fmt}")
                return None
        except Exception as e:
            logger.error(f"[TRADE] Exception: {e}")
            return None

    def check_results(self):
        """Poll IQ Option for results of active orders"""
        closed = []
        for order_id, trade in list(self.active_orders.items()):
            try:
                result = self.iq.check_win_v3(order_id)
                if result is not None:
                    win_amount = float(result)
                    pnl = win_amount - self.cfg.trade_amount if win_amount > 0 else -self.cfg.trade_amount
                    trade["pnl"] = round(pnl, 2)
                    trade["result"] = "WIN" if win_amount > 0 else "LOSS"
                    trade["close_time"] = datetime.now().isoformat()
                    trade["status"] = "closed"

                    if trade["result"] == "LOSS":
                        self.consecutive_losses += 1
                    else:
                        self.consecutive_losses = 0

                    logger.info(f"[RESULT] {trade['asset']} {trade['direction']} → {trade['result']} | PnL: {pnl:+.2f}")
                    closed.append(order_id)
                    self._save_trades()
            except Exception as e:
                logger.debug(f"Order {order_id} still pending: {e}")

        for oid in closed:
            del self.active_orders[oid]

    def get_stats(self) -> dict:
        closed = [t for t in self.trades if t["status"] == "closed"]
        if not closed:
            return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0, "pnl": 0}
        wins = [t for t in closed if t["result"] == "WIN"]
        losses = [t for t in closed if t["result"] == "LOSS"]
        pnl = sum(t["pnl"] for t in closed if t["pnl"] is not None)
        return {
            "total": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(closed) * 100, 1),
            "pnl": round(pnl, 2),
            "consecutive_losses": self.consecutive_losses,
            "active_orders": len(self.active_orders)
        }

    def _save_trades(self):
        os.makedirs("data", exist_ok=True)
        with open("data/trades.json", "w") as f:
            json.dump(self.trades, f, indent=2, default=str)


# ─────────────────────────────────────────
#  LEARNING ENGINE
# ─────────────────────────────────────────
class LearningEngine:
    """Analyze trade history and generate/disable rules"""

    MIN_SAMPLE = 15  # minimum trades before learning kicks in

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
        high_conf = [t for t in closed if t.get("confidence", 0) >= 80]
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
    "candles": {}
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

    def connect(self) -> bool:
        logger.info(f"[IQ] Connecting as {self.cfg.email} ({self.cfg.account_type})")
        self.iq = IQ_Option(self.cfg.email, self.cfg.password)
        check, reason = self.iq.connect()
        if check:
            self.iq.change_balance(self.cfg.account_type)
            logger.info(f"[IQ] Connected. Balance: {self.iq.get_balance():.2f}")
            self.trade_manager = TradeManager(self.cfg, self.iq)
            return True
        logger.error(f"[IQ] Connection failed: {reason}")
        return False

    def get_candles(self, asset: str) -> Optional[pd.DataFrame]:
        try:
            candles = self.iq.get_candles(asset, self.cfg.timeframe, self.cfg.candles_history, time.time())
            if not candles:
                return None
            df = pd.DataFrame(candles)
            df = df.rename(columns={"open": "open", "close": "close", "max": "high", "min": "low", "volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].astype(float)
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
