import asyncio
from aiogram import Bot, Dispatcher, types, Router
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.client.session.aiohttp import AiohttpSession

TELEGRAM_TOKEN = "8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA"
USER_CHAT_ID = 6337160812

bot = Bot(token=TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
router = Router()

# Handle incoming messages
@router.message()
async def handle_message(message: Message):
    if message.chat.id != USER_CHAT_ID:
        await message.answer("Access denied.")
        return

    if "analyze" in message.text.lower():
        await message.answer("📸 Capturing chart screenshot...")
        # Trigger screenshot logic or webhook here
        await message.answer("Do you want to place this trade? (yes/no)")

    elif message.text.lower() == "yes":
        await message.answer("✅ Trade confirmed! Executing UI.Vision macro...")
        # Trigger UI.Vision or webhook here

    elif message.text.lower() == "no":
        await message.answer("❌ Trade cancelled.")

    else:
        await message.answer("Send /analyze to start.")

async def main():
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    