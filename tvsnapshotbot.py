#!/usr/bin/env python
"""
TradingView Snapshot Telegram Bot
---------------------------------
• /start /help /pairs /menu /snap /snaplist /snapall
• FX + OTC exact pair names (EUR/USD, EUR/USD-OTC, etc.)
• Inline keyboards for quick selection
• Sends chart screenshots using external Node screenshot backend (BASE_URL:/run)

Env vars (optional):
  TELEGRAM_BOT_TOKEN=...
  SNAPSHOT_BASE_URL=https://yourservice.onrender.com
  DEFAULT_INTERVAL=1
  DEFAULT_THEME=dark
  DEFAULT_EXCHANGE=FX   # used for most FX majors
"""

import os
import re
import asyncio
import logging
from typing import List, Tuple, Dict, Optional

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────────────────
# Async event loop patch (useful in notebooks / REPL)
# ─────────────────────────────────────────────────────────
try:
    import nest_asyncio
    nest_asyncio.apply()
except Exception:
    pass

# ─────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("TVSnapBot")

# ─────────────────────────────────────────────────────────
# Load optional .env
# ─────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ─────────────────────────────────────────────────────────
# Config (env override)
# ─────────────────────────────────────────────────────────
TOKEN             = os.environ.get("TELEGRAM_BOT_TOKEN") or "REPLACE_ME"
BASE_URL          = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000")
DEFAULT_EXCHANGE  = os.environ.get("DEFAULT_EXCHANGE", "FX")
DEFAULT_INTERVAL  = os.environ.get("DEFAULT_INTERVAL", "1")
DEFAULT_THEME     = os.environ.get("DEFAULT_THEME", "dark")

if TOKEN == "REPLACE_ME":
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN env var (or edit script).")

# ─────────────────────────────────────────────────────────
# PAIR DEFINITIONS (exact names shown to user)
# ─────────────────────────────────────────────────────────
FX_PAIRS: List[str] = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD",
    "NZD/USD", "USD/CAD", "EUR/GBP", "EUR/JPY", "GBP/JPY",
    "AUD/JPY", "NZD/JPY", "EUR/AUD", "GBP/AUD", "EUR/CAD",
    "USD/MXN", "USD/TRY", "USD/ZAR", "AUD/CHF", "EUR/CHF",
]

# OTC (exact names as shown in your screenshot + common majors)
OTC_PAIRS: List[str] = [
    "EUR/USD-OTC", "GBP/USD-OTC", "USD/JPY-OTC", "USD/CHF-OTC", "AUD/USD-OTC",
    "NZD/USD-OTC", "USD/CAD-OTC", "EUR/GBP-OTC", "EUR/JPY-OTC", "GBP/JPY-OTC",
    "AUD/CHF-OTC", "EUR/CHF-OTC", "KES/USD-OTC", "MAD/USD-OTC",
    "USD/BDT-OTC", "USD/MXN-OTC", "USD/MYR-OTC", "USD/PKR-OTC",
]

ALL_PAIRS: List[str] = FX_PAIRS + OTC_PAIRS

# ─────────────────────────────────────────────────────────
# Mapping to TradingView exchange & ticker
#   Keys canonicalized (uppercase, no slash, "-OTC" kept)
#   Values: (exchange, ticker)
#   Adjust exchanges as needed (OANDA, FXCM, FX_IDC, etc.)
# ─────────────────────────────────────────────────────────
EXOTIC_EXCHANGE = os.environ.get("EXOTIC_EXCHANGE", "FX_IDC")  # fallback feed for exotics

def _canon_key(pair: str) -> str:
    s = pair.strip().upper()
    s = s.replace(" ", "")
    s = s.replace("/", "")
    if s.endswith("OTC") and not s.endswith("-OTC"):
        s = s[:-3] + "-OTC"
    return s

PAIR_MAP: Dict[str, Tuple[str, str]] = {}

def _add_pair_map(name: str, ex: str, tk: str):
    PAIR_MAP[_canon_key(name)] = (ex, tk)

# FX set
for p in FX_PAIRS:
    base = p.replace("/", "")
    _add_pair_map(p, DEFAULT_EXCHANGE, base)

# OTC set → map to underlying FX or exotic feed
_underlying_map = {
    "EUR/USD-OTC": (DEFAULT_EXCHANGE, "EURUSD"),
    "GBP/USD-OTC": (DEFAULT_EXCHANGE, "GBPUSD"),
    "USD/JPY-OTC": (DEFAULT_EXCHANGE, "USDJPY"),
    "USD/CHF-OTC": (DEFAULT_EXCHANGE, "USDCHF"),
    "AUD/USD-OTC": (DEFAULT_EXCHANGE, "AUDUSD"),
    "NZD/USD-OTC": (DEFAULT_EXCHANGE, "NZDUSD"),
    "USD/CAD-OTC": (DEFAULT_EXCHANGE, "USDCAD"),
    "EUR/GBP-OTC": (DEFAULT_EXCHANGE, "EURGBP"),
    "EUR/JPY-OTC": (DEFAULT_EXCHANGE, "EURJPY"),
    "GBP/JPY-OTC": (DEFAULT_EXCHANGE, "GBPJPY"),
    "AUD/CHF-OTC": (DEFAULT_EXCHANGE, "AUDCHF"),
    "EUR/CHF-OTC": (DEFAULT_EXCHANGE, "EURCHF"),
    "KES/USD-OTC": (EXOTIC_EXCHANGE, "USDKES"),  # inverted feed
    "MAD/USD-OTC": (EXOTIC_EXCHANGE, "USDMAD"),
    "USD/BDT-OTC": (EXOTIC_EXCHANGE, "USDBDT"),
    "USD/MXN-OTC": (EXOTIC_EXCHANGE, "USDMXN"),
    "USD/MYR-OTC": (EXOTIC_EXCHANGE, "USDMYR"),
    "USD/PKR-OTC": (EXOTIC_EXCHANGE, "USDPKR"),
}
for p, (ex, tk) in _underlying_map.items():
    _add_pair_map(p, ex, tk)

# ─────────────────────────────────────────────────────────
# Normalize interval (user-friendly → TradingView param)
# Accepts: 1m, 5m, 15m, 1h, 4h, 1d, numbers, etc.
# Returns string TradingView understands (1, 5, 60, D, etc.)
# ─────────────────────────────────────────────────────────
def norm_interval(tf: str) -> str:
    if not tf:
        return DEFAULT_INTERVAL
    t = tf.strip().lower()
    if t.endswith("m"):
        num = t[:-1]
        return num if num.isdigit() else DEFAULT_INTERVAL
    if t.endswith("h"):
        num = t[:-1]
        return str(int(num) * 60) if num.isdigit() else "60"
    if t in ("d", "1d", "day"):
        return "D"
    if t in ("w", "1w", "week"):
        return "W"
    if t in ("mo", "1mo", "1mth", "month", "m1"):
        return "M"
    if t.isdigit():
        return t
    return DEFAULT_INTERVAL

# ─────────────────────────────────────────────────────────
# Normalize theme
# ─────────────────────────────────────────────────────────
def norm_theme(t: str) -> str:
    if not t:
        return DEFAULT_THEME
    return "light" if t.lower().startswith("l") else "dark"

# ─────────────────────────────────────────────────────────
# Resolve user symbol → (exchange, ticker, is_otc)
# Supports raw pair text, EXACT pair, OTC, EXCH:TICKER
# ─────────────────────────────────────────────────────────
def resolve_symbol(raw: str) -> Tuple[str, str, bool]:
    raw = raw.strip()
    if ":" in raw:
        ex, tk = raw.split(":", 1)
        return ex.upper(), tk.upper(), False

    key = _canon_key(raw)
    if key in PAIR_MAP:
        ex, tk = PAIR_MAP[key]
        return ex, tk, key.endswith("-OTC")

    # fallback: treat sanitized pair as ticker
    # remove / and -OTC; default exchange
    fallback = key.replace("-OTC", "")
    return DEFAULT_EXCHANGE, fallback, key.endswith("-OTC")

# ─────────────────────────────────────────────────────────
# Build Node screenshot URL
# ─────────────────────────────────────────────────────────
def build_snapshot_url(exchange: str, ticker: str, interval: str, theme: str, base: str = "chart") -> str:
    return f"{BASE_URL}/run?base={base}&exchange={exchange}&ticker={ticker}&interval={interval}&theme={theme}"

def start_browser_url() -> str:
    return f"{BASE_URL}/start-browser"

def fetch_snapshot_png(exchange: str, ticker: str, interval: str, theme: str) -> bytes:
    url = build_snapshot_url(exchange, ticker, interval, theme)
    logger.info("GET %s", url)
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content

def try_start_browser():
    try:
        r = requests.get(start_browser_url(), timeout=10)
        logger.info("start-browser: %s %s", r.status_code, r.text[:100])
    except Exception as e:
        logger.warning("start-browser failed: %s", e)

# ─────────────────────────────────────────────────────────
# SNAPSHOT COMMAND PARSERS
# ─────────────────────────────────────────────────────────
def parse_snap_args(args: List[str]) -> Tuple[str, str, str, str]:
    """
    /snap EUR/USD 5 light
    /snap EUR/USD-OTC
    /snap FX:EURUSD D dark
    """
    symbol = args[0] if len(args) >= 1 else "EUR/USD"
    tf     = args[1] if len(args) >= 2 else DEFAULT_INTERVAL
    theme  = args[2] if len(args) >= 3 else DEFAULT_THEME

    exchange, ticker, _ = resolve_symbol(symbol)
    return exchange, ticker, norm_interval(tf), norm_theme(theme)

# ─────────────────────────────────────────────────────────
# Telegram Send Helpers
# ─────────────────────────────────────────────────────────
async def send_snapshot_photo(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    exchange: str,
    ticker: str,
    interval: str,
    theme: str,
    prefix: str = "",
):
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    await asyncio.to_thread(try_start_browser)
    try:
        png = await asyncio.to_thread(fetch_snapshot_png, exchange, ticker, interval, theme)
        caption = f"{prefix}{exchange}:{ticker} • TF {interval} • {theme}"
        await context.bot.send_photo(chat_id=chat_id, photo=png, caption=caption)
    except Exception as e:
        logger.exception("snapshot error")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Failed: {exchange}:{ticker} ({e})")

# ─────────────────────────────────────────────────────────
# Command: /start
# ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name if update.effective_user else "there"
    text = (
        f"Hi {name}! 👋\n\n"
        "I can grab TradingView snapshots for Forex & OTC pairs.\n\n"
        "Examples:\n"
        "/snap EUR/USD 5 light\n"
        "/snap EUR/USD-OTC\n"
        "/snaplist fx   (group)\n"
        "/snaplist otc  (group)\n"
        "/menu (tap to pick a pair)\n\n"
        "See /help or /pairs for more."
    )
    await context.bot.send_message(update.effective_chat.id, text=text, reply_markup=build_main_menu_kb())

# ─────────────────────────────────────────────────────────
# Command: /help
# ─────────────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📘 *Help*\n\n"
        "/snap SYMBOL [interval] [theme]\n"
        "   SYMBOL = EUR/USD | EUR/USD-OTC | FX:EURUSD | etc.\n"
        "   interval = 1,5,15,60,D,W,... | 1m,5m,1h also ok.\n"
        "   theme = dark | light\n\n"
        "/snaplist fx | otc | EXCHANGE T1 T2 ... INT [theme]\n"
        "/snapall  (ALL FX + OTC)\n"
        "/menu     (interactive buttons)\n"
        "/pairs    (see all pairs)\n"
    )
    await context.bot.send_message(update.effective_chat.id, text=text, parse_mode="Markdown")

# ─────────────────────────────────────────────────────────
# Command: /pairs  (text list)
# ─────────────────────────────────────────────────────────
async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["📊 *FX Pairs*"] + [f"• {p}" for p in FX_PAIRS]
    lines += ["", "🕒 *OTC Pairs*"] + [f"• {p}" for p in OTC_PAIRS]
    lines += ["", "Use /menu to tap & snapshot."]
    await context.bot.send_message(
        update.effective_chat.id,
        text="\n".join(lines),
        parse_mode="Markdown",
        reply_markup=build_main_menu_kb(),
    )

# ─────────────────────────────────────────────────────────
# Command: /menu (inline keyboard UI)
# ─────────────────────────────────────────────────────────
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        update.effective_chat.id,
        text="Choose a group or pair:",
        reply_markup=build_main_menu_kb(),
    )

# ─────────────────────────────────────────────────────────
# Command: /snap
# ─────────────────────────────────────────────────────────
async def cmd_snap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await context.bot.send_message(update.effective_chat.id, "Usage: /snap SYMBOL [interval] [theme]")
        return
    ex, tk, tf, th = parse_snap_args(context.args)
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, th)

# ─────────────────────────────────────────────────────────
# Command: /snaplist
# Modes:
#   /snaplist          -> summary list
#   /snaplist fx       -> screenshot FX group
#   /snaplist otc      -> screenshot OTC group
#   /snaplist EXCH T1 T2 ... INT [theme]  -> manual batch
# ─────────────────────────────────────────────────────────
async def cmd_snaplist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    chat_id = update.effective_chat.id

    if len(args) == 0:
        # just list everything (text summary)
        await cmd_pairs(update, context)
        return

    if len(args) == 1:
        key = args[0].lower()
        if key == "fx":
            await batch_group(chat_id, context, group="FX")
            return
        if key == "otc":
            await batch_group(chat_id, context, group="OTC")
            return

    # manual mode
    exchange = args[0].upper()
    # detect theme
    theme = DEFAULT_THEME
    if args[-1].lower() in ("dark", "light"):
        theme = args[-1].lower()
        tf = args[-2]
        tokens = args[1:-2]
    else:
        tf = DEFAULT_INTERVAL
        tokens = args[1:]

    # sanitize tokens, fetch each
    if not tokens:
        await context.bot.send_message(chat_id, "No symbols found. Usage: /snaplist EXCH T1 T2 ... INT [theme]")
        return

    await context.bot.send_message(chat_id, f"Capturing {len(tokens)} from {exchange}…")
    for sym in tokens:
        _, tk, _ = resolve_symbol(sym)
        await send_snapshot_photo(chat_id, context, exchange, tk, norm_interval(tf), theme)
        await asyncio.sleep(1.0)

# ─────────────────────────────────────────────────────────
# Command: /snapall (batch all pairs)
# ─────────────────────────────────────────────────────────
async def cmd_snapall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, "⚡ Capturing ALL FX + OTC pairs (this may take a bit)…")
    # FX first
    for p in FX_PAIRS:
        ex, tk, _ = resolve_symbol(p)
        await send_snapshot_photo(chat_id, context, ex, tk, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[FX] ")
        await asyncio.sleep(1.0)
    # OTC next
    for p in OTC_PAIRS:
        ex, tk, _ = resolve_symbol(p)
        await send_snapshot_photo(chat_id, context, ex, tk, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[OTC] ")
        await asyncio.sleep(1.0)

# ─────────────────────────────────────────────────────────
# Batch helper for FX or OTC group
# ─────────────────────────────────────────────────────────
async def batch_group(chat_id: int, context: ContextTypes.DEFAULT_TYPE, group: str):
    pairs = FX_PAIRS if group == "FX" else OTC_PAIRS
    prefix = "[FX] " if group == "FX" else "[OTC] "
    await context.bot.send_message(chat_id, f"Capturing {group} group ({len(pairs)} pairs)…")
    for p in pairs:
        ex, tk, _ = resolve_symbol(p)
        await send_snapshot_photo(chat_id, context, ex, tk, DEFAULT_INTERVAL, DEFAULT_THEME, prefix=prefix)
        await asyncio.sleep(1.0)

# ─────────────────────────────────────────────────────────
# Inline Keyboard Builders
# ─────────────────────────────────────────────────────────
def build_main_menu_kb() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("📊 FX Pairs",  callback_data="menu:fx"),
            InlineKeyboardButton("🕒 OTC Pairs", callback_data="menu:otc"),
        ],
    ]
    # Quick top 4 FX
    for p in ["EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF"]:
        buttons.append([InlineKeyboardButton(p, callback_data=f"pair:{p}")])
    return InlineKeyboardMarkup(buttons)

def build_fx_kb() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for idx, p in enumerate(FX_PAIRS, start=1):
        row.append(InlineKeyboardButton(p, callback_data=f"pair:{p}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅ Back", callback_data="menu:root")])
    return InlineKeyboardMarkup(buttons)

def build_otc_kb() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for idx, p in enumerate(OTC_PAIRS, start=1):
        row.append(InlineKeyboardButton(p, callback_data=f"pair:{p}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅ Back", callback_data="menu:root")])
    return InlineKeyboardMarkup(buttons)

# ─────────────────────────────────────────────────────────
# Callback Query Handler
# ─────────────────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    chat_id = query.message.chat.id

    # Return to main menu
    if data == "menu:root":
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=query.message.message_id,
            text="Choose a group or pair:",
            reply_markup=build_main_menu_kb(),
        )
        return

    if data == "menu:fx":
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=query.message.message_id,
            text="FX Pairs:",
            reply_markup=build_fx_kb(),
        )
        return

    if data == "menu:otc":
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=query.message.message_id,
            text="OTC Pairs:",
            reply_markup=build_otc_kb(),
        )
        return

    if data.startswith("pair:"):
        pair_name = data.split(":", 1)[1]
        ex, tk, _ = resolve_symbol(pair_name)
        # send snapshot, then re-send main menu
        await send_snapshot_photo(chat_id, context, ex, tk, DEFAULT_INTERVAL, DEFAULT_THEME)
        await asyncio.sleep(0.2)
        await context.bot.send_message(chat_id, "Choose another:", reply_markup=build_main_menu_kb())
        return

    # Unknown callback
    await context.bot.send_message(chat_id, f"⚠ Unknown action: {data}")

# ─────────────────────────────────────────────────────────
# Fallback handlers
# ─────────────────────────────────────────────────────────
async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, update.message.text)

async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, "❌ Unknown command. Try /help.")

# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────
async def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("pairs",    cmd_pairs))
    app.add_handler(CommandHandler("menu",     cmd_menu))
    app.add_handler(CommandHandler("snap",     cmd_snap))
    app.add_handler(CommandHandler("snaplist", cmd_snaplist))
    app.add_handler(CommandHandler("snapall",  cmd_snapall))

    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info("Bot polling…")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
