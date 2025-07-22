#!/usr/bin/env python
"""
TradingView → Telegram Snapshot Bot (Inline / Analyze / Pocket-Option Edition)
==========================================================================

Major capabilities
------------------
• Async python-telegram-bot v21+ (no legacy Updater crash).
• Inline keyboards: browse FX / OTC / Indices / Crypto; tap to snapshot, analyze, or trade.
• /snap, /snapmulti, /snapall, /pairs, /trade, /analyze, /next, /help.
• Auto pair resolution across multiple TradingView exchanges (FX, FX_IDC, OANDA, FOREXCOM, IDC, QUOTEX, CURRENCY).
• Snapshot backend primary: /snapshot/<symbol> (expects PNG). Fallback: /run?exchange=...&ticker=....
• Accepts PNG even if server responds text/plain (some Render reverse proxies mis-set headers).
• Safe logging: never dump binary bytes to console; truncate & sanitize high-bit chars.
• Simple TA analyzer: uses last candle JSON (fmt=json&candles=1) plus fast/slow EMA slope to suggest CALL/PUT/NEUTRAL + recommended expiries (1m/3m/5m/15m).
• Trade-size presets ($1..$100 & 1%..100%) and trade-mode state per chat.
• Optional UI.Vision webhook trigger to automate Pocket Option web UI click trading.
• TradingView webhook (/tv, /webhook) -> Telegram alert + (optional) auto-trade via UI.Vision.
• Rotating log file.
• Basic JSON state persistence (per-chat defaults & last trade data).

NOTE: This bot **does not directly integrate with Pocket Option API** (no public API). Instead it
can call a local UI.Vision macro endpoint that automates the broker website.

-----------------------------------------------------------------------
ENV VARS (all optional except TELEGRAM_BOT_TOKEN)
-----------------------------------------------------------------------
TELEGRAM_BOT_TOKEN  = <required>
TELEGRAM_CHAT_ID    = default chat to send alerts when a TV webhook has no chat_id
SNAPSHOT_BASE_URL   = http://localhost:10000 (Node/Puppeteer snapshot server)
DEFAULT_EXCHANGE    = FX (symbol prefix fallback)
DEFAULT_INTERVAL    = 1 (minutes)  | Accepts number or D/W/M
DEFAULT_THEME       = dark         | dark|light
TV_WEBHOOK_PORT     = 8081         | Flask port
TV_WEBHOOK_URL      = Optional; informational/for inline copy text
UI_VISION_URL       = http://localhost:8080/pocket-trade  (optional; POST JSON to run macro)
UI_VISION_MACRO_NAME= PocketTrade  (macro name)
UI_VISION_MACRO_PARAMS = {"login":"user"}  (additional macro params JSON string)
AUTO_TRADE_FROM_TV  = 0/1          | if 1, auto-fire UI.Vision on TV alerts
SIM_DEBIT           = 0/1          | if 1, don’t actually call UI.Vision; log instead
STATE_FILE          = tvsnap_state.json (override path)
LOG_FILE            = logs/tvsnapshotbot.log

-----------------------------------------------------------------------
SERVER ENDPOINT EXPECTATIONS
-----------------------------------------------------------------------
Primary:   GET /snapshot/<symbol>?tf=1&theme=dark[&fmt=png|json][&candles=N]
Fallback:  GET /run?exchange=FX&ticker=EURUSD&interval=1&theme=dark
Health:    GET /healthz (optional)
Warmup:    GET /start-browser (optional)

/snapshot should return PNG bytes on success (image/png) >=2KB.
If fmt=json, should return JSON {candles:[{t,o,h,l,c},...], ...}.

-----------------------------------------------------------------------
LICENSE: You own your modifications. This scaffold is provided as-is.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from functools import partial
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
from flask import Flask, jsonify, request

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    InputMediaPhoto,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Global Config / Env
# ---------------------------------------------------------------------------

def _env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name, default)
    if v is None:
        return None
    # strip matching double or single quotes user might have put in .env
    v = v.strip().strip("'\"")
    return v or default

TOKEN: str = _env_str("TELEGRAM_BOT_TOKEN", "") or ""
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment.")

DEFAULT_CHAT_ID: str = _env_str("TELEGRAM_CHAT_ID", "") or ""
BASE_URL: str = _env_str("SNAPSHOT_BASE_URL", "http://localhost:10000") or "http://localhost:10000"
DEFAULT_EXCHANGE: str = _env_str("DEFAULT_EXCHANGE", "FX") or "FX"
DEFAULT_INTERVAL: str = _env_str("DEFAULT_INTERVAL", "1") or "1"
DEFAULT_THEME: str = (_env_str("DEFAULT_THEME", "dark") or "dark").lower()
TV_WEBHOOK_PORT: int = int(_env_str("TV_WEBHOOK_PORT", "8081") or 8081)
TV_WEBHOOK_URL: Optional[str] = _env_str("TV_WEBHOOK_URL")
UI_VISION_URL: Optional[str] = _env_str("UI_VISION_URL")
UI_VISION_MACRO_NAME: str = _env_str("UI_VISION_MACRO_NAME", "PocketTrade") or "PocketTrade"
UI_VISION_MACRO_PARAMS_RAW: str = _env_str("UI_VISION_MACRO_PARAMS", "{}").strip() or "{}"
AUTO_TRADE_FROM_TV: bool = bool(int(_env_str("AUTO_TRADE_FROM_TV", "0") or 0))
SIM_DEBIT: bool = bool(int(_env_str("SIM_DEBIT", "0") or 0))
STATE_FILE: str = _env_str("STATE_FILE", "tvsnap_state.json") or "tvsnap_state.json"
LOG_FILE: str = _env_str("LOG_FILE", os.path.join("logs", "tvsnapshotbot.log")) or os.path.join("logs", "tvsnapshotbot.log")
WEBHOOK_SECRET: Optional[str] = _env_str("WEBHOOK_SECRET")

# Presets
TRADE_SIZE_PRESETS_DOLLAR = [1, 5, 10, 25, 50, 100]
TRADE_SIZE_PRESETS_PERCENT = [1, 2, 5, 10, 25, 50, 100]
TRADE_EXPIRY_PRESETS = ["1m", "3m", "5m", "15m"]

# Additional categories (indices, crypto) – placeholder tickers; adjust to taste.
INDEX_PAIRS = [
    "US500",  # S&P 500
    "NAS100", # Nasdaq
    "DE30",   # DAX
    "UK100",  # FTSE
]
CRYPTO_PAIRS = [
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "XRP/USD",
]

FX_PAIRS = [
    "EUR/USD","GBP/USD","USD/JPY","USD/CHF","AUD/USD",
    "NZD/USD","USD/CAD","EUR/GBP","EUR/JPY","GBP/JPY",
    "AUD/JPY","NZD/JPY","EUR/AUD","GBP/AUD","EUR/CAD",
    "USD/MXN","USD/TRY","USD/ZAR","AUD/CHF","EUR/CHF",
]

OTC_PAIRS = [
    "EUR/USD-OTC","GBP/USD-OTC","USD/JPY-OTC","USD/CHF-OTC","AUD/USD-OTC",
    "NZD/USD-OTC","USD/CAD-OTC","EUR/GBP-OTC","EUR/JPY-OTC","GBP/JPY-OTC",
    "AUD/CHF-OTC","EUR/CHF-OTC","KES/USD-OTC","MAD/USD-OTC",
    "USD/BDT-OTC","USD/MXN-OTC","USD/MYR-OTC","USD/PKR-OTC",
]

ALL_PAIRS = FX_PAIRS + OTC_PAIRS + INDEX_PAIRS + CRYPTO_PAIRS

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _safe_text(obj: Any, maxlen: int = 200) -> str:
    s = str(obj)
    if len(s) > maxlen:
        s = s[:maxlen] + "…"
    # replace non-printables
    return s.encode("utf-8", "replace").decode("utf-8", "replace")

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
_stream_handler = logging.StreamHandler(stream=sys.stdout)
_stream_handler.setLevel(logging.INFO)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[_file_handler, _stream_handler],
)
logger = logging.getLogger("TVSnapBot")
logger.info(
    "Bot starting… BASE_URL=%s | DefaultEX=%s | WebhookPort=%s | UI_VISION_URL=%s | AUTO_TRADE_FROM_TV=%s | SIM_DEBIT=%s",
    BASE_URL, DEFAULT_EXCHANGE, TV_WEBHOOK_PORT, UI_VISION_URL, AUTO_TRADE_FROM_TV, SIM_DEBIT,
)

# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------
_http = requests.Session()
_http.headers.update({"User-Agent": "TVSnapBot/inline+analyze"})

# ---------------------------------------------------------------------------
# State (per chat defaults)
# ---------------------------------------------------------------------------

@dataclass
class ChatState:
    last_pair: Optional[str] = None        # display name
    last_expiry: str = "5m"
    size_mode: str = "$"                 # "$" or "%"
    size_value: float = 1.0               # amount or percent
    auto_trade: bool = False              # auto send to UI.Vision after analyze

@dataclass
class GlobalState:
    chats: Dict[str, ChatState] = field(default_factory=dict)

STATE = GlobalState()


def load_state() -> None:
    if not os.path.exists(STATE_FILE):
        logger.info("No state file found; starting fresh.")
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:  # noqa: BLE001
        logger.warning("State load failed: %s", e)
        return
    for cid, data in raw.get("chats", {}).items():
        STATE.chats[cid] = ChatState(**data)
    logger.info("Loaded state for %d chats.", len(STATE.chats))


def save_state() -> None:
    try:
        raw = {"chats": {cid: vars(cs) for cid, cs in STATE.chats.items()}}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)
    except Exception as e:  # noqa: BLE001
        logger.error("State save error: %s", e)


def get_chat_state(chat_id: int | str) -> ChatState:
    cid = str(chat_id)
    cs = STATE.chats.get(cid)
    if cs is None:
        cs = ChatState()
        STATE.chats[cid] = cs
    return cs

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
LAST_SNAPSHOT_PER_CHAT: Dict[int, float] = {}
RATE_LIMIT_SECONDS = 3.0
GLOBAL_LAST_SNAPSHOT = 0.0
GLOBAL_MIN_GAP = 0.75  # sec


def rate_limited(chat_id: int) -> bool:
    now = time.time()
    last = LAST_SNAPSHOT_PER_CHAT.get(chat_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    LAST_SNAPSHOT_PER_CHAT[chat_id] = now
    return False


def global_throttle_wait() -> None:
    global GLOBAL_LAST_SNAPSHOT  # noqa: PLW0603
    now = time.time()
    gap = now - GLOBAL_LAST_SNAPSHOT
    if gap < GLOBAL_MIN_GAP:
        time.sleep(GLOBAL_MIN_GAP - gap)
    GLOBAL_LAST_SNAPSHOT = time.time()

# ---------------------------------------------------------------------------
# Pair Mapping Infrastructure
# ---------------------------------------------------------------------------

# A more structured mapping that includes exchange + ticker + alt fallback list.
# Display names are exactly as shown to users.
PairInfo = Tuple[str, str, List[str]]  # (primary_exchange, ticker, alt_exchanges)

_pair_map: Dict[str, PairInfo] = {}


def _canon_key(pair: str) -> str:
    return pair.strip().upper().replace(" ", "").replace("/", "")


# Build map for FX (primary = user DEFAULT_EXCHANGE but we also include real FX fallback list)
_DEFAULT_FX_FALLBACKS = ["FX_IDC", "OANDA", "FOREXCOM", "IDC", "QUOTEX", "CURRENCY"]

for p in FX_PAIRS:
    tk = p.replace("/", "")
    _pair_map[_canon_key(p)] = (DEFAULT_EXCHANGE.upper(), tk, _DEFAULT_FX_FALLBACKS)

# OTC pairs: we show label but trade underlying major;
# set primary exchange to QUOTEX (or DEFAULT_EXCHANGE?) We'll pick QUOTEX; alt uses FX fallbacks.
_OTC_UNDER = {
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
for p, tk in _OTC_UNDER.items():
    _pair_map[_canon_key(p)] = ("QUOTEX", tk, _DEFAULT_FX_FALLBACKS)

# Indices (rough guess exchanges) – adjust to actual TV symbols you want.
_INDEX_UNDER = {
    "US500":    ("CME_MINI", "ES1!"),   # alt futures symbol; just example
    "NAS100":   ("CME_MINI", "NQ1!"),
    "DE30":     ("EUREX", "FDAX1!"),
    "UK100":    ("CURRENCY", "UK100"),
}
for p, (ex, tk) in _INDEX_UNDER.items():
    _pair_map[_canon_key(p)] = (ex, tk, ["INDEX", "CURRENCY", "FX_IDC"])

# Crypto (TV usually has BINANCE:BTCUSDT style; we’ll try CRYPTOCAP fallback.)
_CRYPTO_UNDER = {
    "BTC/USD": ("BINANCE", "BTCUSDT"),
    "ETH/USD": ("BINANCE", "ETHUSDT"),
    "SOL/USD": ("BINANCE", "SOLUSDT"),
    "XRP/USD": ("BINANCE", "XRPUSDT"),
}
for p, (ex, tk) in _CRYPTO_UNDER.items():
    _pair_map[_canon_key(p)] = (ex, tk, ["BINANCE", "CRYPTO", "CRYPTOCAP", "KRAKEN"])


# Resolution ----------------------------------------------------------

def resolve_symbol(raw: str) -> Tuple[str, str, List[str], str]:
    """Return (exchange, ticker, alt_exchanges, display_name).

    Accepts 'EUR/USD', 'FX:EURUSD', 'EURUSD', 'EUR/USD-OTC', etc.
    """
    if not raw:
        return DEFAULT_EXCHANGE.upper(), "EURUSD", _DEFAULT_FX_FALLBACKS, "EUR/USD"

    s = raw.strip()
    display = s
    s_up = s.upper()

    if ":" in s_up:  # explicit EX:TK wins
        ex, tk = s_up.split(":", 1)
        return ex, tk, _DEFAULT_FX_FALLBACKS, display

    key = _canon_key(s_up)
    if key in _pair_map:
        ex, tk, alt = _pair_map[key]
        return ex, tk, alt, display

    # fallback guess – strip non-alnum
    tk = re.sub(r"[^A-Z0-9]+", "", s_up)
    return DEFAULT_EXCHANGE.upper(), tk, _DEFAULT_FX_FALLBACKS, display


# ---------------------------------------------------------------------------
# Interval & Theme Normalization
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


def norm_theme(val: str) -> str:
    return "light" if (val and val.lower().startswith("l")) else "dark"

# ---------------------------------------------------------------------------
# Snapshot HTTP Helpers
# ---------------------------------------------------------------------------

_MIN_VALID_PNG = 2048  # bytes


def _looks_like_png(b: bytes) -> bool:
    return len(b) >= 8 and b[:8] == b"\x89PNG\r\n\x1a\n"


def fetch_snapshot_png_http(url: str, timeout: int = 75) -> Tuple[bool, Optional[bytes], str]:
    """Attempt GET and decide if we got an image.

    Returns (ok, bytes|None, errmsg).
    ok == True when we think we got a valid PNG (content-type image/ OR header mismatch but data looks PNG & size>_MIN_VALID_PNG).
    """
    try:
        global_throttle_wait()
        r = _http.get(url, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return False, None, _safe_text(e)

    ct = r.headers.get("Content-Type", "")
    if r.status_code == 200:
        data = r.content
        if (ct.startswith("image") or _looks_like_png(data)) and len(data) >= _MIN_VALID_PNG:
            return True, data, ""
        return False, None, f"200 but not image (len={len(data)})"

    # When error, still capture snippet for log
    err = f"HTTP {r.status_code}: {_safe_text(r.text)}"
    return False, None, err


async def warm_browser_async() -> None:
    await asyncio.to_thread(warm_browser_sync)


def warm_browser_sync() -> None:
    try:
        _http.get(f"{BASE_URL}/start-browser", timeout=10)
    except Exception as e:  # noqa: BLE001
        logger.debug("start-browser failed: %s", e)


async def fetch_snapshot_png_any(exchange: str, ticker: str, interval: str, theme: str, alt_exchanges: Optional[Sequence[str]] = None) -> Tuple[bytes, str]:
    """Try /snapshot first, then /run across fallback exchanges.

    Returns (png_bytes, exchange_used). Raises RuntimeError on total failure.
    """
    alt = list(alt_exchanges or [])

    # 1) try /snapshot/<exchange:ticker>
    symbol_combo = f"{exchange}:{ticker}"
    url = f"{BASE_URL}/snapshot/{symbol_combo}?tf={interval}&theme={theme}"
    ok, png, err = await asyncio.to_thread(fetch_snapshot_png_http, url)
    if ok and png:
        logger.info("Snapshot success via %s", symbol_combo)
        return png, exchange
    logger.warning("Snapshot %s failed -> %s", symbol_combo, err)

    # 2) try /snapshot/<ticker> (unqualified)
    url2 = f"{BASE_URL}/snapshot/{ticker}?tf={interval}&theme={theme}"
    ok, png, err = await asyncio.to_thread(fetch_snapshot_png_http, url2)
    if ok and png:
        logger.info("Snapshot success via ticker-only %s", ticker)
        return png, exchange
    logger.warning("Snapshot %s (ticker-only) failed -> %s", ticker, err)

    # 3) fallback /run across alt exchanges + known list
    fallback_list = list(alt) + ["FX", "FX_IDC", "OANDA", "FOREXCOM", "IDC", "QUOTEX", "CURRENCY"]
    tried = []
    last_err = None
    for ex in fallback_list:
        tried.append(ex)
        run_url = f"{BASE_URL}/run?exchange={ex}&ticker={ticker}&interval={interval}&theme={theme}"
        ok, png, err = await asyncio.to_thread(fetch_snapshot_png_http, run_url)
        if ok and png:
            logger.info("Snapshot success /run via %s:%s", ex, ticker)
            return png, ex
        last_err = err
        logger.warning("Snapshot failed %s:%s -> %s", ex, ticker, err)
        await asyncio.sleep(1.0)

    raise RuntimeError(f"All exchanges failed for {ticker}. Last error: {last_err}. Tried: {tried}")


async def fetch_snapshot_json_any(exchange: str, ticker: str, interval: str, candles: int = 1) -> Optional[Dict[str, Any]]:
    """Try to fetch JSON candle data from /snapshot.

    Returns parsed dict or None.
    """
    symbol_combo = f"{exchange}:{ticker}"
    url = f"{BASE_URL}/snapshot/{symbol_combo}?tf={interval}&fmt=json&candles={candles}"
    try:
        global_throttle_wait()
        r = await asyncio.to_thread(_http.get, url, 30)
    except Exception as e:  # noqa: BLE001
        logger.debug("snapshot json err: %s", e)
        return None
    if r.status_code != 200:
        logger.debug("snapshot json bad status %s %s", r.status_code, url)
        return None
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return None

# ---------------------------------------------------------------------------
# Simple Technical Analyzer
# ---------------------------------------------------------------------------

@dataclass
class AnalyzeResult:
    direction: str        # CALL|PUT|NEUTRAL
    score_call: float     # 0..1
    score_put: float      # 0..1
    reason: str
    suggested_expiry: str # from presets
    last_close: Optional[float] = None
    last_open: Optional[float] = None


def _ema(values: Sequence[float], length: int) -> float:
    if not values:
        return float("nan")
    k = 2 / (length + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val


def analyze_candles(candles: Sequence[Dict[str, Any]]) -> AnalyzeResult:
    """Very lightweight analyzer.

    candles: list of dicts w/ keys: o,h,l,c.
    We'll use last up to 20 closes.
    Rules:
      • If latest close > open and slope of fast EMA > slow EMA => CALL bias.
      • If latest close < open and slope negative => PUT bias.
      • else NEUTRAL.
    Score is heuristic (# of confirming conditions / 3).
    """
    if not candles:
        return AnalyzeResult("NEUTRAL", 0.5, 0.5, "no data", "5m")

    closes = [float(bar.get("c", 0)) for bar in candles if bar.get("c") is not None]
    opens  = [float(bar.get("o", 0)) for bar in candles if bar.get("o") is not None]
    if not closes:
        return AnalyzeResult("NEUTRAL", 0.5, 0.5, "no closes", "5m")

    closes20 = closes[-20:]
    opens20  = opens[-20:]

    fast = _ema(closes20, 5)
    slow = _ema(closes20, 13)
    slope = closes20[-1] - closes20[0] if len(closes20) > 1 else 0.0

    last_close = closes20[-1]
    last_open = opens20[-1] if opens20 else last_close

    up_cond1 = last_close > last_open
    up_cond2 = fast > slow
    up_cond3 = slope > 0

    dn_cond1 = last_close < last_open
    dn_cond2 = fast < slow
    dn_cond3 = slope < 0

    call_score = sum([up_cond1, up_cond2, up_cond3]) / 3.0
    put_score  = sum([dn_cond1, dn_cond2, dn_cond3]) / 3.0

    if call_score > put_score and call_score >= 0.34:  # at least 1 confirm
        dir_ = "CALL"
        exp = "5m" if call_score < 0.67 else "15m"
        msg = f"Up bias ({call_score:.2f}) fast>{slow:.5f}? slope={slope:.5f}"
    elif put_score > call_score and put_score >= 0.34:
        dir_ = "PUT"
        exp = "5m" if put_score < 0.67 else "15m"
        msg = f"Down bias ({put_score:.2f}) fast<{slow:.5f}? slope={slope:.5f}"
    else:
        dir_ = "NEUTRAL"
        exp = "1m"
        msg = "Mixed / indecisive"

    return AnalyzeResult(dir_, call_score, put_score, msg, exp, last_close, last_open)


async def analyze_pair(exchange: str, ticker: str, interval: str) -> AnalyzeResult:
    data = await fetch_snapshot_json_any(exchange, ticker, interval, candles=50)
    if data and "candles" in data:
        return analyze_candles(data["candles"])
    # fallback: no data
    return AnalyzeResult("NEUTRAL", 0.5, 0.5, "no json data", "5m")

# ---------------------------------------------------------------------------
# Direction parsing (user input synonyms)
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
# Telegram Inline Keyboards
# ---------------------------------------------------------------------------

CATEGORY_FX     = "CAT_FX"
CATEGORY_OTC    = "CAT_OTC"
CATEGORY_INDEX  = "CAT_INDEX"
CATEGORY_CRYPTO = "CAT_CRYPTO"

# callback_data patterns
# cat|FX          -> show pairs in cat
# pair|EURUSD     -> show expiry selection
# exp|PAIR|5m     -> show size selection + analyze
# size|PAIR|5m|$|10   -> trade confirm
# go|PAIR|5m|$|10     -> execute trade snapshot + UI.Vision
# ana|PAIR|tf         -> /analyze quick

CB_PREFIX_CAT   = "cat"
CB_PREFIX_PAIR  = "pair"
CB_PREFIX_EXP   = "exp"
CB_PREFIX_SIZE  = "size"
CB_PREFIX_GO    = "go"
CB_PREFIX_ANA   = "ana"


def _mk_kb_main() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("💱 FX", callback_data=f"{CB_PREFIX_CAT}|FX"), InlineKeyboardButton("🕒 OTC", callback_data=f"{CB_PREFIX_CAT}|OTC")],
        [InlineKeyboardButton("📈 Indices", callback_data=f"{CB_PREFIX_CAT}|INDEX"), InlineKeyboardButton("₿ Crypto", callback_data=f"{CB_PREFIX_CAT}|CRYPTO")],
    ]
    return InlineKeyboardMarkup(rows)


def _mk_kb_pairs(cat: str) -> InlineKeyboardMarkup:
    names = {
        "FX": FX_PAIRS,
        "OTC": OTC_PAIRS,
        "INDEX": INDEX_PAIRS,
        "CRYPTO": CRYPTO_PAIRS,
    }.get(cat.upper(), [])
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for p in names:
        slug = _canon_key(p)  # remove slash & spaces
        row.append(InlineKeyboardButton(p, callback_data=f"{CB_PREFIX_PAIR}|{slug}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="root")])
    return InlineKeyboardMarkup(rows)


def _mk_kb_exp(pair_slug: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for exp in TRADE_EXPIRY_PRESETS:
        row.append(InlineKeyboardButton(exp, callback_data=f"{CB_PREFIX_EXP}|{pair_slug}|{exp}"))
    rows.append(row)
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="root")])
    return InlineKeyboardMarkup(rows)


def _mk_kb_size(pair_slug: str, exp: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []

    # $ row(s)
    row: List[InlineKeyboardButton] = []
    for amt in TRADE_SIZE_PRESETS_DOLLAR:
        row.append(InlineKeyboardButton(f"${amt}", callback_data=f"{CB_PREFIX_SIZE}|{pair_slug}|{exp}|$|{amt}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    # % row(s)
    row = []
    for pct in TRADE_SIZE_PRESETS_PERCENT:
        row.append(InlineKeyboardButton(f"{pct}%", callback_data=f"{CB_PREFIX_SIZE}|{pair_slug}|{exp}|%|{pct}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("✅ Confirm", callback_data=f"{CB_PREFIX_GO}|{pair_slug}|{exp}")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="root")])
    return InlineKeyboardMarkup(rows)

# ---------------------------------------------------------------------------
# Utility: slug ↔ display
# ---------------------------------------------------------------------------

def slug_to_display(slug: str) -> str:
    # We can scan known lists
    for name in ALL_PAIRS:
        if _canon_key(name) == slug.upper():
            return name
    # fallback guess: put slash before last 3? Keep slug.
    return slug

# ---------------------------------------------------------------------------
# Telegram Bot Command Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: D401
    nm = update.effective_user.first_name if update.effective_user else "Trader"
    msg = (
        f"Hi {nm} 👋\n\n"
        "I'm your TradingView Snapshot Bot (Pocket Option / Binary edition).\n\n"
        "Use /pairs to browse markets, or /snap EUR/USD 5 dark, /analyze EUR/USD, /trade EUR/USD CALL 5m."
    )
    await context.bot.send_message(update.effective_chat.id, msg, reply_markup=_mk_kb_main())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "📘 *Help*\n\n"
        "*/snap* SYMBOL [interval] [theme]\n"
        "*/analyze* SYMBOL [interval]\n"
        "*/trade* SYMBOL CALL|PUT [expiry] [theme]\n"
        "*/snapmulti* S1 S2 ... [interval] [theme]\n"
        "*/snapall* (all FX+OTC+indices+crypto)\n"
        "*/pairs* browse clickable markets\n"
        "*/next* watch for next signal (from TV alerts)\n\n"
        "Intervals: minutes (#) or D/W/M.\n"
        "Themes: dark|light.\n"
    )
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN, reply_markup=_mk_kb_main())


async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(update.effective_chat.id, "Select a market category:", reply_markup=_mk_kb_main())


# ----------- Argument parsing for typed commands --------------------

def parse_snap_args(args: Sequence[str]) -> Tuple[str, str, str, str, List[str]]:
    symbol = args[0] if args else "EUR/USD"
    tf = DEFAULT_INTERVAL
    th = DEFAULT_THEME
    if len(args) >= 2 and args[1].lower() not in ("dark", "light"):
        tf = args[1]
    if len(args) >= 2 and args[-1].lower() in ("dark", "light"):
        th = args[-1].lower()
    elif len(args) >= 3 and args[2].lower() in ("dark", "light"):
        th = args[2].lower()
    ex, tk, alt, _disp = resolve_symbol(symbol)
    return ex, tk, norm_interval(tf), norm_theme(th), alt


def parse_multi_args(args: Sequence[str]) -> Tuple[List[str], str, str]:
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
    return list(args), norm_interval(tf), norm_theme(theme)


def parse_trade_args(args: Sequence[str]) -> Tuple[str, str, str, str]:
    if not args:
        return "EUR/USD", "CALL", "5m", DEFAULT_THEME
    symbol = args[0]
    direction = parse_direction(args[1] if len(args) >= 2 else None) or "CALL"
    expiry = args[2] if len(args) >= 3 else "5m"
    theme = args[3] if len(args) >= 4 else DEFAULT_THEME
    return symbol, direction, expiry, theme

# ---------------------------------------------------------------------------
# Low-level send helpers
# ---------------------------------------------------------------------------

async def _send_rate_limited(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if rate_limited(chat_id):
        await context.bot.send_message(chat_id, "⏳ Too many requests; wait a few seconds…")
        return True
    return False


async def send_snapshot_photo(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    exchange: str,
    ticker: str,
    interval: str,
    theme: str,
    prefix: str = "",
    alt_exchanges: Optional[Sequence[str]] = None,
) -> None:
    if await _send_rate_limited(chat_id, context):
        return
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    await warm_browser_async()
    try:
        png, ex_used = await fetch_snapshot_png_any(exchange, ticker, interval, theme, alt_exchanges)
        caption = f"{prefix}{ex_used}:{ticker} • TF {interval} • {theme}"
        await context.bot.send_photo(chat_id=chat_id, photo=png, caption=caption)
    except Exception as e:  # noqa: BLE001
        logger.exception("snapshot photo error")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Failed: {exchange}:{ticker} ({e})")


def _build_media_items_sync(
    pairs: List[Tuple[str, str, str, List[str]]],
    interval: str,
    theme: str,
    prefix: str,
) -> List[InputMediaPhoto]:
    out: List[InputMediaPhoto] = []
    for ex, tk, lab, alt_list in pairs:
        try:
            ok, png, err = fetch_snapshot_png_http(f"{BASE_URL}/snapshot/{ex}:{tk}?tf={interval}&theme={theme}")
            if not ok or png is None:
                # fallback /run
                ok2, png2, err2 = fetch_snapshot_png_http(
                    f"{BASE_URL}/run?exchange={ex}&ticker={tk}&interval={interval}&theme={theme}"
                )
                if not ok2 or png2 is None:
                    logger.warning("Media build fail %s:%s %s/%s", ex, tk, err, err2)
                    continue
                png = png2
            bio = io.BytesIO(png)
            bio.name = "chart.png"
            cap = f"{prefix}{ex}:{tk} • {lab} • TF {interval} • {theme}"
            out.append(InputMediaPhoto(media=bio, caption=cap))
        except Exception as e:  # noqa: BLE001
            logger.warning("Media build fail %s:%s %s", ex, tk, e)
    return out


async def send_media_group_chunked(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    media_items: List[InputMediaPhoto],
    chunk_size: int = 5,
) -> None:
    for i in range(0, len(media_items), chunk_size):
        chunk = media_items[i : i + chunk_size]
        if not chunk:
            continue
        if len(chunk) > 1:  # only first caption sticks; clear others
            for m in chunk[1:]:
                m.caption = None
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(1.0)

# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

async def cmd_snap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ex, tk, tf, th, alt = parse_snap_args(context.args)
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, th, alt_exchanges=alt)


async def cmd_snapmulti(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pairs, tf, th = parse_multi_args(context.args)
    if not pairs:
        await context.bot.send_message(update.effective_chat.id, "Usage: /snapmulti SYM1 SYM2 ... [interval] [theme]")
        return
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, f"📸 Capturing {len(pairs)} charts…")
    p_trip: List[Tuple[str, str, str, List[str]]] = []
    for p in pairs:
        ex, tk, alt, _d = resolve_symbol(p)
        p_trip.append((ex, tk, p, alt))
    media_items = await asyncio.to_thread(_build_media_items_sync, p_trip, tf, th, prefix="[MULTI] ")
    if not media_items:
        await context.bot.send_message(chat_id, "❌ No charts captured.")
        return
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)


async def cmd_snapall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, f"⚡ Capturing all {len(ALL_PAIRS)} pairs… this may take a while.")
    p_trip: List[Tuple[str, str, str, List[str]]] = []
    for p in ALL_PAIRS:
        ex, tk, alt, _d = resolve_symbol(p)
        p_trip.append((ex, tk, p, alt))
    media_items = await asyncio.to_thread(_build_media_items_sync, p_trip, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[ALL] ")
    if not media_items:
        await context.bot.send_message(chat_id, "❌ No charts captured.")
        return
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)


async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    symbol, direction, expiry, theme = parse_trade_args(context.args)
    ex, tk, alt, _d = resolve_symbol(symbol)
    tf = norm_interval(DEFAULT_INTERVAL)
    th = norm_theme(theme)
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    msg = f"{arrow} *{symbol}* {direction}  Expiry: {expiry}"
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN)
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, th, prefix="[TRADE] ", alt_exchanges=alt)


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(update.effective_chat.id, "👀 Watching for next signal (hook TradingView alerts to /tv).")


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    symbol = context.args[0] if context.args else "EUR/USD"
    tf = context.args[1] if len(context.args) > 1 else DEFAULT_INTERVAL
    ex, tk, alt, _d = resolve_symbol(symbol)
    tf = norm_interval(tf)
    ar = await analyze_pair(ex, tk, tf)
    arrow = "🟢↑" if ar.direction == "CALL" else ("🔴↓" if ar.direction == "PUT" else "⚪")
    msg = (
        f"🔍 *Analyze* {symbol} TF {tf}m\n"
        f"Bias: {arrow} {ar.direction}\n"
        f"CallScore: {ar.score_call:.2f}  PutScore: {ar.score_put:.2f}\n"
        f"Suggested Expiry: {ar.suggested_expiry}\n"
        f"Reason: {ar.reason}"
    )
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN)
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, DEFAULT_THEME, prefix="[ANALYZE] ", alt_exchanges=alt)


# ---------------------------------------------------------------------------
# Text fallback & unknown
# ---------------------------------------------------------------------------
_trade_re = re.compile(r"(?i)trade\s+([A-Z/:-]+)\s+(call|put|buy|sell|up|down)\s+([0-9]+m?)")


async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = update.message.text.strip()
    m = _trade_re.match(txt)
    if m:
        symbol, dirw, exp = m.group(1), m.group(2), m.group(3)
        direction = parse_direction(dirw) or "CALL"
        ex, tk, alt, _d = resolve_symbol(symbol)
        arrow = "🟢↑" if direction == "CALL" else "🔴↓"
        await context.bot.send_message(
            update.effective_chat.id,
            f"{arrow} *{symbol}* {direction} Expiry {exp}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await send_snapshot_photo(update.effective_chat.id, context, ex, tk, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[TRADE] ", alt_exchanges=alt)
        return
    await context.bot.send_message(update.effective_chat.id, f"You said: {txt}\nTry /trade EUR/USD CALL 5m")


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(update.effective_chat.id, "❌ Unknown command. Try /help.")

# ---------------------------------------------------------------------------
# Callback Query Handling (inline buttons)
# ---------------------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: C901 - complex
    query = update.callback_query
    if not query:
        return
    data = query.data or ""

    if data == "root":
        await query.answer()
        await query.edit_message_text("Select a market category:", reply_markup=_mk_kb_main())
        return

    parts = data.split("|")
    if len(parts) == 0:
        await query.answer()
        return

    p0 = parts[0]

    # category -> show pairs
    if p0 == CB_PREFIX_CAT and len(parts) >= 2:
        cat = parts[1]
        await query.answer()
        await query.edit_message_text(f"Select {cat} pair:", reply_markup=_mk_kb_pairs(cat))
        return

    # pair -> show expiry
    if p0 == CB_PREFIX_PAIR and len(parts) >= 2:
        slug = parts[1]
        disp = slug_to_display(slug)
        await query.answer()
        await query.edit_message_text(f"{disp}\nPick expiry:", reply_markup=_mk_kb_exp(slug))
        return

    # expiry -> show size options & run quick analyze
    if p0 == CB_PREFIX_EXP and len(parts) >= 3:
        slug, exp = parts[1], parts[2]
        disp = slug_to_display(slug)
        ex, tk, alt, _d = resolve_symbol(disp)
        ar = await analyze_pair(ex, tk, norm_interval(DEFAULT_INTERVAL))
        arrow = "🟢↑" if ar.direction == "CALL" else ("🔴↓" if ar.direction == "PUT" else "⚪")
        txt = (
            f"🔍 *{disp}* analyze\n"
            f"Bias: {arrow} {ar.direction}\n"
            f"Suggested: {ar.suggested_expiry}\n"
            f"Select stake for trade @ {exp}."
        )
        await query.answer()
        await query.edit_message_text(
            txt,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_mk_kb_size(slug, exp),
        )
        return

    # size selection -> update chat state, show confirm
    if p0 == CB_PREFIX_SIZE and len(parts) >= 5:
        slug, exp, mode, amt = parts[1], parts[2], parts[3], parts[4]
        disp = slug_to_display(slug)
        cs = get_chat_state(update.effective_chat.id)
        cs.last_pair = disp
        cs.last_expiry = exp
        cs.size_mode = mode
        cs.size_value = float(amt)
        save_state()
        await query.answer("Size selected")
        txt = f"{disp}\nExpiry {exp}\nSize: {mode}{amt}\nTap ✅ Confirm to trade."
        await query.edit_message_text(txt, reply_markup=_mk_kb_size(slug, exp))
        return

    # confirm -> execute trade (snapshot + UI.Vision call)
    if p0 == CB_PREFIX_GO and len(parts) >= 3:
        slug, exp = parts[1], parts[2]
        disp = slug_to_display(slug)
        ex, tk, alt, _d = resolve_symbol(disp)
        cs = get_chat_state(update.effective_chat.id)
        msg = f"📤 Executing trade {disp} Exp {exp} Size {cs.size_mode}{cs.size_value}"
        await query.answer()
        await query.edit_message_text(msg)
        # snapshot send
        await send_snapshot_photo(update.effective_chat.id, context, ex, tk, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[TRADE] ", alt_exchanges=alt)
        # UI Vision call
        await _ui_vision_trade_async(update.effective_chat.id, disp, exp, cs)
        return

    # analyze direct
    if p0 == CB_PREFIX_ANA and len(parts) >= 3:
        slug, tf = parts[1], parts[2]
        disp = slug_to_display(slug)
        await query.answer()
        fake_update = Update(update.update_id, message=None)
        context.args = [disp, tf]
        # We can't directly call cmd_analyze (needs update). We'll send new message:
        ex, tk, alt, _d = resolve_symbol(disp)
        ar = await analyze_pair(ex, tk, norm_interval(tf))
        arrow = "🟢↑" if ar.direction == "CALL" else ("🔴↓" if ar.direction == "PUT" else "⚪")
        txt = (
            f"🔍 *{disp}* TF {tf}\n"
            f"Bias: {arrow} {ar.direction}\n"
            f"CallScore: {ar.score_call:.2f}  PutScore: {ar.score_put:.2f}\n"
            f"Suggested Expiry: {ar.suggested_expiry}\n"
            f"Reason: {ar.reason}"
        )
        await context.bot.send_message(update.effective_chat.id, txt, parse_mode=ParseMode.MARKDOWN)
        await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, DEFAULT_THEME, prefix="[ANALYZE] ", alt_exchanges=alt)
        return

    await query.answer()

# ---------------------------------------------------------------------------
# UI.Vision integration
# ---------------------------------------------------------------------------

async def _ui_vision_trade_async(chat_id: int, pair: str, expiry: str, cs: ChatState) -> None:
    if not UI_VISION_URL:
        return  # disabled
    payload = {
        "macro": UI_VISION_MACRO_NAME,
        "params": {
            "pair": pair,
            "expiry": expiry,
            "size_mode": cs.size_mode,
            "size_value": cs.size_value,
        },
    }
    # merge raw params from env
    try:
        extra = json.loads(UI_VISION_MACRO_PARAMS_RAW)
        if isinstance(extra, dict):
            payload["params"].update(extra)
    except Exception:  # noqa: BLE001
        pass

    if SIM_DEBIT:
        logger.info("(SIM) UI.Vision trade: %s", payload)
        return

    try:
        r = await asyncio.to_thread(_http.post, UI_VISION_URL, json=payload, timeout=30)
        if r.status_code != 200:
            await _send_text_safe(chat_id, f"UI.Vision trade error: {_safe_text(r.text)}")
        else:
            await _send_text_safe(chat_id, "UI.Vision trade triggered.")
    except Exception as e:  # noqa: BLE001
        await _send_text_safe(chat_id, f"UI.Vision trade exception: {e}")


async def _send_text_safe(chat_id: int, text: str) -> None:
    # context not always known; use raw Telegram HTTP fallback
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        _http.post(url, json={"chat_id": chat_id, "text": text})
    except Exception as e:  # noqa: BLE001
        logger.error("send_text_safe fail: %s", e)

# ---------------------------------------------------------------------------
# Flask TradingView Webhook → Telegram (+ optional auto-trade)
# ---------------------------------------------------------------------------

flask_app = Flask(__name__)


def _parse_tv_payload(data: dict) -> Dict[str, str]:
    d: Dict[str, str] = {}
    d["chat_id"]   = str(data.get("chat_id") or DEFAULT_CHAT_ID or "")
    d["pair"]      = str(data.get("pair") or data.get("symbol") or data.get("ticker") or "EUR/USD")
    d["direction"] = str(data.get("direction") or "CALL").upper()
    d["expiry"]    = str(data.get("default_expiry_min") or data.get("expiry") or "5m")
    d["strategy"]  = str(data.get("strategy") or "")
    d["winrate"]   = str(data.get("winrate") or "")
    d["timeframe"] = str(data.get("timeframe") or data.get("tf") or DEFAULT_INTERVAL)
    d["theme"]     = str(data.get("theme") or DEFAULT_THEME)
    return d


def tg_api_send_message(chat_id: str, text: str, parse_mode: Optional[str] = None) -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        _http.post(url, json=payload, timeout=30)
    except Exception as e:  # noqa: BLE001
        logger.error("tg_api_send_message: %s", e)


def tg_api_send_photo_bytes(chat_id: str, png: bytes, caption: str = "") -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", png, "image/png")}
    data = {"chat_id": chat_id, "caption": caption}
    try:
        _http.post(url, data=data, files=files, timeout=60)
    except Exception as e:  # noqa: BLE001
        logger.error("tg_api_send_photo_bytes: %s", e)


def _handle_tv_alert(data: dict) -> Tuple[Dict[str, Any], int]:
    # security check
    if WEBHOOK_SECRET:
        hdr = request.headers.get("X-Webhook-Token", "")
        body_secret = str(data.get("secret") or data.get("token") or "")
        if hdr != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET:
            logger.warning("Webhook secret mismatch; rejecting.")
            return {"ok": False, "error": "unauthorized"}, 403

    payload = _parse_tv_payload(data)
    logger.info("TV payload: %s", payload)

    chat_id = payload["chat_id"]
    raw_pair = payload["pair"]
    direction = parse_direction(payload["direction"]) or "CALL"
    expiry = payload["expiry"]
    strat = payload["strategy"]
    winrate = payload["winrate"]
    tf = norm_interval(payload["timeframe"])
    theme = norm_theme(payload["theme"])

    ex, tk, alt, _d = resolve_symbol(raw_pair)

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

    # attempt chart
    try:
        warm_browser_sync()
        # synchronous fallback run
        ok, png, err = fetch_snapshot_png_http(f"{BASE_URL}/snapshot/{ex}:{tk}?tf={tf}&theme={theme}")
        if not ok or png is None:
            ok2, png2, err2 = fetch_snapshot_png_http(
                f"{BASE_URL}/run?exchange={ex}&ticker={tk}&interval={tf}&theme={theme}"
            )
            if not ok2 or png2 is None:
                raise RuntimeError(f"snapshot fail {err}/{err2}")
            png = png2
        tg_api_send_photo_bytes(chat_id, png, caption=f"{ex}:{tk} • TF {tf} • {theme}")
    except Exception as e:  # noqa: BLE001
        logger.error("TV snapshot error %s:%s -> %s", ex, tk, e)
        tg_api_send_message(chat_id, f"⚠ Chart snapshot failed for {raw_pair}: {e}")

    # auto trade? (if configured)
    if AUTO_TRADE_FROM_TV and UI_VISION_URL:
        cs = ChatState(last_pair=raw_pair, last_expiry=expiry, size_mode="$", size_value=1.0)
        try:
            asyncio.get_event_loop().create_task(_ui_vision_trade_async(int(chat_id or 0), raw_pair, expiry, cs))
        except Exception:  # noqa: BLE001
            pass

    return {"ok": True}, 200


@flask_app.post("/tv")
def tv_route() -> Any:  # noqa: ANN401
    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:  # noqa: BLE001
        logger.error("TV /tv invalid JSON: %s", e)
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    body, code = _handle_tv_alert(data)
    return jsonify(body), code


@flask_app.route("/webhook", methods=["POST"])
def tv_route_alias() -> Any:  # noqa: ANN401
    return tv_route()


def start_flask_background() -> None:
    threading.Thread(
        target=lambda: flask_app.run(
            host="0.0.0.0", port=TV_WEBHOOK_PORT, debug=False, use_reloader=False, threaded=True
        ),
        daemon=True,
    ).start()
    logger.info("Flask TV webhook listening on port %s", TV_WEBHOOK_PORT)

# ---------------------------------------------------------------------------
# Application build & main
# ---------------------------------------------------------------------------

def build_application() -> Application:
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pairs", cmd_pairs))
    app.add_handler(CommandHandler("snap", cmd_snap))
    app.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    app.add_handler(CommandHandler("snapall", cmd_snapall))
    app.add_handler(CommandHandler("trade", cmd_trade))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("analyze", cmd_analyze))

    # inline callbacks
    app.add_handler(CallbackQueryHandler(on_callback))

    # text & unknown
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    return app


def main() -> None:  # noqa: D401
    load_state()
    start_flask_background()
    application = build_application()
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        close_loop=True,
        drop_pending_updates=True,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
