#!/usr/bin/python

import logging
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import requests
import nest_asyncio
import os
nest_asyncio.apply()

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Optional: Load .env file for local development ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set")

def snapshot(mesg):
    cmd = [x if i == 0 else x.upper() for i, x in enumerate(mesg)] if len(
        mesg) >= 4 and len(mesg) <= 5 and (mesg[0] == '-' or (len(mesg[0]) == 8 and not mesg[0].islower() and not mesg[0].isupper())) else ['-', 'BINANCE', 'BTCUSDT', '1D'] if len(mesg) == 0 else '❌ Wrong Command ! Try  like this "/snap - nse nifty 1d light" or "/snap - nse nifty 1d dark".\n\nPlease Try Again with a correct one ❗️, You may wanna check /help❓for details.\n\nThank You 👍.'
    if isinstance(cmd, str):
        return cmd
    else:
        ChartID = f'chart/{cmd[0]}/' if len(cmd[0]) == 8 and not cmd[0].islower() and not cmd[0].isupper() else 'chart/'
        theme = 'light' if len(cmd) == 4 else 'dark' if len(cmd) == 5 and cmd[4].lower() == 'dark' else 'light'
        # Use port 10000 to match your Node.js server
        requesturl =  f'http://localhost:10000/run?base={ChartID}&exchange={cmd[1]}&ticker={cmd[2]}&interval={cmd[3]}&theme={theme}'
        return f'https://www.tradingview.com/x/{requests.get(requesturl).text}'

def snapshotlist(mesg):
    snapshotsurl = []
    cmd = [x if i == 0 else x.upper() for i, x in enumerate(mesg)] if len(
        mesg) >= 6 and (mesg[-1] == 'light' or mesg[-1] =='dark') and (mesg[0] == '-' or (len(mesg[0]) == 8 and not mesg[0].islower() and not mesg[0].isupper())) else ['-', 'BINANCE', 'BTCUSDT', '1W'] if len(mesg) == 0 else '❌ Wrong Command ! Try  like this "/snap - nse nifty 1d light" or "/snap - nse nifty 1d dark".\n\nPlease Try Again with a correct one ❗️, You may wanna check /help❓for details.\n\nThank You 👍.'
    if isinstance(cmd, str):
        return cmd
    else:
        ChartID = f'chart/{cmd[0]}/' if len(cmd[0]) == 8 and not cmd[0].islower() and not cmd[0].isupper() else 'chart/'
        tickers = cmd[2:-2]
        for symbol in tickers:
            # Use port 10000 to match your Node.js server
            requesturl =  f'http://localhost:10000/run?base={ChartID}&exchange={cmd[1]}&ticker={symbol}&interval={cmd[-2]}&theme={cmd[-1].lower()}'
            snapshotsurl.append(f'https://www.tradingview.com/x/{requests.get(requesturl).text}')
        return snapshotsurl

async def start(update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.from_user.first_name
    reply = "Hi!! {}".format(name)
    await context.bot.send_message(chat_id=update.effective_chat.id,
                             text=f'{reply} \n\nI\'m a TVSnapShot Bot 🤖, I can generate Tradingview Chart Snapshot 📊 of your choice.\n\nPlease type /help❓to know request commands. \n\nThank You. 👍')

async def help(update, context: ContextTypes.DEFAULT_TYPE):
    reply = """❗️⚠️ Please type /snap or /snaplist first to initate command reception.

You can send the following parameters with a space in between to generate the snapshot URL. 🚀

1️⃣ ChartID = The 8 character long chart id of your saved chart layout or a simple '-'. (Also make sure your chart layout sharing is ON)
(Note: To find your ChartID, you may wanna look at your url https://www.tradingview.com/chart/chartid/)

2️⃣ Exchange Name = Type The Exchange Name Such as 'NSE', 'BSE', 'NASDAQ', 'BINANCE' Etc.

3️⃣ Ticker / Symbol Name = Type the trading ticker name such as 'NIFTY', 'BANKNIFTY', 'BTCUSDT', 'ETHUSDT' Etc.

4️⃣ Interval / Timeframe = Type the chart Timeframe / Interval such as '1D' for 1 Day, '1W' for 1 Week Etc...

- Interval Cheat Codes = Accepted Intervals are [1, 3, 5, 15, 30, 45, 1H, 2H, 3H, 4H, 1D, 1W, 1M]
- (Note: Number without letters are in minutes, for example '1' means 1 Minute Timeframe / Interval)

5️⃣ Theme = light / dark (Note: if left empty then light theme will be used by default)

Example-1: 👉 To generate a snapshot of SBIN trading at NSE with 1 minute time frame, the command shall be

- With Default chart layout: /snap - nse sbin 1 light
- With your chart layout: /snap aSdfzXcV nse sbin 1 light

Example-2: 👉 To generate a snapshot of all Tickers/Symbols trading at BINANCE with 1 day time frame, the command shall be

- With Default chart layout: /snaplist - binance ethusdt btcusdt dogeusdt xrpusdt yfiiusdt bnbusdt 1d dark
- With your chart layout: /snaplist aSdfzXcV binance ethusdt btcusdt dogeusdt xrpusdt yfiiusdt bnbusdt 1d light"""

    await context.bot.send_message(chat_id=update.effective_chat.id,
                             text=reply)

async def snap(update, context: ContextTypes.DEFAULT_TYPE):
    reply = snapshot(context.args)
    if 'tradingview.com/x/' in reply:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=f'🥳 Hooray 🥳 - The Requested SnapShot Is Generated: ✔️ 👇 : {reply}')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)

async def snaplist(update, context: ContextTypes.DEFAULT_TYPE):
    snapurls = snapshotlist(context.args)
    if isinstance(snapurls, str):
        await context.bot.send_message(chat_id=update.effective_chat.id, text=snapurls)
    else:
        for snapurl in snapurls:
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=f'🥳 Hooray 🥳 - The Requested SnapShot Is Generated: ✔️ 👇 : {snapurl}')  

async def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help))
    app.add_handler(CommandHandler("snap", snap))
    app.add_handler(CommandHandler("snaplist", snaplist))

    async def echo_text(update, context: ContextTypes.DEFAULT_TYPE):
        await context.bot.send_message(chat_id=update.effective_chat.id, text=update.message.text)

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_text))

    async def sticker(update, context: ContextTypes.DEFAULT_TYPE):
        await context.bot.send_message(chat_id=update.effective_chat.id, text="👍 Nice sticker!")

    app.add_handler(MessageHandler(filters.Sticker.ALL & (~filters.COMMAND), sticker))

    async def unknown(update, context: ContextTypes.DEFAULT_TYPE):
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Sorry, I didn't understand that command.")

    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    logger.info("Started...")
    await app.run_polling()

if __name__ == "__main__":
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
    