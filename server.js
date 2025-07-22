#!/usr/bin/env python
"""
TradingView → Telegram Snapshot Bot (Inline Edition)
====================================================

Features
--------
• python-telegram-bot v20+ async architecture.
• Inline keyboards:
    /pairs → pick asset class → pick pair → Direction → Expiry (1m/3m/5m/15m) →
    Size Mode ($ or %) → preset sizes → Confirm trade (manual or auto).
• Supports FX, OTC (Pocket Option style), Indices, Crypto (expandable).
• Screenshot backend (Node/Puppeteer) with flexible endpoints:
    /snapshot/<pair>?tf=1&theme=dark  (preferred)
    /run?exchange=FX&ticker=EURUSD&interval=1&theme=dark  (fallback)
• Accepts PNG even if server responds 4xx/5xx but body is PNG.
• Per-chat rate limiting + global throttle.
• Safe logging (binary bodies truncated & sanitized).
• Rotating file log + UTF‑8 console log.
• TradingView webhook (/tv, /webhook) → Telegram alert + snapshot + *optional* auto-trade trigger.
• Optional UI.Vision automation hook to broker (Pocket Option, Quotex, etc).
• Optional simulated debit log instead of live trading.
• JSON persistence of user defaults, last selections, trade stats.
• Clean, branded help UI.

Author: ChatGPT assist (w/ user collaboration)
License: MIT
"""

from __future__ import annotations

import os
import io
import re
import sys
import json
import time
import base64
import queue
import types
import asyncio
import threading
import traceback
from dataclasses import dataclass, field, asdict
from typing import (
    Any,
    Dict,
    List,
    Tuple,
    Optional,
    Callable,
    Union,
)

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
try:
    import httpx
except ImportError:  # lightweight fallback if user lacks httpx
    raise SystemExit("Please install httpx: pip install httpx")

try:
    from flask import Flask, jsonify, request
except ImportError:
    raise SystemExit("Please install Flask: pip install flask")

try:
    # python-telegram-bot v20+ modules
    from telegram import (
        Update,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        InputMediaPhoto,
    )
    from telegram.constants import ChatAction, ParseMode
    from telegram.ext import (
        ApplicationBuilder,
        ContextTypes,
        CommandHandler,
        MessageHandler,
        CallbackQueryHandler,
        filters,
    )
except ImportError:
    raise SystemExit(
        "Please install python-telegram-bot v20+: pip install 'python-telegram-bot[rate-limiter]'"
    )

# Optional: PTB AIORateLimiter
try:
    from telegram.ext import AIORateLimiter
    HAVE_AIO_RL = True
except ImportError:
    HAVE_AIO_RL = False

# ---------------------------------------------------------------------------
# Logging setup (UTF‑8 safe console + rotating file)
# ---------------------------------------------------------------------------
import logging
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "tvsnapshotbot.log")

# Custom stream handler w/ encoding for Windows consoles
_console_handler = logging.StreamHandler(stream=sys.stdout)
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
))

_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
))

logging.basicConfig(level=logging.INFO, handlers=[_console_handler, _file_handler])
logger = logging.getLogger("TVSnapBot")


# ---------------------------------------------------------------------------
# Environment / Config
# ---------------------------------------------------------------------------
TOKEN            = os.environ.get("TELEGRAM_BOT_TOKEN")
DEFAULT_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID")  # optional default target
BASE_URL         = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000")
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "FX")
DEFAULT_INTERVAL = os.environ.get("DEFAULT_INTERVAL", "1")
DEFAULT_THEME    = os.environ.get("DEFAULT_THEME", "dark")
TV_WEBHOOK_PORT  = int(os.environ.get("TV_WEBHOOK_PORT", "8081"))
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET")  # optional
UI_VISION_URL    = os.environ.get("UI_VISION_URL")   # optional automation trigger
AUTO_TRADE_FROM_TV = os.environ.get("AUTO_TRADE_FROM_TV", "").lower() in ("1", "true", "yes")
SIM_DEBIT        = os.environ.get("SIM_DEBIT", "").lower() in ("1", "true", "yes")

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment.")

# ---------------------------------------------------------------------------
# HTTP Clients
# ---------------------------------------------------------------------------
# requests is occasionally handy synchronously (Flask thread)
import requests
_req = requests.Session()

# async usage: httpx
_httpx = httpx.Client(timeout=30.0)
_async_httpx = httpx.AsyncClient(timeout=30.0)


# ---------------------------------------------------------------------------
# Utility: safe logging of binary or long text
# ---------------------------------------------------------------------------
def safe_snip(obj: Union[str, bytes], max_len: int = 200) -> str:
    if isinstance(obj, bytes):
        try:
            s = obj.decode("utf-8", errors="replace")
        except Exception:
            s = repr(obj)
    else:
        s = str(obj)
    # collapse newlines
    s = s.replace("\r", "\\r").replace("\n", "\\n")
    if len(s) > max_len:
        s = s[:max_len] + "...(trunc)"
    # ensure printable
    return "".join(ch if 31 < ord(ch) < 127 else ch for ch in s)


# ---------------------------------------------------------------------------
# Asset universe
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

INDEX_SYMBOLS: List[str] = [
    "US30","SPX500","NAS100","DE40","UK100","JP225",
    "FR40","ES35","HK50","AU200",
]

CRYPTO_PAIRS: List[str] = [
    "BTC/USD","ETH/USD","SOL/USD","XRP/USD","LTC/USD",
    "ADA/USD","DOGE/USD","BNB/USD","DOT/USD","LINK/USD",
]

ALL_PAIRS: List[str] = FX_PAIRS + OTC_PAIRS + INDEX_SYMBOLS + CRYPTO_PAIRS

# Predefined categories for /pairs UI
CATEGORY_MAP: Dict[str, List[str]] = {
    "FX": FX_PAIRS,
    "OTC": OTC_PAIRS,
    "INDICES": INDEX_SYMBOLS,
    "CRYPTO": CRYPTO_PAIRS,
}


# ---------------------------------------------------------------------------
# Exchange mapping / alt fallback lists
# ---------------------------------------------------------------------------
# Exchange fallbacks we cycle when fetching charts.
KNOWN_EXCHANGES_FX = ["FX", "FX_IDC", "OANDA", "FOREXCOM", "IDC"]
KNOWN_EXCHANGES_OTC = ["QUOTEX", "IQOPTION", "CURRENCY"]  # stub alt names
KNOWN_EXCHANGES_IDX = ["INDEX", "FOREXCOM", "OANDA", "IDC"]
KNOWN_EXCHANGES_CRY = ["BINANCE", "COINBASE", "KRAKEN", "BITSTAMP", "CRYPTO"]

# canonicalizer
def _canon_key(pair: str) -> str:
    return pair.strip().upper().replace(" ", "").replace("/", "").replace("-", "")

# For each canonical symbol, define primary exchange + alt fallback list.
PAIR_XREF: Dict[str, Tuple[str, str, List[str]]] = {}

def _add_xref(raw: str, primary: str, ticker: str, alts: List[str]) -> None:
    PAIR_XREF[_canon_key(raw)] = (primary, ticker, alts)

# FX: user wants DEFAULT_EXCHANGE primary (often FX)
for p in FX_PAIRS:
    tk = p.replace("/", "")
    _add_xref(p, DEFAULT_EXCHANGE, tk, KNOWN_EXCHANGES_FX)

# OTC underlying -> map to QUOTEX primary; alt includes FX if needed
_underlying_otc = {
    "EUR/USD-OTC":"EURUSD","GBP/USD-OTC":"GBPUSD","USD/JPY-OTC":"USDJPY",
    "USD/CHF-OTC":"USDCHF","AUD/USD-OTC":"AUDUSD","NZD/USD-OTC":"NZDUSD",
    "USD/CAD-OTC":"USDCAD","EUR/GBP-OTC":"EURGBP","EUR/JPY-OTC":"EURJPY",
    "GBP/JPY-OTC":"GBPJPY","AUD/CHF-OTC":"AUDCHF","EUR/CHF-OTC":"EURCHF",
    "KES/USD-OTC":"USDKES","MAD/USD-OTC":"USDMAD","USD/BDT-OTC":"USDBDT",
    "USD/MXN-OTC":"USDMXN","USD/MYR-OTC":"USDMYR","USD/PKR-OTC":"USDPKR",
}
for raw, tk in _underlying_otc.items():
    _add_xref(raw, "QUOTEX", tk, KNOWN_EXCHANGES_OTC + KNOWN_EXCHANGES_FX)

# Indices
_idx_map = {
    "US30": "DXY",    # placeholder; change to "DJI" if your backend supports
    "SPX500": "SPX",
    "NAS100": "NDX",
    "DE40": "DE40",
    "UK100": "UKX",
    "JP225": "JP225",
    "FR40": "FR40",
    "ES35": "ES35",
    "HK50": "HK50",
    "AU200": "AU200",
}
for raw, tk in _idx_map.items():
    _add_xref(raw, "INDEX", tk, KNOWN_EXCHANGES_IDX)

# Crypto
_crypto_map = {
    "BTC/USD":"BTCUSD","ETH/USD":"ETHUSD","SOL/USD":"SOLUSD","XRP/USD":"XRPUSD","LTC/USD":"LTCUSD",
    "ADA/USD":"ADAUSD","DOGE/USD":"DOGEUSD","BNB/USD":"BNBUSD","DOT/USD":"DOTUSD","LINK/USD":"LINKUSD",
}
for raw, tk in _crypto_map.items():
    _add_xref(raw, "BINANCE", tk, KNOWN_EXCHANGES_CRY)


# ---------------------------------------------------------------------------
# Interval & Theme Normalization
# ---------------------------------------------------------------------------
def norm_interval(tf: str) -> str:
    if not tf:
        return DEFAULT_INTERVAL
    t = tf.strip().lower()
    if t.endswith("m") and t[:-1].isdigit():
        return t[:-1]  # raw minutes
    if t.endswith("h") and t[:-1].isdigit():
        return str(int(t[:-1]) * 60)
    if t in ("d","1d","day"):
        return "D"
    if t in ("w","1w","week"):
        return "W"
    if t in ("mo","mth","1m","month"):
        return "M"
    if t.isdigit():
        return t
    return DEFAULT_INTERVAL


def norm_theme(val: str) -> str:
    return "light" if (val and val.lower().startswith("l")) else "dark"


# ---------------------------------------------------------------------------
# Direction parsing / canonicalization
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Expiry parsing
# ---------------------------------------------------------------------------
VALID_EXPIRIES = ["1m","3m","5m","15m","30m","1h"]  # expand as needed

def norm_expiry(exp: str) -> str:
    if not exp:
        return "5m"
    e = exp.strip().lower()
    if e in VALID_EXPIRIES:
        return e
    # accept raw digits -> minutes
    if e.isdigit():
        return f"{e}m"
    # catch "5" or "5min"
    m = re.match(r"(\d+)\s*(m|min)?$", e)
    if m:
        return f"{m.group(1)}m"
    return "5m"


# ---------------------------------------------------------------------------
# Trade size presets
# ---------------------------------------------------------------------------
SIZE_PRESETS_USD = [1, 5, 10, 25, 50, 100]
SIZE_PRESETS_PCT = [1, 2, 5, 10, 25, 50, 100]

# Note: per-user custom sizes stored in persistence.


# ---------------------------------------------------------------------------
# Persistence (user settings, last trades, stats)
# ---------------------------------------------------------------------------
STATE_FILE = "tvsnapshot_state.json"

@dataclass
class UserSettings:
    default_interval: str = DEFAULT_INTERVAL
    default_theme: str = DEFAULT_THEME
    default_size_mode: str = "usd"  # or 'pct'
    default_size_value: float = 10.0
    auto_confirm_trade: bool = False

@dataclass
class UserStats:
    trades_sent: int = 0
    trades_signaled: int = 0
    last_pair: Optional[str] = None
    last_direction: Optional[str] = None
    last_expiry: Optional[str] = None
    last_size_mode: Optional[str] = None
    last_size_value: Optional[float] = None

@dataclass
class PersistState:
    users: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    version: int = 1

    def get_user_settings(self, user_id: int) -> UserSettings:
        entry = self.users.setdefault(str(user_id), {})
        if "settings" not in entry:
            entry["settings"] = asdict(UserSettings())
        sdict = entry["settings"]
        return UserSettings(**sdict)

    def set_user_settings(self, user_id: int, settings: UserSettings) -> None:
        entry = self.users.setdefault(str(user_id), {})
        entry["settings"] = asdict(settings)

    def get_user_stats(self, user_id: int) -> UserStats:
        entry = self.users.setdefault(str(user_id), {})
        if "stats" not in entry:
            entry["stats"] = asdict(UserStats())
        sdict = entry["stats"]
        return UserStats(**sdict)

    def set_user_stats(self, user_id: int, stats: UserStats) -> None:
        entry = self.users.setdefault(str(user_id), {})
        entry["stats"] = asdict(stats)


_PERSIST = PersistState()

def load_state() -> None:
    if not os.path.exists(STATE_FILE):
        logger.info("No state file found; starting fresh.")
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _PERSIST.users = data.get("users", {})
        _PERSIST.version = data.get("version", 1)
        logger.info("Loaded state from %s (%d users).", STATE_FILE, len(_PERSIST.users))
    except Exception as e:
        logger.error("Failed loading state: %s", e)

def save_state() -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(_PERSIST), f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.error("Failed saving state: %s", e)


# ---------------------------------------------------------------------------
# Resolve symbol -> (primary_ex, ticker, is_otc, alt_exchanges)
# ---------------------------------------------------------------------------
def resolve_symbol(raw: str) -> Tuple[str, str, bool, List[str]]:
    if not raw:
        return DEFAULT_EXCHANGE, "EURUSD", False, KNOWN_EXCHANGES_FX
    s = raw.strip().upper()
    is_otc = "-OTC" in s
    key = _canon_key(s)
    if key in PAIR_XREF:
        primary, tk, alts = PAIR_XREF[key]
        return primary, tk, is_otc, alts
    # fallback parse exchange:ticker
    if ":" in s:
        ex, tk = s.split(":", 1)
        return ex, tk, is_otc, []
    # fallback raw cleanup
    tk = re.sub(r"[^A-Z0-9]", "", s)
    alts = KNOWN_EXCHANGES_OTC if is_otc else KNOWN_EXCHANGES_FX
    return DEFAULT_EXCHANGE, tk, is_otc, alts


# ---------------------------------------------------------------------------
# Screenshot backend
# ---------------------------------------------------------------------------
# Several attempts:
#  1. /snapshot/<ticker>?tf=..&theme=..
#  2. /snapshot/<exchange>:<ticker>?tf=..   (some Node servers)
#  3. /run?exchange=..&ticker=..&interval=..&theme=..
#
# Accept PNG in body even if 500/404 (some Node wrappers incorrectly set status).
#
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

def _try_url(url: str, timeout: float = 75.0) -> Tuple[bool, Optional[bytes], str]:
    """Return (success, png_bytes_or_none, err_text)."""
    try:
        resp = _req.get(url, timeout=timeout)
    except Exception as e:
        return False, None, f"HTTP err: {e}"
    ct = resp.headers.get("Content-Type", "")
    data = resp.content
    # Accept good status + image
    if resp.status_code == 200 and ct.lower().startswith("image"):
        return True, data, ""
    # Accept PNG magic even if error status
    if data.startswith(PNG_MAGIC):
        return True, data, ""
    return False, None, f"HTTP {resp.status_code}: {safe_snip(data)}"


def node_start_browser() -> None:
    """Ping optional start endpoint (non-fatal)."""
    try:
        url = f"{BASE_URL}/start-browser"
        resp = _req.get(url, timeout=10)
        logger.debug("start-browser resp=%s", resp.status_code)
    except Exception as e:
        logger.debug("start-browser ping failed: %s", e)


def fetch_snapshot_png_any(
    primary_ex: str,
    tk: str,
    interval: str,
    theme: str,
    base: str = "chart",
    alt_exchanges: Optional[List[str]] = None,
) -> Tuple[bytes, str]:
    """
    Attempt multi-endpoint PNG retrieval. Returns (png_bytes, exchange_used).
    Raises RuntimeError if all fail.
    """
    tried: List[str] = []
    last_err = "no_attempts"

    # Compose tries
    # For each exchange in primary + alt list + dedup
    exchanges: List[str] = []
    if primary_ex:
        exchanges.append(primary_ex)
    if alt_exchanges:
        exchanges.extend(alt_exchanges)
    # add global known classes for broad fallback
    exchanges.extend(["FX", "FX_IDC", "OANDA", "FOREXCOM", "IDC", "QUOTEX", "CURRENCY", "BINANCE"])
    # dedup preserve order
    seen = set()
    dedup: List[str] = []
    for e in exchanges:
        eu = e.upper()
        if eu not in seen:
            dedup.append(eu)
            seen.add(eu)

    # Try each
    for ex in dedup:
        # 1. snapshot route: /snapshot/<ex>/<tk>
        # 2. snapshot short: /snapshot/<tk>
        # 3. run route
        urls = [
            f"{BASE_URL}/snapshot/{ex}:{tk}?tf={interval}&theme={theme}",
            f"{BASE_URL}/snapshot/{tk}?tf={interval}&theme={theme}",
            f"{BASE_URL}/run?base={base}&exchange={ex}&ticker={tk}&interval={interval}&theme={theme}",
            f"{BASE_URL}/run?exchange={ex}&ticker={tk}&interval={interval}&theme={theme}",
        ]
        for u in urls:
            tried.append(f"{ex}|{u}")
            ok, png, err = _try_url(u)
            if ok and png:
                logger.info("Snapshot success %s:%s via %s (%d bytes)", ex, tk, u, len(png))
                return png, ex
            last_err = err
            logger.warning("Snapshot fail %s:%s -> %s", ex, tk, safe_snip(err))
        # throttle between exchanges
        time.sleep(0.5)

    raise RuntimeError(f"All exchanges failed for {tk}. Last error: {last_err}. Tried: {tried}")


# ---------------------------------------------------------------------------
# Rate limiting (per chat + global)
# ---------------------------------------------------------------------------
LAST_SNAPSHOT_PER_CHAT: Dict[int, float] = {}
RATE_LIMIT_SECONDS = 3.0

GLOBAL_LAST_SNAPSHOT = 0.0
GLOBAL_MIN_GAP = 0.75  # seconds

def rate_limited(chat_id: int) -> bool:
    now = time.time()
    last = LAST_SNAPSHOT_PER_CHAT.get(chat_id, 0.0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    LAST_SNAPSHOT_PER_CHAT[chat_id] = now
    return False

def global_throttle_wait() -> None:
    global GLOBAL_LAST_SNAPSHOT
    now = time.time()
    gap = now - GLOBAL_LAST_SNAPSHOT
    if gap < GLOBAL_MIN_GAP:
        time.sleep(GLOBAL_MIN_GAP - gap)
    GLOBAL_LAST_SNAPSHOT = time.time()


# ---------------------------------------------------------------------------
# Inline Interaction State (in-memory ephemeral)
# ---------------------------------------------------------------------------
# We track per-user ongoing flows:
# flow: 'trade' or None
# selected: pair, direction, expiry, size_mode, size_value
USER_FSM: Dict[int, Dict[str, Any]] = {}


def _fsm_get(uid: int) -> Dict[str, Any]:
    return USER_FSM.setdefault(uid, {"flow": None})


def _fsm_reset(uid: int) -> None:
    USER_FSM[uid] = {"flow": None}


# ---------------------------------------------------------------------------
# CallbackData Helpers
# ---------------------------------------------------------------------------
# We keep callback data under ~64 chars. pattern:
#   CAT:<name>              category selected
#   PR:<pair_key>           pair selected
#   DIR:C / DIR:P           direction
#   EXP:1m / EXP:5m ...
#   SZM:usd / SZM:pct       size mode
#   SIZ:10 / SIZ:25         size value
#   CONF:ok                 confirm
#   BK:<level>              back nav
#
# We'll stash full raw pair label in user_fsm to avoid losing spaces/slashes.

def cd_cat(cat: str) -> str: return f"CAT:{cat}"
def cd_pr(key: str)  -> str: return f"PR:{key}"
def cd_dir(d: str)   -> str: return f"DIR:{d}"
def cd_exp(e: str)   -> str: return f"EXP:{e}"
def cd_szm(m: str)   -> str: return f"SZM:{m}"
def cd_siz(v: Union[int,float]) -> str: return f"SIZ:{v}"
def cd_conf() -> str: return "CONF:ok"
def cd_back(level: str) -> str: return f"BK:{level}"


# ---------------------------------------------------------------------------
# Telegram send helpers (async)
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
) -> None:
    """Send one snapshot photo to chat."""
    if rate_limited(chat_id):
        await context.bot.send_message(chat_id, "⏳ Too many requests; wait a few seconds…")
        return

    # user feedback
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)

    # background node warm-up
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
    for ex, tk, lab, alts in pairs:
        try:
            png, ex_used = fetch_snapshot_png_any(ex, tk, interval, theme, "chart", alts)
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
) -> None:
    for i in range(0, len(media_items), chunk_size):
        chunk = media_items[i:i+chunk_size]
        if not chunk:
            continue
        # only first caption is guaranteed visible
        if len(chunk) > 1:
            for m in chunk[1:]:
                m.caption = None
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------
def parse_snap_args(args: List[str]) -> Tuple[str, str, str, str, List[str]]:
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


def parse_multi_args(args: List[str]) -> Tuple[List[str], str, str]:
    # /snapmulti P1 P2 ... [interval] [theme]
    if not args:
        return [], DEFAULT_INTERVAL, DEFAULT_THEME
    theme = DEFAULT_THEME
    if args[-1].lower() in ("dark","light"):
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
    expiry string is returned normalized; we don't convert to ms here.
    """
    if not args:
        return "EUR/USD","CALL","5m",DEFAULT_THEME
    symbol = args[0]
    direction = parse_direction(args[1] if len(args)>=2 else None) or "CALL"
    expiry = norm_expiry(args[2] if len(args)>=3 else "5m")
    theme = args[3] if len(args)>=4 else DEFAULT_THEME
    return symbol, direction, expiry, theme


# ---------------------------------------------------------------------------
# Inline Keyboard Builders
# ---------------------------------------------------------------------------
def kb_main_categories() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("💱 FX", callback_data=cd_cat("FX")),
            InlineKeyboardButton("🕒 OTC", callback_data=cd_cat("OTC")),
        ],
        [
            InlineKeyboardButton("📈 Indices", callback_data=cd_cat("INDICES")),
            InlineKeyboardButton("💹 Crypto", callback_data=cd_cat("CRYPTO")),
        ],
    ]
    return InlineKeyboardMarkup(rows)

def kb_pairs_for_category(cat: str) -> InlineKeyboardMarkup:
    pairs = CATEGORY_MAP.get(cat, [])
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for p in pairs:
        key = _canon_key(p)
        # show short label: "EUR/USD" or raw
        label = p
        row.append(InlineKeyboardButton(label, callback_data=cd_pr(key)))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅ Back", callback_data=cd_back("CATMENU"))])
    return InlineKeyboardMarkup(rows)

def kb_direction() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🟢 CALL / BUY", callback_data=cd_dir("CALL")),
            InlineKeyboardButton("🔴 PUT / SELL", callback_data=cd_dir("PUT")),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data=cd_back("PAIRSEL"))],
    ]
    return InlineKeyboardMarkup(rows)

def kb_expiry() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("1m", callback_data=cd_exp("1m")),
            InlineKeyboardButton("3m", callback_data=cd_exp("3m")),
            InlineKeyboardButton("5m", callback_data=cd_exp("5m")),
            InlineKeyboardButton("15m", callback_data=cd_exp("15m")),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data=cd_back("DIRSEL"))],
    ]
    return InlineKeyboardMarkup(rows)

def kb_size_mode() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("$ Size", callback_data=cd_szm("usd")),
            InlineKeyboardButton("% of Bal", callback_data=cd_szm("pct")),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data=cd_back("EXPSEL"))],
    ]
    return InlineKeyboardMarkup(rows)

def kb_size_values(mode: str) -> InlineKeyboardMarkup:
    if mode == "pct":
        vals = SIZE_PRESETS_PCT
    else:
        vals = SIZE_PRESETS_USD
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for v in vals:
        txt = f"{v}%" if mode == "pct" else f"${v}"
        row.append(InlineKeyboardButton(txt, callback_data=cd_siz(v)))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅ Back", callback_data=cd_back("SIZMODE"))])
    return InlineKeyboardMarkup(rows)

def kb_confirm() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("✅ Confirm Trade", callback_data=cd_conf()),
            InlineKeyboardButton("❌ Cancel", callback_data=cd_back("CANCEL")),
        ]
    ]
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Flow UI Messages
# ---------------------------------------------------------------------------
def fmt_trade_summary(pair: str, direction: str, expiry: str, mode: str, size_val: float) -> str:
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    if mode == "pct":
        size_txt = f"{size_val:.0f}% of balance"
    else:
        size_txt = f"${size_val:.2f}"
    return (
        f"{arrow} *Trade Setup*\n"
        f"Pair: `{pair}`\n"
        f"Direction: *{direction}*\n"
        f"Expiry: `{expiry}`\n"
        f"Size: `{size_txt}`\n\n"
        f"Confirm?"
    )


# ---------------------------------------------------------------------------
# Handler Helpers: update FSM & persist last picks
# ---------------------------------------------------------------------------
def _fsm_set_pair(uid: int, raw_pair: str) -> None:
    f = _fsm_get(uid)
    f["flow"] = "trade"
    f["pair"] = raw_pair

def _fsm_set_dir(uid: int, direction: str) -> None:
    f = _fsm_get(uid)
    f["direction"] = direction

def _fsm_set_exp(uid: int, expiry: str) -> None:
    f = _fsm_get(uid)
    f["expiry"] = expiry

def _fsm_set_size_mode(uid: int, mode: str) -> None:
    f = _fsm_get(uid)
    f["size_mode"] = mode

def _fsm_set_size_value(uid: int, value: float) -> None:
    f = _fsm_get(uid)
    f["size_value"] = float(value)

def _fsm_complete(uid: int) -> Optional[Dict[str, Any]]:
    f = _fsm_get(uid)
    needed = ("pair","direction","expiry","size_mode","size_value")
    if all(k in f for k in needed):
        return f
    return None


# ---------------------------------------------------------------------------
# Broker / automation hook
# ---------------------------------------------------------------------------
def trigger_ui_vision_trade(
    pair: str,
    direction: str,
    expiry: str,
    size_mode: str,
    size_value: float,
) -> bool:
    """
    Trigger external UI.Vision / automation / broker integration.
    Replace with actual call (Pocket Option / Quotex / etc).
    Currently: GET UI_VISION_URL?pair=...&dir=...&expiry=...&mode=...&size=...
    """
    if not UI_VISION_URL:
        logger.info("UI_VISION_URL not configured; skipping external trade.")
        return False
    params = {
        "pair": pair,
        "direction": direction,
        "expiry": expiry,
        "mode": size_mode,
        "size": str(size_value),
    }
    try:
        resp = _req.get(UI_VISION_URL, params=params, timeout=10)
        logger.info("UI.Vision call -> %s %s", resp.status_code, resp.text[:200])
        return resp.ok
    except Exception as e:
        logger.error("UI.Vision trade error: %s", e)
        return False


def simulate_debit(size_mode: str, size_value: float) -> None:
    """Just log a pretend trade debit."""
    logger.info("[SIM_DEBIT] Debiting trade: mode=%s value=%s", size_mode, size_value)


# ---------------------------------------------------------------------------
# Execute Trade & Snapshot (final step)
# ---------------------------------------------------------------------------
async def execute_trade_and_snapshot(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    uid: int,
    pair: str,
    direction: str,
    expiry: str,
    size_mode: str,
    size_value: float,
) -> None:
    # stats record
    stats = _PERSIST.get_user_stats(uid)
    stats.trades_sent += 1
    stats.last_pair = pair
    stats.last_direction = direction
    stats.last_expiry = expiry
    stats.last_size_mode = size_mode
    stats.last_size_value = size_value
    _PERSIST.set_user_stats(uid, stats)
    save_state()

    # notification
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    size_txt = f"{size_value:.0f}%" if size_mode == "pct" else f"${size_value:.2f}"
    msg = (
        f"{arrow} *TRADE SENT*\n"
        f"Pair: `{pair}`\n"
        f"Direction: *{direction}*\n"
        f"Expiry: `{expiry}`\n"
        f"Size: `{size_txt}`"
    )
    await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN)

    # call broker / automation
    if SIM_DEBIT:
        simulate_debit(size_mode, size_value)
    else:
        trigger_ui_vision_trade(pair, direction, expiry, size_mode, size_value)

    # snapshot
    ex, tk, _is_otc, alt = resolve_symbol(pair)
    tf = norm_interval(DEFAULT_INTERVAL)
    th = norm_theme(DEFAULT_THEME)
    await send_snapshot_photo(chat_id, context, ex, tk, tf, th, prefix="[TRADE] ", alt_exchanges=alt)

    # reset FSM
    _fsm_reset(uid)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    nm = user.first_name if user else ""
    msg = (
        f"Hi {nm} 👋\n\n"
        "I'm your *TradingView Snapshot / Trade Bot*.\n\n"
        "Use the menu below to get started:\n"
        "- /pairs → pick asset, analyze & trade\n"
        "- /trade SYMBOL CALL|PUT 5m\n"
        "- /snap SYMBOL [tf] [theme]\n"
        "- /help for details\n"
    )
    await context.bot.send_message(
        update.effective_chat.id,
        msg,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_main_categories(),
    )


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📘 *Help*\n\n"
        "*/pairs* → inline asset picker & trade workflow.\n"
        "*/trade* SYMBOL CALL|PUT expiry [theme] → quick manual trade.\n"
        "*/snap* SYMBOL [interval] [theme] → chart screenshot.\n"
        "*/snapmulti* SYM1 SYM2 ... [interval] [theme] → album.\n"
        "*/snapall* → all assets (FX+OTC+Indices+Crypto).\n"
        "*/settings* → user defaults.\n\n"
        "*Intervals:* minutes (number), D, W, M.\n"
        "*Themes:* dark | light.\n"
        "*Expiries:* 1m, 3m, 5m, 15m, 30m, 1h.\n"
        "*Size modes:* $ fixed | % of balance.\n"
    )
    await context.bot.send_message(
        update.effective_chat.id,
        msg,
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
# /pairs
# ---------------------------------------------------------------------------
async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _fsm_reset(update.effective_user.id)
    await context.bot.send_message(
        update.effective_chat.id,
        "Select a market category:",
        reply_markup=kb_main_categories(),
    )


# ---------------------------------------------------------------------------
# /snap
# ---------------------------------------------------------------------------
async def cmd_snap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ex, tk, tf, th, alt = parse_snap_args(context.args)
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, th, alt_exchanges=alt)


# ---------------------------------------------------------------------------
# /snapmulti
# ---------------------------------------------------------------------------
async def cmd_snapmulti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs, tf, th = parse_multi_args(context.args)
    if not pairs:
        await context.bot.send_message(
            update.effective_chat.id,
            "Usage: /snapmulti SYM1 SYM2 ... [interval] [theme]"
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


# ---------------------------------------------------------------------------
# /snapall
# ---------------------------------------------------------------------------
async def cmd_snapall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, f"⚡ Capturing all {len(ALL_PAIRS)} assets… this may take a while.")

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
# /trade quick text command
# ---------------------------------------------------------------------------
async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol, direction, expiry, theme = parse_trade_args(context.args)
    ex, tk, _is_otc, alt = resolve_symbol(symbol)
    tf = norm_interval(DEFAULT_INTERVAL)
    th = norm_theme(theme)
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    msg = f"{arrow} *{symbol}* {direction}  Expiry: {expiry}"
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN)
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, th, prefix="[TRADE] ", alt_exchanges=alt)


# ---------------------------------------------------------------------------
# /settings
# ---------------------------------------------------------------------------
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = _PERSIST.get_user_settings(uid)
    msg = (
        "⚙ *Your Settings*\n"
        f"Default TF: `{s.default_interval}`\n"
        f"Theme: `{s.default_theme}`\n"
        f"Size Mode: `{s.default_size_mode}`\n"
        f"Size Value: `{s.default_size_value}`\n"
        f"Auto Confirm: `{s.auto_confirm_trade}`\n\n"
        "Settings editing via inline keyboard coming soon.\n"
    )
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# /next (placeholder)
# ---------------------------------------------------------------------------
async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        update.effective_chat.id,
        "👀 Watching for next signal (placeholder). Connect TradingView alerts to /tv.",
    )


# ---------------------------------------------------------------------------
# Echo text (quick NL trade parse)
# ---------------------------------------------------------------------------
_trade_re = re.compile(r"(?i)trade\s+([A-Z/\-]+)\s+(call|put|buy|sell|up|down)\s+([\d]+m?)")

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
        await send_snapshot_photo(
            update.effective_chat.id, context, ex, tk,
            DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[TRADE] ", alt_exchanges=alt
        )
        return
    await context.bot.send_message(update.effective_chat.id, f"You said: {txt}\nTry /trade EUR/USD CALL 5m")


# ---------------------------------------------------------------------------
# Unknown command fallback
# ---------------------------------------------------------------------------
async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, "❌ Unknown command. Try /help.")


# ---------------------------------------------------------------------------
# Callback Query Handler
# ---------------------------------------------------------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id
    chat_id = query.message.chat_id

    # Category
    if data.startswith("CAT:"):
        cat = data.split(":",1)[1]
        _fsm_reset(uid)
        _fsm_get(uid)["cat"] = cat
        await context.bot.edit_message_text(
            "Select a pair:",
            chat_id=chat_id,
            message_id=query.message.message_id,
            reply_markup=kb_pairs_for_category(cat),
        )
        return

    # Back from category to main
    if data == cd_back("CATMENU"):
        _fsm_reset(uid)
        await context.bot.edit_message_text(
            "Select a market category:",
            chat_id=chat_id,
            message_id=query.message.message_id,
            reply_markup=kb_main_categories(),
        )
        return

    # Pair
    if data.startswith("PR:"):
        key = data.split(":",1)[1]
        # find raw pair label by searching categories
        raw_pair = None
        for cat, arr in CATEGORY_MAP.items():
            for p in arr:
                if _canon_key(p) == key:
                    raw_pair = p
                    break
            if raw_pair:
                break
        if not raw_pair:
            await context.bot.send_message(chat_id, "Pair not found; try /pairs again.")
            return
        _fsm_set_pair(uid, raw_pair)
        await context.bot.edit_message_text(
            f"Pair: {raw_pair}\nSelect direction:",
            chat_id=chat_id,
            message_id=query.message.message_id,
            reply_markup=kb_direction(),
        )
        return

    # Back from direction to pair selection
    if data == cd_back("PAIRSEL"):
        f = _fsm_get(uid)
        cat = f.get("cat", "FX")
        await context.bot.edit_message_text(
            "Select a pair:",
            chat_id=chat_id,
            message_id=query.message.message_id,
            reply_markup=kb_pairs_for_category(cat),
        )
        return

    # Direction
    if data.startswith("DIR:"):
        direction = data.split(":",1)[1]
        _fsm_set_dir(uid, direction)
        f = _fsm_get(uid)
        pair = f.get("pair", "?")
        await context.bot.edit_message_text(
            f"Pair: {pair}\nDirection: {direction}\nSelect expiry:",
            chat_id=chat_id,
            message_id=query.message.message_id,
            reply_markup=kb_expiry(),
        )
        return

    # Back from expiry to direction
    if data == cd_back("DIRSEL"):
        f = _fsm_get(uid)
        pair = f.get("pair","?")
        await context.bot.edit_message_text(
            f"Pair: {pair}\nSelect direction:",
            chat_id=chat_id,
            message_id=query.message.message_id,
            reply_markup=kb_direction(),
        )
        return

    # Expiry
    if data.startswith("EXP:"):
        exp = norm_expiry(data.split(":",1)[1])
        _fsm_set_exp(uid, exp)
        f = _fsm_get(uid)
        pair = f.get("pair","?")
        direction = f.get("direction","?")
        await context.bot.edit_message_text(
            f"Pair: {pair}\nDirection: {direction}\nExpiry: {exp}\nSelect size mode:",
            chat_id=chat_id,
            message_id=query.message.message_id,
            reply_markup=kb_size_mode(),
        )
        return

    # Back from size mode to expiry
    if data == cd_back("EXPSEL"):
        f = _fsm_get(uid)
        pair = f.get("pair","?")
        direction = f.get("direction","?")
        await context.bot.edit_message_text(
            f"Pair: {pair}\nDirection: {direction}\nSelect expiry:",
            chat_id=chat_id,
            message_id=query.message.message_id,
            reply_markup=kb_expiry(),
        )
        return

    # Size mode
    if data.startswith("SZM:"):
        mode = data.split(":",1)[1]
        if mode not in ("usd","pct"):
            mode = "usd"
        _fsm_set_size_mode(uid, mode)
        f = _fsm_get(uid)
        pair = f.get("pair","?")
        direction = f.get("direction","?")
        exp = f.get("expiry","?")
        await context.bot.edit_message_text(
            f"Pair: {pair}\nDirection: {direction}\nExpiry: {exp}\nSelect trade size:",
            chat_id=chat_id,
            message_id=query.message.message_id,
            reply_markup=kb_size_values(mode),
        )
        return

    # Back from size value to size mode
    if data == cd_back("SIZMODE"):
        f = _fsm_get(uid)
        pair = f.get("pair","?")
        direction = f.get("direction","?")
        exp = f.get("expiry","?")
        await context.bot.edit_message_text(
            f"Pair: {pair}\nDirection: {direction}\nExpiry: {exp}\nSelect size mode:",
            chat_id=chat_id,
            message_id=query.message.message_id,
            reply_markup=kb_size_mode(),
        )
        return

    # Size value
    if data.startswith("SIZ:"):
        vraw = data.split(":",1)[1]
        try:
            val = float(vraw)
        except ValueError:
            val = 10.0
        _fsm_set_size_value(uid, val)
        f = _fsm_get(uid)
        pair = f.get("pair","?")
        direction = f.get("direction","?")
        exp = f.get("expiry","?")
        mode = f.get("size_mode","usd")
        txt = fmt_trade_summary(pair, direction, exp, mode, val)
        await context.bot.edit_message_text(
            txt,
            chat_id=chat_id,
            message_id=query.message.message_id,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_confirm(),
        )
        return

    # Cancel
    if data == cd_back("CANCEL"):
        _fsm_reset(uid)
        await context.bot.edit_message_text(
            "❌ Trade cancelled.",
            chat_id=chat_id,
            message_id=query.message.message_id,
        )
        return

    # Confirm
    if data == cd_conf():
        f = _fsm_complete(uid)
        if not f:
            await context.bot.send_message(chat_id, "Trade not complete; try /pairs again.")
            return
        pair = f["pair"]
        direction = f["direction"]
        expiry = f["expiry"]
        mode = f["size_mode"]
        size_val = f["size_value"]
        # handle final execute
        await execute_trade_and_snapshot(chat_id, context, uid, pair, direction, expiry, mode, size_val)
        try:
            await context.bot.delete_message(chat_id, query.message.message_id)
        except Exception:
            pass
        return

    # Unknown callback
    await context.bot.send_message(chat_id, f"Unknown selection: {data}")


# ---------------------------------------------------------------------------
# TradingView Webhook Handling
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
        _req.post(url, json=payload, timeout=30)
    except Exception as e:
        logger.error("tg_api_send_message: %s", e)


def tg_api_send_photo_bytes(chat_id: str, png: bytes, caption: str=""):
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", png, "image/png")}
    data = {"chat_id": chat_id, "caption": caption}
    try:
        _req.post(url, data=data, files=files, timeout=60)
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
    expiry    = norm_expiry(payload["expiry"])
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
        node_start_browser()
        png, ex_used = fetch_snapshot_png_any(ex, tk, tf, theme, "chart", alt)
        tg_api_send_photo_bytes(chat_id, png, caption=f"{ex_used}:{tk} • TF {tf} • {theme}")
    except Exception as e:
        logger.error("TV snapshot error for %s:%s -> %s", ex, tk, e)
        tg_api_send_message(chat_id, f"⚠ Chart snapshot failed for {raw_pair}: {e}")

    # Auto-trade?
    if AUTO_TRADE_FROM_TV and chat_id:
        logger.info("AUTO_TRADE_FROM_TV enabled; triggering UI.Vision trade.")
        if SIM_DEBIT:
            simulate_debit("pct", 1.0)  # arbitrary
        else:
            trigger_ui_vision_trade(raw_pair, direction, expiry, "pct", 1.0)

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


@flask_app.route("/webhook", methods=["POST"])
def tv_route_alias():
    return tv_route()


def start_flask_background() -> None:
    threading.Thread(
        target=lambda: flask_app.run(
            host="0.0.0.0", port=TV_WEBHOOK_PORT,
            debug=False, use_reloader=False, threaded=True
        ),
        daemon=True,
    ).start()
    logger.info("Flask TV webhook listening on port %s", TV_WEBHOOK_PORT)


# ---------------------------------------------------------------------------
# Build Application
# ---------------------------------------------------------------------------
def build_application():
    builder = ApplicationBuilder().token(TOKEN)

    # Rate limiter if installed
    if HAVE_AIO_RL:
        try:
            builder = builder.rate_limiter(AIORateLimiter())
        except Exception as e:
            logger.warning("AIORateLimiter setup failed: %s", e)
    else:
        logger.info("AIORateLimiter not installed; continuing without PTB throttling.")

    app = builder.build()

    # Commands
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("pairs",     cmd_pairs))
    app.add_handler(CommandHandler("snap",      cmd_snap))
    app.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    app.add_handler(CommandHandler("snapall",   cmd_snapall))
    app.add_handler(CommandHandler("trade",     cmd_trade))
    app.add_handler(CommandHandler("next",      cmd_next))
    app.add_handler(CommandHandler("settings",  cmd_settings))

    # Callback queries
    app.add_handler(CallbackQueryHandler(on_callback))

    # Fallbacks
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    return app


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    logger.info(
        "Bot starting… BASE_URL=%s | DefaultEX=%s | WebhookPort=%s | UI_VISION_URL=%s | AUTO_TRADE_FROM_TV=%s | SIM_DEBIT=%s",
        BASE_URL, DEFAULT_EXCHANGE, TV_WEBHOOK_PORT, UI_VISION_URL, AUTO_TRADE_FROM_TV, SIM_DEBIT
    )

    # load persisted state
    load_state()

    # start webhook server in background
    start_flask_background()

    # build & run PTB bot (blocking)
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
