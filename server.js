// server.js

const express = require('express');
const bodyParser = require('body-parser');
const axios = require('axios');
const { exec } = require('child_process');
require('dotenv').config();

const app = express();
const PORT = 8080;

app.use(bodyParser.json());

// === CONFIGURATION ===
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID;
const LOCAL_UIVISION_URL = 'http://localhost:3366/command'; // UI.Vision browser extension port
const AUTO_TRADE_ENABLED = true;
const MIN_CONFIDENCE = 70; // Require confirmation below this

// === TELEGRAM HELPER ===
async function sendTelegramMessage(text, buttons = []) {
  const payload = {
    chat_id: TELEGRAM_CHAT_ID,
    text,
    parse_mode: 'HTML',
  };
  if (buttons.length > 0) {
    payload.reply_markup = {
      inline_keyboard: [buttons],
    };
  }
  try {
    await axios.post(`https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`, payload);
  } catch (err) {
    console.error('Telegram Error:', err.response?.data || err.message);
  }
}

// === TRADE EXECUTION ===
function triggerTradeUIVision(pair, action, expiry, amount) {
  const macroPayload = {
    cmd: 'runMacro',
    macro: 'TradePocketOption',
    args: [pair, action, expiry, amount.toString()]
  };
  return axios.post(LOCAL_UIVISION_URL, macroPayload);
}

// === CONFIRMATION HANDLER ===
let pendingTrade = null;
app.post('/confirm-trade', async (req, res) => {
  const decision = req.body?.decision?.toLowerCase();
  if (pendingTrade && decision) {
    if (decision === 'yes') {
      await triggerTradeUIVision(...pendingTrade);
      await sendTelegramMessage(`✅ Trade confirmed and placed: ${pendingTrade.join(', ')}`);
    } else {
      await sendTelegramMessage(`❌ Trade canceled.`);
    }
    pendingTrade = null;
  }
  res.sendStatus(200);
});

// === ALERT HANDLER ===
app.post('/webhook', async (req, res) => {
  const alert = req.body;
  if (!alert || !alert.pair || !alert.direction) {
    return res.status(400).send('Invalid alert');
  }

  const {
    pair = 'EUR/USD',
    direction = 'buy',
    confidence = 75,
    expiry = 5,
    strategy = 'default',
    snapshot = '',
    trade_amount = 1
  } = alert;

  let amount = Math.min(Math.max(trade_amount, 1), 100); // Clamp to $1–$100

  // Alert message
  const message = `<b>📡 New Trade Signal</b>\n\n` +
    `<b>Pair:</b> ${pair}\n<b>Direction:</b> ${direction.toUpperCase()}\n<b>Confidence:</b> ${confidence}%\n<b>Expiry:</b> ${expiry} min\n<b>Amount:</b> $${amount}\n<b>Strategy:</b> ${strategy}\n\n` +
    (snapshot ? `<a href="${snapshot}">📸 View Chart Snapshot</a>` : '');

  if (confidence >= MIN_CONFIDENCE && AUTO_TRADE_ENABLED) {
    await sendTelegramMessage(message + `\n\n✅ <b>Auto trade executed</b>`);
    await triggerTradeUIVision(pair, direction, expiry, amount);
  } else {
    // Ask for confirmation
    pendingTrade = [pair, direction, expiry, amount];
    await sendTelegramMessage(message + `\n\n⚠️ Confidence under ${MIN_CONFIDENCE}%\nConfirm to trade:`,
      [
        { text: '✅ Yes', callback_data: 'yes' },
        { text: '❌ No', callback_data: 'no' }
      ]
    );
  }

  res.sendStatus(200);
});

// === START SERVER ===
app.listen(PORT, () => {
  console.log(`📡 Webhook server running on http://localhost:${PORT}`);
});
