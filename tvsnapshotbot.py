#!/usr/bin/env python
"""
TradingView → Telegram Snapshot + Trade Bot (Inline / Pocket Option / Persistent)
=================================================================================

Major capabilities
------------------
• Async python-telegram-bot v20+ with inline keyboards & per-user session state.
• Persistent per-user data (JSON): balance, trade mode, defaults, last trade size prefs.
• Multi-category instrument chooser: FX, OTC, Indices, Crypto.
• Symbol chooser (/pairs) → direction → expiry (1m/3m/5m/15m) → size ($ or %) → auto/manual trade.
• Trade size presets + custom keyed input; per-user balance & trade % sizing.
• Pocket Option automation hook via UI.Vision (HTTP POST) OR manual mode message.
• /snap, /snapmulti, /snapall, /pairs, /trade, /next, /balance, /setbal, /setmode, /setbalpct.
• TradingView webhook (/tv,/webhook) sends alerts to Telegram & can auto trade (env toggle).
• Robust logging: rotating file + unicode-safe console; truncated binary in logs.
• Rate limiting + global throttle to avoid hammering snapshot backend.
• PNG acceptance even when server mislabels as text; error fallback.
• Periodic autosave & debounced save-on-change; tolerant load on start.

Run
---
python tvsnapshotbot.py

"""

from __future__ import annotations

import os
import io
import re
import sys
import json
import time
import enum
import math
import asyncio
import logging
import threading
import atexit
from dataclasses import dataclass, asdict, field
from typing import (
    List, Tuple, Dict, Optional, Any, Callable, Iterable, TypedDict,
)

import requests
import httpx
from flask import Flask, jsonify, request

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
    Application,
    AIORateLimiter,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    JobQueue,
    Job,
    filters,
)

# =============================================================================
# Logging Setup (unicode-safe, rotating file)
# =============================================================================

os.makedirs("logs", exist_ok=True)
LOG_FILE = "logs/tvsnapshotbot.log"

# safe formatter that replaces undecodable characters
class SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        try:
            return super().format(record)
        except Exception:
            try:
                raw = record.getMessage()
            except Exception:
                raw = "<unformattable>"
            return f"{record.levelname} | {raw.encode('utf-8','replace').decode('utf-8','replace')}"

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(
    SafeFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(
    SafeFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_file_handler, _console_handler],
)

logger = logging.getLogger("TVSnapBot")


# =============================================================================
# Environment / Config
# =============================================================================

env = os.environ

TOKEN: str = env.get("TELEGRAM_BOT_TOKEN", "").strip()
DEFAULT_CHAT_ID: str = env.get("TELEGRAM_CHAT_ID", "").strip()
BASE_URL: str = env.get("SNAPSHOT_BASE_URL", "http://localhost:10000").rstrip("/")
DEFAULT_EXCHANGE: str = env.get("DEFAULT_EXCHANGE", "FX").strip().upper() or "FX"
DEFAULT_INTERVAL: str = env.get("DEFAULT_INTERVAL", "1").strip() or "1"
DEFAULT_THEME: str = env.get("DEFAULT_THEME", "dark").strip().lower() or "dark"
TV_WEBHOOK_PORT: int = int(env.get("TV_WEBHOOK_PORT", "8081"))
WEBHOOK_SECRET: Optional[str] = env.get("WEBHOOK_SECRET") or None
UI_VISION_URL: Optional[str] = env.get("UI_VISION_URL") or None
AUTO_TRADE_FROM_TV: bool = env.get("AUTO_TRADE_FROM_TV", "0").strip() == "1"
DEFAULT_BALANCE: float = float(env.get("DEFAULT_BALANCE", "1000") or 1000)
SIM_DEBIT: bool = env.get("SIM_DEBIT", "0").strip() == "1"  # subtract trade amount from balance?

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment.")

# shared session objects
_http = requests.Session()
_httpx_client = httpx.Client(timeout=30)

# Snapshot service endpoint names (server.js)
SNAP_ENDPOINT = "/run"          # expects query: exchange=...&ticker=...&interval=...&theme=...
START_BROWSER_ENDPOINT = "/start-browser"
HEALTH_ENDPOINT = "/healthz"

# =============================================================================
# Rate Limiting
# =============================================================================

LAST_SNAPSHOT_PER_CHAT: Dict[int, float] = {}
RATE_LIMIT_SECONDS = 3
GLOBAL_LAST_SNAPSHOT = 0.0
GLOBAL_MIN_GAP = 0.75  # seconds between ANY snapshot call

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


# =============================================================================
# Instrument Universe
# =============================================================================

# --- FX ---
FX_LIST: List[str] = [
    "EUR/USD","GBP/USD","USD/JPY","USD/CHF","AUD/USD",
    "NZD/USD","USD/CAD","EUR/GBP","EUR/JPY","GBP/JPY",
    "AUD/JPY","NZD/JPY","EUR/AUD","GBP/AUD","EUR/CAD",
    "USD/MXN","USD/TRY","USD/ZAR","AUD/CHF","EUR/CHF",
]

# --- OTC (Pocket Option style overlays) ---
OTC_LIST: List[str] = [
    "EUR/USD-OTC","GBP/USD-OTC","USD/JPY-OTC","USD/CHF-OTC","AUD/USD-OTC",
    "NZD/USD-OTC","USD/CAD-OTC","EUR/GBP-OTC","EUR/JPY-OTC","GBP/JPY-OTC",
    "AUD/CHF-OTC","EUR/CHF-OTC","KES/USD-OTC","MAD/USD-OTC",
    "USD/BDT-OTC","USD/MXN-OTC","USD/MYR-OTC","USD/PKR-OTC",
]

# --- Indices (TradingView TVC feed primary) ---
# Display name : (primary_ex, ticker)
_INDICES_BASE: Dict[str, Tuple[str,str]] = {
    "US30 (Dow)":        ("TVC","US30"),
    "SPX500 (S&P)":      ("TVC","SPX"),
    "NAS100 (Nasdaq)":   ("TVC","NDX"),     # alt: NAS100, NDX, NASDAQ100
    "GER40 (DAX)":       ("TVC","GER40"),
    "UK100 (FTSE)":      ("TVC","UKX"),
    "JP225 (Nikkei)":    ("TVC","NI225"),
    "FRA40 (CAC)":       ("TVC","CAC40"),
    "HK50 (HangSeng)":   ("TVC","HSI"),
    "ES1! (Mini)":       ("CME_MINI","ES1!"),
    "NQ1! (Mini)":       ("CME_MINI","NQ1!"),
}

INDICES_LIST = list(_INDICES_BASE.keys())

# --- Crypto (Binance primaries) ---
# Use USDT majors to maximize chart availability
_CRYPTO_BASE: Dict[str, Tuple[str,str]] = {
    "BTC/USDT":  ("BINANCE","BTCUSDT"),
    "ETH/USDT":  ("BINANCE","ETHUSDT"),
    "SOL/USDT":  ("BINANCE","SOLUSDT"),
    "XRP/USDT":  ("BINANCE","XRPUSDT"),
    "DOGE/USDT": ("BINANCE","DOGEUSDT"),
    "ADA/USDT":  ("BINANCE","ADAUSDT"),
    "BNB/USDT":  ("BINANCE","BNBUSDT"),
    "LTC/USDT":  ("BINANCE","LTCUSDT"),
    "TRX/USDT":  ("BINANCE","TRXUSDT"),
    "LINK/USDT": ("BINANCE","LINKUSDT"),
    "MATIC/USDT":("BINANCE","MATICUSDT"),
    "DOT/USDT":  ("BINANCE","DOTUSDT"),
}

CRYPTO_LIST = list(_CRYPTO_BASE.keys())


# --- Canonicalization / Fallbacks ---
def _canon_key(pair: str) -> str:
    return (
        pair.strip()
        .upper()
        .replace(" ", "")
        .replace("/", "")
        .replace("-", "")
        .replace("(", "")
        .replace(")", "")
        .replace("!", "")
    )

# Standard fallback sequence for FX-style quotes
KNOWN_FX_EXCHANGES: List[str] = ["FX", "FX_IDC", "OANDA", "FOREXCOM", "IDC"]
EXCHANGE_FALLBACKS: List[str] = [DEFAULT_EXCHANGE] + KNOWN_FX_EXCHANGES

# Indices fallback (broad TV sources)
INDICES_FALLBACKS: List[str] = ["TVC","CURRENCYCOM","OANDA","FOREXCOM","IDC"]

# Crypto fallback
CRYPTO_FALLBACKS: List[str] = ["BINANCE","BYBIT","COINBASE","KRAKEN","BITSTAMP","BITFINEX","OKX"]

# Registry dataclass
@dataclass
class PairMeta:
    slug: str
    display: str
    primary_ex: str
    ticker: str
    fallbacks: List[str]
    category: str  # FX, OTC, IND, CRYPTO

PAIR_REGISTRY: Dict[str, PairMeta] = {}

def _slugify(disp: str) -> str:
    return re.sub(r"[^a-z0-9]+","-",disp.lower()).strip("-")

def _register_pair(display: str, primary_ex: str, ticker: str, fallbacks: List[str], category: str):
    slug = _slugify(display)
    # dedup fallback
    dedup: List[str] = []
    seen = set()
    for e in [primary_ex] + fallbacks:
        e = e.upper()
        if e not in seen:
            dedup.append(e)
            seen.add(e)
    pm = PairMeta(slug=slug, display=display, primary_ex=primary_ex.upper(), ticker=ticker.upper(), fallbacks=dedup[1:], category=category)
    PAIR_REGISTRY[slug] = pm

# Build registry
for disp in FX_LIST:
    tk = disp.replace("/","").upper()
    _register_pair(disp, DEFAULT_EXCHANGE, tk, KNOWN_FX_EXCHANGES, "FX")

_otc_underlying = {
    "EUR/USD-OTC":"EURUSD","GBP/USD-OTC":"GBPUSD","USD/JPY-OTC":"USDJPY",
    "USD/CHF-OTC":"USDCHF","AUD/USD-OTC":"AUDUSD","NZD/USD-OTC":"NZDUSD",
    "USD/CAD-OTC":"USDCAD","EUR/GBP-OTC":"EURGBP","EUR/JPY-OTC":"EURJPY",
    "GBP/JPY-OTC":"GBPJPY","AUD/CHF-OTC":"AUDCHF","EUR/CHF-OTC":"EURCHF",
    "KES/USD-OTC":"USDKES","MAD/USD-OTC":"USDMAD","USD/BDT-OTC":"USDBDT",
    "USD/MXN-OTC":"USDMXN","USD/MYR-OTC":"USDMYR","USD/PKR-OTC":"USDPKR",
}
for disp, tk in _otc_underlying.items():
    _register_pair(disp, "QUOTEX", tk, EXCHANGE_FALLBACKS, "OTC")

for disp, (ex, tk) in _INDICES_BASE.items():
    _register_pair(disp, ex, tk, INDICES_FALLBACKS, "IND")

for disp, (ex, tk) in _CRYPTO_BASE.items():
    _register_pair(disp, ex, tk, CRYPTO_FALLBACKS, "CRYPTO")

# Reverse index: category → slug list (ordered original)
CATEGORY_TO_SLUGS: Dict[str,List[str]] = {
    "FX":     [_slugify(d) for d in FX_LIST],
    "OTC":    [_slugify(d) for d in OTC_LIST],
    "IND":    [_slugify(d) for d in INDICES_LIST],
    "CRYPTO": [_slugify(d) for d in CRYPTO_LIST],
}

# =============================================================================
# Interval & Theme Normalization
# =============================================================================

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
    if t in ("mo","mth","1mo","1mth","month"):
        return "M"
    if t.isdigit():
        return t
    return DEFAULT_INTERVAL

def norm_theme(val: str) -> str:
    if not val:
        return DEFAULT_THEME
    return "light" if val.lower().startswith("l") else "dark"


# =============================================================================
# Direction Parsing / Normalization
# =============================================================================

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


# =============================================================================
# Expiry Normalization
# =============================================================================

VALID_EXPIRIES = ["1m","3m","5m","15m"]

def norm_expiry(exp: str) -> str:
    if not exp:
        return "5m"
    e = exp.strip().lower()
    if e in VALID_EXPIRIES:
        return e
    if e.isdigit():
        return f"{e}m"
    m = re.match(r"(\d+)", e)
    if m:
        return f"{m.group(1)}m"
    return "5m"


# =============================================================================
# Resolve user-entered symbol (search registry first, else parse fallback)
# =============================================================================

def resolve_symbol(raw: str) -> Tuple[str, str, bool, List[str]]:
    """
    Returns (primary_exchange, ticker, is_otc, alt_exchanges)
    """
    if not raw:
        pm = PAIR_REGISTRY[next(iter(CATEGORY_TO_SLUGS["FX"]))]
        return pm.primary_ex, pm.ticker, False, pm.fallbacks

    s = raw.strip()
    up = s.upper()
    is_otc = "-OTC" in up

    # slug direct?
    slug = _slugify(s)
    pm = PAIR_REGISTRY.get(slug)
    if pm:
        return pm.primary_ex, pm.ticker, (pm.category == "OTC"), pm.fallbacks

    # explicit EX:TK
    if ":" in up:
        ex, tk = up.split(":", 1)
        # guess fallbacks by type
        if ex in CRYPTO_FALLBACKS:
            return ex, tk, False, CRYPTO_FALLBACKS
        if ex in INDICES_FALLBACKS:
            return ex, tk, False, INDICES_FALLBACKS
        return ex, tk, is_otc, EXCHANGE_FALLBACKS

    # attempted match ignoring slash/hyphen/space
    ck = _canon_key(up)
    for meta in PAIR_REGISTRY.values():
        if _canon_key(meta.display) == ck or meta.ticker == ck:
            return meta.primary_ex, meta.ticker, (meta.category == "OTC"), meta.fallbacks

    # fallback numeric strip
    tk = re.sub(r"[^A-Z0-9]","",up)
    return DEFAULT_EXCHANGE, tk, is_otc, EXCHANGE_FALLBACKS


# =============================================================================
# Snapshot Backend Helpers
# =============================================================================

def node_start_browser() -> None:
    """Ping Node service to ensure Chromium warm; ignore errors."""
    try:
        url = f"{BASE_URL}{START_BROWSER_ENDPOINT}"
        _http.get(url, timeout=10)
    except Exception as e:
        logger.debug("start-browser ping failed: %s", e)

def _safe_trunc(s: str, limit: int = 200) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + "...(trunc)"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

def _attempt_snapshot_url(ex: str, tk: str, interval: str, theme: str) -> tuple[bool, Optional[bytes], str]:
    """Attempt snapshot fetch once."""
    try:
        global_throttle_wait()
        url = (
            f"{BASE_URL}{SNAP_ENDPOINT}"
            f"?exchange={ex}&ticker={tk}&interval={interval}&theme={theme}"
        )
        r = _http.get(url, timeout=75)
        ct = r.headers.get("Content-Type", "")
        if r.status_code == 200:
            content = r.content
            if ct.startswith("image") or content.startswith(PNG_MAGIC):
                return True, content, ""
            return False, None, f"200 but not image: ct={ct}"
        return False, None, f"HTTP {r.status_code}: {_safe_trunc(r.text)}"
    except Exception as e:
        return False, None, str(e)

def fetch_snapshot_png_any(
    primary_ex: str,
    tk: str,
    interval: str,
    theme: str,
    extra_exchanges: Optional[List[str]] = None,
) -> tuple[bytes, str]:
    """
    Multi-exchange fallback. Try primary, then extra_exchanges, then EXCHANGE_FALLBACKS,
    then category fallbacks deduced from known sets. Returns (png_bytes, exchange_used) or raises.
    """
    tried: List[str] = []
    last_err = "unknown"

    # unify search order
    order: List[str] = []
    if primary_ex:
        order.append(primary_ex.upper())
    if extra_exchanges:
        order.extend([e.upper() for e in extra_exchanges])

    # guess category from tk or fallback sets
    if tk.endswith("USDT"):
        order.extend(CRYPTO_FALLBACKS)
    elif re.match(r"^(US30|SPX|NDX|GER40|UKX|NI225|CAC40|HSI|ES1|NQ1)", tk):
        order.extend(INDICES_FALLBACKS)
    else:
        order.extend(EXCHANGE_FALLBACKS)

    # dedup
    dedup: List[str] = []
    seen = set()
    for e in order:
        if e not in seen:
            dedup.append(e)
            seen.add(e)

    for ex in dedup:
        tried.append(ex)
        ok, png, err = _attempt_snapshot_url(ex, tk, interval, theme)
        if ok and png:
            logger.info("Snapshot success: %s:%s via %s (%d bytes)", ex, tk, ex, len(png))
            return png, ex
        last_err = err
        logger.warning("Snapshot failed %s:%s via %s -> %s", ex, tk, ex, err)
        time.sleep(0.5)

    raise RuntimeError(f"All exchanges failed for {tk}. Last error: {last_err}. Tried: {tried}")


# =============================================================================
# User Session State + Persistence
# =============================================================================

class TradeMode(enum.Enum):
    MANUAL = "manual"
    AUTO   = "auto"

class SizeMode(enum.Enum):
    DOLLAR = "dollar"
    PERCENT = "percent"

@dataclass
class UserState:
    balance: float = field(default=DEFAULT_BALANCE)
    default_interval: str = field(default=DEFAULT_INTERVAL)
    default_theme: str = field(default=DEFAULT_THEME)
    trade_mode: TradeMode = field(default=TradeMode.MANUAL)
    size_mode: SizeMode = field(default=SizeMode.DOLLAR)
    size_value: float = field(default=10)  # $ or % depending on mode

    sel_pair_slug: Optional[str] = None  # key into registry
    sel_direction: Optional[str] = None # CALL/PUT
    sel_expiry: Optional[str] = None    # "1m" etc
    sel_size_mode: Optional[SizeMode] = None
    sel_size_value: Optional[float] = None

    waiting_custom_size: bool = False   # expecting typed value after "Custom…"

    def clear_selection(self) -> None:
        self.sel_pair_slug = None
        self.sel_direction = None
        self.sel_expiry = None
        self.sel_size_mode = None
        self.sel_size_value = None
        self.waiting_custom_size = False

# ------------- Persistence -------------
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "user_state.json")

# global registry of user states keyed by Telegram user id
STATE_REGISTRY: Dict[int, UserState] = {}
STATE_LOCK = threading.Lock()
_save_timer: Optional[threading.Timer] = None

def serialize_user_state(st: UserState) -> Dict[str, Any]:
    d = asdict(st)
    d["trade_mode"] = st.trade_mode.value
    d["size_mode"] = st.size_mode.value
    if st.sel_size_mode is not None:
        d["sel_size_mode"] = st.sel_size_mode.value
    return d

def deserialize_user_state(d: Dict[str, Any]) -> UserState:
    try:
        st = UserState()
        st.balance = float(d.get("balance", DEFAULT_BALANCE))
        st.default_interval = str(d.get("default_interval", DEFAULT_INTERVAL))
        st.default_theme = str(d.get("default_theme", DEFAULT_THEME))
        st.trade_mode = TradeMode(d.get("trade_mode","manual"))
        st.size_mode = SizeMode(d.get("size_mode","dollar"))
        st.size_value = float(d.get("size_value",10))
        st.sel_pair_slug = d.get("sel_pair_slug")
        st.sel_direction = d.get("sel_direction")
        st.sel_expiry = d.get("sel_expiry")
        ssm = d.get("sel_size_mode")
        if ssm:
            st.sel_size_mode = SizeMode(ssm)
        st.sel_size_value = d.get("sel_size_value")
        st.waiting_custom_size = bool(d.get("waiting_custom_size", False))
        return st
    except Exception as e:
        logger.error("deserialize_user_state error: %s", e)
        return UserState()

def load_state_registry() -> None:
    if not os.path.exists(STATE_FILE):
        logger.info("No state file found; starting fresh.")
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("invalid root")
        with STATE_LOCK:
            STATE_REGISTRY.clear()
            for k, v in raw.items():
                try:
                    uid = int(k)
                except Exception:
                    continue
                if isinstance(v, dict):
                    STATE_REGISTRY[uid] = deserialize_user_state(v)
        logger.info("Loaded %d user states from %s", len(STATE_REGISTRY), STATE_FILE)
    except Exception as e:
        logger.error("Failed loading state registry: %s", e)

def save_state_registry() -> None:
    with STATE_LOCK:
        payload = {str(uid): serialize_user_state(st) for uid, st in STATE_REGISTRY.items()}
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp, STATE_FILE)
        logger.debug("State saved (%d users).", len(payload))
    except Exception as e:
        logger.error("save_state_registry error: %s", e)
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass

def _debounced_save():
    global _save_timer
    _save_timer = None
    save_state_registry()

def schedule_state_save(delay: float = 1.5) -> None:
    """Debounced write: collapse bursts of updates."""
    global _save_timer
    with STATE_LOCK:
        if _save_timer is not None:
            _save_timer.cancel()
        _save_timer = threading.Timer(delay, _debounced_save)
        _save_timer.daemon = True
        _save_timer.start()

def register_state(uid: int, st: UserState) -> None:
    with STATE_LOCK:
        STATE_REGISTRY[uid] = st
    schedule_state_save()

def get_user_state(context: ContextTypes.DEFAULT_TYPE, user_id: Optional[int] = None) -> UserState:
    """
    Retrieve per-user state from context.user_data; create if needed; merge persistent if exists.
    """
    if user_id is None:
        user_id = context._user_id  # type: ignore[attr-defined]
    ud = context.user_data
    st = ud.get("_state")
    if isinstance(st, UserState):
        return st
    # load from registry if available
    with STATE_LOCK:
        reg_st = STATE_REGISTRY.get(user_id)
    if reg_st:
        ud["_state"] = reg_st
        return reg_st
    # new
    st = UserState()
    ud["_state"] = st
    register_state(user_id, st)
    return st

def persist_all_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    save_state_registry()

atexit.register(save_state_registry)


# =============================================================================
# Trade Sizing Helpers
# =============================================================================

DOLLAR_PRESETS = [1, 5, 10, 25, 50, 100]
PERCENT_PRESETS = [1, 2, 5, 10, 20, 50, 100]

def resolve_trade_amount(st: UserState, size_mode: SizeMode, value: float) -> float:
    if size_mode == SizeMode.DOLLAR:
        return max(0.01, float(value))
    amt = st.balance * (value / 100.0)
    return max(0.01, round(amt, 2))

def pretty_size_label(size_mode: SizeMode, value: float, st: Optional[UserState] = None) -> str:
    if size_mode == SizeMode.DOLLAR:
        return f"${value:.2f}".rstrip('0').rstrip('.') if value < 100 else f"${int(value)}"
    if st:
        calc = resolve_trade_amount(st, size_mode, value)
        return f"{value:.0f}% (${calc:.2f})"
    return f"{value:.0f}%"


# =============================================================================
# Pocket Option / Broker Hook (UI.Vision)
# =============================================================================

def broker_auto_trade(
    pair_display: str,
    direction: str,
    expiry: str,
    amount: float,
    size_mode: SizeMode,
    user_id: int,
) -> bool:
    """
    Send a JSON POST to UI_VISION_URL (if configured).
    Return True if accepted, False otherwise.
    """
    if not UI_VISION_URL:
        return False
    payload = {
        "user_id": user_id,
        "pair": pair_display,
        "direction": direction,
        "expiry": expiry,
        "amount": amount,
        "sizemode": size_mode.value,
        "broker": "PocketOption",
        "source": "TVSnapBot",
        "ts": int(time.time()),
    }
    try:
        r = _httpx_client.post(UI_VISION_URL, json=payload, timeout=30)
        if r.status_code == 200:
            logger.info("broker_auto_trade accepted: %s", r.text)
            return True
        logger.warning("broker_auto_trade non-200 %s %s", r.status_code, r.text)
    except Exception as e:
        logger.error("broker_auto_trade error: %s", e)
    return False


# =============================================================================
# Telegram Snapshot Send (async)
# =============================================================================

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
            fetch_snapshot_png_any, exchange, ticker, interval, theme, alt_exchanges
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
            png, ex_used = fetch_snapshot_png_any(ex, tk, interval, theme, alt_list)
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
        if len(chunk) > 1:
            for m in chunk[1:]:
                m.caption = None
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(0.75)


# =============================================================================
# Command Parsing Helpers
# =============================================================================

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
    """
    if not args:
        return "EUR/USD","CALL","5m",DEFAULT_THEME
    symbol = args[0]
    direction = parse_direction(args[1] if len(args)>=2 else None) or "CALL"
    expiry = args[2] if len(args)>=3 else "5m"
    theme = args[3] if len(args)>=4 else DEFAULT_THEME
    return symbol, direction, expiry, theme


# =============================================================================
# Inline Keyboard Builders
# =============================================================================

INLINE_PAGE_SIZE = 8  # pairs per page set

def _chunk(lst: List[Any], n: int) -> List[List[Any]]:
    return [lst[i:i+n] for i in range(0, len(lst), n)]

def kb_pairs_category() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🌐 FX",      callback_data="CAT:FX:0"),
            InlineKeyboardButton("🕒 OTC",     callback_data="CAT:OTC:0"),
        ],
        [
            InlineKeyboardButton("📈 Indices", callback_data="CAT:IND:0"),
            InlineKeyboardButton("🪙 Crypto",  callback_data="CAT:CRYPTO:0"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="ACT:CANCEL")],
    ]
    return InlineKeyboardMarkup(keyboard)

def kb_pairs_list(cat: str, page: int = 0, per_page: int = INLINE_PAGE_SIZE) -> InlineKeyboardMarkup:
    slugs = CATEGORY_TO_SLUGS.get(cat, [])
    total = len(slugs)
    pages = max(1, math.ceil(total / per_page))
    page = max(0, min(page, pages - 1))
    start = page * per_page
    end = start + per_page
    subset = slugs[start:end]

    rows: List[List[InlineKeyboardButton]] = []
    for slug in subset:
        pm = PAIR_REGISTRY[slug]
        rows.append([InlineKeyboardButton(pm.display, callback_data=f"PAIR:{slug}:{cat}:{page}")])

    nav_row: List[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅", callback_data=f"CAT:{cat}:{page-1}"))
    nav_row.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="NOP"))
    if page < pages - 1:
        nav_row.append(InlineKeyboardButton("➡", callback_data=f"CAT:{cat}:{page+1}"))

    rows.append(nav_row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="ACT:CANCEL")])
    return InlineKeyboardMarkup(rows)

def kb_direction() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 CALL", callback_data="DIR:CALL"),
            InlineKeyboardButton("🔴 PUT",  callback_data="DIR:PUT"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data="ACT:BACK:PAIR")],
    ])

def kb_expiry() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1m",  callback_data="EXP:1m"),
            InlineKeyboardButton("3m",  callback_data="EXP:3m"),
            InlineKeyboardButton("5m",  callback_data="EXP:5m"),
            InlineKeyboardButton("15m", callback_data="EXP:15m"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data="ACT:BACK:DIR")],
    ])

def kb_size_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("$ Amount", callback_data="SZMODE:DOLLAR"),
            InlineKeyboardButton("% Balance", callback_data="SZMODE:PERCENT"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data="ACT:BACK:EXP")],
    ])

def kb_size_dollar() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(f"${v}", callback_data=f"SIZE:D:{v}") for v in DOLLAR_PRESETS]
    return InlineKeyboardMarkup(_chunk(row, 3) + [[InlineKeyboardButton("Custom…", callback_data="SIZE:D:CUSTOM")],
                                                  [InlineKeyboardButton("⬅ Back", callback_data="ACT:BACK:SZMODE")]])

def kb_size_percent() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(f"{v}%", callback_data=f"SIZE:P:{v}") for v in PERCENT_PRESETS]
    return InlineKeyboardMarkup(_chunk(row, 3) + [[InlineKeyboardButton("Custom…", callback_data="SIZE:P:CUSTOM")],
                                                  [InlineKeyboardButton("⬅ Back", callback_data="ACT:BACK:SZMODE")]])

def kb_trade_confirm(auto_enabled: bool) -> InlineKeyboardMarkup:
    kn = []
    if auto_enabled:
        kn.append([
            InlineKeyboardButton("✅ Auto Trade", callback_data="CONF:AUTO"),
            InlineKeyboardButton("📲 Manual",     callback_data="CONF:MAN"),
        ])
    else:
        kn.append([
            InlineKeyboardButton("📲 Send Manual", callback_data="CONF:MAN"),
        ])
    kn.append([InlineKeyboardButton("❌ Cancel", callback_data="ACT:CANCEL")])
    return InlineKeyboardMarkup(kn)


# =============================================================================
# Text Format Helpers
# =============================================================================

def format_selection_summary(st: UserState) -> str:
    parts = []
    if st.sel_pair_slug:
        pm = PAIR_REGISTRY.get(st.sel_pair_slug)
        if pm:
            parts.append(f"*Pair:* {pm.display}")
    if st.sel_direction:
        arrow = "🟢" if st.sel_direction == "CALL" else "🔴"
        parts.append(f"*Direction:* {arrow} {st.sel_direction}")
    if st.sel_expiry:
        parts.append(f"*Expiry:* {st.sel_expiry}")
    if st.sel_size_mode and st.sel_size_value is not None:
        parts.append(f"*Size:* {pretty_size_label(st.sel_size_mode, st.sel_size_value, st)}")
    return "\n".join(parts) if parts else "_No selection yet._"

def format_balance_summary(st: UserState) -> str:
    return f"💰 *Balance:* ${st.balance:,.2f}\nMode: {st.trade_mode.value} | Default TF: {st.default_interval} | Theme: {st.default_theme}"


# =============================================================================
# Bot Command Handlers
# =============================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_user_state(context, update.effective_user.id)
    nm = update.effective_user.first_name if update.effective_user else ""
    msg = (
        f"Hi {nm} 👋\n\n"
        "I'm your TradingView Snapshot + Pocket Option helper bot.\n\n"
        "Quick actions:\n"
        "• /pairs → pick a market from list (inline)\n"
        "• /trade EUR/USD CALL 5m → quick trade\n"
        "• /snap EUR/USD → chart\n"
        "• /balance → show balance\n"
        "• /setbal 1000 → set balance\n\n"
        f"{format_balance_summary(st)}"
    )
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📘 *Help*\n\n"
        "`/snap SYMBOL interval theme`\n"
        "`/trade SYMBOL CALL|PUT expiry theme`\n"
        "`/snapmulti S1 S2 ... interval theme`\n"
        "`/snapall` (all FX+OTC)\n"
        "`/pairs` browse FX • OTC • Indices • Crypto\n"
        "`/balance` show / set account balance\n"
        "`/setbal <amt>` set new $ balance\n"
        "`/setbalpct <num>` set next trade size to <num>% of bal\n"
        "`/setmode auto|manual` toggle trade mode\n"
        "`/next` watch for next TV signal\n\n"
        "*Intervals:* number=minutes, D/W/M.\n"
        "*Expiries:* 1m,3m,5m,15m.\n"
        "*Themes:* dark|light."
    )
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_user_state(context, update.effective_user.id)
    st.clear_selection()
    register_state(update.effective_user.id, st)  # persist
    await context.bot.send_message(
        update.effective_chat.id,
        "Select a category:",
        reply_markup=kb_pairs_category(),
    )

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
    all_disp = FX_LIST + OTC_LIST
    await context.bot.send_message(chat_id, f"⚡ Capturing all {len(all_disp)} FX+OTC pairs…")
    p_trip: List[Tuple[str, str, str, List[str]]] = []
    for p in all_disp:
        ex, tk, _is_otc, alt = resolve_symbol(p)
        p_trip.append((ex, tk, p, alt))
    media_items = await asyncio.to_thread(build_media_items_sync, p_trip, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[ALL] ")
    if not media_items:
        await context.bot.send_message(chat_id, "❌ No charts captured.")
        return
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)

async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_user_state(context, update.effective_user.id)
    symbol, direction, expiry, theme = parse_trade_args(context.args)
    ex, tk, _is_otc, alt = resolve_symbol(symbol)
    tf = norm_interval(st.default_interval)
    th = norm_theme(theme)
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    await context.bot.send_message(
        update.effective_chat.id,
        f"{arrow} *{symbol}* {direction}  Expiry: {expiry}",
        parse_mode=ParseMode.MARKDOWN,
    )
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, th, prefix="[TRADE] ", alt_exchanges=alt)

async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        update.effective_chat.id,
        "👀 Watching for next signal (placeholder). Connect TradingView alerts to /tv.",
    )

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_user_state(context, update.effective_user.id)
    await context.bot.send_message(
        update.effective_chat.id,
        format_balance_summary(st),
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_setbal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_user_state(context, update.effective_user.id)
    if not context.args:
        await context.bot.send_message(update.effective_chat.id, "Usage: /setbal <amount>")
        return
    try:
        amt = float(context.args[0])
        if amt <= 0:
            raise ValueError
        st.balance = amt
        register_state(update.effective_user.id, st)
        await context.bot.send_message(
            update.effective_chat.id,
            f"✅ Balance set to ${st.balance:,.2f}",
        )
    except Exception:
        await context.bot.send_message(update.effective_chat.id, "❌ Invalid amount.")

async def cmd_setbalpct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_user_state(context, update.effective_user.id)
    if not context.args:
        await context.bot.send_message(update.effective_chat.id, "Usage: /setbalpct <percent>")
        return
    try:
        pct = float(context.args[0])
        if pct <= 0:
            raise ValueError
        st.size_mode = SizeMode.PERCENT
        st.size_value = pct
        register_state(update.effective_user.id, st)
        await context.bot.send_message(update.effective_chat.id, f"✅ Next trades default to {pct}% of balance.")
    except Exception:
        await context.bot.send_message(update.effective_chat.id, "❌ Invalid percent.")

async def cmd_setmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_user_state(context, update.effective_user.id)
    if not context.args:
        await context.bot.send_message(update.effective_chat.id, f"Current mode: {st.trade_mode.value}\nUsage: /setmode auto|manual")
        return
    arg = context.args[0].lower()
    if arg.startswith("a"):
        if UI_VISION_URL:
            st.trade_mode = TradeMode.AUTO
            await context.bot.send_message(update.effective_chat.id, "✅ Trade mode: AUTO.")
        else:
            st.trade_mode = TradeMode.MANUAL
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠ Auto mode requested but UI_VISION_URL not configured; staying in MANUAL.",
            )
    elif arg.startswith("m"):
        st.trade_mode = TradeMode.MANUAL
        await context.bot.send_message(update.effective_chat.id, "✅ Trade mode: MANUAL.")
    else:
        await context.bot.send_message(update.effective_chat.id, "Usage: /setmode auto|manual")
        return
    register_state(update.effective_user.id, st)


# =============================================================================
# Text / Trade Parsing Fallback (user typed message)
# =============================================================================
_trade_re = re.compile(r"(?i)\btrade\s+([A-Z/\-]+)\s+(call|put|buy|sell|up|down)\s+(\d+m?)")

async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    m = _trade_re.search(txt)
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
    await context.bot.send_message(
        update.effective_chat.id,
        f"You said: {txt}\nTry `/trade EUR/USD CALL 5m` or `/pairs`.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, "❌ Unknown command. Try /help.")


# =============================================================================
# Inline Callback Handler
# =============================================================================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query: CallbackQuery = update.callback_query
    await query.answer()  # ack
    user_id = query.from_user.id
    st = get_user_state(context, user_id)
    data = query.data or ""

    # CANCEL
    if data.startswith("ACT:CANCEL"):
        st.clear_selection()
        register_state(user_id, st)
        await query.edit_message_text("❌ Cancelled.")
        return

    # BACK
    if data.startswith("ACT:BACK:"):
        stage = data.split(":",2)[2] if ":" in data else ""
        if stage == "PAIR":
            st.sel_pair_slug = None
            register_state(user_id, st)
            await query.edit_message_text("Select a category:", reply_markup=kb_pairs_category())
        elif stage == "DIR":
            st.sel_direction = None
            register_state(user_id, st)
            await query.edit_message_text(format_selection_summary(st), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_direction())
        elif stage == "EXP":
            st.sel_expiry = None
            register_state(user_id, st)
            await query.edit_message_text(format_selection_summary(st), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_expiry())
        elif stage == "SZMODE":
            st.sel_size_mode = None
            st.sel_size_value = None
            st.waiting_custom_size = False
            register_state(user_id, st)
            await query.edit_message_text(format_selection_summary(st), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_size_mode())
        else:
            await query.edit_message_text("Back unavailable.")
        return

    # CATEGORY page nav & show
    if data.startswith("CAT:"):
        _, cat, page_s = data.split(":",2)
        page = int(page_s) if page_s.isdigit() else 0
        await query.edit_message_text(
            f"Select a {cat} pair:",
            reply_markup=kb_pairs_list(cat, page=page),
        )
        return

    # PAIR selection
    if data.startswith("PAIR:"):
        # data format: PAIR:<slug>:<cat>:<page>
        parts = data.split(":")
        if len(parts) >= 2:
            slug = parts[1]
        else:
            slug = CATEGORY_TO_SLUGS["FX"][0]
        pm = PAIR_REGISTRY.get(slug)
        if not pm:
            await query.edit_message_text("Unknown pair.")
            return
        st.sel_pair_slug = slug
        register_state(user_id, st)
        # show direction
        msg = f"{format_selection_summary(st)}\n\nSelect direction:"
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_direction())
        # send snapshot preview (separate message)
        await send_snapshot_photo(
            query.message.chat_id,
            context,
            pm.primary_ex,
            pm.ticker,
            st.default_interval,
            st.default_theme,
            prefix="[PAIR] ",
            alt_exchanges=pm.fallbacks,
        )
        return

    # DIRECTION
    if data.startswith("DIR:"):
        dirw = data.split(":",1)[1]
        direction = parse_direction(dirw) or "CALL"
        st.sel_direction = direction
        register_state(user_id, st)
        msg = f"{format_selection_summary(st)}\n\nSelect expiry:"
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_expiry())
        return

    # EXPIRY
    if data.startswith("EXP:"):
        exp = norm_expiry(data.split(":",1)[1])
        st.sel_expiry = exp
        register_state(user_id, st)
        msg = f"{format_selection_summary(st)}\n\nTrade size mode?"
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_size_mode())
        return

    # SIZE MODE
    if data.startswith("SZMODE:"):
        mode = data.split(":",1)[1]
        if mode == "DOLLAR":
            st.sel_size_mode = SizeMode.DOLLAR
            st.waiting_custom_size = False
            register_state(user_id, st)
            msg = f"{format_selection_summary(st)}\n\nSelect $ amount:"
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_size_dollar())
        else:
            st.sel_size_mode = SizeMode.PERCENT
            st.waiting_custom_size = False
            register_state(user_id, st)
            msg = f"{format_selection_summary(st)}\n\nSelect % of balance:"
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_size_percent())
        return

    # SIZE preset or custom
    if data.startswith("SIZE:"):
        _, mode_char, val = data.split(":",2)
        if val == "CUSTOM":
            st.waiting_custom_size = True
            register_state(user_id, st)
            msg = "Type amount in chat:\n• $ mode: `12.5`\n• % mode: `7%`."
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
            return
        try:
            num = float(val)
        except Exception:
            num = 5.0
        if mode_char == "D":
            st.sel_size_mode = SizeMode.DOLLAR
        else:
            st.sel_size_mode = SizeMode.PERCENT
        st.sel_size_value = num
        st.waiting_custom_size = False
        register_state(user_id, st)
        await show_trade_confirm(query, context)
        return

    # CONFIRM
    if data.startswith("CONF:"):
        action = data.split(":",1)[1]
        await complete_trade(query, context, action)
        return

    # NOP
    if data == "NOP":
        return

    # fallback
    await query.edit_message_text("Unknown selection. Try /pairs.")


async def show_trade_confirm(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    st = get_user_state(context, user_id)
    auto_enabled = (st.trade_mode == TradeMode.AUTO) and bool(UI_VISION_URL)
    msg = f"Review Trade:\n\n{format_selection_summary(st)}\n\nProceed?"
    await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_trade_confirm(auto_enabled))


async def complete_trade(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, action: str):
    user_id = query.from_user.id
    st = get_user_state(context, user_id)
    chat_id = query.message.chat_id

    pm = PAIR_REGISTRY.get(st.sel_pair_slug or "", None)
    pair_disp = pm.display if pm else "EUR/USD"
    primary_ex = pm.primary_ex if pm else DEFAULT_EXCHANGE
    ticker = pm.ticker if pm else "EURUSD"
    fallbacks = pm.fallbacks if pm else EXCHANGE_FALLBACKS

    direction = st.sel_direction or "CALL"
    expiry    = st.sel_expiry or "5m"

    smode = st.sel_size_mode or st.size_mode
    sval  = st.sel_size_value if st.sel_size_value is not None else st.size_value
    amount = resolve_trade_amount(st, smode, sval)

    # optionally debit simulated balance
    if SIM_DEBIT:
        if smode == SizeMode.PERCENT:
            debit_amt = amount  # already converted
        else:
            debit_amt = amount
        st.balance = max(0.0, st.balance - debit_amt)
        register_state(user_id, st)

    arrow = "🟢" if direction == "CALL" else "🔴"

    if action == "AUTO" and UI_VISION_URL and st.trade_mode == TradeMode.AUTO:
        ok = broker_auto_trade(pair_disp, direction, expiry, amount, smode, user_id)
        if ok:
            await query.edit_message_text(
                f"✅ Auto Trade Sent\n{arrow} *{pair_disp}* {direction}  {expiry}  {pretty_size_label(smode, sval, st)}",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await query.edit_message_text(
                f"⚠ Auto trade failed / disabled.\nSending manual instead.",
                parse_mode=ParseMode.MARKDOWN,
            )
            await context.bot.send_message(
                chat_id,
                f"📲 MANUAL Trade:\n{arrow} *{pair_disp}* {direction}  {expiry}  {pretty_size_label(smode, sval, st)}",
                parse_mode=ParseMode.MARKDOWN,
            )
    else:
        await query.edit_message_text(
            f"📲 MANUAL Trade:\n{arrow} *{pair_disp}* {direction}  {expiry}  {pretty_size_label(smode, sval, st)}",
            parse_mode=ParseMode.MARKDOWN,
        )

    # optional snapshot after confirm
    await send_snapshot_photo(chat_id, context, primary_ex, ticker, st.default_interval, st.default_theme, alt_exchanges=fallbacks)

    st.clear_selection()
    register_state(user_id, st)


# =============================================================================
# Custom Amount Text Catcher (after SIZE:CUSTOM)
# We'll intercept next text from user if st.waiting_custom_size True.
# =============================================================================
async def on_text_for_custom_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = get_user_state(context, user_id)
    txt = (update.message.text or "").strip()

    if not st.waiting_custom_size:
        await echo_text(update, context)
        return

    # Determine using st.sel_size_mode if already chosen, else detect from typed
    typed_pct = txt.endswith("%")
    if st.sel_size_mode == SizeMode.PERCENT or typed_pct:
        numtxt = txt.rstrip("% ").replace("$","")
        try:
            pct = float(numtxt)
            st.sel_size_mode = SizeMode.PERCENT
            st.sel_size_value = pct
            st.waiting_custom_size = False
            register_state(user_id, st)
        except Exception:
            await context.bot.send_message(update.effective_chat.id, "❌ Invalid % value. Try again.")
            return
    else:
        # parse $
        try:
            amt = float(txt.replace("$",""))
            st.sel_size_mode = SizeMode.DOLLAR
            st.sel_size_value = amt
            st.waiting_custom_size = False
            register_state(user_id, st)
        except Exception:
            await context.bot.send_message(update.effective_chat.id, "❌ Invalid $ value. Try again.")
            return

    msg = await context.bot.send_message(update.effective_chat.id, "Amount recorded. Confirming…")
    # emulate callback-like confirm
    fake_query = type("FakeCB", (), {})()
    fake_query.message = msg
    fake_query.from_user = update.effective_user
    async def fake_answer(): ...
    fake_query.answer = fake_answer
    await show_trade_confirm(fake_query, context)


# =============================================================================
# TradingView Webhook Server (Flask) → Telegram (and optional auto-trade)
# =============================================================================

flask_app = Flask(__name__)

def _parse_tv_payload(data: dict) -> Dict[str,str]:
    d = {}
    d["chat_id"]   = str(data.get("chat_id") or DEFAULT_CHAT_ID or "")
    d["pair"]      = str(data.get("pair") or data.get("symbol") or data.get("ticker") or "EUR/USD")
    d["direction"] = str(data.get("direction") or "CALL").upper()
    d["expiry"]    = str(data.get("default_expiry_min") or data.get("expiry") or "5m")
    d["strategy"]  = str(data.get("strategy") or "")
    d["winrate"]   = str(data.get("winrate") or "")
    d["timeframe"] = str(data.get("timeframe") or data.get("tf") or DEFAULT_INTERVAL)
    d["theme"]     = str(data.get("theme") or DEFAULT_THEME)
    d["amount"]    = str(data.get("amount") or "")  # optional trade size
    d["sizemode"]  = str(data.get("sizemode") or "") # "dollar" or "%"
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
    Process a TradingView alert payload synchronously (Flask thread).
    Accept both header-based and body-based secrets (TradingView can't set custom header easily).
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

    # parse trade size optional
    trade_amt: Optional[float] = None
    smode: Optional[SizeMode] = None
    amt_s = payload.get("amount","").strip()
    smode_s = payload.get("sizemode","").strip().lower()
    if amt_s:
        try:
            if smode_s.startswith("%") or amt_s.endswith("%") or smode_s == "percent":
                smode = SizeMode.PERCENT
                amt_s = amt_s.rstrip("%")
            elif smode_s in ("$","dollar","usd"):
                smode = SizeMode.DOLLAR
            if smode is None:
                smode = SizeMode.PERCENT if amt_s.endswith("%") else SizeMode.DOLLAR
            amt = float(amt_s.replace("$",""))
            trade_amt = amt
        except Exception:
            trade_amt = None

    ex, tk, _is_otc, alt = resolve_symbol(raw_pair)
    arrow = "🟢" if direction == "CALL" else "🔴"

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

    # Attempt chart
    try:
        node_start_browser()
        png, ex_used = fetch_snapshot_png_any(ex, tk, tf, theme, alt)
        tg_api_send_photo_bytes(chat_id, png, caption=f"{ex_used}:{tk} • TF {tf} • {theme}")
    except Exception as e:
        logger.error("TV snapshot error for %s:%s -> %s", ex, tk, e)
        tg_api_send_message(chat_id, f"⚠ Chart snapshot failed for {raw_pair}: {e}")

    # Optional auto trade from TV alerts
    if AUTO_TRADE_FROM_TV and UI_VISION_URL and trade_amt is not None:
        broker_payload = {
            "pair": raw_pair,
            "direction": direction,
            "expiry": expiry,
            "amount": trade_amt,
            "sizemode": smode.value if smode else "dollar",
            "source": "TVWebhook",
            "strategy": strat,
            "winrate": winrate,
        }
        try:
            r = _httpx_client.post(UI_VISION_URL, json=broker_payload, timeout=15)
            if r.status_code == 200:
                tg_api_send_message(chat_id, "✅ Auto trade triggered from TV alert.", parse_mode="Markdown")
            else:
                tg_api_send_message(chat_id, f"⚠ Auto trade failed: {r.status_code}.", parse_mode="Markdown")
        except Exception as e:
            tg_api_send_message(chat_id, f"⚠ Auto trade error: {e}", parse_mode="Markdown")

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

# compatibility alias
@flask_app.post("/webhook")
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


# =============================================================================
# Dispatcher Setup
# =============================================================================

def build_application() -> Application:
    app = ApplicationBuilder().token(TOKEN).rate_limiter(AIORateLimiter()).build()

    # periodic persistence job (every 60s as backup)
    app.job_queue.run_repeating(persist_all_job, interval=60, first=60)

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("pairs",     cmd_pairs))
    app.add_handler(CommandHandler("snap",      cmd_snap))
    app.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    app.add_handler(CommandHandler("snapall",   cmd_snapall))
    app.add_handler(CommandHandler("trade",     cmd_trade))
    app.add_handler(CommandHandler("next",      cmd_next))
    app.add_handler(CommandHandler("balance",   cmd_balance))
    app.add_handler(CommandHandler("setbal",    cmd_setbal))
    app.add_handler(CommandHandler("setbalpct", cmd_setbalpct))
    app.add_handler(CommandHandler("setmode",   cmd_setmode))

    # callback queries
    app.add_handler(CallbackQueryHandler(on_callback))

    # text messages (custom amount or quick parse)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text_for_custom_size))

    # unknown commands fallback
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    return app


# =============================================================================
# Main
# =============================================================================

def main():
    logger.info(
        "Bot starting… BASE_URL=%s | DefaultEX=%s | WebhookPort=%s | UI_VISION_URL=%s | AUTO_TRADE_FROM_TV=%s | SIM_DEBIT=%s",
        BASE_URL, DEFAULT_EXCHANGE, TV_WEBHOOK_PORT, UI_VISION_URL, AUTO_TRADE_FROM_TV, SIM_DEBIT
    )

    # Load persisted state
    load_state_registry()

    # start Flask background server for TV alerts
    start_flask_background()

    # build and run the Telegram bot (blocking)
    application = build_application()
    application.run_polling(drop_pending_updates=True)

    # on exit, ensure save
    save_state_registry()


if __name__ == "__main__":
    main()
