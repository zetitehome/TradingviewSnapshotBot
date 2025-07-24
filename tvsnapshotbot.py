import os
import logging
import asyncio
import subprocess
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.types import Message, InputFile
from aiogram.enums.parse_mode import ParseMode
from aiohttp import web
from aiogram.webhook.aiohttp_server import setup_application

# === CONFIG ===
API_TOKEN = "8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA"
WEBHOOK_URL = "https://6c3090b3d7a5.ngrok-free.app/callback"  # Your ngrok https url + /callback
WEBAPP_HOST = "localhost"
WEBAPP_PORT = 3000
TELEGRAM_CHAT_ID = 6337160812  # Your Telegram chat ID

bot = Bot(token=API_TOKEN, parse_mode=ParseMode.MARKDOWN)
dp = Dispatcher()

# === HTML LOG FILE SETUP ===
HTML_LOG_FILE = "trade_logs.html"
if not os.path.exists(HTML_LOG_FILE):
    with open(HTML_LOG_FILE, "w", encoding="utf-8") as f:
        f.write(
            """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Trade Logs</title>
<style>
  body { font-family: Arial, sans-serif; padding: 1rem; background: #f5f5f5; }
  h2 { color: #2a9d8f; }
  ul#logs { list-style-type: none; padding-left: 0; }
  ul#logs li { background: #e0f7fa; margin-bottom: 0.5rem; padding: 0.5rem; border-radius: 5px; }
</style>
</head>
<body>
<h2>Trade Logs</h2>
<ul id="logs">
"""
        )

def log_to_html(message: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"<li>[{timestamp}] {message}</li>\n"
    with open(HTML_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry)

# === WEBHOOK ROUTE ===
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer("👋 Quantum Signal Bot is online and ready!")

@dp.message(F.text == "/menu")
async def cmd_menu(message: Message):
    menu_text = (
        "📊 *Commands:*\n"
        "/signal <pair> - Get signal for a pair\n"
        "/stats - Show trading stats\n"
        "/snapshot - Get chart snapshot\n"
        "/auto - Toggle auto-trade mode\n"
        "/mode - Switch fixed $1 / % balance trade amount\n"
    )
    await message.answer(menu_text)

@dp.message(F.text == "/stats")
async def cmd_stats(message: Message):
    # Dummy stats example, replace with real call to your TradeLogger
    stats = {
        "total_profit": "$500",
        "total_trades": 100,
        "wins": 60,
        "losses": 40,
        "success_rate": 60,
        "avg_profit": "$5",
        "signals_sent": 120,
        "signal_accuracy": 65,
    }
    text = (
        f"📊 *Quantum Level Stats*\n"
        f"• Total P/L: {stats['total_profit']}\n"
        f"• Trades: {stats['total_trades']} ({stats['wins']}W/{stats['losses']}L)\n"
        f"• Success Rate: {stats['success_rate']}%\n"
        f"• Avg PnL: {stats['avg_profit']}\n"
        f"• Signals Sent: {stats['signals_sent']}\n"
        f"• Signal Accuracy: {stats['signal_accuracy']}%\n"
    )
    await message.answer(text)

# This example webhook handler accepts POST JSON from TradingView or external signal sender
async def tradingview_webhook(request):
    data = await request.json()
    signal = data.get("signal", "No signal")
    pair = data.get("pair", "N/A")
    expiry = data.get("expiry", "N/A")

    text = (
        f"📥 *New Trade Signal*\n\n"
        f"🟢 *Signal:* {signal}\n"
        f"💱 *Pair:* {pair}\n"
        f"⏳ *Expiry:* {expiry}\n\n"
        "Reply with 'yes' to confirm trade, or 'no' to cancel."
    )

    await bot.send_message(TELEGRAM_CHAT_ID, text)

    log_to_html(f"Received signal: {signal} for {pair} with expiry {expiry}")

    # Auto-trigger UI.Vision macro here:
    # Example: subprocess.Popen to run macro URI or executable
    subprocess.Popen(["cmd", "/c", "start", "", "uivision://run?macro=TradeMacro"])

    return web.Response(text="OK")

# === Setup aiohttp app ===
app = web.Application()
app.router.add_post("/callback", tradingview_webhook)
setup_application(app, dp)

# === Run web server ===
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"🚀 Starting bot with webhook at {WEBHOOK_URL}")
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)
