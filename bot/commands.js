require('dotenv').config();
const express = require('express');
const TelegramBot = require('node-telegram-bot-api');
const fetch = require('node-fetch');

// Load config from .env
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID;

const TV_WEBHOOK_PORT = Number(process.env.TV_WEBHOOK_PORT) || 8081;

const UI_VISION_URL = process.env.UI_VISION_URL;
const UI_VISION_MACRO_NAME = process.env.UI_VISION_MACRO_NAME || 'PocketTrade';
const DEFAULT_EXCHANGE = process.env.DEFAULT_EXCHANGE || 'FX';
const DEFAULT_INTERVAL = process.env.DEFAULT_INTERVAL || '1';
const DEFAULT_THEME = process.env.DEFAULT_THEME || 'dark';

// Initialize Telegram bot
const bot = new TelegramBot(TELEGRAM_BOT_TOKEN, { polling: true });

// Express app for TradingView webhook
const app = express();
app.use(express.json());

// Trigger UI.Vision macro
async function triggerUIMacro(symbol, interval, exchange = DEFAULT_EXCHANGE, theme = DEFAULT_THEME) {
  const macroParams = {
    symbol,
    interval,
    exchange,
    theme
  };
  const url = `${UI_VISION_URL}?macro=${UI_VISION_MACRO_NAME}&params=${encodeURIComponent(JSON.stringify(macroParams))}`;
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    console.log(`UI.Vision macro triggered for ${symbol} @${interval}`);
    return true;
  } catch (e) {
    console.error('Error triggering UI.Vision macro:', e);
    return false;
  }
}

// Telegram /start command
bot.onText(/\/start/, (msg) => {
  bot.sendMessage(msg.chat.id, `👋 Hello ${msg.from.first_name}, I am running!`);
});

// TradingView webhook POST handler
app.post('/webhook', async (req, res) => {
  try {
    const { symbol, interval, signal } = req.body;
    if (!symbol || !interval || !signal) {
      res.status(400).send('Missing fields in webhook data');
      return;
    }

    // Trigger macro
    await triggerUIMacro(symbol, interval);

    // Notify Telegram
    const text = `📈 Signal received:\nSymbol: ${symbol}\nInterval: ${interval}\nSignal: ${signal}`;
    bot.sendMessage(TELEGRAM_CHAT_ID, text);

    res.status(200).send('Webhook processed');
  } catch (e) {
    console.error('Webhook error:', e);
    res.status(500).send('Internal error');
  }
});

// Start server
app.listen(TV_WEBHOOK_PORT, () => {
  console.log(`TradingView webhook server running on port ${TV_WEBHOOK_PORT}`);
});
const sqlite3 = require('sqlite3').verbose();
const fs = require('fs');
// SQLite setup
const db = new sqlite3.Database('./trades.db');
db.run(`CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pair TEXT,
  direction TEXT,
  expiry TEXT,
  result TEXT,
  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)`);

// === Valid pairs including OTC ===
const validPairs = new Set([
  'EUR/USD', 'GBP/USD', 'USD/JPY', 'AUD/USD', 'USD/CHF', 'NZD/USD', 'EUR/GBP', 'EUR/JPY', 'GBP/JPY',
  'EUR/USD OTC', 'GBP/USD OTC', 'USD/JPY OTC', 'AUD/USD OTC', 'USD/CHF OTC', 'NZD/USD OTC', 'EUR/GBP OTC', 'EUR/JPY OTC', 'GBP/JPY OTC'
]);

// === UTILS ===
function isValidPair(pair) {
  return validPairs.has(pair.toUpperCase());
}

function calcStats(callback) {
  db.all('SELECT result, COUNT(*) as count FROM trades GROUP BY result', (err, rows) => {
    if (err) return callback(err);
    const stats = {};
    let total = 0;
    rows.forEach(r => {
      stats[r.result || 'PENDING'] = r.count;
      total += r.count;
    });
    stats.total = total;
    callback(null, stats);
  });
}

// === UI.Vision Macro Trigger ===
async function triggerMacro(trade) {
  const macroWebhookUrl = process.env.UIVISION_WEBHOOK_URL || 'http://localhost:3001'; // your UI.Vision local listener URL

  try {
    const res = await fetch(macroWebhookUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(trade)
    });
    if (!res.ok) {
      console.error('UI.Vision macro trigger failed:', res.statusText);
    } else {
      console.log('UI.Vision macro triggered for trade:', trade);
    }
  } catch (e) {
    console.error('Error triggering UI.Vision macro:', e.message);
  }
}

// === EXPRESS WEBHOOK: receive alerts from TradingView ===
app.post('/webhook', (req, res) => {
  const { pair, direction, expiry, confidence } = req.body;

  if (!pair || !direction || !expiry) {
    return res.status(400).send('Missing required parameters');
  }

  if (!isValidPair(pair)) {
    console.log(`Rejected invalid pair: ${pair}`);
    return res.status(400).send('Invalid trading pair');
  }

  // Simple confidence check (expecting 0-100)
  if (confidence && confidence < 60) {
    console.log(`Rejected low confidence trade: ${confidence}%`);
    return res.status(200).send('Confidence below threshold');
  }

  // Insert trade into DB with no result yet
  db.run(
    'INSERT INTO trades (pair, direction, expiry, result) VALUES (?, ?, ?, NULL)',
    [pair.toUpperCase(), direction.toUpperCase(), expiry],
    function (err) {
      if (err) {
        console.error('DB insert error:', err.message);
        return res.status(500).send('DB error');
      }

      const trade = {
        id: this.lastID,
        pair,
        direction,
        expiry,
        confidence
      };

      // Notify Telegram channel/user
      const text = `🚀 New trade signal #${trade.id}\nPair: ${pair}\nDirection: ${direction}\nExpiry: ${expiry}\nConfidence: ${confidence || 'N/A'}%`;
      bot.sendMessage(TELEGRAM_CHAT_ID, text, {
        reply_markup: {
          inline_keyboard: [[
            { text: '✅ Confirm & Trade', callback_data: `trade_${trade.id}` },
            { text: '❌ Cancel', callback_data: `cancel_${trade.id}` }
          ]]
        }
      });

      // Optionally auto-trigger without confirmation (remove this block if confirmation preferred)
      // triggerMacro(trade);

      res.status(200).send('Trade signal received');
    }
  );
});

// === Telegram Bot Commands ===
bot.onText(/\/start/, msg => {
  bot.sendMessage(msg.chat.id, `👋 Welcome ${msg.from.first_name}! Use /trade to log trades.`);
});

// Manual /trade command for users to log trades
bot.onText(/\/trade/, msg => {
  const chatId = msg.chat.id;
  bot.sendMessage(chatId, '📈 Enter trade details (e.g. EUR/USD BUY 1m):', {
    reply_markup: { force_reply: true }
  }).then(() => {
    bot.once('message', reply => {
      const [pair, direction, expiry] = reply.text.trim().split(/\s+/);
      if (!isValidPair(pair)) {
        return bot.sendMessage(chatId, `❌ Invalid pair: ${pair}`);
      }
      db.run('INSERT INTO trades (pair, direction, expiry, result) VALUES (?, ?, ?, NULL)', [pair.toUpperCase(), direction.toUpperCase(), expiry], function (err) {
        if (err) return bot.sendMessage(chatId, '❌ DB error saving trade.');
        const tradeId = this.lastID;
        const text = `🆕 Trade #${tradeId}\nPair: ${pair}\nDirection: ${direction}\nExpiry: ${expiry}`;
        bot.sendMessage(chatId, text, {
          reply_markup: {
            inline_keyboard: [[
              { text: '✅ Win', callback_data: `win_${tradeId}` },
              { text: '❌ Loss', callback_data: `loss_${tradeId}` }
            ]]
          }
        });
      });
    });
  });
});

// Update trade result via inline buttons
bot.on('callback_query', query => {
  const chatId = query.message.chat.id;
  const [action, tradeId] = query.data.split('_');

  if (action === 'win' || action === 'loss') {
    db.run('UPDATE trades SET result = ? WHERE id = ?', [action, tradeId], err => {
      if (err) {
        bot.answerCallbackQuery(query.id, { text: 'Error updating result' });
      } else {
        bot.editMessageReplyMarkup({ inline_keyboard: [] }, { chat_id: chatId, message_id: query.message.message_id });
        bot.sendMessage(chatId, `🎯 Trade #${tradeId} marked as ${action.toUpperCase()}`);
        bot.answerCallbackQuery(query.id);
      }
    });
  } else if (action === 'trade') {
    // Confirm trade execution - trigger UI.Vision macro
    db.get('SELECT * FROM trades WHERE id = ?', [tradeId], (err, row) => {
      if (err || !row) {
        bot.answerCallbackQuery(query.id, { text: 'Trade not found' });
        return;
      }
      triggerMacro(row);
      bot.editMessageReplyMarkup({ inline_keyboard: [] }, { chat_id: chatId, message_id: query.message.message_id });
      bot.sendMessage(chatId, `🚀 Executing trade #${tradeId}...`);
      bot.answerCallbackQuery(query.id);
    });
  } else if (action === 'cancel') {
    bot.editMessageReplyMarkup({ inline_keyboard: [] }, { chat_id: chatId, message_id: query.message.message_id });
    bot.sendMessage(chatId, `❌ Trade #${tradeId} cancelled.`);
    bot.answerCallbackQuery(query.id);
  }
});

// Show recent trades and their results
bot.onText(/\/result/, msg => {
  db.all('SELECT * FROM trades ORDER BY timestamp DESC LIMIT 5', (err, rows) => {
    if (err || rows.length === 0) {
      return bot.sendMessage(msg.chat.id, '📭 No recent trades found.');
    }
    const text = rows.map(r => `#${r.id} - ${r.pair} ${r.direction} [${r.expiry}] → ${r.result || 'PENDING'}`).join('\n');
    bot.sendMessage(msg.chat.id, `📊 Recent Trades:\n\n${text}`);
  });
});

// Show daily/overall stats
bot.onText(/\/stats/, msg => {
  calcStats((err, stats) => {
    if (err) {
      return bot.sendMessage(msg.chat.id, 'Error fetching stats');
    }
    const winCount = stats.win || 0;
    const lossCount = stats.loss || 0;
    const pendingCount = stats.PENDING || 0;
    const total = stats.total || 0;
    const winRate = total ? ((winCount / total) * 100).toFixed(2) : '0.00';
    const text = `📈 Trade Stats:\n\nTotal: ${total}\nWins: ${winCount}\nLosses: ${lossCount}\nPending: ${pendingCount}\nWin Rate: ${winRate}%`;
    bot.sendMessage(msg.chat.id, text);
  });
});

bot.onText(/\/settings/, msg => {
  bot.sendMessage(msg.chat.id, `⚙️ Settings coming soon. Want auto-trading, timezones, or trade limits?`);
});

// Start Express server for webhook listening
app.listen(PORT, () => {
  console.log(`🚀 Server listening on port ${PORT}`);
});

