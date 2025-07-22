#!/usr/bin/env python
"""
TradingView → Telegram Snapshot Bot  (Pocket Option / FX Edition)
=================================================================
Works with the companion Node snapshot service (server.js).

Features
--------
✓ Multi-exchange fallback (CURRENCY → FOREXCOM → OANDA → FX_IDC → FX → IDC → QUOTEX → IQOPTION)
✓ OTC pair normalization (EUR/USD-OTC etc.)
✓ /snap, /trade, /snapmulti, /snapall, /pairs, /next, /help
✓ TradingView webhook (/tv, /webhook) accepts Pine alert() JSON
✓ Rate limiting per-chat + global throttle
✓ Retry + graceful placeholder image fallback (no 404 loops)
✓ Rotating log file (logs/tvsnapshotbot.log)

Env Vars (PowerShell examples)
------------------------------
$env:TELEGRAM_BOT_TOKEN="123456:ABCDEF"
$env:TELEGRAM_CHAT_ID="6337160812"      # default target if payload missing chat_id
$env:SNAPSHOT_BASE_URL="http://localhost:10000"  # or Render / ngrok endpoint
$env:TV_WEBHOOK_PORT="8081"
$env:WEBHOOK_SECRET="optional_secret"   # if using shared secret

Run
---
python tvsnapshotbot.py
"""

import asyncio
import os
import io
import re
import time
import json
import threading
import logging
from logging.handlers import RotatingFileHandler
from typing import List, Tuple, Dict, Optional

import requests
from flask import Flask, request, jsonify

from telegram import Update, InputMediaPhoto
from telegram.constants import ChatAction, ParseMode
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
_log_file = "logs/tvsnapshotbot.log"
log_handler = RotatingFileHandler(_log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[log_handler, logging.StreamHandler()],
)
logger = logging.getLogger("TVSnapBot")

# ─────────────────────────────────────────────────────────
# Env & Config
# ─────────────────────────────────────────────────────────
TOKEN            = os.environ.get("TELEGRAM_BOT_TOKEN")
DEFAULT_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
BASE_URL         = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000")
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "CURRENCY")
DEFAULT_INTERVAL = os.environ.get("DEFAULT_INTERVAL", "1")
DEFAULT_THEME    = os.environ.get("DEFAULT_THEME", "dark")
TV_WEBHOOK_PORT  = int(os.environ.get("TV_WEBHOOK_PORT", "8081"))
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET")  # optional shared body|header secret

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment.")

_http = requests.Session()

# ─────────────────────────────────────────────────────────
# Rate Limiting
# ─────────────────────────────────────────────────────────
LAST_SNAPSHOT_PER_CHAT: Dict[int, float] = {}
RATE_LIMIT_SECONDS = 3
GLOBAL_LAST_SNAPSHOT = 0.0
GLOBAL_MIN_GAP = 0.75  # seconds between *any* snapshot calls

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

# ─────────────────────────────────────────────────────────
# Pair Lists (displayed exactly)
# ─────────────────────────────────────────────────────────
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

ALL_PAIRS = FX_PAIRS + OTC_PAIRS

# canonicalization helper
def _canon_key(pair: str) -> str:
    return pair.strip().upper().replace(" ", "").replace("/", "")

# build lookup map to primary exchange & ticker
PAIR_MAP: Dict[str, Tuple[str, str]] = {}
for p in FX_PAIRS:
    PAIR_MAP[_canon_key(p)] = ("CURRENCY", p.replace("/", ""))  # TradingView global currency feed

_underlying_otc = {
    "EUR/USD-OTC":"EURUSD","GBP/USD-OTC":"GBPUSD","USD/JPY-OTC":"USDJPY",
    "USD/CHF-OTC":"USDCHF","AUD/USD-OTC":"AUDUSD","NZD/USD-OTC":"NZDUSD",
    "USD/CAD-OTC":"USDCAD","EUR/GBP-OTC":"EURGBP","EUR/JPY-OTC":"EURJPY",
    "GBP/JPY-OTC":"GBPJPY","AUD/CHF-OTC":"AUDCHF","EUR/CHF-OTC":"EURCHF",
    "KES/USD-OTC":"USDKES","MAD/USD-OTC":"USDMAD","USD/BDT-OTC":"USDBDT",
    "USD/MXN-OTC":"USDMXN","USD/MYR-OTC":"USDMYR","USD/PKR-OTC":"USDPKR",
}
for p, tk in _underlying_otc.items():
    PAIR_MAP[_canon_key(p)] = ("IQOPTION", tk)

# fallback exchange search order (used in fetch_snapshot_png_any)
EXCHANGE_FALLBACKS = [
    "FOREXCOM",
    "OANDA",
    "FX_IDC",
    "FX",
    "IDC",
    "QUOTEX",
    "IQOPTION",
]

# Known FX feed list (extra belt+suspenders)
KNOWN_FX_EXCHANGES = ["CURRENCY", "FOREXCOM", "OANDA", "FX_IDC", "FX", "IDC"]

# ─────────────────────────────────────────────────────────
# Normalization helpers
# ─────────────────────────────────────────────────────────
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

# ─────────────────────────────────────────────────────────
# Direction parsing (Pocket Option / binary friendly)
# ─────────────────────────────────────────────────────────
_CALL_WORDS = {"CALL", "BUY", "UP", "LONG"}
_PUT_WORDS  = {"PUT", "SELL", "DOWN", "SHORT"}

def parse_direction(word: Optional[str]) -> Optional[str]:
    if not word:
        return None
    w = word.strip().upper()
    if w in _CALL_WORDS:
        return "CALL"
    if w in _PUT_WORDS:
        return "PUT"
    return None

# ─────────────────────────────────────────────────────────
# Symbol resolution -> (exchange, ticker, is_otc, alt_exchanges_list)
# ─────────────────────────────────────────────────────────
def resolve_symbol(raw: str) -> Tuple[str, str, bool, List[str]]:
    """
    Accepts:
      EUR/USD
      EUR/USD-OTC
      CURRENCY:EURUSD
      FOREXCOM:EURUSD
      EURUSD
    Returns: (primary_exchange, ticker, is_otc, alt_exchanges_list)
    """
    if not raw:
        return DEFAULT_EXCHANGE, "EURUSD", False, EXCHANGE_FALLBACKS[:]

    s = raw.strip().upper()
    is_otc = "-OTC" in s

    # explicit EX:TK
    if ":" in s:
        ex, tk = s.split(":", 1)
        alt = [x for x in EXCHANGE_FALLBACKS if x != ex.upper()]
        return ex.upper(), tk, is_otc, alt

    key = _canon_key(s)
    if key in PAIR_MAP:
        ex, tk = PAIR_MAP[key]
        # alt: everything except the chosen
        alt = [x for x in EXCHANGE_FALLBACKS if x != ex.upper()]
        # always include core CURRENCY top as early fallback
        if ex.upper() != "CURRENCY":
            alt.insert(0, "CURRENCY")
        return ex.upper(), tk, is_otc, alt

    # fallback guess – strip junk
    tk = re.sub(r"[^A-Z0-9]", "", s)
    ex = DEFAULT_EXCHANGE.upper()
    alt = [x for x in EXCHANGE_FALLBACKS if x != ex]
    if ex != "CURRENCY":
        alt.insert(0, "CURRENCY")
    return ex, tk, is_otc, alt

# ─────────────────────────────────────────────────────────
# Snapshot backend calls (Node service)
# ─────────────────────────────────────────────────────────
def _attempt_snapshot_url(ex: str, tk: str, interval: str, theme: str, base: str, source_hint: str) -> Tuple[bool, Optional[bytes], str]:
    """
    Single attempt at Node GET /run → (success?, png_bytes, error_string)
    """
    try:
        global_throttle_wait()
        params = {
            "base": base,
            "exchange": ex,
            "ticker": tk,
            "interval": interval,
            "theme": theme,
            "source": source_hint,  # tv|iq|auto
        }
        # build URL
        url = f"{BASE_URL}/run"
        r = _http.get(url, params=params, timeout=75)
        ct = r.headers.get("Content-Type", "")
        if r.status_code == 200 and ct.startswith("image"):
            return True, r.content, ""
        return False, None, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, None, str(e)

def fetch_snapshot_png_any(primary_ex: str, tk: str, interval: str, theme: str, base: str = "chart", alt_exchanges: Optional[List[str]] = None) -> Tuple[bytes, str]:
    """
    Multi-exchange fallback. Returns (png_bytes, exchange_used).
    Always tries in this order:
      1. primary_ex
      2. alt_exchanges param (if any)
      3. EXCHANGE_FALLBACKS
      4. 'CURRENCY' final sanity
    If everything fails, raises RuntimeError.
    """
    tried = []
    last_err = None

    ordered = [primary_ex.upper()]
    if alt_exchanges:
        ordered.extend([x.upper() for x in alt_exchanges])
    ordered.extend(EXCHANGE_FALLBACKS)
    ordered.append("CURRENCY")

    # dedup
    dedup = []
    seen = set()
    for x in ordered:
        if x not in seen:
            dedup.append(x)
            seen.add(x)

    # If looks like OTC, hint 'iq'; else 'tv'
    source_hint = "iq" if tk.endswith("OTC") or primary_ex.upper() == "IQOPTION" else "tv"

    for ex in dedup:
        tried.append(ex)
        ok, png, err = _attempt_snapshot_url(ex, tk, interval, theme, base, source_hint)
        if ok and png:
            logger.info("Snapshot success %s:%s via %s (%d bytes)", ex, tk, ex, len(png))
            return png, ex
        last_err = err
        logger.warning("Snapshot failed %s:%s via %s -> %s", ex, tk, ex, err)
        time.sleep(0.6)

    raise RuntimeError(f"All exchanges failed for {tk}. Last error: {last_err}. Tried: {tried}")

# ─────────────────────────────────────────────────────────
# Telegram send helpers
# ─────────────────────────────────────────────────────────
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

    try:
        png, ex_used = await context.application.run_in_executor(
            None,
            fetch_snapshot_png_any,
            exchange,
            ticker,
            interval,
            theme,
            "chart",
            alt_exchanges,
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
        # Telegram shows only first caption reliably
        if len(chunk) > 1:
            for m in chunk[1:]:
                m.caption = None
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(1.0)

# ─────────────────────────────────────────────────────────
# Command parsing
# ─────────────────────────────────────────────────────────
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
    # /trade SYMBOL CALL|PUT [expiry] [theme]
    if not args:
        return "EUR/USD", "CALL", "5m", DEFAULT_THEME
    symbol = args[0]
    direction = parse_direction(args[1] if len(args) >= 2 else None) or "CALL"
    expiry = args[2] if len(args) >= 3 else "5m"
    theme = args[3] if len(args) >= 4 else DEFAULT_THEME
    return symbol, direction, expiry, theme

# ─────────────────────────────────────────────────────────
# Bot command handlers
# ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nm = update.effective_user.first_name if update.effective_user else ""
    msg = (
        f"Hi {nm} 👋\n\n"
        "I'm your TradingView Snapshot Bot (Pocket Option / Binary mode).\n\n"
        "Try:\n"
        "/snap EUR/USD 5 dark\n"
        "/trade EUR/USD CALL 5m\n"
        "/snapmulti EUR/USD GBP/USD 15 light\n"
        "/snapall\n"
        "/pairs\n"
        "/help"
    )
    await context.bot.send_message(update.effective_chat.id, msg)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📘 *Help*\n\n"
        "*/snap* SYMBOL [interval] [theme]\n"
        "*/trade* SYMBOL CALL|PUT [expiry] [theme]\n"
        "*/snapmulti* S1 S2 ... [interval] [theme]\n"
        "*/snapall* (all FX+OTC)\n"
        "*/pairs* list supported names\n"
        "*/next* watch for next signal (from TV alerts)\n\n"
        "_Intervals:_ minutes (#), D, W, M.\n"
        "_Themes:_ dark|light.\n"
    )
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fx_lines  = "\n".join(f"• {p}" for p in FX_PAIRS)
    otc_lines = "\n".join(f"• {p}" for p in OTC_PAIRS)
    msg = (
        "📊 *FX Pairs*\n" + fx_lines + "\n\n" +
        "🕒 *OTC Pairs* (Pocket Option)\n" + otc_lines + "\n\n" +
        "Example: `/trade EUR/USD CALL 5m`\n"
    )
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN)

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
    media_items = await context.application.run_in_executor(None, build_media_items_sync, p_trip, tf, th, "[MULTI] ")
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
    media_items = await context.application.run_in_executor(None, build_media_items_sync, p_trip, DEFAULT_INTERVAL, DEFAULT_THEME, "[ALL] ")
    if not media_items:
        await context.bot.send_message(chat_id, "❌ No charts captured.")
        return
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)

async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol, direction, expiry, theme = parse_trade_args(context.args)
    ex, tk, _is_otc, alt = resolve_symbol(symbol)
    tf = norm_interval(DEFAULT_INTERVAL)  # chart timeframe = bot default
    th = norm_theme(theme)
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    msg = f"{arrow} *{symbol}* {direction}  Expiry: {expiry}"
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN)
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, th, prefix="[TRADE] ", alt_exchanges=alt)

async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        update.effective_chat.id,
        "👀 Watching for next signal…\nSend TradingView alerts to /tv (see README).",
    )

# quick natural-language /trade fallback
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

# ─────────────────────────────────────────────────────────
# Flask TradingView Webhook
# ─────────────────────────────────────────────────────────
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
    Process a TradingView alert payload synchronously (Flask worker thread).
    Accepts secret in header X-Webhook-Token OR JSON body .secret/.token
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

    try:
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

# compatibility alias for TradingView "webhook URL" older config
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

# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────
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
    tg_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    tg_app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info("Bot polling… (Default=%s) | Webhook port %s", DEFAULT_EXCHANGE, TV_WEBHOOK_PORT)
    tg_app.run_polling()

if __name__ == "__main__":
    main()
