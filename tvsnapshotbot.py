#!/usr/bin/env python
"""
TradingView Snapshot Telegram Bot - Clean Commands + Pair Validation
====================================================================
Features:
• Default exchange = QUOTEX (switchable via env var).
• Screenshot backend: Node/Puppeteer (/run, /start-browser) on Render or local.
• Commands: /start /help /pairs /fx /otc /snap /snapmulti /snapall /tokencheck
• Clean Markdown help text and usage examples.
• Pair validation + auto-suggestions (EURUSD → EUR/USD, EURUSDOTC → EUR/USD-OTC, etc.).
• Rate limiting (per-chat) + global throttle (protect Render).
• Retry screenshot fetch (3 tries, 5s backoff).
• Retry Telegram sends (3 tries, exponential backoff).
• Rotating logs: logs/tvsnapshotbot.log & logs/signals.log.
• TradingView webhook server (/tv, /webhook) -> Telegram message + chart (background thread).
"""

import os
import io
import re
import time
import json
import difflib
import asyncio
import logging
import threading
from logging.handlers import RotatingFileHandler
from typing import List, Tuple, Dict, Optional

import requests
from flask import Flask, request, jsonify

from telegram import Update, InputMediaPhoto
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)

log_handler_main = RotatingFileHandler(
    "logs/tvsnapshotbot.log", maxBytes=5 * 1024 * 1024, backupCount=3
)
log_handler_signals = RotatingFileHandler(
    "logs/signals.log", maxBytes=5 * 1024 * 1024, backupCount=5
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[log_handler_main, logging.StreamHandler()],
)
logger = logging.getLogger("TVSnapBot")

signals_logger = logging.getLogger("TVSignals")
signals_logger.setLevel(logging.INFO)
signals_logger.addHandler(log_handler_signals)

# ─────────────────────────────────────────────────────────
# Environment / Config
# ─────────────────────────────────────────────────────────
TOKEN            = os.environ.get("TELEGRAM_BOT_TOKEN") or "REPLACE_ME"
DEFAULT_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID") or "6337160812"
BASE_URL         = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000")
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "QUOTEX")  # <— switched to Quotex
DEFAULT_INTERVAL = os.environ.get("DEFAULT_INTERVAL", "1")
DEFAULT_THEME    = os.environ.get("DEFAULT_THEME", "dark")
TV_WEBHOOK_PORT  = int(os.environ.get("TV_WEBHOOK_PORT", "8081"))
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET")  # optional shared secret

if TOKEN == "REPLACE_ME":
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN environment variable before running.")

# reuse HTTP session
_http = requests.Session()

# ─────────────────────────────────────────────────────────
# Rate limiting (per chat + global)
# ─────────────────────────────────────────────────────────
LAST_SNAPSHOT_PER_CHAT: Dict[int, float] = {}
RATE_LIMIT_SECONDS = 3

GLOBAL_LAST_SNAPSHOT = 0.0
GLOBAL_MIN_GAP = 0.75  # sec between any two requests to Render

def rate_limited(chat_id: int) -> bool:
    now = time.time()
    last = LAST_SNAPSHOT_PER_CHAT.get(chat_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    LAST_SNAPSHOT_PER_CHAT[chat_id] = now
    return False

def global_throttle_wait():
    """Block so we don't hammer Render too fast."""
    global GLOBAL_LAST_SNAPSHOT
    now = time.time()
    gap = now - GLOBAL_LAST_SNAPSHOT
    if gap < GLOBAL_MIN_GAP:
        time.sleep(GLOBAL_MIN_GAP - gap)
    GLOBAL_LAST_SNAPSHOT = time.time()

# ─────────────────────────────────────────────────────────
# Pair lists (exact names shown to user)
# ─────────────────────────────────────────────────────────
FX_PAIRS: List[str] = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD",
    "NZD/USD", "USD/CAD", "EUR/GBP", "EUR/JPY", "GBP/JPY",
    "AUD/JPY", "NZD/JPY", "EUR/AUD", "GBP/AUD", "EUR/CAD",
    "USD/MXN", "USD/TRY", "USD/ZAR", "AUD/CHF", "EUR/CHF",
]

OTC_PAIRS: List[str] = [
    "EUR/USD-OTC", "GBP/USD-OTC", "USD/JPY-OTC", "USD/CHF-OTC", "AUD/USD-OTC",
    "NZD/USD-OTC", "USD/CAD-OTC", "EUR/GBP-OTC", "EUR/JPY-OTC", "GBP/JPY-OTC",
    "AUD/CHF-OTC", "EUR/CHF-OTC", "KES/USD-OTC", "MAD/USD-OTC",
    "USD/BDT-OTC", "USD/MXN-OTC", "USD/MYR-OTC", "USD/PKR-OTC",
]

ALL_PAIRS: List[str] = FX_PAIRS + OTC_PAIRS

# Build quick lookup maps
def _canon_key(pair: str) -> str:
    # Keep "-OTC" so OTC variants stay distinct; remove slashes/spaces/underscores/dashes (except -OTC part)
    s = pair.strip().upper()
    s = s.replace(" ", "")
    s = s.replace("_", "")
    # temporarily protect "-OTC"
    s = s.replace("-OTC", "_OTC_")
    s = s.replace("/", "")
    s = s.replace("-", "")
    s = s.replace("_OTC_", "-OTC")
    return s

DISPLAY_LOOKUP: Dict[str, str] = {}
for p in ALL_PAIRS:
    DISPLAY_LOOKUP[_canon_key(p)] = p

# Pair map to underlying tickers (Quotex)
PAIR_MAP: Dict[str, Tuple[str, str]] = {}  # canon -> (exchange, ticker)

# Map majors to Quotex
for p in FX_PAIRS:
    tk = p.replace("/", "")
    PAIR_MAP[_canon_key(p)] = (DEFAULT_EXCHANGE, tk)

# OTC -> underlying (still chart real markets)
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
    PAIR_MAP[_canon_key(p)] = (DEFAULT_EXCHANGE, tk)

ALL_CANON_KEYS = list(DISPLAY_LOOKUP.keys())

# ─────────────────────────────────────────────────────────
# Pair Normalization & Suggestion
# ─────────────────────────────────────────────────────────
def normalize_user_pair_text(raw: str) -> str:
    """Return best display form (EUR/USD or EUR/USD-OTC) if known, else raw unchanged."""
    key = _canon_key(raw)
    return DISPLAY_LOOKUP.get(key, raw)

def guess_otc_variant(raw: str) -> Optional[str]:
    # user typed eurusd or eurusd-otc? try to match
    k = _canon_key(raw)
    # if already direct map good
    if k in DISPLAY_LOOKUP:
        return DISPLAY_LOOKUP[k]
    # try base no -otc
    # remove -OTC if present in messy form
    nootc = k.replace("-OTC", "")
    # find first key that startswith or equals this
    matches = [disp for ck, disp in DISPLAY_LOOKUP.items() if ck.replace("-OTC", "") == nootc]
    if matches:
        # prefer non-OTC exact
        for m in matches:
            if "-OTC" not in m:
                return m
        return matches[0]
    return None

def validate_pair_input(raw: str) -> Tuple[bool, str, Optional[Tuple[str, str]]]:
    """
    Validate a user-supplied pair string.
    Returns: (is_valid, display_name, (exchange, ticker) or None)
    """
    disp = normalize_user_pair_text(raw)
    k = _canon_key(disp)
    if k in PAIR_MAP:
        return True, DISPLAY_LOOKUP.get(k, disp), PAIR_MAP[k]

    # try fallback guess
    maybe = guess_otc_variant(raw)
    if maybe:
        mk = _canon_key(maybe)
        if mk in PAIR_MAP:
            return True, DISPLAY_LOOKUP.get(mk, maybe), PAIR_MAP[mk]

    return False, disp, None

def pair_suggestions(raw: str, n: int = 5) -> List[str]:
    k = _canon_key(raw)
    # build keys to compare by removing "-OTC" marking
    cleaned_keys = {ck.replace("-OTC", ""): ck for ck in ALL_CANON_KEYS}
    cleaned = k.replace("-OTC", "")
    close_clean = difflib.get_close_matches(cleaned, list(cleaned_keys.keys()), n=n, cutoff=0.4)
    sug = []
    for c in close_clean:
        ck = cleaned_keys[c]
        sug.append(DISPLAY_LOOKUP[ck])
    # also substring suggestions
    raw_u = raw.upper()
    for p in ALL_PAIRS:
        if raw_u.replace("/", "")[:3] in p.upper().replace("/", ""):
            if p not in sug:
                sug.append(p)
    return sug[:n]

# ─────────────────────────────────────────────────────────
# Interval & theme normalization
# ─────────────────────────────────────────────────────────
def norm_interval(tf: str) -> str:
    if not tf:
        return DEFAULT_INTERVAL
    t = tf.strip().lower()
    if t.endswith("m"):
        num = t[:-1]
        return num if num.isdigit() else DEFAULT_INTERVAL
    if t.endswith("h"):
        num = t[:-1]
        return str(int(num) * 60) if num.isdigit() else "60"
    if t in ("d", "1d", "day"):
        return "D"
    if t in ("w", "1w", "week"):
        return "W"
    if t in ("mo", "1mo", "month"):
        return "M"
    if t.isdigit():
        return t
    return DEFAULT_INTERVAL

def norm_theme(val: str) -> str:
    if not val:
        return DEFAULT_THEME
    return "light" if val.lower().startswith("l") else "dark"

# ─────────────────────────────────────────────────────────
# Screenshot backend helpers
# ─────────────────────────────────────────────────────────
def node_start_browser():
    try:
        url = f"{BASE_URL}/start-browser"
        r = _http.get(url, timeout=10)
        logger.info("start-browser %s %s", r.status_code, r.text[:100])
    except Exception as e:
        logger.warning("start-browser failed: %s", e)

def fetch_snapshot_png_retry(ex: str, tk: str, interval: str, theme: str) -> bytes:
    """
    Try to fetch PNG up to 3 times with 5s backoff.
    """
    last_err = None
    for attempt in range(1, 4):
        try:
            global_throttle_wait()
            url = f"{BASE_URL}/run?exchange={ex}&ticker={tk}&interval={interval}&theme={theme}"
            r = _http.get(url, timeout=75)
            if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image"):
                return r.content
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            last_err = str(e)
        logger.warning("Snapshot %s:%s attempt %d failed: %s", ex, tk, attempt, last_err)
        time.sleep(5)
    raise RuntimeError(f"Failed after retries: {last_err}")

# ─────────────────────────────────────────────────────────
# Telegram send helpers (w/ retry) – used by webhook thread
# ─────────────────────────────────────────────────────────
def telegram_api_post(endpoint: str, data=None, files=None, max_retries: int = 3, timeout: int = 60):
    """
    Low-level POST to Telegram Bot API with retry/backoff.
    """
    url = f"https://api.telegram.org/bot{TOKEN}/{endpoint}"
    last = None
    for attempt in range(1, max_retries + 1):
        try:
            if files:
                resp = _http.post(url, data=data, files=files, timeout=timeout)
            else:
                resp = _http.post(url, json=data, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            last = str(e)
        sleep_for = min(2 ** (attempt - 1), 10)
        logger.warning("Telegram POST %s attempt %d failed: %s (retry %ss)", endpoint, attempt, last, sleep_for)
        time.sleep(sleep_for)
    logger.error("Telegram POST %s failed after retries: %s", endpoint, last)
    return None

def tg_api_send_message(chat_id: str, text: str, parse_mode: Optional[str] = None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return telegram_api_post("sendMessage", data=payload)

def tg_api_send_photo_bytes(chat_id: str, png: bytes, caption: str = ""):
    files = {"photo": ("chart.png", png, "image/png")}
    data = {"chat_id": chat_id, "caption": caption}
    return telegram_api_post("sendPhoto", data=data, files=files)

# ─────────────────────────────────────────────────────────
# Telegram send helpers (async for bot commands)
# ─────────────────────────────────────────────────────────
async def send_snapshot_photo(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    exchange: str,
    ticker: str,
    interval: str,
    theme: str,
    prefix: str = "",
):
    if rate_limited(chat_id):
        await context.bot.send_message(chat_id, "⏳ Too many requests. Please wait a few seconds...")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    await asyncio.to_thread(node_start_browser)

    try:
        png = await asyncio.to_thread(fetch_snapshot_png_retry, exchange, ticker, interval, theme)
        caption = f"{prefix}{exchange}:{ticker} • TF {interval} • {theme}"
        await context.bot.send_photo(chat_id=chat_id, photo=png, caption=caption)
    except Exception as e:
        logger.exception("snapshot photo error")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Failed: {exchange}:{ticker} ({e})")

def build_media_items_sync(
    pairs: List[Tuple[str, str, str]],
    interval: str,
    theme: str,
    prefix: str,
) -> List[InputMediaPhoto]:
    out: List[InputMediaPhoto] = []
    for ex, tk, lab in pairs:
        try:
            png = fetch_snapshot_png_retry(ex, tk, interval, theme)
            bio = io.BytesIO(png)
            bio.name = "chart.png"
            cap = f"{prefix}{ex}:{tk} • {lab} • TF {interval} • {theme}"
            out.append(InputMediaPhoto(media=bio, caption=cap))
        except Exception as e:
            logger.warning("Failed building media for %s:%s -> %s", ex, tk, e)
    return out

async def send_media_group_chunked(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    media_items: List[InputMediaPhoto],
    chunk_size: int = 5,
):
    for i in range(0, len(media_items), chunk_size):
        chunk = media_items[i:i+chunk_size]
        if not chunk:
            continue
        if len(chunk) > 1:
            for m in chunk[1:]:
                m.caption = None
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(1.0)

# ─────────────────────────────────────────────────────────
# Command argument parsing helpers
# ─────────────────────────────────────────────────────────
def parse_snap_args(args: List[str]) -> Tuple[str, str, str, str, str]:
    # returns (display, exchange, ticker, interval, theme)
    symbol_raw = args[0] if args else "EUR/USD"
    tf = DEFAULT_INTERVAL
    th = DEFAULT_THEME
    if len(args) >= 2 and args[1].lower() not in ("dark", "light"):
        tf = args[1]
    if len(args) >= 2 and args[-1].lower() in ("dark", "light"):
        th = args[-1].lower()
    elif len(args) >= 3 and args[2].lower() in ("dark", "light"):
        th = args[2].lower()

    ok, disp, ex_tk = validate_pair_input(symbol_raw)
    if ok:
        ex, tk = ex_tk
    else:
        ex, tk = (DEFAULT_EXCHANGE, re.sub(r"[^A-Z0-9]", "", symbol_raw.upper()))
    return disp, ex, tk, norm_interval(tf), norm_theme(th)

def parse_multi_args(args: List[str]) -> Tuple[List[str], str, str]:
    if not args:
        return [], DEFAULT_INTERVAL, DEFAULT_THEME
    theme = DEFAULT_THEME
    if args[-1].lower() in ("dark", "light"):
        theme = args[-1].lower()
        args = args[:-1]
    tf = DEFAULT_INTERVAL
    if args and not re.search(r"[^\d]", args[-1]):  # numeric interval
        tf = args[-1]
        args = args[:-1]
    return args, norm_interval(tf), norm_theme(theme)

# ─────────────────────────────────────────────────────────
# Clean Telegram Bot Commands
# ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name if update.effective_user else "Trader"
    msg = (
        f"👋 Hello {name}!\n\n"
        "I'm your *TradingView Snapshot Bot* 📸.\n\n"
        "Commands:\n"
        "• `/snap EUR/USD 5 dark` – Single snapshot.\n"
        "• `/snapmulti EUR/USD GBP/USD 1 light` – Multiple snapshots.\n"
        "• `/snapall` – All FX + OTC pairs.\n"
        "• `/pairs` – All supported pairs.\n"
        "• `/fx` – FX pairs.\n"
        "• `/otc` – OTC pairs.\n"
        "• `/help` – Full help.\n"
        "• `/tokencheck` – Bot connection status.\n"
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg, parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📘 *Help & Usage*\n\n"
        "`/snap SYMBOL [interval] [theme]`\n"
        "   Example: `/snap EUR/USD 5 dark`\n"
        "`/snapmulti S1 S2 ... [interval] [theme]`\n"
        "   Example: `/snapmulti EUR/USD GBP/USD 15 light`\n"
        "`/snapall` – Snapshot of all supported pairs.\n"
        "`/pairs` – View all FX + OTC pairs.\n"
        "`/fx` – FX only.\n"
        "`/otc` – OTC only.\n\n"
        "*Intervals:* `1, 3, 5, 15, 30, 60` (minutes) or `D, W`.\n"
        "*Themes:* `dark` or `light`.\n"
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg, parse_mode="Markdown")

def _pairs_list_md(pairs: List[str]) -> str:
    return "\n".join([f"{i+1}. `{p}`" for i, p in enumerate(pairs)])

async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📊 *Available Pairs*\n\n"
        "💱 *FX Pairs:*\n"
        f"{_pairs_list_md(FX_PAIRS)}\n\n"
        "🕒 *OTC Pairs:*\n"
        f"{_pairs_list_md(OTC_PAIRS)}\n\n"
        "_Use `/snap EUR/USD` or `/snap EUR/USD-OTC`._"
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg, parse_mode="Markdown")

async def cmd_fx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "💱 *FX Pairs:*\n" + _pairs_list_md(FX_PAIRS)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg, parse_mode="Markdown")

async def cmd_otc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "🕒 *OTC Pairs:*\n" + _pairs_list_md(OTC_PAIRS)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg, parse_mode="Markdown")

async def cmd_snap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Usage: `/snap EUR/USD 5 dark`", parse_mode="Markdown")
        return

    disp, ex, tk, tf, th = parse_snap_args(context.args)
    ok, _, _ = validate_pair_input(disp)
    if not ok:
        sugs = pair_suggestions(disp)
        sug_txt = ", ".join(f"`{s}`" for s in sugs) if sugs else "no close matches"
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Unknown pair `{disp}`.\nSuggested: {sug_txt}",
            parse_mode="Markdown"
        )
        return

    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, th)

async def cmd_snapmulti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs, tf, th = parse_multi_args(context.args)
    if not pairs:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Usage: `/snapmulti EUR/USD GBP/USD 5 dark`", parse_mode="Markdown")
        return

    invalid = []
    valid_trip: List[Tuple[str, str, str]] = []
    for raw in pairs:
        ok, disp, ex_tk = validate_pair_input(raw)
        if ok and ex_tk:
            ex, tk = ex_tk
            valid_trip.append((ex, tk, disp))
        else:
            invalid.append(raw)

    chat_id = update.effective_chat.id

    if invalid:
        sug_lines = []
        for bad in invalid:
            sugs = pair_suggestions(bad)
            if sugs:
                sug_lines.append(f"• `{bad}` → {', '.join(f'`{s}`' for s in sugs)}")
            else:
                sug_lines.append(f"• `{bad}` (no match)")
        warn_msg = "⚠ Some pairs are invalid:\n" + "\n".join(sug_lines)
        await context.bot.send_message(chat_id=chat_id, text=warn_msg, parse_mode="Markdown")

    if not valid_trip:
        await context.bot.send_message(chat_id=chat_id, text="❌ No valid pairs found. Try `/pairs`.", parse_mode="Markdown")
        return

    await context.bot.send_message(chat_id, f"📸 Capturing *{len(valid_trip)}* charts… Please wait.", parse_mode="Markdown")

    media_items = await asyncio.to_thread(build_media_items_sync, valid_trip, tf, th, prefix="[MULTI] ")
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)

async def cmd_snapall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, f"⚡ Capturing all *{len(ALL_PAIRS)}* pairs… This may take a while.", parse_mode="Markdown")
    p_trip: List[Tuple[str, str, str]] = []
    for p in ALL_PAIRS:
        ok, disp, ex_tk = validate_pair_input(p)
        if ok and ex_tk:
            ex, tk = ex_tk
            p_trip.append((ex, tk, disp))
    media_items = await asyncio.to_thread(build_media_items_sync, p_trip, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[ALL] ")
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)

async def cmd_tokencheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    resp = _http.get(f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=30)
    if resp.ok:
        data = resp.json()
        await context.bot.send_message(update.effective_chat.id, f"✅ Token OK\n{json.dumps(data, indent=2)}")
    else:
        await context.bot.send_message(update.effective_chat.id, f"❌ Token test failed: {resp.status_code} {resp.text}")

# Echo fallback text
async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, f"You said: {update.message.text}\nTry /help.")

# Unknown command fallback
async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, "❌ Unknown command. Try /help.")

# ─────────────────────────────────────────────────────────
# TradingView Webhook Server (Flask) → Telegram
# ─────────────────────────────────────────────────────────
flask_app = Flask(__name__)

def normalize_direction(val: str) -> str:
    if not val:
        return "CALL"
    v = val.strip().upper()
    if v in ("BUY", "LONG", "UP", "CALL", "BULL", "BULLISH"):
        return "CALL"
    if v in ("SELL", "SHORT", "DOWN", "PUT", "BEAR", "BEARISH"):
        return "PUT"
    return "CALL"  # fallback

def _parse_tv_payload(data: dict) -> Dict[str, str]:
    """
    Normalize a TradingView JSON alert payload into standard fields.
    Accept keys: pair, symbol, ticker; direction; expiry; strategy; winrate; timeframe; theme; chat_id.
    """
    d = {}
    d["chat_id"]   = str(data.get("chat_id") or DEFAULT_CHAT_ID)
    d["pair"]      = str(data.get("pair") or data.get("symbol") or data.get("ticker") or "EURUSD")
    d["direction"] = normalize_direction(data.get("direction") or "")
    d["expiry"]    = str(data.get("expiry") or "")
    d["strategy"]  = str(data.get("strategy") or "")
    d["winrate"]   = str(data.get("winrate") or "")
    d["timeframe"] = str(data.get("timeframe") or data.get("tf") or DEFAULT_INTERVAL)
    d["theme"]     = str(data.get("theme") or DEFAULT_THEME)
    d["price"]     = str(data.get("price") or "")
    d["time"]      = str(data.get("time") or "")
    return d

def process_tv_alert_async(payload: Dict[str, str]):
    """
    Runs in background thread after Flask returns 200.
    """
    signals_logger.info("ALERT_IN %s", json.dumps(payload))
    chat_id   = payload["chat_id"]
    raw_pair  = payload["pair"]
    direction = payload["direction"]
    expiry    = payload["expiry"]
    strat     = payload["strategy"]
    winrate   = payload["winrate"]
    tf        = norm_interval(payload["timeframe"])
    theme     = norm_theme(payload["theme"])
    price     = payload.get("price", "")
    timestr   = payload.get("time", "")

    # Validate incoming pair (webhook)
    ok, disp, ex_tk = validate_pair_input(raw_pair)
    if not ok or not ex_tk:
        sugs = pair_suggestions(raw_pair)
        tg_api_send_message(chat_id, f"⚠ Unknown webhook pair: {raw_pair}\nSuggestions: {', '.join(sugs) if sugs else 'none'}")
        signals_logger.warning("ALERT_BADPAIR %s", raw_pair)
        return
    ex, tk = ex_tk

    # send text first
    msg = (
        f"🔔 *TradingView Alert*\n"
        f"Pair: {disp}\n"
        f"Direction: {direction}\n"
        f"Expiry: {expiry}\n"
        f"Price: {price}\n"
        f"Strategy: {strat}\n"
        f"Win Rate: {winrate}\n"
        f"TF: {tf} • Theme: {theme}\n"
        f"Time: {timestr}"
    )
    tg_api_send_message(chat_id, msg, parse_mode="Markdown")

    # try screenshot
    try:
        node_start_browser()
        png = fetch_snapshot_png_retry(ex, tk, tf, theme)
        tg_api_send_photo_bytes(chat_id, png, caption=f"{ex}:{tk} • TF {tf} • {theme}")
        signals_logger.info("ALERT_OK %s", disp)
    except Exception as e:
        logger.error("TV snapshot error: %s", e)
        tg_api_send_message(chat_id, f"⚠ Chart snapshot failed for {disp}: {e}")
        signals_logger.error("ALERT_FAIL %s %s", disp, e)

def _handle_tv_alert(data: dict):
    """
    Process TradingView alert quickly: validate, normalize, queue thread.
    """
    if WEBHOOK_SECRET:
        hdr = request.headers.get("X-Webhook-Token", "")
        if hdr != WEBHOOK_SECRET:
            logger.warning("Webhook secret mismatch.")
            return {"ok": False, "error": "unauthorized"}, 403

    payload = _parse_tv_payload(data)
    logger.info("TV payload normalized: %s", payload)

    # Spawn worker thread so TV gets fast 200
    threading.Thread(target=process_tv_alert_async, args=(payload,), daemon=True).start()
    return {"ok": True}, 200

@flask_app.route("/tv", methods=["POST"])
def tv_route():
    try:
        data = request.get_json(force=True)
    except Exception as e:
        logger.error("TV /tv parse error: %s", e)
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    body, code = _handle_tv_alert(data)
    return jsonify(body), code

@flask_app.route("/webhook", methods=["POST"])
def webhook_route():
    # alias to /tv
    try:
        data = request.get_json(force=True)
    except Exception as e:
        logger.error("TV /webhook parse error: %s", e)
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    body, code = _handle_tv_alert(data)
    return jsonify(body), code

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

# ─────────────────────────────────────────────────────────
# Main (non-async) — avoids nested event loop crash
# ─────────────────────────────────────────────────────────
def main():
    start_flask_background()

    application = ApplicationBuilder().token(TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start",     cmd_start))
    application.add_handler(CommandHandler("help",      cmd_help))
    application.add_handler(CommandHandler("pairs",     cmd_pairs))
    application.add_handler(CommandHandler("fx",        cmd_fx))
    application.add_handler(CommandHandler("otc",       cmd_otc))
    application.add_handler(CommandHandler("snap",      cmd_snap))
    application.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    application.add_handler(CommandHandler("snapall",   cmd_snapall))
    application.add_handler(CommandHandler("tokencheck", cmd_tokencheck))

    # Fallbacks
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info("Bot polling… (Quotex default)  |  Webhook on port %s", TV_WEBHOOK_PORT)
    # NOTE: This call blocks and manages its own event loop. Do NOT wrap in asyncio.run().
    application.run_polling()

if __name__ == "__main__":
    main()
