#!/usr/bin/env python
"""
TradingView Snapshot Telegram Bot
---------------------------------
Features:
  • /start /help /pairs /menu /snap /snapmulti /snaplist /snapall /alerttest
  • FX + OTC exact pair mapping (EUR/USD, EUR/USD-OTC, KES/USD-OTC, etc.)
  • Screenshot backend: Node/Puppeteer (/run route) — local or Render
  • Album batching (sendMediaGroup) for multi-pair commands
  • TradingView webhook endpoint (/tv) → Telegram auto-signal + chart

Environment variables (optional overrides):
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=...            # default chat for TradingView alerts
  SNAPSHOT_BASE_URL=http://localhost:10000
  DEFAULT_EXCHANGE=FX
  DEFAULT_INTERVAL=1
  DEFAULT_THEME=dark
  EXOTIC_EXCHANGE=FX_IDC
  TV_WEBHOOK_PORT=8081            # Flask webhook server listen port

If running behind ngrok, expose the Flask port (e.g., 8081) and use
  https://<ngrok>.ngrok-free.app/tv   as the webhook URL in TradingView.
"""

import os
import re
import io
import json
import time
import asyncio
import logging
import threading
from typing import List, Tuple, Dict, Optional

import requests
from flask import Flask, request, jsonify

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
# Optional nest_asyncio (safe even if duplicated)
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
# Env
# ─────────────────────────────────────────────────────────
TOKEN            = os.environ.get("TELEGRAM_BOT_TOKEN") or "REPLACE_ME"
DEFAULT_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID")   # optional; for TV alerts
BASE_URL         = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000")
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "FX")
DEFAULT_INTERVAL = os.environ.get("DEFAULT_INTERVAL", "1")    # minutes unless TV code uses letter
DEFAULT_THEME    = os.environ.get("DEFAULT_THEME", "dark")
EXOTIC_EXCHANGE  = os.environ.get("EXOTIC_EXCHANGE", "FX_IDC")
TV_WEBHOOK_PORT  = int(os.environ.get("TV_WEBHOOK_PORT", "8081"))

if TOKEN == "REPLACE_ME":
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN env var or edit tvsnapshotbot.py.")

# ------------------------------------------------------------------
# Pair definitions (exact names shown to user)
# ------------------------------------------------------------------
FX_PAIRS: List[str] = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD",
    "NZD/USD", "USD/CAD", "EUR/GBP", "EUR/JPY", "GBP/JPY",
    "AUD/JPY", "NZD/JPY", "EUR/AUD", "GBP/AUD", "EUR/CAD",
    "USD/MXN", "USD/TRY", "USD/ZAR", "AUD/CHF", "EUR/CHF",
]

# OTC (include the ones from your screenshots + common majors)
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
    """Uppercase, remove spaces & slashes. Ensure '-OTC' if present."""
    s = pair.strip().upper().replace(" ", "")
    s = s.replace("/", "")
    if s.endswith("OTC") and not s.endswith("-OTC"):
        s = s[:-3] + "-OTC"
    return s

# ------------------------------------------------------------------
# Pair map: canonical → (exchange, ticker)
# ------------------------------------------------------------------
PAIR_MAP: Dict[str, Tuple[str, str]] = {}

def _add_pair(name: str, ex: str, tk: str):
    PAIR_MAP[_canon_key(name)] = (ex, tk)

# FX pairs map to DEFAULT_EXCHANGE
for p in FX_PAIRS:
    tk = p.replace("/", "")
    _add_pair(p, DEFAULT_EXCHANGE, tk)

# OTC underlying mapping
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
    "KES/USD-OTC": (EXOTIC_EXCHANGE, "USDKES"),  # invert
    "MAD/USD-OTC": (EXOTIC_EXCHANGE, "USDMAD"),
    "USD/BDT-OTC": (EXOTIC_EXCHANGE, "USDBDT"),
    "USD/MXN-OTC": (EXOTIC_EXCHANGE, "USDMXN"),
    "USD/MYR-OTC": (EXOTIC_EXCHANGE, "USDMYR"),
    "USD/PKR-OTC": (EXOTIC_EXCHANGE, "USDPKR"),
}
for p, (ex, tk) in _underlying_map.items():
    _add_pair(p, ex, tk)

# ------------------------------------------------------------------
# Exchange fallbacks (order matters)
# ------------------------------------------------------------------
EXCHANGE_FALLBACK_ORDER = [
    DEFAULT_EXCHANGE,  # e.g., FX
    EXOTIC_EXCHANGE,   # e.g., FX_IDC
    "OANDA",
    "FOREXCOM",
    "FXCM",
    "IDC",
]

# ------------------------------------------------------------------
# Interval normalization
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
# NOTE: Node expects separate exchange & ticker params.
# ------------------------------------------------------------------
def build_run_url(exchange: str, ticker: str, interval: str, theme: str, base: str = "chart") -> str:
    return f"{BASE_URL}/run?base={base}&exchange={exchange}&ticker={ticker}&interval={interval}&theme={theme}"

def build_run_url_alt(symbol_full: str, interval: str, theme: str, base: str = "chart") -> str:
    # fallback: send combined (some forks accept symbol=EX:TICKER)
    return f"{BASE_URL}/run?base={base}&ticker={symbol_full}&interval={interval}&theme={theme}"

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
    Try mapped exchange first; if 4xx/5xx, try fallback exchanges,
    then alt format (?ticker=EX:TICKER).
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
            last_err = f"{r.status_code} {r.text[:200]}"
        except Exception as e:
            last_err = str(e)

    # alt style
    alt_symbol = f"{ex}:{tk}"
    url2 = build_run_url_alt(alt_symbol, interval, theme)
    tried.append(url2)
    try:
        r2 = requests.get(url2, timeout=60)
        if r2.status_code == 200:
            return r2.content
        last_err = f"(alt) {r2.status_code} {r2.text[:200]}"
    except Exception as e:
        last_err = f"(alt) {e}"

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
    bio = io.BytesIO(png_bytes)
    bio.name = "chart.png"
    return InputMediaPhoto(media=bio, caption=caption)

def build_media_items_sync(
    pairs: List[Tuple[str, str, str]],
    interval: str,
    theme: str,
    prefix: str,
) -> List[InputMediaPhoto]:
    items: List[InputMediaPhoto] = []
    for idx, (ex, tk, lab) in enumerate(pairs):
        try:
            png = fetch_snapshot_png_first_ok(ex, tk, interval, theme)
            # caption only first in album chunk later; attach now, we may strip later
            cap = f"{prefix}{ex}:{tk} • {lab} • TF {interval} • {theme}"
            items.append(_make_media_item(png, cap))
        except Exception as e:
            logger.warning("Failed building media for %s:%s -> %s", ex, tk, e)
    return items

async def send_media_group_chunked(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    media_items: List[InputMediaPhoto],
    chunk_size: int = 10,
):
    # Telegram allows caption only on first item of chunk
    for i in range(0, len(media_items), chunk_size):
        chunk = media_items[i:i+chunk_size]
        if not chunk:
            continue
        # ensure only first has caption; others None
        if len(chunk) > 1:
            first_cap = chunk[0].caption
            for m in chunk[1:]:
                m.caption = None
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(0.5)

# ------------------------------------------------------------------
# Argument parsing helpers
# ------------------------------------------------------------------
def parse_snap_args(args: List[str]) -> Tuple[str, str, str, str]:
    symbol = args[0] if args else "EUR/USD"
    tf     = args[1] if len(args) >= 2 and args[1].lower() not in ("light","dark") else DEFAULT_INTERVAL
    theme  = args[-1] if len(args) >= 2 and args[-1].lower() in ("light","dark") else DEFAULT_THEME
    ex, tk, _ = resolve_symbol(symbol)
    return ex, tk, norm_interval(tf), norm_theme(theme)

def parse_multi_args(args: List[str]) -> Tuple[List[str], str, str]:
    """
    /snapmulti EUR/USD GBP/USD USD/JPY 5 light
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
    # interval guess
    if rest and re.match(r"^\d+[mh]?$|^[dw]$|^(d|w|m)$|^([0-9]+[hd])$", rest[-1].lower()):
        tf = rest[-1]
        rest = rest[:-1]
    pairs = rest
    if not pairs:
        pairs = ["EUR/USD","GBP/USD"]
    return pairs, norm_interval(tf), norm_theme(theme)

# ------------------------------------------------------------------
# Inline keyboard builders
# ------------------------------------------------------------------
def build_main_menu_kb() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("📊 FX Pairs",  callback_data="menu:fx"),
            InlineKeyboardButton("🕒 OTC Pairs", callback_data="menu:otc"),
        ],
    ]
    for p in ["EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF"]:
        buttons.append([InlineKeyboardButton(p, callback_data=f"pair:{p}")])
    return InlineKeyboardMarkup(buttons)

def build_fx_kb() -> InlineKeyboardMarkup:
    buttons, row = [], []
    for p in FX_PAIRS:
        row.append(InlineKeyboardButton(p, callback_data=f"pair:{p}"))
        if len(row) == 2:
            buttons.append(row); row=[]
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅ Back", callback_data="menu:root")])
    return InlineKeyboardMarkup(buttons)

def build_otc_kb() -> InlineKeyboardMarkup:
    buttons, row = [], []
    for p in OTC_PAIRS:
        row.append(InlineKeyboardButton(p, callback_data=f"pair:{p}"))
        if len(row) == 2:
            buttons.append(row); row=[]
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅ Back", callback_data="menu:root")])
    return InlineKeyboardMarkup(buttons)

# ------------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name if update.effective_user else "there"
    text = (
        f"Hi {name}! 👋\n\n"
        "I grab TradingView snapshots for Forex & OTC.\n\n"
        "Try:\n"
        "/snap EUR/USD 5 light\n"
        "/snaplist fx\n"
        "/snapmulti EUR/USD GBP/USD 15 dark\n"
        "/snapall\n"
        "/alerttest EUR/USD CALL 5m\n\n"
        "Use /menu for buttons."
    )
    await context.bot.send_message(update.effective_chat.id, text=text, reply_markup=build_main_menu_kb())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📘 *Help*\n\n"
        "/snap SYMBOL [interval] [theme]\n"
        "/snapmulti S1 S2 ... [interval] [theme]\n"
        "/snaplist fx|otc|EX SYM1 SYM2 ... INT [theme]\n"
        "/snapall (ALL)\n"
        "/alerttest SYMBOL DIR [expiry]\n"
        "/menu (buttons)  |  /pairs (list)\n\n"
        "Intervals: 1,5,15,60,D,W,...   Themes: dark|light."
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
    /snaplist fx       -> FX batch album
    /snaplist otc      -> OTC batch album
    /snaplist EX SYM1 SYM2 ... INT [theme] -> manual single sends
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
        _, tk, _ = resolve_symbol(sym)
        await send_snapshot_photo(chat_id, context, exchange, tk, norm_interval(tf), norm_theme(theme))
        await asyncio.sleep(1.0)

async def cmd_snapmulti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs, tf, theme = parse_multi_args(context.args)
    if not pairs:
        await context.bot.send_message(update.effective_chat.id, "Usage: /snapmulti P1 P2 ... [interval] [theme]")
        return
    await batch_media_albums(update.effective_chat.id, context, pairs, tf, theme, prefix="[MULTI] ")

async def cmd_snapall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, "⚡ Capturing ALL FX + OTC pairs…")
    await batch_media_albums(chat_id, context, FX_PAIRS, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[FX] ")
    await batch_media_albums(chat_id, context, OTC_PAIRS, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[OTC] ")

# ------------------------------------------------------------------
# /alerttest SYMBOL DIR [expiry]
# Simulate a TradingView webhook payload
# ------------------------------------------------------------------
async def cmd_alerttest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await context.bot.send_message(update.effective_chat.id, "Usage: /alerttest SYMBOL DIR [expiry]")
        return
    symbol = args[0]
    direction = args[1] if len(args) >= 2 else "CALL"
    expiry = args[2] if len(args) >= 3 else "5m"

    # simulate webhook handling
    payload = {
        "symbol": symbol,
        "direction": direction,
        "expiry": expiry,
        "source": "alerttest",
        "time": int(time.time()*1000),
    }
    await handle_tv_alert_to_telegram(update.effective_chat.id, context, payload)

# ------------------------------------------------------------------
# Batch album sender
# ------------------------------------------------------------------
async def batch_media_albums(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    pairs_arg,
    interval: str,
    theme: str,
    prefix: str = "",
):
    # Normalize to list[(ex,tk,label)]
    pairs: List[Tuple[str,str,str]] = []
    if pairs_arg and isinstance(pairs_arg[0], tuple):
        pairs = [(ex, tk, lab) for ex, tk, lab in pairs_arg]
    else:
        for p in pairs_arg:
            ex, tk, _ = resolve_symbol(p)
            pairs.append((ex, tk, p))
    interval = norm_interval(interval)
    theme = norm_theme(theme)

    await context.bot.send_message(chat_id, f"Fetching {len(pairs)} charts…")
    media_items = await asyncio.to_thread(build_media_items_sync, pairs, interval, theme, prefix)
    if not media_items:
        await context.bot.send_message(chat_id, "No charts captured.")
        return
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=10)

# ------------------------------------------------------------------
# Callback handler (inline menu)
# ------------------------------------------------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    chat_id = q.message.chat.id

    if data == "menu:root":
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=q.message.message_id,
            text="Choose a group or pair:",
            reply_markup=build_main_menu_kb(),
        )
        return

    if data == "menu:fx":
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=q.message.message_id,
            text="FX Pairs:",
            reply_markup=build_fx_kb(),
        )
        return

    if data == "menu:otc":
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=q.message.message_id,
            text="OTC Pairs:",
            reply_markup=build_otc_kb(),
        )
        return

    if data.startswith("pair:"):
        pair_name = data.split(":",1)[1]
        ex, tk, _ = resolve_symbol(pair_name)
        await send_snapshot_photo(chat_id, context, ex, tk, DEFAULT_INTERVAL, DEFAULT_THEME)
        await asyncio.sleep(0.2)
        await context.bot.send_message(chat_id, "Choose another:", reply_markup=build_main_menu_kb())
        return

    await context.bot.send_message(chat_id, f"⚠ Unknown action: {data}")

# ------------------------------------------------------------------
# Fallback handlers
# ------------------------------------------------------------------
async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, update.message.text)

async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, "❌ Unknown command. Try /help.")

# ------------------------------------------------------------------
# TradingView webhook → Telegram
#   Called from Flask thread wrapper (sync) OR /alerttest (async)
# ------------------------------------------------------------------
def tg_send_message(chat_id: str, text: str, parse_mode: Optional[str] = None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    try:
        requests.post(url, data=data, timeout=30)
    except Exception as e:
        logger.error("tg_send_message error: %s", e)

def tg_send_photo_bytes(chat_id: str, png_bytes: bytes, caption: str = ""):
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", png_bytes, "image/png")}
    data  = {"chat_id": chat_id, "caption": caption}
    try:
        requests.post(url, data=data, files=files, timeout=60)
    except Exception as e:
        logger.error("tg_send_photo_bytes error: %s", e)

async def handle_tv_alert_to_telegram(chat_id: int, context: ContextTypes.DEFAULT_TYPE, payload: dict):
    """
    Async helper: send formatted signal + chart (Node screenshot).
    """
    raw_symbol = payload.get("symbol", "EURUSD")
    direction  = payload.get("direction", "CALL")
    expiry     = payload.get("expiry", "")
    tf         = payload.get("timeframe") or DEFAULT_INTERVAL
    theme      = payload.get("theme") or DEFAULT_THEME

    ex, tk, _ = resolve_symbol(raw_symbol)
    tf  = norm_interval(tf)
    th  = norm_theme(theme)

    msg = (
        f"🔔 *TradingView Alert*\n"
        f"Symbol: {raw_symbol}\n"
        f"Direction: {direction}\n"
        f"Expiry: {expiry}\n"
        f"TF: {tf} • Theme: {th}"
    )
    await context.bot.send_message(chat_id, msg, parse_mode="Markdown")
    await send_snapshot_photo(chat_id, context, ex, tk, tf, th)

# ------------------------------------------------------------------
# Flask app to receive TradingView alerts
# ------------------------------------------------------------------
flask_app = Flask(__name__)

@flask_app.route("/tv", methods=["POST"])
def tv_webhook():
    """
    TradingView webhook receiver.
    TradingView alert must send JSON.
    Required: symbol
    Optional: direction, expiry, timeframe, theme, chat_id
    """
    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:
        logger.error("TV webhook JSON error: %s", e)
        return jsonify({"ok": False, "error": "invalid_json"}), 400

    logger.info("TV webhook data: %s", data)

    # Which Telegram chat to send to?
    chat_id = str(data.get("chat_id") or DEFAULT_CHAT_ID or "")
    if not chat_id:
        logger.warning("No DEFAULT_CHAT_ID and none in payload; dropping.")
        return jsonify({"ok": False, "error": "no_chat_id"}), 400

    # Parse payload
    raw_symbol = data.get("symbol", "EURUSD")
    direction  = data.get("direction", "CALL")
    expiry     = data.get("expiry", "")
    tf         = data.get("timeframe") or DEFAULT_INTERVAL
    th         = data.get("theme") or DEFAULT_THEME

    # Resolve & screenshot (sync path)
    ex, tk, _ = resolve_symbol(raw_symbol)
    tf = norm_interval(tf)
    th = norm_theme(th)

    # message first
    msg = (
        f"🔔 *TradingView Alert*\n"
        f"Symbol: {raw_symbol}\n"
        f"Direction: {direction}\n"
        f"Expiry: {expiry}\n"
        f"TF: {tf} • Theme: {th}"
    )
    tg_send_message(chat_id, msg, parse_mode="Markdown")

    try_start_browser()
    try:
        png = fetch_snapshot_png_first_ok(ex, tk, tf, th)
        caption = f"{ex}:{tk} • TF {tf} • {th}"
        tg_send_photo_bytes(chat_id, png, caption)
    except Exception as e:
        logger.error("TV screenshot error: %s", e)
        tg_send_message(chat_id, f"⚠ Chart snapshot failed for {raw_symbol}: {e}")

    return jsonify({"ok": True}), 200

# ------------------------------------------------------------------
# Start Flask in background thread
# ------------------------------------------------------------------
def start_flask_bg():
    # threaded=True so multiple webhook calls don’t block bot
    flask_app.run(host="0.0.0.0", port=TV_WEBHOOK_PORT, debug=False, use_reloader=False, threaded=True)

# ------------------------------------------------------------------
# Main entry
# ------------------------------------------------------------------
async def main():
    # Start webhook server in background
    threading.Thread(target=start_flask_bg, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("pairs",     cmd_pairs))
    app.add_handler(CommandHandler("menu",      cmd_menu))
    app.add_handler(CommandHandler("snap",      cmd_snap))
    app.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    app.add_handler(CommandHandler("snaplist",  cmd_snaplist))
    app.add_handler(CommandHandler("snapall",   cmd_snapall))
    app.add_handler(CommandHandler("alerttest", cmd_alerttest))

    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info(f"Bot polling… TV webhook server on port {TV_WEBHOOK_PORT}")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
