#!/usr/bin/python

import logging
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import requests
import nest_asyncio
import os
nest_asyncio.apply()

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Optional: Load .env file ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or "YOUR_TELEGRAM_BOT_TOKEN"

# ======================
# PAIR MAPPING
# ======================
FX_OTC_PAIRS = [
    "AUD/CHF OTC", "EUR/CHF OTC", "EUR/USD OTC", "KES/USD OTC", "MAD/USD OTC",
    "USD/BDT OTC", "USD/CAD OTC", "USD/CHF OTC", "USD/MXN OTC",
    "USD/MYR OTC", "USD/PKR OTC"
]

MAJOR_FOREX_PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "USD/CAD",
    "AUD/USD", "NZD/USD", "EUR/GBP", "EUR/JPY", "GBP/JPY"
]

ALL_PAIRS = FX_OTC_PAIRS + MAJOR_FOREX_PAIRS

DEFAULT_EXCHANGE = "FX"
DEFAULT_INTERVAL = "1"
DEFAULT_THEME = "dark"
SNAPSHOT_BASE_URL = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000")

# ======================
# SNAPSHOT LOGIC
# ======================
def build_snapshot_url(exchange, symbol, interval, theme):
    request_url = f"{SNAPSHOT_BASE_URL}/run?base=chart/&exchange={exchange}&ticker={symbol}&interval={interval}&theme={theme}"
    try:
        resp = requests.get(request_url, timeout=10)
        return f"https://www.tradingview.com/chart/{resp.text}"
    except Exception as e:
        return f"❌ Error fetching snapshot for {symbol}: {e}"

def snapshot(args):
    if not args:
        return "❌ Please use format: /snap EUR/USD 1m light"
    symbol = args[0].upper() if "/" in args[0] else args[0]
    interval = args[1] if len(args) > 1 else DEFAULT_INTERVAL
    theme = args[2].lower() if len(args) > 2 else DEFAULT_THEME
    return build_snapshot_url(DEFAULT_EXCHANGE, symbol, interval, theme)

def snapshotlist(args):
    symbols = ALL_PAIRS if not args else args
    urls = [build_snapshot_url(DEFAULT_EXCHANGE, s, DEFAULT_INTERVAL, DEFAULT_THEME) for s in symbols]
    return urls

# ======================
# TELEGRAM COMMANDS
# ======================
async def start(update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.from_user.first_name
    reply = f"Hi {name}! I can generate TradingView chart snapshots for Forex & OTC pairs.\nTry /pairs to see all supported pairs."
    await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)

async def pairs(update, context: ContextTypes.DEFAULT_TYPE):
    reply = "📌 *Supported Pairs:*\n" + "\n".join(f"• {p}" for p in ALL_PAIRS)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)

async def snap(update, context: ContextTypes.DEFAULT_TYPE):
    reply = snapshot(context.args)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)

async def snaplist(update, context: ContextTypes.DEFAULT_TYPE):
    urls = snapshotlist(context.args)
    for u in urls:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=u)

async def help_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    reply = """Commands:
/start – Welcome message
/pairs – List all supported pairs
/snap <PAIR> <INTERVAL> <THEME> – Single snapshot
/snaplist – Snapshots for all pairs"""
    await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)

# ======================
# MAIN APP
# ======================
async def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pairs", pairs))
    app.add_handler(CommandHandler("snap", snap))
    app.add_handler(CommandHandler("snaplist", snaplist))
    logger.info("Bot started...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
