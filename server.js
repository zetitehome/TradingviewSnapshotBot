#!/usr/bin/env python
"""
TradingView → Telegram Snapshot Bot (Inline-UI Edition)
======================================================

Features
--------
• python-telegram-bot v20+ async API.
• Nice inline keyboard UI (pairs, analyze, trade).
• Trade quick-picks: CALL/PUT @ 1m,3m,5m,15m.
• Snapshot service integration:
    1) /snapshot/<pair>?interval=&theme= (preferred)
    2) /run?exchange=&ticker=&interval=&theme= (fallbacks)
• Multi-exchange fallback chain (CURRENCY, FX, FX_IDC, OANDA, FOREXCOM, IDC, QUOTEX).
• Rate limiting (per chat + global throttle) to avoid hammering backend/Render.
• Retry logic for snapshot fetch.
• Rotating log file (logs/tvsnapshotbot.log).
• TradingView webhook endpoints (/tv, /webhook) – send alert text + chart to Telegram.
• Text parser: "trade eur/usd call 5m" triggers snapshot & trade message.
• Pocket Option / OTC mapping -> underlying real-market symbols.

Test Flow
---------
1. Run your Node snapshot service (server.js) locally (default :10000) or via Render.
2. Export SNAPSHOT_BASE_URL to point at it.
3. Run this bot. Talk to bot in Telegram (`/start`).
4. Try inline pair selection via `/pairs`.
5. Send a manual TV alert POST to http://localhost:8081/tv (or exposed tunnel).

NOTE
----
No trading is executed – "trade" just posts formatted signals + chart snapshots.

"""

# ----------------------------------------------------------------------
# Standard imports
# ----------------------------------------------------------------------
import os
import io
import re
import sys
import time
import json
import queue
import html
import enum
import asyncio
import logging
import threading
import traceback
from typing import List, Tuple, Dict, Optional

# ----------------------------------------------------------------------
# Third-party imports
# ----------------------------------------------------------------------
import requests
from flask import Flask, request, jsonify

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    constants as tg_consts,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from logging.handlers import RotatingFileHandler


# ======================================================================
# Logging setup
# ======================================================================
os.makedirs("logs", exist_ok=True)
LOG_PATH = "logs/tvsnapshotbot.log"
_log_handler = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[_log_handler, logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("TVSnapBot")


# ======================================================================
# Environment / Config
# ======================================================================
TOKEN            = os.environ.get("TELEGRAM_BOT_TOKEN")
DEFAULT_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
BASE_URL         = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000")
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "CURRENCY")
DEFAULT_INTERVAL = os.environ.get("DEFAULT_INTERVAL", "1")
DEFAULT_THEME    = os.environ.get("DEFAULT_THEME", "dark")
TV_WEBHOOK_PORT  = int(os.environ.get("TV_WEBHOOK_PORT", "8081"))
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET")  # optional

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment.")

# HTTP session reuse
_http = requests.Session()
_http.headers.update({"User-Agent": "TVSnapBot/1.0"})


# ======================================================================
# Rate limiting
# ======================================================================
LAST_SNAPSHOT_PER_CHAT: Dict[int, float] = {}
RATE_LIMIT_SECONDS = 3.0  # per chat
GLOBAL_LAST_SNAPSHOT = 0.0
GLOBAL_MIN_GAP = 0.75     # seconds between *any* snapshots

def rate_limited(chat_id: int) -> bool:
    """Return True if per-chat rate-limit exceeded."""
    now = time.time()
    last = LAST_SNAPSHOT_PER_CHAT.get(chat_id, 0.0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    LAST_SNAPSHOT_PER_CHAT[chat_id] = now
    return False

def global_throttle_wait():
    """Sleep if last snapshot fired too recently (global)."""
    global GLOBAL_LAST_SNAPSHOT
    now = time.time()
    gap = now - GLOBAL_LAST_SNAPSHOT
    if gap < GLOBAL_MIN_GAP:
        time.sleep(GLOBAL_MIN_GAP - gap)
    GLOBAL_LAST_SNAPSHOT = time.time()


# ======================================================================
# Pairs / Mapping
# ======================================================================
# Display strings exactly as user sees
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


# Primary feed mapping for display pairs
PAIR_MAP: Dict[str, Tuple[str, str]] = {}
for _p in FX_PAIRS:
    PAIR_MAP[_canon_key(_p)] = (DEFAULT_EXCHANGE, _p.replace("/", ""))

# OTC underlying -> QUOTEX feed (falls through to fallback chain if unavailable)
_underlying_otc = {
    "EUR/USD-OTC":"EURUSD","GBP/USD-OTC":"GBPUSD","USD/JPY-OTC":"USDJPY",
    "USD/CHF-OTC":"USDCHF","AUD/USD-OTC":"AUDUSD","NZD/USD-OTC":"NZDUSD",
    "USD/CAD-OTC":"USDCAD","EUR/GBP-OTC":"EURGBP","EUR/JPY-OTC":"EURJPY",
    "GBP/JPY-OTC":"GBPJPY","AUD/CHF-OTC":"AUDCHF","EUR/CHF-OTC":"EURCHF",
    "KES/USD-OTC":"USDKES","MAD/USD-OTC":"USDMAD","USD/BDT-OTC":"USDBDT",
    "USD/MXN-OTC":"USDMXN","USD/MYR-OTC":"USDMYR","USD/PKR-OTC":"USDPKR",
}
for _p, _tk in _underlying_otc.items():
    PAIR_MAP[_canon_key(_p)] = ("QUOTEX", _tk)


# Known fallback exchange order (uppercase)
KNOWN_FX_EXCHANGES = ["CURRENCY", "FX", "FX_IDC", "OANDA", "FOREXCOM", "IDC", "QUOTEX"]


# ======================================================================
# Normalization helpers
# ======================================================================
def norm_interval(tf: str) -> str:
    """
    Convert textual timeframe to plugin param.
    Minutes: '1', '5', '15'
    '1m' -> '1', '1h' -> '60', 'd' -> 'D', etc.
    """
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
    if not val:
        return DEFAULT_THEME
    return "light" if val.lower().startswith("l") else "dark"


# ======================================================================
# Direction helpers (binary-friendly)
# ======================================================================
_CALL_WORDS = {"CALL","BUY","UP","LONG","BULL","GREEN"}
_PUT_WORDS  = {"PUT","SELL","DOWN","SHORT","BEAR","RED"}

def parse_direction(word: Optional[str]) -> Optional[str]:
    if not word:
        return None
    w = word.strip().upper()
    if w in _CALL_WORDS:
        return "CALL"
    if w in _PUT_WORDS:
        return "PUT"
    return None


# ======================================================================
# Resolve symbol
# ======================================================================
def resolve_symbol(raw: str) -> Tuple[str, str, bool, List[str]]:
    """
    Return (exchange, ticker, is_otc, alt_exchange_list)
    alt_exchange_list used for fallback attempts.
    """
    if not raw:
        return DEFAULT_EXCHANGE, "EURUSD", False, []

    s = raw.strip().upper()
    is_otc = "-OTC" in s

    # explicit EX:TK
    if ":" in s:
        ex, tk = s.split(":", 1)
        alt = [x for x in KNOWN_FX_EXCHANGES if x != ex.upper()]
        return ex.upper(), tk.upper(), is_otc, alt

    key = _canon_key(s)
    if key in PAIR_MAP:
        ex, tk = PAIR_MAP[key]
        exu = ex.upper()
        alt = [x for x in KNOWN_FX_EXCHANGES if x != exu]
        return exu, tk.upper(), is_otc, alt

    # fallback guess: strip non-alnum
    tk = re.sub(r"[^A-Z0-9]", "", s)
    alt = [x for x in KNOWN_FX_EXCHANGES if x != DEFAULT_EXCHANGE.upper()]
    return DEFAULT_EXCHANGE.upper(), tk.upper(), is_otc, alt


# ======================================================================
# Snapshot service helpers
# ======================================================================
def _safe_get(url: str, timeout: float = 75.0) -> requests.Response:
    """GET wrapper with log + global throttle."""
    global_throttle_wait()
    r = _http.get(url, timeout=timeout)
    return r


def ping_start_browser():
    """Ping Node /start-browser to warm Chromium, ignore errors."""
    try:
        url = f"{BASE_URL}/start-browser"
        _safe_get(url, timeout=10.0)
    except Exception as e:
        logger.warning("start-browser ping error: %s", e)


def attempt_snapshot_snapshot_endpoint(pair: str, interval: str, theme: str) -> Tuple[bool, Optional[bytes], str]:
    """
    Try new /snapshot/<pair> endpoint.
    pair is canonical (EURUSD, or slash names allowed – server may parse).
    Returns (success, bytes, err_msg).
    """
    from urllib.parse import quote
    qp = quote(pair, safe="")
    url = f"{BASE_URL}/snapshot/{qp}?interval={interval}&theme={theme}"
    try:
        r = _safe_get(url)
        ct = r.headers.get("Content-Type", "")
        if r.status_code == 200 and ct.startswith("image"):
            return True, r.content, ""
        return False, None, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, None, str(e)


def attempt_snapshot_run_endpoint(ex: str, tk: str, interval: str, theme: str) -> Tuple[bool, Optional[bytes], str]:
    """
    Fallback /run?exchange=&ticker=...
    """
    url = f"{BASE_URL}/run?exchange={ex}&ticker={tk}&interval={interval}&theme={theme}"
    try:
        r = _safe_get(url)
        ct = r.headers.get("Content-Type", "")
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
    base_pair: Optional[str] = None,
    alt_exchanges: Optional[List[str]] = None,
    retries: int = 3,
    retry_wait: float = 2.5,
) -> Tuple[bytes, str]:
    """
    Attempt snapshot:
        1) /snapshot/<pair> if base_pair provided
        2) /run primary_ex
        3) /run alt_exchanges
        4) /run known list
    Raises RuntimeError if all fail.
    """
    last_err = None
    tried: List[str] = []

    # 1) /snapshot/<pair>
    if base_pair:
        for n in range(1, retries + 1):
            ok, png, err = attempt_snapshot_snapshot_endpoint(base_pair, interval, theme)
            tried.append(f"SNAPSHOT:{base_pair}")
            if ok and png:
                logger.info("Snapshot success /snapshot/%s (%d bytes)", base_pair, len(png))
                return png, f"SNAPSHOT:{base_pair}"
            last_err = err
            logger.warning("Snapshot /snapshot/%s attempt %d failed: %s", base_pair, n, err)
            time.sleep(retry_wait)

    # 2+) build chain for /run fallback
    chain = [primary_ex.upper()]
    if alt_exchanges:
        chain.extend([x.upper() for x in alt_exchanges])
    for x in KNOWN_FX_EXCHANGES:
        if x not in chain:
            chain.append(x)

    for ex in chain:
        for n in range(1, retries + 1):
            ok, png, err = attempt_snapshot_run_endpoint(ex, tk, interval, theme)
            tried.append(f"{ex}")
            if ok and png:
                logger.info("Snapshot success %s:%s (%d bytes)", ex, tk, len(png))
                return png, ex
            last_err = err
            logger.warning("Snapshot %s:%s attempt %d failed: %s", ex, tk, n, err)
            time.sleep(retry_wait)

    raise RuntimeError(f"All exchanges failed for {tk}. Last error: {last_err}. Tried: {tried}")


# ======================================================================
# Telegram sending helpers (async)
# ======================================================================
async def send_snapshot_photo(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    exchange: str,
    ticker: str,
    interval: str,
    theme: str,
    prefix: str = "",
    base_pair: Optional[str] = None,
    alt_exchanges: Optional[List[str]] = None,
):
    """Fetch snapshot (thread) + send photo."""
    if rate_limited(chat_id):
        await context.bot.send_message(chat_id, "⏳ Too many requests; wait a few seconds…")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    await asyncio.to_thread(ping_start_browser)

    try:
        png, source_used = await asyncio.to_thread(
            fetch_snapshot_png_any, exchange, ticker, interval, theme, base_pair, alt_exchanges
        )
        caption = f"{prefix}{source_used}:{ticker} • TF {interval} • {theme}"
        await context.bot.send_photo(chat_id=chat_id, photo=png, caption=caption)
    except Exception as e:
        logger.exception("snapshot photo error")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Failed: {exchange}:{ticker} ({e})")


def _build_media_items_sync(
    pairs: List[Tuple[str, str, str, List[str]]],
    interval: str,
    theme: str,
    prefix: str,
) -> List[InputMediaPhoto]:
    """Blocking build; used via to_thread for /snapmulti & /snapall."""
    out: List[InputMediaPhoto] = []
    for ex, tk, label, alt in pairs:
        try:
            png, source_used = fetch_snapshot_png_any(ex, tk, interval, theme, base_pair=label, alt_exchanges=alt)
            bio = io.BytesIO(png)
            bio.name = "chart.png"
            cap = f"{prefix}{source_used}:{tk} • {label} • TF {interval} • {theme}"
            out.append(InputMediaPhoto(media=bio, caption=cap))
        except Exception as e:
            logger.warning("Media build fail %s:%s %s", ex, tk, e)
    return out


async def send_media_group_chunked(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    media_items: List[InputMediaPhoto],
    chunk_size: int = 5,
):
    for i in range(0, len(media_items), chunk_size):
        chunk = media_items[i : i + chunk_size]
        if not chunk:
            continue
        # Only first caption shows reliably
        for m in chunk[1:]:
            m.caption = None
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(1.0)


# ======================================================================
# Parse args for /snap, /snapmulti, /trade
# ======================================================================
def parse_snap_args(args: List[str]) -> Tuple[str, str, str, str, List[str], str]:
    """
    /snap SYMBOL [interval] [theme]
    Returns (exchange, ticker, interval, theme, alt_list, base_pair_display)
    """
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
    return ex, tk, norm_interval(tf), norm_theme(th), alt, symbol


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
    /trade SYMBOL CALL|PUT expiry theme
    expiry is returned raw string (1m/3m/5m/15m).
    """
    if not args:
        return "EUR/USD", "CALL", "5m", DEFAULT_THEME
    symbol = args[0]
    direction = parse_direction(args[1] if len(args) >= 2 else None) or "CALL"
    expiry = args[2] if len(args) >= 3 else "5m"
    theme = args[3] if len(args) >= 4 else DEFAULT_THEME
    return symbol, direction, expiry, theme


# ======================================================================
# Inline keyboard builders
# ======================================================================
# Callback data protocol: ACTION|arg1|arg2|...
# Keep under 64 bytes recommended.

def kb_main_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📷 Snap", callback_data="OPEN_SNAP")],
        [InlineKeyboardButton("📊 Pairs", callback_data="OPEN_PAIRS")],
        [InlineKeyboardButton("📈 Trade", callback_data="OPEN_TRADE")],
        [InlineKeyboardButton("🕒 Next Signal", callback_data="OPEN_NEXT")],
        [InlineKeyboardButton("ℹ Help", callback_data="OPEN_HELP")],
    ]
    return InlineKeyboardMarkup(buttons)


def kb_pairs_category() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("FX Pairs",  callback_data="LIST_FX|0")],
        [InlineKeyboardButton("OTC Pairs", callback_data="LIST_OTC|0")],
        [InlineKeyboardButton("⬅ Back",    callback_data="BACK_MAIN")],
    ])


def _kb_pairs_list(pairs: List[str], page: int, action_prefix: str, page_size: int = 10) -> InlineKeyboardMarkup:
    start = page * page_size
    sl = pairs[start : start + page_size]
    rows = []
    for p in sl:
        rows.append([InlineKeyboardButton(p, callback_data=f"{action_prefix}_PAIR|{p}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"{action_prefix}|{page-1}"))
    if start + page_size < len(pairs):
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"{action_prefix}|{page+1}"))
    rows.append(nav or [InlineKeyboardButton("—", callback_data="IGNORE")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="OPEN_PAIRS")])
    return InlineKeyboardMarkup(rows)


def kb_pair_actions(pair: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Analyze", callback_data=f"SNAP_PAIR|{pair}|1|dark")],
        [InlineKeyboardButton("📷 Snapshot TF?", callback_data=f"SNAP_SELECT_TF|{pair}")],
        [InlineKeyboardButton("🎯 Trade", callback_data=f"TRADE_PAIR|{pair}")],
        [InlineKeyboardButton("⬅ Back", callback_data="OPEN_PAIRS")],
    ])


def kb_snap_select_tf(pair: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1m",  callback_data=f"SNAP_PAIR|{pair}|1|dark"),
            InlineKeyboardButton("5m",  callback_data=f"SNAP_PAIR|{pair}|5|dark"),
            InlineKeyboardButton("15m", callback_data=f"SNAP_PAIR|{pair}|15|dark"),
        ],
        [
            InlineKeyboardButton("1H", callback_data=f"SNAP_PAIR|{pair}|60|dark"),
            InlineKeyboardButton("4H", callback_data=f"SNAP_PAIR|{pair}|240|dark"),
        ],
        [
            InlineKeyboardButton("Daily", callback_data=f"SNAP_PAIR|{pair}|D|dark"),
            InlineKeyboardButton("Weekly", callback_data=f"SNAP_PAIR|{pair}|W|dark"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data=f"PAIR_ACT|{pair}")],
    ])


def kb_trade_expiry(pair: str) -> InlineKeyboardMarkup:
    # 1m,3m,5m,15m for CALL & PUT
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("CALL 1m",  callback_data=f"TRADE_DO|{pair}|CALL|1m"),
            InlineKeyboardButton("PUT 1m",   callback_data=f"TRADE_DO|{pair}|PUT|1m"),
        ],
        [
            InlineKeyboardButton("CALL 3m",  callback_data=f"TRADE_DO|{pair}|CALL|3m"),
            InlineKeyboardButton("PUT 3m",   callback_data=f"TRADE_DO|{pair}|PUT|3m"),
        ],
        [
            InlineKeyboardButton("CALL 5m",  callback_data=f"TRADE_DO|{pair}|CALL|5m"),
            InlineKeyboardButton("PUT 5m",   callback_data=f"TRADE_DO|{pair}|PUT|5m"),
        ],
        [
            InlineKeyboardButton("CALL 15m", callback_data=f"TRADE_DO|{pair}|CALL|15m"),
            InlineKeyboardButton("PUT 15m",  callback_data=f"TRADE_DO|{pair}|PUT|15m"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data=f"PAIR_ACT|{pair}")],
    ])


# ======================================================================
# Command Handlers
# ======================================================================
HELP_TEXT = (
    "📘 *Trading Bot Help*\n\n"
    "*/snap* SYMBOL [interval] [theme]\n"
    "*/trade* SYMBOL CALL|PUT [expiry] [theme]\n"
    "*/snapmulti* S1 S2 ... [interval] [theme]\n"
    "*/snapall* (all FX+OTC)\n"
    "*/pairs* list supported names\n"
    "*/next* watch for next signal (from TV alerts)\n\n"
    "_Intervals:_ minutes (#), D, W, M.\n"
    "_Themes:_ dark|light.\n"
)

START_TEXT = (
    "👋 Welcome!\n\n"
    "I'm your TradingView Snapshot Bot (Pocket Option / Binary friendly).\n"
    "Use the buttons below or type /help.\n"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        update.effective_chat.id,
        START_TEXT,
        reply_markup=kb_main_menu(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        update.effective_chat.id,
        HELP_TEXT,
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        update.effective_chat.id,
        "Select category:",
        reply_markup=kb_pairs_category(),
    )


async def cmd_snap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ex, tk, tf, th, alt, disp = parse_snap_args(context.args)
    await send_snapshot_photo(
        update.effective_chat.id,
        context,
        ex,
        tk,
        tf,
        th,
        prefix="[SNAP] ",
        base_pair=disp,
        alt_exchanges=alt,
    )


async def cmd_snapmulti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs, tf, th = parse_multi_args(context.args)
    if not pairs:
        await context.bot.send_message(
            update.effective_chat.id,
            "Usage: /snapmulti SYM1 SYM2 ... [interval] [theme]",
        )
        return

    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, f"📸 Capturing {len(pairs)} charts…")

    p_trip: List[Tuple[str, str, str, List[str]]] = []
    for p in pairs:
        ex, tk, _is_otc, alt = resolve_symbol(p)
        p_trip.append((ex, tk, p, alt))

    media_items = await asyncio.to_thread(
        _build_media_items_sync, p_trip, tf, th, prefix="[MULTI] "
    )
    if not media_items:
        await context.bot.send_message(chat_id, "❌ No charts captured.")
        return
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)


async def cmd_snapall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, f"⚡ Capturing all {len(ALL_PAIRS)} pairs… this may take a while.")

    p_trip: List[Tuple[str, str, str, List[str]]] = []
    for p in ALL_PAIRS:
        ex, tk, _is_otc, alt = resolve_symbol(p)
        p_trip.append((ex, tk, p, alt))

    media_items = await asyncio.to_thread(
        _build_media_items_sync, p_trip, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[ALL] "
    )
    if not media_items:
        await context.bot.send_message(chat_id, "❌ No charts captured.")
        return
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)


async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol, direction, expiry, theme = parse_trade_args(context.args)
    ex, tk, _is_otc, alt = resolve_symbol(symbol)
    tf = norm_interval(DEFAULT_INTERVAL)
    th = norm_theme(theme)
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    msg = f"{arrow} *{symbol}* {direction}  Expiry: {expiry}"
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN)
    await send_snapshot_photo(
        update.effective_chat.id,
        context,
        ex,
        tk,
        tf,
        th,
        prefix="[TRADE] ",
        base_pair=symbol,
        alt_exchanges=alt,
    )


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        update.effective_chat.id,
        "👀 Watching for next signal (placeholder – wire up TradingView alerts to /tv).",
    )


# ======================================================================
# Callback query handler (inline buttons)
# ======================================================================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    chat_id = q.message.chat_id if q.message else DEFAULT_CHAT_ID

    try:
        if data == "OPEN_HELP":
            await q.edit_message_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main_menu())
            return

        if data == "OPEN_SNAP":
            await q.edit_message_text("Enter /snap SYMBOL interval theme\nor select /pairs.", reply_markup=kb_main_menu())
            return

        if data == "OPEN_PAIRS":
            await q.edit_message_text("Select category:", reply_markup=kb_pairs_category())
            return

        if data == "OPEN_TRADE":
            await q.edit_message_text("Pick a pair to trade:", reply_markup=kb_pairs_category())
            return

        if data == "OPEN_NEXT":
            await q.edit_message_text("Use /next to enable watch mode (coming soon).", reply_markup=kb_main_menu())
            return

        if data == "BACK_MAIN":
            await q.edit_message_text(START_TEXT, reply_markup=kb_main_menu())
            return

        # paginated lists
        if data.startswith("LIST_FX|"):
            page = int(data.split("|", 1)[1])
            await q.edit_message_text("FX Pairs:", reply_markup=_kb_pairs_list(FX_PAIRS, page, "FX"))
            return

        if data.startswith("LIST_OTC|"):
            page = int(data.split("|", 1)[1])
            await q.edit_message_text("OTC Pairs:", reply_markup=_kb_pairs_list(OTC_PAIRS, page, "OTC"))
            return

        # user picked a pair from FX / OTC lists
        if data.startswith("FX_PAIR|") or data.startswith("OTC_PAIR|"):
            pair = data.split("|", 1)[1]
            await q.edit_message_text(f"Actions for {pair}:", reply_markup=kb_pair_actions(pair))
            return

        if data.startswith("PAIR_ACT|"):
            pair = data.split("|", 1)[1]
            await q.edit_message_text(f"Actions for {pair}:", reply_markup=kb_pair_actions(pair))
            return

        if data.startswith("SNAP_SELECT_TF|"):
            pair = data.split("|", 1)[1]
            await q.edit_message_text(f"Select timeframe for {pair}:", reply_markup=kb_snap_select_tf(pair))
            return

        if data.startswith("SNAP_PAIR|"):
            # SNAP_PAIR|PAIR|interval|theme
            _parts = data.split("|")
            if len(_parts) >= 4:
                pair = _parts[1]
                interval = _parts[2]
                theme = _parts[3]
            else:
                # fallback defaults
                pair = _parts[1] if len(_parts) > 1 else "EUR/USD"
                interval = "1"
                theme = "dark"

            ex, tk, _is_otc, alt = resolve_symbol(pair)
            tf = norm_interval(interval)
            th = norm_theme(theme)
            await q.edit_message_text(f"Fetching {pair} ({tf})…")
            await send_snapshot_photo(chat_id, context, ex, tk, tf, th, prefix="[SNAP] ", base_pair=pair, alt_exchanges=alt)
            return

        if data.startswith("TRADE_PAIR|"):
            pair = data.split("|", 1)[1]
            await q.edit_message_text(f"Trade {pair}: choose type/expiry", reply_markup=kb_trade_expiry(pair))
            return

        if data.startswith("TRADE_DO|"):
            # TRADE_DO|pair|CALL|5m
            _parts = data.split("|")
            pair = _parts[1] if len(_parts) > 1 else "EUR/USD"
            direction = parse_direction(_parts[2] if len(_parts) > 2 else None) or "CALL"
            expiry = _parts[3] if len(_parts) > 3 else "5m"
            ex, tk, _is_otc, alt = resolve_symbol(pair)
            tf = norm_interval(DEFAULT_INTERVAL)
            th = norm_theme(DEFAULT_THEME)
            arrow = "🟢↑" if direction == "CALL" else "🔴↓"
            await q.edit_message_text(f"{arrow} {pair} {direction} Exp: {expiry}")
            await send_snapshot_photo(chat_id, context, ex, tk, tf, th, prefix="[TRADE] ", base_pair=pair, alt_exchanges=alt)
            return

        if data == "IGNORE":
            # do nothing
            return

        # fallback
        await q.edit_message_text("Unknown selection.", reply_markup=kb_main_menu())

    except Exception as e:
        logger.exception("callback error: %s", e)
        try:
            await q.edit_message_text(f"Error: {e}", reply_markup=kb_main_menu())
        except Exception:
            pass


# ======================================================================
# Text message parser (non-command)
# ======================================================================
_trade_re = re.compile(r"(?i)\btrade\s+([A-Z/\-]+)\s+(call|put|buy|sell|up|down)\s+([0-9]+m?)")

async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
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
        await send_snapshot_photo(
            update.effective_chat.id,
            context,
            ex,
            tk,
            DEFAULT_INTERVAL,
            DEFAULT_THEME,
            prefix="[TRADE] ",
            base_pair=symbol,
            alt_exchanges=alt,
        )
        return

    await context.bot.send_message(
        update.effective_chat.id,
        f"You said: {txt}\nTry typing: trade EUR/USD call 5m",
    )


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, "❌ Unknown command. Try /help.")


# ======================================================================
# TradingView Webhook (Flask) -> Telegram
# ======================================================================
flask_app = Flask(__name__)

def _parse_tv_payload(data: dict) -> Dict[str, str]:
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


def tg_api_send_message(chat_id: str, text: str, parse_mode: Optional[str] = None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        _http.post(url, json=payload, timeout=30)
    except Exception as e:
        logger.error("tg_api_send_message: %s", e)


def tg_api_send_photo_bytes(chat_id: str, png: bytes, caption: str = ""):
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", png, "image/png")}
    data = {"chat_id": chat_id, "caption": caption}
    try:
        _http.post(url, data=data, files=files, timeout=60)
    except Exception as e:
        logger.error("tg_api_send_photo_bytes: %s", e)


def _handle_tv_alert(data: dict):
    """
    Process a TradingView alert payload synchronously (Flask thread).
    Accept both header-based and body-based secrets.
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

    # Attempt chart w/ fallback
    try:
        ping_start_browser()
        png, source_used = fetch_snapshot_png_any(ex, tk, tf, theme, base_pair=raw_pair, alt_exchanges=alt)
        tg_api_send_photo_bytes(chat_id, png, caption=f"{source_used}:{tk} • TF {tf} • {theme}")
    except Exception as e:
        logger.error("TV snapshot error for %s:%s -> %s", ex, tk, e)
        tg_api_send_message(chat_id, f"⚠ Chart snapshot failed for {raw_pair}: {e}")

    return {"ok": True}, 200


@flask_app.post("/tv")
def tv_route():
    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:
        logger.error("/tv invalid JSON: %s", e)
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    body, code = _handle_tv_alert(data)
    return jsonify(body), code


# Compatibility alias
@flask_app.post("/webhook")
def tv_route_alias():
    return tv_route()


def start_flask_background():
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


# ======================================================================
# Application start
# ======================================================================
def main():
    start_flask_background()

    app = ApplicationBuilder().token(TOKEN).build()

    # Core commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pairs", cmd_pairs))
    app.add_handler(CommandHandler("snap", cmd_snap))
    app.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    app.add_handler(CommandHandler("snapall", cmd_snapall))
    app.add_handler(CommandHandler("trade", cmd_trade))
    app.add_handler(CommandHandler("next", cmd_next))

    # Inline callbacks
    app.add_handler(CallbackQueryHandler(on_callback))

    # Text + unknown
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info("Bot polling… (Default=%s) | Webhook port %s", DEFAULT_EXCHANGE, TV_WEBHOOK_PORT)
    app.run_polling(close_loop=False)  # do not close loop if embedding


if __name__ == "__main__":
    main()
