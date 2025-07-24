import os
from aiogram import Bot, Dispatcher, types
from aiogram.types import InputFile
from aiogram.utils.webhook import AiohttpWebhook
from aiohttp import web
import asyncio
import subprocess
from datetime import datetime

# === CONFIG ===
API_TOKEN = "8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA"
WEBHOOK_URL = "https://6c3090b3d7a5.ngrok-free.app/callback"
WEBAPP_HOST = 'localhost'
WEBAPP_PORT = 3000
TELEGRAM_CHAT_ID = 6337160812  # ← Your chat ID

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)
routes = web.RouteTableDef()

# === HTML Logging ===
HTML_LOG_FILE = "trade_logs.html"
if not os.path.exists(HTML_LOG_FILE):
    with open(HTML_LOG_FILE, "w") as f:
        f.write("<html><head><title>Trade Logs</title></head><body><h2>Trade Logs</h2><ul id='logs'>\n")

def log_to_html(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"<li>[{timestamp}] {message}</li>\n"
    with open(HTML_LOG_FILE, "a") as f:
        f.write(entry)

# === Routes ===
@routes.post('/callback')
async def on_callback(request):
    data = await request.json()
    signal = data.get("signal", "Unknown")
    pair = data.get("pair", "N/A")
    expiry = data.get("expiry", "N/A")
    
    # Compose message
    msg = f"📥 *Binary Signal Alert*\n\n🟢 *Signal:* {signal}\n💱 *Pair:* {pair}\n⏳ *Expiry:* {expiry}"
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")
    
    # Trigger UI.Vision
    subprocess.Popen(["cmd", "/c", "start", "", "uivision://run?macro=TradeMacro"])

    # Log to HTML
    log_to_html(f"Sent: {signal} on {pair} with {expiry} expiry")

    return web.Response(text="OK")

# === Start Webhook ===
async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    print(f"✅ Webhook set at: {WEBHOOK_URL}")

async def on_shutdown(app):
    await bot.delete_webhook()
    print("❌ Webhook removed")

app = web.Application()
app.add_routes(routes)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == '__main__':
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)
