#!/usr/bin/env python
"""
tvsnapshotbot.py
================
TradingView → Telegram Snapshot Bot (FX + OTC + Pocket/Binary friendly)
Modern async python-telegram-bot v20+ w/ inline keyboards.

Major Features
--------------
• Inline Keyboard UI:
    /pairs → pick category → pick pair → pick direction (CALL/PUT) → pick expiry (1m/3m/5m/15m) → auto snapshot+trade message.
• /snap SYMBOL [interval] [theme]
• /trade SYMBOL CALL|PUT [expiry] [theme]
• /snapmulti S1 S2 ... [interval] [theme]
• /snapall (send all FX+OTC chunked media groups)
• /next (placeholder "watch for next signal" message)
• Rate limiting (per chat + global throttle for snapshot service calls)
• Retry snapshot fetch w/ multi-exchange fallback
• Rotating log file logs/tvsnapshotbot.log
• TradingView webhook server (/tv + /webhook) — receives Pine alert JSON; posts trade + chart to Telegram.
• Pocket Option / Binary style direction synonyms (CALL/PUT/BUY/SELL/UP/DOWN).
• Theme memory & last interval memory per chat (optional convenience).
• Works w/ Node snapshot service (server.js) at $SNAPSHOT_BASE_URL.

Environment Variables
---------------------
TELEGRAM_BOT_TOKEN     (required)
TELEGRAM_CHAT_ID       (fallback if no chat_id in TV payload)
SNAPSHOT_BASE_URL      (http://localhost:10000 or https://your.render.app)
DEFAULT_EXCHANGE       (CURRENCY recommended w/ fallback chain)
DEFAULT_INTERVAL       (1)
DEFAULT_THEME          (dark|light)
TV_WEBHOOK_PORT        (8081)
WEBHOOK_SECRET         (optional shared secret compare to body.secret or header X-Webhook-Token)

Run
---
> set TELEGRAM_BOT_TOKEN=123456:ABC...
> set TELEGRAM_CHAT_ID=6337160812
> set SNAPSHOT_BASE_URL=http://localhost:10000
> python tvsnapshotbot.py

"""

from __future__ import annotations

import os
import io
import re
import json
import time
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Any, Callable
import requests
from flask import Flask, request, jsonify

from telegram import (
    Update,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
os.makedirs("logs", exist_ok=True)
LOG_FILE = "logs/tvsnapshotbot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3),
    ],
)
logger = logging.getLogger("TVSnapBot")

# ---------------------------------------------------------------------------
# Env / Config
# ---------------------------------------------------------------------------
TOKEN            = os.environ.get("TELEGRAM_BOT_TOKEN")
DEFAULT_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
BASE_URL         = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000")
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "CURRENCY").upper()
DEFAULT_INTERVAL = os.environ.get("DEFAULT_INTERVAL", "1")
DEFAULT_THEME    = os.environ.get("DEFAULT_THEME", "dark")
TV_WEBHOOK_PORT  = int(os.environ.get("TV_WEBHOOK_PORT", "8081"))
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET")  # optional

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set.")

# fallback exchanges used by snapshot attempts (in addition to exchange from mapping)
EXCHANGE_FALLBACKS = [
    "FX",
    "FX_IDC",
    "OANDA",
    "FOREXCOM",
    "IDC",
    "QUOTEX",
]
# these we always consider as known foreign feed for currency
KNOWN_FX_EXCHANGES = ["FX", "FX_IDC", "OANDA", "FOREXCOM", "IDC"]

_http = requests.Session()

# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------
LAST_SNAPSHOT_PER_CHAT: Dict[int, float] = {}
RATE_LIMIT_SECONDS = 3

GLOBAL_LAST_SNAPSHOT = 0.0
GLOBAL_MIN_GAP = 0.75  # seconds between ANY two requests to snapshot service


def rate_limited(chat_id: int) -> bool:
    now = time.time()
    last = LAST_SNAPSHOT_PER_CHAT.get(chat_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    LAST_SNAPSHOT_PER_CHAT[chat_id] = now
    return False


def global_throttle_wait():
    global GLOBAL_LAST_SNAPSHOT
    now = time.time()
    gap = now - GLOBAL_LAST_SNAPSHOT
    if gap < GLOBAL_MIN_GAP:
        time.sleep(GLOBAL_MIN_GAP - gap)
    GLOBAL_LAST_SNAPSHOT = time.time()


# ---------------------------------------------------------------------------
# Pairs (FX & OTC)
# ---------------------------------------------------------------------------
FX_PAIRS: List[str] = [
    "EUR/USD","GBP/USD","USD/JPY","USD/CHF","AUD/USD",
    "NZD/USD","USD/CAD","EUR/GBP","EUR/JPY","GBP/JPY",
    "AUD/JPY","NZD/JPY","EUR/AUD","GBP/AUD","EUR/CAD",
    "USD/MXN","USD/TRY","USD/ZAR","AUD/CHF","EUR/CHF",
]

OTC_PAIRS: List[str] = [
    "EUR/USD-OTC","GBP/USD-OTC","USD/JPY-OTC","USD/CHF-OTC","AUD/USD-OTC",
    "NZD/USD-OTC","USD/CAD-OTC","EUR/GBP-OTC","EUR/JPY-OTC","GBP/JPY-OTC",
    "AUD/CHF-OTC","EUR/CHF-OTC","KES/USD-OTC","MAD/USD-OTC",
    "USD/BDT-OTC","USD/MXN-OTC","USD/MYR-OTC","USD/PKR-OTC",
]

ALL_PAIRS: List[str] = FX_PAIRS + OTC_PAIRS


def _canon_key(pair: str) -> str:
    return pair.strip().upper().replace(" ", "").replace("/", "")


# Map: canonical -> (exchange, ticker, alt_exchanges list)
# If pair is OTC we map to underlying ticker but primary exchange = DEFAULT_EXCHANGE?
# We'll pick "CURRENCY" parent feed for majors; OTC underlying uses same.
PAIR_MAP: Dict[str, Tuple[str, str, List[str]]] = {}

# majors → DEFAULT_EXCHANGE w/ alt fallback chain
for p in FX_PAIRS:
    PAIR_MAP[_canon_key(p)] = (
        DEFAULT_EXCHANGE,
        p.replace("/", ""),
        EXCHANGE_FALLBACKS.copy(),
    )

_underlying_otc = {
    "EUR/USD-OTC":"EURUSD","GBP/USD-OTC":"GBPUSD","USD/JPY-OTC":"USDJPY",
    "USD/CHF-OTC":"USDCHF","AUD/USD-OTC":"AUDUSD","NZD/USD-OTC":"NZDUSD",
    "USD/CAD-OTC":"USDCAD","EUR/GBP-OTC":"EURGBP","EUR/JPY-OTC":"EURJPY",
    "GBP/JPY-OTC":"GBPJPY","AUD/CHF-OTC":"AUDCHF","EUR/CHF-OTC":"EURCHF",
    "KES/USD-OTC":"USDKES","MAD/USD-OTC":"USDMAD","USD/BDT-OTC":"USDBDT",
    "USD/MXN-OTC":"USDMXN","USD/MYR-OTC":"USDMYR","USD/PKR-OTC":"USDPKR",
}
for p, tk in _underlying_otc.items():
    # For OTC we still try DEFAULT_EXCHANGE but also QUOTEX
    PAIR_MAP[_canon_key(p)] = (
        DEFAULT_EXCHANGE,
        tk,
        ["QUOTEX"] + EXCHANGE_FALLBACKS.copy(),
    )


# ---------------------------------------------------------------------------
# Normalization Helpers
# ---------------------------------------------------------------------------
def norm_interval(tf: str) -> str:
    if not tf:
        return DEFAULT_INTERVAL
    t = tf.strip().lower()
    if t.endswith("m") and t[:-1].isdigit():
        return t[:-1]
    if t.endswith("h") and t[:-1].isdigit():
        return str(int(t[:-1]) * 60)
    if t in ("d","1d","day"):
        return "D"
    if t in ("w","1w","week"):
        return "W"
    if t in ("m","1m","mo","month"):
        return "M"
    if t.isdigit():
        return t
    return DEFAULT_INTERVAL


def norm_theme(val: str) -> str:
    return "light" if (val and val.lower().startswith("l")) else "dark"


# ---------------------------------------------------------------------------
# Symbol Resolution
# ---------------------------------------------------------------------------
def resolve_symbol(raw: str) -> Tuple[str, str, bool, List[str]]:
    """
    Return (exchange, ticker, is_otc, alt_exchanges_list).
    Accept forms:
        EUR/USD
        EURUSD
        CURRENCY:EURUSD
        eur/usd-otc
    """
    if not raw:
        return DEFAULT_EXCHANGE, "EURUSD", False, EXCHANGE_FALLBACKS.copy()

    s = raw.strip().upper()
    is_otc = False
    if s.endswith("-OTC") or s.endswith("_OTC"):
        is_otc = True
        s = re.sub(r"[-_]OTC$", "", s)

    if ":" in s:
        ex, tk = s.split(":", 1)
        # alt fallback chain always appended
        return ex, tk, is_otc, EXCHANGE_FALLBACKS.copy()

    key = _canon_key(s)
    if key in PAIR_MAP:
        ex, tk, alt = PAIR_MAP[key]
        return ex, tk, is_otc, alt

    tk = re.sub(r"[^A-Z0-9]", "", s)
    return DEFAULT_EXCHANGE, tk, is_otc, EXCHANGE_FALLBACKS.copy()


# ---------------------------------------------------------------------------
# Snapshot Backend Calls (server.js)
# ---------------------------------------------------------------------------
def node_start_browser():
    """Ping Node /start-browser (fire & forget)."""
    try:
        r = _http.get(f"{BASE_URL}/start-browser", timeout=10)
        logger.debug("start-browser %s %s", r.status_code, r.text[:100])
    except Exception as e:
        logger.warning("start-browser failed: %s", e)


def _attempt_snapshot_url(ex: str, tk: str, interval: str, theme: str, base: str) -> Tuple[bool, Optional[bytes], str]:
    """Single attempt; returns (success, png_bytes_or_none, errmsg)."""
    try:
        global_throttle_wait()
        url = f"{BASE_URL}/run?base={base}&exchange={ex}&ticker={tk}&interval={interval}&theme={theme}"
        r = _http.get(url, timeout=75)
        ct = r.headers.get("Content-Type","")
        if r.status_code == 200 and ct.startswith("image"):
            return True, r.content, ""
        return False, None, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, None, str(e)


def fetch_snapshot_png_any(
    primary_ex: str,
    tk: str,
    interval: str,
    theme: str,
    base: str="chart",
    extra_exchanges: Optional[List[str]]=None,
) -> Tuple[bytes, str]:
    """
    Try multiple exchanges to get a snapshot.
    Returns (png_bytes, exchange_used).
    Raises RuntimeError if all fail.
    """
    tried: List[str] = []
    last_err = None

    merged: List[str] = [primary_ex.upper()]
    if extra_exchanges:
        merged.extend([x.upper() for x in extra_exchanges])
    # also include env fallback + known fx
    for x in EXCHANGE_FALLBACKS + KNOWN_FX_EXCHANGES:
        merged.append(x.upper())

    # dedup preserve order
    dedup: List[str] = []
    seen = set()
    for x in merged:
        if x not in seen:
            dedup.append(x)
            seen.add(x)

    for ex in dedup:
        tried.append(ex)
        ok, png, err = _attempt_snapshot_url(ex, tk, interval, theme, base)
        if ok and png:
            logger.info("Snapshot success %s:%s via %s (%d bytes)", ex, tk, ex, len(png))
            return png, ex
        last_err = err
        logger.warning("Snapshot failed %s:%s via %s -> %s", ex, tk, ex, err)
        time.sleep(2)

    raise RuntimeError(f"All exchanges failed for {tk}. Last error: {last_err}. Tried: {tried}")


# ---------------------------------------------------------------------------
# Telegram Send Helpers (async)
# ---------------------------------------------------------------------------
async def send_snapshot_photo(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    exchange: str,
    ticker: str,
    interval: str,
    theme: str,
    prefix: str="",
    alt_exchanges: Optional[List[str]]=None,
):
    if rate_limited(chat_id):
        await context.bot.send_message(chat_id, "⏳ Too many requests; wait a few seconds…")
        return
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    await asyncio.to_thread(node_start_browser)
    try:
        png, ex_used = await asyncio.to_thread(
            fetch_snapshot_png_any, exchange, ticker, interval, theme, "chart", alt_exchanges
        )
        caption = f"{prefix}{ex_used}:{ticker} • TF {interval} • {theme}"
        await context.bot.send_photo(chat_id=chat_id, photo=png, caption=caption)
    except Exception as e:
        logger.exception("snapshot photo error")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Failed: {exchange}:{ticker} ({e})")


def build_media_items_sync(
    pairs: List[Tuple[str,str,str,List[str]]],
    interval: str,
    theme: str,
    prefix: str,
) -> List[InputMediaPhoto]:
    out: List[InputMediaPhoto] = []
    for ex, tk, lab, alt_list in pairs:
        try:
            png, ex_used = fetch_snapshot_png_any(ex, tk, interval, theme, "chart", alt_list)
            bio = io.BytesIO(png); bio.name = "chart.png"
            cap = f"{prefix}{ex_used}:{tk} • {lab} • TF {interval} • {theme}"
            out.append(InputMediaPhoto(media=bio, caption=cap))
        except Exception as e:
            logger.warning("Media build fail %s:%s %s", ex, tk, e)
    return out


async def send_media_group_chunked(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    media_items: List[InputMediaPhoto],
    chunk_size: int=5,
):
    for i in range(0, len(media_items), chunk_size):
        chunk = media_items[i:i+chunk_size]
        if not chunk:
            continue
        # only first caption shows reliably
        if len(chunk) > 1:
            for m in chunk[1:]:
                m.caption = None
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# Direction Parsing (Pocket Option/Binary friendly)
# ---------------------------------------------------------------------------
_CALL_WORDS = {"CALL","BUY","UP","LONG"}
_PUT_WORDS  = {"PUT","SELL","DOWN","SHORT"}


def parse_direction(word: Optional[str]) -> Optional[str]:
    if not word:
        return None
    w = word.strip().upper()
    if w in _CALL_WORDS:
        return "CALL"
    if w in _PUT_WORDS:
        return "PUT"
    return None


# ---------------------------------------------------------------------------
# Chat Session State
# ---------------------------------------------------------------------------
@dataclass
class ChatState:
    theme: str = DEFAULT_THEME
    interval: str = DEFAULT_INTERVAL
    last_pair: Optional[str] = None
    # ephemeral inline workflow scratch
    pending_symbol: Optional[str] = None
    pending_direction: Optional[str] = None
    pending_expiry: Optional[str] = None

CHAT_STATE: Dict[int, ChatState] = {}


def get_chat_state(chat_id: int) -> ChatState:
    st = CHAT_STATE.get(chat_id)
    if not st:
        st = ChatState()
        CHAT_STATE[chat_id] = st
    return st


# ---------------------------------------------------------------------------
# Command Argument Parsing
# ---------------------------------------------------------------------------
def parse_snap_args(args: List[str]) -> Tuple[str,str,str,str,List[str]]:
    # /snap SYMBOL [interval] [theme]
    symbol = args[0] if args else "EUR/USD"
    tf = DEFAULT_INTERVAL
    th = DEFAULT_THEME
    if len(args) >= 2 and args[1].lower() not in ("dark","light"):
        tf = args[1]
    if len(args) >= 2 and args[-1].lower() in ("dark","light"):
        th = args[-1].lower()
    elif len(args) >= 3 and args[2].lower() in ("dark","light"):
        th = args[2].lower()
    ex, tk, _is_otc, alt = resolve_symbol(symbol)
    return ex, tk, norm_interval(tf), norm_theme(th), alt


def parse_multi_args(args: List[str]) -> Tuple[List[str],str,str]:
    # /snapmulti P1 P2 ... [interval] [theme]
    if not args:
        return [], DEFAULT_INTERVAL, DEFAULT_THEME
    theme = DEFAULT_THEME
    if args[-1].lower() in ("dark","light"):
        theme = args[-1].lower()
        args = args[:-1]
    tf = DEFAULT_INTERVAL
    if args and re.fullmatch(r"\d+", args[-1]):
        tf = args[-1]; args = args[:-1]
    return args, norm_interval(tf), norm_theme(theme)


def parse_trade_args(args: List[str]) -> Tuple[str,str,str,str]:
    """
    /trade SYMBOL CALL|PUT [expiry] [theme]
    expiry string is returned raw
    """
    if not args:
        return "EUR/USD","CALL","5m",DEFAULT_THEME
    symbol = args[0]
    direction = parse_direction(args[1] if len(args)>=2 else None) or "CALL"
    expiry = args[2] if len(args)>=3 else "5m"
    theme = args[3] if len(args)>=4 else DEFAULT_THEME
    return symbol, direction, expiry, theme


# ---------------------------------------------------------------------------
# Inline Keyboard Builders
# ---------------------------------------------------------------------------
TRADE_EXPIRIES = ["1m","3m","5m","15m"]


def _kb_main_actions() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 Pick Pair", callback_data="PAIRS")],
        [InlineKeyboardButton("⚡ Snap All", callback_data="ACT_SNAPALL")],
        [InlineKeyboardButton("👀 Watch Next", callback_data="ACT_NEXT")],
        [InlineKeyboardButton("ℹ Help", callback_data="ACT_HELP")],
    ]
    return InlineKeyboardMarkup(rows)


def _kb_pair_categories() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🌍 FX Majors", callback_data="CAT_FX")],
        [InlineKeyboardButton("🕒 OTC / Pocket", callback_data="CAT_OTC")],
        [InlineKeyboardButton("⬅ Back", callback_data="BACK_HOME")],
    ]
    return InlineKeyboardMarkup(rows)


def _kb_pairs_list(pairs: List[str], prefix: str) -> InlineKeyboardMarkup:
    """
    prefix = "FX" or "OTC"
    We'll show up to ~8 per page row; simple vertical list.
    """
    rows = []
    for p in pairs:
        rows.append([InlineKeyboardButton(p, callback_data=f"PAIR|{p}")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="PAIRS")])
    return InlineKeyboardMarkup(rows)


def _kb_direction(pair: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🟢 CALL / BUY ↑", callback_data=f"DIR|{pair}|CALL"),
            InlineKeyboardButton("🔴 PUT / SELL ↓", callback_data=f"DIR|{pair}|PUT"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data="PAIRS")],
    ]
    return InlineKeyboardMarkup(rows)


def _kb_expiry(pair: str, direction: str) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for exp in TRADE_EXPIRIES:
        row.append(InlineKeyboardButton(exp, callback_data=f"EXP|{pair}|{direction}|{exp}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"DIR_BACK|{pair}")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Bot Command Handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    nm = update.effective_user.first_name if update.effective_user else ""
    msg = (
        f"Hi {nm} 👋\n\n"
        "I'm your TradingView Snapshot Bot.\n\n"
        "Tap a button below or try /help."
    )
    await context.bot.send_message(chat_id, msg, reply_markup=_kb_main_actions())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = (
        "📘 *Help*\n\n"
        "*/snap* SYMBOL [interval] [theme]\n"
        "*/trade* SYMBOL CALL|PUT [expiry] [theme]\n"
        "*/snapmulti* S1 S2 ... [interval] [theme]\n"
        "*/snapall* (all FX+OTC)\n"
        "*/pairs* list supported names (interactive)\n"
        "*/next* watch for next signal (TV alerts)\n\n"
        "_Intervals:_ minutes (#), D, W, M.\n"
        "_Themes:_ dark|light.\n"
    )
    await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN, reply_markup=_kb_main_actions())


async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, "Choose a category:", reply_markup=_kb_pair_categories())


async def cmd_snap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ex, tk, tf, th, alt = parse_snap_args(context.args)
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, th, alt_exchanges=alt)


async def cmd_snapmulti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs, tf, th = parse_multi_args(context.args)
    if not pairs:
        await context.bot.send_message(update.effective_chat.id, "Usage: /snapmulti SYM1 SYM2 ... [interval] [theme]")
        return
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, f"📸 Capturing {len(pairs)} charts…")
    p_trip: List[Tuple[str,str,str,List[str]]] = []
    for p in pairs:
        ex, tk, _is_otc, alt = resolve_symbol(p)
        p_trip.append((ex, tk, p, alt))
    media_items = await asyncio.to_thread(build_media_items_sync, p_trip, tf, th, prefix="[MULTI] ")
    if not media_items:
        await context.bot.send_message(chat_id, "❌ No charts captured.")
        return
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)


async def cmd_snapall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, f"⚡ Capturing all {len(ALL_PAIRS)} pairs… this may take a while.")
    p_trip: List[Tuple[str,str,str,List[str]]] = []
    for p in ALL_PAIRS:
        ex, tk, _is_otc, alt = resolve_symbol(p)
        p_trip.append((ex, tk, p, alt))
    media_items = await asyncio.to_thread(build_media_items_sync, p_trip, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[ALL] ")
    if not media_items:
        await context.bot.send_message(chat_id, "❌ No charts captured.")
        return
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)


async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /trade SYMBOL CALL|PUT [expiry] [theme]
    symbol, direction, expiry, theme = parse_trade_args(context.args)
    ex, tk, _is_otc, alt = resolve_symbol(symbol)
    tf = norm_interval(DEFAULT_INTERVAL)  # chart uses bot default timeframe
    th = norm_theme(theme)
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    msg = f"{arrow} *{symbol}* {direction}  Expiry: {expiry}"
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN)
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, th, prefix="[TRADE] ", alt_exchanges=alt)


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        update.effective_chat.id,
        "👀 Watching for next signal (connect TradingView alerts to /tv).",
        reply_markup=_kb_main_actions(),
    )


# ---------------------------------------------------------------------------
# Inline Callback Dispatcher
# ---------------------------------------------------------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q: CallbackQuery = update.callback_query
    await q.answer()
    chat_id = q.message.chat.id
    data = q.data

    st = get_chat_state(chat_id)

    if data == "PAIRS":
        await q.message.edit_text("Choose a category:", reply_markup=_kb_pair_categories())
        return

    if data == "CAT_FX":
        await q.message.edit_text("Select FX pair:", reply_markup=_kb_pairs_list(FX_PAIRS, "FX"))
        return

    if data == "CAT_OTC":
        await q.message.edit_text("Select OTC pair:", reply_markup=_kb_pairs_list(OTC_PAIRS, "OTC"))
        return

    if data == "BACK_HOME":
        await q.message.edit_text("Back to main.", reply_markup=_kb_main_actions())
        return

    if data == "ACT_SNAPALL":
        # reissue /snapall
        await cmd_snapall(update, context)
        return

    if data == "ACT_NEXT":
        await cmd_next(update, context)
        return

    if data == "ACT_HELP":
        await cmd_help(update, context)
        return

    if data.startswith("PAIR|"):
        pair = data.split("|", 1)[1]
        st.pending_symbol = pair
        st.pending_direction = None
        st.pending_expiry = None
        await q.message.edit_text(f"Pair: {pair}\nPick direction:", reply_markup=_kb_direction(pair))
        return

    if data.startswith("DIR_BACK|"):
        pair = data.split("|", 1)[1]
        await q.message.edit_text("Choose a category:", reply_markup=_kb_pair_categories())
        return

    if data.startswith("DIR|"):
        # DIR|PAIR|CALL
        _, pair, direction = data.split("|", 2)
        st.pending_symbol = pair
        st.pending_direction = direction
        await q.message.edit_text(
            f"{pair} {direction}\nSelect expiry:",
            reply_markup=_kb_expiry(pair, direction),
        )
        return

    if data.startswith("EXP|"):
        # EXP|PAIR|DIR|EXPIRY
        _, pair, direction, expiry = data.split("|", 3)
        st.pending_symbol = pair
        st.pending_direction = direction
        st.pending_expiry = expiry
        await handle_trade_inline(chat_id, context, st)
        return

    # unknown fallback
    await q.message.reply_text("Unknown selection. Try /pairs.")


async def handle_trade_inline(chat_id: int, context: ContextTypes.DEFAULT_TYPE, st: ChatState):
    if not st.pending_symbol:
        await context.bot.send_message(chat_id, "No pair selected.")
        return
    pair = st.pending_symbol
    direction = st.pending_direction or "CALL"
    expiry = st.pending_expiry or "5m"

    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    msg = f"{arrow} *{pair}* {direction}  Expiry: {expiry}"
    await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN)

    ex, tk, _is_otc, alt = resolve_symbol(pair)
    tf = st.interval or DEFAULT_INTERVAL
    th = st.theme or DEFAULT_THEME
    await send_snapshot_photo(chat_id, context, ex, tk, norm_interval(tf), norm_theme(th), prefix="[TRADE] ", alt_exchanges=alt)

    # reset ephemeral
    st.pending_symbol = None
    st.pending_direction = None
    st.pending_expiry = None


# ---------------------------------------------------------------------------
# Echo Fallback (quick trade parse)
# ---------------------------------------------------------------------------
_trade_re = re.compile(r"(?i)trade\s+([A-Z/\-]+)\s+(call|put|buy|sell|up|down)\s+([0-9]+m?)")


async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    m = _trade_re.match(txt)
    if m:
        symbol, dirw, exp = m.group(1), m.group(2), m.group(3)
        direction = parse_direction(dirw) or "CALL"
        ex, tk, _is_otc, alt = resolve_symbol(symbol)
        arrow = "🟢↑" if direction == "CALL" else "🔴↓"
        await context.bot.send_message(
            update.effective_chat.id,
            f"{arrow} *{symbol}* {direction} Expiry {exp}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await send_snapshot_photo(update.effective_chat.id, context, ex, tk, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[TRADE] ", alt_exchanges=alt)
        return
    await context.bot.send_message(update.effective_chat.id, f"You said: {txt}\nTry /trade EUR/USD CALL 5m")


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, "❌ Unknown command. Try /help.")


# ---------------------------------------------------------------------------
# TradingView Webhook (Flask) → Telegram
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)


def _parse_tv_payload(data: dict) -> Dict[str,str]:
    d = {}
    d["chat_id"]   = str(data.get("chat_id") or DEFAULT_CHAT_ID or "")
    d["pair"]      = str(data.get("pair") or data.get("symbol") or data.get("ticker") or "EUR/USD")
    d["direction"] = str(data.get("direction") or "CALL").upper()
    d["expiry"]    = str(data.get("default_expiry_min") or data.get("expiry") or "")
    d["strategy"]  = str(data.get("strategy") or "")
    d["winrate"]   = str(data.get("winrate") or "")
    d["timeframe"] = str(data.get("timeframe") or data.get("tf") or DEFAULT_INTERVAL)
    d["theme"]     = str(data.get("theme") or DEFAULT_THEME)
    return d


def tg_api_send_message(chat_id: str, text: str, parse_mode: Optional[str]=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        _http.post(url, json=payload, timeout=30)
    except Exception as e:
        logger.error("tg_api_send_message: %s", e)


def tg_api_send_photo_bytes(chat_id: str, png: bytes, caption: str=""):
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", png, "image/png")}
    data = {"chat_id": chat_id, "caption": caption}
    try:
        _http.post(url, data=data, files=files, timeout=60)
    except Exception as e:
        logger.error("tg_api_send_photo_bytes: %s", e)


def _handle_tv_alert(data: dict):
    """
    Synchronous handler for TradingView alert payload (Flask thread).
    """
    if WEBHOOK_SECRET:
        hdr = request.headers.get("X-Webhook-Token", "")
        body_secret = str(data.get("secret") or data.get("token") or "")
        if hdr != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET:
            logger.warning("Webhook secret mismatch; rejecting.")
            return {"ok": False, "error": "unauthorized"}, 403

    payload = _parse_tv_payload(data)
    logger.info("TV payload normalized: %s", payload)

    chat_id   = payload["chat_id"]
    raw_pair  = payload["pair"]
    direction = parse_direction(payload["direction"]) or "CALL"
    expiry    = payload["expiry"]
    strat     = payload["strategy"]
    winrate   = payload["winrate"]
    tf        = norm_interval(payload["timeframe"])
    theme     = norm_theme(payload["theme"])

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

    # Snapshot fallback attempts
    try:
        node_start_browser()
        png, ex_used = fetch_snapshot_png_any(ex, tk, tf, theme, "chart", alt)
        tg_api_send_photo_bytes(chat_id, png, caption=f"{ex_used}:{tk} • TF {tf} • {theme}")
    except Exception as e:
        logger.error("TV snapshot error for %s:%s -> %s", ex, tk, e)
        tg_api_send_message(chat_id, f"⚠ Chart snapshot failed for {raw_pair}: {e}")

    return {"ok": True}, 200


@flask_app.post("/tv")
def tv_route():
    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:
        logger.error("TV /tv invalid JSON: %s", e)
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    body, code = _handle_tv_alert(data)
    return jsonify(body), code


# optional alias
@flask_app.route("/webhook", methods=["POST"])
def tv_route_alias():
    return tv_route()


def start_flask_background():
    threading.Thread(
        target=lambda: flask_app.run(
            host="0.0.0.0", port=TV_WEBHOOK_PORT,
            debug=False, use_reloader=False, threaded=True
        ),
        daemon=True,
    ).start()
    logger.info("Flask TV webhook listening on port %s", TV_WEBHOOK_PORT)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    start_flask_background()

    tg_app = ApplicationBuilder().token(TOKEN).build()

    tg_app.add_handler(CommandHandler("start",     cmd_start))
    tg_app.add_handler(CommandHandler("help",      cmd_help))
    tg_app.add_handler(CommandHandler("pairs",     cmd_pairs))
    tg_app.add_handler(CommandHandler("snap",      cmd_snap))
    tg_app.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    tg_app.add_handler(CommandHandler("snapall",   cmd_snapall))
    tg_app.add_handler(CommandHandler("trade",     cmd_trade))
    tg_app.add_handler(CommandHandler("next",      cmd_next))

    # Inline callback
    tg_app.add_handler(CallbackQueryHandler(on_callback))

    # Fallback handlers
    tg_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    tg_app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info("Bot polling… (Default=%s) | Webhook port %s", DEFAULT_EXCHANGE, TV_WEBHOOK_PORT)
    tg_app.run_polling()


if __name__ == "__main__":
    main()
