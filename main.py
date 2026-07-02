"""
Main launcher — integrates TelegramBot into the trading loop
Run: python main.py
"""

import asyncio
import html
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
from http_server import run_http_server

logger = logging.getLogger(__name__)

RUNTIME_CONFIG_PATH = Path("data/config.json")


def _h(v) -> str:
    """Escape a dynamic value for Telegram HTML parse_mode."""
    return html.escape(str(v)) if v is not None else ""


def load_runtime_config() -> dict:
    try:
        with open(RUNTIME_CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# Settings that can be changed from the dashboard and persist across restarts.
# timeframe is intentionally env-only (IQ_TIMEFRAME) — not a runtime field — to keep
# the timeframe/expiry pair consistent. See item 8 note below.
# A/B candidate: M15 (IQ_TIMEFRAME=900) with expiry_minutes=30-45 (set in config.json).
RUNTIME_FIELDS = [
    "trade_amount", "confidence_threshold", "expiry_minutes", "max_open_positions",
    "martingale_enabled", "martingale_base", "martingale_multiplier", "martingale_max_steps",
    "max_trades_per_day", "max_consecutive_losses", "loss_cooldown_minutes",
    "daily_profit_target", "daily_loss_limit",
    "adx_min", "dir_margin",                         # quality gates (item 4)
    "rsi_call_min", "rsi_call_max",                  # RSI zones (item 5)
    "rsi_put_min", "rsi_put_max",
]


def save_runtime_config(cfg: TradingConfig):
    os.makedirs("data", exist_ok=True)
    with open(RUNTIME_CONFIG_PATH, "w") as f:
        json.dump({k: getattr(cfg, k) for k in RUNTIME_FIELDS}, f, indent=2)


def apply_runtime_config(cfg: TradingConfig, rt: dict):
    """Apply persisted/dashboard settings onto cfg, coercing to the field's type.
    Martingale OFF (default): max_consecutive_losses is left as-is so the 4-loss
    cooldown fires independently of the (disabled) martingale ladder."""
    for k in RUNTIME_FIELDS:
        if k not in rt:
            continue
        cur = getattr(cfg, k, None)
        if cur is None:
            continue
        try:
            val = bool(rt[k]) if isinstance(cur, bool) else type(cur)(rt[k])
        except (TypeError, ValueError):
            continue
        setattr(cfg, k, val)
    # When martingale is ON: keep base stake in sync and ensure the consecutive-loss
    # pause only fires after the full ladder has been exhausted.
    # When OFF: do NOT touch max_consecutive_losses — let the configured value (e.g. 4) stand.
    if cfg.martingale_enabled:
        cfg.trade_amount = cfg.martingale_base
        cfg.max_consecutive_losses = max(cfg.max_consecutive_losses, cfg.martingale_max_steps)
        # Global single Martingale ladder — only 1 auto trade at a time
        cfg.max_open_positions = 1


# ─────────────────────────────────────────────────
#  ENHANCED BOT WITH TELEGRAM
# ─────────────────────────────────────────────────
class FullTradingBot(TradingBot):

    def __init__(self, cfg: TradingConfig, tg: TelegramBot):
        super().__init__(cfg)
        self.tg = tg
        self._cmd_queue: asyncio.Queue = asyncio.Queue()
        self._paused = False
        self._cooldown_until = 0.0  # epoch secs; > now = in loss-cooldown (auto-resumes, NOT a hard pause)
        self._iq_lock = asyncio.Lock()  # serialize IQ network calls between run_cycle and the sync loop
        self._alerted_rule_ids: set = set()  # track rule id ที่แจ้ง Telegram ไปแล้ว — ป้องกันแจ้งซ้ำทุกรอบ
        self._prev_asset_status: dict = {}   # snapshot of last known asset_status — used for open/close transition alerts
        self._start_notified: bool = False   # fire alert_bot_start only after first successful resolve
        self._watchdog_heartbeat: float = time.time()  # updated every cycle + during inter-cycle sleep

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
            "loss_cooldown_minutes": self.cfg.loss_cooldown_minutes,
            "max_open_positions": self.cfg.max_open_positions,
            "daily_profit_target": self.cfg.daily_profit_target,
            "daily_loss_limit": self.cfg.daily_loss_limit,
            # Quality gates — dashboard can read/override these (items 4 & 5)
            "adx_min": self.cfg.adx_min,
            "dir_margin": self.cfg.dir_margin,
            "rsi_call_min": self.cfg.rsi_call_min,
            "rsi_call_max": self.cfg.rsi_call_max,
            "rsi_put_min": self.cfg.rsi_put_min,
            "rsi_put_max": self.cfg.rsi_put_max,
        }

    # ── Override run_cycle to send Telegram alerts ──
    async def run_cycle(self):
        if self._paused:
            state_store["status"] = "paused"
            await broadcast({"type": "update", "data": state_store})
            return

        self.loop_count += 1
        self._watchdog_heartbeat = time.time()
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

        # Resolve tradable real-forex pairs (ranked by payout). Re-check until we have some,
        # then hourly / on demand.
        if not self._assets_resolved or self.loop_count % 12 == 1 or getattr(self, "_need_resolve", False):
            try:
                await asyncio.wait_for(asyncio.to_thread(self.resolve_assets), timeout=60)
                self._need_resolve = False
            except Exception as e:
                logger.warning(f"[ASSET] resolve failed: {e}")

        # Send the bot-start Telegram alert the first time we have a confirmed asset list.
        # Firing here (after resolve) guarantees the pair count is accurate even if the initial
        # resolve in main_loop timed out or found no open pairs.
        if not self._start_notified and self.cfg.assets:
            self._start_notified = True
            asyncio.create_task(self.tg.alert_bot_start(
                account_type=self.cfg.account_type,
                assets=self.cfg.assets,
                timeframe=self.cfg.timeframe,
                trade_amount=self.cfg.trade_amount,
                confidence_threshold=self.cfg.confidence_threshold,
            ))

        # T5 — Transition detection: fire Telegram alerts when market opens or closes.
        # Skip on first call (_prev_asset_status empty) to avoid flooding alerts at startup.
        _current_asset_status = state_store.get("asset_status", {})
        if self._prev_asset_status and _current_asset_status:
            for _asset, _info in _current_asset_status.items():
                _prev = self._prev_asset_status.get(_asset, {})
                if _prev.get("status") != "open" and _info.get("status") == "open":
                    asyncio.create_task(self.tg.alert_market_open(
                        _asset, _info.get("kind"), _info.get("payout")
                    ))
                elif _prev.get("status") == "open" and _info.get("status") != "open":
                    asyncio.create_task(self.tg.alert_market_closed(_asset))
        if _current_asset_status:
            self._prev_asset_status = {k: dict(v) for k, v in _current_asset_status.items()}

        # No real forex pairs open (weekend / outside market hours) — wait, never trade OTC
        if not self.cfg.assets:
            self.log_activity("💤", "ตลาดคู่เงินจริงปิดอยู่ (สุดสัปดาห์/นอกเวลาทำการ) — รอตลาดเปิด · ไม่เทรด OTC", phase="waiting")
            state_store.update({"status": "running", "signals": [],
                                "risk": self.build_risk(), "stats": self.trade_manager.get_stats()})
            await broadcast({"type": "update", "data": {
                "status": "running", "signals": [], "risk": state_store["risk"],
                "stats": state_store["stats"], "balance": state_store["balance"],
                "activity": state_store["activity"], "activity_log": state_store["activity_log"],
                "asset_status": state_store.get("asset_status", {}),
                "asset_status_updated_at": state_store.get("asset_status_updated_at"),
            }})
            return

        self.log_activity("🔍", f"กำลังสแกน {len(self.cfg.assets)} คู่ (รอบที่ {self.loop_count})", phase="scanning")

        try:
            balance = await asyncio.wait_for(asyncio.to_thread(self.iq.get_balance), timeout=15)
        except Exception:
            balance = state_store.get("balance", 0)
        state_store["balance"] = round(balance, 2)

        candidates = []  # qualifying CALL/PUT signals this cycle
        got_data = False
        for asset in self.cfg.assets:
            # T3 — Scan gate: safety net for race condition between resolve cycles.
            # cfg.assets is open-only after resolve, but market can close mid-cycle.
            if asset not in self.cfg.asset_kind:
                logger.info(f"[SCAN] Skipping {asset} — market closed")
                continue
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
                f"RSI: {signal.rsi:5.1f} | ATR: {signal.atr:.5f} | "
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

        # Smart selection: highest-confidence signal first. With max_open_positions=1 the bot
        # waits for ANY open position (auto, manual, web) to close before entering a new one.
        # slots_free is re-read from active_orders live each iteration — never a stale counter.
        candidates.sort(key=lambda s: s.confidence, reverse=True)
        placed = None
        in_cooldown = time.time() < self._cooldown_until
        if in_cooldown:
            remain = int((self._cooldown_until - time.time()) / 60) + 1
            self.log_activity("⏳", f"พักหลังแพ้ติดกัน — เหลืออีก ~{remain} นาที จึงกลับมาเปิดออเดอร์", phase="cooldown")
        for signal in candidates:
            if in_cooldown:
                break
            # Re-query active_orders and expiry lock live — covers trades placed moments ago
            # or closed by external_sync_loop between iterations.
            if len(self.trade_manager.active_orders) >= self.cfg.max_open_positions:
                break
            if time.time() < self.trade_manager._auto_locked_until:
                break
            async with self._iq_lock:
                trade = await asyncio.to_thread(self.trade_manager.execute_trade, signal)
            if isinstance(trade, dict):
                placed = trade
                state_store["trades"] = self.trade_manager.trades
                await broadcast({"type": "new_trade", "data": trade})
                asyncio.create_task(self.tg.alert_trade_open(trade))
                mg = f" · ไม้ {trade.get('mg_step')}" if trade.get("mg_step") else ""
                self.log_activity("🚀", f"เปิดออเดอร์ {trade['asset']} {trade['direction']} ที่ {(trade.get('confidence') or 0):.0f}%{mg}", phase="trading")
                break
            elif trade is None:
                # Risk block, veto, or order error — stop this cycle
                break
            # else: _ORDER_UNAVAILABLE sentinel — asset not available, try next signal

        # Summarize the decision so the dashboard shows what the bot is doing / waiting for
        best = max(signals_this_cycle, key=lambda s: s.get("confidence") or 0, default=None)
        best_txt = f"{best['asset']} {best['signal']} {(best.get('confidence') or 0):.0f}%" if best else "-"
        if not got_data:
            self.log_activity("⚠️", "ดึงแท่งเทียนไม่ได้สักคู่ — ตลาดอาจปิดหรือการเชื่อมต่อ IQ มีปัญหา", level="warn", phase="error")
        elif placed is None and candidates:
            if len(self.trade_manager.active_orders) >= self.cfg.max_open_positions:
                reason = f"รอปิดไม้ที่เปิดอยู่ก่อน ({len(self.trade_manager.active_orders)} open)"
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

        # Risk: COOL DOWN (not a permanent pause) after a run of losses, then auto-resume.
        # A hard pause here used to deadlock: /start cleared _paused but consecutive_losses
        # stayed at the cap, so can_trade() blocked every entry and the bot never traded again.
        # Now we pause new entries for loss_cooldown_minutes, reset the counter so the cooldown
        # timer is the only gate, and resume automatically when it expires.
        if (self.trade_manager.consecutive_losses >= self.cfg.max_consecutive_losses
                and time.time() >= self._cooldown_until):
            lost = self.trade_manager.consecutive_losses
            cd = max(1, self.cfg.loss_cooldown_minutes)
            self._cooldown_until = time.time() + cd * 60
            self.trade_manager.consecutive_losses = 0  # reset so the cooldown timer is the only gate
            msg = f"{self.cfg.max_consecutive_losses} consecutive losses — cooling down {cd} min, then auto-resume"
            logger.warning(f"[RISK] {msg}")
            self.log_activity("🛑", f"แพ้ติดกัน {lost} ไม้ — พักเทรด {cd} นาที แล้วกลับมาต่ออัตโนมัติ", level="error", phase="cooldown")
            asyncio.create_task(self.tg.alert_risk_pause(msg))
        # auto-resume notice when a loss-cooldown has just elapsed
        elif self._cooldown_until and time.time() >= self._cooldown_until:
            self._cooldown_until = 0.0
            self.log_activity("▶️", "ครบเวลาพักหลังแพ้ติดกัน — กลับมาเปิดออเดอร์ต่อ", phase="running")
            asyncio.create_task(self.tg.alert_bot_resumed())

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

            # แจ้ง Telegram เฉพาะ rule ที่ยังไม่เคยแจ้ง — ป้องกันแจ้งซ้ำทุกรอบ
            disabled_rules = lr.get("disabled_rules") or []
            new_rule_ids = {r["id"] for r in disabled_rules if r["id"] not in self._alerted_rule_ids}
            has_new_warnings = bool(lr.get("warnings")) and not disabled_rules  # warnings-only แจ้งครั้งแรกเสมอ
            if new_rule_ids or has_new_warnings:
                asyncio.create_task(self.tg.alert_learning(lr))
                self._alerted_rule_ids.update(new_rule_ids)

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
            "asset_status": state_store.get("asset_status", {}),
            "asset_status_updated_at": state_store.get("asset_status_updated_at"),
        }})

    # ── Command handler from WebSocket ──
    async def handle_command(self, cmd: str, **kwargs):
        if cmd == "start":
            was_paused = self._paused or time.time() < self._cooldown_until
            self._paused = False
            # Clear any loss-cooldown and reset the streak counter — an explicit resume must
            # actually let the bot trade again (otherwise can_trade() keeps blocking on the old
            # consecutive_losses count and the bot scans forever without placing an order).
            self._cooldown_until = 0.0
            if self.trade_manager:
                self.trade_manager.consecutive_losses = 0
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
                settings["max_open_positions"] = 1  # always 1: global single Martingale ladder
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

    # ── Telegram remote control ──
    def _tg_summary_text(self) -> str:
        tm = self.trade_manager
        if not tm:
            return "ยังไม่ได้เชื่อมต่อ"
        t = tm.get_stats().get("today", {})
        pnl = t.get("pnl", 0) or 0
        sign = "+" if pnl >= 0 else ""
        bot, man = t.get("bot", {}), t.get("manual", {})
        return (
            f"📊 <b>สรุปวันนี้</b>\n\n"
            f"💰 กำไร/ขาดทุน: <b>{sign}{pnl:.2f} บาท</b>\n"
            f"📈 เทรด: <b>{t.get('total', 0)}</b> ไม้ "
            f"(ชนะ {t.get('wins', 0)} / แพ้ {t.get('losses', 0)} / เสมอ {t.get('equals', 0)})\n"
            f"🎯 อัตราชนะ: <b>{t.get('win_rate', 0)}%</b>\n"
            f"🤖 บอท: ชนะ {bot.get('wins',0)} / แพ้ {bot.get('losses',0)} · "
            f"✋ มือ: ชนะ {man.get('wins',0)} / แพ้ {man.get('losses',0)}\n"
            f"💵 ยอดเงิน: <b>฿{state_store.get('balance', 0):,.2f}</b>\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )

    def _tg_dashboard_text(self) -> str:
        signals = state_store.get("signals") or []
        assets = self.cfg.assets or []
        threshold = self.cfg.confidence_threshold

        if not assets:
            return "💤 <b>ไม่มีคู่เงินที่สแกนอยู่</b>\n(ตลาดปิด หรือบอทยังไม่ได้รัน)"

        lines = [f"📡 <b>คู่เงินที่สแกนอยู่ ({len(assets)} คู่)</b>\n"]

        sig_by_asset = {s["asset"]: s for s in signals}

        for asset in assets:
            raw_name = asset[:-3] if asset.endswith("-op") else asset  # drop IQ '-op' suffix for display
            name = _h(raw_name)
            s = sig_by_asset.get(asset)
            if not s:
                lines.append(f"⬜ <b>{name}</b> — รอข้อมูล")
                continue

            sig = s.get("signal", "HOLD")
            conf = s.get("confidence") or 0
            rsi = s.get("rsi")
            adx = s.get("adx")

            if sig == "CALL":
                sig_icon = "🟢 CALL▲"
            elif sig == "PUT":
                sig_icon = "🔴 PUT▼"
            else:
                sig_icon = "⚪ HOLD"

            ready = sig in ("CALL", "PUT") and conf >= threshold
            star = " ⭐" if ready else ""

            detail_parts = []
            if rsi is not None:
                detail_parts.append(f"RSI {rsi:.0f}")
            if adx is not None:
                detail_parts.append(f"ADX {adx:.0f}")
            detail = " · ".join(detail_parts)

            lines.append(
                f"{'🔥' if ready else '  '} <b>{name}</b> {sig_icon} {conf:.0f}%{star}"
                + (f"\n     {detail}" if detail else "")
            )

        ready_count = sum(
            1 for s in signals
            if s.get("signal") in ("CALL", "PUT") and (s.get("confidence") or 0) >= threshold
        )
        lines.append(f"\n🎯 เข้าเงื่อนไข: <b>{ready_count}/{len(assets)}</b> คู่ (≥{threshold:.0f}%)")
        lines.append(f"🕐 {datetime.now().strftime('%H:%M:%S')}")
        return "\n".join(lines)

    def _tg_status_text(self) -> str:
        r = self.build_risk()
        act = html.escape((state_store.get("activity") or {}).get("msg", "-"))
        raw_status = state_store.get("status", "-")
        _status_map = {
            "running": "🟢 กำลังทำงาน",
            "stopped": "⏹ หยุดอยู่",
            "reconnecting": "🔄 กำลังเชื่อมต่อใหม่",
            "paused": "⏸ หยุดชั่วคราว",
            "connection_failed": "❌ เชื่อมต่อล้มเหลว",
        }
        if self._paused:
            status = "⏸ หยุดชั่วคราว"
        elif raw_status.startswith("error"):
            err_detail = _h(raw_status[6:].strip()) if len(raw_status) > 5 else ""
            status = f"❌ ผิดพลาด{': ' + err_detail if err_detail else ''}"
        else:
            status = _status_map.get(raw_status, _h(raw_status))
        return (
            f"🤖 <b>สถานะบอท</b>\n\n"
            f"สถานะ: <b>{status}</b>\n"
            f"บัญชี: <b>{self.cfg.account_type}</b>\n"
            f"💵 ยอดเงิน: <b>฿{state_store.get('balance', 0):,.2f}</b>\n"
            f"ไม้เปิดอยู่: {r.get('open',0)}/{r.get('max_open',3)} · "
            f"เทรดวันนี้: {r.get('today_trades',0)}\n"
            f"กำไรวันนี้: {r.get('daily_pnl',0):+.2f} บาท\n"
            f"กำลัง: {act}\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )

    async def handle_telegram_command(self, text: str, update_id: int | None = None):
        cmd = text.lstrip("/").split()[0].split("@")[0].lower()
        logger.info(f"[TG-CMD] received: /{cmd}")
        if cmd in ("start", "run", "resume", "go"):
            await self.handle_command("start")
            await self.tg.send("▶️ <b>เริ่มบอทแล้ว</b> — กลับไปสแกนหาสัญญาณ")
        elif cmd in ("stop", "pause"):
            await self.handle_command("stop")
            await self.tg.send("⏸ <b>หยุดบอทแล้ว</b> — ไม่เปิดออเดอร์ใหม่ (ไม้ที่เปิดอยู่ยังเดินต่อ)")
        elif cmd == "restart":
            await self.tg.send("🔄 <b>กำลังรีสตาร์ทบอท...</b> จะกลับมาออนไลน์ใน ~15 วินาที")
            logger.warning("[TG-CMD] restart requested — exiting (systemd will relaunch)")
            # Advance Telegram offset ก่อน exit เพื่อป้องกัน get_updates เจอ /restart ซ้ำตอน boot ใหม่
            if update_id is not None:
                try:
                    await self.tg.get_updates(offset=update_id + 1, timeout=1)
                except Exception:
                    pass
            await asyncio.sleep(1)
            os._exit(0)   # systemd Restart=always brings it back
        elif cmd in ("status", "stat", "s"):
            await self.tg.send(self._tg_status_text())
        elif cmd in ("summary", "pnl", "today", "sum"):
            await self.tg.send(self._tg_summary_text())
        elif cmd in ("dashboard", "pairs", "scan", "d"):
            await self.tg.send(self._tg_dashboard_text())
        else:
            await self.tg.send(
                "📋 <b>คำสั่งที่ใช้ได้</b>\n"
                "/start — เริ่ม/เล่นต่อ\n"
                "/stop — หยุดชั่วคราว\n"
                "/restart — รีสตาร์ทบอท\n"
                "/status — ดูสถานะตอนนี้\n"
                "/summary — สรุปกำไรวันนี้\n"
                "/dashboard — ดูคู่เงินที่สแกนอยู่"
            )

    async def telegram_command_loop(self):
        """Listen for Telegram commands (long-poll). Only the configured chat may control."""
        if not self.tg.cfg.enabled or not self.tg.cfg.bot_token:
            return
        allowed = str(self.tg.cfg.chat_id)
        # Skip any backlog so old messages don't trigger actions on startup
        offset = None
        try:
            backlog = await self.tg.get_updates(timeout=0)
            if backlog:
                offset = backlog[-1]["update_id"] + 1
        except Exception:
            pass
        await self.tg.send("🎮 พร้อมรับคำสั่ง: /start /stop /restart /status /summary /dashboard")
        while True:
            try:
                updates = await self.tg.get_updates(offset=offset, timeout=25)
            except Exception as e:
                logger.debug(f"[TG-CMD] poll error: {e}")
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id", ""))
                txt = (msg.get("text") or "").strip()
                if allowed and chat != allowed:
                    logger.warning(f"[TG-CMD] ignored command from unauthorized chat {chat}")
                    continue
                if txt.startswith("/"):
                    try:
                        await self.handle_telegram_command(txt, update_id=u["update_id"])
                    except Exception as e:
                        logger.error(f"[TG-CMD] handler error: {e}")

    async def watchdog_loop(self, check_interval: int = 30, freeze_timeout: int = 180):
        """Monitor _watchdog_heartbeat every check_interval seconds.
        The heartbeat is pulsed at the start of every run_cycle AND every 30s during
        the inter-cycle sleep, so the watchdog correctly distinguishes a normal sleep
        from a genuine deadlock (e.g. _iq_lock held by a stalled coroutine).
        Log CRITICAL and call os._exit(1) so systemd restarts the process cleanly."""
        _frozen_since: float = 0.0

        while True:
            await asyncio.sleep(check_interval)

            # Bot is intentionally paused — reset timer, don't count as freeze
            if self._paused:
                _frozen_since = 0.0
                continue

            now = time.time()
            seconds_since_heartbeat = now - self._watchdog_heartbeat

            if seconds_since_heartbeat < freeze_timeout:
                # Heartbeat is fresh — no freeze
                _frozen_since = 0.0
                continue

            # Heartbeat is stale — bot may be frozen
            if _frozen_since == 0.0:
                _frozen_since = now
                logger.warning(
                    f"[WATCHDOG] heartbeat stale for {seconds_since_heartbeat:.0f}s "
                    f"(loop_count={self.loop_count}) — starting freeze timer"
                )
                continue

            frozen_seconds = now - _frozen_since
            if frozen_seconds >= freeze_timeout:
                active_count = len(self.trade_manager.active_orders) if self.trade_manager else 0
                logger.critical(
                    f"[WATCHDOG] CRITICAL — bot frozen for {seconds_since_heartbeat:.0f}s "
                    f"(loop_count={self.loop_count}, active_orders={active_count}) — calling os._exit(1) for systemd restart"
                )
                os._exit(1)
            else:
                logger.warning(
                    f"[WATCHDOG] heartbeat still stale — frozen {seconds_since_heartbeat:.0f}s / {freeze_timeout * 2}s"
                )

    async def external_sync_loop(self, interval: int = 15):
        """Every ~15s keep the dashboard live: refresh balance, finalize closed trades,
        pull in platform-opened trades, and broadcast a fresh snapshot — independent of
        the 5-minute trading cycle."""
        while True:
            await asyncio.sleep(interval)
            if not self.trade_manager:
                continue

            new_external = []
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

            # alert + log every trade that just closed — drained from the engine's queue, so
            # this fires no matter which loop (5-min cycle / this loop / external sync) closed it
            today_stats = self.trade_manager.get_stats().get("today")
            for t in self.trade_manager.drain_pending_alerts():
                if t.get("result"):
                    icon = {"WIN": "✅", "LOSS": "❌", "EQUAL": "➖"}.get(t["result"], "•")
                    pnl = t.get("pnl") or 0
                    self.log_activity(icon, f"ปิดไม้ {t['asset']} {t['direction']} → {t['result']} ({pnl:+.2f})",
                                      level="error" if t["result"] == "LOSS" else "info", phase="result")
                    asyncio.create_task(self.tg.alert_result(t, today_stats))
                # Force-expired (unresolved, slot freed): notify so the user isn't left guessing
                elif t.get("status") == "expired":
                    self.log_activity("⏰", f"ออเดอร์ค้าง {t['asset']} {t['direction']} — เคลียร์ช่องแล้ว",
                                      level="warning", phase="result")
                    asyncio.create_task(self.tg.alert_expired(t))

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
                "asset_status": state_store.get("asset_status", {}),
                "asset_status_updated_at": state_store.get("asset_status_updated_at"),
            }})

    async def main_loop(self):
        if not await asyncio.to_thread(self.connect):
            logger.error("[BOT] IQ Option connection failed — stopping")
            state_store["status"] = "connection_failed"
            await broadcast({"type": "update", "data": state_store})
            return

        # Resolve tradable real-forex pairs up front so the start alert / dashboard agree
        # (otherwise the alert shows the 5 default majors while the dashboard shows 0).
        try:
            await asyncio.wait_for(asyncio.to_thread(self.resolve_assets), timeout=60)
        except Exception as e:
            logger.warning(f"[ASSET] initial resolve failed: {e}")

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

        self.running = True
        logger.info(f"[BOT] Main loop — aligned to {self.cfg.timeframe}s candle close")

        while self.running:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"[LOOP] {e}", exc_info=True)
                state_store["status"] = f"error: {e}"
            # Sleep until just after the next candle closes, so each cycle acts on a freshly
            # closed candle (not at an arbitrary offset within the candle).
            # Pulse the watchdog heartbeat every 30s so the watchdog doesn't mistake
            # a normal 300s inter-cycle sleep for a freeze.
            tf = self.cfg.timeframe
            now = time.time()
            wait = tf - (now % tf) + 2  # +2s buffer so the broker has finalized the candle
            while wait > 0:
                chunk = min(wait, 30)
                await asyncio.sleep(chunk)
                self._watchdog_heartbeat = time.time()
                wait -= chunk


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
    # ปิด log noise จาก probe connections (InvalidMessage/EOFError ก่อน handshake)
    logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
    async with websockets.serve(ws_handler_with_cmds, "0.0.0.0", 8765):
        logger.info("[WS] Dashboard at ws://0.0.0.0:8765 -> open frontend/dashboard.html")
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

    # ── REAL account validation gate (item 2) ──────────────────────────────
    # Trading on a REAL account requires an explicit opt-in via IQ_ALLOW_REAL=1.
    # If that flag is absent the bot forces PRACTICE regardless of IQ_ACCOUNT,
    # so a misconfigured env can never accidentally trade real money.
    raw_account_type = os.getenv("IQ_ACCOUNT", "PRACTICE").upper()
    if raw_account_type == "REAL" and os.getenv("IQ_ALLOW_REAL", "") != "1":
        raw_account_type = "PRACTICE"
        logger.warning(
            "WARNING: REAL blocked — validation gate: set IQ_ALLOW_REAL=1 to override. "
            "Forcing PRACTICE account."
        )
        print(
            "\n[WARNING] REAL account blocked by safety gate.\n"
            "          Set IQ_ALLOW_REAL=1 in your .env to enable REAL trading.\n"
            "          Falling back to PRACTICE.\n"
        )

    assets_env = os.getenv("IQ_ASSETS", "AUTO").strip()
    # Empty string or "AUTO" → full dynamic scan; anything else → explicit whitelist
    auto_assets = not assets_env or assets_env.upper() == "AUTO"
    _assets_list = None if auto_assets else [a.strip() for a in assets_env.split(",") if a.strip()]

    cfg = TradingConfig(
        email=email,
        password=password,
        account_type=raw_account_type,
        assets=_assets_list,
        auto_discover_assets=auto_assets,
        max_assets=int(os.getenv("IQ_MAX_ASSETS", "12")),
        timeframe=int(os.getenv("IQ_TIMEFRAME", "300")),
        trade_amount=float(os.getenv("IQ_AMOUNT", "50.0")),
        confidence_threshold=float(os.getenv("IQ_CONFIDENCE", "70.0")),
        max_consecutive_losses=int(os.getenv("IQ_MAX_LOSSES", "4")),
        loss_cooldown_minutes=int(os.getenv("IQ_LOSS_COOLDOWN", "30")),
        max_open_positions=int(os.getenv("IQ_MAX_OPEN", "1")),
        max_trades_per_day=int(os.getenv("IQ_MAX_DAY_TRADES", "20")),
        daily_profit_target=float(os.getenv("IQ_DAILY_TARGET", "200.0")),
        daily_loss_limit=float(os.getenv("IQ_DAILY_LOSS_LIMIT", "150.0")),  # match config default so a missing config.json never silently disables the loss limit
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
        run_http_server(),
        bot.main_loop(),
        bot.external_sync_loop(),
        bot.telegram_command_loop(),
        bot.watchdog_loop(),
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
