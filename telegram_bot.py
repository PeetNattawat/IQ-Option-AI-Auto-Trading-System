"""
Telegram Alert System
Sends signal alerts and trade results to Telegram channel
"""

import aiohttp
import asyncio
import logging
from datetime import datetime
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    min_confidence: float = 80.0
    enabled: bool = True


class TelegramBot:
    BASE = "https://api.telegram.org/bot"

    def __init__(self, cfg: TelegramConfig):
        self.cfg = cfg
        self._sent_signals: set = set()  # dedupe by asset+timestamp

    async def send(self, text: str) -> bool:
        if not self.cfg.enabled or not self.cfg.bot_token:
            return False
        url = f"{self.BASE}{self.cfg.bot_token}/sendMessage"
        payload = {"chat_id": self.cfg.chat_id, "text": text, "parse_mode": "HTML"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    ok = r.status == 200
                    if not ok:
                        logger.warning(f"[TG] HTTP {r.status}")
                    return ok
        except Exception as e:
            logger.error(f"[TG] Send error: {e}")
            return False

    async def alert_signal(self, signal) -> bool:
        """Send a signal alert if confidence >= threshold"""
        if signal.confidence < self.cfg.min_confidence:
            return False
        # Dedupe: same asset same minute
        key = f"{signal.asset}_{signal.timestamp[:16]}"
        if key in self._sent_signals:
            return False
        self._sent_signals.add(key)

        emo = "🚀" if signal.signal == "CALL" else "🔻"
        bar = _conf_bar(signal.confidence)
        reasons = "\n".join(f"  ✅ {r}" for r in (signal.reasons or [])[:5])
        tf_label = {60: "M1", 300: "M5", 900: "M15", 1800: "M30"}.get(signal.timeframe, f"{signal.timeframe}s")
        risk = "🟢 LOW" if signal.confidence >= 85 else "🟡 MEDIUM"

        text = (
            f"{emo} <b>SIGNAL ALERT</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 Pair: <b>{signal.asset}</b>\n"
            f"Direction: <b>{signal.signal}</b>\n"
            f"Confidence: <b>{signal.confidence:.0f}%</b> {bar}\n"
            f"Timeframe: <b>{tf_label}</b>\n"
            f"Entry: <b>{signal.entry_price:.5f}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 Indicators:\n{reasons}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Risk: {risk}\n"
            f"Expiry: <b>{signal.timeframe // 60} min</b>\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )
        ok = await self.send(text)
        if ok:
            logger.info(f"[TG] Alert sent: {signal.asset} {signal.signal} {signal.confidence:.0f}%")
        return ok

    async def alert_result(self, trade: dict) -> bool:
        """Send trade result"""
        emo = "✅" if trade.get("result") == "WIN" else "❌"
        pnl = trade.get("pnl", 0) or 0
        text = (
            f"{emo} <b>TRADE RESULT</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Pair: <b>{trade['asset']}</b>\n"
            f"Direction: <b>{trade['direction']}</b>\n"
            f"Result: <b>{trade.get('result', '?')}</b>\n"
            f"P&L: <b>{'+' if pnl >= 0 else ''}{pnl:.2f} USD</b>\n"
            f"Confidence was: {trade.get('confidence', 0):.0f}%\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )
        return await self.send(text)

    async def alert_risk_pause(self, reason: str) -> bool:
        text = (
            f"⚠️ <b>RISK PAUSE</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{reason}\n"
            f"Bot has paused trading.\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )
        return await self.send(text)

    async def alert_learning(self, result: dict) -> bool:
        if not result.get("disabled_rules") and not result.get("warnings"):
            return False
        lines = ["🧠 <b>AI LEARNING UPDATE</b>", "━━━━━━━━━━━━━━━━━━"]
        for r in result.get("disabled_rules", []):
            lines.append(f"🚫 Rule disabled: {r['reason']}")
        for w in result.get("warnings", []):
            lines.append(f"⚠️ {w}")
        lines.append(f"🕐 {datetime.now().strftime('%H:%M:%S')}")
        return await self.send("\n".join(lines))


def _conf_bar(pct: float) -> str:
    filled = int(pct / 10)
    return "█" * filled + "░" * (10 - filled)
