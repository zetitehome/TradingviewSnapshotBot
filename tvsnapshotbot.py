#!/usr/bin/env python
"""
TradingView Snapshot Telegram Bot - Multi-Album Edition
========================================================
• Uses Quotex feed as default for majors.
• Adds /snapmulti and /snapall commands.
• Retries screenshot fetch with delay.
• Logs to logs/tvsnapshotbot.log (rotating logs).
• Rate-limits snapshot calls.
"""

import os
import io
import time
import json
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from typing import List, Tuple, Dict

import requests
from telegram import Update, InputMediaPhoto
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ─────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
log_handler = RotatingFileHandler("logs/tvsnapshotbot.log", maxBytes=5 * 1024 * 1024, backupCount=3)
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[log_handler, logging.StreamHandler()]
)
logger = logging.getLogger("TVSnapBot")

# ─────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────
TOKEN            = os.environ.get("TELEGRAM_BOT_TOKEN") or "REPLACE_ME"
DEFAULT_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID") or "6337160812"
BASE_URL         = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000")
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "QUOTEX")  # Use Quotex feed
DEFAULT_INTERVAL = os.environ.get("DEFAULT_INTERVAL", "1")
DEFAULT_THEME    = os.environ.get("DEFAULT_THEME", "dark")

if TOKEN == "REPLACE_ME":
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN environment variable before running.")

_http = requests.Session()

# Rate limit dictionary
LAST_SNAPSHOT = {}
RATE_LIMIT_SECONDS = 3

def rate_limited(chat_id: int) -> bool:
    now = time.time()
    last = LAST_SNAPSHOT.get(chat_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    LAST_SNAPSHOT[chat_id] = now
    return False

# ─────────────────────────────────────────────────────────
# Pairs
# ─────────────────────────────────────────────────────────
FX_PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD",
    "NZD/USD", "USD/CAD", "EUR/GBP", "EUR/JPY", "GBP/JPY"
]
OTC_PAIRS = [
    "EUR/USD-OTC", "GBP/USD-OTC", "USD/JPY-OTC",
    "USD/CHF-OTC", "AUD/USD-OTC", "NZD/USD-OTC", "USD/CAD-OTC"
]
ALL_PAIRS = FX_PAIRS + OTC_PAIRS

def _canon_key(pair: str) -> str:
    return pair.strip().upper().replace(" ", "").replace("/", "")

PAIR_MAP: Dict[str, Tuple[str, str]] = {}
for p in ALL_PAIRS:
    PAIR_MAP[_canon_key(p)] = (DEFAULT_EXCHANGE, p.replace("/", "").replace("-OTC", ""))

# ─────────────────────────────────────────────────────────
# Snapshot fetching with retry
# ─────────────────────────────────────────────────────────
def fetch_snapshot_png(ex: str, tk: str, interval: str, theme: str) -> bytes:
    attempt = 0
    last_err = None
    while attempt < 3:
        try:
            url = f"{BASE_URL}/run?exchange={ex}&ticker={tk}&interval={interval}&theme={theme}"
            r = _http.get(url, timeout=60)
            if r.status_code == 200:
                return r.content
            last_err = f"HTTP {r.status_code}: {r.text[:100]}"
        except Exception as e:
            last_err = str(e)
        attempt += 1
        logger.warning(f"Fetch attempt {attempt} failed for {tk}: {last_err}")
        time.sleep(5)
    raise RuntimeError(f"Failed after 3 attempts: {last_err}")

async def send_snapshot_photo(chat_id: int, context: ContextTypes.DEFAULT_TYPE, ex: str, tk: str, interval: str, theme: str, prefix: str = ""):
    if rate_limited(chat_id):
        await context.bot.send_message(chat_id, "⏳ Please wait a few seconds before the next snapshot...")
        return
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    try:
        png = await asyncio.to_thread(fetch_snapshot_png, ex, tk, interval, theme)
        caption = f"{prefix}{ex}:{tk} • TF {interval} • {theme}"
        await context.bot.send_photo(chat_id=chat_id, photo=png, caption=caption)
    except Exception as e:
        logger.error(f"send_snapshot_photo error: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Failed: {ex}:{tk} ({e})")

def resolve_symbol(symbol: str) -> Tuple[str, str]:
    s = _canon_key(symbol)
    if s in PAIR_MAP:
        return PAIR_MAP[s]
    return DEFAULT_EXCHANGE, s

# ─────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────
async def cmd_snap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /snap SYMBOL [interval] [theme]")
        return
    symbol = context.args[0]
    tf = context.args[1] if len(context.args) > 1 else DEFAULT_INTERVAL
    theme = context.args[2] if len(context.args) > 2 else DEFAULT_THEME
    ex, tk = resolve_symbol(symbol)
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, theme)

async def cmd_snapmulti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /snapmulti SYMBOL1 SYMBOL2 ... [interval] [theme]")
        return

    *symbols, last_arg = context.args
    theme = DEFAULT_THEME
    tf = DEFAULT_INTERVAL

    if last_arg in ["1", "3", "5", "15"]:
        tf = last_arg
    elif last_arg.lower() in ["dark", "light"]:
        theme = last_arg.lower()
    else:
        symbols.append(last_arg)

    chat_id = update.effective_chat.id
    if rate_limited(chat_id):
        await update.message.reply_text("⏳ Too many requests, wait a few seconds.")
        return

    await update.message.reply_text(f"📸 Generating {len(symbols)} snapshots...")

    media_group = []
    for sym in symbols:
        ex, tk = resolve_symbol(sym)
        try:
            png = await asyncio.to_thread(fetch_snapshot_png, ex, tk, tf, theme)
            media_group.append(InputMediaPhoto(png, caption=f"{ex}:{tk} • TF {tf} • {theme}"))
        except Exception as e:
            media_group.append(InputMediaPhoto(io.BytesIO(b""), caption=f"❌ {sym}: {e}"))

    if media_group:
        await context.bot.send_media_group(chat_id=chat_id, media=media_group)

async def cmd_snapall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"📸 Capturing all {len(ALL_PAIRS)} pairs...")

    batch_size = 5
    for i in range(0, len(ALL_PAIRS), batch_size):
        batch = ALL_PAIRS[i:i + batch_size]
        media_group = []
        for sym in batch:
            ex, tk = resolve_symbol(sym)
            try:
                png = await asyncio.to_thread(fetch_snapshot_png, ex, tk, DEFAULT_INTERVAL, DEFAULT_THEME)
                media_group.append(InputMediaPhoto(png, caption=f"{ex}:{tk}"))
            except Exception as e:
                media_group.append(InputMediaPhoto(io.BytesIO(b""), caption=f"❌ {sym}: {e}"))
        if media_group:
            await context.bot.send_media_group(chat_id=chat_id, media=media_group)
        await asyncio.sleep(3)  # Small delay between batches

# ─────────────────────────────────────────────────────────
# Run Bot
# ─────────────────────────────────────────────────────────
async def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("snap", cmd_snap))
    app.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    app.add_handler(CommandHandler("snapall", cmd_snapall))
    logger.info("Bot started with Quotex feed and multi snapshot support.")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
