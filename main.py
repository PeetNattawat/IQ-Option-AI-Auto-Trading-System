"""
Main launcher — integrates TelegramBot into the trading loop
Run: python main.py
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from dataclasses import asdict

import websockets
from dotenv import load_dotenv
from pathlib import Path

# Load .env from project root (one level up from backend/) OR same dir
_here = Path(__file__).parent
_env_paths = [_here / ".env", _here.parent / ".env", Path(".env")]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(dotenv_path=_p, override=True)
        print(f"[ENV] Loaded: {_p.resolve()}")
        break
else:
    print("[ENV] WARNING: .env file not found — using system environment variables")

from trading_engine import (
    TradingConfig, TradingBot, IndicatorEngine,
    SignalEngine, TradeManager, LearningEngine,
    ws_server, broadcast, state_store, connected_clients,
)
from telegram_bot import TelegramBot, TelegramConfig

logger = logging.getLogger(__name__)

RUNTIME_CONFIG_PATH = Path("data/config.json")


def load_runtime_config() -> dict:
    try:
        with open(RUNTIME_CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# Settings that can be changed from the dashboard and persist across restarts
RUNTIME_FIELDS = [
    "trade_amount", "confidence_threshold", "expiry_minutes", "max_open_positions",
    "martingale_enabled", "martingale_base", "martingale_multiplier", "martingale_max_steps",
    "max_trades_per_day", "max_consecutive_losses", "daily_profit_target", "daily_loss_limit",
]


def save_runtime_config(cfg: TradingConfig):
    os.makedirs("data", exist_ok=True)
    with open(RUNTIME_CONFIG_PATH, "w") as f:
        json.dump({k: getattr(cfg, k) for k in RUNTIME_FIELDS}, f, indent=2)


def apply_runtime_config(cfg: TradingConfig, rt: dict):
    """Apply persisted/dashboard settings onto cfg, coercing to the field's type."""
    for k in RUNTIME_FIELDS:
        if k not in rt:
            continue
        cur = getattr(cfg, k)
        try:
            val = bool(rt[k]) if isinstance(cur, bool) else type(cur)(rt[k])
        except (TypeError, ValueError):
            continue
        setattr(cfg, k, val)
    # keep base stake and martingale base in sync (single "เงินต่อไม้" control), and
    # make the consecutive-loss pause fire only AFTER the full ladder is lost
    if cfg.martingale_enabled:
        cfg.trade_amount = cfg.martingale_base
        cfg.max_consecutive_losses = max(cfg.max_consecutive_losses, cfg.martingale_max_steps)
        # up to max_open_positions concurrent per-asset ladders (don't force to 1)
        cfg.max_open_positions = max(1, cfg.max_open_positions)


# ─────────────────────────────────────────────────
#  ENHANCED BOT WITH TELEGRAM
# ─────────────────────────────────────────────────
class FullTradingBot(TradingBot):

    def __init__(self, cfg: TradingConfig, tg: TelegramBot):
        super().__init__(cfg)
        self.tg = tg
        self._cmd_queue: asyncio.Queue = asyncio.Queue()
        self._paused = False
        self._iq_lock = asyncio.Lock()  # serialize IQ network calls between run_cycle and the sync loop

    def log_activity(self, icon: str, msg: str, phase: str = "", level: str = "info"):
        """Record what the bot is doing → shown live on the dashboard 'กิจกรรมบอท' feed.
        Sets a current one-liner (heartbeat) and appends to a rolling timeline."""
        entry = {"t": datetime.now().isoformat(), "icon": icon, "msg": msg, "level": level}
        state_store["activity"] = {**entry, "phase": phase or level}
        log = state_store.setdefault("activity_log", [])
        log.append(entry)
        del log[:-60]  # keep last 60
        logger.info(f"[ACTIVITY] {icon} {msg}")

    def build_risk(self) -> dict:
        tm = self.trade_manager
        today_pnl = tm.today_pnl() if tm else 0
        return {
            "open": len(tm.active_orders) if tm else 0,
            "max_open": self.cfg.max_open_positions,
            "today_trades": len(tm.today_trades()) if tm else 0,
            "max_day_trades": self.cfg.max_trades_per_day,
            "consec_losses": tm.consecutive_losses if tm else 0,
            "max_consec": self.cfg.max_consecutive_losses,
            "daily_pnl": today_pnl,
            "daily_loss": round(max(0, -today_pnl), 2),
            "daily_target": self.cfg.daily_profit_target,
        }

    def build_config(self) -> dict:
        return {
            "trade_amount": self.cfg.trade_amount,
            "confidence_threshold": self.cfg.confidence_threshold,
            "timeframe": self.cfg.timeframe,
            "expiry_minutes": self.cfg.expiry_minutes,
            "assets": self.cfg.assets,
            "account_type": self.cfg.account_type,
            "martingale_enabled": self.cfg.martingale_enabled,
            "martingale_base": self.cfg.martingale_base,
            "martingale_multiplier": self.cfg.martingale_multiplier,
            "martingale_max_steps": self.cfg.martingale_max_steps,
            "martingale_sequence": [round(self.cfg.martingale_base * (self.cfg.martingale_multiplier ** i), 2)
                                     for i in range(self.cfg.martingale_max_steps)],
            "max_trades_per_day": self.cfg.max_trades_per_day,
            "max_consecutive_losses": self.cfg.max_consecutive_losses,
            "max_open_positions": self.cfg.max_open_positions,
            "daily_profit_target": self.cfg.daily_profit_target,
            "daily_loss_limit": self.cfg.daily_loss_limit,
        }

    # ── Override run_cycle to send Telegram alerts ──
    async def run_cycle(self):
        if self._paused:
            state_store["status"] = "paused"
            await broadcast({"type": "update", "data": state_store})
            return

        self.loop_count += 1
        signals_this_cycle = []

        # Make sure the IQ socket is alive — reconnect if it dropped (otherwise the loop stalls)
        try:
            connected = await asyncio.wait_for(asyncio.to_thread(self.ensure_connected), timeout=45)
        except Exception:
            connected = False
        if not connected:
            self.log_activity("⚠️", "IQ หลุดการเชื่อมต่อ — กำลังต่อใหม่ จะลองอีกครั้งรอบถัดไป", level="error", phase="connecting")
            state_store["status"] = "reconnecting"
            self._need_resolve = True
            await broadcast({"type": "update", "data": {
                "status": "reconnecting",
                "activity": state_store["activity"],
                "activity_log": state_store["activity_log"],
            }})
            return

        # Resolve tradable assets: keep retrying until we have OTC pairs, then hourly / on demand
        if not self._assets_resolved or self.loop_count % 12 == 1 or getattr(self, "_need_resolve", False):
            try:
                await asyncio.wait_for(asyncio.to_thread(self.resolve_assets), timeout=60)
                self._need_resolve = False
            except Exception as e:
                logger.warning(f"[ASSET] resolve failed: {e}")
            if not self._assets_resolved:
                self.log_activity("⚠️", "ดึงรายชื่อคู่เงิน OTC จาก IQ ยังไม่ได้ — กำลังลองใหม่ (อาจเป็นที่เซิร์ฟเวอร์ IQ)", level="warn", phase="connecting")

        self.log_activity("🔍", f"กำลังสแกน {len(self.cfg.assets)} คู่ (รอบที่ {self.loop_count})", phase="scanning")

        try:
            balance = await asyncio.wait_for(asyncio.to_thread(self.iq.get_balance), timeout=15)
        except Exception:
            balance = state_store.get("balance", 0)
        state_store["balance"] = round(balance, 2)

        candidates = []  # qualifying CALL/PUT signals this cycle
        got_data = False
        for asset in self.cfg.assets:
            try:
                df = await asyncio.wait_for(asyncio.to_thread(self.get_candles, asset), timeout=30)
            except Exception as e:
                logger.warning(f"[DATA] {asset}: candle fetch failed/timeout: {e}")
                continue
            if df is None or len(df) < 60:
                logger.warning(f"[DATA] {asset}: not enough candles")
                signals_this_cycle.append({
                    "asset": asset, "timeframe": self.cfg.timeframe,
                    "signal": "HOLD", "confidence": 0,
                    "score_breakdown": {}, "reasons": ["ข้อมูลแท่งเทียนไม่พอ"],
                    "entry_price": 0, "rsi": None, "atr": None,
                    "ema_fast": None, "ema_slow": None, "ema_trend": None,
                    "adx": None, "macd_hist": None,
                    "timestamp": datetime.now().isoformat(),
                })
                continue

            got_data = True
            df = self.indicator_engine.compute_all(df, self.cfg)
            signal = self.signal_engine.evaluate(df, asset)
            signals_this_cycle.append(asdict(signal))

            qualifies = signal.signal in ("CALL", "PUT") and signal.confidence >= self.cfg.confidence_threshold
            decision = "เข้าเงื่อนไข" if qualifies else "ไม่เข้า"
            top_reason = (signal.reasons or ["-"])[0]
            logger.info(
                f"[SIGNAL] {asset}: {signal.signal:4s} | Conf: {signal.confidence:5.1f}% | "
                f"RSI: {signal.rsi:5.1f} | ATR: {signal.atr:.5f} | ADX: {signal.adx:.1f} | "
                f"{decision} — {top_reason}"
            )

            if qualifies:
                candidates.append(signal)

            # Store candles for chart
            cols = ["open", "high", "low", "close", "volume",
                    "ema_fast", "ema_slow", "ema_trend", "rsi", "atr", "macd_hist", "adx"]
            available = [c for c in cols if c in df.columns]
            state_store["candles"][asset] = df.tail(50)[available].to_dict("records")
            await asyncio.sleep(0.3)

        # All configured assets returned no candles (markets closed) — re-resolve next cycle
        if not got_data:
            self._need_resolve = True
            logger.warning("[DATA] no candles from any asset — will re-resolve tradable markets next cycle")

        # Smart selection: highest-confidence signals first. Open into free slots — up to
        # max_open_positions concurrent trades, one per asset (each its own Martingale lane).
        candidates.sort(key=lambda s: s.confidence, reverse=True)
        placed = None
        slots_free = max(0, self.cfg.max_open_positions - self.trade_manager.open_auto_count())
        for signal in candidates:
            if slots_free <= 0:
                break
            trade = await asyncio.to_thread(self.trade_manager.execute_trade, signal)
            if trade:
                placed = trade
                slots_free -= 1
                state_store["trades"] = self.trade_manager.trades
                await broadcast({"type": "new_trade", "data": trade})
                asyncio.create_task(self.tg.alert_trade_open(trade))
                mg = f" · ไม้ {trade.get('mg_step')}" if trade.get("mg_step") else ""
                self.log_activity("🚀", f"เปิดออเดอร์ {trade['asset']} {trade['direction']} ที่ {(trade.get('confidence') or 0):.0f}%{mg}", phase="trading")

        # Summarize the decision so the dashboard shows what the bot is doing / waiting for
        best = max(signals_this_cycle, key=lambda s: s.get("confidence") or 0, default=None)
        best_txt = f"{best['asset']} {best['signal']} {(best.get('confidence') or 0):.0f}%" if best else "-"
        if not got_data:
            self.log_activity("⚠️", "ดึงแท่งเทียนไม่ได้สักคู่ — ตลาดอาจปิดหรือการเชื่อมต่อ IQ มีปัญหา", level="warn", phase="error")
        elif placed is None and candidates:
            if self.cfg.martingale_enabled and self.trade_manager.open_auto_count() >= 1:
                reason = "รอผลไม้ Martingale ที่เปิดอยู่ก่อน"
            else:
                _, reason = self.trade_manager.can_trade()
            self.log_activity("⛔", f"มี {len(candidates)} คู่เข้าเงื่อนไข แต่ยังไม่เปิด: {reason}", level="warn", phase="blocked")
        elif placed is None:
            self.log_activity("💤", f"ยังไม่มีคู่เข้าเงื่อนไข (≥{self.cfg.confidence_threshold:.0f}%) — รอสัญญาณ · เด่นสุด {best_txt}", phase="waiting")

        # Finalize results here too (belt-and-suspenders); result alerts + balance refresh
        # are owned by the 15s external_sync_loop to avoid duplicate notifications.
        async with self._iq_lock:
            try:
                await asyncio.wait_for(asyncio.to_thread(self.trade_manager.check_results), timeout=30)
            except Exception as e:
                logger.warning(f"[RESULT] check_results failed/timeout: {e}")

        # Risk: pause on consecutive losses
        if self.trade_manager.consecutive_losses >= self.cfg.max_consecutive_losses:
            self._paused = True
            msg = f"{self.cfg.max_consecutive_losses} consecutive losses — bot paused"
            logger.warning(f"[RISK] {msg}")
            self.log_activity("🛑", f"หยุดอัตโนมัติ: แพ้ติดกัน {self.trade_manager.consecutive_losses} ไม้ (ครบเพดาน)", level="error", phase="paused")
            asyncio.create_task(self.tg.alert_risk_pause(msg))

        stats = self.trade_manager.get_stats()
        state_store.update({
            "signals": signals_this_cycle,
            "trades": self.trade_manager.trades,
            "stats": stats,
            "status": "running",
            "risk": self.build_risk(),
            "config": self.build_config(),
            "account_type": self.cfg.account_type,
        })

        # Learn from results frequently (trades come slowly under one-at-a-time Martingale)
        if self.loop_count % 5 == 0 and self.trade_manager.trades:
            lr = await asyncio.to_thread(self.learning_engine.analyze, self.trade_manager.trades)
            state_store["learning"] = lr
            if lr.get("disabled_rules") or lr.get("warnings"):
                asyncio.create_task(self.tg.alert_learning(lr))
            self.trade_manager.learning_rules = self.trade_manager._load_learning_rules()

        await broadcast({"type": "update", "data": {
            "signals": signals_this_cycle,
            "stats": stats,
            "balance": state_store["balance"],
            "candles": state_store["candles"],
            "learning": state_store.get("learning", {}),
            "status": state_store["status"],
            "trades": self.trade_manager.trades,
            "risk": state_store["risk"],
            "config": state_store["config"],
            "account_type": state_store["account_type"],
            "activity": state_store["activity"],
            "activity_log": state_store["activity_log"],
        }})

    # ── Command handler from WebSocket ──
    async def handle_command(self, cmd: str, **kwargs):
        if cmd == "start":
            was_paused = self._paused
            self._paused = False
            self.running = True
            logger.info("[CMD] Bot started")
            self.log_activity("▶️", "เริ่ม/เล่นต่อบอท — กำลังกลับไปสแกน", phase="running")
            if was_paused:
                asyncio.create_task(self.tg.alert_bot_resumed())
        elif cmd == "stop":
            state_store["status"] = "stopped"
            if not self._paused:
                self._paused = True
                self.log_activity("⏸", "หยุดบอทชั่วคราว — ไม่เปิดออเดอร์ใหม่ (ไม้ที่เปิดอยู่ยังเดินต่อ)", phase="paused")
                today = self.trade_manager.get_stats().get("today") if self.trade_manager else None
                asyncio.create_task(self.tg.alert_bot_paused(today))
        elif cmd == "step":
            old = self._paused
            self._paused = False
            await self.run_cycle()
            self._paused = old
        elif cmd == "close_all":
            for oid in list(self.trade_manager.active_orders.keys()):
                logger.info(f"[CMD] Force-closing {oid}")
                # IQ Option binary can't be early-closed but we mark it
        elif cmd == "refresh":
            await broadcast({"type": "state", "data": state_store})
        elif cmd == "switch_account":
            account = kwargs.get("account", "PRACTICE")
            self.cfg.account_type = account
            try:
                self.iq.change_balance(account)
                state_store["account_type"] = account
                logger.info(f"[CMD] Switched to {account}")
            except Exception as e:
                logger.error(f"[CMD] Switch failed: {e}")
        elif cmd == "set_amount":
            try:
                amount = max(1.0, float(kwargs.get("amount", self.cfg.trade_amount)))
                self.cfg.trade_amount = amount
                save_runtime_config(self.cfg)
                logger.info(f"[CMD] Trade amount set to {amount}")
                await self._push_settings()
            except (TypeError, ValueError) as e:
                logger.error(f"[CMD] set_amount failed: {e}")
        elif cmd == "set_confidence":
            try:
                conf = min(100.0, max(0.0, float(kwargs.get("confidence", self.cfg.confidence_threshold))))
                self.cfg.confidence_threshold = conf
                save_runtime_config(self.cfg)
                logger.info(f"[CMD] Confidence threshold set to {conf}%")
                await self._push_settings()
            except (TypeError, ValueError) as e:
                logger.error(f"[CMD] set_confidence failed: {e}")
        elif cmd == "update_settings":
            settings = kwargs.get("settings", {}) or {}
            # clamp the few that have hard bounds before applying
            if "confidence_threshold" in settings:
                settings["confidence_threshold"] = min(100.0, max(0.0, float(settings["confidence_threshold"])))
            if "martingale_base" in settings:
                settings["martingale_base"] = max(1.0, float(settings["martingale_base"]))
            if "trade_amount" in settings:
                settings["trade_amount"] = max(1.0, float(settings["trade_amount"]))
            if "martingale_max_steps" in settings:
                settings["martingale_max_steps"] = max(1, min(8, int(settings["martingale_max_steps"])))
            if "max_open_positions" in settings:
                settings["max_open_positions"] = max(1, min(3, int(settings["max_open_positions"])))
            apply_runtime_config(self.cfg, settings)
            save_runtime_config(self.cfg)
            logger.info(f"[CMD] Settings updated: {', '.join(settings.keys())}")
            await self._push_settings()
            await broadcast({"type": "update", "data": {
                "stats": self.trade_manager.get_stats() if self.trade_manager else {},
                "risk": self.build_risk(),
            }})
        elif cmd == "manual_trade":
            asset = kwargs.get("asset", "")
            direction = kwargs.get("direction", "")
            if not asset or not self.trade_manager:
                logger.warning("[CMD] manual_trade missing asset or not connected")
                return
            trade = await asyncio.to_thread(self.trade_manager.execute_manual, asset, direction)
            if trade:
                state_store["trades"] = self.trade_manager.trades
                state_store["risk"] = self.build_risk()
                await broadcast({"type": "new_trade", "data": trade})
                await broadcast({"type": "update", "data": {
                    "trades": self.trade_manager.trades,
                    "risk": state_store["risk"],
                    "stats": self.trade_manager.get_stats(),
                }})
                asyncio.create_task(self.tg.alert_trade_open(trade))
            else:
                await broadcast({"type": "error", "data": {"message": f"Manual trade {asset} {direction} failed/blocked"}})

    async def _push_settings(self):
        state_store["config"] = self.build_config()
        await broadcast({"type": "update", "data": {"config": state_store["config"]}})

    async def external_sync_loop(self, interval: int = 15):
        """Every ~15s keep the dashboard live: refresh balance, finalize closed trades,
        pull in platform-opened trades, and broadcast a fresh snapshot — independent of
        the 5-minute trading cycle."""
        while True:
            await asyncio.sleep(interval)
            if not self.trade_manager:
                continue

            new_external = []
            old_results = {t["id"]: t.get("result") for t in self.trade_manager.trades if t.get("id")}
            async with self._iq_lock:
                # 1) live balance
                try:
                    bal = await asyncio.wait_for(asyncio.to_thread(self.iq.get_balance), timeout=10)
                    state_store["balance"] = round(bal or 0, 2)
                except Exception:
                    pass
                # 2) finalize trades that have expired (open -> WIN/LOSS)
                try:
                    await asyncio.wait_for(asyncio.to_thread(self.trade_manager.check_results), timeout=20)
                except Exception as e:
                    logger.warning(f"[RESULT] sync-loop check failed: {e}")
                # 3) discover platform/web-opened trades (in-memory, fast)
                try:
                    new_external = await asyncio.to_thread(self.trade_manager.sync_external_trades)
                except Exception as e:
                    logger.warning(f"[SYNC] external sync failed: {e}")

            # alert + log results for trades that just closed
            today_stats = self.trade_manager.get_stats().get("today")
            for t in self.trade_manager.trades:
                if t.get("id") and old_results.get(t["id"]) is None and t.get("result"):
                    icon = {"WIN": "✅", "LOSS": "❌", "EQUAL": "➖"}.get(t["result"], "•")
                    pnl = t.get("pnl") or 0
                    self.log_activity(icon, f"ปิดไม้ {t['asset']} {t['direction']} → {t['result']} ({pnl:+.2f})",
                                      level="error" if t["result"] == "LOSS" else "info", phase="result")
                    asyncio.create_task(self.tg.alert_result(t, today_stats))

            # alert any newly discovered platform trades
            for t in new_external:
                logger.info(f"[SYNC] Broadcasting external trade {t['asset']} {t['direction']}")
                if t.get("status") == "open":
                    asyncio.create_task(self.tg.alert_trade_open(t))
                await broadcast({"type": "new_trade", "data": t})

            # always push a fresh snapshot so balance / open count / stats stay current
            state_store["trades"] = self.trade_manager.trades
            state_store["risk"] = self.build_risk()
            state_store["stats"] = self.trade_manager.get_stats()
            await broadcast({"type": "update", "data": {
                "balance": state_store["balance"],
                "trades": self.trade_manager.trades,
                "risk": state_store["risk"],
                "stats": state_store["stats"],
                "status": state_store.get("status"),
                "activity": state_store["activity"],
                "activity_log": state_store["activity_log"],
            }})

    async def main_loop(self):
        if not await asyncio.to_thread(self.connect):
            logger.error("[BOT] IQ Option connection failed — stopping")
            state_store["status"] = "connection_failed"
            await broadcast({"type": "update", "data": state_store})
            return

        # Push balance + initial state immediately so the dashboard fills in before the
        # first (possibly slow) scan cycle finishes.
        try:
            bal = await asyncio.wait_for(asyncio.to_thread(self.iq.get_balance), timeout=15)
        except Exception:
            bal = state_store.get("balance", 0)
        state_store.update({
            "account_type": self.cfg.account_type,
            "config": self.build_config(),
            "risk": self.build_risk(),
            "trades": self.trade_manager.trades,
            "stats": self.trade_manager.get_stats(),
            "balance": round(bal or 0, 2),
            "status": "running",
        })
        await broadcast({"type": "update", "data": state_store})

        await self.tg.alert_bot_start(
            account_type=self.cfg.account_type,
            assets=self.cfg.assets,
            timeframe=self.cfg.timeframe,
            trade_amount=self.cfg.trade_amount,
            confidence_threshold=self.cfg.confidence_threshold,
        )

        self.running = True
        logger.info(f"[BOT] Main loop — aligned to {self.cfg.timeframe}s candle close")

        while self.running:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"[LOOP] {e}", exc_info=True)
                state_store["status"] = f"error"
            # Sleep until just after the next candle closes, so each cycle acts on a freshly
            # closed candle (not at an arbitrary offset within the candle).
            tf = self.cfg.timeframe
            now = time.time()
            wait = tf - (now % tf) + 2  # +2s buffer so the broker has finalized the candle
            await asyncio.sleep(wait)


# ─────────────────────────────────────────────────
#  WEBSOCKET SERVER (with command handling)
# ─────────────────────────────────────────────────
_bot_ref: FullTradingBot = None


async def ws_handler_with_cmds(websocket):
    connected_clients.add(websocket)
    try:
        await websocket.send(json.dumps({"type": "state", "data": state_store}))
        async for raw in websocket:
            try:
                msg = json.loads(raw)
                if msg.get("type") == "cmd" and _bot_ref:
                    await _bot_ref.handle_command(
                        msg.get("action", ""),
                        **{k: v for k, v in msg.items() if k not in ("type", "action")}
                    )
                elif msg.get("type") == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))
            except Exception as e:
                logger.debug(f"[WS] Message error: {e}")
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        connected_clients.discard(websocket)


async def ws_server_full():
    async with websockets.serve(ws_handler_with_cmds, "localhost", 8765):
        logger.info("[WS] Dashboard at ws://localhost:8765 -> open frontend/dashboard.html")
        await asyncio.Future()


# ─────────────────────────────────────────────────
#  ENTRYPOINT
# ─────────────────────────────────────────────────
async def main():
    global _bot_ref

    email    = os.getenv("IQ_EMAIL", "")
    password = os.getenv("IQ_PASSWORD", "")

    # Debug: show what credentials were loaded (mask password)
    print(f"[CFG] IQ_EMAIL    = '{email}'")
    print(f"[CFG] IQ_PASSWORD = '{'*' * len(password) if password else '(empty)'}'")
    print(f"[CFG] IQ_ACCOUNT  = '{os.getenv('IQ_ACCOUNT', 'PRACTICE')}'")

    if not email or not password:
        print("\n[ERROR] Email or password is empty!")
        print("  → Make sure .env file exists next to main.py (or in parent folder)")
        print("  → Content should be:")
        print("      IQ_EMAIL=your@email.com")
        print("      IQ_PASSWORD=yourpassword")
        return

    assets_env = os.getenv("IQ_ASSETS", "AUTO").strip()
    auto_assets = assets_env.upper() == "AUTO"

    cfg = TradingConfig(
        email=email,
        password=password,
        account_type=os.getenv("IQ_ACCOUNT", "PRACTICE"),
        assets=None if auto_assets else assets_env.split(","),
        auto_discover_assets=auto_assets,
        max_assets=int(os.getenv("IQ_MAX_ASSETS", "12")),
        timeframe=int(os.getenv("IQ_TIMEFRAME", "300")),
        trade_amount=float(os.getenv("IQ_AMOUNT", "50.0")),
        confidence_threshold=float(os.getenv("IQ_CONFIDENCE", "70.0")),
        max_consecutive_losses=int(os.getenv("IQ_MAX_LOSSES", "4")),
        max_open_positions=int(os.getenv("IQ_MAX_OPEN", "3")),
        max_trades_per_day=int(os.getenv("IQ_MAX_DAY_TRADES", "20")),
        daily_profit_target=float(os.getenv("IQ_DAILY_TARGET", "200.0")),
        daily_loss_limit=float(os.getenv("IQ_DAILY_LOSS_LIMIT", "0")),
    )

    # Dashboard-saved settings override env
    apply_runtime_config(cfg, load_runtime_config())

    tg_cfg = TelegramConfig(
        bot_token=os.getenv("TG_TOKEN", ""),
        chat_id=os.getenv("TG_CHAT_ID", ""),
        min_confidence=80.0,
        enabled=bool(os.getenv("TG_TOKEN")),
    )

    tg = TelegramBot(tg_cfg)
    bot = FullTradingBot(cfg, tg)
    _bot_ref = bot

    await asyncio.gather(
        ws_server_full(),
        bot.main_loop(),
        bot.external_sync_loop(),
    )


if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    os.makedirs("data", exist_ok=True)
    # Make console + file logging UTF-8 so Thai text and emoji don't throw cp1252 errors on Windows
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("logs/trading.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    asyncio.run(main())
