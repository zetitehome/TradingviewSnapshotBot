/**
 * TradingView Snapshot Bot with Puppeteer + Express + Telegram
 * ------------------------------------------------------------
 * Features:
 *  - /start-browser endpoint (launch Puppeteer)
 *  - /run endpoint (fetch TradingView chart screenshot)
 *  - /healthz endpoint (check server health)
 *  - Telegram bot integration (send chart screenshots on command)
 *  - Handles multiple exchanges (FX, OANDA, FX_IDC, etc.)
 *  - Win-rate + signal logging (for extension)
 *  - Clean error handling
 */

const express = require('express');
const bodyParser = require('body-parser');
const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer');
const FormData = require('form-data');
const https = require('https');

// === CONFIG ===
const PORT = process.env.PORT || 10000;
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || '';
const TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID || '';
const BASE_URL = 'https://www.tradingview.com/chart/?symbol=';

// Exchanges mapping for user-friendly names
const EXCHANGES = {
  FX: 'FX:EURUSD',
  FX_IDC: 'FX_IDC:EURUSD',
  OANDA: 'OANDA:EURUSD'
};

// === APP INITIALIZATION ===
const app = express();
app.use(bodyParser.json());
app.use(bodyParser.urlencoded({ extended: true }));

let browser = null;
let page = null;

// === UTILITY FUNCTIONS ===

/**
 * Initialize Puppeteer
 */
async function startBrowser() {
  if (browser) {
    return browser;
  }

  browser = await puppeteer.launch({
    headless: 'new', // or true
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });

  console.log('[Puppeteer] Browser launched.');
  return browser;
}

/**
 * Take a TradingView snapshot
 */
async function captureChartScreenshot(exchange, ticker, interval = 1, theme = 'dark') {
  if (!page) {
    browser = await startBrowser();
    page = await browser.newPage();
  }

  const symbol = `${exchange}:${ticker}`;
  const url = `${BASE_URL}${symbol}`;
  console.log(`[Snapshot] Navigating to ${url} ...`);

  await page.goto(url, { waitUntil: 'networkidle2' });
  await page.waitForTimeout(5000); // Wait for chart to load

  // Change theme (light/dark)
  if (theme === 'light') {
    try {
      await page.evaluate(() => {
        document.querySelector('body').setAttribute('data-theme', 'light');
      });
    } catch (e) {
      console.warn('[Theme] Unable to switch theme:', e.message);
    }
  }

  const screenshotPath = path.join(__dirname, 'snapshot.png');
  await page.screenshot({ path: screenshotPath });
  console.log(`[Snapshot] Saved to ${screenshotPath}`);
  return screenshotPath;
}

/**
 * Send photo to Telegram
 */
async function sendToTelegram(photoPath, caption = 'TradingView Snapshot') {
  if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) {
    console.error('[Telegram] Missing bot token or chat ID.');
    return;
  }

  const url = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendPhoto`;
  const form = new FormData();
  form.append('chat_id', TELEGRAM_CHAT_ID);
  form.append('caption', caption);
  form.append('photo', fs.createReadStream(photoPath));

  return new Promise((resolve, reject) => {
    form.submit(url, (err, res) => {
      if (err) {
        reject(err);
      } else {
        console.log('[Telegram] Photo sent.');
        res.resume();
        resolve();
      }
    });
  });
}

/**
 * Clean up puppeteer browser
 */
async function closeBrowser() {
  if (browser) {
    await browser.close();
    browser = null;
    page = null;
    console.log('[Puppeteer] Browser closed.');
  }
}

// === EXPRESS ROUTES ===

app.get('/healthz', (req, res) => {
  res.status(200).json({ status: 'ok', uptime: process.uptime() });
});

app.get('/start-browser', async (req, res) => {
  try {
    await startBrowser();
    page = await browser.newPage();
    res.status(200).json({ message: 'Browser started.' });
  } catch (err) {
    console.error('[Error] start-browser:', err);
    res.status(500).json({ error: err.message });
  }
});

app.get('/run', async (req, res) => {
  try {
    const { exchange = 'FX', ticker = 'EURUSD', interval = '1', theme = 'dark' } = req.query;
    const screenshotPath = await captureChartScreenshot(exchange, ticker, interval, theme);
    res.sendFile(screenshotPath);
  } catch (err) {
    console.error('[Error] run:', err);
    res.status(500).json({ error: err.message });
  }
});

app.post('/signal', async (req, res) => {
  try {
    const { exchange = 'FX', ticker = 'EURUSD', interval = '1', theme = 'dark', caption } = req.body;
    const screenshotPath = await captureChartScreenshot(exchange, ticker, interval, theme);
    await sendToTelegram(screenshotPath, caption || `Signal for ${exchange}:${ticker}`);
    res.status(200).json({ status: 'signal sent' });
  } catch (err) {
    console.error('[Error] signal:', err);
    res.status(500).json({ error: err.message });
  }
});

// === TELEGRAM COMMAND LISTENER ===
// Optional: simple webhook to receive /snapshot command
app.post('/telegram', async (req, res) => {
  try {
    const message = req.body.message || {};
    const chatId = message.chat?.id;
    const text = message.text || '';

    if (text.startsWith('/snapshot')) {
      const args = text.split(' ').slice(1);
      const ticker = args[0] || 'EURUSD';
      const screenshotPath = await captureChartScreenshot('FX', ticker);
      await sendToTelegram(screenshotPath, `Snapshot of ${ticker}`);
    }
    res.sendStatus(200);
  } catch (err) {
    console.error('[Error] telegram:', err);
    res.sendStatus(500);
  }
});

// === START SERVER ===
app.listen(PORT, () => {
  console.log(`Snapshot server running on http://localhost:${PORT}`);
});

// === GRACEFUL SHUTDOWN ===
process.on('SIGINT', async () => {
  console.log('[Process] SIGINT received, closing browser...');
  await closeBrowser();
  process.exit(0);
});
process.on('SIGTERM', async () => {
  console.log('[Process] SIGTERM received, closing browser...');
  await closeBrowser();
  process.exit(0);
});
// Handle uncaught exceptions
process.on('uncaughtException', async (err) => {
  console.error('[Process] Uncaught Exception:', err);
  await closeBrowser();
  process.exit(1);
});