# tvsnapshotbot.py

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
import requests

# === Config ===
API_TOKEN = os.getenv("TELEGRAM_TOKEN") or "8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA"
ADMIN_ID = os.getenv("ADMIN_ID") or "6337160812"
SNAPSHOT_DIR = "snapshots"
TRADES_FILE = "trade_logs.json"
WEBHOOK_URL = "http://localhost:5001/trade"
LEVELS = ["Quantum I", "Quantum II", "Quantum III", "Quantum IV", "Quantum V"]

# === State ===
AUTO_MODE = False
CONFIRMATION_THRESHOLD = 70
FIXED_AMOUNT = 1
USE_PERCENTAGE = False
CONFIRMATION_QUEUE = {}

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

strategy = SignalStrategy()
logger = TradeLogger(TRADES_FILE)

os.makedirs(SNAPSHOT_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO)

# === Command Handlers ===

@dp.message_handler(commands=["start", "menu"])
async def send_menu(message: types.Message):
    text = (
        "📊 <b>Quantum Signal Bot</b>\n\n"
        "<b>/signal EURUSD</b> - Analyze a pair\n"
        "<b>/stats</b> - Show performance\n"
        "<b>/auto</b> - Toggle auto/manual mode\n"
        "<b>/mode</b> - Toggle $1 or 5%\n"
        "<b>/snapshot</b> - Capture chart\n"
        "<b>/analyze</b> - Scan all majors"
    )
    await message.reply(text, parse_mode="HTML")

@dp.message_handler(commands=["stats"])
async def show_stats(message: types.Message):
    stats = logger.get_statistics()
    level = LEVELS[min(4, stats['level'])]
    text = (
        f"📊 <b>Level:</b> {level}\n"
        f"📈 P/L: ${stats['total_profit']}\n"
        f"✅ Wins: {stats['wins']} / ❌ Losses: {stats['losses']}\n"
        f"📉 Success Rate: {stats['success_rate']}%\n"
        f"📊 Trades: {stats['total_trades']}\n"
    )
    await message.reply(text, parse_mode="HTML")

@dp.message_handler(commands=["auto"])
async def toggle_auto(message: types.Message):
    global AUTO_MODE
    AUTO_MODE = not AUTO_MODE
    await message.reply(f"🔁 Auto Mode: {'ON' if AUTO_MODE else 'OFF'}")

@dp.message_handler(commands=["mode"])
async def toggle_mode(message: types.Message):
    global USE_PERCENTAGE
    USE_PERCENTAGE = not USE_PERCENTAGE
    await message.reply(f"💵 Entry Mode: {'5% Balance' if USE_PERCENTAGE else '$1 Fixed'}")

@dp.message_handler(commands=["snapshot"])
async def send_snapshot(message: types.Message):
    file = capture_chart("EURUSD")
    await bot.send_photo(message.chat.id, file, caption="🖼 Snapshot")

@dp.message_handler(commands=["analyze"])
async def analyze_all(message: types.Message):
    pairs = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]
    for pair in pairs:
        signal = strategy.generate_signal(pair)
        file = capture_chart(pair)
        caption = f"{pair}: {signal['summary']} | Confidence: {signal['confidence']}%"
        await bot.send_photo(message.chat.id, file, caption=caption)
        logger.log_trade(pair, signal)
        await handle_trade(message.chat.id, pair, signal)
        await asyncio.sleep(1)

@dp.message_handler(commands=["signal"])
async def signal_pair(message: types.Message):
    try:
        _, pair = message.text.split(" ", 1)
    except:
        return await message.reply("❗ Usage: /signal EURUSD")

    signal = strategy.generate_signal(pair)
    file = capture_chart(pair)
    caption = f"{pair}: {signal['summary']} | Confidence: {signal['confidence']}%"
    await bot.send_photo(message.chat.id, file, caption=caption)
    logger.log_trade(pair, signal)
    await handle_trade(message.chat.id, pair, signal)

@dp.message_handler(lambda message: message.text.lower() in ["yes", "no"])
async def handle_reply(message: types.Message):
    user_id = str(message.from_user.id)
    if user_id in CONFIRMATION_QUEUE:
        pair, action, expiry = CONFIRMATION_QUEUE[user_id]
        if message.text.lower() == "yes":
            await send_trade(pair, action, expiry)
            await message.reply(f"✅ Manual trade executed for {pair}")
        else:
            await message.reply("❌ Trade cancelled.")
        del CONFIRMATION_QUEUE[user_id]

# === Core Trade Handler ===

async def handle_trade(chat_id, pair, signal):
    global AUTO_MODE
    confidence = signal.get("confidence", 0)
    action = signal.get("action", "CALL")
    expiry = signal.get("expiry", "1m")

    if AUTO_MODE and confidence >= CONFIRMATION_THRESHOLD:
        await send_trade(pair, action, expiry)
        await bot.send_message(chat_id, f"🚀 Auto-trade: {pair} {action} ({expiry})")
    else:
        CONFIRMATION_QUEUE[str(chat_id)] = (pair, action, expiry)
        await bot.send_message(chat_id, f"⚠️ {pair} | {action} | {confidence}%\nType 'Yes' to confirm, 'No' to cancel.")

# === Send Trade Webhook ===

async def send_trade(pair, action, expiry):
    amount = "5%" if USE_PERCENTAGE else str(FIXED_AMOUNT)
    payload = {
        "pair": pair,
        "direction": action,
        "expiry": expiry,
        "amount": amount
    }
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=3)
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Webhook failed: {e}")

# === Snapshot Utility ===

def capture_chart(pair):
    img = Image.new("RGB", (640, 360), color=(73, 109, 137))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

# === Run Bot ===

if __name__ == '__main__':
    logging.info("🚀 Bot is running")
    executor.start_polling(dp, skip_updates=True)
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field