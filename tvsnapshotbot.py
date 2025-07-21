#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
===============================================================================
TradingView → Telegram Snapshot Bot  (FX + OTC + Pocket Option helper)
===============================================================================

Capabilities
------------
• Screenshot backend: Node/Puppeteer service (/run?exchange=...&ticker=...)
  - Hosted on Render (recommended) or local dev / ngrok tunnel.
• Robust exchange fallback: If chart fails for your preferred exchange symbol,
  bot automatically retries alternate feeds (FX, OANDA, FOREXCOM, etc.).
• FX + OTC pair tables; OTC resolves to an underlying real market feed.
• Commands:
    /start         Quick intro.
    /help          Command usage & formatting hints.
    /pairs         List supported FX & OTC names (copy/paste friendly).
    /snap          Single chart snapshot.
    /trade         Symbol + CALL/PUT + expiry (binary style) + optional theme.
    /snapmulti     Multiple symbols in one media album (chunks of 5).
    /snapall       Bulk snapshot (all configured FX + OTC pairs).
    /next          Placeholder "watch next signal" (future automation).
    /check         Debug: try all exchanges for a symbol; report success/fail.
    /config        Show current environment config loaded by bot.
• TradingView webhook endpoint:
    POST /tv       Accepts JSON (from Pine alert() or manual POST) -> Telegram
                   text + snapshot attempt (with fallback).
    POST /webhook  Alias for older configs.
• Rate limiting & global throttle (avoid hammering Render free tier).
• Rotating log file: logs/tvsnapshotbot.log   (5MB x 3 backups).
• Minimal external dependencies: requests, flask, python-telegram-bot v20+.

Installation Quick Notes
------------------------
pip install:
    python-telegram-bot~=20.8
    flask~=3.0
    requests~=2.32

Set environment variables before launching (PowerShell example):

    $env:TELEGRAM_BOT_TOKEN="123456789:ABCDEF..."
    $env:TELEGRAM_CHAT_ID="6337160812"  # default fallback chat
    $env:SNAPSHOT_BASE_URL="https://your-render-service.onrender.com"
    $env:TV_WEBHOOK_PORT="8081"
    # optional secret:
    # $env:WEBHOOK_SECRET="mysupersecret"

Then run:

    python tvsnapshotbot.py

TradingView Alert JSON Example
------------------------------
In your Pine Script alert message (Any alert() function call):

    {
      "chat_id": "6337160812",
      "pair": "{{ticker}}",
      "direction": "CALL",     // or PUT
      "expiry": "5m",
      "strategy": "AM_SNR",
      "timeframe": "{{interval}}",
      "theme": "dark"
      // "secret": "mysupersecret"  <-- If you set WEBHOOK_SECRET
    }

Webhook Target URL (TradingView alert dialog → Webhook URL field):

    https://YOUR_NGROK_OR_RENDER/tv

Security
--------
If you set WEBHOOK_SECRET in your environment, the bot will require either:
  • HTTP header: X-Webhook-Token: <secret>
  • or JSON body key: "secret": "<secret>" (good for TradingView which
    cannot always set custom headers).

===============================================================================
"""

# ===========================================================================
# Standard Library Imports
# ===========================================================================
import os
import io
import re
import time
import json
import threading
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from typing import List, Tuple, Dict, Optional

# ===========================================================================
# Third-Party Imports
# ===========================================================================
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

# ===========================================================================
# Logging Setup
# ===========================================================================
os.makedirs("logs", exist_ok=True)
LOG_PATH = "logs/tvsnapshotbot.log"
_log_handler = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[_log_handler, logging.StreamHandler()],
)
logger = logging.getLogger("TVSnapBot")

# ===========================================================================
# Global Configuration (Environment Driven)
# ===========================================================================
TOKEN            = os.environ.get("TELEGRAM_BOT_TOKEN")           # REQUIRED
DEFAULT_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")         # fallback if none in webhook
BASE_URL         = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000")
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "CURRENCY") # your preferred feed
DEFAULT_INTERVAL = os.environ.get("DEFAULT_INTERVAL", "1")        # minutes unless D/W/M
DEFAULT_THEME    = os.environ.get("DEFAULT_THEME", "dark")
TV_WEBHOOK_PORT  = int(os.environ.get("TV_WEBHOOK_PORT", "8081"))
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET")  # optional shared secret

# Parse optional CSV fallback override (e.g. "FX,FX_IDC,OANDA")
_env_fallbacks = os.environ.get("EXCHANGE_FALLBACKS_CSV")
if _env_fallbacks:
    EXCHANGE_FALLBACKS = [x.strip().upper() for x in _env_fallbacks.split(",") if x.strip()]
else:
    EXCHANGE_FALLBACKS = ["FX", "FX_IDC", "OANDA", "FOREXCOM", "IDC", "CURRENCY", "QUOTEX"]

# Secondary known group used when building deep fallback lists
KNOWN_FX_EXCHANGES = ["FX", "FX_IDC", "OANDA", "FOREXCOM", "IDC"]

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in environment.")

# Single global HTTP session
_http = requests.Session()


# ===========================================================================
# Rate Limiting
# ===========================================================================
LAST_SNAPSHOT_PER_CHAT: Dict[int, float] = {}
RATE_LIMIT_SECONDS = 3          # minimum time between snapshots per chat
GLOBAL_LAST_SNAPSHOT = 0.0
GLOBAL_MIN_GAP = 0.75           # min gap between ANY snapshots (across all chats)

def rate_limited(chat_id: int) -> bool:
    """Return True if the chat should be rate limited."""
    now = time.time()
    last = LAST_SNAPSHOT_PER_CHAT.get(chat_id, 0.0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    LAST_SNAPSHOT_PER_CHAT[chat_id] = now
    return False

def global_throttle_wait():
    """Global sleep to avoid hammering your Render free tier."""
    global GLOBAL_LAST_SNAPSHOT
    now = time.time()
    gap = now - GLOBAL_LAST_SNAPSHOT
    if gap < GLOBAL_MIN_GAP:
        time.sleep(GLOBAL_MIN_GAP - gap)
    GLOBAL_LAST_SNAPSHOT = time.time()


# ===========================================================================
# Pair Tables (exact display names — copy/paste friendly)
# ===========================================================================
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

ALL_PAIRS = FX_PAIRS + OTC_PAIRS

def _canon_key(pair: str) -> str:
    return pair.strip().upper().replace(" ", "").replace("/", "")

# Map canonical pair → (exchange, ticker)
PAIR_MAP: Dict[str, Tuple[str, str]] = {}

# Majors → CURRENCY (or your DEFAULT_EXCHANGE, but we stand up CURRENCY since you requested)
for p in FX_PAIRS:
    PAIR_MAP[_canon_key(p)] = ("CURRENCY", p.replace("/", ""))

# OTC pairs map to a "real world" underlying feed (QUOTEX tag used as label for debugging)
_underlying_otc: Dict[str, str] = {
    "EUR/USD-OTC":"EURUSD","GBP/USD-OTC":"GBPUSD","USD/JPY-OTC":"USDJPY",
    "USD/CHF-OTC":"USDCHF","AUD/USD-OTC":"AUDUSD","NZD/USD-OTC":"NZDUSD",
    "USD/CAD-OTC":"USDCAD","EUR/GBP-OTC":"EURGBP","EUR/JPY-OTC":"EURJPY",
    "GBP/JPY-OTC":"GBPJPY","AUD/CHF-OTC":"AUDCHF","EUR/CHF-OTC":"EURCHF",
    "KES/USD-OTC":"USDKES","MAD/USD-OTC":"USDMAD","USD/BDT-OTC":"USDBDT",
    "USD/MXN-OTC":"USDMXN","USD/MYR-OTC":"USDMYR","USD/PKR-OTC":"USDPKR",
}
for p, tk in _underlying_otc.items():
    # Use QUOTEX tag here so you know the pair came from OTC list
    PAIR_MAP[_canon_key(p)] = ("QUOTEX", tk)


# ===========================================================================
# Interval & Theme Normalization Helpers
# ===========================================================================
def norm_interval(tf: str) -> str:
    """
    Convert user/timeframe strings into TradingView chart param.
    Return minutes when numeric; else D/W/M tokens.
    """
    if not tf:
        return DEFAULT_INTERVAL
    t = tf.strip().lower()

    if t.endswith("m") and t[:-1].isdigit():   # 5m -> 5
        return t[:-1]

    if t.endswith("h") and t[:-1].isdigit():   # 1h -> 60
        return str(int(t[:-1]) * 60)

    if t in ("d","1d","day"):   return "D"
    if t in ("w","1w","week"):  return "W"
    if t in ("mo","mth","1m","month"): return "M"

    if t.isdigit():             return t  # already numeric minutes

    return DEFAULT_INTERVAL


def norm_theme(val: str) -> str:
    """Return 'light' or 'dark'."""
    return "light" if (val and val.lower().startswith("l")) else "dark"


# ===========================================================================
# Direction Parsing (binary friendly)
# ===========================================================================
_CALL_WORDS = {"CALL","BUY","UP","LONG","BULL","GREEN"}
_PUT_WORDS  = {"PUT","SELL","DOWN","SHORT","BEAR","RED"}

def parse_direction(word: Optional[str]) -> Optional[str]:
    if not word:
        return None
    w = word.strip().upper()
    if w in _CALL_WORDS: return "CALL"
    if w in _PUT_WORDS:  return "PUT"
    return None


# ===========================================================================
# Symbol Resolution
# ===========================================================================
def resolve_symbol(raw: str) -> Tuple[str, str, bool, List[str]]:
    """
    Return (exchange, ticker, is_otc, alt_exchanges).
    alt_exchanges = global fallback list (EXCHANGE_FALLBACKS).
    """
    alt = EXCHANGE_FALLBACKS

    if not raw:
        return DEFAULT_EXCHANGE, "EURUSD", False, alt

    s = raw.strip().upper()
    is_otc = "-OTC" in s

    # If user explicitly typed EX:TK (e.g., OANDA:EURUSD) trust it
    if ":" in s:
        ex, tk = s.split(":", 1)
        return ex, tk, is_otc, alt

    # Try canonical map
    key = _canon_key(s)
    if key in PAIR_MAP:
        ex, tk = PAIR_MAP[key]
        return ex, tk, is_otc, alt

    # Fallback: raw cleaned
    tk = re.sub(r"[^A-Z0-9]", "", s)
    return DEFAULT_EXCHANGE, tk, is_otc, alt


# ===========================================================================
# Screenshot Backend Helpers
# ===========================================================================
def node_start_browser():
    """
    Ping Node service to ensure headless Chromium is warm.
    Nonfatal.
    """
    try:
        _http.get(f"{BASE_URL}/start-browser", timeout=10)
    except Exception as e:
        logger.warning("start-browser failed: %s", e)


def _attempt_snapshot_url(ex: str, tk: str, interval: str, theme: str, base: str):
    """
    Single synchronous attempt to fetch PNG bytes.
    Returns (ok, png_or_None, errstr).
    """
    try:
        global_throttle_wait()
        url = f"{BASE_URL}/run?base={base}&exchange={ex}&ticker={tk}&interval={interval}&theme={theme}"
        r = _http.get(url, timeout=75)
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
    base: str = "chart",
    extra_exchanges: Optional[List[str]] = None,
) -> Tuple[bytes, str]:
    """
    Try multiple exchanges until a chart loads.

    Order:
      - primary_ex
      - extra_exchanges (if passed, e.g., from resolve_symbol)
      - EXCHANGE_FALLBACKS (env / default)
      - KNOWN_FX_EXCHANGES (safety duplicates pruned)
    """
    tried: List[str] = []
    last_err: Optional[str] = None

    merged: List[str] = [primary_ex.upper()]
    if extra_exchanges:
        merged.extend([x.upper() for x in extra_exchanges])
    merged.extend(EXCHANGE_FALLBACKS + KNOWN_FX_EXCHANGES)

    # dedupe w/ order preserved
    dedup: List[str] = []
    seen = set()
    for x in merged:
        if x not in seen:
            dedup.append(x)
            seen.add(x)

    for ex in dedup:
        tried.append(ex)
        ok, png, err = _attempt_snapshot_url(ex, tk, interval, theme, base)
        if ok and png:
            logger.info("Snapshot success %s:%s via %s (%d bytes)", ex, tk, ex, len(png))
            return png, ex
        last_err = err
        logger.warning("Snapshot failed %s:%s via %s -> %s", ex, tk, ex, err)
        time.sleep(1.5)  # gentle backoff

    raise RuntimeError(f"All exchanges failed for {tk}. Last error: {last_err}. Tried: {tried}")


# ===========================================================================
# Telegram Send Helpers (async for PTB)
# ===========================================================================
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
    """
    Resolve and send a single snapshot photo to Telegram.
    """
    if rate_limited(chat_id):
        await context.bot.send_message(chat_id, "⏳ Too many requests; wait a few seconds…")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)

    # warm browser in thread
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
    """
    Build InputMediaPhoto list synchronously (for to_thread).
    Each item: (exchange, ticker, label, alt_exchanges)
    """
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
    """
    Telegram limits: max 10 per album, but we chunk to 5 for safety + spacing.
    Only the first item’s caption will show when chunk > 1 (Telegram behavior).
    """
    for i in range(0, len(media_items), chunk_size):
        chunk = media_items[i : i + chunk_size]
        if not chunk:
            continue
        if len(chunk) > 1:
            # Only keep caption on first media so we don't spam repeated lines
            for m in chunk[1:]:
                m.caption = None
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(1.0)


# ===========================================================================
# Command Argument Parsing Helpers
# ===========================================================================
def parse_snap_args(args: List[str]) -> Tuple[str, str, str, str, List[str]]:
    """
    /snap SYMBOL [interval] [theme]

    Examples:
      /snap EUR/USD
      /snap EUR/USD 5
      /snap EUR/USD 5 light
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
    return ex, tk, norm_interval(tf), norm_theme(th), alt


def parse_multi_args(args: List[str]) -> Tuple[List[str], str, str]:
    """
    /snapmulti S1 S2 ... [interval] [theme]
    Last numeric = interval, last dark/light = theme
    """
    if not args:
        return [], DEFAULT_INTERVAL, DEFAULT_THEME

    theme = DEFAULT_THEME
    if args[-1].lower() in ("dark", "light"):
        theme = args[-1].lower()
        args = args[:-1]

    tf = DEFAULT_INTERVAL
    if args and re.fullmatch(r"\d+", args[-1]):  # numeric only
        tf = args[-1]
        args = args[:-1]

    return args, norm_interval(tf), norm_theme(theme)


def parse_trade_args(args: List[str]) -> Tuple[str, str, str, str]:
    """
    /trade SYMBOL CALL|PUT [expiry] [theme]
    Expiry is passed through to chat only (e.g., "5m").
    """
    if not args:
        return "EUR/USD", "CALL", "5m", DEFAULT_THEME

    symbol = args[0]
    direction = parse_direction(args[1] if len(args) >= 2 else None) or "CALL"
    expiry = args[2] if len(args) >= 3 else "5m"
    theme = args[3] if len(args) >= 4 else DEFAULT_THEME
    return symbol, direction, expiry, theme


# ===========================================================================
# Telegram Command Handlers
# ===========================================================================
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
        "*/check* SYMBOL [interval]   (debug which exchange works)\n"
        "*/config* show current settings\n"
        "*/next* watch for next signal (placeholder)\n\n"
        "_Intervals:_ minutes (#), D=day, W=week, M=month.\n"
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

    media_items = await asyncio.to_thread(build_media_items_sync, p_trip, tf, th, prefix="[MULTI] ")
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

    media_items = await asyncio.to_thread(build_media_items_sync, p_trip, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[ALL] ")
    if not media_items:
        await context.bot.send_message(chat_id, "❌ No charts captured.")
        return
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)


async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /trade SYMBOL CALL|PUT [expiry] [theme]
    symbol, direction, expiry, theme = parse_trade_args(context.args)
    ex, tk, _is_otc, alt = resolve_symbol(symbol)
    tf = norm_interval(DEFAULT_INTERVAL)  # chart timeframe uses bot default
    th = norm_theme(theme)
    arrow = "🟢↑" if direction == "CALL" else "🔴↓"
    msg = f"{arrow} *{symbol}* {direction}  Expiry: {expiry}  (Pocket Option)"
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.MARKDOWN)
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, th, prefix="[TRADE] ", alt_exchanges=alt)


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        update.effective_chat.id,
        "👀 Watching for next signal (placeholder). Connect TradingView alerts to /tv webhook.",
    )


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "⚙ <b>Bot Configuration</b>\n"
        f"token set: {'yes' if TOKEN else 'no'}\n"
        f"default chat: {DEFAULT_CHAT_ID or '(none)'}\n"
        f"snapshot base: {BASE_URL}\n"
        f"default exchange: {DEFAULT_EXCHANGE}\n"
        f"default interval: {DEFAULT_INTERVAL}\n"
        f"default theme: {DEFAULT_THEME}\n"
        f"webhook port: {TV_WEBHOOK_PORT}\n"
        f"secret set: {'yes' if WEBHOOK_SECRET else 'no'}\n"
        f"fallbacks: {', '.join(EXCHANGE_FALLBACKS)}\n"
    )
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.HTML)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Debug: /check SYMBOL [interval]
    Probes all fallback exchanges and reports success/fail.
    """
    if not context.args:
        await context.bot.send_message(update.effective_chat.id, "Usage: /check SYMBOL [interval]")
        return

    symbol = context.args[0]
    tf = norm_interval(context.args[1] if len(context.args) >= 2 else DEFAULT_INTERVAL)
    theme = DEFAULT_THEME

    ex, tk, _is_otc, alt = resolve_symbol(symbol)

    await context.bot.send_message(update.effective_chat.id, f"🔍 Checking exchanges for {symbol} ({tk}) TF={tf}…")

    results_lines = []
    found_png: Optional[bytes] = None
    found_ex: Optional[str] = None

    # Build probe list: primary + alt + fallback extras
    probe_list = [ex] + alt + EXCHANGE_FALLBACKS + KNOWN_FX_EXCHANGES
    probe_list = list(dict.fromkeys([p.upper() for p in probe_list]))

    for exch in probe_list:
        ok, png, err = _attempt_snapshot_url(exch, tk, tf, theme, "chart")
        if ok and png:
            results_lines.append(f"✅ {exch}:{tk}")
            if found_png is None:
                found_png = png
                found_ex = exch
        else:
            results_lines.append(f"❌ {exch}:{tk} ({err})")
        time.sleep(0.25)

    await context.bot.send_message(update.effective_chat.id, "\n".join(results_lines))

    if found_png is not None:
        await context.bot.send_photo(update.effective_chat.id, photo=found_png,
                                     caption=f"[CHECK] {found_ex}:{tk} • TF {tf} • {theme}")
    else:
        await context.bot.send_message(update.effective_chat.id, "No working exchange found.")


# ===========================================================================
# Non-command text handler (quick trade parser)
# ===========================================================================
_trade_re = re.compile(r"(?i)trade\s+([A-Z/\-]+)\s+(call|put|buy|sell|up|down)\s+([0-9]+m?)")

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
            update.effective_chat.id, context,
            ex, tk, DEFAULT_INTERVAL, DEFAULT_THEME,
            prefix="[TRADE] ", alt_exchanges=alt
        )
        return

    await context.bot.send_message(update.effective_chat.id, f"You said: {txt}\nTry /trade EUR/USD CALL 5m")


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, "❌ Unknown command. Try /help.")


# ===========================================================================
# TradingView Webhook (Flask)
# ===========================================================================
flask_app = Flask(__name__)

def _parse_tv_payload(data: dict) -> Dict[str, str]:
    """
    Accept common keys & return normalized dict of strings.
    """
    d: Dict[str, str] = {}
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
    """
    Direct sync Telegram send (used from Flask thread).
    """
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        _http.post(url, json=payload, timeout=30)
    except Exception as e:
        logger.error("tg_api_send_message: %s", e)


def tg_api_send_photo_bytes(chat_id: str, png: bytes, caption: str = ""):
    """
    Direct sync Telegram photo send (used from Flask thread).
    """
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
    Supports optional WEBHOOK_SECRET via header (X-Webhook-Token) OR JSON body key "secret".
    """
    # --- Security gate ------------------------------------------------------
    if WEBHOOK_SECRET:
        hdr = request.headers.get("X-Webhook-Token", "")
        body_secret = str(data.get("secret") or data.get("token") or "")
        if hdr != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET:
            logger.warning("Webhook secret mismatch; rejecting.")
            return {"ok": False, "error": "unauthorized"}, 403

    # --- Normalize ----------------------------------------------------------
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

    # --- Attempt chart snapshot ---------------------------------------------
    try:
        node_start_browser()
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


# Backward compatible alias
@flask_app.route("/webhook", methods=["POST"])
def tv_route_alias():
    return tv_route()


def start_flask_background():
    """
    Run Flask in a background daemon thread so PTB polling can run in the main thread.
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


# ===========================================================================
# Main Entry
# ===========================================================================
def main():
    # Start webhook server
    start_flask_background()

    # Build Telegram application
    tg_app = ApplicationBuilder().token(TOKEN).build()

    # Command handlers
    tg_app.add_handler(CommandHandler("start",     cmd_start))
    tg_app.add_handler(CommandHandler("help",      cmd_help))
    tg_app.add_handler(CommandHandler("pairs",     cmd_pairs))
    tg_app.add_handler(CommandHandler("snap",      cmd_snap))
    tg_app.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    tg_app.add_handler(CommandHandler("snapall",   cmd_snapall))
    tg_app.add_handler(CommandHandler("trade",     cmd_trade))
    tg_app.add_handler(CommandHandler("next",      cmd_next))
    tg_app.add_handler(CommandHandler("config",    cmd_config))
    tg_app.add_handler(CommandHandler("check",     cmd_check))

    # Fallback handlers
    tg_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    tg_app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info("Bot polling… (Default=%s) | Webhook port %s", DEFAULT_EXCHANGE, TV_WEBHOOK_PORT)
    tg_app.run_polling()


if __name__ == "__main__":
    main()

# End of file
# vim: set ts=4 sw=4 et fileencoding=utf-8:
