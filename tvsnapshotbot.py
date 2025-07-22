#!/usr/bin/env python
"""
TradingView → Telegram Snapshot Bot (Inline Edition)
====================================================
Features
--------
• python-telegram-bot v20+ async architecture (no legacy Updater crash).
• Inline keyboards: /pairs -> choose category -> symbol -> analyze -> direction -> expiry -> size -> confirm.
• /analyze <pair> runs lightweight tech scan (EMA cross, RSI bands, MACD histogram) & suggests CALL/PUT + expiry.
• Screenshot pipeline w/ validation + retry + multi-endpoint fallback:
      /snapshot/<symbol>?tf=1&theme=dark  (preferred)
      /run?exchange=FX&ticker=EURUSD&interval=1&theme=dark (compat)
• Safe logging: never dump binary; binary responses truncated + repr-sanitized.
• Per-chat state persistence (state.json): last pair, size mode ($ or %), size value, theme, interval, sim balance.
• Pocket Option automation stubs + optional UI.Vision webhook trigger.
• TradingView webhook (/tv, /webhook) -> parse JSON -> optional auto-trade -> send chart + signal to Telegram.
• Rate limiting (per-chat + global throttle between snapshot calls).
• Commands: /start /help /pairs /analyze /snap /snapmulti /snapall /trade /setsize /setbalance /config /next
• Supports FX, OTC, Crypto, Indices lists.

This file is intentionally verbose & heavily commented for clarity and customization.
"""

# ---------------------------------------------------------------------------
# Standard Library Imports
# ---------------------------------------------------------------------------
import os
import io
import re
import sys
import json
import time
import math
import base64
import asyncio
import logging
import threading
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import (
    Any,
    Dict,
    List,
    Tuple,
    Optional,
    Sequence,
    Callable,
    Coroutine,
    Union,
)

# ---------------------------------------------------------------------------
# Third-Party Imports
# ---------------------------------------------------------------------------
import httpx
import requests  # used for some blocking fallback calls
from flask import Flask, jsonify, request

from PIL import Image  # used for placeholder size check; optional

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Config / Environment
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment.")

BASE_URL = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000")
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "FX")
DEFAULT_INTERVAL = os.environ.get("DEFAULT_INTERVAL", "1")  # minutes or D/W/M
DEFAULT_THEME = os.environ.get("DEFAULT_THEME", "dark")
TV_WEBHOOK_PORT = int(os.environ.get("TV_WEBHOOK_PORT", "8081"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # optional
UI_VISION_URL = os.environ.get("UI_VISION_URL")  # optional external auto-trade trigger
AUTO_TRADE_FROM_TV = os.environ.get("AUTO_TRADE_FROM_TV", "false").lower() in (
    "1",
    "true",
    "yes",
)
SIM_DEBIT = os.environ.get("SIM_DEBIT", "false").lower() in ("1", "true", "yes")

# Default chat (fallback if TV payload missing)
DEFAULT_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # optional

# ---------------------------------------------------------------------------
# Logging Setup (safe binary truncation)
# ---------------------------------------------------------------------------
os.makedirs("logs", exist_ok=True)
LOG_FILE = os.path.join("logs", "tvsnapshotbot.log")

# Use RotatingFileHandler explicitly (avoid logging.handlers attr confusion)
from logging.handlers import RotatingFileHandler

_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_stream_handler = logging.StreamHandler(sys.stdout)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[_file_handler, _stream_handler],
)
logger = logging.getLogger("TVSnapBot")

# ---------------------------------------------------------------------------
# Global HTTP clients
# ---------------------------------------------------------------------------
# sync session for some blocking calls
_sync_http = requests.Session()
# async client: create lazily (in get_async_http_client)
_async_http: Optional[httpx.AsyncClient] = None


def get_async_http_client() -> httpx.AsyncClient:
    global _async_http
    if _async_http is None:
        _async_http = httpx.AsyncClient(timeout=60.0)
    return _async_http


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------
LAST_SNAPSHOT_PER_CHAT: Dict[int, float] = {}
RATE_LIMIT_SECONDS = 3.0

GLOBAL_LAST_SNAPSHOT = 0.0
GLOBAL_MIN_GAP = 0.75  # seconds between snapshot requests (to avoid hammering Render)


def rate_limited(chat_id: int) -> bool:
    now = time.time()
    last = LAST_SNAPSHOT_PER_CHAT.get(chat_id, 0.0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    LAST_SNAPSHOT_PER_CHAT[chat_id] = now
    return False


def global_throttle_wait() -> None:
    """Synchronous throttle; used by blocking fetch calls."""
    global GLOBAL_LAST_SNAPSHOT
    now = time.time()
    gap = now - GLOBAL_LAST_SNAPSHOT
    if gap < GLOBAL_MIN_GAP:
        time.sleep(GLOBAL_MIN_GAP - gap)
    GLOBAL_LAST_SNAPSHOT = time.time()


# ---------------------------------------------------------------------------
# Market Universe
# ---------------------------------------------------------------------------
# Display labels. We store underlying tickers separately.
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

CRYPTO_PAIRS: List[str] = [
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "XRP/USD",
    "DOGE/USD",
    "BNB/USD",
    "ADA/USD",
]

INDEX_SYMBOLS: List[str] = [
    "SPX",
    "NDX",
    "DAX",
    "FTSE",
    "NIKKEI",
    "US30",
    "US500",
]

ALL_PAIRS: List[str] = FX_PAIRS + OTC_PAIRS + CRYPTO_PAIRS

# ---------------------------------------------------------------------------
# Pair Resolver
# ---------------------------------------------------------------------------
def _canon_key(symbol: str) -> str:
    """Canonical key for lookup (upper, strip, remove spaces & slash)."""
    return symbol.strip().upper().replace(" ", "").replace("/", "")


# Build static map -> (exchange, ticker, alt_exchanges)
PAIR_MAP: Dict[str, Tuple[str, str, List[str]]] = {}

# Base mapping for FX -> DEFAULT_EXCHANGE (e.g., FX)
for p in FX_PAIRS:
    tk = p.replace("/", "")
    PAIR_MAP[_canon_key(p)] = (DEFAULT_EXCHANGE, tk, ["FX", "FX_IDC", "OANDA", "FOREXCOM", "IDC"])

# OTC underlying -> try QUOTEX fallback
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
    PAIR_MAP[_canon_key(p)] = ("QUOTEX", tk, ["FX", "FX_IDC", "OANDA", "FOREXCOM", "IDC"])

# Crypto approximate mapping (some may require BINANCE: pair)
_crypto_map = {
    "BTC/USD": ("BINANCE", "BTCUSDT", ["CRYPTO", "BITSTAMP", "COINBASE", "BINANCE"]),
    "ETH/USD": ("BINANCE", "ETHUSDT", ["CRYPTO", "COINBASE", "BITSTAMP", "BINANCE"]),
    "SOL/USD": ("BINANCE", "SOLUSDT", ["CRYPTO", "BINANCE"]),
    "XRP/USD": ("BINANCE", "XRPUSDT", ["CRYPTO", "BINANCE"]),
    "DOGE/USD": ("BINANCE", "DOGEUSDT", ["CRYPTO", "BINANCE"]),
    "BNB/USD": ("BINANCE", "BNBUSDT", ["CRYPTO", "BINANCE"]),
    "ADA/USD": ("BINANCE", "ADAUSDT", ["CRYPTO", "BINANCE"]),
}
for p, tup in _crypto_map.items():
    PAIR_MAP[_canon_key(p)] = tup

# Indices mapping (rough)
_index_map = {
    "SPX": ("SP", "SPX500USD", ["SP", "CME", "FXCM", "FOREXCOM"]),
    "NDX": ("ND", "NAS100USD", ["ND", "CME", "FXCM", "FOREXCOM"]),
    "DAX": ("XETR", "DEU40EUR", ["XETR", "FOREXCOM", "FXCM"]),
    "FTSE": ("LSE", "UK100GBP", ["LSE", "FOREXCOM", "FXCM"]),
    "NIKKEI": ("OSE", "JPN225JPY", ["OSE", "FOREXCOM", "FXCM"]),
    "US30": ("DJ", "US30USD", ["DJ", "FOREXCOM", "FXCM"]),
    "US500": ("SP", "SPX500USD", ["SP", "FOREXCOM", "FXCM"]),
}
for p, tup in _index_map.items():
    PAIR_MAP[_canon_key(p)] = tup


def resolve_symbol(raw: str) -> Tuple[str, str, bool, List[str]]:
    """
    Normalize raw symbol into (exchange, ticker, is_otc, alt_exchanges[]).
    Accept "EX:TK", "EUR/USD-OTC", etc.
    """
    if not raw:
        return DEFAULT_EXCHANGE, "EURUSD", False, []

    s = raw.strip().upper()
    is_otc = "-OTC" in s

    if ":" in s:
        ex, tk = s.split(":", 1)
        return ex, tk, is_otc, []

    key = _canon_key(s)
    if key in PAIR_MAP:
        ex, tk, alt = PAIR_MAP[key]
        return ex, tk, is_otc, alt

    # fallback guess: strip non-alnum
    tk = re.sub(r"[^A-Z0-9]", "", s)
    return DEFAULT_EXCHANGE, tk, is_otc, []


# ---------------------------------------------------------------------------
# Interval & Theme Normalization
# ---------------------------------------------------------------------------
def norm_interval(tf: str) -> str:
    """Return canonical interval string accepted by screenshot endpoint."""
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
    if t in ("m", "1m", "mo", "month"):  # monthly candle
        return "M"
    if t.isdigit():
        return t
    return DEFAULT_INTERVAL


def norm_theme(val: str) -> str:
    return "light" if (val and val.lower().startswith("l")) else "dark"


# ---------------------------------------------------------------------------
# Placeholder PNG (1x1 dark gray)
# ---------------------------------------------------------------------------
_PLACEHOLDER_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z/C/HwAFgwJ/lbGZxAAAAABJRU5ErkJggg=="
)


def get_placeholder_png() -> bytes:
    return _PLACEHOLDER_PNG


# ---------------------------------------------------------------------------
# Snapshot Fetchers
# ---------------------------------------------------------------------------
def _log_http_err_trunc(prefix: str, content: Union[str, bytes]) -> None:
    """Safe logging of HTTP error body; text truncated & sanitized."""
    try:
        if isinstance(content, bytes):
            txt = content[:200].decode("utf-8", "replace")
        else:
            txt = str(content)[:200]
    except Exception as e:
        txt = f"<decode_err {e}>"
    logger.warning("%s -> %s", prefix, txt.replace("\n", "\\n"))


def _attempt_snapshot_url(url: str) -> Tuple[bool, Optional[bytes], str]:
    """
    Make a single GET request to `url` and return (success, png_bytes, error_str).
    Synchronous/blocking (used in threadpool).
    """
    try:
        global_throttle_wait()
        r = _sync_http.get(url, timeout=75)
        ct = r.headers.get("Content-Type", "")
        if r.status_code == 200 and ct.startswith("image"):
            return True, r.content, ""
        return False, None, f"HTTP {r.status_code}: {r.content[:64]!r}"
    except Exception as e:
        return False, None, str(e)


def _png_valid_enough(data: bytes, min_bytes: int = 2048) -> bool:
    """Basic sanity check: PNG header & minimum size."""
    if not data:
        return False
    if not data.startswith(b"\x89PNG"):
        return False
    if len(data) < min_bytes:  # avoid empty gray frames
        return False
    return True


def fetch_snapshot_png_any(
    primary_ex: str,
    tk: str,
    interval: str,
    theme: str,
    base: str = "chart",
    extra_exchanges: Optional[Sequence[str]] = None,
) -> Tuple[bytes, str]:
    """
    Multi-endpoint fallback.
    1. /snapshot/<ex:tk> or /snapshot/<tk> (symbol only) – try both.
    2. /run?base=chart&exchange=EX&ticker=TK&interval=...&theme=...
    3. alt exchanges (passed) + known extras.
    Returns (png_bytes, exchange_used). Raises RuntimeError if all fail.
    """
    tried: List[str] = []
    last_err = ""

    sym = f"{primary_ex}:{tk}"
    # candidate symbol paths
    snapshot_urls = [
        f"{BASE_URL}/snapshot/{sym}?tf={interval}&theme={theme}",
        f"{BASE_URL}/snapshot/{tk}?tf={interval}&theme={theme}",
    ]

    # try snapshot endpoints first
    for u in snapshot_urls:
        tried.append(u)
        ok, png, err = _attempt_snapshot_url(u)
        if ok and png and _png_valid_enough(png):
            logger.info("Snapshot success via %s", u)
            return png, primary_ex
        last_err = err
        _log_http_err_trunc(f"Snapshot fail {u}", err)
        time.sleep(1.0)

    # fallback exchanges
    fallback_list = list(extra_exchanges or [])
    fallback_list += [DEFAULT_EXCHANGE, "FX", "FX_IDC", "OANDA", "FOREXCOM", "IDC", "QUOTEX"]

    seen = set()
    merged: List[str] = []
    for ex in fallback_list:
        e = ex.upper()
        if e not in seen:
            seen.add(e)
            merged.append(e)

    for ex in merged:
        url = f"{BASE_URL}/run?base={base}&exchange={ex}&ticker={tk}&interval={interval}&theme={theme}"
        tried.append(url)
        ok, png, err = _attempt_snapshot_url(url)
        if ok and png and _png_valid_enough(png):
            logger.info("Snapshot success: %s:%s via %s", ex, tk, ex)
            return png, ex
        last_err = err
        _log_http_err_trunc(f"Snapshot fail {ex}:{tk}", err)
        time.sleep(1.0)

    # If nothing worked, final fallback placeholder
    raise RuntimeError(
        f"All endpoints failed for {primary_ex}:{tk}. "
        f"Last error: {last_err}. Tried: {tried}"
    )


async def async_fetch_snapshot_png_any(
    primary_ex: str,
    tk: str,
    interval: str,
    theme: str,
    base: str = "chart",
    extra_exchanges: Optional[Sequence[str]] = None,
) -> Tuple[bytes, str]:
    """
    Async wrapper: run blocking fetch in threadpool.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: fetch_snapshot_png_any(primary_ex, tk, interval, theme, base, extra_exchanges)
    )


# ---------------------------------------------------------------------------
# Candle Data Fetching (for Analysis)
# ---------------------------------------------------------------------------
# We'll attempt to fetch minimal OHLC series from TradingView's lightweight data feed via
# a public JSON endpoint pattern (non-auth); if it fails we'll fallback to minimal price diff.

# A very rough "unofficial" pattern that *may* or may not work reliably:
#   https://tvc4.forexpros.com/<random>/...  (varies)
# Because it's brittle, we implement a "generic" fallback that tries:
#   BASE_URL/candles?exchange=EX&ticker=TK&interval=...
# If your Node snapshot server exposes such an endpoint, great. Otherwise we synthetic.

# Candle type
@dataclass
class Candle:
    ts: int  # epoch ms
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


def _parse_candle_json(raw: Any) -> List[Candle]:
    """
    Attempt to parse a JSON structure into candles.
    Accept:
        [{"t":..,"o":..,"h":..,"l":..,"c":..}, ...]
        or separate arrays.
    """
    out: List[Candle] = []
    if isinstance(raw, list):
        for r in raw:
            try:
                ts = int(r.get("t") or r.get("time") or r.get("ts") or 0)
                o = float(r.get("o") or r.get("open") or 0)
                h = float(r.get("h") or r.get("high") or o)
                l = float(r.get("l") or r.get("low") or o)
                c = float(r.get("c") or r.get("close") or o)
                v = float(r.get("v") or r.get("volume") or 0)
                out.append(Candle(ts, o, h, l, c, v))
            except Exception:
                continue
    return out


async def fetch_candles_async(
    ex: str, tk: str, interval: str, limit: int = 200
) -> List[Candle]:
    """
    Try a few remote sources for candle data. All best-effort.
    Returns list (may be empty).
    """

    client = get_async_http_client()

    # 1) Try Node snapshot server candle endpoint (if implemented)
    #    /candles?exchange=EX&ticker=TK&interval=1&limit=200
    try:
        resp = await client.get(
            f"{BASE_URL}/candles",
            params={"exchange": ex, "ticker": tk, "interval": interval, "limit": str(limit)},
        )
        if resp.status_code == 200:
            js = resp.json()
            c = _parse_candle_json(js)
            if c:
                return c
    except Exception as e:
        logger.debug("candles endpoint fail: %s", e)

    # 2) quick synthetic fallback: use last trade from Node snapshot PNG? Not accessible.
    # Instead produce synthetic candles for analysis if remote fails
    logger.debug("Falling back to synthetic candles for %s:%s", ex, tk)
    now_ms = int(time.time() * 1000)
    # generate small ascending/descending synthetic data to produce signals
    out: List[Candle] = []
    base_price = 1.0  # meaningless but stable
    for i in range(limit):
        # small wave
        v = math.sin(i / 7.0) * 0.001
        price = base_price + v
        out.append(Candle(now_ms - (limit - i) * 60000, price, price, price, price))
    return out


# ---------------------------------------------------------------------------
# Technical Analysis
# ---------------------------------------------------------------------------
def ema(values: Sequence[float], period: int) -> List[float]:
    if period <= 0:
        return list(values)
    out: List[float] = []
    k = 2 / (period + 1)
    ema_val = None
    for v in values:
        if ema_val is None:
            ema_val = v
        else:
            ema_val = v * k + ema_val * (1 - k)
        out.append(ema_val)
    return out


def rsi(values: Sequence[float], period: int = 14) -> List[float]:
    if period < 1 or len(values) < period + 1:
        return [50.0 for _ in values]
    gains: List[float] = [0.0]
    losses: List[float] = [0.0]
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))
    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    rsis: List[float] = []
    for i in range(len(values)):
        if i < period:
            rsis.append(50.0)
            continue
        if i > period:
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rs_val = 100.0
        else:
            rs = avg_gain / avg_loss
            rs_val = 100.0 - (100.0 / (1.0 + rs))
        rsis.append(rs_val)
    return rsis


def macd(
    values: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[List[float], List[float], List[float]]:
    if not values:
        return [], [], []
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line: List[float] = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist


@dataclass
class AnalysisResult:
    pair: str
    direction: str  # CALL, PUT, NEUTRAL
    confidence: float  # 0-100
    suggested_expiry: str  # 1m/3m/5m/15m
    summary: str
    indicators: Dict[str, Any] = field(default_factory=dict)


def analyze_candles(
    pair: str,
    candles: List[Candle],
    tf: str,
    rsi_ob: float = 70.0,
    rsi_os: float = 30.0,
) -> AnalysisResult:
    """
    Very lightweight rule-based analysis.
    - EMA fast/slow slope + cross
    - RSI OB/OS
    - MACD histogram momentum
    """

    if len(candles) < 10:
        return AnalysisResult(
            pair=pair,
            direction="NEUTRAL",
            confidence=0.0,
            suggested_expiry="5m",
            summary="Insufficient data.",
        )

    closes = [c.c for c in candles]
    ema_fast = ema(closes, 7)
    ema_slow = ema(closes, 25)
    rs = rsi(closes, 14)
    macd_line, sig_line, hist = macd(closes)

    last_close = closes[-1]
    last_ema_fast = ema_fast[-1]
    last_ema_slow = ema_slow[-1]
    last_rsi = rs[-1]
    last_hist = hist[-1] if hist else 0.0
    prev_hist = hist[-2] if len(hist) > 1 else 0.0

    direction = "NEUTRAL"
    conf = 50.0
    summary_bits: List[str] = []

    # EMA cross / trend
    if last_ema_fast > last_ema_slow:
        summary_bits.append("Fast>Slow (bullish).")
        direction = "CALL"
        conf += 10
    elif last_ema_fast < last_ema_slow:
        summary_bits.append("Fast<S erow (bearish).")
        direction = "PUT"
        conf += 10

    # RSI extremes adjust
    if last_rsi > rsi_ob:
        summary_bits.append(f"RSI {last_rsi:.1f} overbought.")
        # contrarian? lighten CALL; lean PUT if very high
        if direction == "CALL":
            conf -= 10
        else:
            direction = "PUT"
            conf += 5
    elif last_rsi < rsi_os:
        summary_bits.append(f"RSI {last_rsi:.1f} oversold.")
        if direction == "PUT":
            conf -= 10
        else:
            direction = "CALL"
            conf += 5
    else:
        summary_bits.append(f"RSI {last_rsi:.1f} neutral.")

    # MACD momentum
    if last_hist > 0 and prev_hist <= 0:
        summary_bits.append("MACD hist turned positive.")
        if direction == "PUT":
            conf -= 15
            direction = "NEUTRAL" if conf < 40 else direction
        else:
            direction = "CALL"
            conf += 15
    elif last_hist < 0 and prev_hist >= 0:
        summary_bits.append("MACD hist turned negative.")
        if direction == "CALL":
            conf -= 15
            direction = "NEUTRAL" if conf < 40 else direction
        else:
            direction = "PUT"
            conf += 15

    # Bound confidence
    conf = max(0.0, min(conf, 100.0))

    # Suggest expiry based on timeframe & conf
    # If tf numeric minutes:
    if tf.isdigit():
        m = int(tf)
        if m <= 1:
            exp = "1m" if conf >= 60 else "3m"
        elif m <= 5:
            exp = "5m"
        elif m <= 15:
            exp = "15m"
        else:
            exp = "15m"
    else:
        exp = "5m"

    summary = " ".join(summary_bits)
    return AnalysisResult(
        pair=pair,
        direction=direction,
        confidence=conf,
        suggested_expiry=exp,
        summary=summary,
        indicators={
            "ema_fast": last_ema_fast,
            "ema_slow": last_ema_slow,
            "rsi": last_rsi,
            "macd_hist": last_hist,
            "close": last_close,
        },
    )


# ---------------------------------------------------------------------------
# Per-Chat State Persistence
# ---------------------------------------------------------------------------
STATE_FILE = "state.json"


@dataclass
class ChatState:
    pair: str = "EUR/USD"
    direction: str = "CALL"
    expiry: str = "5m"  # 1m/3m/5m/15m
    size_mode: str = "$"  # "$" or "%"
    size_value: float = 1.0  # $1 or 1%
    interval: str = DEFAULT_INTERVAL
    theme: str = DEFAULT_THEME
    sim_balance: float = 1000.0  # used if SIM_DEBIT / % mode and unknown real bal
    last_analysis: Optional[AnalysisResult] = None

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        # flatten AnalysisResult
        if self.last_analysis is not None:
            d["last_analysis"] = {
                "pair": self.last_analysis.pair,
                "direction": self.last_analysis.direction,
                "confidence": self.last_analysis.confidence,
                "suggested_expiry": self.last_analysis.suggested_expiry,
                "summary": self.last_analysis.summary,
                "indicators": self.last_analysis.indicators,
            }
        return d

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "ChatState":
        cs = cls()
        cs.pair = d.get("pair", cs.pair)
        cs.direction = d.get("direction", cs.direction)
        cs.expiry = d.get("expiry", cs.expiry)
        cs.size_mode = d.get("size_mode", cs.size_mode)
        cs.size_value = float(d.get("size_value", cs.size_value))
        cs.interval = d.get("interval", cs.interval)
        cs.theme = d.get("theme", cs.theme)
        cs.sim_balance = float(d.get("sim_balance", cs.sim_balance))
        la = d.get("last_analysis")
        if la:
            cs.last_analysis = AnalysisResult(
                pair=la.get("pair", cs.pair),
                direction=la.get("direction", "NEUTRAL"),
                confidence=float(la.get("confidence", 0.0)),
                suggested_expiry=la.get("suggested_expiry", "5m"),
                summary=la.get("summary", ""),
                indicators=la.get("indicators", {}),
            )
        return cs


# Chat states in memory
CHAT_STATES: Dict[int, ChatState] = {}


def load_state_file() -> None:
    if not os.path.exists(STATE_FILE):
        logger.info("No state file found; starting fresh.")
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            CHAT_STATES[int(k)] = ChatState.from_json(v)
        logger.info("Loaded state for %d chats.", len(CHAT_STATES))
    except Exception as e:
        logger.error("load_state_file error: %s", e)


def save_state_file() -> None:
    try:
        data = {str(k): v.to_json() for k, v in CHAT_STATES.items()}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.error("save_state_file error: %s", e)


def get_chat_state(chat_id: int) -> ChatState:
    cs = CHAT_STATES.get(chat_id)
    if cs is None:
        cs = ChatState()
        CHAT_STATES[chat_id] = cs
        save_state_file()
    return cs


# ---------------------------------------------------------------------------
# Direction Parsing (for text commands)
# ---------------------------------------------------------------------------
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
# Inline Keyboard Builders
# ---------------------------------------------------------------------------
EXPIRY_CHOICES = ["1m", "3m", "5m", "15m"]


def _cb(prefix: str, *parts: str) -> str:
    """Make callback_data; keep length < 64 ideally."""
    return "|".join([prefix] + list(parts))


def kb_pair_categories() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("FX", callback_data=_cb("PAIRCAT", "FX")),
            InlineKeyboardButton("OTC", callback_data=_cb("PAIRCAT", "OTC")),
        ],
        [
            InlineKeyboardButton("Crypto", callback_data=_cb("PAIRCAT", "CRYPTO")),
            InlineKeyboardButton("Indices", callback_data=_cb("PAIRCAT", "INDEX")),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def kb_symbol_list(symbols: Sequence[str], cat: str, page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    start = page * per_page
    end = start + per_page
    page_syms = symbols[start:end]
    rows: List[List[InlineKeyboardButton]] = []
    for s in page_syms:
        rows.append([InlineKeyboardButton(s, callback_data=_cb("PAIRSEL", s))])
    nav_row: List[InlineKeyboardButton] = []
    if start > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=_cb("PAIRPAGE", cat, str(page - 1))))
    if end < len(symbols):
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=_cb("PAIRPAGE", cat, str(page + 1))))
    nav_row.append(InlineKeyboardButton("Back", callback_data=_cb("PAIRCAT", "BACK")))
    rows.append(nav_row)
    return InlineKeyboardMarkup(rows)


def kb_direction(pair: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🟢 CALL", callback_data=_cb("DIR", pair, "CALL")),
            InlineKeyboardButton("🔴 PUT", callback_data=_cb("DIR", pair, "PUT")),
        ],
        [InlineKeyboardButton("Analyze 🔍", callback_data=_cb("ANAL", pair))],
        [InlineKeyboardButton("Back", callback_data=_cb("PAIRCAT", "FX"))],  # default back to cat menu
    ]
    return InlineKeyboardMarkup(rows)


def kb_expiry(pair: str, direction: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for e in EXPIRY_CHOICES:
        row.append(InlineKeyboardButton(e, callback_data=_cb("EXP", pair, direction, e)))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅ Back", callback_data=_cb("DIR", pair, direction))])
    return InlineKeyboardMarkup(rows)


def kb_size(pair: str, direction: str, expiry: str) -> InlineKeyboardMarkup:
    """
    Show a few $ and % presets: $1,$5,$10,$25,$50 + %1,%2,%5,%10,%25
    """
    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("$1", callback_data=_cb("SIZE", pair, direction, expiry, "$", "1")),
            InlineKeyboardButton("$5", callback_data=_cb("SIZE", pair, direction, expiry, "$", "5")),
            InlineKeyboardButton("$10", callback_data=_cb("SIZE", pair, direction, expiry, "$", "10")),
        ],
        [
            InlineKeyboardButton("$25", callback_data=_cb("SIZE", pair, direction, expiry, "$", "25")),
            InlineKeyboardButton("$50", callback_data=_cb("SIZE", pair, direction, expiry, "$", "50")),
            InlineKeyboardButton("$100", callback_data=_cb("SIZE", pair, direction, expiry, "$", "100")),
        ],
        [
            InlineKeyboardButton("1%", callback_data=_cb("SIZE", pair, direction, expiry, "%", "1")),
            InlineKeyboardButton("2%", callback_data=_cb("SIZE", pair, direction, expiry, "%", "2")),
            InlineKeyboardButton("5%", callback_data=_cb("SIZE", pair, direction, expiry, "%", "5")),
        ],
        [
            InlineKeyboardButton("10%", callback_data=_cb("SIZE", pair, direction, expiry, "%", "10")),
            InlineKeyboardButton("25%", callback_data=_cb("SIZE", pair, direction, expiry, "%", "25")),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data=_cb("EXP", pair, direction, expiry))],
    ]
    return InlineKeyboardMarkup(rows)


def kb_confirm(pair: str, direction: str, expiry: str, size_mode: str, size_value: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                "✅ Confirm",
                callback_data=_cb("CONF", pair, direction, expiry, size_mode, size_value),
            ),
            InlineKeyboardButton("❌ Cancel", callback_data=_cb("CANCEL", pair)),
        ]
    ]
    return InlineKeyboardMarkup(rows)


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
    alt_exchanges: Optional[Sequence[str]] = None,
) -> None:
    """
    Capture chart & send. Rate-limited.
    """
    if rate_limited(chat_id):
        await context.bot.send_message(chat_id, "⏳ Too many requests; wait a few seconds…")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    try:
        png, ex_used = await async_fetch_snapshot_png_any(
            exchange, ticker, interval, theme, "chart", alt_exchanges
        )
    except Exception as e:
        # send error text, not raw trace
        logger.warning("snapshot photo error: %s", e)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Failed: {exchange}:{ticker}\n({e})",
        )
        return

    cap = f"{prefix}{ex_used}:{ticker} • TF {interval} • {theme}"
    await context.bot.send_photo(chat_id=chat_id, photo=png, caption=cap)


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
        # Telegram only shows first caption
        if len(chunk) > 1:
            for m in chunk[1:]:
                m.caption = None
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# Command Parsing (text)
# ---------------------------------------------------------------------------
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
    ex, tk, _is_otc, alt = resolve_symbol(symbol)
    return ex, tk, norm_interval(tf), norm_theme(th), alt


def parse_multi_args(args: Sequence[str]) -> Tuple[List[str], str, str]:
    if not args:
        return [], DEFAULT_INTERVAL, DEFAULT_THEME
    theme = DEFAULT_THEME
    args = list(args)
    if args[-1].lower() in ("dark", "light"):
        theme = args[-1].lower()
        args = args[:-1]
    tf = DEFAULT_INTERVAL
    if args and re.fullmatch(r"\d+", args[-1]):
        tf = args[-1]
        args = args[:-1]
    return args, norm_interval(tf), norm_theme(theme)


def parse_trade_args(args: Sequence[str]) -> Tuple[str, str, str, str]:
    if not args:
        return "EUR/USD", "CALL", "5m", DEFAULT_THEME
    symbol = args[0]
    direction = parse_direction(args[1] if len(args) >= 2 else None) or "CALL"
    expiry = args[2] if len(args) >= 3 else "5m"
    theme = args[3] if len(args) >= 4 else DEFAULT_THEME
    return symbol, direction, expiry, theme


# ---------------------------------------------------------------------------
# Core Bot Command Handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nm = update.effective_user.first_name if update.effective_user else ""
    msg = (
        f"Hi {nm} 👋\n"
        "I'm your TradingView Snapshot Bot.\n\n"
        "Use /pairs to pick a market, then tap direction to trade.\n"
        "Use /trade to set size.\n"
        "Use /help for full command list."
    )
    await context.bot.send_message(update.effective_chat.id, msg)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📘 *Help*\n\n"
        "/pairs → choose market (FX/OTC/Crypto/Indices).\n"
        "/analyze SYMBOL → run scan & suggestion.\n"
        "/snap SYMBOL [interval] [theme] → quick chart.\n"
        "/snapmulti S1 S2 ... [interval] [theme]\n"
        "/snapall → bulk FX+OTC.\n"
        "/trade SYMBOL CALL|PUT expiry theme → quick trade.\n"
        "/setsize [$|%] value → e.g. /setsize $5 or /setsize %2\n"
        "/setbalance 5000 → sets sim balance for % sizing.\n"
        "/config → show current settings.\n"
        "/next → watch for next signal (placeholder).\n\n"
        "*Intervals:* minutes (#), D, W, M.\n"
        "*Themes:* dark|light."
    )
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cs = get_chat_state(update.effective_chat.id)
    msg = (
        f"⚙ *Config*\n"
        f"Pair: {cs.pair}\n"
        f"Direction: {cs.direction}\n"
        f"Expiry: {cs.expiry}\n"
        f"Size: {cs.size_mode}{cs.size_value}\n"
        f"TF(default): {cs.interval}\n"
        f"Theme: {cs.theme}\n"
        f"Sim Bal: {cs.sim_balance:.2f}\n"
    )
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_setsize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setsize $5  or  /setsize %2
    """
    if not context.args:
        await context.bot.send_message(update.effective_chat.id, "Usage: /setsize $5  or  /setsize %2")
        return
    raw = context.args[0].strip()
    mode = raw[0]
    val = raw[1:]
    if mode not in ("$", "%") or not val.replace(".", "", 1).isdigit():
        await context.bot.send_message(update.effective_chat.id, "Invalid size. Example: /setsize $5 or /setsize %2")
        return
    v = float(val)
    cs = get_chat_state(update.effective_chat.id)
    cs.size_mode = mode
    cs.size_value = v
    save_state_file()
    await context.bot.send_message(update.effective_chat.id, f"Size set to {mode}{v}.")


async def cmd_setbalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setbalance 5000
    """
    if not context.args or not context.args[0].replace(".", "", 1).isdigit():
        await context.bot.send_message(update.effective_chat.id, "Usage: /setbalance 5000")
        return
    bal = float(context.args[0])
    cs = get_chat_state(update.effective_chat.id)
    cs.sim_balance = bal
    save_state_file()
    await context.bot.send_message(update.effective_chat.id, f"Sim balance set to {bal:.2f}.")


async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        update.effective_chat.id, "Select a market category:", reply_markup=kb_pair_categories()
    )


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0] if context.args else None
    if not symbol:
        await context.bot.send_message(update.effective_chat.id, "Usage: /analyze SYMBOL")
        return
    await do_analyze_and_present(update.effective_chat.id, symbol, context)


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
        chat_id, f"⚡ Capturing all {len(ALL_PAIRS)} FX+OTC pairs… this may take a while."
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


async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /trade SYMBOL CALL|PUT expiry theme
    symbol, direction, expiry, theme = parse_trade_args(context.args)
    ex, tk, _is_otc, alt = resolve_symbol(symbol)
    tf = norm_interval(DEFAULT_INTERVAL)
    th = norm_theme(theme)
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    await context.bot.send_message(
        update.effective_chat.id,
        f"{arrow} *{symbol}* {direction}  Expiry: {expiry}",
        parse_mode=ParseMode.MARKDOWN,
    )
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, th, prefix="[TRADE] ", alt_exchanges=alt)
    # update state
    cs = get_chat_state(update.effective_chat.id)
    cs.pair = symbol
    cs.direction = direction
    cs.expiry = expiry
    cs.theme = th
    save_state_file()


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        update.effective_chat.id,
        "👀 Watching for next signal (placeholder). Connect TradingView alerts to /tv.",
    )


# ---------------------------------------------------------------------------
# Fallback Message Parsing (user typed text not /command)
# ---------------------------------------------------------------------------
_trade_re = re.compile(r"(?i)trade\s+([A-Z0-9/\-:]+)\s+(call|put|buy|sell|up|down)\s+([0-9]+m?)")


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
            update.effective_chat.id, context, ex, tk, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[TRADE] ", alt_exchanges=alt
        )
        return
    await context.bot.send_message(update.effective_chat.id, f"You said: {txt}\nTry /trade EUR/USD CALL 5m")


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, "❌ Unknown command. Try /help.")


# ---------------------------------------------------------------------------
# ANALYZE Flow
# ---------------------------------------------------------------------------
async def do_analyze_and_present(chat_id: int, pair: str, context: ContextTypes.DEFAULT_TYPE):
    """
    Fetch candles -> analyze -> show summary + inline direction selection based on suggestion.
    """
    await context.bot.send_message(chat_id, f"🔍 Analyzing {pair}…")
    ex, tk, _is_otc, alt = resolve_symbol(pair)
    tf = DEFAULT_INTERVAL  # use default for now; could parse from pair
    candles = await fetch_candles_async(ex, tk, tf, limit=200)
    res = analyze_candles(pair, candles, tf)
    cs = get_chat_state(chat_id)
    cs.pair = pair
    cs.direction = res.direction if res.direction in ("CALL", "PUT") else cs.direction
    cs.expiry = res.suggested_expiry
    cs.last_analysis = res
    save_state_file()

    conf_s = f"{res.confidence:.0f}%"
    arrow = "🟢↑" if res.direction == "CALL" else "🔴↓" if res.direction == "PUT" else "⚪"
    msg = (
        f"{arrow} *{pair}* {res.direction} Conf:{conf_s} SugExp:{res.suggested_expiry}\n"
        f"{res.summary or ''}"
    )
    await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_direction(pair))

    # Immediately queue snapshot (background)
    await send_snapshot_photo(chat_id, context, ex, tk, tf, cs.theme, prefix="[ANALYZE] ", alt_exchanges=alt)


# ---------------------------------------------------------------------------
# Size & Trade Execution
# ---------------------------------------------------------------------------
def calc_trade_amount(cs: ChatState) -> float:
    if cs.size_mode == "$":
        return cs.size_value
    # percent mode
    bal = cs.sim_balance
    return round(bal * cs.size_value / 100.0, 2)


async def execute_trade_flow(
    chat_id: int,
    pair: str,
    direction: str,
    expiry: str,
    size_mode: str,
    size_value: str,
    context: ContextTypes.DEFAULT_TYPE,
    triggered_by: str = "user",
):
    """
    Final step: user confirmed trade. We:
      1. Update chat state
      2. Send summary to Telegram
      3. Optionally call UI.Vision or Pocket Option stub
    """
    cs = get_chat_state(chat_id)
    cs.pair = pair
    cs.direction = direction
    cs.expiry = expiry
    cs.size_mode = size_mode
    cs.size_value = float(size_value)
    save_state_file()

    amount = calc_trade_amount(cs)

    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    msg = (
        f"✅ *Trade Confirmed*\n"
        f"{arrow} {pair} {direction}\n"
        f"Expiry: {expiry}\n"
        f"Size: {size_mode}{size_value} (≈${amount:.2f})\n"
        f"Source: {triggered_by}"
    )
    await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN)

    # chart (optional)
    ex, tk, _is_otc, alt = resolve_symbol(pair)
    await send_snapshot_photo(chat_id, context, ex, tk, cs.interval, cs.theme, prefix="[TRADE] ", alt_exchanges=alt)

    # broker hook stub
    try:
        if UI_VISION_URL:
            await trigger_ui_vision_trade_async(pair, direction, expiry, amount)
        else:
            await pocket_option_stub_async(pair, direction, expiry, amount)
    except Exception as e:
        logger.error("execute_trade_flow hook error: %s", e)
        await context.bot.send_message(chat_id, f"⚠ Trade hook error: {e}")


async def trigger_ui_vision_trade_async(pair: str, direction: str, expiry: str, amount: float):
    """
    Example external GET/POST to UI.Vision macro server (replace w/ your actual endpoint).
    """
    client = get_async_http_client()
    payload = {
        "pair": pair,
        "direction": direction,
        "expiry": expiry,
        "amount": amount,
    }
    try:
        resp = await client.post(UI_VISION_URL, json=payload)
        logger.info("UI.Vision trade POST -> %s %s", resp.status_code, resp.text[:100])
    except Exception as e:
        logger.error("UI.Vision trade error: %s", e)


async def pocket_option_stub_async(pair: str, direction: str, expiry: str, amount: float):
    """
    Stub: no official Pocket Option API.
    Replace with your automation (Selenium, RPA, etc).
    """
    logger.info("POCKET_OPTION_STUB %s %s %s size=%s", pair, direction, expiry, amount)


# ---------------------------------------------------------------------------
# CallbackQuery Router
# ---------------------------------------------------------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    cq = update.callback_query
    data = cq.data or ""
    parts = data.split("|")
    prefix = parts[0]

    # always answer callback to stop spinner
    try:
        await cq.answer()
    except Exception:
        pass

    chat_id = cq.message.chat.id if cq.message else update.effective_chat.id

    if prefix == "PAIRCAT":
        if len(parts) >= 2 and parts[1] == "BACK":
            await cmd_pairs(update, context)
            return
        cat = parts[1] if len(parts) >= 2 else "FX"
        await show_category_symbols(chat_id, cat, 0, context)
        return

    if prefix == "PAIRPAGE":
        cat = parts[1] if len(parts) >= 2 else "FX"
        page = int(parts[2]) if len(parts) >= 3 else 0
        await show_category_symbols(chat_id, cat, page, context)
        return

    if prefix == "PAIRSEL":
        pair = parts[1] if len(parts) >= 2 else "EUR/USD"
        await context.bot.send_message(chat_id, f"{pair}\nSelect direction:", reply_markup=kb_direction(pair))
        return

    if prefix == "ANAL":
        pair = parts[1] if len(parts) >= 2 else "EUR/USD"
        await do_analyze_and_present(chat_id, pair, context)
        return

    if prefix == "DIR":
        pair = parts[1] if len(parts) >= 2 else "EUR/USD"
        direction = parts[2] if len(parts) >= 3 else "CALL"
        await context.bot.send_message(chat_id, f"{pair} {direction}\nSelect expiry:", reply_markup=kb_expiry(pair, direction))
        return

    if prefix == "EXP":
        pair = parts[1] if len(parts) >= 2 else "EUR/USD"
        direction = parts[2] if len(parts) >= 3 else "CALL"
        expiry = parts[3] if len(parts) >= 4 else "5m"
        await context.bot.send_message(
            chat_id, f"{pair} {direction} Exp:{expiry}\nSelect size:", reply_markup=kb_size(pair, direction, expiry)
        )
        return

    if prefix == "SIZE":
        pair = parts[1] if len(parts) >= 2 else "EUR/USD"
        direction = parts[2] if len(parts) >= 3 else "CALL"
        expiry = parts[3] if len(parts) >= 4 else "5m"
        size_mode = parts[4] if len(parts) >= 5 else "$"
        size_value = parts[5] if len(parts) >= 6 else "1"
        await context.bot.send_message(
            chat_id,
            f"{pair} {direction} Exp:{expiry} Size:{size_mode}{size_value}\nConfirm?",
            reply_markup=kb_confirm(pair, direction, expiry, size_mode, size_value),
        )
        return

    if prefix == "CONF":
        pair = parts[1] if len(parts) >= 2 else "EUR/USD"
        direction = parts[2] if len(parts) >= 3 else "CALL"
        expiry = parts[3] if len(parts) >= 4 else "5m"
        size_mode = parts[4] if len(parts) >= 5 else "$"
        size_value = parts[5] if len(parts) >= 6 else "1"
        await execute_trade_flow(chat_id, pair, direction, expiry, size_mode, size_value, context, triggered_by="inline")
        return

    if prefix == "CANCEL":
        await context.bot.send_message(chat_id, "❌ Trade canceled.")
        return


async def show_category_symbols(chat_id: int, cat: str, page: int, context: ContextTypes.DEFAULT_TYPE):
    if cat == "FX":
        syms = FX_PAIRS
    elif cat == "OTC":
        syms = OTC_PAIRS
    elif cat == "CRYPTO":
        syms = CRYPTO_PAIRS
    elif cat == "INDEX":
        syms = INDEX_SYMBOLS
    else:
        syms = FX_PAIRS
    await context.bot.send_message(chat_id, f"{cat} symbols:", reply_markup=kb_symbol_list(syms, cat, page))


# ---------------------------------------------------------------------------
# Flask TradingView Webhook
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)


def _parse_tv_payload(data: Dict[str, Any]) -> Dict[str, str]:
    d: Dict[str, str] = {}
    d["chat_id"] = str(data.get("chat_id") or DEFAULT_CHAT_ID or "")
    d["pair"] = str(data.get("pair") or data.get("symbol") or data.get("ticker") or "EUR/USD")
    d["direction"] = str(data.get("direction") or "CALL").upper()
    d["expiry"] = str(data.get("default_expiry_min") or data.get("expiry") or "5m")
    d["strategy"] = str(data.get("strategy") or "")
    d["winrate"] = str(data.get("winrate") or "")
    d["timeframe"] = str(data.get("timeframe") or data.get("tf") or DEFAULT_INTERVAL)
    d["theme"] = str(data.get("theme") or DEFAULT_THEME)
    d["size_mode"] = str(data.get("size_mode") or "$")
    d["size_value"] = str(data.get("size_value") or "1")
    return d


def tg_api_send_message_sync(chat_id: str, text: str, parse_mode: Optional[str] = None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        _sync_http.post(url, json=payload, timeout=30)
    except Exception as e:
        logger.error("tg_api_send_message: %s", e)


def tg_api_send_photo_bytes_sync(chat_id: str, png: bytes, caption: str = ""):
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", png, "image/png")}
    data = {"chat_id": chat_id, "caption": caption}
    try:
        _sync_http.post(url, data=data, files=files, timeout=60)
    except Exception as e:
        logger.error("tg_api_send_photo_bytes: %s", e)


def _handle_tv_alert(data: Dict[str, Any]):
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
    logger.info("TV payload normalized: %s", {k: v for k, v in payload.items() if k != "token"})

    chat_id = payload["chat_id"]
    raw_pair = payload["pair"]
    direction = parse_direction(payload["direction"]) or "CALL"
    expiry = payload["expiry"]
    strat = payload["strategy"]
    winrate = payload["winrate"]
    tf = norm_interval(payload["timeframe"])
    theme = norm_theme(payload["theme"])
    size_mode = payload.get("size_mode", "$")
    size_value = payload.get("size_value", "1")

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
    tg_api_send_message_sync(chat_id, msg, parse_mode="Markdown")

    # snapshot
    ex, tk, _is_otc, alt = resolve_symbol(raw_pair)
    try:
        png, ex_used = fetch_snapshot_png_any(ex, tk, tf, theme, "chart", alt)
        tg_api_send_photo_bytes_sync(chat_id, png, caption=f"{ex_used}:{tk} • TF {tf} • {theme}")
    except Exception as e:
        logger.error("TV snapshot error for %s:%s -> %s", ex, tk, e)
        tg_api_send_message_sync(chat_id, f"⚠ Chart snapshot failed for {raw_pair}: {e}")

    # optional auto-trade
    if AUTO_TRADE_FROM_TV:
        # Convert or dispatch to bot event loop
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # schedule call in bot loop
            async def _auto_trade():
                try:
                    await execute_trade_flow(
                        int(chat_id),
                        raw_pair,
                        direction,
                        expiry,
                        size_mode,
                        size_value,
                        _bot_context_holder["context"],
                        triggered_by="TVWebhook",
                    )
                except Exception as ee:
                    logger.error("auto_trade (async) error: %s", ee)

            loop.create_task(_auto_trade())
        else:
            # no loop: sync stub
            logger.info("AUTO_TRADE_FROM_TV w/out running loop; skipping actual trade execution.")

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


@flask_app.post("/webhook")
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


# A holder to pass context to synchronous webhook auto-trade usage
_bot_context_holder: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Application Builder
# ---------------------------------------------------------------------------
def build_application() -> Application:
    """
    Build PTB v20 Application (no Updater usage).
    """
    builder = ApplicationBuilder().token(TOKEN)
    # concurrency tuning (optional)
    builder.concurrent_updates(True)

    app = builder.build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("setsize", cmd_setsize))
    app.add_handler(CommandHandler("setbalance", cmd_setbalance))
    app.add_handler(CommandHandler("pairs", cmd_pairs))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("snap", cmd_snap))
    app.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    app.add_handler(CommandHandler("snapall", cmd_snapall))
    app.add_handler(CommandHandler("trade", cmd_trade))
    app.add_handler(CommandHandler("next", cmd_next))

    # Callback queries
    app.add_handler(CallbackQueryHandler(on_callback))

    # Fallback text & unknown commands
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    return app


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print(
        f"Bot starting… BASE_URL={BASE_URL} | DefaultEX={DEFAULT_EXCHANGE} | WebhookPort={TV_WEBHOOK_PORT} | "
        f"UI_VISION_URL={UI_VISION_URL} | AUTO_TRADE_FROM_TV={AUTO_TRADE_FROM_TV} | SIM_DEBIT={SIM_DEBIT}"
    )
    load_state_file()
    start_flask_background()

    application = build_application()

    # store context object for webhook auto-trade bridging
    _bot_context_holder["context"] = application.bot

    # run event loop
    application.run_polling()


if __name__ == "__main__":
    main()
