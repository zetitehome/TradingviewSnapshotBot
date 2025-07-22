#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingView → Telegram Snapshot Bot (Enhanced Edition)
======================================================
"""

import os
import json
import logging
from logging.handlers import RotatingFileHandler
from functools import partial
from typing import Dict, Any

from flask import Flask, request, jsonify
from telegram import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Update,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# === CONFIG ===
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BASE_URL = os.getenv("SNAPSHOT_BASE_URL", "http://localhost:10000")
DEFAULT_EXCHANGE = os.getenv("DEFAULT_EXCHANGE", "FX")
DEFAULT_INTERVAL = os.getenv("DEFAULT_INTERVAL", "1")
DEFAULT_THEME = os.getenv("DEFAULT_THEME", "dark")
STATE_FILE = "bot_state.json"

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment.")

# === LOGGING ===
logger = logging.getLogger("TVSnapBot")
logger.setLevel(logging.INFO)
os.makedirs("logs", exist_ok=True)
file_handler = RotatingFileHandler("logs/tvsnapshotbot.log", maxBytes=1_000_000, backupCount=3)
formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
logger.addHandler(logging.StreamHandler())

# === STATE PERSISTENCE ===
def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.warning("Failed to read state file; starting with empty state.")
    return {}

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error("Failed to save state: %s", e)

state: Dict[str, Any] = load_state()

# === FLASK FOR TV SNAPSHOTS ===
app = Flask(__name__)

@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"})

@app.route("/tvhook", methods=["POST"])
def tvhook():
    data = request.json or {}
    logger.info("[Webhook] Received: %s", data)
    return jsonify({"status": "received"})

# === TELEGRAM BOT COMMANDS ===
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to TradingView Snapshot Bot!\nUse /pairs to pick a pair.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Commands:\n/pairs – Pick a trading pair\n/trade – Set trade amount")

# Inline keyboard for /pairs
PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "BTCUSD", "ETHUSD"]

def build_pairs_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(pair, callback_data=f"pair:{pair}") for pair in PAIRS[i:i+2]]
        for i in range(0, len(PAIRS), 2)
    ])

async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Choose a pair:", reply_markup=build_pairs_keyboard())

async def pair_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, pair = query.data.split(":")
    state["selected_pair"] = pair
    save_state()
    await query.edit_message_text(f"Selected {pair}. Now set expiry or trade size.")

# Trade size selection
TRADE_SIZE_OPTIONS = ["$1", "$5", "$10", "5%", "10%", "25%", "50%"]

def build_trade_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(opt, callback_data=f"trade:{opt}") for opt in TRADE_SIZE_OPTIONS[i:i+3]]
        for i in range(0, len(TRADE_SIZE_OPTIONS), 3)
    ])

async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Select trade size:", reply_markup=build_trade_keyboard())

async def trade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, size = query.data.split(":")
    state["trade_size"] = size
    save_state()
    await query.edit_message_text(f"Trade size set to {size}.")

async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Unknown command. Use /help.")

# === APPLICATION BUILDER ===
def build_application() -> Application:
    builder = Application.builder().token(TOKEN)
    app = builder.build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pairs", cmd_pairs))
    app.add_handler(CommandHandler("trade", cmd_trade))
    app.add_handler(CallbackQueryHandler(pair_callback, pattern="^pair:"))
    app.add_handler(CallbackQueryHandler(trade_callback, pattern="^trade:"))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    return app

# === MAIN ===
def main():
    logger.info(
        "Bot starting… BASE_URL=%s | DefaultEX=%s | WebhookPort=%s",
        BASE_URL, DEFAULT_EXCHANGE, 8081
    )
    app_flask = partial(app.run, host="0.0.0.0", port=8081)
    import threading
    threading.Thread(target=app_flask, daemon=True).start()

    application = build_application()
    application.run_polling()

if __name__ == "__main__":
    main()
