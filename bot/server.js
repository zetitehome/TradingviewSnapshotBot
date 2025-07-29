require('dotenv').config();
const express = require('express');
const bodyParser = require('body-parser');
const axios = require('axios');
const app = express();

const PORT = process.env.PORT || 3000;
const TV_WEBHOOK_PORT = process.env.TV_WEBHOOK_PORT || 8081;
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID;
const UI_VISION_URL = process.env.UI_VISION_URL;
const UI_VISION_MACRO_NAME = process.env.UI_VISION_MACRO_NAME;
const UI_VISION_MACRO_PARAMS_JSON = process.env.UI_VISION_MACRO_PARAMS_JSON;

app.use(bodyParser.json());

const triggerUIVisionMacro = async (symbol, interval, exchange, theme) => {
  try {
    const macroParams = JSON.parse(UI_VISION_MACRO_PARAMS_JSON
      .replace('{symbol}', symbol)
      .replace('{interval}', interval)
      .replace('{exchange}', exchange)
      .replace('{theme}', theme));

    await axios.post(UI_VISION_URL, {
      macro: UI_VISION_MACRO_NAME,
      params: macroParams
    });

    console.log(`✅ UI.Vision macro triggered for ${symbol}`);
  } catch (error) {
    console.error('❌ Failed to trigger UI.Vision:', error.message);
  }
};

const sendTelegramAlert = async (message) => {
  try {
    await axios.post(`https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`, {
      chat_id: TELEGRAM_CHAT_ID,
      text: message,
      parse_mode: 'Markdown'
    });
    console.log(`📨 Telegram alert sent`);
  } catch (error) {
    console.error('❌ Telegram alert error:', error.message);
  }
};

const tvApp = express();
tvApp.use(bodyParser.json());

tvApp.post('/webhook', async (req, res) => {
  const data = req.body;

  const symbol = data.symbol || 'EURUSD';
  const interval = data.interval || process.env.DEFAULT_INTERVAL || '1';
  const exchange = data.exchange || process.env.DEFAULT_EXCHANGE || 'FX';
  const theme = data.theme || process.env.DEFAULT_THEME || 'dark';
  const signal = data.signal || 'BUY';

  const msg = `📈 *Signal Received:*\n• Pair: *${symbol}*\n• Interval: *${interval}m*\n• Signal: *${signal}*\n• Exchange: *${exchange}*`;

  await sendTelegramAlert(msg);
  await triggerUIVisionMacro(symbol, interval, exchange, theme);

  res.status(200).json({ status: 'ok', message: 'Signal processed.' });
});

app.get('/', (req, res) => {
  res.send('📡 PocketSignal Bot is running!');
});

app.listen(PORT, () => {
  console.log(`🤖 Telegram Bot Server running on http://localhost:${PORT}`);
});

tvApp.listen(TV_WEBHOOK_PORT, () => {
  console.log(`📩 TradingView Webhook Server running on http://localhost:${TV_WEBHOOK_PORT}/webhook`);
});
(async () => {
  try {
    await startBrowser();
    console.log('Browser started successfully.');
  } catch (err) {
    console.error('Failed to start browser:', err);
  }
})();