"""Send one of each new Telegram alert format so you can preview readability."""
import asyncio, os
from dotenv import load_dotenv
from telegram_bot import TelegramBot, TelegramConfig

load_dotenv()
tg = TelegramBot(TelegramConfig(
    bot_token=os.getenv("TG_TOKEN", ""),
    chat_id=os.getenv("TG_CHAT_ID", ""),
    enabled=True,
))

sample_open = {
    "asset": "EURUSD-OTC", "direction": "CALL", "amount": 50, "confidence": 75,
    "expiry": 5, "source": "auto", "mg_step": 2, "rsi": 55.3, "atr": 0.00143, "adx": 28.1,
    "reasons": [
        "EMA bull stack: 1.16086 > 1.16077 > 1.16040",
        "RSI 55.3 in bullish zone [50-70]",
        "MACD histogram bullish 0.00012",
        "ADX 28.1 — strong trend",
    ],
}
sample_win = {"asset": "EURUSD-OTC", "direction": "CALL", "amount": 50, "confidence": 75,
              "source": "auto", "mg_step": 2, "result": "WIN", "pnl": 43.0}
sample_loss = {"asset": "GBPJPY-OTC", "direction": "PUT", "amount": 50,
               "source": "manual", "result": "LOSS", "pnl": -50.0}
today = {"wins": 3, "losses": 1, "pnl": 130.0}

async def main():
    await tg.alert_bot_start(account_type="PRACTICE", assets=list(range(12)),
                             timeframe=300, trade_amount=50, confidence_threshold=70)
    await tg.alert_trade_open(sample_open)
    await tg.alert_result(sample_win, today)
    await tg.alert_result(sample_loss, today)
    await tg.alert_bot_paused(today)
    print("sent 5 preview alerts")

asyncio.run(main())
