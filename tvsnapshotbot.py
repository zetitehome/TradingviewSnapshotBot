import os
import io
import time
import logging
import asyncio
import httpx
from telegram import InputMediaPhoto

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
BASE_URL = os.environ.get("SNAPSHOT_NODE_URL", "https://tradingviewsnapshotbot.onrender.com")
DEFAULT_THEME = os.environ.get("DEFAULT_THEME", "dark")
DEFAULT_INTERVAL = os.environ.get("DEFAULT_INTERVAL", "1")
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "QUOTEX")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Exchange fallback order
_env_ex_fallback = os.environ.get(
    "EXCHANGE_FALLBACKS",
    "FX_IDC,OANDA,FOREXCOM,FXCM,IDC"
)
EXCHANGE_FALLBACKS = [x.strip().upper() for x in _env_ex_fallback.split(",") if x.strip()]

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("TVSnapBot")

_http = httpx.Client(timeout=90)

# Rate limit / throttle
_last_request_ts = 0
def global_throttle_wait(min_gap=1.5):
    global _last_request_ts
    now = time.time()
    gap = now - _last_request_ts
    if gap < min_gap:
        time.sleep(min_gap - gap)
    _last_request_ts = time.time()

# ------------------------------------------------------------
# SNAPSHOT FETCHER WITH FALLBACK
# ------------------------------------------------------------
def fetch_snapshot_png_any(primary_ex: str, tk: str, interval: str, theme: str, base: str = "chart") -> tuple[bytes, str]:
    """
    Try the user's requested exchange first, then fall back through
    EXCHANGE_FALLBACKS until one returns a valid PNG image.

    Returns: (png_bytes, exchange_used)

    Raises RuntimeError if all attempts fail.
    """
    tried = []
    last_err = None
    cand = [primary_ex.upper()] + [e for e in EXCHANGE_FALLBACKS if e.upper() != primary_ex.upper()]

    for ex in cand:
        tried.append(ex)
        try:
            global_throttle_wait()
            url = f"{BASE_URL}/run?base={base}&exchange={ex}&ticker={tk}&interval={interval}&theme={theme}"
            logger.info("Snapshot try %s:%s  URL=%s", ex, tk, url)
            r = _http.get(url)
            ct = r.headers.get("Content-Type", "")
            if r.status_code == 200 and ct.startswith("image"):
                logger.info("Snapshot success %s:%s via %s (%d bytes)", ex, tk, ex, len(r.content))
                return r.content, ex
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            logger.warning("Snapshot failed %s:%s via %s -> %s", ex, tk, ex, last_err)
        except Exception as e:
            last_err = str(e)
            logger.warning("Snapshot exception %s:%s via %s -> %s", ex, tk, ex, last_err)
        time.sleep(2)

    raise RuntimeError(f"All exchanges failed for {tk}. Last error: {last_err}. Tried: {tried}")

# ------------------------------------------------------------
# TELEGRAM API (Simple sendPhoto)
# ------------------------------------------------------------
def tg_api_send_photo_bytes(chat_id, png_bytes, caption=""):
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN env var")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", png_bytes, "image/png")}
    data = {"chat_id": chat_id, "caption": caption}
    r = _http.post(url, data=data, files=files)
    if r.status_code != 200:
        logger.error("Telegram sendPhoto failed: %s", r.text)
    return r.json()

# ------------------------------------------------------------
# SEND SINGLE SNAPSHOT
# ------------------------------------------------------------
async def send_snapshot_photo(chat_id, exchange, ticker, interval, theme, prefix=""):
    try:
        png, ex_used = await asyncio.to_thread(fetch_snapshot_png_any, exchange, ticker, interval, theme)
        caption = f"{prefix}{ex_used}:{ticker} • TF {interval} • {theme}"
        return tg_api_send_photo_bytes(chat_id, png, caption)
    except Exception as e:
        logger.error("Snapshot failed for %s:%s -> %s", exchange, ticker, e)
        return None

# ------------------------------------------------------------
# SNAPSHOT MULTI
# ------------------------------------------------------------
def build_media_items_sync(chat_id, pairs, interval, theme, prefix=""):
    out = []
    for ex, tk, lab in pairs:
        try:
            png, ex_used = fetch_snapshot_png_any(ex, tk, interval, theme)
            bio = io.BytesIO(png)
            bio.name = "chart.png"
            cap = f"{prefix}{ex_used}:{tk} • {lab} • TF {interval} • {theme}"
            out.append(InputMediaPhoto(media=bio, caption=cap))
        except Exception as e:
            logger.warning("Failed building media for %s:%s -> %s", ex, tk, e)
    return out

# ------------------------------------------------------------
# TRADINGVIEW WEBHOOK HANDLER
# ------------------------------------------------------------
def _handle_tv_alert(data):
    chat_id = data.get("chat_id", TELEGRAM_CHAT_ID)
    ex = data.get("exchange", DEFAULT_EXCHANGE)
    tk = data.get("pair", "EUR/USD").replace("/", "")
    tf = data.get("timeframe", DEFAULT_INTERVAL)
    theme = data.get("theme", DEFAULT_THEME)
    logger.info("TV Alert: %s %s:%s TF=%s", chat_id, ex, tk, tf)
    try:
        png, ex_used = fetch_snapshot_png_any(ex, tk, tf, theme)
        tg_api_send_photo_bytes(chat_id, png, caption=f"{ex_used}:{tk} • TF {tf} • {theme}")
    except Exception as e:
        logger.error("⚠ Chart snapshot failed for %s:%s: %s", ex, tk, e)
        tg_api_send_photo_bytes(chat_id, b"", caption=f"⚠ Chart snapshot failed for {tk}: {e}")

# ------------------------------------------------------------
# BOT START
# ------------------------------------------------------------
async def main():
    logger.info("Bot started with multi-exchange fallback support.")
    # Start your flask or aiohttp webhook server here
    # Example:
    # from mywebhookserver import start_webhook
    # await start_webhook()

if __name__ == "__main__":
    asyncio.run(main())
