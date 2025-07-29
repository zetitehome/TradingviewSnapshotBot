// index.js
require('dotenv').config();
const express = require('express');
const bodyParser = require('body-parser');
const axios = require('axios');
const TelegramBot = require('node-telegram-bot-api');

const bot = new TelegramBot(process.env.TELEGRAM_BOT_TOKEN, { polling: true });
const TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID;

const app = express();
app.use(bodyParser.json());

// === TradingView Webhook Listener ===
app.post('/webhook', async (req, res) => {
  const alert = req.body;
  console.log("📩 Received alert from TradingView:", alert);

  try {
    // === Format Telegram Message ===
    const signalText = `📊 New Signal:
🪙 Pair: ${alert.symbol}
🕐 Timeframe: ${alert.interval} min
📈 Signal: ${alert.signal.toUpperCase()}
🔥 Win Rate: ${alert.winrate}%
⌛ Expiry: ${alert.expiry} min`;

    // === Send to Telegram ===
    await bot.sendMessage(TELEGRAM_CHAT_ID, signalText);

    // === Trigger UI.Vision Macro ===
    const macroParams = {
      symbol: alert.symbol || 'EURUSD',
      interval: alert.interval || '1',
      exchange: alert.exchange || 'FX',
      theme: alert.theme || 'dark',
    };

    await axios.post(process.env.UI_VISION_URL, {
      cmd: "RUN",
      macro: process.env.UI_VISION_MACRO_NAME,
      storage: "local",
      closeRPA: false,
      timeout: 60,
      parameters: macroParams,
    });

    console.log("✅ Signal forwarded to Telegram and UI.Vision triggered.");
    res.sendStatus(200);
  } catch (err) {
    console.error("❌ Error:", err.message);
    res.status(500).send("Error processing webhook");
  }
});

// === Start Server ===
app.listen(process.env.TV_WEBHOOK_PORT, () => {
  console.log(`📡 TradingView Webhook Server running at http://localhost:${process.env.TV_WEBHOOK_PORT}/webhook`);
});
