#!/usr/bin/env python
"""
TradingView Snapshot Telegram Bot - FX/OTC + Media Groups + Mapping
-------------------------------------------------------------------
Features:
  • /start /help /pairs /menu /snap /snaplist /snapall /snapmulti
  • Exact-pair input: EUR/USD, USD/CHF, EUR/USD-OTC, USD/MXN-OTC, etc.
  • Auto map OTC names to underlying TradingView symbols.
  • Multiple exchange fallbacks to reduce 404 errors (FX, FX_IDC, OANDA, FOREXCOM).
  • Batch snapshot commands send Telegram albums (media groups) in chunks of 10.
  • Uses external Node screenshot backend (/run) you deployed on Render.

Environment (optional overrides):
  TELEGRAM_BOT_TOKEN=...
  SNAPSHOT_BASE_URL=https://tradingviewsnapshotbot.onrender.com
  DEFAULT_INTERVAL=1      # 1-minute by default
  DEFAULT_THEME=dark
  DEFAULT_EXCHANGE=FX     # try FX first (generic); others tried automatically if needed
  EXOTIC_EXCHANGE=FX_IDC
"""

import os
import re
import io
import asyncio
import logging
from typing import List, Tuple, Dict, Optional

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
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
# Optional nest_asyncio (safe no-op if unavailable)
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
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("TVSnapBot")

# ─────────────────────────────────────────────────────────
# Optional .env
# ─────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ─────────────────────────────────────────────────────────
# Config (env overrides)
# ─────────────────────────────────────────────────────────
TOKEN            = os.environ.get("TELEGRAM_BOT_TOKEN") or "REPLACE_ME"
BASE_URL         = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000")
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "FX")
DEFAULT_INTERVAL = os.environ.get("DEFAULT_INTERVAL", "1")
DEFAULT_THEME    = os.environ.get("DEFAULT_THEME", "dark")
EXOTIC_EXCHANGE  = os.environ.get("EXOTIC_EXCHANGE", "FX_IDC")

if TOKEN == "REPLACE_ME":
    raise RuntimeError("Please set TELEGRAM_BOT_TOKEN (env var) or edit script.")

# ------------------------------------------------------------------
# FX & OTC lists shown to user (exact format)
# ------------------------------------------------------------------
FX_PAIRS: List[str] = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD",
    "NZD/USD", "USD/CAD", "EUR/GBP", "EUR/JPY", "GBP/JPY",
    "AUD/JPY", "NZD/JPY", "EUR/AUD", "GBP/AUD", "EUR/CAD",
    "USD/MXN", "USD/TRY", "USD/ZAR", "AUD/CHF", "EUR/CHF",
]

OTC_PAIRS: List[str] = [
    "EUR/USD-OTC", "GBP/USD-OTC", "USD/JPY-OTC", "USD/CHF-OTC", "AUD/USD-OTC",
    "NZD/USD-OTC", "USD/CAD-OTC", "EUR/GBP-OTC", "EUR/JPY-OTC", "GBP/JPY-OTC",
    "AUD/CHF-OTC", "EUR/CHF-OTC", "KES/USD-OTC", "MAD/USD-OTC",
    "USD/BDT-OTC", "USD/MXN-OTC", "USD/MYR-OTC", "USD/PKR-OTC",
]

ALL_PAIRS: List[str] = FX_PAIRS + OTC_PAIRS

# ------------------------------------------------------------------
# Canonicalization helpers
# ------------------------------------------------------------------
def _canon_key(pair: str) -> str:
    """Uppercase, remove spaces & slashes. Ensure '-OTC' form."""
    s = pair.strip().upper().replace(" ", "")
    s = s.replace("/", "")
    if s.endswith("OTC") and not s.endswith("-OTC"):
        s = s[:-3] + "-OTC"
    return s

# ------------------------------------------------------------------
# Build mapping: canonical key -> (exchange, ticker)
# You may override exchange/ticker per pair as needed.
# ------------------------------------------------------------------
PAIR_MAP: Dict[str, Tuple[str, str]] = {}

def _add_pair(name: str, ex: str, tk: str):
    PAIR_MAP[_canon_key(name)] = (ex, tk)

# FX mapped to DEFAULT_EXCHANGE by default
for p in FX_PAIRS:
    tk = p.replace("/", "")
    _add_pair(p, DEFAULT_EXCHANGE, tk)

# OTC mapped to underlying FX or exotic feed
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
    _add_pair(p, ex, tk)

# ------------------------------------------------------------------
# Exchanges to try if the first one fails (fallback list)
# Order matters: we'll try the mapped exchange first, then these:
# ------------------------------------------------------------------
EXCHANGE_FALLBACK_ORDER = [
    DEFAULT_EXCHANGE,    # e.g. FX
    "FX_IDC",
    "OANDA",
    "FOREXCOM",
    "FXCM",
    "IDC",
]

# ------------------------------------------------------------------
# Interval normalization
#   Accepts: 1, 5, 15m, 1h, 4h, D, 1d, W, 1w, etc.
# ------------------------------------------------------------------
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
    if t in ("mo", "1mo", "1mth", "month"):
        return "M"
    if t.isdigit():
        return t
    return DEFAULT_INTERVAL

def norm_theme(val: str) -> str:
    if not val:
        return DEFAULT_THEME
    return "light" if val.lower().startswith("l") else "dark"

# ------------------------------------------------------------------
# Resolve user raw symbol -> (exchange, ticker, is_otc)
# Supports EXCH:TICKER, exact "EUR/USD", "EUR/USD-OTC", plain "EURUSD"
# ------------------------------------------------------------------
def resolve_symbol(raw: str) -> Tuple[str, str, bool]:
    raw = raw.strip()
    if ":" in raw:
        ex, tk = raw.split(":", 1)
        return ex.upper(), tk.upper(), False

    key = _canon_key(raw)
    if key in PAIR_MAP:
        ex, tk = PAIR_MAP[key]
        return ex, tk, key.endswith("-OTC")

    # fallback try raw sanitized
    is_otc = key.endswith("-OTC")
    tk = key.replace("-OTC", "")
    return DEFAULT_EXCHANGE, tk, is_otc

# ------------------------------------------------------------------
# Node screenshot backend calls
# Node expects: /run?base=chart&exchange=FX&ticker=EURUSD&interval=1&theme=dark
# ------------------------------------------------------------------
def build_run_url(exchange: str, ticker: str, interval: str, theme: str, base: str = "chart") -> str:
    return f"{BASE_URL}/run?base={base}&exchange={exchange}&ticker={ticker}&interval={interval}&theme={theme}"

def build_start_browser_url() -> str:
    return f"{BASE_URL}/start-browser"

def try_start_browser():
    try:
        r = requests.get(build_start_browser_url(), timeout=10)
        logger.info("start-browser %s %s", r.status_code, r.text[:80])
    except Exception as e:
        logger.warning("start-browser failed: %s", e)

def fetch_snapshot_png_first_ok(ex: str, tk: str, interval: str, theme: str) -> bytes:
    """
    Try mapped exchange first; if 404 or >=400, try fallbacks.
    """
    tried = []
    candidates = [ex] + [e for e in EXCHANGE_FALLBACK_ORDER if e != ex]
    last_err = None

    for exch in candidates:
        url = build_run_url(exch, tk, interval, theme)
        tried.append(url)
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 200:
                return r.content
            else:
                last_err = f"{r.status_code} {r.text[:200]}"
        except Exception as e:
            last_err = str(e)

    raise RuntimeError(f"All exchanges failed for {tk}. Last error: {last_err}. Tried: {tried}")

# ------------------------------------------------------------------
# Telegram send helpers
# ------------------------------------------------------------------
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
        png = await asyncio.to_thread(fetch_snapshot_png_first_ok, exchange, ticker, interval, theme)
        caption = f"{prefix}{exchange}:{ticker} • TF {interval} • {theme}"
        await context.bot.send_photo(chat_id=chat_id, photo=png, caption=caption)
    except Exception as e:
        logger.exception("snapshot photo error")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Failed: {exchange}:{ticker} ({e})")

def _make_media_item(png_bytes: bytes, caption: Optional[str] = None) -> InputMediaPhoto:
    # Telegram requires file-like or raw input. BytesIO used.
    bio = io.BytesIO(png_bytes)
    bio.name = "chart.png"  # Telegram uses filename hint
    if caption:
        return InputMediaPhoto(media=bio, caption=caption)
    return InputMediaPhoto(media=bio)

async def build_media_items_for_pairs(
    pairs: List[Tuple[str, str, str]],
    interval: str,
    theme: str,
    prefix: str = "",
) -> List[InputMediaPhoto]:
    """
    pairs: list of (exchange, ticker, label_for_user)
    returns InputMediaPhoto objects (caption on first of each album chunk later)
    """
    items: List[InputMediaPhoto] = []
    for ex, tk, label in pairs:
        try_start_browser()  # quick ping, non-blocking
        try:
            png = fetch_snapshot_png_first_ok(ex, tk, interval, theme)
            cap = f"{prefix}{ex}:{tk} • {label} • TF {interval} • {theme}"
            items.append(_make_media_item(png, cap))
        except Exception as e:
            # Fallback error photo? Simpler: send text after album send.
            logger.warning("Pair %s:%s failed: %s", ex, tk, e)
            # Create a 1px blank? Too much. We'll just skip & log; caller can send summary.
    return items

async def send_media_group_chunked(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    media_items: List[InputMediaPhoto],
    chunk_size: int = 10,
):
    # Telegram limit is 10 per media group
    for i in range(0, len(media_items), chunk_size):
        chunk = media_items[i:i+chunk_size]
        if not chunk:
            continue
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(0.5)

# ------------------------------------------------------------------
# UI keyboard builders
# ------------------------------------------------------------------
def build_main_menu_kb() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("📊 FX Pairs",  callback_data="menu:fx"),
            InlineKeyboardButton("🕒 OTC Pairs", callback_data="menu:otc"),
        ],
    ]
    # quick top 4
    for p in ["EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF"]:
        buttons.append([InlineKeyboardButton(p, callback_data=f"pair:{p}")])
    return InlineKeyboardMarkup(buttons)

def build_fx_kb() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for p in FX_PAIRS:
        row.append(InlineKeyboardButton(p, callback_data=f"pair:{p}"))
        if len(row) == 2:
            buttons.append(row); row=[]
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅ Back", callback_data="menu:root")])
    return InlineKeyboardMarkup(buttons)

def build_otc_kb() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for p in OTC_PAIRS:
        row.append(InlineKeyboardButton(p, callback_data=f"pair:{p}"))
        if len(row) == 2:
            buttons.append(row); row=[]
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅ Back", callback_data="menu:root")])
    return InlineKeyboardMarkup(buttons)

# ------------------------------------------------------------------
# Command helpers
# ------------------------------------------------------------------
def parse_snap_args(args: List[str]) -> Tuple[str, str, str, str]:
    """
    /snap EUR/USD 5 light
    """
    symbol = args[0] if args else "EUR/USD"
    tf     = args[1] if len(args) >= 2 and args[1].lower() not in ("light","dark") else DEFAULT_INTERVAL
    theme  = args[-1] if len(args) >= 2 and args[-1].lower() in ("light","dark") else DEFAULT_THEME
    ex, tk, _ = resolve_symbol(symbol)
    return ex, tk, norm_interval(tf), norm_theme(theme)

def parse_multi_args(args: List[str]) -> Tuple[List[str], str, str]:
    """
    /snapmulti EUR/USD GBP/USD USD/JPY 5 light
    Return: [pairs], interval, theme
    """
    if not args:
        return [], DEFAULT_INTERVAL, DEFAULT_THEME
    theme = DEFAULT_THEME
    tf    = DEFAULT_INTERVAL
    if args[-1].lower() in ("light","dark"):
        theme = args[-1].lower()
        rest = args[:-1]
    else:
        rest = args[:]
    # interval guess = second to last token if numeric-ish
    if rest and re.match(r"^\d+[mh]?$|^[dw]$|^[dw]1?$", rest[-1].lower()):
        tf = rest[-1]
        rest = rest[:-1]
    pairs = rest
    if not pairs:
        pairs = ["EUR/USD","GBP/USD"]
    return pairs, norm_interval(tf), norm_theme(theme)

# ------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name if update.effective_user else "there"
    text = (
        f"Hi {name}! 👋\n\n"
        "I grab TradingView snapshots for Forex & OTC pairs.\n\n"
        "Examples:\n"
        "/snap EUR/USD 5 light\n"
        "/snap EUR/USD-OTC\n"
        "/snaplist fx   (FX batch)\n"
        "/snaplist otc  (OTC batch)\n"
        "/snapmulti EUR/USD GBP/USD USD/JPY\n"
        "/snapall (ALL pairs — lots!)\n\n"
        "Use /menu for buttons, /help for full guide."
    )
    await context.bot.send_message(update.effective_chat.id, text=text, reply_markup=build_main_menu_kb())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📘 *Help*\n\n"
        "/snap SYMBOL [interval] [theme]\n"
        "/snapmulti SYMBOL1 SYMBOL2 ... [interval] [theme]\n"
        "/snaplist fx|otc|EXCH SYM1 SYM2 ... INT [theme]\n"
        "/snapall (ALL)\n"
        "/pairs (list)\n"
        "/menu (buttons)\n\n"
        "Intervals: 1,5,15,60, D, W, etc. Also 1m/5m/1h.\n"
        "Themes: dark | light."
    )
    await context.bot.send_message(update.effective_chat.id, text=text, parse_mode="Markdown")

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

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        update.effective_chat.id,
        text="Choose a group or pair:",
        reply_markup=build_main_menu_kb(),
    )

async def cmd_snap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await context.bot.send_message(update.effective_chat.id, "Usage: /snap SYMBOL [interval] [theme]")
        return
    ex, tk, tf, th = parse_snap_args(context.args)
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, th)

async def cmd_snaplist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /snaplist          -> show list (text)
    /snaplist fx       -> FX batch (albums)
    /snaplist otc      -> OTC batch (albums)
    /snaplist EX SYM1 SYM2 ... INT [theme] -> manual single-send per pair (photo)
    """
    args = context.args
    chat_id = update.effective_chat.id

    if len(args) == 0:
        await cmd_pairs(update, context)
        return

    if len(args) == 1:
        key = args[0].lower()
        if key == "fx":
            await batch_media_albums(chat_id, context, FX_PAIRS, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[FX] ")
            return
        if key == "otc":
            await batch_media_albums(chat_id, context, OTC_PAIRS, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[OTC] ")
            return

    # manual mode
    exchange = args[0].upper()
    # theme
    theme = DEFAULT_THEME
    if args[-1].lower() in ("light","dark"):
        theme = args[-1].lower()
        tf = args[-2]
        toks = args[1:-2]
    else:
        tf = DEFAULT_INTERVAL
        toks = args[1:]

    if not toks:
        await context.bot.send_message(chat_id, "No symbols. Usage: /snaplist EX SYM1 SYM2 ... INT [theme]")
        return

    await context.bot.send_message(chat_id, f"Capturing {len(toks)} from {exchange}…")
    for sym in toks:
        # we do not remap exchange in manual mode; assume user knows
        _, tk, _ = resolve_symbol(sym)
        await send_snapshot_photo(chat_id, context, exchange, tk, norm_interval(tf), norm_theme(theme))
        await asyncio.sleep(1.0)

async def cmd_snapmulti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /snapmulti EUR/USD GBP/USD USD/JPY 5 light
    Creates album(s).
    """
    pairs, tf, theme = parse_multi_args(context.args)
    if not pairs:
        await context.bot.send_message(update.effective_chat.id, "Usage: /snapmulti P1 P2 ... [interval] [theme]")
        return
    await batch_media_albums(update.effective_chat.id, context, pairs, tf, theme, prefix="[MULTI] ")

async def cmd_snapall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    All FX + OTC (albums).
    """
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, "⚡ Capturing ALL FX + OTC pairs (this may take a bit)…")
    await batch_media_albums(chat_id, context, FX_PAIRS, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[FX] ")
    await batch_media_albums(chat_id, context, OTC_PAIRS, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[OTC] ")

# ------------------------------------------------------------------
# Batch album sender
# pairs_arg may be list of exact pair strings OR list of (ex,tk,label)
# ------------------------------------------------------------------
async def batch_media_albums(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    pairs_arg,
    interval: str,
    theme: str,
    prefix: str = "",
):
    # Normalize pairs_arg into list[(ex,tk,label)]
    pairs: List[Tuple[str,str,str]] = []
    if pairs_arg and isinstance(pairs_arg[0], tuple):
        # already structured
        for ex, tk, lab in pairs_arg:
            pairs.append((ex, tk, lab))
    else:
        for p in pairs_arg:
            ex, tk, _ = resolve_symbol(p)
            pairs.append((ex, tk, p))

    interval = norm_interval(interval)
    theme = norm_theme(theme)

    await context.bot.send_message(chat_id, f"Fetching {len(pairs)} charts…")
    # Build media items
    media_items = await asyncio.to_thread(build_media_items_sync, pairs, interval, theme, prefix)
    if not media_items:
        await context.bot.send_message(chat_id, "No charts captured.")
        return

    # Send chunked
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=10)

def build_media_items_sync(pairs: List[Tuple[str,str,str]], interval: str, theme: str, prefix: str) -> List[InputMediaPhoto]:
    items: List[InputMediaPhoto] = []
    for ex, tk, lab in pairs:
        try:
            png = fetch_snapshot_png_first_ok(ex, tk, interval, theme)
            cap = f"{prefix}{ex}:{tk} • {lab} • TF {interval} • {theme}"
            items.append(_make_media_item(png, cap))
        except Exception as e:
            logger.warning("Failed building media for %s:%s -> %s", ex, tk, e)
    return items

# ------------------------------------------------------------------
# Callback handler for inline keyboard
# ------------------------------------------------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    chat_id = query.message.chat.id

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
        await send_snapshot_photo(chat_id, context, ex, tk, DEFAULT_INTERVAL, DEFAULT_THEME)
        # show menu again
        await context.bot.send_message(chat_id, "Choose another:", reply_markup=build_main_menu_kb())
        return

    await context.bot.send_message(chat_id, f"⚠ Unknown action: {data}")

# ------------------------------------------------------------------
# Fallbacks
# ------------------------------------------------------------------
async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, update.message.text)

async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, "❌ Unknown command. Try /help.")

# ------------------------------------------------------------------
# Main entry
# ------------------------------------------------------------------
async def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("pairs",     cmd_pairs))
    app.add_handler(CommandHandler("menu",      cmd_menu))
    app.add_handler(CommandHandler("snap",      cmd_snap))
    app.add_handler(CommandHandler("snaplist",  cmd_snaplist))
    app.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    app.add_handler(CommandHandler("snapall",   cmd_snapall))

    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info("Bot polling…")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
