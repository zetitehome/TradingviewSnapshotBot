/**
 * TradingView Snapshot Bot
 * Express + Puppeteer + Telegram Webhook + TradingView Alert Intake
 *
 * Render-ready. Requires:
 *   BOT_TOKEN        = Telegram bot token
 *   ALERT_CHAT_ID    = Chat ID to send auto alerts (optional but recommended)
 *   DEFAULT_EXCHANGE = e.g., FX (optional)
 *   DEFAULT_TICKER   = e.g., EURUSD (optional)
 */

const express    = require('express');
const bodyParser = require('body-parser');
const puppeteer  = require('puppeteer');
const FormData   = require('form-data');        // for sending Telegram photos
const fetch      = global.fetch;                // Node 18+ global

// ────────────────────────────────────────────────────────────
// ENV / CONFIG
// ────────────────────────────────────────────────────────────
const app              = express();
const PORT             = process.env.PORT || 10000;
const BOT_TOKEN        = process.env.BOT_TOKEN || "";
const ALERT_CHAT_ID    = process.env.ALERT_CHAT_ID || ""; // your own Telegram chat for TV auto alerts
const DEFAULT_EXCHANGE = process.env.DEFAULT_EXCHANGE || "FX";
const DEFAULT_TICKER   = process.env.DEFAULT_TICKER   || "EURUSD";
const DEFAULT_THEME    = "dark";
const DEFAULT_INTERVAL = "1"; // minutes unless letter timeframe

if (!BOT_TOKEN) {
  console.warn("⚠ BOT_TOKEN not set. Telegram replies will fail.");
}

app.use(bodyParser.json({ limit: "1mb" }));
app.use(bodyParser.urlencoded({ extended: true }));

// ────────────────────────────────────────────────────────────
// Puppeteer
// ────────────────────────────────────────────────────────────
const chromeOptions = {
  headless: true,
  args: [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    '--disable-accelerated-2d-canvas',
    '--disable-gpu',
    '--no-zygote',
    '--single-process',
    '--window-size=1920x1080'
  ],
};

let browser = null;
let page    = null;

async function ensureBrowser() {
  if (browser && page) return { browser, page };
  console.log('🔄 Launching headless Chromium...');
  browser = await puppeteer.launch(chromeOptions);
  page    = await browser.newPage();
  await page.setUserAgent('Mozilla/5.0 (X11; Linux x86_64)');
  await page.setViewport({ width: 1920, height: 1080 });
  return { browser, page };
}

async function safeCloseBrowser() {
  try {
    if (page) await page.close();
    if (browser) await browser.close();
  } catch (err) {
    console.error('Error closing browser:', err);
  } finally {
    page = null;
    browser = null;
  }
}

['SIGINT', 'SIGTERM'].forEach(sig => {
  process.on(sig, async () => {
    console.log(`\n${sig} received. Closing browser...`);
    await safeCloseBrowser();
    process.exit(0);
  });
});

// ────────────────────────────────────────────────────────────
function buildTradingViewUrl({ base = 'chart', exchange = DEFAULT_EXCHANGE, ticker = DEFAULT_TICKER, interval = DEFAULT_INTERVAL, theme = DEFAULT_THEME }) {
  const hasQuery = base.includes('?');
  const prefix   = hasQuery ? base : `${base}/?`;
  return `https://www.tradingview.com/${prefix}symbol=${encodeURIComponent(`${exchange}:${ticker}`)}&interval=${encodeURIComponent(interval)}&theme=${encodeURIComponent(theme)}`;
}

async function captureTradingView({ exchange, ticker, interval, theme }) {
  const { page } = await ensureBrowser();
  const url = buildTradingViewUrl({ exchange, ticker, interval, theme });
  console.log('📸 Navigating to:', url);
  await page.goto(url, { waitUntil: 'networkidle2', timeout: 45000 });
  await page.waitForTimeout(5000); // chart load settle
  const screenshot = await page.screenshot({ type: 'png' });
  return { screenshot, url };
}

// ────────────────────────────────────────────────────────────
// Telegram Send Helpers
// ────────────────────────────────────────────────────────────
async function telegramSendMessage(chatId, text, extra = {}) {
  if (!BOT_TOKEN) return console.error('BOT_TOKEN not set; cannot send Telegram message.');
  const url  = `https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`;
  const body = { chat_id: chatId, text, ...extra };
  const resp = await fetch(url, {
    method : 'POST',
    headers: { 'Content-Type': 'application/json' },
    body   : JSON.stringify(body)
  });
  if (!resp.ok) {
    console.error('Telegram sendMessage failed:', await resp.text());
  }
}

async function telegramSendPhoto(chatId, pngBuffer, caption = '') {
  if (!BOT_TOKEN) return console.error('BOT_TOKEN not set; cannot send Telegram photo.');
  const url  = `https://api.telegram.org/bot${BOT_TOKEN}/sendPhoto`;
  const form = new FormData();
  form.append('chat_id', String(chatId));
  if (caption) form.append('caption', caption);
  form.append('photo', pngBuffer, { filename: 'snapshot.png', contentType: 'image/png' });
  const resp = await fetch(url, { method: 'POST', body: form });
  if (!resp.ok) {
    console.error('Telegram sendPhoto failed:', await resp.text());
  }
}

// ────────────────────────────────────────────────────────────
// Command Parsers
// ────────────────────────────────────────────────────────────

// /snapshot [SYMBOL] [interval] [theme]
// Accepts EXCHANGE:TICKER or just ticker (defaults exchange=FX)
function parseSnapshotCommand(text) {
  const parts = text.trim().split(/\s+/);
  let exchange = DEFAULT_EXCHANGE;
  let ticker   = DEFAULT_TICKER;
  let interval = DEFAULT_INTERVAL;
  let theme    = DEFAULT_THEME;

  if (parts.length >= 2) {
    if (parts[1].includes(':')) {
      const [ex, tk] = parts[1].split(':');
      if (ex) exchange = ex.toUpperCase();
      if (tk) ticker   = tk.toUpperCase();
    } else {
      ticker = parts[1].toUpperCase();
    }
  }
  if (parts.length >= 3) interval = parts[2];
  if (parts.length >= 4) theme    = parts[3].toLowerCase() === 'light' ? 'light' : 'dark';
  return { exchange, ticker, interval, theme };
}

// /signal SYMBOL DIR EXPIRY [interval] [theme]
// DIR can be CALL/PUT/BUY/SELL
// EXPIRY can be 1m,3m,5m,15m or raw number (minutes)
function parseSignalCommand(text) {
  const parts = text.trim().split(/\s+/);
  // parts[0] == /signal
  if (parts.length < 3) {
    return null; // invalid
  }

  let rawSym   = parts[1]; // may include exchange:
  let dirInput = parts[2];
  let expiry   = parts[3] || '5m';
  let interval = parts[4] || DEFAULT_INTERVAL;
  let theme    = parts[5] ? (parts[5].toLowerCase() === 'light' ? 'light' : 'dark') : DEFAULT_THEME;

  let exchange = DEFAULT_EXCHANGE;
  let ticker   = DEFAULT_TICKER;

  if (rawSym.includes(':')) {
    const [ex, tk] = rawSym.split(':');
    if (ex) exchange = ex.toUpperCase();
    if (tk) ticker   = tk.toUpperCase();
  } else {
    ticker = rawSym.toUpperCase();
  }

  let direction = dirInput.toUpperCase();
  if (direction === 'BUY') direction = 'CALL';
  if (direction === 'SELL') direction = 'PUT';

  return { exchange, ticker, direction, expiry, interval, theme };
}

// ────────────────────────────────────────────────────────────
// TELEGRAM WEBHOOK ROUTE
// ────────────────────────────────────────────────────────────
app.post('/webhook', async (req, res) => {
  // Always acknowledge immediately so Telegram stops retrying
  res.send({ ok: true });

  const update = req.body;
  console.log("Telegram update:", update);

  const msg = update.message;
  if (!msg) return; // ignore edits/callbacks for now

  const chatId = msg.chat?.id;
  const text   = msg.text?.trim();
  if (!chatId || !text) return;

  // /start
  if (text === '/start') {
    await telegramSendMessage(
      chatId,
      '👋 Welcome!\nCommands:\n' +
      '/snapshot [SYMBOL] [interval] [theme]\n' +
      '/signal SYMBOL DIR EXPIRY [interval] [theme]\n' +
      '/help for more.'
    );
    return;
  }

  // /help
  if (text === '/help') {
    await telegramSendMessage(
      chatId,
      'Usage examples:\n' +
      '/snapshot              → default chart\n' +
      '/snapshot EURUSD 5     → 5m EURUSD chart\n' +
      '/snapshot BINANCE:BTCUSDT 15 light\n\n' +
      '/signal EURUSD CALL 5m → signal + screenshot\n' +
      '/signal GBPUSD PUT 3m 1 light\n\n' +
      'DIR: CALL|PUT|BUY|SELL\n' +
      'EXPIRY: 1m|3m|5m|15m (or minutes)\n' +
      'Theme: dark|light'
    );
    return;
  }

  // /snapshot
  if (text.toLowerCase().startsWith('/snapshot')) {
    await telegramSendMessage(chatId, '⏳ Capturing chart...');
    const { exchange, ticker, interval, theme } = parseSnapshotCommand(text);
    try {
      const { screenshot, url } = await captureTradingView({ exchange, ticker, interval, theme });
      const caption = `Snapshot: ${exchange}:${ticker} | TF ${interval} | ${theme}\n${url}`;
      await telegramSendPhoto(chatId, screenshot, caption);
    } catch (err) {
      console.error('Snapshot error:', err);
      await telegramSendMessage(chatId, `❌ Snapshot failed: ${err.message}`);
    }
    return;
  }

  // /signal
  if (text.toLowerCase().startsWith('/signal')) {
    const parsed = parseSignalCommand(text);
    if (!parsed) {
      await telegramSendMessage(chatId, '❌ Invalid /signal. Format: /signal EURUSD CALL 5m [interval] [theme]');
      return;
    }

    const { exchange, ticker, direction, expiry, interval, theme } = parsed;

    await telegramSendMessage(chatId, `📡 Signal received: ${exchange}:${ticker} ${direction} / Exp ${expiry} / TF ${interval} (${theme})\n⏳ capturing chart...`);

    try {
      const { screenshot, url } = await captureTradingView({ exchange, ticker, interval, theme });
      const caption =
        `🔔 SIGNAL\n` +
        `Pair: ${exchange}:${ticker}\n` +
        `Direction: ${direction}\n` +
        `Expiry: ${expiry}\n` +
        `TF: ${interval}\n` +
        `Theme: ${theme}\n` +
        `${url}`;
      await telegramSendPhoto(chatId, screenshot, caption);
    } catch (err) {
      console.error('Signal screenshot error:', err);
      await telegramSendMessage(chatId, `❌ Signal screenshot failed: ${err.message}`);
    }
    return;
  }

  // Default fallback
  await telegramSendMessage(chatId, '❓ Unknown command. Try /help.');
});

// ────────────────────────────────────────────────────────────
// TRADINGVIEW ALERT INTAKE ROUTE
// Use this as the webhook URL in TradingView alerts OR forward via n8n.
// Body JSON expected:
// {
//   "symbol":"FX:EURUSD",
//   "direction":"CALL",
//   "expiry":"5m",
//   "interval":"1",
//   "theme":"dark",
//   "chat_id":"<override chat>",
//   "capture":true
// }
// ────────────────────────────────────────────────────────────
app.post('/tv-alert', async (req, res) => {
  try {
    const {
      symbol,
      direction,
      expiry    = '5m',
      interval  = DEFAULT_INTERVAL,
      theme     = DEFAULT_THEME,
      chat_id   = ALERT_CHAT_ID,
      capture   = true
    } = req.body || {};

    if (!chat_id) {
      res.status(400).json({ ok:false, error:"No chat_id (and ALERT_CHAT_ID not set)" });
      return;
    }

    if (!symbol) {
      res.status(400).json({ ok:false, error:"No symbol in alert" });
      return;
    }

    // Parse symbol
    let exchange = DEFAULT_EXCHANGE;
    let ticker   = DEFAULT_TICKER;
    if (symbol.includes(':')) {
      const [ex, tk] = symbol.split(':');
      if (ex) exchange = ex.toUpperCase();
      if (tk) ticker   = tk.toUpperCase();
    } else {
      ticker = symbol.toUpperCase();
    }

    // Normalize direction
    let dir = direction ? direction.toUpperCase() : '';
    if (dir === 'BUY') dir = 'CALL';
    if (dir === 'SELL') dir = 'PUT';

    // Build base message
    let msg =
      `🔔 TradingView Alert\n` +
      `Pair: ${exchange}:${ticker}\n` +
      (dir ? `Direction: ${dir}\n` : '') +
      `Expiry: ${expiry}\n` +
      `TF: ${interval}`;

    // Send first
    await telegramSendMessage(chat_id, msg);

    // Screenshot?
    if (capture) {
      try {
        const { screenshot, url } = await captureTradingView({ exchange, ticker, interval, theme });
        const caption = msg + `\n${url}`;
        await telegramSendPhoto(chat_id, screenshot, caption);
      } catch (err) {
        console.error('TV alert screenshot error:', err);
        await telegramSendMessage(chat_id, `❌ TV alert screenshot failed: ${err.message}`);
      }
    }

    res.json({ ok: true });
  } catch (err) {
    console.error('Error in /tv-alert:', err);
    res.status(500).json({ ok:false, error: err.message });
  }
});

// ────────────────────────────────────────────────────────────
// EXISTING UTILITY ROUTES
// ────────────────────────────────────────────────────────────

// Manual browser start
app.get('/start-browser', async (req, res) => {
  try {
    await ensureBrowser();
    res.send('✅ Browser started (or already running).');
  } catch (err) {
    console.error(err);
    res.status(500).send('Failed to start browser: ' + err.message);
  }
});

// Screenshot via query params
// /run?exchange=FX&ticker=EURUSD&interval=5&theme=dark
app.get('/run', async (req, res) => {
  const {
    base     = 'chart',
    exchange = DEFAULT_EXCHANGE,
    ticker   = DEFAULT_TICKER,
    interval = DEFAULT_INTERVAL,
    theme    = DEFAULT_THEME
  } = req.query;

  try {
    await ensureBrowser();
    const { screenshot } = await captureTradingView({ exchange, ticker, interval, theme });
    res.set('Content-Type', 'image/png');
    res.send(screenshot);
  } catch (err) {
    console.error(err);
    res.status(500).send('Error taking screenshot: ' + err.message);
  }
});

// Health
app.get('/', (req, res) => {
  res.send('TradingView Snapshot Bot is running. Telegram -> /webhook, TradingView Alerts -> /tv-alert, manual -> /run.');
});

// ────────────────────────────────────────────────────────────
// START SERVER
// ────────────────────────────────────────────────────────────
app.listen(PORT, () => {
  console.log(`✅ App listening on port ${PORT}`);
});
