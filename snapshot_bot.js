/**
 * TradingView Snapshot Bot with Puppeteer + Express + Telegram (Telegraf)
 * ------------------------------------------------------------
 * Features:
 * - /start-browser endpoint (launch Puppeteer)
 * - /run endpoint (fetch TradingView chart screenshot)
 * - /healthz endpoint (check server health)
 * - Telegram bot integration (send chart screenshots on command using Telegraf)
 * - Handles multiple exchanges (FX, OANDA, FX_IDC, etc.)
 * - Improved error handling and logging
 * - Graceful shutdown for Puppeteer browser
 */

// === MODULE IMPORTS ===
const express = require('express'); // Web framework for Node.js
const bodyParser = require('body-parser'); // Middleware to parse incoming request bodies
const fs = require('fs'); // File system module for reading/writing files
const path = require('path'); // Path module for working with file and directory paths
const puppeteer = require('puppeteer'); // Headless Chrome Node.js API
const FormData = require('form-data'); // For building multipart/form-data requests
const { Telegraf } = require('telegraf'); // Telegram Bot API framework
require('dotenv').config(); // Loads environment variables from a .env file into process.env

// === CONFIGURATION ===
const PORT = process.env.PORT || 10000; // Port for the main Express server
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN; // Your Telegram bot's API token
const TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID; // Default chat ID for alerts (can be overridden by Telegram command context)
const TRADINGVIEW_BASE_URL = 'https://www.tradingview.com/chart/?symbol='; // Base URL for TradingView charts

// Exchanges mapping for user-friendly names to TradingView symbols
// Example: 'FX' maps to 'FX:EURUSD' for the default pair.
// When a user specifies 'EURUSD', we will combine it with the chosen exchange.
const EXCHANGES = {
  FX: 'FX', // Forex.com
  OANDA: 'OANDA', // OANDA
  FX_IDC: 'FX_IDC', // FX_IDC
  BINANCE: 'BINANCE', // Binance (for crypto)
  NASDAQ: 'NASDAQ', // NASDAQ (for stocks)
  NYSE: 'NYSE' // NYSE (for stocks)
  // Add more as needed
};

// === APP INITIALIZATION ===
const app = express(); // Initialize Express app
app.use(bodyParser.json()); // Enable JSON body parsing
app.use(bodyParser.urlencoded({ extended: true })); // Enable URL-encoded body parsing

// Initialize Telegraf bot
if (!TELEGRAM_BOT_TOKEN) {
  console.error('❌ ERROR: TELEGRAM_BOT_TOKEN is not defined in your .env file.');
  process.exit(1); // Exit if essential token is missing
}
const bot = new Telegraf(TELEGRAM_BOT_TOKEN);

let browser = null; // Puppeteer browser instance
let page = null; // Puppeteer page instance

// === UTILITY FUNCTIONS ===

/**
 * Initializes or returns the existing Puppeteer browser instance.
 * Ensures only one browser instance is running.
 * @returns {Promise<puppeteer.Browser>} The Puppeteer browser instance.
 */
async function startBrowser() {
  if (browser) {
    console.log('[Puppeteer] Browser already launched, reusing existing instance.');
    return browser;
  }

  try {
    browser = await puppeteer.launch({
      headless: 'new', // Use 'new' for the new headless mode, or true for old headless
      args: [
        '--no-sandbox', // Required for Docker/CI environments
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage', // Overcomes limited resource problems in Docker
        '--disable-accelerated-2d-canvas',
        '--no-first-run',
        '--no-zygote',
        '--single-process', // Use if --no-sandbox is not enough.
        '--disable-gpu' // Disable GPU hardware acceleration
      ]
    });
    console.log('[Puppeteer] Browser launched successfully.');
    return browser;
  } catch (error) {
    console.error('❌ [Puppeteer] Failed to launch browser:', error);
    throw new Error('Failed to launch Puppeteer browser.');
  }
}

/**
 * Initializes or returns the existing Puppeteer page instance.
 * Creates a new page if none exists or if the current one is closed.
 * @returns {Promise<puppeteer.Page>} The Puppeteer page instance.
 */
async function getOrCreatePage() {
  if (!browser) {
    await startBrowser();
  }
  if (!page || page.isClosed()) {
    page = await browser.newPage();
    // Set a default viewport size for consistency
    await page.setViewport({ width: 1280, height: 720 });
    console.log('[Puppeteer] New page created.');
  } else {
    console.log('[Puppeteer] Reusing existing page.');
  }
  return page;
}

/**
 * Captures a screenshot of a TradingView chart.
 * @param {string} ticker - The trading pair ticker (e.g., EURUSD, BTCUSD).
 * @param {string} [interval='1'] - The chart interval (e.g., '1', '5', '60', 'D', 'W').
 * @param {string} [exchange='FX'] - The exchange prefix (e.g., 'FX', 'OANDA', 'BINANCE').
 * @param {string} [theme='dark'] - The chart theme ('dark' or 'light').
 * @returns {Promise<string>} The path to the saved screenshot.
 */
async function captureChartScreenshot(ticker, interval = '1', exchange = 'FX', theme = 'dark') {
  try {
    const currentPage = await getOrCreatePage();

    // Construct the full TradingView symbol
    const fullSymbol = `${EXCHANGES[exchange] || exchange}:${ticker}`;
    // Construct the URL with symbol and interval
    const url = `${TRADINGVIEW_BASE_URL}${fullSymbol}&interval=${interval}`;
    console.log(`[Snapshot] Navigating to ${url} ...`);

    // Navigate to the URL and wait for the network to be idle
    await currentPage.goto(url, { waitUntil: 'networkidle2', timeout: 60000 }); // 60 seconds timeout

    // Wait for the chart to visibly load. This selector targets the main chart canvas.
    // Adjust selector if TradingView changes its structure.
    await currentPage.waitForSelector('.chart-widget-popup-content', { visible: true, timeout: 30000 });
    console.log('[Snapshot] Chart widget content visible.');

    // Change theme (light/dark) by injecting JavaScript
    // TradingView often uses data-theme attributes or specific classes on the body/html
    try {
      await currentPage.evaluate((selectedTheme) => {
        const body = document.querySelector('body');
        if (body) {
          if (selectedTheme === 'light') {
            body.setAttribute('data-theme', 'light');
            body.classList.remove('theme-dark'); // Remove dark theme class if present
            body.classList.add('theme-light'); // Add light theme class
          } else {
            body.setAttribute('data-theme', 'dark');
            body.classList.remove('theme-light'); // Remove light theme class if present
            body.classList.add('theme-dark'); // Add dark theme class
          }
        }
        // Also try to click the theme button if it exists and is visible
        const themeButton = document.querySelector('.js-theme-button, [data-qa-id="theme-switcher"]');
        if (themeButton && themeButton.getAttribute('data-theme') !== selectedTheme) {
          themeButton.click(); // Simulate click to change theme
        }
      }, theme);
      console.log(`[Theme] Switched to ${theme} theme.`);
      await currentPage.waitForTimeout(1000); // Give a moment for theme change to apply
    } catch (e) {
      console.warn(`[Theme] Unable to switch theme to ${theme}:`, e.message);
    }

    const screenshotPath = path.join(__dirname, 'snapshot.png');
    await currentPage.screenshot({ path: screenshotPath, fullPage: false }); // fullPage: false for just viewport
    console.log(`[Snapshot] Saved to ${screenshotPath}`);
    return screenshotPath;
  } catch (error) {
    console.error(`❌ [Snapshot] Failed to capture screenshot for ${ticker}:`, error);
    throw new Error(`Failed to capture chart screenshot: ${error.message}`);
  }
}

/**
 * Sends a photo to Telegram using the bot API.
 * @param {string} photoPath - The local path to the photo file.
 * @param {string} caption - The caption for the photo.
 * @param {number} chatId - The Telegram chat ID to send the photo to.
 * @returns {Promise<void>}
 */
async function sendToTelegram(photoPath, caption = 'TradingView Snapshot', chatId) {
  if (!TELEGRAM_BOT_TOKEN) {
    console.error('[Telegram] Missing bot token. Cannot send photo.');
    return;
  }
  if (!chatId) {
    console.error('[Telegram] Missing chat ID. Cannot send photo.');
    return;
  }

  const url = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendPhoto`;
  const form = new FormData();
  form.append('chat_id', chatId);
  form.append('caption', caption);
  form.append('photo', fs.createReadStream(photoPath));

  return new Promise((resolve, reject) => {
    // Use form.submit for Node.js's native http/https module
    form.submit(url, (err, res) => {
      if (err) {
        console.error('❌ [Telegram] Error sending photo:', err);
        return reject(err);
      }

      let responseBody = '';
      res.on('data', (chunk) => {
        responseBody += chunk;
      });
      res.on('end', () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          console.log('[Telegram] Photo sent successfully.');
          resolve();
        } else {
          const errorMsg = `[Telegram] Failed to send photo. Status: ${res.statusCode}, Response: ${responseBody}`;
          console.error(errorMsg);
          reject(new Error(errorMsg));
        }
      });
      res.on('error', (e) => {
        console.error('❌ [Telegram] Response stream error:', e);
        reject(e);
      });
    });
  });
}

/**
 * Closes the Puppeteer browser instance.
 */
async function closeBrowser() {
  if (browser) {
    await browser.close();
    browser = null;
    page = null;
    console.log('[Puppeteer] Browser closed.');
  }
}

// === EXPRESS ROUTES (for external triggers/monitoring) ===

// Health check endpoint
app.get('/healthz', (req, res) => {
  res.status(200).json({ status: 'ok', uptime: process.uptime(), browserStatus: browser ? 'open' : 'closed' });
});

// Endpoint to explicitly start the browser (useful for pre-warming)
app.get('/start-browser', async (req, res) => {
  try {
    await startBrowser();
    await getOrCreatePage(); // Ensure a page is also ready
    res.status(200).json({ message: 'Puppeteer browser and page started.' });
  } catch (err) {
    console.error('[Error] /start-browser:', err);
    res.status(500).json({ error: err.message });
  }
});

// Endpoint to capture a screenshot and serve it directly
app.get('/run', async (req, res) => {
  try {
    const { ticker = 'EURUSD', interval = '1', exchange = 'FX', theme = 'dark' } = req.query;
    const screenshotPath = await captureChartScreenshot(ticker, interval, exchange, theme);
    res.sendFile(screenshotPath, {}, (err) => {
      if (err) {
        console.error('❌ [Error] Sending file:', err);
        res.status(500).send('Error sending screenshot file.');
      } else {
        // Optionally delete the file after sending
        fs.unlink(screenshotPath, (unlinkErr) => {
          if (unlinkErr) console.error('❌ [Cleanup] Error deleting screenshot file:', unlinkErr);
        });
      }
    });
  } catch (err) {
    console.error('[Error] /run:', err);
    res.status(500).json({ error: err.message });
  }
});

// Endpoint to capture a screenshot and send it to Telegram (e.g., from a TradingView webhook)
app.post('/signal', async (req, res) => {
  try {
    const { ticker = 'EURUSD', interval = '1', exchange = 'FX', theme = 'dark', caption, chat_id } = req.body;
    const targetChatId = chat_id || TELEGRAM_CHAT_ID; // Use provided chat_id or default

    if (!targetChatId) {
      return res.status(400).json({ status: 'error', message: 'Telegram Chat ID not provided or configured.' });
    }

    const screenshotPath = await captureChartScreenshot(ticker, interval, exchange, theme);
    await sendToTelegram(screenshotPath, caption || `📊 Signal for ${exchange}:${ticker} (${interval}m)`, targetChatId);

    // Clean up the screenshot file after sending
    fs.unlink(screenshotPath, (err) => {
      if (err) console.error('❌ [Cleanup] Error deleting screenshot file after signal:', err);
    });

    res.status(200).json({ status: 'signal sent' });
  } catch (err) {
    console.error('[Error] /signal:', err);
    res.status(500).json({ error: err.message });
  }
});

// === TELEGRAM BOT COMMAND HANDLERS (using Telegraf) ===

// /start command
bot.start((ctx) => {
  ctx.reply(`👋 Welcome, ${ctx.from.first_name}! I'm your TradingView Snapshot Bot.
Use /help to see available commands.`);
});

// /help command
bot.help((ctx) => {
  ctx.reply(`📚 Available commands:
/snapshot [ticker] [interval] [exchange] [theme] - Get a chart snapshot.
  Examples:
  /snapshot EURUSD 5 FX dark
  /snapshot BTCUSD 15 BINANCE light
  /snapshot SPX 60 NASDAQ
  (Defaults: EURUSD, 1m, FX, dark)
/ping - Check if the bot is online.
/healthz - Check server health.
`);
});

// /ping command
bot.command('ping', (ctx) => {
  ctx.reply('🏓 Pong!');
});

// /healthz command (triggers the Express endpoint and reports status)
bot.command('healthz', async (ctx) => {
  try {
    const response = await fetch(`http://localhost:${PORT}/healthz`); // Assuming bot is running on same host
    const data = await response.json();
    ctx.reply(`✅ Server Status: ${data.status}\nUptime: ${data.uptime.toFixed(2)} seconds\nBrowser: ${data.browserStatus}`);
  } catch (error) {
    console.error('❌ [Telegram] Health check failed:', error);
    ctx.reply('❌ Server health check failed. The server might not be running or accessible.');
  }
});

// /snapshot command handler
bot.command('snapshot', async (ctx) => {
  const args = ctx.message.text.split(' ').slice(1); // Get arguments after /snapshot
  let ticker = args[0] || 'EURUSD';
  let interval = args[1] || '1';
  let exchange = args[2] || 'FX';
  let theme = args[3] || 'dark';

  // Basic validation for exchange
  if (!Object.keys(EXCHANGES).includes(exchange.toUpperCase())) {
    ctx.reply(`⚠️ Invalid exchange: "${exchange}". Supported exchanges: ${Object.keys(EXCHANGES).join(', ')}. Defaulting to FX.`);
    exchange = 'FX';
  } else {
    exchange = exchange.toUpperCase(); // Normalize exchange to uppercase
  }

  // Basic validation for theme
  if (!['dark', 'light'].includes(theme.toLowerCase())) {
    ctx.reply(`⚠️ Invalid theme: "${theme}". Supported themes: "dark" or "light". Defaulting to dark.`);
    theme = 'dark';
  } else {
    theme = theme.toLowerCase(); // Normalize theme to lowercase
  }

  ctx.reply(`📸 Taking snapshot of ${exchange}:${ticker} (${interval}m, ${theme} theme)...`);

  try {
    const screenshotPath = await captureChartScreenshot(ticker, interval, exchange, theme);
    await sendToTelegram(screenshotPath, `📊 ${exchange}:${ticker} (${interval}m, ${theme} theme)`, ctx.chat.id);

    // Clean up the screenshot file after sending
    fs.unlink(screenshotPath, (err) => {
      if (err) console.error('❌ [Cleanup] Error deleting screenshot file after command:', err);
    });
  } catch (error) {
    console.error('❌ [Telegram] Error processing snapshot command:', error);
    ctx.reply(`❌ Failed to get snapshot for ${ticker}. Error: ${error.message}`);
  }
});

// === START SERVERS ===

// Start the Express server
app.listen(PORT, async () => {
  console.log(`🚀 Express server running on http://localhost:${PORT}`);
  // Optionally start the Puppeteer browser when the server starts
  try {
    await startBrowser();
    await getOrCreatePage(); // Ensure a page is ready
    console.log('✅ Puppeteer browser and page pre-initialized.');
  } catch (err) {
    console.error('❌ Failed to pre-initialize Puppeteer:', err);
  }
});

// Start the Telegram bot polling
bot.launch()
  .then(() => console.log('🤖 Telegram bot started (polling mode).'))
  .catch((err) => console.error('❌ Failed to start Telegram bot:', err));

// Enable graceful stop for Telegram bot
process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));

// === GRACEFUL SHUTDOWN FOR PUPPETEER ===
// Ensure Puppeteer browser is closed on process exit
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
  console.error('❌ [Process] Uncaught Exception:', err);
  await closeBrowser();
  process.exit(1);
});
/**
 * This code sets up a TradingView Snapshot Bot that captures chart screenshots
 * and sends them via Telegram. It uses Puppeteer for browser automation and Telegraf
 * for Telegram bot interactions.
 * 
 * The bot listens for commands to capture snapshots of trading charts, supports multiple exchanges,
 * and allows users to specify themes and intervals.
 */