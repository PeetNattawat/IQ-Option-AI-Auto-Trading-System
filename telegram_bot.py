"""
Telegram Alert System
Clean, easy-to-read alerts (Thai): bot on/off, order placement with reasons, trade result summary.
"""

import aiohttp
import asyncio
import html
import logging
from datetime import datetime
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_TF_LABEL = {60: "M1", 300: "M5", 900: "M15", 1800: "M30"}


def _h(v) -> str:
    """Escape a dynamic value for Telegram HTML parse_mode."""
    return html.escape(str(v)) if v is not None else ""


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
        self._sent_signals: set = set()

    async def send(self, text: str) -> bool:
        if not self.cfg.enabled or not self.cfg.bot_token:
            return False
        url = f"{self.BASE}{self.cfg.bot_token}/sendMessage"
        payload = {"chat_id": self.cfg.chat_id, "text": text,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    ok = r.status == 200
                    if not ok:
                        logger.warning(f"[TG] HTTP {r.status}: {await r.text()}")
                    return ok
        except Exception as e:
            logger.error(f"[TG] Send error: {e}")
            return False

    async def get_updates(self, offset=None, timeout: int = 25) -> list:
        """Long-poll Telegram for incoming commands. Returns the raw 'result' list."""
        if not self.cfg.enabled or not self.cfg.bot_token:
            return []
        url = f"{self.BASE}{self.cfg.bot_token}/getUpdates"
        params = {"timeout": timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=timeout + 10)) as r:
                    if r.status != 200:
                        return []
                    data = await r.json()
            return data.get("result", []) or []
        except Exception as e:
            logger.debug(f"[TG] getUpdates error: {e}")
            return []

    # ── helpers ──
    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%H:%M:%S")

    @staticmethod
    def _src_label(source: str) -> str:
        return "🤖 Bot" if source == "auto" else "✋ Manual"

    @staticmethod
    def _pretty_asset(asset) -> str:
        """Drop IQ's '-op' real-option suffix for display (GBPNZD-op -> GBPNZD)."""
        a = str(asset or "?")
        return a[:-3] if a.endswith("-op") else a

    @staticmethod
    def _dir_label(direction: str) -> str:
        return "CALL 🟢▲" if direction == "CALL" else ("PUT 🔴▼" if direction == "PUT" else direction)

    @staticmethod
    def _tf_label(timeframe) -> str:
        return _TF_LABEL.get(timeframe, f"{timeframe}s")

    # ─────────────────────────────────────────
    #  1) SYSTEM ON / OFF
    # ─────────────────────────────────────────
    async def alert_bot_start(self, *, account_type: str, assets: list, timeframe: int,
                              trade_amount: float, confidence_threshold: float) -> bool:
        acc = "🔴 REAL — เงินจริง" if account_type == "REAL" else "🟢 PRACTICE — เงินทดลอง"
        tf_label = _h(self._tf_label(timeframe))
        text = (
            f"🟢 <b>บอทเริ่มทำงานแล้ว</b>\n"
            f"\n"
            f"💼 บัญชี: <b>{acc}</b>\n"
            f"📈 คู่เงิน: <b>{len(assets)} คู่</b>\n"
            f"⏱ ไทม์เฟรม: <b>{tf_label}</b> · หมดอายุ {timeframe // 60} นาที\n"
            f"💵 เงินต่อไม้: <b>฿{trade_amount:.0f}</b>\n"
            f"🎯 เข้าเทรดเมื่อมั่นใจ ≥ <b>{confidence_threshold:.0f}%</b>\n"
            f"\n"
            f"🕐 {self._now()}"
        )
        return await self.send(text)

    async def alert_bot_paused(self, today: dict = None) -> bool:
        text = (
            f"⏸ <b>หยุดบอทชั่วคราว</b>\n"
            f"หยุดเปิดออเดอร์ใหม่ (ไม้ที่เปิดอยู่ยังเดินต่อ)\n"
            f"{self._today_line(today)}"
            f"\n🕐 {self._now()}"
        )
        return await self.send(text)

    async def alert_bot_resumed(self) -> bool:
        return await self.send(f"▶️ <b>บอทกลับมาเทรดต่อแล้ว</b>\n🕐 {self._now()}")

    @staticmethod
    def _today_line(today: dict) -> str:
        if not today:
            return ""
        wins, losses = today.get("wins", 0), today.get("losses", 0)
        pnl = today.get("pnl", 0) or 0
        sign = "+" if pnl >= 0 else ""
        return f"📊 วันนี้: {wins}ชนะ / {losses}แพ้ · {sign}{pnl:.2f} บาท\n"

    # ─────────────────────────────────────────
    #  2) ORDER PLACED (with decision reasons)
    # ─────────────────────────────────────────
    async def alert_trade_open(self, trade: dict) -> bool:
        direction = trade.get("direction", "?")
        conf = trade.get("confidence")
        conf_line = (f"🎯 ความมั่นใจ: <b>{conf:.0f}%</b>  {_conf_bar(conf)}\n"
                     if conf is not None else "")

        # compact indicator glance (auto trades carry these)
        metrics = []
        if trade.get("rsi") is not None:
            metrics.append(f"RSI {trade['rsi']:.0f}")
        if trade.get("atr") is not None:
            metrics.append(f"ATR {trade['atr']:.5f}")
        if trade.get("adx") is not None:
            metrics.append(f"ADX {trade['adx']:.0f}")
        metric_line = f"📐 {' · '.join(metrics)}\n" if metrics else ""

        # escape < > & — EMA reasons contain '<'/'>' which break Telegram HTML parse mode
        reasons = [html.escape(r) for r in (trade.get("reasons") or []) if r][:4]
        if reasons:
            reason_block = "📋 <b>เหตุผลที่เข้า:</b>\n" + "\n".join(f"  ✅ {r}" for r in reasons) + "\n"
        else:
            reason_block = ""

        mg_line = f"🎲 ไม้ทบ (Martingale): สเต็ป {trade['mg_step']}\n" if trade.get("mg_step") else ""

        asset_name = _h(self._pretty_asset(trade.get('asset')))
        expiry = _h(trade.get('expiry', '?'))
        text = (
            f"🚀 <b>ออกออเดอร์ {self._dir_label(direction)}</b>\n"
            f"\n"
            f"📌 คู่เงิน: <b>{asset_name}</b>\n"
            f"💵 ลงทุน: <b>฿{trade.get('amount', 0):.0f}</b>\n"
            f"{mg_line}"
            f"{conf_line}"
            f"⏱ หมดอายุ: <b>{expiry} นาที</b>\n"
            f"👤 ที่มา: {self._src_label(trade.get('source', 'auto'))}\n"
            f"{metric_line}"
            f"{reason_block}"
            f"\n🕐 {self._now()}"
        )
        ok = await self.send(text)
        if ok:
            logger.info(f"[TG] Order alert: {trade.get('asset')} {direction}")
        return ok

    # ─────────────────────────────────────────
    #  3) TRADE RESULT SUMMARY
    # ─────────────────────────────────────────
    async def alert_result(self, trade: dict, today: dict = None) -> bool:
        result = trade.get("result")
        head = {"WIN": "✅ <b>ผล: ชนะ</b> 🎉",
                "LOSS": "❌ <b>ผล: แพ้</b>",
                "EQUAL": "➖ <b>ผล: เสมอ</b> (คืนทุน)"}.get(result, "<b>ผล: ?</b>")

        trade_pnl = trade.get("pnl", 0) or 0
        amount = trade.get("amount", 0) or 0
        trade_sign = "+" if trade_pnl >= 0 else ""
        pnl_label = "กำไร" if trade_pnl >= 0 else "ขาดทุน"
        conf = trade.get("confidence")
        conf_line = f"🎯 ความมั่นใจ: {conf:.0f}%\n" if conf is not None else ""
        mg_line = f"🎲 ไม้ทบ: สเต็ป {trade['mg_step']}\n" if trade.get("mg_step") else ""

        # Build a clear "สรุปวันนี้" line directly from `today` to avoid slicing issues
        if today:
            wins, losses = today.get("wins", 0), today.get("losses", 0)
            day_pnl = today.get("pnl", 0) or 0
            day_sign = "+" if day_pnl >= 0 else ""
            summary_line = f"\n📈 สรุปวันนี้: {wins}ชนะ / {losses}แพ้ · {day_sign}{day_pnl:.2f} บาท\n"
        else:
            summary_line = ""

        asset_name = _h(self._pretty_asset(trade.get('asset')))
        text = (
            f"{head}\n"
            f"\n"
            f"📌 <b>{asset_name}</b> · {self._dir_label(trade.get('direction', '?'))}\n"
            f"👤 ที่มา: {self._src_label(trade.get('source', 'auto'))}\n"
            f"💰 ลงทุน ฿{amount:.0f} · {pnl_label} <b>{trade_sign}{trade_pnl:.2f} บาท</b>\n"
            f"{mg_line}"
            f"{conf_line}"
            f"{summary_line}"
            f"\n🕐 {self._now()}"
        )
        return await self.send(text)

    async def alert_expired(self, trade: dict) -> bool:
        """A trade that could not be resolved (IQ connection drop) and was force-expired
        to free the open-position slot. No PnL impact — informational only."""
        asset_name = _h(self._pretty_asset(trade.get('asset')))
        text = (
            f"⏰ <b>ออเดอร์ค้าง — เคลียร์ช่องว่างแล้ว</b>\n"
            f"\n"
            f"📌 <b>{asset_name}</b> · {self._dir_label(trade.get('direction', '?'))}\n"
            f"👤 ที่มา: {self._src_label(trade.get('source', 'auto'))}\n"
            f"⚠️ ปิดผลไม่ได้ (IQ หลุดการเชื่อมต่อ) — ปล่อยช่องให้เทรดต่อ\n"
            f"ℹ️ ไม่นับแพ้/ชนะ ไม่กระทบยอด\n"
            f"\n🕐 {self._now()}"
        )
        return await self.send(text)

    # ─────────────────────────────────────────
    #  RISK / LEARNING
    # ─────────────────────────────────────────
    async def alert_risk_pause(self, reason: str) -> bool:
        text = (
            f"⚠️ <b>หยุดอัตโนมัติ (Risk)</b>\n"
            f"\n"
            f"{_h(reason)}\n"
            f"บอทหยุดเปิดออเดอร์ใหม่แล้ว\n"
            f"\n🕐 {self._now()}"
        )
        return await self.send(text)

    async def alert_learning(self, result: dict) -> bool:
        if not result.get("disabled_rules") and not result.get("warnings"):
            return False
        lines = ["🧠 <b>AI เรียนรู้และปรับกฎ</b>", ""]
        for r in result.get("disabled_rules", []):
            lines.append(f"🚫 ปิดกฎ: {_h(r['reason'])}")
        for w in result.get("warnings", []):
            lines.append(f"⚠️ {_h(w)}")
        lines.append(f"\n🕐 {self._now()}")
        return await self.send("\n".join(lines))


def _conf_bar(pct: float) -> str:
    filled = max(0, min(10, int(pct / 10)))
    return "█" * filled + "░" * (10 - filled)
