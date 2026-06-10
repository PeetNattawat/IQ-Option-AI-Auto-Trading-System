"""
Main launcher — integrates TelegramBot into the trading loop
Run: python main.py
"""

import asyncio
import json
import logging
import os
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


# ─────────────────────────────────────────────────
#  ENHANCED BOT WITH TELEGRAM
# ─────────────────────────────────────────────────
class FullTradingBot(TradingBot):

    def __init__(self, cfg: TradingConfig, tg: TelegramBot):
        super().__init__(cfg)
        self.tg = tg
        self._cmd_queue: asyncio.Queue = asyncio.Queue()
        self._paused = False

    # ── Override run_cycle to send Telegram alerts ──
    async def run_cycle(self):
        if self._paused:
            state_store["status"] = "paused"
            await broadcast({"type": "update", "data": state_store})
            return

        self.loop_count += 1
        signals_this_cycle = []
        try:
            balance = self.iq.get_balance()
        except Exception:
            balance = state_store.get("balance", 0)
        state_store["balance"] = round(balance, 2)

        for asset in self.cfg.assets:
            df = self.get_candles(asset)
            if df is None or len(df) < 60:
                logger.warning(f"[DATA] {asset}: not enough candles")
                continue

            df = self.indicator_engine.compute_all(df, self.cfg)
            signal = self.signal_engine.evaluate(df, asset)
            signals_this_cycle.append(asdict(signal))

            logger.info(
                f"[SIGNAL] {asset}: {signal.signal:4s} | "
                f"Conf: {signal.confidence:5.1f}% | "
                f"RSI: {signal.rsi:5.1f} | ATR: {signal.atr:.5f} | ADX: {signal.adx:.1f}"
            )

            # Send Telegram alert
            if signal.signal in ("CALL", "PUT"):
                asyncio.create_task(self.tg.alert_signal(signal))

            # Execute trade
            if signal.signal in ("CALL", "PUT") and signal.confidence >= self.cfg.confidence_threshold:
                trade = self.trade_manager.execute_trade(signal)
                if trade:
                    state_store["trades"] = self.trade_manager.trades
                    await broadcast({"type": "new_trade", "data": trade})

            # Store candles for chart
            cols = ["open", "high", "low", "close", "volume",
                    "ema_fast", "ema_slow", "ema_trend", "rsi", "atr", "macd_hist", "adx"]
            available = [c for c in cols if c in df.columns]
            state_store["candles"][asset] = df.tail(50)[available].to_dict("records")
            await asyncio.sleep(0.3)

        # Check results & send result alerts
        old_trades = {t["id"]: t.get("result") for t in self.trade_manager.trades if t.get("id")}
        self.trade_manager.check_results()
        for t in self.trade_manager.trades:
            if t.get("id") and old_trades.get(t["id"]) is None and t.get("result"):
                asyncio.create_task(self.tg.alert_result(t))

        # Risk: pause on consecutive losses
        if self.trade_manager.consecutive_losses >= self.cfg.max_consecutive_losses:
            self._paused = True
            msg = f"{self.cfg.max_consecutive_losses} consecutive losses — bot paused"
            logger.warning(f"[RISK] {msg}")
            asyncio.create_task(self.tg.alert_risk_pause(msg))

        stats = self.trade_manager.get_stats()
        state_store.update({
            "signals": signals_this_cycle,
            "trades": self.trade_manager.trades,
            "stats": stats,
            "status": "running",
        })

        # Learning every 30 cycles
        if self.loop_count % 30 == 0 and self.trade_manager.trades:
            lr = self.learning_engine.analyze(self.trade_manager.trades)
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
        }})

    # ── Command handler from WebSocket ──
    async def handle_command(self, cmd: str, **kwargs):
        if cmd == "start":
            self._paused = False
            self.running = True
            logger.info("[CMD] Bot started")
        elif cmd == "stop":
            self._paused = True
            state_store["status"] = "stopped"
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
                logger.info(f"[CMD] Switched to {account}")
            except Exception as e:
                logger.error(f"[CMD] Switch failed: {e}")

    async def main_loop(self):
        if not self.connect():
            logger.error("[BOT] IQ Option connection failed — stopping")
            state_store["status"] = "connection_failed"
            await broadcast({"type": "update", "data": state_store})
            return

        await self.tg.send(
            f"🟢 <b>Bot Started</b>\n"
            f"Account: {self.cfg.account_type}\n"
            f"Assets: {', '.join(self.cfg.assets)}\n"
            f"Timeframe: {self.cfg.timeframe}s\n"
            f"Min confidence: {self.cfg.confidence_threshold}%"
        )

        self.running = True
        logger.info(f"[BOT] Main loop — interval {self.cfg.timeframe}s")

        while self.running:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"[LOOP] {e}", exc_info=True)
                state_store["status"] = f"error"
            await asyncio.sleep(self.cfg.timeframe)


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
        logger.info("[WS] Dashboard at ws://localhost:8765  →  open frontend/dashboard.html")
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

    cfg = TradingConfig(
        email=email,
        password=password,
        account_type=os.getenv("IQ_ACCOUNT", "PRACTICE"),
        assets=os.getenv("IQ_ASSETS", "EURUSD,GBPUSD,AUDUSD,USDJPY").split(","),
        timeframe=int(os.getenv("IQ_TIMEFRAME", "300")),
        trade_amount=float(os.getenv("IQ_AMOUNT", "1.0")),
        confidence_threshold=float(os.getenv("IQ_CONFIDENCE", "70.0")),
        max_consecutive_losses=int(os.getenv("IQ_MAX_LOSSES", "3")),
    )

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
    )


if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    os.makedirs("data", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("logs/trading.log"),
            logging.StreamHandler(),
        ],
    )
    asyncio.run(main())
