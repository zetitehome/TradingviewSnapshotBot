// Import necessary modules
const express = require('express'); // Web framework for Node.js
const bodyParser = require('body-parser'); // Middleware to parse incoming request bodies
const axios = require('axios'); // Promise-based HTTP client for the browser and node.js
require('dotenv').config(); // Loads environment variables from a .env file into process.env

// Initialize the Express application
const app = express(); // Main application server
const tvApp = express(); // Dedicated server for TradingView webhooks

// Define port numbers from environment variables or use defaults
const PORT = process.env.PORT || 3000; // Port for the main bot server
const TV_WEBHOOK_PORT = process.env.TV_WEBHOOK_PORT || 8081; // Port for the TradingView webhook server

// Retrieve sensitive information and configuration from environment variables
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN; // Your Telegram bot's API token
const TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID; // The chat ID where Telegram alerts will be sent
const UI_VISION_URL = process.env.UI_VISION_URL; // URL of the UI.Vision RPA software's API endpoint
const UI_VISION_MACRO_NAME = process.env.UI_VISION_MACRO_NAME; // The name of the UI.Vision macro to trigger
// IMPORTANT: This should be a stringified JSON object that UI.Vision expects as parameters.
// Example: '{"symbol": "{symbol}", "interval": "{interval}", "exchange": "{exchange}", "theme": "{theme}"}'
const UI_VISION_MACRO_PARAMS_JSON = process.env.UI_VISION_MACRO_PARAMS_JSON;

// Middleware to parse JSON request bodies for both apps
app.use(bodyParser.json());
tvApp.use(bodyParser.json());

/**
 * Triggers a UI.Vision RPA macro with dynamic parameters.
 * @param {string} symbol - The trading symbol (e.g., EURUSD).
 * @param {string} interval - The chart interval (e.g., 1, 5, 15).
 * @param {string} exchange - The exchange (e.g., FX, NASDAQ).
 * @param {string} theme - The chart theme (e.g., dark, light).
 */
const triggerUIVisionMacro = async (symbol, interval, exchange, theme) => {
  try {
    // Replace placeholders in the macro parameters JSON string with actual values
    // Ensure UI_VISION_MACRO_PARAMS_JSON is correctly defined in your .env file
    if (!UI_VISION_MACRO_PARAMS_JSON) {
      console.error('❌ UI_VISION_MACRO_PARAMS_JSON is not defined in environment variables.');
      return;
    }

    const macroParams = JSON.parse(
      UI_VISION_MACRO_PARAMS_JSON
        .replace(/{symbol}/g, symbol) // Use /g for global replacement
        .replace(/{interval}/g, interval)
        .replace(/{exchange}/g, exchange)
        .replace(/{theme}/g, theme)
    );

    // Send a POST request to the UI.Vision API to trigger the macro
    await axios.post(UI_VISION_URL, {
      macro: UI_VISION_MACRO_NAME,
      params: macroParams
    });

    console.log(`✅ UI.Vision macro "${UI_VISION_MACRO_NAME}" triggered for ${symbol}`);
  } catch (error) {
    console.error('❌ Failed to trigger UI.Vision macro:', error.message);
    // Log the full error if available for debugging
    if (error.response) {
      console.error('UI.Vision Response Data:', error.response.data);
      console.error('UI.Vision Response Status:', error.response.status);
    } else if (error.request) {
      console.error('No response received from UI.Vision:', error.request);
    }
  }
};

/**
 * Sends an alert message to a specified Telegram chat.
 * @param {string} message - The message content to send. Supports Markdown.
 */
const sendTelegramAlert = async (message) => {
  try {
    if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) {
      console.error('❌ Telegram bot token or chat ID is not defined in environment variables.');
      return;
    }
    // Send a POST request to the Telegram Bot API
    await axios.post(`https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`, {
      chat_id: TELEGRAM_CHAT_ID,
      text: message,
      parse_mode: 'Markdown' // Enables Markdown formatting in the message
    });
    console.log(`✉️ Telegram alert sent`);
  } catch (error) {
    console.error('❌ Telegram alert error:', error.message);
    // Log the full error if available for debugging
    if (error.response) {
      console.error('Telegram API Response Data:', error.response.data);
      console.error('Telegram API Response Status:', error.response.status);
    } else if (error.request) {
      console.error('No response received from Telegram API:', error.request);
    }
  }
};

// TradingView Webhook Endpoint
tvApp.post('/webhook', async (req, res) => {
  const data = req.body; // The incoming data from the TradingView webhook

  // Extract data from the webhook payload, providing default values
  const symbol = data.symbol || 'EURUSD';
  const interval = data.interval || process.env.DEFAULT_INTERVAL || '1';
  const exchange = data.exchange || process.env.DEFAULT_EXCHANGE || 'FX';
  const theme = data.theme || process.env.DEFAULT_THEME || 'dark';
  const signal = data.signal || 'BUY'; // The trading signal (e.g., BUY, SELL)

  // Construct the message for the Telegram alert
  const msg = `📊 *Signal Received:*\n• Pair: *${symbol}*\n• Interval: *${interval}m*\n• Signal: *${signal}*\n• Exchange: *${exchange}*`;

  // Send the Telegram alert and trigger the UI.Vision macro concurrently
  await Promise.all([
    sendTelegramAlert(msg),
    triggerUIVisionMacro(symbol, interval, exchange, theme)
  ]);

  // Send a success response back to TradingView
  res.status(200).json({ status: 'ok', message: 'Signal processed.' });
});

// Main application route
app.get('/', (req, res) => {
  res.send('📡 PocketSignal Bot is running!');
});

// Start the main application server
app.listen(PORT, () => {
  console.log(`🚀 Telegram Bot Server running on http://localhost:${PORT}`);
});

// Start the TradingView webhook server
tvApp.listen(TV_WEBHOOK_PORT, () => {
  console.log(`🔗 TradingView Webhook Server running on http://localhost:${TV_WEBHOOK_PORT}/webhook`);
});

// Removed the undefined `startBrowser()` call.
// UI.Vision is expected to be running as a separate service accessible via UI_VISION_URL.
// This server will handle incoming TradingView signals and trigger the UI.Vision macro accordingly.