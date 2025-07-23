# =============================
# QuantumTraderBot Full Package
# =============================
#
# This document contains THREE Python source files you asked for:
#   1. tvsnapshotbot.py   – main Telegram bot + Flask webhook + UI + analyze + trading hooks
#   2. strategy.py        – pluggable technical-analysis strategy helpers
#   3. tradelogger.py     – persistent trade/alert statistics + quantum level system
#
# Copy each section into its own .py file in your project directory.
# Make sure to install the required dependencies (see INSTALL notes below).
#
# ---------------------------------------------------------------------------
# INSTALL (first time)
# ---------------------------------------------------------------------------
# In PowerShell (Windows):
#   cd C:\Users\Chop\TeleTradingView\TradingviewSnapshotBot
#   python -m venv .venv
#   .\.venv\Scripts\Activate.ps1
#   pip install --upgrade pip
#   pip install "python-telegram-bot[rate-limiter]>=20,<21" flask httpx pandas numpy pillow packaging
#   # If you plan to run TA extras that need TA-Lib, skip unless installed; we fallback.
#
# Node screenshot microservice must be running (server.js) on the host/port set in SNAPSHOT_BASE_URL.
#
# ---------------------------------------------------------------------------
# ENVIRONMENT (.env or PowerShell session vars)
# ---------------------------------------------------------------------------
# Example (PowerShell):
#   $env:TELEGRAM_BOT_TOKEN="8009536179:XXXXXXXXXXXXXXX"
#   $env:TELEGRAM_CHAT_ID="6337160812"               # default chat fallback
#   $env:SNAPSHOT_BASE_URL="http://localhost:10000"  # your Node screenshot server
#   $env:DEFAULT_EXCHANGE="FX"
#   $env:DEFAULT_INTERVAL="1"
#   $env:DEFAULT_THEME="dark"
#   $env:TV_WEBHOOK_PORT="8081"                      # Flask port
#   $env:TV_WEBHOOK_URL="http://localhost:8081/tv"   # for TradingView alerts
#   $env:UI_VISION_URL="http://localhost:8080/pocket-trade"  # UI.Vision REST macro trigger
#   $env:UI_VISION_MACRO_NAME="PocketTrade"
#   $env:UI_VISION_MACRO_PARAMS="{}"
#   $env:WEBHOOK_SECRET="optionalsecret"
#
# Then run:
#   python tvsnapshotbot.py
#
# ---------------------------------------------------------------------------
# FILE 1/3: tvsnapshotbot.py
# ---------------------------------------------------------------------------

"""
QuantumTraderBot (tvsnapshotbot.py)
==================================

Modern async Telegram bot that:
  • Fetches TradingView chart PNGs via Node screenshot microservice (/snapshot/:pair then /run fallback).
  • Supports FX + OTC + Indices + Crypto instrument mapping.
  • Inline keyboard flows: choose pair → timeframe → theme → size → analyze → trade.
  • /analyze command & callback: quick TA via strategy.py helpers; suggests CALL/PUT & expiry (1m/3m/5m/15m).
  • /trade command & callback: builds trade JSON; optional UI.Vision webhook trigger to automate Pocket Option.
  • /pairs interactive selection (paged) + text list.
  • /stats shows cumulative P/L & performance levels (Quantum levels) via tradelogger.py.
  • TradingView webhook receiver (Flask) at /tv & /webhook.
  • Safe logging (never dumps raw binary) + rotating file logs.
  • JSON state persistence (per-user settings, last pair, preferred trade size).

Tested against python-telegram-bot v20+.
"""

from __future__ import annotations

import os
import io
import re
import sys
import json
import time
import math
import enum
import queue
import atexit
import asyncio
import logging
import pathlib
import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple, Optional, Callable, Union

import httpx
from flask import Flask, request, jsonify

from telegram import (
    __version__ as PTB_VER,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    Application,
    AIORateLimiter,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Local imports (strategy & stats)
try:
    import strategy  # type: ignore
except Exception:  # minimal fallback
    strategy = None  # we'll guard uses

try:
    import tradelogger  # type: ignore
except Exception:
    tradelogger = None

# ------------------------------------------------------------------
# Directories & paths
# ------------------------------------------------------------------
BASE_DIR = pathlib.Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / "state"
LOG_DIR.mkdir(exist_ok=True)
STATE_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "tvsnapshotbot.log"
STATE_FILE = STATE_DIR / "state.json"
STATS_FILE = STATE_DIR / "stats.json"

# ------------------------------------------------------------------
# Logging (safe)
# ------------------------------------------------------------------
_logger = logging.getLogger("TVSnapBot")
_logger.setLevel(logging.INFO)
_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter("%(message)s"))
_logger.addHandler(_file_handler)
_logger.addHandler(_console_handler)

# Avoid noisy httpx debug by default
logging.getLogger("httpx").setLevel(logging.INFO)

# ------------------------------------------------------------------
# Environment variables
# ------------------------------------------------------------------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DEFAULT_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BASE_URL = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000")
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "FX")
DEFAULT_INTERVAL = os.environ.get("DEFAULT_INTERVAL", "1")
DEFAULT_THEME = os.environ.get("DEFAULT_THEME", "dark")
TV_WEBHOOK_PORT = int(os.environ.get("TV_WEBHOOK_PORT", "8081"))
TV_WEBHOOK_URL = os.environ.get("TV_WEBHOOK_URL", f"http://localhost:{TV_WEBHOOK_PORT}/tv")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # optional
UI_VISION_URL = os.environ.get("UI_VISION_URL")  # e.g. http://localhost:8080/pocket-trade
UI_VISION_MACRO_NAME = os.environ.get("UI_VISION_MACRO_NAME", "PocketTrade")
UI_VISION_MACRO_PARAMS = os.environ.get("UI_VISION_MACRO_PARAMS", "{}")
AUTO_TRADE_FROM_TV = os.environ.get("AUTO_TRADE_FROM_TV", "0") not in (None, "0", "false", "False")
SIM_DEBIT = os.environ.get("SIM_DEBIT", "0") not in (None, "0", "false", "False")

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment.")

# ------------------------------------------------------------------
# Global HTTP client(s)
# ------------------------------------------------------------------
_http = httpx.AsyncClient(timeout=60.0)
_sync = httpx.Client(timeout=60.0)

# ------------------------------------------------------------------
# Instrument universe (FX, OTC, Indices, Crypto)
# ------------------------------------------------------------------
# Display names EXACTLY as user sees them. Mapping -> TradingView symbol info.

FX_PAIRS: List[str] = [
    "EUR/USD","GBP/USD","USD/JPY","USD/CHF","AUD/USD","NZD/USD","USD/CAD","EUR/GBP","EUR/JPY","GBP/JPY",
    "AUD/JPY","NZD/JPY","EUR/AUD","GBP/AUD","EUR/CAD","USD/MXN","USD/TRY","USD/ZAR","AUD/CHF","EUR/CHF",
]

OTC_PAIRS: List[str] = [
    "EUR/USD-OTC","GBP/USD-OTC","USD/JPY-OTC","USD/CHF-OTC","AUD/USD-OTC","NZD/USD-OTC","USD/CAD-OTC","EUR/GBP-OTC","EUR/JPY-OTC","GBP/JPY-OTC",
    "AUD/CHF-OTC","EUR/CHF-OTC","KES/USD-OTC","MAD/USD-OTC","USD/BDT-OTC","USD/MXN-OTC","USD/MYR-OTC","USD/PKR-OTC",
]

INDEX_SYMBOLS: List[str] = [
    "US30","US100","US500","GER40","UK100","JPN225","HK50","AUS200","SPX","NDX","DAX","FTSE","NIKKEI","SENSEX",
]

CRYPTO_SYMBOLS: List[str] = [
    "BTC/USD","ETH/USD","SOL/USD","XRP/USD","BNB/USD","DOGE/USD","ADA/USD","LTC/USD","DOT/USD","TRX/USD",
]

ALL_INSTRUMENTS: List[str] = FX_PAIRS + OTC_PAIRS + INDEX_SYMBOLS + CRYPTO_SYMBOLS

# If you want to page results in inline UI
PAIRS_PER_PAGE = 10

# ------------------------------------------------------------------
# Exchange mapping table
# ------------------------------------------------------------------
# We maintain a canonical {KEY: (exchange, ticker, [fallback_exchanges])} table.
# KEY format: uppercase, remove spaces & slashes; preserve -OTC.

# Known fallback exchanges to try when capturing charts.
KNOWN_FX_FALLBACKS = ["FX", "FX_IDC", "OANDA", "FOREXCOM", "IDC"]
KNOWN_OTC_FALLBACKS = ["QUOTEX", "CURRENCY"] + KNOWN_FX_FALLBACKS
KNOWN_INDEX_FALLBACKS = ["INDEX", "CME", "TVC", "OANDA"]
KNOWN_CRYPTO_FALLBACKS = ["BINANCE", "COINBASE", "BYBIT", "BITFINEX", "KRAKEN", "OANDA"]

# Primary mapping dictionary is built below.
PAIR_MAP: Dict[str, Tuple[str, str, List[str]]] = {}


def _canon_key(name: str) -> str:
    return name.strip().upper().replace(" ", "").replace("/", "")

# --- FX ---
for disp in FX_PAIRS:
    key = _canon_key(disp)
    PAIR_MAP[key] = (DEFAULT_EXCHANGE, disp.replace("/", ""), KNOWN_FX_FALLBACKS)

# --- OTC -> underlying feed guess via QUOTEX first ---
_underlying_otc = {
    "EUR/USD-OTC":"EURUSD","GBP/USD-OTC":"GBPUSD","USD/JPY-OTC":"USDJPY","USD/CHF-OTC":"USDCHF","AUD/USD-OTC":"AUDUSD",
    "NZD/USD-OTC":"NZDUSD","USD/CAD-OTC":"USDCAD","EUR/GBP-OTC":"EURGBP","EUR/JPY-OTC":"EURJPY","GBP/JPY-OTC":"GBPJPY",
    "AUD/CHF-OTC":"AUDCHF","EUR/CHF-OTC":"EURCHF","KES/USD-OTC":"USDKES","MAD/USD-OTC":"USDMAD","USD/BDT-OTC":"USDBDT",
    "USD/MXN-OTC":"USDMXN","USD/MYR-OTC":"USDMYR","USD/PKR-OTC":"USDPKR",
}
for disp, tk in _underlying_otc.items():
    key = _canon_key(disp)
    PAIR_MAP[key] = ("QUOTEX", tk, KNOWN_OTC_FALLBACKS)

# --- Indices mapping guess (TVC common) ---
_index_map = {
    "US30":"DJI","US100":"NDX","US500":"SPX","GER40":"DAX","UK100":"FTSE","JPN225":"NI225","HK50":"HSI","AUS200":"ASX" ,
    "SPX":"SPX","NDX":"NDX","DAX":"DAX","FTSE":"FTSE","NIKKEI":"NI225","SENSEX":"SENSEX",
}
for disp, tk in _index_map.items():
    key = _canon_key(disp)
    PAIR_MAP[key] = ("TVC", tk, KNOWN_INDEX_FALLBACKS)

# --- Crypto mapping guess (BINANCE spot perpetual) ---
_crypto_map = {
    "BTC/USD":"BTCUSDT","ETH/USD":"ETHUSDT","SOL/USD":"SOLUSDT","XRP/USD":"XRPUSDT","BNB/USD":"BNBUSDT",
    "DOGE/USD":"DOGEUSDT","ADA/USD":"ADAUSDT","LTC/USD":"LTCUSDT","DOT/USD":"DOTUSDT","TRX/USD":"TRXUSDT",
}
for disp, tk in _crypto_map.items():
    key = _canon_key(disp)
    PAIR_MAP[key] = ("BINANCE", tk, KNOWN_CRYPTO_FALLBACKS)


# ------------------------------------------------------------------
# Timeframe & theme utilities
# ------------------------------------------------------------------

def norm_interval(v: str | None) -> str:
    if not v:
        return DEFAULT_INTERVAL
    t = v.strip().lower()
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


def norm_theme(v: str | None) -> str:
    return "light" if (v and v.lower().startswith("l")) else "dark"


# ------------------------------------------------------------------
# Resolve symbol helpers
# ------------------------------------------------------------------

def resolve_symbol(raw: str) -> Tuple[str, str, List[str]]:
    """Return (exchange, ticker, fallback_list)."""
    if not raw:
        return DEFAULT_EXCHANGE, "EURUSD", KNOWN_FX_FALLBACKS
    s = raw.strip().upper()
    if ":" in s:  # explicit EX:TK
        ex, tk = s.split(":", 1)
        return ex, tk, KNOWN_FX_FALLBACKS
    key = _canon_key(s)
    return PAIR_MAP.get(key, (DEFAULT_EXCHANGE, re.sub(r"[^A-Z0-9]", "", s), KNOWN_FX_FALLBACKS))


# ------------------------------------------------------------------
# Pocket Option / direction utilities
# ------------------------------------------------------------------
_CALL_WORDS = {"CALL","BUY","UP","LONG","HIGH"}
_PUT_WORDS  = {"PUT","SELL","DOWN","SHORT","LOW"}


def parse_direction(word: Optional[str]) -> Optional[str]:
    if not word:
        return None
    w = word.strip().upper()
    if w in _CALL_WORDS:
        return "CALL"
    if w in _PUT_WORDS:
        return "PUT"
    return None


# ------------------------------------------------------------------
# Persistent user settings & sessions
# ------------------------------------------------------------------

class TradeSizeMode(enum.Enum):
    DOLLAR = "USD"
    PERCENT = "%"


@dataclass
class UserSettings:
    chat_id: int
    default_interval: str = DEFAULT_INTERVAL
    default_theme: str = DEFAULT_THEME
    size_mode: TradeSizeMode = TradeSizeMode.DOLLAR
    size_value: float = 1.0  # $ or %
    last_pair: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "default_interval": self.default_interval,
            "default_theme": self.default_theme,
            "size_mode": self.size_mode.value,
            "size_value": self.size_value,
            "last_pair": self.last_pair,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "UserSettings":
        try:
            mode = TradeSizeMode(d.get("size_mode", TradeSizeMode.DOLLAR.value))
        except Exception:
            mode = TradeSizeMode.DOLLAR
        return cls(
            chat_id=int(d["chat_id"]),
            default_interval=str(d.get("default_interval", DEFAULT_INTERVAL)),
            default_theme=str(d.get("default_theme", DEFAULT_THEME)),
            size_mode=mode,
            size_value=float(d.get("size_value", 1.0)),
            last_pair=d.get("last_pair"),
        )


USER_SETTINGS: Dict[int, UserSettings] = {}


def load_state() -> None:
    if not STATE_FILE.exists():
        _logger.info("No state file found; starting fresh.")
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        for k, v in data.items():
            USER_SETTINGS[int(k)] = UserSettings.from_dict(v)
        _logger.info("Loaded %d user settings entries.", len(USER_SETTINGS))
    except Exception as e:
        _logger.error("State load failed: %s", e)


def save_state() -> None:
    try:
        out = {str(k): v.to_dict() for k, v in USER_SETTINGS.items()}
        STATE_FILE.write_text(json.dumps(out, indent=2), encoding="utf-8")
    except Exception as e:
        _logger.error("State save failed: %s", e)


atexit.register(save_state)


def get_user_settings(chat_id: int) -> UserSettings:
    s = USER_SETTINGS.get(chat_id)
    if s is None:
        s = UserSettings(chat_id=chat_id)
        USER_SETTINGS[chat_id] = s
    return s


# ------------------------------------------------------------------
# Trade logger integration (tradelogger.py)
# ------------------------------------------------------------------
if tradelogger is not None:
    STATS = tradelogger.TradeStatsStore(STATS_FILE)
else:
    STATS = None  # We'll guard uses.


# ------------------------------------------------------------------
# Screenshot fetch (PNG) + JSON candle fetch
# ------------------------------------------------------------------

MIN_VALID_PNG = 2000  # bytes


async def _http_get_bytes(url: str, timeout: float = 60.0) -> Tuple[int, bytes, str]:
    try:
        r = await _http.get(url, timeout=timeout)
        ct = r.headers.get("content-type", "")
        data = r.content
        return r.status_code, data, ct
    except Exception as e:  # network error
        return 0, b"", str(e)


async def _http_get_json(url: str, timeout: float = 60.0) -> Tuple[int, Any, str]:
    try:
        r = await _http.get(url, timeout=timeout)
        ct = r.headers.get("content-type", "")
        if r.status_code == 200:
            try:
                return r.status_code, r.json(), ct
            except Exception as je:
                return r.status_code, None, f"json error: {je}"
        return r.status_code, None, ct
    except Exception as e:
        return 0, None, str(e)


async def fetch_snapshot_png_any(
    pair_display: str,
    interval: str,
    theme: str,
) -> Tuple[bytes, str]:
    """Try /snapshot/:pair first. If fail or <MIN_VALID_PNG, fallback across mapped exchanges.
    Returns (png_bytes, exchange_used).
    Raises RuntimeError if all fail.
    """
    # 1) snapshot direct
    snap_url = f"{BASE_URL}/snapshot/{pair_display.replace('/', '')}?tf={interval}&theme={theme}"
    code, data, ct = await _http_get_bytes(snap_url)
    if code == 200 and ct.startswith("image") and len(data) >= MIN_VALID_PNG:
        return data, "SNAPSHOT"

    # 2) fallback by exchange list
    ex, tk, fallbacks = resolve_symbol(pair_display)
    tried = []
    last_err = None
    for exch in [ex] + fallbacks:
        tried.append(exch)
        url = f"{BASE_URL}/run?exchange={exch}&ticker={tk}&interval={interval}&theme={theme}"
        code, data, ct = await _http_get_bytes(url)
        if code == 200 and ct.startswith("image") and len(data) >= MIN_VALID_PNG:
            return data, exch
        last_err = f"HTTP {code} {ct} bytes={len(data)}"
    raise RuntimeError(f"All exchanges failed for {pair_display}: {last_err}. Tried: {tried}")


async def fetch_chart_json(
    pair_display: str,
    interval: str,
    lookback: int = 200,
) -> Optional[Dict[str, Any]]:
    """Ask Node service for JSON candles if supported.
    Returns dict with keys: o,h,l,c,v,t (lists) or None.
    """
    # Most Node services implement /snapshot/:pair?fmt=json&limit=N
    base = pair_display.replace("/", "")
    url = f"{BASE_URL}/snapshot/{base}?fmt=json&limit={lookback}&tf={interval}"
    code, js, _ = await _http_get_json(url)
    if code == 200 and isinstance(js, dict):
        return js
    return None


# ------------------------------------------------------------------
# UI keyboards
# ------------------------------------------------------------------

EXPIRY_BUTTONS = [
    ("1m", "1m"),
    ("3m", "3m"),
    ("5m", "5m"),
    ("15m", "15m"),
]

SIZE_DOLLAR_PRESETS = [1, 5, 10, 25, 50, 100]
SIZE_PERCENT_PRESETS = [1, 2, 5, 10, 25, 50, 100]


def build_pairs_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    items = ALL_INSTRUMENTS
    start = page * PAIRS_PER_PAGE
    end = start + PAIRS_PER_PAGE
    chunk = items[start:end]
    rows = []
    for p in chunk:
        rows.append([InlineKeyboardButton(p, callback_data=f"pair:{p}")])
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"pairs:page:{page-1}"))
    if end < len(items):
        nav.append(InlineKeyboardButton("▶", callback_data=f"pairs:page:{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def build_expiry_keyboard(pair: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(txt, callback_data=f"expiry:{pair}:{val}") for txt, val in EXPIRY_BUTTONS]
    ])


def build_size_mode_keyboard(pair: str, expiry: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("$", callback_data=f"sizemode:{pair}:{expiry}:$"),
            InlineKeyboardButton("%", callback_data=f"sizemode:{pair}:{expiry}:%"),
        ]
    ])


def build_size_value_keyboard(pair: str, expiry: str, mode: TradeSizeMode) -> InlineKeyboardMarkup:
    vals = SIZE_DOLLAR_PRESETS if mode == TradeSizeMode.DOLLAR else SIZE_PERCENT_PRESETS
    rows = []
    row: List[InlineKeyboardButton] = []
    for v in vals:
        row.append(InlineKeyboardButton(str(v), callback_data=f"sizeval:{pair}:{expiry}:{mode.value}:{v}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def build_theme_keyboard(pair: str, expiry: str, mode: TradeSizeMode, size: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Dark", callback_data=f"theme:{pair}:{expiry}:{mode.value}:{size}:dark"), InlineKeyboardButton("Light", callback_data=f"theme:{pair}:{expiry}:{mode.value}:{size}:light")]
    ])


def build_direction_keyboard(pair: str, expiry: str, mode: TradeSizeMode, size: float, theme: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 CALL", callback_data=f"dir:{pair}:{expiry}:{mode.value}:{size}:{theme}:CALL"), InlineKeyboardButton("🔴 PUT", callback_data=f"dir:{pair}:{expiry}:{mode.value}:{size}:{theme}:PUT")],
        [InlineKeyboardButton("🔍 Analyze", callback_data=f"analyze:{pair}:{expiry}:{mode.value}:{size}:{theme}" )],
    ])


# ------------------------------------------------------------------
# Inline handler router
# ------------------------------------------------------------------
async def inline_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""

    try:
        if data.startswith("pairs:page:"):
            pg = int(data.split(":")[2])
            await q.edit_message_reply_markup(build_pairs_keyboard(pg))
            return

        if data.startswith("pair:"):
            pair = data.split(":", 1)[1]
            # store last_pair
            us = get_user_settings(update.effective_chat.id)
            us.last_pair = pair
            await q.edit_message_text(
                text=f"Selected *{pair}*. Choose expiry:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_expiry_keyboard(pair),
            )
            return

        if data.startswith("expiry:"):
            _, pair, expiry = data.split(":", 2)
            await q.edit_message_text(
                text=f"Pair: *{pair}*\nExpiry: *{expiry}*\nChoose size mode:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_size_mode_keyboard(pair, expiry),
            )
            return

        if data.startswith("sizemode:"):
            _, pair, expiry, mode_char = data.split(":", 3)
            mode = TradeSizeMode.DOLLAR if mode_char == "$" else TradeSizeMode.PERCENT
            await q.edit_message_text(
                text=f"Pair: *{pair}*\nExpiry: *{expiry}*\nSize mode: *{mode.value}*\nSelect amount:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_size_value_keyboard(pair, expiry, mode),
            )
            return

        if data.startswith("sizeval:"):
            _, pair, expiry, mode_char, val = data.split(":", 4)
            mode = TradeSizeMode.DOLLAR if mode_char == "$" else TradeSizeMode.PERCENT
            size_val = float(val)
            await q.edit_message_text(
                text=f"Pair: *{pair}*\nExpiry: *{expiry}*\nSize: *{val}{mode.value}*\nChoose theme:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_theme_keyboard(pair, expiry, mode, size_val),
            )
            return

        if data.startswith("theme:"):
            _, pair, expiry, mode_char, val, theme = data.split(":", 5)
            mode = TradeSizeMode.DOLLAR if mode_char == "$" else TradeSizeMode.PERCENT
            size_val = float(val)
            await q.edit_message_text(
                text=(
                    f"Pair: *{pair}*\n"
                    f"Expiry: *{expiry}*\n"
                    f"Size: *{val}{mode.value}*\n"
                    f"Theme: *{theme}*\nChoose direction or Analyze:"
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_direction_keyboard(pair, expiry, mode, size_val, theme),
            )
            return

        if data.startswith("analyze:"):
            _, pair, expiry, mode_char, val, theme = data.split(":", 5)
            mode = TradeSizeMode.DOLLAR if mode_char == "$" else TradeSizeMode.PERCENT
            size_val = float(val)
            await _do_analyze_callback(q, context, pair, expiry, mode, size_val, theme)
            return

        if data.startswith("dir:"):
            _, pair, expiry, mode_char, val, theme, direction = data.split(":", 6)
            mode = TradeSizeMode.DOLLAR if mode_char == "$" else TradeSizeMode.PERCENT
            size_val = float(val)
            await _do_trade_callback(q, context, pair, expiry, mode, size_val, theme, direction)
            return

    except Exception as e:  # catch parse errors
        _logger.error("inline_router error: %s", e)
        await q.edit_message_text("Something went wrong. Try /pairs again.")


# ------------------------------------------------------------------
# Inline callback implementations
# ------------------------------------------------------------------
async def _do_analyze_callback(q, context, pair: str, expiry: str, mode: TradeSizeMode, size_val: float, theme: str) -> None:
    chat_id = q.message.chat.id if q.message else q.from_user.id
    await q.edit_message_text(f"Analyzing {pair}… please wait…")
    tf = get_user_settings(chat_id).default_interval
    result = await analyze_symbol(pair, tf)
    if result is None:
        text = f"❌ Analysis failed for *{pair}* (no data)."
    else:
        text = format_analysis_text(pair, result)
    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
    # send chart snapshot
    await send_snapshot_photo(chat_id, context, pair, tf, theme)
    # return to direction choose
    await context.bot.send_message(
        chat_id,
        "Select direction to trade:",
        reply_markup=build_direction_keyboard(pair, expiry, mode, size_val, theme),
    )


async def _do_trade_callback(q, context, pair: str, expiry: str, mode: TradeSizeMode, size_val: float, theme: str, direction: str) -> None:
    chat_id = q.message.chat.id if q.message else q.from_user.id
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    txt = f"{arrow} *{pair}* {direction} Expiry {expiry} Size {size_val}{mode.value}"
    await q.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN)
    # snapshot
    tf = get_user_settings(chat_id).default_interval
    await send_snapshot_photo(chat_id, context, pair, tf, theme, prefix="[TRADE] ")
    # record trade (sim)
    if STATS is not None:
        STATS.record_trade(pair=pair, direction=direction, amount=size_val, amount_mode=mode.value, expiry=expiry, result=None)
    # optional auto trigger to PocketOption (UI.Vision)
    await trigger_pocket_trade(pair, direction, expiry, size_val, mode)


# ------------------------------------------------------------------
# Pocket Option trade trigger (UI.Vision REST)
# ------------------------------------------------------------------
async def trigger_pocket_trade(pair: str, direction: str, expiry: str, size_val: float, mode: TradeSizeMode) -> None:
    if not UI_VISION_URL:
        return
    payload = {
        "macro": UI_VISION_MACRO_NAME,
        "pair": pair,
        "direction": direction,
        "expiry": expiry,
        "size": size_val,
        "mode": mode.value,
        "params": UI_VISION_MACRO_PARAMS,
    }
    try:
        resp = await _http.post(UI_VISION_URL, json=payload)
        _logger.info("UI.Vision trade POST %s -> %s", UI_VISION_URL, resp.status_code)
    except Exception as e:
        _logger.error("UI.Vision trigger error: %s", e)


# ------------------------------------------------------------------
# PNG sending utilities
# ------------------------------------------------------------------
async def send_snapshot_photo(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    pair_display: str,
    interval: str,
    theme: str,
    prefix: str = "",
) -> None:
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
        png, ex_used = await fetch_snapshot_png_any(pair_display, interval, theme)
        cap = f"{prefix}{ex_used}:{pair_display.replace('/', '')} • TF {interval} • {theme}"
        await context.bot.send_photo(chat_id=chat_id, photo=png, caption=cap)
    except Exception as e:
        _logger.error("send_snapshot_photo error: %s", e)
        await context.bot.send_message(chat_id, f"❌ Snapshot failed: {pair_display} ({e})")


async def send_media_group_chunked(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    pair_list: List[str],
    interval: str,
    theme: str,
    prefix: str = "",
    chunk_size: int = 5,
) -> None:
    media: List[InputMediaPhoto] = []
    for p in pair_list:
        try:
            png, ex_used = await fetch_snapshot_png_any(p, interval, theme)
            bio = io.BytesIO(png)
            bio.name = "chart.png"
            cap = f"{prefix}{ex_used}:{p}"
            media.append(InputMediaPhoto(media=bio, caption=cap))
        except Exception as e:
            _logger.warning("group snapshot failed %s: %s", p, e)
    if not media:
        await context.bot.send_message(chat_id, "❌ No charts captured.")
        return
    # chunk
    for i in range(0, len(media), chunk_size):
        chunk = media[i:i+chunk_size]
        if len(chunk) > 1:
            for m in chunk[1:]:
                m.caption = None
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(1.0)


# ------------------------------------------------------------------
# Analysis logic (uses strategy.py if present else fallback)
# ------------------------------------------------------------------
@dataclass
class AnalysisResult:
    direction: Optional[str]
    confidence: float  # 0-1
    comment: str
    indicators: Dict[str, Any] = field(default_factory=dict)
    suggested_expiry: str = "5m"


def format_analysis_text(pair: str, ar: AnalysisResult) -> str:
    dir_txt = ar.direction or "NEUTRAL"
    arrow = "🟢↑" if ar.direction == "CALL" else ("🔴↓" if ar.direction == "PUT" else "➖")
    conf_pct = f"{ar.confidence*100:.0f}%" if ar.confidence is not None else "—"
    lines = [
        f"📈 *Analysis* — {pair}",
        f"Direction: {arrow} *{dir_txt}*",        
        f"Confidence: {conf_pct}",
        f"Suggested Expiry: *{ar.suggested_expiry}*",
        "",
        ar.comment or "No comment.",
    ]
    return "\n".join(lines)


async def analyze_symbol(pair: str, interval: str) -> Optional[AnalysisResult]:
    # fetch JSON candles
    js = await fetch_chart_json(pair, interval)
    if not js:
        return None
    # if strategy module available, call it
    if strategy is not None and hasattr(strategy, "quick_analyze"):
        try:
            return strategy.quick_analyze(pair, js)
        except Exception as e:
            _logger.error("strategy.quick_analyze error: %s", e)
    # fallback naive
    try:
        closes = js.get("c") or []
        if len(closes) < 5:
            return None
        last = closes[-1]
        prev = closes[-5]
        direction = "CALL" if last > prev else "PUT"
        conf = min(1.0, abs(last - prev) / (prev * 0.01 + 1e-9))
        comment = "Naive momentum over last 5 bars."
        return AnalysisResult(direction=direction, confidence=conf, comment=comment, suggested_expiry="5m")
    except Exception:
        return None


# ------------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    us = get_user_settings(chat_id)
    msg = (
        f"Hi {update.effective_user.first_name if update.effective_user else ''} 👋\n\n"
        "I'm *QuantumTraderBot*. I grab TradingView charts, analyze signals, and can forward trades to Pocket Option (UI.Vision).\n\n"
        "Commands:\n"
        "• /pairs – choose an instrument\n"
        "• /analyze SYMBOL – quick TA & chart\n"
        "• /trade SYMBOL CALL|PUT expiry – send trade & chart\n"
        "• /stats – performance summary\n"
        "• /settings – update defaults\n"
        "• /help – full reference\n"
    )
    await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN, reply_markup=build_pairs_keyboard(0))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "📘 *Help*\n\n"
        "`/snap SYMBOL [interval] [theme]` – capture single chart\n"
        "`/snapmulti S1 S2 ... [interval] [theme]` – capture many\n"
        "`/analyze SYMBOL [interval]` – TA suggestion\n"
        "`/trade SYMBOL CALL|PUT [expiry] [size][mode][$|%]` – trade & chart\n"
        "`/stats` – performance\n"
        "`/pairs` – interactive instrument picker\n"
        "`/settings` – adjust defaults\n"
        "`/next` – placeholder signal watcher\n\n"
        "Intervals: minutes (#) or D/W/M. Themes: dark|light."
    )
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(
        update.effective_chat.id,
        "Select a pair/instrument:",
        reply_markup=build_pairs_keyboard(0),
    )


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await context.bot.send_message(update.effective_chat.id, "Usage: /analyze SYMBOL [interval]")
        return
    symbol = args[0]
    tf = args[1] if len(args) > 1 else get_user_settings(update.effective_chat.id).default_interval
    theme = get_user_settings(update.effective_chat.id).default_theme
    res = await analyze_symbol(symbol, norm_interval(tf))
    if res is None:
        await context.bot.send_message(update.effective_chat.id, f"❌ Analysis failed for {symbol}.")
    else:
        await context.bot.send_message(update.effective_chat.id, format_analysis_text(symbol, res), parse_mode=ParseMode.MARKDOWN)
    await send_snapshot_photo(update.effective_chat.id, context, symbol, norm_interval(tf), theme)


async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /trade SYMBOL CALL|PUT expiry [size] [mode]
    args = context.args
    if len(args) < 3:
        await context.bot.send_message(update.effective_chat.id, "Usage: /trade SYMBOL CALL|PUT expiry [size] [$|%]")
        return
    symbol = args[0]
    direction = parse_direction(args[1]) or "CALL"
    expiry = args[2]
    size_val = float(args[3]) if len(args) > 3 else get_user_settings(update.effective_chat.id).size_value
    mode = TradeSizeMode.PERCENT if (len(args) > 4 and args[4].startswith("%")) else TradeSizeMode.DOLLAR
    theme = get_user_settings(update.effective_chat.id).default_theme
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    txt = f"{arrow} *{symbol}* {direction} Expiry {expiry} Size {size_val}{mode.value}"
    await context.bot.send_message(update.effective_chat.id, txt, parse_mode=ParseMode.MARKDOWN)
    tf = get_user_settings(update.effective_chat.id).default_interval
    await send_snapshot_photo(update.effective_chat.id, context, symbol, tf, theme, prefix="[TRADE] ")
    if STATS is not None:
        STATS.record_trade(pair=symbol, direction=direction, amount=size_val, amount_mode=mode.value, expiry=expiry, result=None)
    await trigger_pocket_trade(symbol, direction, expiry, size_val, mode)


async def cmd_snap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    symbol = args[0] if args else "EUR/USD"
    tf = args[1] if len(args) >= 2 else get_user_settings(update.effective_chat.id).default_interval
    th = args[2] if len(args) >= 3 else get_user_settings(update.effective_chat.id).default_theme
    await send_snapshot_photo(update.effective_chat.id, context, symbol, norm_interval(tf), norm_theme(th))


async def cmd_snapmulti(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await context.bot.send_message(update.effective_chat.id, "Usage: /snapmulti S1 S2 ... [interval] [theme]")
        return
    # detect theme last
    theme = get_user_settings(update.effective_chat.id).default_theme
    if args[-1].lower() in ("dark","light"):
        theme = args[-1].lower(); args = args[:-1]
    tf = get_user_settings(update.effective_chat.id).default_interval
    if args and re.fullmatch(r"\d+", args[-1]):
        tf = args[-1]; args = args[:-1]
    await context.bot.send_message(update.effective_chat.id, f"Capturing {len(args)} charts…")
    await send_media_group_chunked(update.effective_chat.id, context, args, norm_interval(tf), norm_theme(theme))


async def cmd_snapall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(update.effective_chat.id, f"Capturing all {len(ALL_INSTRUMENTS)} instruments… this may take a while.")
    us = get_user_settings(update.effective_chat.id)
    await send_media_group_chunked(update.effective_chat.id, context, ALL_INSTRUMENTS, us.default_interval, us.default_theme)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if STATS is None:
        await context.bot.send_message(update.effective_chat.id, "No statistics module loaded.")
        return
    chat_id = update.effective_chat.id
    summary = STATS.summary_for_chat(chat_id if chat_id else int(DEFAULT_CHAT_ID or 0))
    txt = tradelogger.format_stats_summary(summary)
    await context.bot.send_message(chat_id, txt, parse_mode=ParseMode.MARKDOWN)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = get_user_settings(update.effective_chat.id)
    txt = (
        "⚙ *Settings*\n\n"
        f"Default Interval: `{us.default_interval}`\n"
        f"Default Theme: `{us.default_theme}`\n"
        f"Trade Size: `{us.size_value}{us.size_mode.value}`\n"
        f"Last Pair: `{us.last_pair}`\n\n"
        "Use commands:\n"
        "/setinterval N | /settheme dark|light | /setsize value [$|%]"
    )
    await context.bot.send_message(update.effective_chat.id, txt, parse_mode=ParseMode.MARKDOWN)


async def cmd_setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await context.bot.send_message(update.effective_chat.id, "Usage: /setinterval N")
        return
    us = get_user_settings(update.effective_chat.id)
    us.default_interval = norm_interval(args[0])
    save_state()
    await context.bot.send_message(update.effective_chat.id, f"Interval set to {us.default_interval}.")


async def cmd_settheme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await context.bot.send_message(update.effective_chat.id, "Usage: /settheme dark|light")
        return
    us = get_user_settings(update.effective_chat.id)
    us.default_theme = norm_theme(args[0])
    save_state()
    await context.bot.send_message(update.effective_chat.id, f"Theme set to {us.default_theme}.")


async def cmd_setsize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await context.bot.send_message(update.effective_chat.id, "Usage: /setsize value [$|%]")
        return
    val = float(args[0])
    mode = TradeSizeMode.PERCENT if (len(args) > 1 and args[1].startswith("%")) else TradeSizeMode.DOLLAR
    us = get_user_settings(update.effective_chat.id)
    us.size_mode = mode
    us.size_value = val
    save_state()
    await context.bot.send_message(update.effective_chat.id, f"Size set to {val}{mode.value}.")


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(update.effective_chat.id, "👀 Watching for next signal (placeholder; feed TradingView alerts to /tv).")


# Echo fallback (quick parse of inline trade text)
_trade_re = re.compile(r"(?i)trade\s+([A-Z/:-]+)\s+(call|put|buy|sell|up|down)\s+([0-9]+m?)")


async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = update.message.text.strip()
    m = _trade_re.match(txt)
    if m:
        symbol, dirw, exp = m.group(1), m.group(2), m.group(3)
        direction = parse_direction(dirw) or "CALL"
        arrow = "🟢↑" if direction == "CALL" else "🔴↓"
        await context.bot.send_message(update.effective_chat.id, f"{arrow} *{symbol}* {direction} Expiry {exp}", parse_mode=ParseMode.MARKDOWN)
        us = get_user_settings(update.effective_chat.id)
        await send_snapshot_photo(update.effective_chat.id, context, symbol, us.default_interval, us.default_theme, prefix="[TRADE] ")
        if STATS is not None:
            STATS.record_trade(symbol, direction, us.size_value, us.size_mode.value, exp, None)
        await trigger_pocket_trade(symbol, direction, exp, us.size_value, us.size_mode)
        return
    await context.bot.send_message(update.effective_chat.id, "Try /help.")


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(update.effective_chat.id, "❌ Unknown command. Try /help.")


# ------------------------------------------------------------------
# Flask TradingView webhook integration
# ------------------------------------------------------------------
flask_app = Flask(__name__)


def _parse_tv_payload(data: dict) -> Dict[str, str]:
    d: Dict[str, str] = {}
    d["chat_id"]   = str(data.get("chat_id") or DEFAULT_CHAT_ID or "")
    d["pair"]      = str(data.get("pair") or data.get("symbol") or data.get("ticker") or "EUR/USD")
    d["direction"] = str(data.get("direction") or "CALL").upper()
    d["expiry"]    = str(data.get("expiry") or data.get("default_expiry_min") or "")
    d["strategy"]  = str(data.get("strategy") or "")
    d["winrate"]   = str(data.get("winrate") or "")
    d["timeframe"] = str(data.get("timeframe") or data.get("tf") or DEFAULT_INTERVAL)
    d["theme"]     = str(data.get("theme") or DEFAULT_THEME)
    return d


def _tv_send_message(chat_id: str, text: str, parse_mode: Optional[str] = None) -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        _sync.post(url, json=payload, timeout=30)
    except Exception as e:
        _logger.error("tv_send_message error: %s", e)


def _tv_send_photo(chat_id: str, png: bytes, caption: str = "") -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", png, "image/png")}
    data = {"chat_id": chat_id, "caption": caption}
    try:
        _sync.post(url, data=data, files=files, timeout=60)
    except Exception as e:
        _logger.error("tv_send_photo error: %s", e)


def _handle_tv_alert_sync(data: dict):
    # auth
    if WEBHOOK_SECRET:
        hdr = request.headers.get("X-Webhook-Token", "")
        body_secret = str(data.get("secret") or data.get("token") or "")
        if hdr != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET:
            _logger.warning("Webhook secret mismatch.")
            return {"ok": False, "error": "unauthorized"}, 403

    payload = _parse_tv_payload(data)
    _logger.info("TV payload: %s", payload)

    chat_id   = payload["chat_id"]
    pair      = payload["pair"]
    direction = parse_direction(payload["direction"]) or "CALL"
    expiry    = payload["expiry"]
    strat     = payload["strategy"]
    winrate   = payload["winrate"]
    tf        = norm_interval(payload["timeframe"])
    theme     = norm_theme(payload["theme"])

    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    msg = (
        f"🔔 *TradingView Alert*\n"
        f"Pair: {pair}\n"
        f"Direction: {arrow} {direction}\n"
        f"Expiry: {expiry}\n"
        f"Strategy: {strat}\n"
        f"Win Rate: {winrate}\n"
        f"TF: {tf} • Theme: {theme}"
    )
    _tv_send_message(chat_id, msg, parse_mode="Markdown")

    # try snapshot (sync fallback calls async via anyio would be overkill; do blocking req)
    try:
        # use /snapshot first
        base = pair.replace("/", "")
        snap_url = f"{BASE_URL}/snapshot/{base}?tf={tf}&theme={theme}"
        r = _sync.get(snap_url, timeout=60)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image") and len(r.content) >= MIN_VALID_PNG:
            _tv_send_photo(chat_id, r.content, caption=f"{pair} • TF {tf}")
        else:
            # fallback EX
            ex, tk, fall = resolve_symbol(pair)
            for exch in [ex] + fall:
                url = f"{BASE_URL}/run?exchange={exch}&ticker={tk}&interval={tf}&theme={theme}"
                rr = _sync.get(url, timeout=60)
                if rr.status_code == 200 and rr.headers.get("content-type","" ).startswith("image") and len(rr.content) >= MIN_VALID_PNG:
                    _tv_send_photo(chat_id, rr.content, caption=f"{exch}:{tk} • TF {tf}")
                    break
            else:
                _tv_send_message(chat_id, f"⚠ Chart snapshot failed for {pair}.")
    except Exception as e:
        _logger.error("TV snapshot sync error: %s", e)
        _tv_send_message(chat_id, f"⚠ Chart snapshot failed for {pair}: {e}")

    # optional auto trade
    if AUTO_TRADE_FROM_TV:
        # minimal default: 1$ CALL
        try:
            asyncio.get_event_loop().create_task(trigger_pocket_trade(pair, direction, expiry or "5m", 1.0, TradeSizeMode.DOLLAR))
        except Exception as e:
            _logger.error("auto trade spawn error: %s", e)

    return {"ok": True}, 200


@flask_app.post("/tv")
def tv_route():
    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:
        _logger.error("TV /tv invalid JSON: %s", e)
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    body, code = _handle_tv_alert_sync(data)
    return jsonify(body), code


@flask_app.post("/webhook")
def tv_route_alias():
    return tv_route()


# ------------------------------------------------------------------
# Build & run Telegram application
# ------------------------------------------------------------------

def build_application() -> Application:
    builder = ApplicationBuilder().token(TOKEN).concurrent_updates(True)
    try:
        builder = builder.rate_limiter(AIORateLimiter())
    except Exception:
        pass
    app = builder.build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pairs", cmd_pairs))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("trade", cmd_trade))
    app.add_handler(CommandHandler("snap", cmd_snap))
    app.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    app.add_handler(CommandHandler("snapall", cmd_snapall))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("setinterval", cmd_setinterval))
    app.add_handler(CommandHandler("settheme", cmd_settheme))
    app.add_handler(CommandHandler("setsize", cmd_setsize))
    app.add_handler(CommandHandler("next", cmd_next))

    # inline callbacks
    app.add_handler(CallbackQueryHandler(inline_router))

    # fallback echo text
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    return app


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    print(f"Bot starting… BASE_URL={BASE_URL} | DefaultEX={DEFAULT_EXCHANGE} | WebhookPort={TV_WEBHOOK_PORT} | UI_VISION_URL={UI_VISION_URL} | AUTO_TRADE_FROM_TV={AUTO_TRADE_FROM_TV} | SIM_DEBIT={SIM_DEBIT}")
    load_state()

    # start Flask in background thread
    import threading
    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=TV_WEBHOOK_PORT, debug=False, use_reloader=False, threaded=True),
        daemon=True,
    ).start()
    _logger.info("Flask TV webhook listening on port %s", TV_WEBHOOK_PORT)

    # Telegram app
    application = build_application()
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
