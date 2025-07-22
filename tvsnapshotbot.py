#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
=======================================================================
TradingView → Telegram Snapshot Bot  (Inline UI / PocketOption Edition)
=======================================================================

FEATURE SUMMARY
---------------
• Modern python-telegram-bot v20+ async code.
• Inline keyboard UI:
    /pairs  → choose FX vs OTC → select pair → direction (CALL/PUT) →
              expiry (1m/3m/5m/15m) → snapshot + trade signal message.
• /snap, /trade, /snapmulti, /snapall, /next commands.
• Extensive FX + OTC pair mapping; resolution to TradingView symbols.
• Multi-exchange fallback chain; automatically tries alternates when the
  primary fails.
• Accepts PNG *even if* upstream returns HTTP 4xx/5xx (some custom
  screenshot servers incorrectly send error status codes with a good
  image payload; we’ll still use the image).
• Safe logging (no binary puke to console; truncated & sanitized).
• Rotating log file at logs/tvsnapshotbot.log (5MB * 3 backups).
• Rate limiting (per chat & global throttle).
• Flask webhook (/tv, alias /webhook) accepts TradingView alert JSON and
  pushes formatted signal + chart to Telegram.
• Direction synonyms (CALL/PUT/BUY/SELL/UP/DOWN).
• Binary expiry presets: 1m / 3m / 5m / 15m.
• Environment-driven configuration (see below).

-----------------------------------------------------------------------
ENVIRONMENT VARIABLES
-----------------------------------------------------------------------
TELEGRAM_BOT_TOKEN   : REQUIRED. Bot token from BotFather.
TELEGRAM_CHAT_ID     : Optional fallback chat ID (string).
SNAPSHOT_BASE_URL    : URL to your Node/Puppeteer screenshot service.
                       e.g., http://localhost:10000   or
                              https://your-render-host.onrender.com
DEFAULT_EXCHANGE     : Base exchange tag used when mapping is unknown.
                       (Recommended: "CURRENCY" or "FX"; historically "QUOTEX").
DEFAULT_INTERVAL     : Default timeframe for snapshots (e.g., "1").
DEFAULT_THEME        : "dark" or "light".
TV_WEBHOOK_PORT      : Local port for Flask TradingView webhook server.
WEBHOOK_SECRET       : Optional shared secret token (header or body).

-----------------------------------------------------------------------
QUICK START
-----------------------------------------------------------------------
1. Run the Node snapshot server (port 10000 default).
2. Export env vars (above).
3. Run this Python bot.
4. In Telegram: /start  then explore.

-----------------------------------------------------------------------
COPYRIGHT / LICENSE
-----------------------------------------------------------------------
This file authored for you by ChatGPT (OpenAI) based on your requests.
You may use / modify / distribute freely; no warranty.

=======================================================================
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import string
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Sequence, Tuple

import requests
from flask import Flask, jsonify, request

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------
# LOGGING SETUP
# ---------------------------------------------------------------------
os.makedirs("logs", exist_ok=True)
LOG_FILE = os.path.join("logs", "tvsnapshotbot.log")


def _safe_trunc(obj, maxlen: int = 200) -> str:
    """
    Convert arbitrary object (possibly bytes) to safe, printable, truncated str.
    Prevents binary PNG data or control chars from exploding logs.
    """
    if obj is None:
        return "None"
    if isinstance(obj, bytes):
        # hex-ish sample
        txt = obj[: maxlen // 2].hex(" ")
        return f"<{len(obj)} bytes: {txt}...>"
    s = str(obj)
    # remove non-printable
    printable = set(string.printable)
    s = "".join(ch if ch in printable else "?" for ch in s)
    if len(s) > maxlen:
        s = s[:maxlen] + "...(trunc)"
    return s


log_handlers = [
    logging.StreamHandler(),
    RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3),
]
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=log_handlers,
)
logger = logging.getLogger("TVSnapBot")
logger.propagate = False  # avoid double logs

# ---------------------------------------------------------------------
# CONFIG FROM ENV
# ---------------------------------------------------------------------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DEFAULT_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BASE_URL = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000")
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "CURRENCY")
DEFAULT_INTERVAL = os.environ.get("DEFAULT_INTERVAL", "1")  # minutes default
DEFAULT_THEME = os.environ.get("DEFAULT_THEME", "dark")
TV_WEBHOOK_PORT = int(os.environ.get("TV_WEBHOOK_PORT", "8081"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # optional

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment.")

# ---------------------------------------------------------------------
# GLOBAL HTTP SESSION
# ---------------------------------------------------------------------
_http = requests.Session()

# ---------------------------------------------------------------------
# RATE LIMITING
# ---------------------------------------------------------------------
LAST_SNAPSHOT_PER_CHAT: Dict[int, float] = {}
RATE_LIMIT_SECONDS = 3

GLOBAL_LAST_SNAPSHOT = 0.0
GLOBAL_MIN_GAP = 0.75  # seconds between *any* screenshot calls


def rate_limited(chat_id: int) -> bool:
    """
    Returns True if per-chat rate limit hit.
    """
    now = time.time()
    last = LAST_SNAPSHOT_PER_CHAT.get(chat_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    LAST_SNAPSHOT_PER_CHAT[chat_id] = now
    return False


def global_throttle_wait():
    """
    Sleep just enough so we don't hammer the screenshot backend.
    """
    global GLOBAL_LAST_SNAPSHOT
    now = time.time()
    gap = now - GLOBAL_LAST_SNAPSHOT
    if gap < GLOBAL_MIN_GAP:
        time.sleep(GLOBAL_MIN_GAP - gap)
    GLOBAL_LAST_SNAPSHOT = time.time()


# ---------------------------------------------------------------------
# PAIR DEFINITIONS
# ---------------------------------------------------------------------
# Display names EXACTLY as user sees / chooses:
FX_PAIRS: List[str] = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "USD/CHF",
    "AUD/USD",
    "NZD/USD",
    "USD/CAD",
    "EUR/GBP",
    "EUR/JPY",
    "GBP/JPY",
    "AUD/JPY",
    "NZD/JPY",
    "EUR/AUD",
    "GBP/AUD",
    "EUR/CAD",
    "USD/MXN",
    "USD/TRY",
    "USD/ZAR",
    "AUD/CHF",
    "EUR/CHF",
]

# OTC list (Pocket Option style names you sent)
OTC_PAIRS: List[str] = [
    "EUR/USD-OTC",
    "GBP/USD-OTC",
    "USD/JPY-OTC",
    "USD/CHF-OTC",
    "AUD/USD-OTC",
    "NZD/USD-OTC",
    "USD/CAD-OTC",
    "EUR/GBP-OTC",
    "EUR/JPY-OTC",
    "GBP/JPY-OTC",
    "AUD/CHF-OTC",
    "EUR/CHF-OTC",
    "KES/USD-OTC",
    "MAD/USD-OTC",
    "USD/BDT-OTC",
    "USD/MXN-OTC",
    "USD/MYR-OTC",
    "USD/PKR-OTC",
]

ALL_PAIRS: List[str] = FX_PAIRS + OTC_PAIRS


def _canon_key(pair: str) -> str:
    return pair.strip().upper().replace(" ", "").replace("/", "")


# Exchange fallback chain used when primary fails.
# Order chosen by typical FX liquidity: direct feed -> alt feeds -> generic.
EXCHANGE_FALLBACKS_DEFAULT: List[str] = [
    "CURRENCY",  # your default
    "FX",
    "FX_IDC",
    "OANDA",
    "FOREXCOM",
    "IDC",
    "QUOTEX",
]


# Map display name → (primary_exchange, ticker, alt_exchanges[])
# alt_exchanges will be appended to global fallback chain, so it can be empty.
PairMap = Dict[str, Tuple[str, str, List[str]]]
PAIR_MAP: PairMap = {}

# ----- FX majors: use DEFAULT_EXCHANGE as primary (commonly "CURRENCY") -----
for p in FX_PAIRS:
    canon = _canon_key(p)
    ticker = p.replace("/", "")
    PAIR_MAP[canon] = (DEFAULT_EXCHANGE, ticker, [])

# ----- OTC underlying map: we’ll try QUOTEX first, then fallback to FX chain -----
_underlying_otc = {
    "EUR/USD-OTC": "EURUSD",
    "GBP/USD-OTC": "GBPUSD",
    "USD/JPY-OTC": "USDJPY",
    "USD/CHF-OTC": "USDCHF",
    "AUD/USD-OTC": "AUDUSD",
    "NZD/USD-OTC": "NZDUSD",
    "USD/CAD-OTC": "USDCAD",
    "EUR/GBP-OTC": "EURGBP",
    "EUR/JPY-OTC": "EURJPY",
    "GBP/JPY-OTC": "GBPJPY",
    "AUD/CHF-OTC": "AUDCHF",
    "EUR/CHF-OTC": "EURCHF",
    "KES/USD-OTC": "USDKES",
    "MAD/USD-OTC": "USDMAD",
    "USD/BDT-OTC": "USDBDT",
    "USD/MXN-OTC": "USDMXN",
    "USD/MYR-OTC": "USDMYR",
    "USD/PKR-OTC": "USDPKR",
}
for p, tk in _underlying_otc.items():
    canon = _canon_key(p)
    # Use QUOTEX as the first attempt to align w/ OTC style (change if you prefer)
    PAIR_MAP[canon] = ("QUOTEX", tk, [DEFAULT_EXCHANGE])


# ---------------------------------------------------------------------
# INTERVAL / THEME NORMALIZATION
# ---------------------------------------------------------------------
def norm_interval(tf: str) -> str:
    """
    Convert user frames to TradingView style:
       "1" => "1"
       "5m" => "5"
       "1h" => "60"
       "d"  => "D"
       "w"  => "W"
       "m"  => "M"
    """
    if not tf:
        return DEFAULT_INTERVAL
    t = tf.strip().lower()
    if t.endswith("m") and t[:-1].isdigit():
        return t[:-1]
    if t.endswith("h") and t[:-1].isdigit():
        return str(int(t[:-1]) * 60)
    if t in ("d", "1d", "day"):
        return "D"
    if t in ("w", "1w", "week"):
        return "W"
    if t in ("mo", "m", "1m", "month"):
        return "M"
    if t.isdigit():
        return t
    return DEFAULT_INTERVAL


def norm_theme(val: str) -> str:
    return "light" if (val and val.lower().startswith("l")) else "dark"


# ---------------------------------------------------------------------
# DIRECTION PARSING
# ---------------------------------------------------------------------
_CALL_WORDS = {"CALL", "BUY", "UP", "LONG"}
_PUT_WORDS = {"PUT", "SELL", "DOWN", "SHORT"}


def parse_direction(word: Optional[str]) -> Optional[str]:
    if not word:
        return None
    w = word.strip().upper()
    if w in _CALL_WORDS:
        return "CALL"
    if w in _PUT_WORDS:
        return "PUT"
    return None


# ---------------------------------------------------------------------
# SYMBOL RESOLUTION
# ---------------------------------------------------------------------
def resolve_symbol(raw: str) -> Tuple[str, str, bool, List[str]]:
    """
    Return (exchange, ticker, is_otc, alt_exchanges[]) for a raw user string.
    Accepts: "EUR/USD", "EUR/USD-OTC", "FX:EURUSD", "eurusd" etc.
    """
    if not raw:
        return DEFAULT_EXCHANGE, "EURUSD", False, []

    s = raw.strip().upper()
    is_otc = "-OTC" in s

    # explicit "EX:TK"
    if ":" in s:
        ex, tk = s.split(":", 1)
        return ex, tk, is_otc, []

    key = _canon_key(s)
    if key in PAIR_MAP:
        ex, tk, alt = PAIR_MAP[key]
        return ex, tk, is_otc, alt

    # fallback guess
    tk = re.sub(r"[^A-Z0-9]", "", s)
    return DEFAULT_EXCHANGE, tk, is_otc, []


# ---------------------------------------------------------------------
# SNAPSHOT BACKEND HELPERS
# ---------------------------------------------------------------------
def _attempt_snapshot(
    base_url: str,
    ex: str,
    tk: str,
    interval: str,
    theme: str,
    base: str = "chart",
    timeout: int = 75,
) -> Tuple[bool, Optional[bytes], str]:
    """
    Low-level snapshot attempt, returns (ok?, bytes|None, error_str).
    Accepts PNG even if HTTP code != 200 (some servers mis-set status).
    """
    try:
        global_throttle_wait()
        url = (
            f"{base_url}/run?base={base}&exchange={ex}&ticker={tk}"
            f"&interval={interval}&theme={theme}"
        )
        r = _http.get(url, timeout=timeout)
    except Exception as e:  # network error
        return False, None, _safe_trunc(e)

    ct = r.headers.get("Content-Type", "")

    # Accept PNG regardless of status
    if ct.startswith("image") or ct.startswith("application/octet-stream"):
        return True, r.content, ""  # good enough

    # Not an image; treat as error
    err = f"HTTP {r.status_code}: {_safe_trunc(r.text)}"
    return False, None, err


def fetch_snapshot_png_any(
    primary_ex: str,
    tk: str,
    interval: str,
    theme: str,
    base: str = "chart",
    extra_exchanges: Optional[Sequence[str]] = None,
    base_url: str = BASE_URL,
) -> Tuple[bytes, str]:
    """
    Try primary exchange, any extras passed, then global fallback chain.
    Return (png_bytes, exchange_used) or raise RuntimeError.
    """
    tried: List[str] = []
    last_err = "no attempts"

    # Build attempt order
    chain: List[str] = [primary_ex.upper()]
    if extra_exchanges:
        for e in extra_exchanges:
            e = e.upper()
            if e not in chain:
                chain.append(e)
    for e in EXCHANGE_FALLBACKS_DEFAULT:
        e = e.upper()
        if e not in chain:
            chain.append(e)

    for ex in chain:
        tried.append(ex)
        ok, png, err = _attempt_snapshot(base_url, ex, tk, interval, theme, base=base)
        if ok and png:
            logger.info("Snapshot success %s:%s via %s (%d bytes)", ex, tk, ex, len(png))
            return png, ex
        last_err = err
        logger.warning(
            "Snapshot failed %s:%s via %s -> %s", ex, tk, ex, _safe_trunc(err)
        )
        time.sleep(0.75)

    raise RuntimeError(
        f"All exchanges failed for {tk}. Last error: {last_err}. Tried: {tried}"
    )


# ---------------------------------------------------------------------
# TELEGRAM SEND HELPERS
# ---------------------------------------------------------------------
async def send_snapshot_photo(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    exchange: str,
    ticker: str,
    interval: str,
    theme: str,
    prefix: str = "",
    alt_exchanges: Optional[Sequence[str]] = None,
):
    """
    Async wrapper: screenshot + send photo.
    """
    if rate_limited(chat_id):
        await context.bot.send_message(
            chat_id, "⏳ Too many requests; please wait a few seconds…"
        )
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)

    # May want to ping "start-browser" proactively (ignore errors)
    await asyncio.to_thread(node_start_browser)

    try:
        png, ex_used = await asyncio.to_thread(
            fetch_snapshot_png_any,
            exchange,
            ticker,
            interval,
            theme,
            "chart",
            alt_exchanges,
            BASE_URL,
        )
        caption = f"{prefix}{ex_used}:{ticker} • TF {interval} • {theme}"
        await context.bot.send_photo(chat_id=chat_id, photo=png, caption=caption)
    except Exception as e:
        logger.exception("snapshot photo error")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Failed: {exchange}:{ticker} ({_safe_trunc(e, 350)})",
        )


def build_media_items_sync(
    pairs: List[Tuple[str, str, str, List[str]]],
    interval: str,
    theme: str,
    prefix: str,
) -> List[InputMediaPhoto]:
    """
    Build a list of InputMediaPhoto objects for a list of pairs (blocking).
    """
    out: List[InputMediaPhoto] = []
    for ex, tk, lab, alt_list in pairs:
        try:
            png, ex_used = fetch_snapshot_png_any(
                ex, tk, interval, theme, "chart", alt_list, BASE_URL
            )
            bio = io.BytesIO(png)
            bio.name = "chart.png"
            cap = f"{prefix}{ex_used}:{tk} • {lab} • TF {interval} • {theme}"
            out.append(InputMediaPhoto(media=bio, caption=cap))
        except Exception as e:
            logger.warning("Media build fail %s:%s %s", ex, tk, _safe_trunc(e))
    return out


async def send_media_group_chunked(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    media_items: List[InputMediaPhoto],
    chunk_size: int = 5,
):
    """
    Telegram's media group limit ~10; we chunk w/ safe margin.
    """
    for i in range(0, len(media_items), chunk_size):
        chunk = media_items[i : i + chunk_size]
        if not chunk:
            continue
        # Only first caption in a group reliably displays
        if len(chunk) > 1:
            for m in chunk[1:]:
                m.caption = None
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------
# COMMAND ARG PARSERS
# ---------------------------------------------------------------------
def parse_snap_args(args: List[str]) -> Tuple[str, str, str, str, List[str]]:
    # /snap SYMBOL [interval] [theme]
    symbol = args[0] if args else "EUR/USD"
    tf = DEFAULT_INTERVAL
    th = DEFAULT_THEME
    if len(args) >= 2 and args[1].lower() not in ("dark", "light"):
        tf = args[1]
    if len(args) >= 2 and args[-1].lower() in ("dark", "light"):
        th = args[-1].lower()
    elif len(args) >= 3 and args[2].lower() in ("dark", "light"):
        th = args[2].lower()
    ex, tk, _is_otc, alt = resolve_symbol(symbol)
    return ex, tk, norm_interval(tf), norm_theme(th), alt


def parse_multi_args(args: List[str]) -> Tuple[List[str], str, str]:
    # /snapmulti P1 P2 ... [interval] [theme]
    if not args:
        return [], DEFAULT_INTERVAL, DEFAULT_THEME
    theme = DEFAULT_THEME
    if args[-1].lower() in ("dark", "light"):
        theme = args[-1].lower()
        args = args[:-1]
    tf = DEFAULT_INTERVAL
    if args and re.fullmatch(r"\d+", args[-1]):
        tf = args[-1]
        args = args[:-1]
    return args, norm_interval(tf), norm_theme(theme)


def parse_trade_args(args: List[str]) -> Tuple[str, str, str, str]:
    """
    /trade SYMBOL CALL|PUT [expiry] [theme]
    expiry string returned raw (e.g., "5m").
    """
    if not args:
        return "EUR/USD", "CALL", "5m", DEFAULT_THEME
    symbol = args[0]
    direction = parse_direction(args[1] if len(args) >= 2 else None) or "CALL"
    expiry = args[2] if len(args) >= 3 else "5m"
    theme = args[3] if len(args) >= 4 else DEFAULT_THEME
    return symbol, direction, expiry, theme


# ---------------------------------------------------------------------
# INLINE KEYBOARD HELPERS
# ---------------------------------------------------------------------
EXPIRY_CHOICES = ["1m", "3m", "5m", "15m"]


def _ik_row(*buttons: InlineKeyboardButton) -> List[InlineKeyboardButton]:
    return list(buttons)


def kb_pairs_root() -> InlineKeyboardMarkup:
    """
    /pairs initial: choose FX vs OTC.
    """
    kb = [
        _ik_row(InlineKeyboardButton("💱 FX Pairs", callback_data="pairs_fx")),
        _ik_row(InlineKeyboardButton("🕒 OTC Pairs", callback_data="pairs_otc")),
    ]
    return InlineKeyboardMarkup(kb)


def kb_pairs_list(pairs: Sequence[str], prefix: str) -> InlineKeyboardMarkup:
    """
    Build a grid of pair buttons; prefix is 'pfx' or 'potc' etc to encode domain.
    """
    rows: List[List[InlineKeyboardButton]] = []
    # 2 per row
    for i in range(0, len(pairs), 2):
        a = pairs[i]
        b = pairs[i + 1] if i + 1 < len(pairs) else None
        row = [
            InlineKeyboardButton(a, callback_data=f"{prefix}:{a}"),
        ]
        if b:
            row.append(InlineKeyboardButton(b, callback_data=f"{prefix}:{b}"))
        rows.append(row)
    rows.append(_ik_row(InlineKeyboardButton("⬅ Back", callback_data="pairs_back")))
    return InlineKeyboardMarkup(rows)


def kb_direction(pair: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            _ik_row(
                InlineKeyboardButton("🟢 CALL ↑", callback_data=f"dir:CALL:{pair}"),
                InlineKeyboardButton("🔴 PUT ↓", callback_data=f"dir:PUT:{pair}"),
            ),
            _ik_row(InlineKeyboardButton("⬅ Back", callback_data="pairs_back")),
        ]
    )


def kb_expiry(pair: str, direction: str) -> InlineKeyboardMarkup:
    row1 = _ik_row(
        InlineKeyboardButton("1m", callback_data=f"exp:{direction}:1m:{pair}"),
        InlineKeyboardButton("3m", callback_data=f"exp:{direction}:3m:{pair}"),
        InlineKeyboardButton("5m", callback_data=f"exp:{direction}:5m:{pair}"),
        InlineKeyboardButton("15m", callback_data=f"exp:{direction}:15m:{pair}"),
    )
    kb = [row1, _ik_row(InlineKeyboardButton("⬅ Back", callback_data="pairs_back"))]
    return InlineKeyboardMarkup(kb)


# ---------------------------------------------------------------------
# BOT COMMAND HANDLERS
# ---------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nm = update.effective_user.first_name if update.effective_user else ""
    msg = (
        f"Hi {nm} 👋\n\n"
        "I'm your *TradingView Snapshot Bot* (Pocket Option / Binary).\n\n"
        "Try:\n"
        "• /pairs – tap a pair, pick direction & expiry.\n"
        "• /trade EUR/USD CALL 5m – quick shot.\n"
        "• /snap EUR/USD 5 dark – chart only.\n"
        "• /snapmulti EUR/USD GBP/USD 15 light.\n"
        "• /snapall – all FX & OTC.\n"
        "• /help – full help.\n"
    )
    await update.effective_chat.send_message(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📘 *Help*\n\n"
        "*/snap* SYMBOL [interval] [theme]\n"
        "*/trade* SYMBOL CALL|PUT [expiry] [theme]\n"
        "*/snapmulti* S1 S2 ... [interval] [theme]\n"
        "*/snapall* (all FX+OTC)\n"
        "*/pairs* interactive selection\n"
        "*/next* watch for next signal (from TV alerts)\n\n"
        "_Intervals:_ number=minutes, D=day, W=week, M=month.\n"
        "_Themes:_ dark|light.\n"
    )
    await update.effective_chat.send_message(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "Select a category:", reply_markup=kb_pairs_root()
    )


async def cmd_snap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ex, tk, tf, th, alt = parse_snap_args(context.args)
    await send_snapshot_photo(
        update.effective_chat.id, context, ex, tk, tf, th, alt_exchanges=alt
    )


async def cmd_snapmulti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs, tf, th = parse_multi_args(context.args)
    if not pairs:
        await update.effective_chat.send_message(
            "Usage: /snapmulti SYM1 SYM2 ... [interval] [theme]"
        )
        return
    chat_id = update.effective_chat.id
    await update.effective_chat.send_message(
        f"📸 Capturing {len(pairs)} charts… please wait."
    )
    p_trip: List[Tuple[str, str, str, List[str]]] = []
    for p in pairs:
        ex, tk, _is_otc, alt = resolve_symbol(p)
        p_trip.append((ex, tk, p, alt))
    media_items = await asyncio.to_thread(
        build_media_items_sync, p_trip, tf, th, prefix="[MULTI] "
    )
    if not media_items:
        await update.effective_chat.send_message("❌ No charts captured.")
        return
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)


async def cmd_snapall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.effective_chat.send_message(
        f"⚡ Capturing all {len(ALL_PAIRS)} pairs… this may take a while."
    )
    p_trip: List[Tuple[str, str, str, List[str]]] = []
    for p in ALL_PAIRS:
        ex, tk, _is_otc, alt = resolve_symbol(p)
        p_trip.append((ex, tk, p, alt))
    media_items = await asyncio.to_thread(
        build_media_items_sync, p_trip, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[ALL] "
    )
    if not media_items:
        await update.effective_chat.send_message("❌ No charts captured.")
        return
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)


async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /trade SYMBOL CALL|PUT [expiry] [theme]
    symbol, direction, expiry, theme = parse_trade_args(context.args)
    ex, tk, _is_otc, alt = resolve_symbol(symbol)
    tf = norm_interval(DEFAULT_INTERVAL)
    th = norm_theme(theme)
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    await update.effective_chat.send_message(
        f"{arrow} *{symbol}* {direction}  Expiry: {expiry}",
        parse_mode=ParseMode.MARKDOWN,
    )
    await send_snapshot_photo(
        update.effective_chat.id,
        context,
        ex,
        tk,
        tf,
        th,
        prefix="[TRADE] ",
        alt_exchanges=alt,
    )


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "👀 Watching for next signal (placeholder). Connect TradingView alerts to /tv."
    )


# ---------------------------------------------------------------------
# MESSAGE ECHO w/ QUICK TRADE PARSE
# ---------------------------------------------------------------------
_trade_re = re.compile(
    r"(?i)trade\s+([A-Z/\-]+)\s+(call|put|buy|sell|up|down)\s+([0-9]+m?)"
)


async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    m = _trade_re.match(txt)
    if m:
        symbol, dirw, exp = m.group(1), m.group(2), m.group(3)
        direction = parse_direction(dirw) or "CALL"
        ex, tk, _is_otc, alt = resolve_symbol(symbol)
        arrow = "🟢↑" if direction == "CALL" else "🔴↓"
        await update.effective_chat.send_message(
            f"{arrow} *{symbol}* {direction} Expiry {exp}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await send_snapshot_photo(
            update.effective_chat.id,
            context,
            ex,
            tk,
            DEFAULT_INTERVAL,
            DEFAULT_THEME,
            prefix="[TRADE] ",
            alt_exchanges=alt,
        )
        return
    await update.effective_chat.send_message(
        f"You said: {txt}\nTry /trade EUR/USD CALL 5m"
    )


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message("❌ Unknown command. Try /help.")


# ---------------------------------------------------------------------
# CALLBACK QUERY HANDLERS (INLINE UI FLOW)
# ---------------------------------------------------------------------
# Data forms:
#   pairs_fx / pairs_otc / pairs_back
#   pfx:EUR/USD
#   potc:EUR/USD-OTC
#   dir:CALL:EUR/USD
#   dir:PUT:EUR/USD
#   exp:CALL:5m:EUR/USD
#   exp:PUT:1m:GBP/USD-OTC


async def cb_pairs_root(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Select a category:", reply_markup=kb_pairs_root())


async def cb_pairs_fx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "FX Pairs (tap one):", reply_markup=kb_pairs_list(FX_PAIRS, "pfx")
    )


async def cb_pairs_otc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "OTC Pairs (tap one):", reply_markup=kb_pairs_list(OTC_PAIRS, "potc")
    )


async def cb_pair_selected(update: Update, context: ContextTypes.DEFAULT_TYPE, pair: str):
    q = update.callback_query
    await q.answer()
    context.user_data["sel_pair"] = pair
    await q.edit_message_text(
        text=f"{pair}\nSelect direction:", reply_markup=kb_direction(pair)
    )


async def cb_dir_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE, direction: str, pair: str
):
    q = update.callback_query
    await q.answer()
    context.user_data["sel_pair"] = pair
    context.user_data["sel_dir"] = direction
    await q.edit_message_text(
        text=f"{pair} {direction}\nSelect expiry:",
        reply_markup=kb_expiry(pair, direction),
    )


async def cb_exp_selected(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    direction: str,
    expiry: str,
    pair: str,
):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    await q.edit_message_text(
        text=f"{arrow} {pair} {direction}  Expiry: {expiry}", reply_markup=None
    )

    # Kick off trade snapshot
    ex, tk, _is_otc, alt = resolve_symbol(pair)
    tf = norm_interval(DEFAULT_INTERVAL)
    th = DEFAULT_THEME
    await send_snapshot_photo(
        chat_id,
        context,
        ex,
        tk,
        tf,
        th,
        prefix="[TRADE] ",
        alt_exchanges=alt,
    )


# Single unified callback dispatcher
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data

    if data == "pairs_back":
        await cb_pairs_root(update, context)
        return

    if data == "pairs_fx":
        await cb_pairs_fx(update, context)
        return

    if data == "pairs_otc":
        await cb_pairs_otc(update, context)
        return

    if data.startswith("pfx:"):
        pair = data.split(":", 1)[1]
        await cb_pair_selected(update, context, pair)
        return

    if data.startswith("potc:"):
        pair = data.split(":", 1)[1]
        await cb_pair_selected(update, context, pair)
        return

    if data.startswith("dir:"):
        # dir:CALL:EUR/USD
        _, direction, pair = data.split(":", 2)
        await cb_dir_selected(update, context, direction, pair)
        return

    if data.startswith("exp:"):
        # exp:CALL:5m:EUR/USD
        _, direction, expiry, pair = data.split(":", 3)
        await cb_exp_selected(update, context, direction, expiry, pair)
        return

    # fallback unknown
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Unknown selection. Try /pairs.")


# ---------------------------------------------------------------------
# FLASK TRADINGVIEW WEBHOOK
# ---------------------------------------------------------------------
flask_app = Flask(__name__)


def _parse_tv_payload(data: dict) -> Dict[str, str]:
    d: Dict[str, str] = {}
    d["chat_id"] = str(data.get("chat_id") or DEFAULT_CHAT_ID or "")
    d["pair"] = str(data.get("pair") or data.get("symbol") or data.get("ticker") or "EUR/USD")
    d["direction"] = str(data.get("direction") or "CALL").upper()
    d["expiry"] = str(data.get("default_expiry_min") or data.get("expiry") or "")
    d["strategy"] = str(data.get("strategy") or "")
    d["winrate"] = str(data.get("winrate") or "")
    d["timeframe"] = str(data.get("timeframe") or data.get("tf") or DEFAULT_INTERVAL)
    d["theme"] = str(data.get("theme") or DEFAULT_THEME)
    return d


def tg_api_send_message(chat_id: str, text: str, parse_mode: Optional[str] = None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        _http.post(url, json=payload, timeout=30)
    except Exception as e:
        logger.error("tg_api_send_message: %s", _safe_trunc(e))


def tg_api_send_photo_bytes(chat_id: str, png: bytes, caption: str = ""):
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", png, "image/png")}
    data = {"chat_id": chat_id, "caption": caption}
    try:
        _http.post(url, data=data, files=files, timeout=60)
    except Exception as e:
        logger.error("tg_api_send_photo_bytes: %s", _safe_trunc(e))


def node_start_browser():
    """
    Non-fatal ping to warm the snapshot server’s browser session.
    """
    try:
        r = _http.get(f"{BASE_URL}/start-browser", timeout=10)
        logger.debug("start-browser %s %s", r.status_code, _safe_trunc(r.text))
    except Exception as e:
        logger.warning("start-browser failed: %s", _safe_trunc(e))


def _handle_tv_alert(data: dict):
    """
    Process a TradingView alert payload synchronously (Flask thread).
    Accept both header-based and body-based secrets.
    """
    # Security
    if WEBHOOK_SECRET:
        hdr = request.headers.get("X-Webhook-Token", "")
        body_secret = str(data.get("secret") or data.get("token") or "")
        if hdr != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET:
            logger.warning("Webhook secret mismatch; rejecting.")
            return {"ok": False, "error": "unauthorized"}, 403

    payload = _parse_tv_payload(data)
    logger.info("TV payload normalized: %s", payload)

    chat_id = payload["chat_id"]
    raw_pair = payload["pair"]
    direction = parse_direction(payload["direction"]) or "CALL"
    expiry = payload["expiry"]
    strat = payload["strategy"]
    winrate = payload["winrate"]
    tf = norm_interval(payload["timeframe"])
    theme = norm_theme(payload["theme"])

    ex, tk, _is_otc, alt = resolve_symbol(raw_pair)

    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    msg = (
        f"🔔 *TradingView Alert*\n"
        f"Pair: {raw_pair}\n"
        f"Direction: {arrow} {direction}\n"
        f"Expiry: {expiry}\n"
        f"Strategy: {strat}\n"
        f"Win Rate: {winrate}\n"
        f"TF: {tf} • Theme: {theme}"
    )
    tg_api_send_message(chat_id, msg, parse_mode="Markdown")

    # Attempt chart w/ fallback
    try:
        node_start_browser()
        png, ex_used = fetch_snapshot_png_any(
            ex, tk, tf, theme, "chart", alt, BASE_URL
        )
        tg_api_send_photo_bytes(
            chat_id, png, caption=f"{ex_used}:{tk} • TF {tf} • {theme}"
        )
    except Exception as e:
        logger.error(
            "TV snapshot error for %s:%s -> %s", ex, tk, _safe_trunc(e, 350)
        )
        tg_api_send_message(
            chat_id, f"⚠ Chart snapshot failed for {raw_pair}: {_safe_trunc(e, 350)}"
        )

    return {"ok": True}, 200


@flask_app.post("/tv")
def tv_route():
    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:
        logger.error("TV /tv invalid JSON: %s", _safe_trunc(e))
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    body, code = _handle_tv_alert(data)
    return jsonify(body), code


# compatibility alias
@flask_app.route("/webhook", methods=["POST"])
def tv_route_alias():
    return tv_route()


def start_flask_background():
    """
    Run Flask in its own thread so Telegram bot polling can run in main thread.
    """
    threading.Thread(
        target=lambda: flask_app.run(
            host="0.0.0.0",
            port=TV_WEBHOOK_PORT,
            debug=False,
            use_reloader=False,
            threaded=True,
        ),
        daemon=True,
    ).start()
    logger.info("Flask TV webhook listening on port %s", TV_WEBHOOK_PORT)


# ---------------------------------------------------------------------
# APPLICATION FACTORY
# ---------------------------------------------------------------------
def build_application() -> Application:
    """
    Build the python-telegram-bot Application w/ handlers.
    """
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .rate_limiter(AIORateLimiter())  # PTB built-in flood control
        .build()
    )

    # COMMANDS
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pairs", cmd_pairs))
    app.add_handler(CommandHandler("snap", cmd_snap))
    app.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    app.add_handler(CommandHandler("snapall", cmd_snapall))
    app.add_handler(CommandHandler("trade", cmd_trade))
    app.add_handler(CommandHandler("next", cmd_next))

    # CALLBACKS
    app.add_handler(CallbackQueryHandler(on_callback))

    # MESSAGES
    app.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text)
    )
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    return app


# ---------------------------------------------------------------------
# MAIN ENTRY
# ---------------------------------------------------------------------
def main():
    start_flask_background()

    application = build_application()

    logger.info(
        "Bot polling… (Default=%s) | Webhook port %s | Snapshot base=%s",
        DEFAULT_EXCHANGE,
        TV_WEBHOOK_PORT,
        BASE_URL,
    )
    # run_polling blocks; handles its own event loop
    application.run_polling()


if __name__ == "__main__":
    main()
