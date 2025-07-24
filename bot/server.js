require('dotenv').config();
const express = require('express');
const bodyParser = require('body-parser');
const axios = require('axios');

const app = express();
app.use(bodyParser.json());

// === ENV VARIABLES ===
const TELEGRAM_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const CHAT_ID = process.env.TELEGRAM_CHAT_ID;
const SNAPSHOT_BASE_URL = process.env.SNAPSHOT_BASE_URL || 'http://localhost:10000';
const UI_VISION_URL = process.env.UI_VISION_URL;
const MACRO_NAME = process.env.UI_VISION_MACRO_NAME || 'PocketTrade';
const MACRO_PARAMS_JSON = JSON.parse(process.env.UI_VISION_MACRO_PARAMS_JSON || '{}');

// === DEFAULT SETTINGS ===
const defaultExchange = process.env.DEFAULT_EXCHANGE || 'FX';
const defaultInterval = process.env.DEFAULT_INTERVAL || '1';
const defaultTheme = process.env.DEFAULT_THEME || 'dark';

let autoTradeEnabled = false;

// === PAIR CONFIG ===
const pairConfigs = {
  'EUR/USD': { expiry: 3, strategy: 'EMA+Candle' },
  'GBP/USD': { expiry: 5, strategy: 'RSI+Pinbar' },
  'OTC_EURUSD': { expiry: 1, strategy: 'SMA+Doji' },
};

// === UTILITIES ===
const sendTelegram = async (msg) => {
  try {
    await axios.post(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage`, {
      chat_id: CHAT_ID,
      text: msg,
      parse_mode: 'HTML',
    });
  } catch (err) {
    console.error('Telegram error:', err.message);
  }
};

const sendTradeToUIVision = async (symbol, interval, source = 'Manual') => {
  const config = pairConfigs[symbol] || { expiry: 3, strategy: 'Default' };
  const payload = {
    symbol,
    interval,
    expiry: config.expiry,
    strategy: config.strategy,
    source,
    exchange: defaultExchange,
    theme: defaultTheme,
  };

  const finalMacroParams = {
    ...MACRO_PARAMS_JSON,
    ...payload,
  };

  try {
    await axios.post(`${UI_VISION_URL}`, {
      macro: MACRO_NAME,
      parameters: finalMacroParams,
    });

    await sendTelegram(`✅ Trade sent for <b>${symbol}</b> (Strategy: <i>${payload.strategy}</i>, Source: <i>${source}</i>, Expiry: <i>${payload.expiry}m</i>)`);
  } catch (err) {
    console.error('UI.Vision error:', err.message);
    await sendTelegram(`❌ <b>Trade failed</b> for <b>${symbol}</b>: ${err.message}`);
    retryTrade(payload); // Retry on fail
  }
};

const retryTrade = async (payload) => {
  setTimeout(() => {
    sendTelegram(`🔁 Retrying trade for <b>${payload.symbol}</b>...`);
    sendTradeToUIVision(payload.symbol, payload.interval, payload.source);
  }, 5000);
};

// === TELEGRAM COMMANDS ===
app.post(`/telegram/${TELEGRAM_TOKEN}`, async (req, res) => {
  const msg = req.body.message;
  if (!msg || !msg.text) return res.sendStatus(200);

  const text = msg.text.toLowerCase();
  const from = msg.chat.id;

  if (text.startsWith('/start')) {
    await sendTelegram(`🤖 Bot Ready.\nUse /analyze or /auto ON/OFF`);
  } else if (text.startsWith('/auto')) {
    autoTradeEnabled = text.includes('on');
    await sendTelegram(`🛠️ Auto Trade is now <b>${autoTradeEnabled ? 'ENABLED' : 'DISABLED'}</b>`);
  } else if (text.startsWith('/analyze')) {
    await sendTelegram('🔍 Analyzing best trade opportunity...');
    // Logic to analyze signals
    sendTradeToUIVision('EUR/USD', '1', 'Manual');
  } else if (text.startsWith('/status')) {
    await sendTelegram(`📊 Auto Mode: <b>${autoTradeEnabled}</b>\nDefault: ${defaultExchange}/${defaultInterval}`);
  } else {
    await sendTelegram(`❓ Unknown command: <code>${text}</code>`);
  }

  res.sendStatus(200);
});

// === TRADINGVIEW WEBHOOK ===
app.post('/webhook', async (req, res) => {
  try {
    const { symbol, timeframe, source } = req.body;
    const tf = timeframe || defaultInterval;

    await sendTelegram(`📩 Signal received from ${source || 'TradingView'} for <b>${symbol}</b> on ${tf}min`);
    if (autoTradeEnabled) {
      await sendTradeToUIVision(symbol, tf, source || 'TradingView');
    } else {
      await sendTelegram(`🕹️ Auto mode OFF. Use /auto on to activate.`);
    }

    res.send({ ok: true });
  } catch (err) {
    console.error(err);
    await sendTelegram(`❌ Error processing signal: ${err.message}`);
    res.sendStatus(500);
  }
});

// === STATS EXPORT PLACEHOLDER ===
app.get('/stats', async (req, res) => {
  res.json({
    stats: {
      totalTrades: 12,
      wins: 8,
      losses: 4,
      winRate: '66.6%',
    },
  });
});

// === SERVER START ===
const PORT = process.env.TV_WEBHOOK_PORT || 8081;
app.listen(PORT, () => {
  console.log(`📡 Webhook server running on port ${PORT}`);
});
S