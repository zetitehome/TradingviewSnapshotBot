import os
import logging
import subprocess
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.bot import DefaultBotProperties
from aiogram.enums.parse_mode import ParseMode
from aiohttp import web
from aiogram.webhook.aiohttp_server import setup_application

# === CONFIG ===
API_TOKEN = "8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA"
WEBHOOK_URL = "https://6c3090b3d7a5.ngrok-free.app/callback"  # ngrok URL + /callback
WEBAPP_HOST = "0.0.0.0"  # External accessibility
WEBAPP_PORT = 3000
TELEGRAM_CHAT_ID = 6337160812  # Your Telegram chat ID

# Initialize bot with default Markdown parse mode
default_properties = DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
bot = Bot(token=API_TOKEN, default=default_properties)
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

# === Telegram Command Handlers ===

@dp.message(F.text == "/start")
async def cmd_start(message: types.Message):
    await message.answer("👋 Quantum Signal Bot is online and ready!")

@dp.message(F.text == "/menu")
async def cmd_menu(message: types.Message):
    menu_text = (
        "📊 *Commands:*\n"
        "/signal <pair> - Get signal for a pair\n"
        "/stats - Show trading stats\n"
        "/snapshot - Get chart snapshot\n"
        "/auto - Toggle auto-trade mode\n"
        "/mode - Switch fixed $1 / % balance trade amount\n"
        "/result <timestamp> <win|loss> - Update trade result\n"
    )
    await message.answer(menu_text)

@dp.message(F.text == "/stats")
async def cmd_stats(message: types.Message):
    # TODO: Replace dummy stats with real trade log analysis
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

@dp.message(F.text == "/help")
async def cmd_help(message: types.Message):
    help_text = (
        "Available commands:\n"
        "/result <timestamp> <win|loss> - Update trade result\n"
        "/stats - Show trading statistics\n"
        "/help - Show this help message\n"
    )
    await message.answer(help_text)

@dp.message(F.text.startswith("/result"))
async def cmd_result(message: types.Message):
    args = message.text.split()
    if len(args) != 3:
        await message.answer("Usage: /result <timestamp> <win|loss>")
        return
    timestamp, result = args[1], args[2].lower()
    if result not in ("win", "loss"):
        await message.answer("Result must be 'win' or 'loss'")
        return
    # TODO: Update trade result in DB/logs here
    log_to_html(f"Trade result updated: {timestamp} - {result.upper()}")
    await message.answer(f"Trade result recorded: {result.upper()} at {timestamp}")

@dp.message()
async def unknown_command(message: types.Message):
    await message.answer("Unknown command. Type /help for commands.")

# === TradingView Webhook Handler (with stop loss & take profit) ===
async def tradingview_webhook(request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    signal = data.get("signal", "No signal")
    pair = data.get("pair", "N/A")
    expiry = data.get("expiry", "N/A")
    amount = data.get("amount", "N/A")  # Accept amount param
    stop_loss = data.get("stop_loss")   # Optional stop loss param (e.g. % or fixed)
    take_profit = data.get("take_profit")  # Optional take profit param

    text = (
        f"📥 *New Trade Signal*\n\n"
        f"🟢 *Signal:* {signal}\n"
        f"💱 *Pair:* {pair}\n"
        f"💰 *Amount:* {amount}\n"
        f"⏳ *Expiry:* {expiry} min\n"
    )
    if stop_loss:
        text += f"🔻 *Stop Loss:* {stop_loss}\n"
    if take_profit:
        text += f"🔺 *Take Profit:* {take_profit}\n"
    text += "\nReply with 'yes' to confirm trade, or 'no' to cancel."

    await bot.send_message(TELEGRAM_CHAT_ID, text)
    log_to_html(f"Received signal: {signal} for {pair} amount {amount} expiry {expiry} stop_loss {stop_loss} take_profit {take_profit}")

    # Pass parameters to UI.Vision macro via webhook URL or external means (example below)
    uivision_url = (
        "uivision://run?macro=TradeMacro"
        f"&pair={pair}"
        f"&amount={amount}"
        f"&expiry={expiry}"
        f"&signal={signal}"
    )
    if stop_loss:
        uivision_url += f"&stop_loss={stop_loss}"
    if take_profit:
        uivision_url += f"&take_profit={take_profit}"

    try:
        subprocess.Popen(["cmd", "/c", "start", "", uivision_url])
    except Exception as e:
        logging.error(f"Failed to trigger UI.Vision macro: {e}")

    return web.Response(text="OK")

# === Setup aiohttp app and routes ===

app = web.Application()
app.router.add_post("/callback", tradingview_webhook)
setup_application(app, dp)

async def on_shutdown(app):
    with open(HTML_LOG_FILE, "a", encoding="utf-8") as f:
        f.write("</ul>\n</body>\n</html>")

app.on_shutdown.append(on_shutdown)

# === Run the bot ===
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"🚀 Starting bot with webhook at {WEBHOOK_URL}")
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)
    logging.info(f"Bot started at {WEBHOOK_URL}")
    print(f"Bot started at {WEBHOOK_URL}")