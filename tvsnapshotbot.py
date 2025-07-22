#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
TradingView → Telegram Snapshot Bot (Inline Edition)
====================================================
Key Features
------------
• Async python-telegram-bot v20+ (no legacy Updater crash).
• Inline keyboards: pick Pair → Analyze → Trade flow.
• /pairs paginated list (FX, OTC, Crypto, Indices, Metals, Custom).
• /analyze SYMBOL [tf] [theme] → pulls JSON candles from /snapshot first, runs quick TA, suggests CALL/PUT + 1m/3m/5m/15m.
• Snapshot fetch order: GET /snapshot/:pair?tf=X&theme=Y → fallback to /run?base=chart&exchange=EX&ticker=TK&interval=X&theme=Y.
• Accept/validate PNG (>2 KB) and ignore HTML error bodies.
• TradingView webhook (/tv & /webhook) → Telegram alert + snapshot + “Trade This” button.
• Pocket Option automation: POST to UI.Vision macro endpoint (configurable).
• Trade size presets: $1, $5, $10, $25, $50, %1, %2.5, %5, %10 (user‑selectable; stored in JSON state).
• Rotating logs + Windows console safe logging (binary truncation).
• Simple persistence: `state.json` (per‑chat size mode/amount + last pair).
• Rate limiting + global throttle.
• Config by environment variables (or defaults).

Environment Variables (expected in shell or .env)
-------------------------------------------------
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=6337160812                # optional default target
SNAPSHOT_BASE_URL=http://localhost:10000   # your Node/Render snapshot svc
DEFAULT_EXCHANGE=FX
DEFAULT_INTERVAL=1
DEFAULT_THEME=dark
TV_WEBHOOK_PORT=8081
TV_WEBHOOK_URL=http://localhost:8081/tv    # optional self URL (for instructions)
UI_VISION_URL=http://localhost:8080/pocket-trade   # optional
UI_VISION_MACRO_NAME=PocketTrade
UI_VISION_MACRO_PARAMS={}                  # JSON string; optional
WEBHOOK_SECRET=optionalsharedtoken         # optional
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote, urlencode

import httpx
from flask import Flask, jsonify, request

# telegram imports (v20+)
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    InputMediaPhoto,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    AIORateLimiter,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Version / Globals
# ---------------------------------------------------------------------------
BOT_VERSION = "3.0.0-inline"
APP_NAME = "TVSnapBot"

# ---------------------------------------------------------------------------
# Logging Setup (safe for binary bodies)
# ---------------------------------------------------------------------------
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "tvsnapshotbot.log")


class _SafeFormatter(logging.Formatter):
    def format(self, record):
        if isinstance(record.args, tuple):
            safe_args = []
            for a in record.args:
                if isinstance(a, (bytes, bytearray)):
                    safe_args.append(f"<{len(a)} bytes>")
                else:
                    s = str(a)
                    if len(s) > 300:
                        s = s[:300] + "...(trunc)…"
                    # replace non-print
                    safe_args.append("".join(ch if 32 <= ord(ch) <= 126 else "�" for ch in s))
            record.args = tuple(safe_args)
        return super().format(record)


_console_handler = logging.StreamHandler(stream=sys.stdout)
_console_handler.setFormatter(_SafeFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))

from logging.handlers import RotatingFileHandler

_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
_file_handler.setFormatter(_SafeFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_console_handler, _file_handler])
logger = logging.getLogger(APP_NAME)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment.")

DEFAULT_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SNAPSHOT_BASE_URL = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000").rstrip("/")
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "FX").upper()
DEFAULT_INTERVAL = os.environ.get("DEFAULT_INTERVAL", "1")
DEFAULT_THEME = os.environ.get("DEFAULT_THEME", "dark")
TV_WEBHOOK_PORT = int(os.environ.get("TV_WEBHOOK_PORT", "8081"))
TV_WEBHOOK_URL = os.environ.get("TV_WEBHOOK_URL")  # optional
UI_VISION_URL = os.environ.get("UI_VISION_URL")  # optional
UI_VISION_MACRO_NAME = os.environ.get("UI_VISION_MACRO_NAME", "PocketTrade")
UI_VISION_MACRO_PARAMS = os.environ.get("UI_VISION_MACRO_PARAMS")  # JSON string
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # optional

# fallback size if UI_VISION_MACRO_PARAMS invalid
try:
    UI_VISION_MACRO_PARAMS_OBJ = json.loads(UI_VISION_MACRO_PARAMS) if UI_VISION_MACRO_PARAMS else {}
except Exception:
    UI_VISION_MACRO_PARAMS_OBJ = {}

HTTP_TIMEOUT = 60
_http = httpx.Client(timeout=HTTP_TIMEOUT)

# ---------------------------------------------------------------------------
# Rate Limits
# ---------------------------------------------------------------------------
RATE_LIMIT_SECONDS = 3
GLOBAL_MIN_GAP = 0.75

_last_per_chat: Dict[int, float] = {}
_global_last = 0.0


def rate_limited(chat_id: int) -> bool:
    now = time.time()
    last = _last_per_chat.get(chat_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    _last_per_chat[chat_id] = now
    return False


def global_throttle_wait():
    global _global_last
    now = time.time()
    gap = now - _global_last
    if gap < GLOBAL_MIN_GAP:
        time.sleep(GLOBAL_MIN_GAP - gap)
    _global_last = time.time()


# ---------------------------------------------------------------------------
# Asset Catalogs
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

INDEX_PAIRS = [
    "US30", "SPX", "NAS100", "GER40", "UK100",
]

METAL_PAIRS = [
    "XAU/USD", "XAG/USD",
]

ALL_PAIRS = FX_PAIRS + OTC_PAIRS + CRYPTO_PAIRS + INDEX_PAIRS + METAL_PAIRS


def _canon_key(pair: str) -> str:
    return pair.strip().upper().replace(" ", "").replace("/", "")


# Map OTC to underlying tickers (for /run fallback)
_OTC_UNDERLYING = {
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

# Additional feed preferences per asset group
# For indices & metals we often need TV-specific root tickers; user may override in env
INDEX_FALLBACKS = ["INDEX", "CURRENCY", "FX", "OANDA", "FOREXCOM"]
METAL_FALLBACKS = ["METALS", "CURRENCY", "FX_IDC", "OANDA", "FOREXCOM"]
CRYPTO_FALLBACKS = ["CRYPTO", "BINANCE", "BYBIT", "BITFINEX", "COINBASE", "KRAKEN"]

# ---------------------------------------------------------------------------
# Size Presets
# ---------------------------------------------------------------------------
USD_PRESETS = [1, 5, 10, 25, 50, 100]
PCT_PRESETS = [1.0, 2.5, 5.0, 10.0]

# ---------------------------------------------------------------------------
# State (per chat) Persistence
# ---------------------------------------------------------------------------
STATE_FILE = "state.json"


@dataclass
class ChatState:
    mode: str = "USD"  # USD or PCT
    usd_size: int = 5
    pct_size: float = 1.0
    last_pair: Optional[str] = None
    last_tf: Optional[str] = None
    last_theme: Optional[str] = None

    def current_size_display(self) -> str:
        return f"${self.usd_size}" if self.mode == "USD" else f"{self.pct_size:.1f}%"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_chat_state: Dict[int, ChatState] = {}


def load_state():
    if not os.path.exists(STATE_FILE):
        logger.info("No state file found; starting fresh.")
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for k, v in raw.items():
            try:
                _chat_state[int(k)] = ChatState(**v)
            except Exception:
                continue
        logger.info("Loaded state for %d chats.", len(_chat_state))
    except Exception as e:
        logger.warning("State load failed: %s", e)


def save_state():
    try:
        raw = {str(k): v.to_dict() for k, v in _chat_state.items()}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)
    except Exception as e:
        logger.warning("State save failed: %s", e)


def get_state(chat_id: int) -> ChatState:
    st = _chat_state.get(chat_id)
    if not st:
        st = ChatState()
        _chat_state[chat_id] = st
    return st


# ---------------------------------------------------------------------------
# Normalization Helpers
# ---------------------------------------------------------------------------
def norm_theme(val: Optional[str]) -> str:
    if not val:
        return DEFAULT_THEME
    return "light" if val.lower().startswith("l") else "dark"


def norm_interval(tf: Optional[str]) -> str:
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


# ---------------------------------------------------------------------------
# Symbol Resolution
# ---------------------------------------------------------------------------
def _classify_symbol(raw: str) -> str:
    """Return asset class hint: FX, OTC, CRYPTO, INDEX, METAL, UNKNOWN."""
    s = raw.upper()
    if "-OTC" in s:
        return "OTC"
    if s in {p.upper() for p in FX_PAIRS}:
        return "FX"
    if s in {p.upper() for p in CRYPTO_PAIRS}:
        return "CRYPTO"
    if s in {p.upper() for p in INDEX_PAIRS}:
        return "INDEX"
    if s in {"XAUUSD", "XAGUSD", "XAU/USD", "XAG/USD"}:
        return "METAL"
    return "UNKNOWN"


def resolve_symbol(raw: str) -> Tuple[str, str, bool, List[str]]:
    """
    Return (exchange, ticker, is_otc, alt_exchanges).
    """
    if not raw:
        return DEFAULT_EXCHANGE, "EURUSD", False, []

    s = raw.strip().upper()
    is_otc = "-OTC" in s

    if ":" in s:  # explicit
        ex, tk = s.split(":", 1)
        return ex, re.sub(r"[^A-Z0-9]", "", tk), is_otc, []

    # Known direct lists
    if is_otc and raw in _OTC_UNDERLYING:
        # we route OTC to QUOTEX (or alt feed) for snapshot fallback
        tk = _OTC_UNDERLYING[raw]
        return "QUOTEX", tk, True, [DEFAULT_EXCHANGE, "FX", "FX_IDC", "OANDA", "FOREXCOM"]

    # Try FX
    canon = _canon_key(raw)
    if canon.endswith("OTC"):
        for k, v in _OTC_UNDERLYING.items():
            if _canon_key(k) == canon:
                tk = v
                return "QUOTEX", tk, True, [DEFAULT_EXCHANGE, "FX", "FX_IDC", "OANDA", "FOREXCOM"]

    if canon in (p.replace("/", "") for p in FX_PAIRS):
        tk = canon
        return DEFAULT_EXCHANGE, tk, False, ["FX", "FX_IDC", "OANDA", "FOREXCOM", "IDC"]

    # Crypto
    if raw.upper() in {p.upper() for p in CRYPTO_PAIRS}:
        tk = raw.upper().replace("/", "")
        return "CRYPTO", tk, False, CRYPTO_FALLBACKS

    # Index
    if raw.upper() in {p.upper() for p in INDEX_PAIRS}:
        tk = raw.upper().replace("/", "")
        return "INDEX", tk, False, INDEX_FALLBACKS

    # Metal
    if raw.upper() in {"XAU/USD", "XAUUSD"}:
        return "METALS", "XAUUSD", False, METAL_FALLBACKS
    if raw.upper() in {"XAG/USD", "XAGUSD"}:
        return "METALS", "XAGUSD", False, METAL_FALLBACKS

    # fallback guess
    tk = re.sub(r"[^A-Z0-9]", "", s)
    return DEFAULT_EXCHANGE, tk, is_otc, ["FX", "FX_IDC", "OANDA", "FOREXCOM", "IDC"]


# ---------------------------------------------------------------------------
# Snapshot Fetchers
# ---------------------------------------------------------------------------
def _snapshot_url_path(pair: str, tf: str, theme: str, params: Dict[str, str] | None = None) -> str:
    """
    Build /snapshot/:pair URL (pair path-escaped).
    Additional query params optional.
    """
    qp = {"tf": tf, "theme": theme}
    if params:
        qp.update(params)
    # safe pair path: percent encode, but keep colon? server supports encoded colon
    safe_pair = quote(pair, safe="")
    return f"{SNAPSHOT_BASE_URL}/snapshot/{safe_pair}?{urlencode(qp)}"


def _run_url(exchange: str, ticker: str, interval: str, theme: str) -> str:
    return (
        f"{SNAPSHOT_BASE_URL}/run?"
        f"base=chart&exchange={quote(exchange)}&ticker={quote(ticker)}&interval={quote(interval)}&theme={quote(theme)}"
    )


def _attempt_get(url: str) -> httpx.Response:
    global_throttle_wait()
    return _http.get(url)


def _valid_png(resp: httpx.Response, min_bytes: int = 2048) -> Optional[bytes]:
    if resp.status_code != 200:
        return None
    ctype = resp.headers.get("Content-Type", "")
    if "image" not in ctype.lower():
        return None
    data = resp.content
    if len(data) < min_bytes:
        return None
    if not data.startswith(b"\x89PNG"):
        return None
    return data


def fetch_snapshot_png_best(pair: str, tf: str, theme: str, exchange: str, ticker: str, alts: List[str]) -> Tuple[bytes, str]:
    """
    Try /snapshot/:pair first; if good PNG return it.
    Else try /run across exchange & alts. Return (png, exchange_used).
    Raises RuntimeError if all fail.
    """
    # 1) snapshot
    snap_url = _snapshot_url_path(pair, tf, theme)
    try:
        resp = _attempt_get(snap_url)
        png = _valid_png(resp)
        if png:
            logger.info("Snapshot success via /snapshot (%s, %d bytes).", pair, len(png))
            return png, f"SNAP:{pair}"
        else:
            logger.warning("Snapshot /snapshot failed (%s): %s %s", pair, resp.status_code, resp.text[:100])
    except Exception as e:
        logger.warning("Snapshot /snapshot error %s: %s", pair, e)

    # 2) fallback /run across exchange cascade
    tried = []
    last_err = ""
    cascade = [exchange] + alts
    # always include DEFAULT_EXCHANGE final
    if DEFAULT_EXCHANGE not in cascade:
        cascade.append(DEFAULT_EXCHANGE)
    for ex in cascade:
        url = _run_url(ex, ticker, tf, theme)
        tried.append(ex)
        try:
            resp = _attempt_get(url)
            png = _valid_png(resp)
            if png:
                logger.info("Snapshot success via /run %s:%s (%d bytes).", ex, ticker, len(png))
                return png, ex
            last_err = f"HTTP {resp.status_code}: {resp.text[:120]}"
            logger.warning("Snapshot fail %s:%s -> %s", ex, ticker, last_err)
        except Exception as e:
            last_err = str(e)
            logger.warning("Snapshot error %s:%s -> %s", ex, ticker, e)

    raise RuntimeError(f"All exchanges failed for {ticker}. Last error: {last_err}. Tried: {tried}")


async def async_fetch_snapshot_png_best(pair: str, tf: str, theme: str, exchange: str, ticker: str, alts: List[str]) -> Tuple[bytes, str]:
    return await asyncio.to_thread(fetch_snapshot_png_best, pair, tf, theme, exchange, ticker, alts)


# ---------------------------------------------------------------------------
# Snapshot JSON (for /analyze)
# ---------------------------------------------------------------------------
def fetch_snapshot_json(pair: str, tf: str, theme: str, candles: int = 100) -> Optional[Dict[str, Any]]:
    url = _snapshot_url_path(pair, tf, theme, params={"fmt": "json", "candles": str(candles)})
    try:
        resp = _attempt_get(url)
        if resp.status_code == 200:
            return resp.json()
        logger.warning("JSON snapshot fail %s: %s %s", pair, resp.status_code, resp.text[:100])
    except Exception as e:
        logger.warning("JSON snapshot error %s: %s", pair, e)
    return None


async def async_fetch_snapshot_json(pair: str, tf: str, theme: str, candles: int = 100) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(fetch_snapshot_json, pair, tf, theme, candles)


# ---------------------------------------------------------------------------
# Quick TA
# ---------------------------------------------------------------------------
def _extract_closes(candles: List[Dict[str, Any]]) -> List[float]:
    out = []
    for c in candles:
        try:
            out.append(float(c.get("close") or c.get("c") or 0))
        except Exception:
            out.append(0.0)
    return out


def ema(series: List[float], length: int) -> float:
    if not series:
        return float("nan")
    k = 2 / (length + 1)
    ema_val = series[0]
    for v in series[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val


def rsi(series: List[float], length: int = 14) -> float:
    if len(series) < length + 1:
        return float("nan")
    gains = []
    losses = []
    for i in range(1, length + 1):
        diff = series[-i] - series[-i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(-diff)
    avg_gain = sum(gains) / length
    avg_loss = sum(losses) / length
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def candle_direction(last_candle: Dict[str, Any]) -> Optional[str]:
    try:
        o = float(last_candle.get("open") or last_candle.get("o"))
        c = float(last_candle.get("close") or last_candle.get("c"))
    except Exception:
        return None
    if c > o:
        return "UP"
    if c < o:
        return "DOWN"
    return None


def analyze_candles(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Minimal TA: EMA(7), EMA(25), diff cross, RSI14, last candle dir.
    Suggest CALL if fast>slow & rsi>35; PUT if fast<slow & rsi<65; else flat.
    """
    closes = _extract_closes(candles)
    ema7 = ema(closes[-50:], 7) if len(closes) >= 7 else float("nan")
    ema25 = ema(closes[-100:], 25) if len(closes) >= 25 else float("nan")
    rsi14 = rsi(closes, 14)
    ldir = candle_direction(candles[-1]) if candles else None

    suggestion = "FLAT"
    if ema7 > ema25 and rsi14 > 35:
        suggestion = "CALL"
    elif ema7 < ema25 and rsi14 < 65:
        suggestion = "PUT"
    else:
        # fallback last candle bias
        if ldir == "UP":
            suggestion = "CALL"
        elif ldir == "DOWN":
            suggestion = "PUT"

    # expiry ranking: trending? longer; choppy? shorter
    if suggestion == "CALL" and ema7 > ema25 and abs(rsi14 - 50) > 10:
        expiries = ["5m", "15m", "3m", "1m"]
    elif suggestion == "PUT" and ema7 < ema25 and abs(rsi14 - 50) > 10:
        expiries = ["5m", "15m", "3m", "1m"]
    else:
        expiries = ["1m", "3m", "5m", "15m"]

    return {
        "ema7": ema7,
        "ema25": ema25,
        "rsi14": rsi14,
        "last_dir": ldir,
        "suggestion": suggestion,
        "expiries": expiries,
    }


# ---------------------------------------------------------------------------
# Inline Keyboard Builders
# ---------------------------------------------------------------------------
def chunk_list(lst: List[str], n: int) -> List[List[str]]:
    return [lst[i : i + n] for i in range(0, len(lst), n)]


def build_pairs_keyboard(category: str, page: int = 0, per_page: int = 9) -> InlineKeyboardMarkup:
    cat_map = {
        "FX": FX_PAIRS,
        "OTC": OTC_PAIRS,
        "CRYPTO": CRYPTO_PAIRS,
        "INDEX": INDEX_PAIRS,
        "METAL": METAL_PAIRS,
        "ALL": ALL_PAIRS,
    }
    items = cat_map.get(category, [])
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))

    start = page * per_page
    end = start + per_page
    page_items = items[start:end]

    rows: List[List[InlineKeyboardButton]] = []
    for p in page_items:
        rows.append([InlineKeyboardButton(p, callback_data=f"PAIR|{p}")])

    nav_row: List[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅", callback_data=f"PG|{category}|{page-1}"))
    nav_row.append(InlineKeyboardButton(f"{category} {page+1}/{total_pages}", callback_data="NOP"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡", callback_data=f"PG|{category}|{page+1}"))

    rows.append(nav_row)

    # category switch row
    rows.append([
        InlineKeyboardButton("FX", callback_data="CAT|FX"),
        InlineKeyboardButton("OTC", callback_data="CAT|OTC"),
        InlineKeyboardButton("CRYPTO", callback_data="CAT|CRYPTO"),
        InlineKeyboardButton("INDEX", callback_data="CAT|INDEX"),
        InlineKeyboardButton("METAL", callback_data="CAT|METAL"),
    ])

    return InlineKeyboardMarkup(rows)


def build_direction_keyboard(pair: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 CALL", callback_data=f"DIR|{pair}|CALL"),
            InlineKeyboardButton("🔴 PUT",  callback_data=f"DIR|{pair}|PUT"),
        ],
        [InlineKeyboardButton("🔍 Analyze", callback_data=f"ANZ|{pair}")],
    ])


def build_expiry_keyboard(pair: str, direction: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1m", callback_data=f"EXP|{pair}|{direction}|1m"),
            InlineKeyboardButton("3m", callback_data=f"EXP|{pair}|{direction}|3m"),
            InlineKeyboardButton("5m", callback_data=f"EXP|{pair}|{direction}|5m"),
            InlineKeyboardButton("15m", callback_data=f"EXP|{pair}|{direction}|15m"),
        ],
        [InlineKeyboardButton("↩ Back", callback_data=f"DIRSEL|{pair}")],
    ])


def build_size_keyboard(pair: str, direction: str, expiry: str, chat_id: int) -> InlineKeyboardMarkup:
    st = get_state(chat_id)
    rows: List[List[InlineKeyboardButton]] = []
    # USD row
    usd_buttons = []
    for amt in USD_PRESETS:
        usd_buttons.append(InlineKeyboardButton(f"${amt}", callback_data=f"SZ|{pair}|{direction}|{expiry}|USD|{amt}"))
    rows.append(usd_buttons)
    # PCT row
    pct_buttons = []
    for pct in PCT_PRESETS:
        pct_buttons.append(InlineKeyboardButton(f"{pct:g}%", callback_data=f"SZ|{pair}|{direction}|{expiry}|PCT|{pct}"))
    rows.append(pct_buttons)

    rows.append([InlineKeyboardButton("↩ Back", callback_data=f"EXPSEL|{pair}|{direction}")])
    return InlineKeyboardMarkup(rows)


def build_confirm_keyboard(pair: str, direction: str, expiry: str, mode: str, amt: Union[int, float]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm Trade", callback_data=f"CONF|{pair}|{direction}|{expiry}|{mode}|{amt}"),
        ],
        [
            InlineKeyboardButton("↩ Size", callback_data=f"EXP|{pair}|{direction}|{expiry}"),
            InlineKeyboardButton("✖ Cancel", callback_data="CANCEL"),
        ],
    ])


# ---------------------------------------------------------------------------
# Pocket Option / UI.Vision Hook
# ---------------------------------------------------------------------------
def pocket_trade(pair: str, direction: str, expiry: str, mode: str, amt: Union[int, float], chat_id: int) -> bool:
    """
    Fire UI.Vision (or other) webhook if configured.
    Returns True if accepted (HTTP 2xx), False otherwise.
    """
    if not UI_VISION_URL:
        logger.info("Pocket trade skipped (UI_VISION_URL not set).")
        return False

    payload = {
        "macro": UI_VISION_MACRO_NAME,
        "pair": pair,
        "direction": direction,
        "expiry": expiry,
        "mode": mode,
        "amount": amt,
        "chat_id": chat_id,
        "params": UI_VISION_MACRO_PARAMS_OBJ,
    }
    try:
        r = _http.post(UI_VISION_URL, json=payload)
        if r.status_code // 100 == 2:
            logger.info("Pocket trade accepted by UI.Vision endpoint.")
            return True
        logger.warning("Pocket trade HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        logger.warning("Pocket trade error: %s", e)
    return False


# ---------------------------------------------------------------------------
# Telegram Bot Command Handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = get_state(chat_id)
    msg = (
        f"Hi {update.effective_user.first_name or ''} 👋\n\n"
        f"*{APP_NAME}* v{BOT_VERSION}\n"
        f"_Default size:_ {st.current_size_display()}\n\n"
        "Commands:\n"
        "• /pairs – pick a symbol\n"
        "• /analyze SYMBOL [tf] [theme]\n"
        "• /snap SYMBOL [tf] [theme]\n"
        "• /trade SYMBOL CALL|PUT [expiry]\n"
        "• /size – change trade size\n"
        "• /help – usage\n"
    )
    await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN, reply_markup=build_pairs_keyboard("FX"))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📘 *Help*\n\n"
        "`/pairs` pick a symbol\n"
        "`/analyze EUR/USD 5 dark` quick TA\n"
        "`/snap EUR/USD 1 dark` chart only\n"
        "`/trade EUR/USD CALL 3m` trade flow\n"
        "`/size` choose $ or % presets\n\n"
        "Timeframes: minutes (# | 5m), D/W/M.\n"
        "Themes: dark|light.\n"
        "Pocket Option auto‑trade: configure UI_VISION_URL env.\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = get_state(chat_id)
    msg = f"Current size: {st.current_size_display()}\nPick a new preset:"
    # reuse size keyboard but we need placeholder pair
    kb = build_size_keyboard("EUR/USD", "CALL", "5m", chat_id)
    await update.message.reply_text(msg, reply_markup=kb)


async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Select a category / pair:", reply_markup=build_pairs_keyboard("FX"))


def _parse_snap_args(args: List[str]) -> Tuple[str, str, str]:
    symbol = args[0] if args else "EUR/USD"
    tf = DEFAULT_INTERVAL
    th = DEFAULT_THEME
    if len(args) >= 2 and args[1].lower() not in ("dark", "light"):
        tf = args[1]
    if len(args) >= 2 and args[-1].lower() in ("dark", "light"):
        th = args[-1]
    elif len(args) >= 3 and args[2].lower() in ("dark", "light"):
        th = args[2]
    return symbol, tf, th


async def cmd_snap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol, tf, th = _parse_snap_args(context.args)
    await do_send_snapshot(update.effective_chat.id, context, symbol, tf, th)


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol, tf, th = _parse_snap_args(context.args)
    tf = norm_interval(tf)
    th = norm_theme(th)
    res = await do_analyze_symbol(update.effective_chat.id, context, symbol, tf, th, send_chart=True)
    if not res:
        await context.bot.send_message(update.effective_chat.id, f"❌ Analyze failed for {symbol}.")


async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await context.bot.send_message(update.effective_chat.id, "Usage: /trade SYMBOL CALL|PUT [expiry]")
        return
    symbol = args[0]
    direction = parse_direction(args[1] if len(args) >= 2 else None) or "CALL"
    expiry = args[2] if len(args) >= 3 else "5m"
    kb = build_size_keyboard(symbol, direction, expiry, update.effective_chat.id)
    await context.bot.send_message(
        update.effective_chat.id,
        f"Trade {symbol} {direction} {expiry} — choose size:",
        reply_markup=kb,
    )


# ---------------------------------------------------------------------------
# Command Implementations
# ---------------------------------------------------------------------------
async def do_send_snapshot(chat_id: int, context: ContextTypes.DEFAULT_TYPE, symbol: str, tf: str, th: str):
    if rate_limited(chat_id):
        await context.bot.send_message(chat_id, "⏳ Too many requests; please wait…")
        return

    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)

    ex, tk, _is_otc, alts = resolve_symbol(symbol)
    tf = norm_interval(tf)
    th = norm_theme(th)

    try:
        png, used = await async_fetch_snapshot_png_best(symbol, tf, th, ex, tk, alts)
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=png,
            caption=f"{used}:{tk} • TF {tf} • {th}",
        )
        st = get_state(chat_id)
        st.last_pair = symbol
        st.last_tf = tf
        st.last_theme = th
        save_state()
    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ Failed: {symbol} ({e})")


async def do_analyze_symbol(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    symbol: str,
    tf: str,
    th: str,
    send_chart: bool = False,
) -> Optional[Dict[str, Any]]:
    tf = norm_interval(tf)
    th = norm_theme(th)

    data = await async_fetch_snapshot_json(symbol, tf, th, candles=150)
    candles = data.get("candles") if data else None

    if not candles or len(candles) < 5:
        logger.warning("No candles for %s; fallback chart only.", symbol)
        if send_chart:
            await do_send_snapshot(chat_id, context, symbol, tf, th)
        return None

    ta = analyze_candles(candles)
    # build message
    msg = (
        f"🔍 *Analysis* {symbol}\n"
        f"EMA7: {ta['ema7']:.5f}  EMA25: {ta['ema25']:.5f}\n"
        f"RSI14: {ta['rsi14']:.1f}  LastCandle: {ta['last_dir']}\n"
        f"➡ Suggest: *{ta['suggestion']}*\n"
        f"Expiries: {', '.join(ta['expiries'])}\n"
    )
    kb = build_direction_keyboard(symbol)
    await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    if send_chart:
        await do_send_snapshot(chat_id, context, symbol, tf, th)

    return ta


# ---------------------------------------------------------------------------
# CallbackQuery Handler
# ---------------------------------------------------------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""

    if data == "NOP":
        return
    if data == "CANCEL":
        await q.edit_message_reply_markup(reply_markup=None)
        return

    if data.startswith("CAT|"):
        _, cat = data.split("|", 1)
        await q.edit_message_text(
            f"Select {cat} pair:",
            reply_markup=build_pairs_keyboard(cat, page=0),
        )
        return

    if data.startswith("PG|"):
        _, cat, pg = data.split("|", 2)
        page = int(pg)
        await q.edit_message_reply_markup(reply_markup=build_pairs_keyboard(cat, page))
        return

    if data.startswith("PAIR|"):
        _, pair = data.split("|", 1)
        await q.edit_message_text(
            f"{pair}\nChoose direction or analyze.",
            reply_markup=build_direction_keyboard(pair),
        )
        return

    if data.startswith("DIRSEL|"):
        _, pair = data.split("|", 1)
        # return to direction keyboard
        await q.edit_message_reply_markup(reply_markup=build_direction_keyboard(pair))
        return

    if data.startswith("ANZ|"):
        _, pair = data.split("|", 1)
        await q.edit_message_text(f"Analyzing {pair}…")
        tf = DEFAULT_INTERVAL
        th = DEFAULT_THEME
        await do_analyze_symbol(q.message.chat.id, context, pair, tf, th, send_chart=True)
        return

    if data.startswith("DIR|"):
        _, pair, direction = data.split("|", 2)
        await q.edit_message_text(
            f"{pair} {direction}\nPick expiry:",
            reply_markup=build_expiry_keyboard(pair, direction),
        )
        return

    if data.startswith("EXPSEL|"):
        _, pair, direction = data.split("|", 2)
        await q.edit_message_reply_markup(reply_markup=build_expiry_keyboard(pair, direction))
        return

    if data.startswith("EXP|"):
        _, pair, direction, expiry = data.split("|", 3)
        chat_id = q.message.chat.id
        await q.edit_message_text(
            f"{pair} {direction} {expiry}\nPick size:",
            reply_markup=build_size_keyboard(pair, direction, expiry, chat_id),
        )
        return

    if data.startswith("SZ|"):
        _, pair, direction, expiry, mode, amt = data.split("|", 5)
        chat_id = q.message.chat.id
        st = get_state(chat_id)
        if mode == "USD":
            st.mode = "USD"
            st.usd_size = int(float(amt))
            disp = f"${st.usd_size}"
        else:
            st.mode = "PCT"
            st.pct_size = float(amt)
            disp = f"{st.pct_size:g}%"
        save_state()
        await q.edit_message_text(
            f"{pair} {direction} {expiry}\nSize: {disp}\nConfirm?",
            reply_markup=build_confirm_keyboard(pair, direction, expiry, mode, amt),
        )
        return

    if data.startswith("CONF|"):
        _, pair, direction, expiry, mode, amt = data.split("|", 5)
        chat_id = q.message.chat.id
        # Trade dispatch
        ok = pocket_trade(pair, direction, expiry, mode, float(amt), chat_id)
        msg = f"✅ Trade sent: {pair} {direction} {expiry} {amt}{mode}\n" if ok else \
              f"⚠ Trade queued (no broker hook) {pair} {direction} {expiry} {amt}{mode}"
        await q.edit_message_text(msg)
        return


# ---------------------------------------------------------------------------
# Text Echo with Quick Parse
# ---------------------------------------------------------------------------
_trade_re = re.compile(r"(?i)^\s*trade\s+([A-Z0-9/_:-]+)\s+(call|put|buy|sell|up|down)\s+([0-9]+m?)")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    m = _trade_re.match(txt)
    if m:
        pair, dir_word, exp = m.group(1), m.group(2), m.group(3)
        direction = parse_direction(dir_word) or "CALL"
        kb = build_size_keyboard(pair, direction, exp, update.effective_chat.id)
        await update.message.reply_text(f"Trade {pair} {direction} {exp} — choose size:", reply_markup=kb)
        return
    await update.message.reply_text("I didn't get that. Try `/trade EUR/USD CALL 5m`.", parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Flask TV Webhook
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)


def _parse_tv_payload(data: Dict[str, Any]) -> Dict[str, str]:
    d = {}
    d["chat_id"] = str(data.get("chat_id") or DEFAULT_CHAT_ID or "")
    d["pair"] = str(data.get("pair") or data.get("symbol") or data.get("ticker") or "EUR/USD")
    d["direction"] = str(data.get("direction") or "CALL").upper()
    d["expiry"] = str(data.get("default_expiry_min") or data.get("expiry") or "5m")
    d["strategy"] = str(data.get("strategy") or "")
    d["winrate"] = str(data.get("winrate") or "")
    d["timeframe"] = str(data.get("timeframe") or data.get("tf") or DEFAULT_INTERVAL)
    d["theme"] = str(data.get("theme") or DEFAULT_THEME)
    return d


def _tv_send_alert(payload: Dict[str, str]):
    """
    Fire Telegram alert (no context). Used by Flask thread.
    """
    chat_id = payload["chat_id"]
    raw_pair = payload["pair"]
    direction = parse_direction(payload["direction"]) or "CALL"
    expiry = payload["expiry"]
    strat = payload["strategy"]
    winrate = payload["winrate"]
    tf = norm_interval(payload["timeframe"])
    theme = norm_theme(payload["theme"])
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"

    msg = (
        f"🔔 *TV Alert*\n"
        f"Pair: {raw_pair}\n"
        f"Direction: {arrow} {direction}\n"
        f"Expiry: {expiry}\n"
        f"Strategy: {strat}\n"
        f"WinRate: {winrate}\n"
        f"TF {tf} • {theme}"
    )

    _tg_send_message(chat_id, msg, parse_mode="Markdown")

    # chart
    ex, tk, _is_otc, alts = resolve_symbol(raw_pair)
    try:
        png, used = fetch_snapshot_png_best(raw_pair, tf, theme, ex, tk, alts)
        _tg_send_photo(chat_id, png, caption=f"{used}:{tk} • TF {tf} • {theme}")
    except Exception as e:
        _tg_send_message(chat_id, f"⚠ Chart snapshot failed for {raw_pair}: {e}")

    # trade button message
    kb = build_direction_keyboard(raw_pair)
    _tg_send_message(chat_id, "Tap to Trade:", reply_markup=kb, parse_mode=None)


def _tg_send_message(chat_id: str, text: str, parse_mode: Optional[str] = None, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup.to_dict()
    try:
        _http.post(url, json=payload, timeout=30)
    except Exception as e:
        logger.warning("tg_send_message error: %s", e)


def _tg_send_photo(chat_id: str, png: bytes, caption: str = ""):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", png, "image/png")}
    data = {"chat_id": chat_id, "caption": caption}
    try:
        _http.post(url, data=data, files=files, timeout=60)
    except Exception as e:
        logger.warning("tg_send_photo error: %s", e)


@flask_app.post("/tv")
def tv_route():
    if WEBHOOK_SECRET:
        hdr = request.headers.get("X-Webhook-Token", "")
        body_secret = str(request.json.get("secret") or request.json.get("token") or "")
        if hdr != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "unauthorized"}), 403
    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    payload = _parse_tv_payload(data)
    threading.Thread(target=_tv_send_alert, args=(payload,), daemon=True).start()
    return jsonify({"ok": True}), 200


# backward compat
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


# ---------------------------------------------------------------------------
# Application Builder (telegram)
# ---------------------------------------------------------------------------
def build_application() -> Application:
    # Using Application.builder() directly (avoids Updater bug seen w/ mismatched PTB installs)
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True)
    try:
        # optional rate limiter if installed w/ extras
        builder.rate_limiter(AIORateLimiter())
    except Exception:
        pass
    app = builder.build()

    # Handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("size", cmd_size))
    app.add_handler(CommandHandler("pairs", cmd_pairs))
    app.add_handler(CommandHandler("snap", cmd_snap))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("trade", cmd_trade))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print(
        f"Bot starting… BASE_URL={SNAPSHOT_BASE_URL} | DefaultEX={DEFAULT_EXCHANGE} | WebhookPort={TV_WEBHOOK_PORT} | "
        f"UI_VISION_URL={UI_VISION_URL} | AUTO_TRADE_FROM_TV={'True' if UI_VISION_URL else 'False'} | SIM_DEBIT=False"
    )
    load_state()
    start_flask_background()

    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
