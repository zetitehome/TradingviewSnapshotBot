import logging
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.enums.parse_mode import ParseMode
from aiogram.webhook.aiohttp_server import setup_application
from aiohttp import web

API_TOKEN = "8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA"
CHAT_ID = 6337160812  # Your Telegram Chat ID

bot = Bot(token=API_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

@dp.message(F.text == "/start")
async def start_command(msg: Message):
    await msg.answer("🤖 Bot is active and listening!")

@dp.message(F.text.startswith("/trade"))
async def trade_command(msg: Message):
    await msg.answer("📥 Trade signal received. Confirm with 'yes' to proceed.")

    def check_reply(m: Message):
        return m.from_user.id == msg.from_user.id and m.text.lower() in ["yes", "no"]

    try:
        response = await dp.wait_for_message(timeout=10.0, filters=check_reply)
        if response.text.lower() == "yes":
            await msg.answer("✅ Trade confirmed and sent.")
            # Add trading logic here (e.g., trigger UI.Vision macro)
        else:
            await msg.answer("❌ Trade cancelled.")
    except asyncio.TimeoutError:
        await msg.answer("⏰ Timeout. Auto-executing trade by default.")
        # Add auto-trade fallback here

# Webhook route for TradingView or ngrok
async def webhook_handler(request):
    data = await request.json()
    signal = data.get("signal", "No signal")
    await bot.send_message(CHAT_ID, f"📈 New Signal: <b>{signal}</b>\nReply with 'yes' or 'no' to confirm.")
    return web.Response(text="OK")

def create_app():
    app = web.Application()
    app.router.add_post("/callback", webhook_handler)
    setup_application(app, dp)
    return app

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(), port=3000)
