#!/usr/bin/env python
"""
TradingView → Telegram Snapshot Bot
-----------------------------------
- QUOTEX default exchange (override via env)
- Node/Puppeteer screenshot backend (/run) hosted on Render or ngrok
- /snap, /snapmulti, /snapall, /pairs, /help
- Flask /tv webhook for TradingView alert JSON (from Pine alert())
- Rate limiting + retry + rotating logs
"""

import os, io, re, time, json, threading, asyncio, logging
from logging.handlers import RotatingFileHandler
from typing import List, Tuple, Dict, Optional

import requests
from flask import Flask, request, jsonify

from telegram import Update, InputMediaPhoto
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
os.makedirs("logs", exist_ok=True)
_log_file = "logs/tvsnapshotbot.log"
log_handler = RotatingFileHandler(_log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[log_handler, logging.StreamHandler()],
)
logger = logging.getLogger("TVSnapBot")

# ------------------------------------------------------------------
# Env & Config
# ------------------------------------------------------------------
TOKEN            = os.environ.get("TELEGRAM_BOT_TOKEN")
DEFAULT_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
BASE_URL         = os.environ.get("SNAPSHOT_BASE_URL", "http://localhost:10000")
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "QUOTEX")
DEFAULT_INTERVAL = os.environ.get("DEFAULT_INTERVAL", "1")
DEFAULT_THEME    = os.environ.get("DEFAULT_THEME", "dark")
TV_WEBHOOK_PORT  = int(os.environ.get("TV_WEBHOOK_PORT", "8081"))
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET")  # optional

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set.")

_http = requests.Session()

# ------------------------------------------------------------------
# Rate limiting
# ------------------------------------------------------------------
LAST_SNAPSHOT_PER_CHAT: Dict[int, float] = {}
RATE_LIMIT_SECONDS = 3
GLOBAL_LAST_SNAPSHOT = 0.0
GLOBAL_MIN_GAP = 0.75  # seconds

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

# ------------------------------------------------------------------
# Pair Lists (shown exactly as typed)
# ------------------------------------------------------------------
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

def _canon_key(pair: str) -> str:
    return pair.strip().upper().replace(" ", "").replace("/", "")

PAIR_MAP: Dict[str, Tuple[str,str]] = {}
# majors map to DEFAULT_EXCHANGE
for p in FX_PAIRS:
    PAIR_MAP[_canon_key(p)] = (DEFAULT_EXCHANGE, p.replace("/", ""))

_underlying_otc = {
    "EUR/USD-OTC":"EURUSD","GBP/USD-OTC":"GBPUSD","USD/JPY-OTC":"USDJPY",
    "USD/CHF-OTC":"USDCHF","AUD/USD-OTC":"AUDUSD","NZD/USD-OTC":"NZDUSD",
    "USD/CAD-OTC":"USDCAD","EUR/GBP-OTC":"EURGBP","EUR/JPY-OTC":"EURJPY",
    "GBP/JPY-OTC":"GBPJPY","AUD/CHF-OTC":"AUDCHF","EUR/CHF-OTC":"EURCHF",
    "KES/USD-OTC":"USDKES","MAD/USD-OTC":"USDMAD","USD/BDT-OTC":"USDBDT",
    "USD/MXN-OTC":"USDMXN","USD/MYR-OTC":"USDMYR","USD/PKR-OTC":"USDPKR",
}
for p, tk in _underlying_otc.items():
    PAIR_MAP[_canon_key(p)] = (DEFAULT_EXCHANGE, tk)

# ------------------------------------------------------------------
# Interval & Theme Normalization
# ------------------------------------------------------------------
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
    if t in ("m","1m","mo","month"):
        return "M"
    if t.isdigit():
        return t
    return DEFAULT_INTERVAL

def norm_theme(val: str) -> str:
    return "light" if (val and val.lower().startswith("l")) else "dark"

# ------------------------------------------------------------------
# Resolve symbol -> (exchange, ticker, is_otc)
# ------------------------------------------------------------------
def resolve_symbol(raw: str) -> Tuple[str,str,bool]:
    if not raw:
        return DEFAULT_EXCHANGE, "EURUSD", False
    s = raw.strip().upper()
    is_otc = "-OTC" in s
    if ":" in s:
        ex, tk = s.split(":",1)
        return ex, tk, is_otc
    key = _canon_key(s)
    if key in PAIR_MAP:
        ex, tk = PAIR_MAP[key]
        return ex, tk, is_otc
    tk = re.sub(r"[^A-Z0-9]","",s)
    return DEFAULT_EXCHANGE, tk, is_otc

# ------------------------------------------------------------------
# Screenshot Backend Helpers
# ------------------------------------------------------------------
def node_start_browser():
    try:
        r = _http.get(f"{BASE_URL}/start-browser", timeout=10)
        logger.debug("start-browser %s %s", r.status_code, r.text[:100])
    except Exception as e:
        logger.warning("start-browser failed: %s", e)

def fetch_snapshot_png_retry(ex: str, tk: str, interval: str, theme: str) -> bytes:
    last_err = None
    for attempt in range(1,4):
        try:
            global_throttle_wait()
            url = f"{BASE_URL}/run?exchange={ex}&ticker={tk}&interval={interval}&theme={theme}"
            r = _http.get(url, timeout=75)
            if r.status_code == 200 and r.headers.get("Content-Type","").startswith("image"):
                return r.content
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            last_err = str(e)
        logger.warning("Snapshot %s:%s attempt %d failed: %s", ex, tk, attempt, last_err)
        time.sleep(5)
    raise RuntimeError(f"Failed after retries: {last_err}")

# ------------------------------------------------------------------
# Telegram Send Helpers (async)
# ------------------------------------------------------------------
async def send_snapshot_photo(chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                              exchange: str, ticker: str, interval: str, theme: str, prefix: str=""):
    if rate_limited(chat_id):
        await context.bot.send_message(chat_id, "⏳ Too many requests; wait a few seconds…")
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

def build_media_items_sync(pairs: List[Tuple[str,str,str]], interval: str, theme: str, prefix: str) -> List[InputMediaPhoto]:
    out: List[InputMediaPhoto] = []
    for ex, tk, lab in pairs:
        try:
            png = fetch_snapshot_png_retry(ex, tk, interval, theme)
            bio = io.BytesIO(png); bio.name = "chart.png"
            cap = f"{prefix}{ex}:{tk} • {lab} • TF {interval} • {theme}"
            out.append(InputMediaPhoto(media=bio, caption=cap))
        except Exception as e:
            logger.warning("Media build fail %s:%s %s", ex, tk, e)
    return out

async def send_media_group_chunked(chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                                   media_items: List[InputMediaPhoto], chunk_size: int=5):
    for i in range(0, len(media_items), chunk_size):
        chunk = media_items[i:i+chunk_size]
        if not chunk: 
            continue
        if len(chunk) > 1:
            first_cap = chunk[0].caption
            for m in chunk[1:]:
                m.caption = None
        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
        await asyncio.sleep(1.0)

# ------------------------------------------------------------------
# Command Parsing
# ------------------------------------------------------------------
def parse_snap_args(args: List[str]) -> Tuple[str,str,str,str]:
    symbol = args[0] if args else "EUR/USD"
    tf = DEFAULT_INTERVAL
    th = DEFAULT_THEME
    if len(args) >= 2 and args[1].lower() not in ("dark","light"):
        tf = args[1]
    if len(args) >= 2 and args[-1].lower() in ("dark","light"):
        th = args[-1].lower()
    elif len(args) >= 3 and args[2].lower() in ("dark","light"):
        th = args[2].lower()
    ex, tk, _ = resolve_symbol(symbol)
    return ex, tk, norm_interval(tf), norm_theme(th)

def parse_multi_args(args: List[str]) -> Tuple[List[str],str,str]:
    if not args:
        return [], DEFAULT_INTERVAL, DEFAULT_THEME
    theme = DEFAULT_THEME
    if args[-1].lower() in ("dark","light"):
        theme = args[-1].lower()
        args = args[:-1]
    tf = DEFAULT_INTERVAL
    if args and re.fullmatch(r"\d+", args[-1]):
        tf = args[-1]; args = args[:-1]
    return args, norm_interval(tf), norm_theme(theme)

# ------------------------------------------------------------------
# Bot Commands
# ------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nm = update.effective_user.first_name if update.effective_user else ""
    msg = (
        f"Hi {nm} 👋\n"
        "I grab TradingView charts (Quotex feed).\n\n"
        "Try:\n"
        "/snap EUR/USD 5 dark\n"
        "/snapmulti EUR/USD GBP/USD 15 light\n"
        "/snapall\n"
        "/pairs\n"
        "/help"
    )
    await context.bot.send_message(update.effective_chat.id, msg)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📘 Help\n\n"
        "/snap SYMBOL [interval] [theme]\n"
        "/snapmulti S1 S2 ... [interval] [theme]\n"
        "/snapall  (all FX+OTC pairs)\n"
        "/pairs    (list)\n\n"
        "Intervals: minutes (num) or D/W/M.\n"
        "Themes: dark|light."
    )
    await context.bot.send_message(update.effective_chat.id, msg)

async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["📊 FX Pairs:"] + [f"• {p}" for p in FX_PAIRS] + ["", "🕒 OTC Pairs:"] + [f"• {p}" for p in OTC_PAIRS]
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
    p_trip = []
    for p in pairs:
        ex, tk, _ = resolve_symbol(p)
        p_trip.append((ex, tk, p))
    media_items = await asyncio.to_thread(build_media_items_sync, p_trip, tf, th, prefix="[MULTI] ")
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)

async def cmd_snapall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, f"⚡ Capturing all {len(ALL_PAIRS)} pairs…")
    p_trip = []
    for p in ALL_PAIRS:
        ex, tk, _ = resolve_symbol(p)
        p_trip.append((ex, tk, p))
    media_items = await asyncio.to_thread(build_media_items_sync, p_trip, DEFAULT_INTERVAL, DEFAULT_THEME, prefix="[ALL] ")
    await send_media_group_chunked(chat_id, context, media_items, chunk_size=5)

async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, f"You said: {update.message.text}\nTry /help.")

async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, "❌ Unknown command. Try /help.")

# ------------------------------------------------------------------
# Flask TradingView Webhook
# ------------------------------------------------------------------
flask_app = Flask(__name__)

def _parse_tv_payload(data: dict) -> Dict[str,str]:
    d = {}
    d["chat_id"]   = str(data.get("chat_id") or DEFAULT_CHAT_ID or "")
    d["pair"]      = str(data.get("pair") or data.get("symbol") or data.get("ticker") or "EURUSD")
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

# --- replace old _handle_tv_alert & /tv route with this ---

def _handle_tv_alert(data: dict):
    """
    Process a TradingView alert payload synchronously (Flask thread).
    Accept both header-based and body-based secrets.
    """
    # Security: header OR body secret allowed (TradingView can't send custom headers)
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
    direction = payload["direction"]
    expiry    = payload["expiry"] or f"{data.get('default_expiry_min','') }m"
    strat     = payload["strategy"]
    winrate   = payload["winrate"]
    tf        = norm_interval(payload["timeframe"])
    theme     = norm_theme(payload["theme"])

    ex, tk, _ = resolve_symbol(raw_pair)

    # Inform chat
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

    # Attempt chart
    try:
        node_start_browser()
        png = fetch_snapshot_png_retry(ex, tk, tf, theme)
        tg_api_send_photo_bytes(chat_id, png, caption=f"{ex}:{tk} • TF {tf} • {theme}")
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

# optional compatibility alias
@flask_app.route("/webhook", methods=["POST"])
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

# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    start_flask_background()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("pairs",     cmd_pairs))
    app.add_handler(CommandHandler("snap",      cmd_snap))
    app.add_handler(CommandHandler("snapmulti", cmd_snapmulti))
    app.add_handler(CommandHandler("snapall",   cmd_snapall))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info("Bot polling… (Quotex default) | Webhook on port %s", TV_WEBHOOK_PORT)
    app.run_polling()

if __name__ == "__main__":
    main()
