// bot.js
const TelegramBot = require('node-telegram-bot-api');
const axios = require('axios');
const fs = require('fs');
const path = require('path');

const TELEGRAM_TOKEN = '8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA';
const CHAT_LOG_ID = '6337160812'; // Your Telegram user ID
const bot = new TelegramBot(TELEGRAM_TOKEN, { polling: true });

const TRADE_ENDPOINT = 'http://localhost:3333/run-trade'; // Your local webhook server
const LOG_FILE = path.join(__dirname, 'logs/trade-log.json');

// In-memory state (per user)
const userSessions = {};

function logTrade(data) {
  const existing = fs.existsSync(LOG_FILE) ? JSON.parse(fs.readFileSync(LOG_FILE)) : [];
  existing.push(data);
  fs.writeFileSync(LOG_FILE, JSON.stringify(existing, null, 2));
}

function sendLogToTelegram(message) {
  bot.sendMessage(CHAT_LOG_ID, `📘 Trade Log:\n${message}`);
}

bot.onText(/\/start|\/menu/, (msg) => {
  const chatId = msg.chat.id;
  userSessions[chatId] = {};
  bot.sendMessage(chatId, 'Welcome! Choose a trading pair:', {
    reply_markup: {
      inline_keyboard: [
        [
          { text: 'EUR/USD', callback_data: 'pair_EURUSD' },
          { text: 'GBP/USD', callback_data: 'pair_GBPUSD' },
        ],
        [
          { text: 'AUD/JPY', callback_data: 'pair_AUDJPY' },
          { text: 'OTC/EURUSD', callback_data: 'pair_OTCEURUSD' },
        ]
      ]
    }
  });
});

bot.on('callback_query', async (query) => {
  const chatId = query.message.chat.id;
  const userId = query.from.id;
  const data = query.data;
  const session = userSessions[chatId] || {};

  if (data.startsWith('pair_')) {
    session.pair = data.split('_')[1];
    userSessions[chatId] = session;
    bot.sendMessage(chatId, 'Select expiry time:', {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '1 min', callback_data: 'expiry_1' },
            { text: '3 min', callback_data: 'expiry_3' },
            { text: '5 min', callback_data: 'expiry_5' },
          ],
          [
            { text: '15 min', callback_data: 'expiry_15' }]
        ]
      }
    });
  } else if (data.startsWith('expiry_')) {
    session.expiry = data.split('_')[1];
    userSessions[chatId] = session;
    bot.sendMessage(chatId, `Ready to place trade:\n\n💱 Pair: ${session.pair}\n⏱ Expiry: ${session.expiry} min`, {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '✅ Confirm Trade', callback_data: 'confirm_trade' },
            { text: '❌ Cancel', callback_data: 'cancel_trade' },
          ]
        ]
      }
    });
  } else if (data === 'confirm_trade') {
    const payload = {
      pair: session.pair,
      expiry: session.expiry,
      amount: '5%',
      mode: 'auto',
    };
    try {
      const response = await axios.post(TRADE_ENDPOINT, payload);
      const logMessage = `Trade executed\nPair: ${payload.pair}\nExpiry: ${payload.expiry} min\nMode: ${payload.mode}`;
      sendLogToTelegram(logMessage);
      logTrade({ ...payload, timestamp: new Date().toISOString(), result: 'executed' });
      bot.sendMessage(chatId, '🚀 Trade placed successfully!');
    } catch (err) {
      sendLogToTelegram(`❗ Trade error: ${err.message}`);
      bot.sendMessage(chatId, '⚠️ Failed to place trade.');
    }
  } else if (data === 'cancel_trade') {
    bot.sendMessage(chatId, '❌ Trade canceled. Start again with /menu');
    userSessions[chatId] = {};
  }
});

bot.onText(/\/log/, (msg) => {
  const chatId = msg.chat.id;
  if (!fs.existsSync(LOG_FILE)) return bot.sendMessage(chatId, 'No logs yet.');
  const logs = JSON.parse(fs.readFileSync(LOG_FILE));
  const recent = logs.slice(-5).map(l => `• ${l.pair} (${l.expiry}m) — ${l.result}`).join('\n');
  bot.sendMessage(chatId, `📊 Last 5 Trades:\n${recent}`);
});