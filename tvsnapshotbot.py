# tvsnapshotbot.py - Quantum Signal Bot

import os
import time
import logging
import asyncio
import json
import datetime
import io
from aiogram import Bot, Dispatcher, types, executor
from PIL import Image
from strategy import SignalStrategy
from tradelogger import TradeLogger

API_TOKEN = os.getenv("TELEGRAM_TOKEN") or "YOUR_TELEGRAM_BOT_TOKEN"
ADMIN_ID = os.getenv("ADMIN_ID") or "123456789"
SNAPSHOT_DIR = "snapshots"
TRADES_FILE = "trade_logs.json"
LEVELS = ["Quantum I", "Quantum II", "Quantum III", "Quantum IV", "Quantum V"]

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

strategy = SignalStrategy()
logger = TradeLogger(TRADES_FILE)

os.makedirs(SNAPSHOT_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)

@dp.message_handler(commands=["start"])
async def send_welcome(message: types.Message):
    menu = (
        "👋 Welcome to Quantum Signal Bot\n"
        "\n"
        "📊 Commands:\n"
        "/signal <pair> - Analyze and send next signal\n"
        "/stats - View trade statistics\n"
        "/menu - View this help menu again\n"
        "/snapshot - Capture current chart snapshot\n"
        "/analyze - Run auto-analysis across major pairs\n"
    )
    await message.reply(menu)

@dp.message_handler(commands=["menu"])
async def show_menu(message: types.Message):
    await send_welcome(message)

@dp.message_handler(commands=["stats"])
async def show_stats(message: types.Message):
    stats = logger.get_statistics()
    level = LEVELS[min(4, stats['level'])]

    text = (
        f"📊 Quantum Level: {level}\n"
        f"• Total P/L: {stats['total_profit']}\n"
        f"• Total Trades: {stats['total_trades']} ({stats['wins']}W/{stats['losses']}L)\n"
        f"• Success Rate: {stats['success_rate']}%\n"
        f"• Avg Profit/Trade: {stats['avg_profit']}\n"
        f"• Max Drawdown: {stats['max_drawdown']}\n"
        "\n"
        "📉 Risk Metrics\n"
        f"• Avg Loss/Trade: {stats['avg_loss']}\n"
        f"• Max Single Loss: {stats['max_single_loss']}\n"
        f"• Losing Streak: {stats['max_consecutive_losses']}\n"
        "\n"
        "📡 Signal Analysis\n"
        f"• Signals Sent: {stats['signals_sent']}\n"
        f"• Accuracy: {stats['signal_accuracy']}%\n"
        f"• Best Signal: {stats['best_signal']}\n"
        f"• Worst Signal: {stats['worst_signal']}\n"
        "\n‼️ Updated every 3 days."
    )
    await message.reply(text)

@dp.message_handler(commands=["signal"])
async def signal_pair(message: types.Message):
    try:
        _, pair = message.text.split(" ", 1)
    except ValueError:
        await message.reply("❗ Usage: /signal EURUSD")
        return

    await message.reply(f"📈 Analyzing {pair}...")

    signal = strategy.generate_signal(pair)
    file = capture_chart(pair)

    logger.log_trade(pair, signal)

    await bot.send_photo(message.chat.id, photo=file, caption=f"{signal['summary']}")

@dp.message_handler(commands=["snapshot"])
async def take_snapshot(message: types.Message):
    file = capture_chart("EURUSD")
    await bot.send_photo(message.chat.id, photo=file, caption="🖼 Chart Snapshot")

@dp.message_handler(commands=["analyze"])
async def analyze_all(message: types.Message):
    pairs = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]
    await message.reply("🔎 Scanning major pairs...")

    for pair in pairs:
        signal = strategy.generate_signal(pair)
        file = capture_chart(pair)
        await bot.send_photo(message.chat.id, photo=file, caption=f"{pair}: {signal['summary']}")
        logger.log_trade(pair, signal)
        await asyncio.sleep(1.5)

# === Mock Snapshot Capture ===
def capture_chart(pair):
    img = Image.new("RGB", (640, 360), color=(73, 109, 137))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

if __name__ == '__main__':
    logging.info("Starting Quantum Signal Bot")
    executor.start_polling(dp, skip_updates=True)
