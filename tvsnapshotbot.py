#!/usr/bin/env python
"""
TradingView Snapshot Telegram Bot - Webhook + Multi Snapshot Edition
====================================================================
Features:
• Default exchange = QUOTEX (switchable via env var).
• Screenshot backend: Node/Puppeteer (/run, /start-browser) on Render or local.
• /snap, /snapmulti, /snapall, /pairs, /start, /help bot commands.
• Rate limiting: per-chat + global throttle.
• Retry screenshot fetch (3 tries, 5s backoff).
• Rotating log file: logs/tvsnapshotbot.log
• TradingView webhook server (/tv, /webhook) -> Telegram message + chart.
• Webhook sends to Telegram via direct HTTP (thread-safe).
"""

import os
import io
import re
import time
import json
import queue
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
log_handler = RotatingFileHandler(
    "logs/tvsnapshotbot.log", maxBytes=5 * 1024 * 1024, backupCount=3
)
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[log_handler, logging.StreamHandler()],
)
logger = logging.getLogger("TVSnapBot")


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
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET")  # optional shared secret token

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
    """Block enough so we don't hammer Render too fast."""
    global GLOBAL_LAST_SNAPSHOT
    now = time.time()
    gap = now - GLOBAL_LAST_SNAPSHOT
    if gap < GLOBAL_MIN_GAP:
        time.sleep(GLOBAL_MIN_GAP - gap)
    GLOBAL_LAST_SNAPSHOT = time.time()


# ─────────────────────────────────────────────────────────
# Pair lists (exact names shown to user — no auto alias)
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


# Canonicalize keys
def _canon_key(pair: str) -> str:
    s = pair.strip().upper()
    s = s.replace(" ", "").replace("/", "")
    return s


# Pair map
PAIR_MAP: Dict[str, Tuple[str, str]] = {}  # canon -> (exchange, ticker)

# Map majors to Quotex
for p in FX_PAIRS:
    tk = p.replace("/", "")
    PAIR_MAP[_canon_key(p)] = (DEFAULT_EXCHANGE, tk)

# OTC underlying to base feed (still pull real market chart)
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
    "KES/USD-OTC": "USDKES",  # invert underlying
    "MAD/USD-OTC": "USDMAD",
    "USD/BDT-OTC": "USDBDT",
    "USD/MXN-OTC": "USDMXN",
    "USD/MYR-OTC": "USDMYR",
    "USD/PKR-OTC": "USDPKR",
}
for p, tk in _underlying_otc.items():
    PAIR_MAP[_canon_key(p)] = (DEFAULT_EXCHANGE, tk)


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
    if t in ("m", "1m", "mo", "month"):
        return "M"
    if t.isdigit():
        return t
    return DEFAULT_INTERVAL


def norm_theme(val: str) -> str:
    if not val:
        return DEFAULT_THEME
    return "light" if val.lower().startswith("l") else "dark"


# ─────────────────────────────────────────────────────────
# Resolve raw symbol -> (exchange, ticker, is_otc)
# Accepts: "EUR/USD", "EUR/USD-OTC", "QUOTEX:EURUSD", "EURUSD" etc.
# ─────────────────────────────────────────────────────────
def resolve_symbol(raw: str) -> Tuple[str, str, bool]:
    if not raw:
        return DEFAULT_EXCHANGE, "EURUSD", False

    s = raw.strip().upper()
    if ":" in s:  # explicit EX:TK
        ex, tk = s.split(":", 1)
        return ex, tk, s.endswith("-OTC")

    key = _canon_key(s)
    if key in PAIR_MAP:
        ex, tk = PAIR_MAP[key]
        return ex, tk, "-OTC" in raw.upper()

    # fallback guess: raw uppercase no slash
    tk = re.sub(r"[^A-Z0-9]", "", s)
    return DEFAULT_EXCHANGE, tk, "-OTC" in raw.upper()


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
# Telegram send helpers (async for bot handlers)
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
    # launch browser in background (non-blocking)
    await asyncio.to_thread(node_start_browser)

    try:
        png = await asyncio.to_thread(fetch_snapshot_png_retry, exchange, ticker, interval, theme)
        caption = f"{prefix}{exchange}:{ticker} • TF {interval} • {theme}"
        await context.bot.send_photo(chat_id=chat_id, photo=png, caption=caption)
    except Exception as e:
        logger.exception("snapshot photo error")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Failed: {exchange}:{ticker} ({e})")


# Build media items in threadpool for /snapmulti, /snapall
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
    # chunk & send
    for i in range(0, len(media_items), chunk_size):
        chunk = media_items[i:i+chunk_size]
        if not chunk:
            continue
        # only first gets caption
        if len(chunk) > 1:
            first_cap = chunk[0].caption
            for m in chunk[1:]:
                m.caption = None
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(1.0)


# ─────────────────────────────────────────────────────────
# Command argument parsing
# ─────────────────────────────────────────────────────────
def parse_snap_args(args: List[str]) -> Tuple[str, str, str, str]:
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
    ex, tk, _ = resolve_symbol(symbol)
    return ex, tk, norm_interval(tf), norm_theme(th)


def parse_multi_args(args: List[str]) -> Tuple[List[str], str, str]:
    # /snapmulti S1 S2 ... [interval] [theme]
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
# Bot command handlers
# ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nm = update.effective_user.first_name if update.effective_user else ""
    msg = (
        f"Hi {nm} 👋\n\n"
        "I grab TradingView charts (Quotex feed).\n\n"
        "Try:\n"
        "/snap EUR/USD 5 dark\n"
        "/snapmulti EUR/USD GBP/USD 15 light\n"
        "/snapall\n"
        "/pairs\n\n"
        "TradingView alerts can hit me at /tv.\n"
    )
    await context.bot.send_message(update.effective_chat.id, msg)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📘 *Help*\n\n"
        "/snap SYMBOL [interval] [theme]\n"
        "/snapmulti S1 S2 ... [interval] [theme]\n"
        "/snapall (FX+OTC chunked)\n"
        "/pairs list supported pairs\n\n"
        "Intervals: number=minutes, D=day, W=week.\n"
        "Themes: dark|light.\n"
    )
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode="Markdown")


async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["📊 FX Pairs:"] + [f"• {p}" for p in FX_PAIRS]
    lines += ["", "🕒 OTC Pairs:"] + [f"• {p}" for p in OTC_PAIRS]
    await context.bot.send_message(update.effective_chat.id, "\n".join(lines))


async def cmd_snap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ex, tk, tf, th = parse_snap_args(context.args)
    await send_snapshot_photo(update.effective_chat.id, context, ex, tk, tf, th)


async def cmd_snapmulti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs, tf, th = parse_multi_args(context.args)
    if not pairs:
        await context.bot.send_message(update.effective_chat.id, "Usage: /snapmulti SYM1 SYM2 ... [interval] [theme]")
        return

    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, f"📸 Capturing {len(pairs)} charts…")

    # build list of (ex,tk,label)
    p_trip: List[Tuple[str, str, str]] = []
    for p in pairs:
        ex, tk, _ = resolve_symbol(p)
        p_trip.append((ex, tk, p))

    media_items = await asyncio.to_thread(build_media_items_sync, p_trip, tf, th, prefix="[MULTI] ")
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)


async def cmd_snapall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, f"⚡ Capturing all {len(ALL_PAIRS)} pairs… this may take a while.")

    p_trip: List[Tuple[str, str, str]] = []
    for p in ALL_PAIRS:
        ex, tk, _ = resolve_symbol(p)
        p_trip.append((ex, tk, p))

    media_items = await asyncio.to_thread(build_media_items_sync, p_trip, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[ALL] ")
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)


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


def tg_api_send_message(chat_id: str, text: str, parse_mode: Optional[str] = None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        _http.post(url, json=payload, timeout=30)
    except Exception as e:
        logger.error("tg_api_send_message error: %s", e)


def tg_api_send_photo_bytes(chat_id: str, png: bytes, caption: str = ""):
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", png, "image/png")}
    data = {"chat_id": chat_id, "caption": caption}
    try:
        _http.post(url, data=data, files=files, timeout=60)
    except Exception as e:
        logger.error("tg_api_send_photo_bytes error: %s", e)


def _parse_tv_payload(data: dict) -> Dict[str, str]:
    """
    Normalize a TradingView JSON alert payload into standard fields.
    Accept keys: pair, symbol, ticker; direction; expiry; strategy; winrate; timeframe; theme; chat_id.
    """
    d = {}
    d["chat_id"]   = str(data.get("chat_id") or DEFAULT_CHAT_ID)
    d["pair"]      = str(data.get("pair") or data.get("symbol") or data.get("ticker") or "EURUSD")
    d["direction"] = str(data.get("direction") or "CALL").upper()
    d["expiry"]    = str(data.get("expiry") or "")
    d["strategy"]  = str(data.get("strategy") or "")
    d["winrate"]   = str(data.get("winrate") or "")
    d["timeframe"] = str(data.get("timeframe") or data.get("tf") or DEFAULT_INTERVAL)
    d["theme"]     = str(data.get("theme") or DEFAULT_THEME)
    return d


def _handle_tv_alert(data: dict):
    """
    Process a TradingView alert payload synchronously (Flask thread).
    """
    if WEBHOOK_SECRET:
        hdr = request.headers.get("X-Webhook-Token", "")
        if hdr != WEBHOOK_SECRET:
            logger.warning("Webhook secret mismatch.")
            return {"ok": False, "error": "unauthorized"}, 403

    payload = _parse_tv_payload(data)
    logger.info("TV payload normalized: %s", payload)

    chat_id   = payload["chat_id"]
    raw_pair  = payload["pair"]
    direction = payload["direction"]
    expiry    = payload["expiry"]
    strat     = payload["strategy"]
    winrate   = payload["winrate"]
    tf        = norm_interval(payload["timeframe"])
    theme     = norm_theme(payload["theme"])

    ex, tk, _ = resolve_symbol(raw_pair)

    # send text first
    msg = (
        f"🔔 *TradingView Alert*\n"
        f"Pair: {raw_pair}\n"
        f"Direction: {direction}\n"
        f"Expiry: {expiry}\n"
        f"Strategy: {strat}\n"
        f"Win Rate: {winrate}\n"
        f"TF: {tf} • Theme: {theme}"
    )
    tg_api_send_message(chat_id, msg, parse_mode="Markdown")

    # try screenshot
    try:
        node_start_browser()
        png = fetch_snapshot_png_retry(ex, tk, tf, theme)
        tg_api_send_photo_bytes(chat_id, png, caption=f"{ex}:{tk} • TF {tf} • {theme}")
    except Exception as e:
        logger.error("TV snapshot error: %s", e)
        tg_api_send_message(chat_id, f"⚠ Chart snapshot failed for {raw_pair}: {e}")

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
    # alias
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
    application.add_handler(CommandHandler("snap",      cmd_snap))
    application.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    application.add_handler(CommandHandler("snapall",   cmd_snapall))

    # Fallbacks
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info("Bot polling… (Quotex default)  |  Webhook on port %s", TV_WEBHOOK_PORT)
    # NOTE: This call blocks and manages its own event loop. Do NOT wrap in asyncio.run().
    application.run_polling()


if __name__ == "__main__":
    main()
