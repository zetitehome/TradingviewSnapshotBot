require('dotenv').config();
const express = require('express');
const bodyParser = require('body-parser');
const puppeteer = require('puppeteer');
const FormData = require('form-data');
const fetch = global.fetch || require('node-fetch');
const path = require('path');
const fs = require('fs');
const TelegramBot = require('node-telegram-bot-api');

const app = express();
const PORT = process.env.PORT || 10000;
const BOT_TOKEN = process.env.BOT_TOKEN || "8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA";
const ALERT_CHAT_ID = process.env.ALERT_CHAT_ID || "6337160812";

if (!BOT_TOKEN) console.warn("⚠️ BOT_TOKEN not set.");

app.use(bodyParser.json({ limit: "2mb" }));
app.use(bodyParser.urlencoded({ extended: true }));
app.use(express.static('public')); // serve static dashboard files from /public

// Puppeteer setup
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
    '--window-size=1920,1080'
  ]
};
let browser = null;
let page = null;

async function ensureBrowser() {
  if (browser && page) return { browser, page };
  browser = await puppeteer.launch(chromeOptions);
  page = await browser.newPage();
  await page.setUserAgent('Mozilla/5.0 (X11; Linux x86_64)');
  await page.setViewport({ width: 1920, height: 1080 });
  return { browser, page };
}

async function safeCloseBrowser() {
  try {
    if (page) await page.close();
    if (browser) await browser.close();
  } catch (e) {
    console.error('Error closing browser:', e);
  } finally {
    browser = null;
    page = null;
  }
}

process.on('SIGINT', async () => {
  console.log('SIGINT received, closing browser...');
  await safeCloseBrowser();
  process.exit();
});

process.on('SIGTERM', async () => {
  console.log('SIGTERM received, closing browser...');
  await safeCloseBrowser();
  process.exit();
});

function buildTradingViewUrl({ exchange = 'FX', ticker = 'EURUSD', interval = '1', theme = 'dark' }) {
  return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(exchange + ':' + ticker)}&interval=${encodeURIComponent(interval)}&theme=${encodeURIComponent(theme)}`;
}

async function captureTradingView({ exchange, ticker, interval, theme }) {
  const { page } = await ensureBrowser();
  const url = buildTradingViewUrl({ exchange, ticker, interval, theme });
  console.log('Capturing URL:', url);
  await page.goto(url, { waitUntil: 'networkidle2', timeout: 60000 });
  await new Promise(res => setTimeout(res, 5000));
  const screenshot = await page.screenshot({ type: 'png' });
  return { screenshot, url };
}

async function telegramSendMessage(chatId, text, extra = {}) {
  if (!BOT_TOKEN) return console.error('BOT_TOKEN not set; cannot send message.');
  const url = `https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`;
  const body = { chat_id: chatId, text, ...extra };
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (!res.ok) {
    console.error('Telegram sendMessage failed:', await res.text());
  }
}

async function telegramSendPhoto(chatId, pngBuffer, caption = '') {
  if (!BOT_TOKEN) return console.error('BOT_TOKEN not set; cannot send photo.');
  const url = `https://api.telegram.org/bot${BOT_TOKEN}/sendPhoto`;
  const form = new FormData();
  form.append('chat_id', String(chatId));
  if (caption) form.append('caption', caption);
  form.append('photo', pngBuffer, { filename: 'snapshot.png', contentType: 'image/png' });
  const res = await fetch(url, { method: 'POST', body: form });
  if (!res.ok) {
    console.error('Telegram sendPhoto failed:', await res.text());
  }
}

// --- Trade Logging (simple file-based for demo) ---
const DATA_PATH = path.resolve(__dirname, 'data');
const TRADES_FILE = path.join(DATA_PATH, 'trades.json');

// Ensure data dir exists
if (!fs.existsSync(DATA_PATH)) fs.mkdirSync(DATA_PATH);

// Load trades from file
function loadTrades() {
  try {
    if (fs.existsSync(TRADES_FILE)) {
      return JSON.parse(fs.readFileSync(TRADES_FILE));
    }
    return [];
  } catch (e) {
    console.error('Error loading trades:', e);
    return [];
  }
}

// Save trades to file
function saveTrades(trades) {
  try {
    fs.writeFileSync(TRADES_FILE, JSON.stringify(trades, null, 2));
    return true;
  } catch (e) {
    console.error('Error saving trades:', e);
    return false;
  }
}

// Add new trade log entry
function addTradeLog(trade) {
  const trades = loadTrades();
  trades.push(trade);
  saveTrades(trades);
}

// Update trade result by timestamp
function updateTradeResult(timestamp, result) {
  const trades = loadTrades();
  const index = trades.findIndex(t => t.timestamp === timestamp);
  if (index === -1) return false;
  trades[index].result = result;
  saveTrades(trades);
  return true;
}

// Calculate stats
function calculateStats() {
  const trades = loadTrades();
  const total = trades.length;
  const wins = trades.filter(t => t.result === 'win').length;
  const losses = trades.filter(t => t.result === 'loss').length;
  const winRate = total > 0 ? ((wins / total) * 100).toFixed(2) : "0";
  return { total, wins, losses, winRate };
}

// === API Endpoints for dashboard ===

// Serve your dashboard.html at /dashboard (assuming you move it to /public/dashboard.html)
app.get('/dashboard', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'dashboard.html'));
});

// Get trade logs JSON
app.get('/logs', (req, res) => {
  res.json(loadTrades());
});

// Get stats JSON
app.get('/stats', (req, res) => {
  res.json(calculateStats());
});

// POST /trade - place new trade from dashboard
app.post('/trade', (req, res) => {
  const { pair, expiry, amount, stopLoss, takeProfit } = req.body;
  if (!pair || !expiry || !amount) {
    res.json({ success: false, message: 'pair, expiry, and amount are required' });
    return;
  }
  const timestamp = Date.now();
  const trade = { pair, expiry, amount, timestamp };
  if (stopLoss) trade.stopLoss = stopLoss;
  if (takeProfit) trade.takeProfit = takeProfit;
  addTradeLog(trade);
  res.json({ success: true, message: 'Trade logged', timestamp });
});

// POST /trade-result - update a trade's result
app.post('/trade-result', (req, res) => {
  const { timestamp, result } = req.body;
  if (!timestamp || !result) {
    res.json({ success: false, message: 'timestamp and result are required' });
    return;
  }
  const success = updateTradeResult(Number(timestamp), result);
  res.json({ success, message: success ? 'Trade updated' : 'Trade not found' });
});

// --- Chart Capture Management (for manual capture button on dashboard) ---
let captureState = { state: 'idle', message: '', lastImagePath: '' };

app.post('/start-capture', async (req, res) => {
  const { url } = req.body;
  if (!url) return res.status(400).json({ error: 'URL required' });
  captureState = { state: 'working', message: 'Capturing...' };

  try {
    await ensureBrowser();
    const { page } = await ensureBrowser();
    await page.goto(url, { waitUntil: 'networkidle2' });
    await new Promise(r => setTimeout(r, 4000));
    const screenshotBuffer = await page.screenshot({ type: 'png' });
    // Save screenshot to public folder with timestamp
    const filename = `capture_${Date.now()}.png`;
    const filepath = path.join(__dirname, 'public', filename);
    fs.writeFileSync(filepath, screenshotBuffer);
    captureState = { state: 'done', message: 'Capture complete', lastImagePath: `/${filename}` };
    res.json({ success: true, message: 'Capture started' });
  } catch (err) {
    captureState = { state: 'error', message: err.message, lastImagePath: '' };
    res.status(500).json({ error: err.message });
  }
});

app.get('/capture-status', (req, res) => {
  res.json(captureState);
});

// --- Telegram Bot Setup ---

const bot = new TelegramBot(BOT_TOKEN, { polling: true });

const botLogic = require('./bot');
botLogic(bot, { addTradeLog, updateTradeResult, telegramSendMessage, telegramSendPhoto, captureTradingView });

app.listen(PORT, () => {
  console.log(`✅ Server listening on port ${PORT}`);
});
