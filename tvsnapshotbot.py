#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingView → Telegram Snapshot Bot (Inline + Trade Sizing Edition)
===================================================================
Key Features
------------
• Async python-telegram-bot (PTB ≥20 required).
• Inline keyboards for pair selection, expiry (1m/3m/5m/15m), trade size ($ or %),
  theme (dark/light), and direction (CALL/PUT).
• Large symbol universe: Majors FX, OTC equivalents, crypto, indices.
• Persistent per-user settings saved to JSON file (pair, expiry, theme, size mode).
• Snapshot backend: Node/Puppeteer service with /snapshot/:pair OR /run style endpoints.
  - Accepts PNG from HTTP 200.
  - If HTTP != 200 but response is PNG (500 mis‑set headers cases), still use image.
  - Multi‑exchange fallback cascade.
• Rate limiting: per chat + global throttle.
• TradingView webhook (Flask) → Telegram alert + snapshot.
• Optional Pocket Option / UI.Vision trigger placeholders (user chooses manual vs auto).
• Safe logging: never writes bot token; binary response bodies truncated/escaped.

This file is intentionally verbose for clarity & extensibility.
"""

# ---------------------------------------------------------------------------
# Standard Lib Imports
# ---------------------------------------------------------------------------
import asyncio
import io
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, asdict, field
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Third-Party Imports
# ---------------------------------------------------------------------------
import requests
import httpx
from flask import Flask, request, jsonify

# python-telegram-bot
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

try:
    # available in PTB ≥20
    from telegram import __version_info__ as PTB_VERSION_INFO
except Exception:  # pragma: no cover
    PTB_VERSION_INFO = (0, 0, 0, "unknown")

# ---------------------------------------------------------------------------
# Globals: Environment Config
# ---------------------------------------------------------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BASE_URL = os.getenv("SNAPSHOT_BASE_URL", "http://localhost:10000")  # Node screenshot service
DEFAULT_EXCHANGE = os.getenv("DEFAULT_EXCHANGE", "FX")
DEFAULT_INTERVAL = os.getenv("DEFAULT_INTERVAL", "1")  # minutes
DEFAULT_THEME = os.getenv("DEFAULT_THEME", "dark")
TV_WEBHOOK_PORT = int(os.getenv("TV_WEBHOOK_PORT", "8081"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # optional
UI_VISION_URL = os.getenv("UI_VISION_URL")  # optional external automation trigger
AUTO_TRADE_FROM_TV = os.getenv("AUTO_TRADE_FROM_TV", "false").lower() in ("1", "true", "yes")
SIM_DEBIT = os.getenv("SIM_DEBIT", "false").lower() in ("1", "true", "yes")

STATE_FILE = "bot_state.json"
LOG_FILE = "logs/tvsnapshotbot.log"

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment.")

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
os.makedirs("logs", exist_ok=True)

logger = logging.getLogger("TVSnapBot")
logger.setLevel(logging.INFO)

# File log (rotating)
_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
)
logger.addHandler(_file_handler)

# Console log
_console = logging.StreamHandler()
_console.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
)
logger.addHandler(_console)

# Avoid leaking token in urllib3 debug if enabled
logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("urllib3").setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
_http = requests.Session()  # sync reuse for snapshots + raw telegram calls

def _safe_trunc(value: Union[str, bytes], max_len: int = 200) -> str:
    """Return safe unicode preview for logs; binary -> hex length only."""
    if isinstance(value, bytes):
        if len(value) > max_len:
            return f"<{len(value)} bytes>"
        try:
            return value.decode("utf-8", "replace")
        except Exception:  # pragma: no cover
            return f"<{len(value)} bytes bin>"
    if len(value) <= max_len:
        return value
    return value[:max_len] + "..."

def _safe_env(val: str) -> str:
    """Mask env values like tokens."""
    if not val:
        return ""
    if len(val) <= 6:
        return "*" * len(val)
    return val[:4] + "..." + val[-2:]

# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------
LAST_SNAPSHOT_PER_CHAT: Dict[int, float] = {}
RATE_LIMIT_SECONDS = 3
GLOBAL_LAST_SNAPSHOT = 0.0
GLOBAL_MIN_GAP = 0.75  # seconds between any 2 backend calls

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
# Symbol Universe
# ---------------------------------------------------------------------------
FX_PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD",
    "NZD/USD", "USD/CAD", "EUR/GBP", "EUR/JPY", "GBP/JPY",
    "AUD/JPY", "NZD/JPY", "EUR/AUD", "GBP/AUD", "EUR/CAD",
    "USD/MXN", "USD/TRY", "USD/ZAR", "AUD/CHF", "EUR/CHF",
]

OTC_PAIRS = [
    "EUR/USD-OTC", "GBP/USD-OTC", "USD/JPY-OTC", "USD/CHF-OTC", "AUD/USD-OTC",
    "NZD/USD-OTC", "USD/CAD-OTC", "EUR/GBP-OTC", "EUR/JPY-OTC", "GBP/JPY-OTC",
    "AUD/CHF-OTC", "EUR/CHF-OTC", "KES/USD-OTC", "MAD/USD-OTC",
    "USD/BDT-OTC", "USD/MXN-OTC", "USD/MYR-OTC", "USD/PKR-OTC",
]

CRYPTO_PAIRS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD",
]

INDICES = [
    "US30", "US500", "NAS100", "DAX40", "UK100",
]

ALL_PAIRS = FX_PAIRS + OTC_PAIRS + CRYPTO_PAIRS + INDICES

# canonicalize key for mapping
def _canon_key(pair: str) -> str:
    return pair.strip().upper().replace(" ", "").replace("/", "")

# build mapping (exchange, ticker, alt_exchanges)
# alt_exchanges: fallback list to try if primary fails
PAIR_MAP: Dict[str, Tuple[str, str, List[str]]] = {}

# map majors to DEFAULT_EXCHANGE, with fallback common FX data vendors
FX_FALLBACKS = ["FX", "FX_IDC", "OANDA", "FOREXCOM", "IDC"]
for p in FX_PAIRS:
    PAIR_MAP[_canon_key(p)] = (DEFAULT_EXCHANGE, p.replace("/", ""), FX_FALLBACKS)

# OTC underlying -> we’ll pull standard market data symbol, but mark alt fallback
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
    PAIR_MAP[_canon_key(p)] = ("FX", tk, FX_FALLBACKS)

# Crypto – TradingView uses BITSTAMP or CRYPTOCAP or BINANCE; we try cascade
CRYPTO_FALLBACKS = ["BINANCE", "COINBASE", "BITSTAMP", "KRAKEN", "BYBIT"]
_crypto_map = {
    "BTC/USD": "BTCUSDT",
    "ETH/USD": "ETHUSDT",
    "SOL/USD": "SOLUSDT",
    "XRP/USD": "XRPUSDT",
    "DOGE/USD": "DOGEUSDT",
}
for p, tk in _crypto_map.items():
    PAIR_MAP[_canon_key(p)] = ("BINANCE", tk, CRYPTO_FALLBACKS)

# Indices – common tickers; adjust as needed
INDEX_FALLBACKS = ["TVC", "CURRENCYCOM", "OANDA", "IDC"]
_index_map = {
    "US30": "DJI",
    "US500": "SPX",
    "NAS100": "NDX",
    "DAX40": "DAX",
    "UK100": "FTSE",
}
for p, tk in _index_map.items():
    PAIR_MAP[_canon_key(p)] = ("TVC", tk, INDEX_FALLBACKS)

# ---------------------------------------------------------------------------
# Interval / Theme Normalization
# ---------------------------------------------------------------------------
def norm_interval(tf: str) -> str:
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
    if t in ("m", "1m", "mo", "month"):
        return "M"
    if t.isdigit():
        return t
    return DEFAULT_INTERVAL

def norm_theme(val: Optional[str]) -> str:
    if not val:
        return DEFAULT_THEME
    return "light" if val.lower().startswith("l") else "dark"

# ---------------------------------------------------------------------------
# Symbol Resolver
# ---------------------------------------------------------------------------
def resolve_symbol(raw: str) -> Tuple[str, str, bool, List[str]]:
    """
    Returns (exchange, ticker, is_otc, alt_exchanges_list)
    """
    if not raw:
        return DEFAULT_EXCHANGE, "EURUSD", False, FX_FALLBACKS
    s = raw.strip().upper()
    is_otc = "-OTC" in s
    if ":" in s:
        ex, tk = s.split(":", 1)
        return ex, tk, is_otc, []
    key = _canon_key(s)
    if key in PAIR_MAP:
        ex, tk, alt = PAIR_MAP[key]
        return ex, tk, is_otc, alt
    # fallback guess
    tk = re.sub(r"[^A-Z0-9]", "", s)
    return DEFAULT_EXCHANGE, tk, is_otc, FX_FALLBACKS

# ---------------------------------------------------------------------------
# Snapshot Backend Helpers
# ---------------------------------------------------------------------------
def node_healthz() -> bool:
    try:
        r = _http.get(f"{BASE_URL}/healthz", timeout=5)
        return r.status_code == 200
    except Exception:  # pragma: no cover
        return False

def node_start_browser():
    try:
        r = _http.get(f"{BASE_URL}/start-browser", timeout=10)
        logger.debug("start-browser %s %s", r.status_code, _safe_trunc(r.text))
    except Exception as e:  # pragma: no cover
        logger.warning("start-browser failed: %s", e)

def _attempt_snapshot_url(url: str) -> Tuple[bool, Optional[bytes], str]:
    """
    Perform single HTTP GET. Accept PNG from non-200 if header or sniff says PNG.
    """
    try:
        global_throttle_wait()
        r = _http.get(url, timeout=75)
    except Exception as e:
        return False, None, str(e)

    ct = r.headers.get("Content-Type", "").lower()
    body = r.content

    is_png = False
    if ct.startswith("image/png"):
        is_png = True
    elif body.startswith(b"\x89PNG\r\n\x1a\n"):
        is_png = True  # sniff

    if is_png:
        return True, body, ""
    # else failure
    preview = _safe_trunc(body, 120)
    return False, None, f"HTTP {r.status_code}: {preview}"

def fetch_snapshot_png_retry(ex: str, tk: str, interval: str, theme: str, base: str = "chart") -> bytes:
    """
    Try 3 times for a given exchange:ticker.
    """
    last_err = None
    for attempt in range(1, 4):
        url = f"{BASE_URL}/run?base={base}&exchange={ex}&ticker={tk}&interval={interval}&theme={theme}"
        ok, png, err = _attempt_snapshot_url(url)
        if ok and png:
            return png
        last_err = err
        logger.warning("Snapshot %s:%s attempt %d failed: %s", ex, tk, attempt, err)
        time.sleep(2)
    raise RuntimeError(f"Failed after retries: {last_err}")

def fetch_snapshot_png_any(
    primary_ex: str,
    tk: str,
    interval: str,
    theme: str,
    base: str = "chart",
    extra_exchanges: Optional[List[str]] = None,
) -> Tuple[bytes, str]:
    """
    Try primary exchange, then extras.
    """
    tried: List[str] = []
    last_err = None

    merged = [primary_ex.upper()]
    if extra_exchanges:
        merged.extend([x.upper() for x in extra_exchanges])
    # Guarantee we end with DEFAULT_EXCHANGE if not included
    if DEFAULT_EXCHANGE.upper() not in merged:
        merged.append(DEFAULT_EXCHANGE.upper())

    # dedupe order
    seen = set()
    dedup = []
    for x in merged:
        if x not in seen:
            dedup.append(x)
            seen.add(x)

    for ex in dedup:
        tried.append(ex)
        try:
            png = fetch_snapshot_png_retry(ex, tk, interval, theme, base)
            logger.info("Snapshot success %s:%s via %s", ex, tk, ex)
            return png, ex
        except Exception as e:
            last_err = str(e)
            logger.warning("Snapshot failed %s:%s via %s -> %s", ex, tk, ex, e)

    raise RuntimeError(f"All exchanges failed for {tk}. Last error: {last_err}. Tried: {tried}")

# ---------------------------------------------------------------------------
# User Settings Persistence
# ---------------------------------------------------------------------------
@dataclass
class UserSettings:
    pair: str = "EUR/USD"
    expiry: str = "5m"
    theme: str = DEFAULT_THEME
    interval: str = DEFAULT_INTERVAL
    size_mode: str = "$"      # "$" or "%"
    size_value: float = 5.0   # dollars OR percent
    auto_trade: bool = False  # auto execute broker trade?

# state keyed by str(chat_id)
USER_SETTINGS: Dict[str, UserSettings] = {}

def _load_state():
    global USER_SETTINGS
    if not os.path.exists(STATE_FILE):
        logger.info("No state file found; starting fresh.")
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for k, v in raw.get("users", {}).items():
            USER_SETTINGS[k] = UserSettings(**v)
        logger.info("Loaded %d user settings from state.", len(USER_SETTINGS))
    except Exception as e:
        logger.error("Failed to load state: %s", e)

def _save_state():
    try:
        data = {"users": {k: asdict(v) for k, v in USER_SETTINGS.items()}}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:  # pragma: no cover
        logger.error("Failed to save state: %s", e)

def get_user_settings(chat_id: int) -> UserSettings:
    k = str(chat_id)
    if k not in USER_SETTINGS:
        USER_SETTINGS[k] = UserSettings()
    return USER_SETTINGS[k]

# ---------------------------------------------------------------------------
# Inline Keyboards
# ---------------------------------------------------------------------------
# Expiries we support for binary-style trade
EXPIRY_CHOICES = ["1m", "3m", "5m", "15m"]
THEME_CHOICES = ["dark", "light"]
DIRECTION_CHOICES = ["CALL", "PUT"]
TRADE_SIZE_DOLLARS = [1, 5, 10, 25, 50, 100]
TRADE_SIZE_PERCENTS = [1, 5, 10, 25, 50, 100]

# Paged pair selection -------------------------------------------------------
PAIRS_PER_PAGE = 10  # show 10 pairs per page
PAIR_LIST = ALL_PAIRS  # user requested combined list

def _pair_page_count() -> int:
    return (len(PAIR_LIST) + PAIRS_PER_PAGE - 1) // PAIRS_PER_PAGE

def build_pairs_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    total_pages = _pair_page_count()
    start = page * PAIRS_PER_PAGE
    end = min(len(PAIR_LIST), start + PAIRS_PER_PAGE)
    rows: List[List[InlineKeyboardButton]] = []

    for p in PAIR_LIST[start:end]:
        # compress label: show exactly typed
        rows.append([InlineKeyboardButton(p, callback_data=f"pair:{p}")])

    # nav row
    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"pairs_pg:{page-1}"))
    nav.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"pairs_pg:{page+1}"))
    rows.append(nav)
    return InlineKeyboardMarkup(rows)

def build_expiry_keyboard(current: Optional[str] = None) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for e in EXPIRY_CHOICES:
        lab = f"{'✅ ' if e == current else ''}{e}"
        rows.append([InlineKeyboardButton(lab, callback_data=f"expiry:{e}")])
    return InlineKeyboardMarkup(rows)

def build_theme_keyboard(current: Optional[str] = None) -> InlineKeyboardMarkup:
    rows = []
    for t in THEME_CHOICES:
        lab = f"{'✅ ' if t == current else ''}{t}"
        rows.append([InlineKeyboardButton(lab, callback_data=f"theme:{t}")])
    return InlineKeyboardMarkup(rows)

def build_direction_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 CALL", callback_data="direction:CALL"),
            InlineKeyboardButton("🔴 PUT",  callback_data="direction:PUT"),
        ]
    ])

def build_size_mode_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if current_mode == '$' else ''}$ Mode", callback_data="sizemode:$"
            ),
            InlineKeyboardButton(
                f"{'✅ ' if current_mode == '%' else ''}% Mode", callback_data="sizemode:%"
            ),
        ]
    ]
    return InlineKeyboardMarkup(rows)

def build_size_value_keyboard(mode: str, current: float) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if mode == "$":
        for v in TRADE_SIZE_DOLLARS:
            lab = f"{'✅ ' if current == v else ''}${v}"
            rows.append([InlineKeyboardButton(lab, callback_data=f"sizeval:{v}")])
    else:
        for v in TRADE_SIZE_PERCENTS:
            lab = f"{'✅ ' if current == v else ''}{v}%"
            rows.append([InlineKeyboardButton(lab, callback_data=f"sizeval:{v}")])
    return InlineKeyboardMarkup(rows)

# ---------------------------------------------------------------------------
# Command Parsing Helpers
# ---------------------------------------------------------------------------
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
    # /snapmulti SYM1 SYM2 ... [interval] [theme]
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

# parse manual trade messages
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

_trade_re = re.compile(r"(?i)trade\s+([A-Z/\-]+)\s+(call|put|buy|sell|up|down)\s+(\d+m?)")

# ---------------------------------------------------------------------------
# Telegram Send Helpers
# ---------------------------------------------------------------------------
async def send_snapshot_photo(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    exchange: str,
    ticker: str,
    interval: str,
    theme: str,
    prefix: str = "",
    alt_exchanges: Optional[List[str]] = None,
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
    pairs: List[Tuple[str, str, str, List[str]]],
    interval: str,
    theme: str,
    prefix: str,
) -> List[InputMediaPhoto]:
    out: List[InputMediaPhoto] = []
    for ex, tk, lab, alt_list in pairs:
        try:
            png, ex_used = fetch_snapshot_png_any(ex, tk, interval, theme, "chart", alt_list)
            bio = io.BytesIO(png)
            bio.name = "chart.png"
            cap = f"{prefix}{ex_used}:{tk} • {lab} • TF {interval} • {theme}"
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
        # only first caption shows reliably
        if len(chunk) > 1:
            for m in chunk[1:]:
                m.caption = None
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(1.0)

# ---------------------------------------------------------------------------
# Command Handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nm = update.effective_user.first_name if update.effective_user else ""
    await update.message.reply_text(
        f"Hi {nm} 👋\n"
        "I'm your TradingView Snapshot Bot.\n\n"
        "Use /pairs to pick a market, then tap direction to trade.\n"
        "Use /trade to set size.\n"
        "Use /help for full command list."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📘 *Help*\n\n"
        "/pairs – pick market (inline)\n"
        "/trade – set trade size ($ or %)\n"
        "/snap SYMBOL [interval] [theme]\n"
        "/snapmulti S1 S2 ... [interval] [theme]\n"
        "/snapall – (all FX/OTC/Crypto/Indices)\n"
        "/next – watch for next signal (placeholder)\n"
        "/settings – show current user settings\n\n"
        "Intervals: minutes (#), D, W, M.\n"
        "Themes: dark|light.\n",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Choose a pair:", reply_markup=build_pairs_keyboard(0))

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    us = get_user_settings(update.effective_chat.id)
    txt = (
        f"⚙ *Your Settings*\n"
        f"Pair: {us.pair}\n"
        f"Expiry: {us.expiry}\n"
        f"Theme: {us.theme}\n"
        f"Interval: {us.interval}\n"
        f"Size: {us.size_value}{us.size_mode}\n"
        f"Auto-trade: {'ON' if us.auto_trade else 'OFF'}"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)

async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    us = get_user_settings(update.effective_chat.id)
    await update.message.reply_text(
        "Select trade size mode:", reply_markup=build_size_mode_keyboard(us.size_mode)
    )

async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👀 Watching for next signal (placeholder). Connect TradingView alerts to /tv."
    )

async def cmd_snap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ex, tk, tf, th, alt = parse_snap_args(context.args)
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, th, alt_exchanges=alt)

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
    media_items = await asyncio.to_thread(build_media_items_sync, p_trip, tf, th, prefix="[MULTI] ")
    if not media_items:
        await context.bot.send_message(chat_id, "❌ No charts captured.")
        return
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)

async def cmd_snapall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id, f"⚡ Capturing all {len(ALL_PAIRS)} pairs… this may take a while."
    )
    p_trip: List[Tuple[str, str, str, List[str]]] = []
    for p in ALL_PAIRS:
        ex, tk, _is_otc, alt = resolve_symbol(p)
        p_trip.append((ex, tk, p, alt))
    media_items = await asyncio.to_thread(
        build_media_items_sync, p_trip, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[ALL] "
    )
    if not media_items:
        await context.bot.send_message(chat_id, "❌ No charts captured.")
        return
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)

# ---------------------------------------------------------------------------
# Callback Query Handlers (inline keyboards)
# ---------------------------------------------------------------------------
async def cb_pairs_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, pg = query.data.split(":")
    page = int(pg)
    await query.edit_message_reply_markup(reply_markup=build_pairs_keyboard(page))

async def cb_pair_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, pair = query.data.split(":", 1)
    chat_id = query.message.chat_id
    us = get_user_settings(chat_id)
    us.pair = pair
    _save_state()
    # prompt expiry next
    await query.edit_message_text(
        f"Selected *{pair}*.\nChoose expiry:", parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_expiry_keyboard(us.expiry)
    )

async def cb_expiry_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, exp = query.data.split(":")
    us = get_user_settings(query.message.chat_id)
    us.expiry = exp
    _save_state()
    await query.edit_message_text(
        f"Expiry set to *{exp}*.\nChoose theme:", parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_theme_keyboard(us.theme)
    )

async def cb_theme_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, th = query.data.split(":")
    us = get_user_settings(query.message.chat_id)
    us.theme = th
    _save_state()
    await query.edit_message_text(
        f"Theme set to *{th}*.\nPick direction:", parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_direction_keyboard()
    )

async def cb_direction_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, direction = query.data.split(":")
    chat_id = query.message.chat_id
    us = get_user_settings(chat_id)
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    # store last direction requested?
    # we'll just send snapshot + trade confirm
    ex, tk, _is_otc, alt = resolve_symbol(us.pair)
    await query.edit_message_text(
        f"{arrow} {us.pair} {direction} Exp: {us.expiry}\nSending chart…",
        parse_mode=ParseMode.MARKDOWN,
    )
    # send snapshot using user interval + theme
    await send_snapshot_photo(chat_id, context, ex, tk, norm_interval(us.interval), us.theme, prefix="[TRADE] ", alt_exchanges=alt)
    # optionally auto-trade
    if us.auto_trade and UI_VISION_URL:
        await _trigger_auto_trade(chat_id, us.pair, direction, us.expiry, us)

async def cb_size_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, mode = query.data.split(":")
    us = get_user_settings(query.message.chat_id)
    us.size_mode = mode
    _save_state()
    await query.edit_message_text(
        f"Mode set to {mode}. Choose amount:",
        reply_markup=build_size_value_keyboard(mode, us.size_value)
    )

async def cb_size_val(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, val = query.data.split(":")
    us = get_user_settings(query.message.chat_id)
    us.size_value = float(val)
    _save_state()
    await query.edit_message_text(
        f"Trade size set to {us.size_value}{us.size_mode}.", parse_mode=ParseMode.MARKDOWN
    )

# ---------------------------------------------------------------------------
# Text Echo Parsing (quick "trade EUR/USD call 5m")
# ---------------------------------------------------------------------------
async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    m = _trade_re.match(txt)
    if m:
        symbol, dirw, exp = m.group(1), m.group(2), m.group(3)
        direction = parse_direction(dirw) or "CALL"
        us = get_user_settings(update.effective_chat.id)
        us.pair = symbol
        us.expiry = exp
        arrow = "🟢↑" if direction == "CALL" else "🔴↓"
        await update.message.reply_text(
            f"{arrow} *{symbol}* {direction} Exp {exp}\nSending chart…",
            parse_mode=ParseMode.MARKDOWN,
        )
        ex, tk, _is_otc, alt = resolve_symbol(symbol)
        await send_snapshot_photo(
            update.effective_chat.id,
            context,
            ex,
            tk,
            norm_interval(us.interval),
            us.theme,
            prefix="[TRADE] ",
            alt_exchanges=alt,
        )
        if us.auto_trade and UI_VISION_URL:
            await _trigger_auto_trade(update.effective_chat.id, symbol, direction, exp, us)
        return
    await update.message.reply_text("Unrecognized. Try: trade EUR/USD CALL 5m  |  or /help.")

async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Unknown command. Try /help.")

# ---------------------------------------------------------------------------
# Pocket Option / UI.Vision Automation (placeholder)
# ---------------------------------------------------------------------------
async def _trigger_auto_trade(chat_id: int, pair: str, direction: str, expiry: str, us: UserSettings):
    if not UI_VISION_URL:
        await _send_bot_msg(chat_id, "⚙ Auto-trade URL not configured; manual only.")
        return
    payload = {
        "pair": pair,
        "direction": direction,
        "expiry": expiry,
        "size_mode": us.size_mode,
        "size_value": us.size_value,
        "chat_id": chat_id,
    }
    try:
        r = await asyncio.to_thread(_http.post, UI_VISION_URL, json=payload, timeout=30)
        if r.status_code == 200:
            await _send_bot_msg(chat_id, "✅ Auto-trade triggered.")
        else:
            await _send_bot_msg(chat_id, f"⚠ Auto-trade error: HTTP {r.status_code}")
    except Exception as e:  # pragma: no cover
        await _send_bot_msg(chat_id, f"⚠ Auto-trade exception: {e}")

async def _send_bot_msg(chat_id: int, text: str):
    # convenience wrapper (async)
    from telegram import Bot
    bot: Bot = _app.bot  # type: ignore  # set after build
    await bot.send_message(chat_id=chat_id, text=text)

# ---------------------------------------------------------------------------
# TradingView Webhook (Flask) -> Telegram
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)  # we run in thread

def _parse_tv_payload(data: dict) -> Dict[str, str]:
    d = {}
    d["chat_id"] = str(data.get("chat_id", ""))  # allow empty -> manual target?
    d["pair"] = str(data.get("pair") or data.get("symbol") or data.get("ticker") or "EUR/USD")
    d["direction"] = str(data.get("direction") or "CALL").upper()
    d["expiry"] = str(data.get("expiry") or data.get("default_expiry_min") or "")
    d["strategy"] = str(data.get("strategy") or "")
    d["winrate"] = str(data.get("winrate") or "")
    d["timeframe"] = str(data.get("timeframe") or data.get("tf") or DEFAULT_INTERVAL)
    d["theme"] = str(data.get("theme") or DEFAULT_THEME)
    d["size_mode"] = str(data.get("size_mode") or "")
    d["size_value"] = str(data.get("size_value") or "")
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
    Called from Flask thread. Synchronous.
    """
    if WEBHOOK_SECRET:
        hdr = request.headers.get("X-Webhook-Token", "")
        body_secret = str(data.get("secret") or data.get("token") or "")
        if hdr != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET:
            logger.warning("Webhook secret mismatch; rejecting.")
            return {"ok": False, "error": "unauthorized"}, 403

    payload = _parse_tv_payload(data)
    logger.info("TV payload normalized: %s", payload)

    chat_id = payload["chat_id"] or os.getenv("TELEGRAM_CHAT_ID", "")
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
    if chat_id:
        tg_api_send_message(chat_id, msg, parse_mode="Markdown")

        # attempt chart snapshot
        try:
            node_start_browser()
            png, ex_used = fetch_snapshot_png_any(ex, tk, tf, theme, "chart", alt)
            tg_api_send_photo_bytes(chat_id, png, caption=f"{ex_used}:{tk} • TF {tf} • {theme}")
        except Exception as e:
            logger.error("TV snapshot error for %s:%s -> %s", ex, tk, e)
            tg_api_send_message(chat_id, f"⚠ Chart snapshot failed for {raw_pair}: {e}")
    else:
        logger.warning("Webhook missing chat_id and TELEGRAM_CHAT_ID not set; dropping.")

    # auto-trade?
    if AUTO_TRADE_FROM_TV and UI_VISION_URL and chat_id:
        try:
            payload = {
                "pair": raw_pair,
                "direction": direction,
                "expiry": expiry,
                "size_mode": payload.get("size_mode", "$"),
                "size_value": payload.get("size_value", "5"),
                "chat_id": chat_id,
            }
            _http.post(UI_VISION_URL, json=payload, timeout=30)
        except Exception as e:  # pragma: no cover
            logger.error("Auto-trade trigger error: %s", e)

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

@flask_app.route("/webhook", methods=["POST"])  # alias
def tv_route_alias():
    return tv_route()

def start_flask_background():
    threading.Thread(
        target=lambda: flask_app.run(
            host="0.0.0.0", port=TV_WEBHOOK_PORT, debug=False, use_reloader=False, threaded=True
        ),
        daemon=True,
    ).start()
    logger.info("Flask TV webhook listening on port %s", TV_WEBHOOK_PORT)

# ---------------------------------------------------------------------------
# Build & Run Telegram Application
# ---------------------------------------------------------------------------
_app: Application  # set in build_application()

def build_application() -> Application:
    # version safety
    if PTB_VERSION_INFO[0] < 20:
        raise RuntimeError(
            f"python-telegram-bot too old (found {PTB_VERSION_INFO}); install >=20: "
            'pip install --upgrade "python-telegram-bot[rate-limiter,http2]>=21.6,<22"'
        )

    builder = Application.builder().token(TOKEN)
    app = builder.build()

    # register bot instance globally for auto-trade toggle messages
    global _app
    _app = app

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pairs", cmd_pairs))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("trade", cmd_trade))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("snap", cmd_snap))
    app.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    app.add_handler(CommandHandler("snapall", cmd_snapall))

    # Callbacks
    app.add_handler(CallbackQueryHandler(cb_pairs_page, pattern=r"^pairs_pg:"))
    app.add_handler(CallbackQueryHandler(cb_pair_select, pattern=r"^pair:"))
    app.add_handler(CallbackQueryHandler(cb_expiry_select, pattern=r"^expiry:"))
    app.add_handler(CallbackQueryHandler(cb_theme_select, pattern=r"^theme:"))
    app.add_handler(CallbackQueryHandler(cb_direction_select, pattern=r"^direction:"))
    app.add_handler(CallbackQueryHandler(cb_size_mode, pattern=r"^sizemode:"))
    app.add_handler(CallbackQueryHandler(cb_size_val, pattern=r"^sizeval:"))

    # Fallbacks
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    return app

# ---------------------------------------------------------------------------
# Main Entry
# ---------------------------------------------------------------------------
def main():
    logger.info(
        "Bot starting… BASE_URL=%s | DefaultEX=%s | WebhookPort=%s | UI_VISION_URL=%s | AUTO_TRADE_FROM_TV=%s | SIM_DEBIT=%s",
        BASE_URL,
        DEFAULT_EXCHANGE,
        TV_WEBHOOK_PORT,
        UI_VISION_URL,
        AUTO_TRADE_FROM_TV,
        SIM_DEBIT,
    )

    _load_state()
    start_flask_background()

    application = build_application()
    application.run_polling()

if __name__ == "__main__":
    main()
