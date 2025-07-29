/**
 * Telegram Bot for Pocket Option Trade Automation
 * -----------------------------------------------
 * This bot integrates with UI.Vision RPA to automate actions on Pocket Option.
 * It uses Telegraf for Telegram bot interactions and loads configurations
 * from environment variables for secure and flexible deployment.
 */

// === MODULE IMPORTS ===
const { Telegraf } = require('telegraf'); // Telegram Bot API framework
const axios = require('axios'); // Promise-based HTTP client for making requests to UI.Vision
require('dotenv').config(); // Loads environment variables from a .env file into process.env

// === CONFIGURATION ===
// Retrieve sensitive information and configuration from environment variables
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN; // Your Telegram bot's API token
const DEFAULT_TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID; // Default chat ID for background alerts (if used)

// UI.Vision RPA Configuration (ensure these are set in your .env file)
const UI_VISION_URL = process.env.UI_VISION_URL; // e.g., http://localhost:3333/api/macros/execute
const UI_VISION_MACRO_NAME = process.env.UI_VISION_MACRO_NAME; // e.g., PocketOptionTrade
// This JSON string defines the parameters expected by your UI.Vision macro.
// It MUST include all variables used in your macro (symbol, direction, amount, expiry, username, password).
const UI_VISION_MACRO_PARAMS_JSON = process.env.UI_VISION_MACRO_PARAMS_JSON;

// Pocket Option Credentials (ensure these are set in your .env file)
const POCKET_OPTION_USERNAME = process.env.POCKET_OPTION_USERNAME;
const POCKET_OPTION_PASSWORD = process.env.POCKET_OPTION_PASSWORD;

// Validate essential environment variables
if (!TELEGRAM_BOT_TOKEN) {
  console.error('❌ ERROR: TELEGRAM_BOT_TOKEN is not defined in your .env file.');
  process.exit(1); // Exit if the bot token is missing
}
if (!UI_VISION_URL || !UI_VISION_MACRO_NAME || !UI_VISION_MACRO_PARAMS_JSON) {
  console.warn('⚠️ WARNING: UI.Vision configuration (UI_VISION_URL, UI_VISION_MACRO_NAME, UI_VISION_MACRO_PARAMS_JSON) is incomplete in your .env file. UI.Vision calls might fail.');
}
if (!POCKET_OPTION_USERNAME || !POCKET_OPTION_PASSWORD) {
  console.warn('⚠️ WARNING: Pocket Option credentials (POCKET_OPTION_USERNAME, POCKET_OPTION_PASSWORD) are not fully defined in your .env file. UI.Vision trades might fail.');
}

// Initialize the Telegraf bot
const bot = new Telegraf(TELEGRAM_BOT_TOKEN);

// === UTILITY FUNCTIONS ===

/**
 * Triggers a UI.Vision RPA macro with dynamic parameters.
 * This function is designed to send commands to a locally running UI.Vision XModule.
 * @param {string} macroToRun - The specific macro name to run (e.g., 'PocketOptionTrade', 'AnalyzeChart').
 * @param {Object} params - An object containing all parameters for the macro.
 * Must include username and password for Pocket Option trades.
 */
const triggerUIVisionMacro = async (macroToRun, params) => {
  if (!UI_VISION_URL) {
    console.error('❌ UI.Vision URL is not configured. Cannot trigger macro.');
    throw new Error('UI.Vision URL missing.');
  }

  try {
    // Construct the payload for UI.Vision
    const payload = {
      macro: macroToRun,
      params: params
    };

    await axios.post(UI_VISION_URL, payload);

    console.log(`✅ UI.Vision macro "${macroToRun}" triggered with params:`, params);
  } catch (error) {
    console.error(`❌ Failed to trigger UI.Vision macro "${macroToRun}":`, error.message);
    // Log the full error if available for debugging
    if (error.response) {
      console.error('UI.Vision Response Data:', error.response.data);
      console.error('UI.Vision Response Status:', error.response.status);
    } else if (error.request) {
      console.error('No response received from UI.Vision:', error.request);
    }
    throw new Error(`Failed to trigger UI.Vision: ${error.message}`); // Re-throw for calling function to catch
  }
};


// === Telegram Commands ===

// === Start Command ===
bot.start((ctx) => {
  ctx.reply(`👋 Welcome, ${ctx.from.first_name}! I'm your Pocket Option trade bot.`);
});

// === Ping Command ===
bot.command('ping', (ctx) => {
  ctx.reply('🏓 Pong!');
});

// === Help Command ===
bot.command('help', (ctx) => {
  ctx.reply(`📖 Available commands:
/ping - Test if bot is online.
/signal - Show a sample signal (placeholder).
/analyze - Trigger UI.Vision for chart analysis (placeholder).
/auto - Enable auto-trading mode (placeholder).
/trade <pair> <buy|sell> <amount> <expiry_minutes> - Execute a trade via UI.Vision.
  Example: /trade EURUSD_OTC buy 100 5`);
});

// === Sample Signal Command ===
bot.command('signal', (ctx) => {
  ctx.reply('📈 New signal: BUY EUR/USD in 1 min (Winrate: 74%)');
});

// === Analyze Command ===
bot.command('analyze', async (ctx) => {
  ctx.reply('🔍 Analyzing chart, please wait...');
  try {
    // This assumes you have a UI.Vision macro specifically for analysis,
    // and that it doesn't require complex parameters or uses dummy ones.
    // If you have a dedicated 'AnalyzeChart' macro, use that name here.
    const analysisMacroName = 'AnalyzeChart' || UI_VISION_MACRO_NAME; // Fallback to main trade macro
    const analysisParams = {
      username: POCKET_OPTION_USERNAME,
      password: POCKET_OPTION_PASSWORD,
      // Add any specific analysis parameters needed by your 'AnalyzeChart' macro
      // For example, if it expects a symbol: symbol: 'EURUSD_OTC'
    };

    await triggerUIVisionMacro(analysisMacroName, analysisParams);
    ctx.reply('✅ Analysis started via UI.Vision (assuming macro is configured).');
  } catch (err) {
    console.error('Error in /analyze command:', err);
    ctx.reply('❌ Failed to start analysis via UI.Vision. Check server logs.');
  }
});

// === Auto-Trading Command ===
bot.command('auto', async (ctx) => {
  ctx.reply('🤖 Auto-trading enabled. Watching for signals...');
  try {
    // This assumes you have a UI.Vision macro for auto-trading.
    // It might just be a trigger to start a loop within UI.Vision.
    const autoTradeMacroName = 'StartAutoTrade' || UI_VISION_MACRO_NAME; // Fallback to main trade macro
    const autoTradeParams = {
      username: POCKET_OPTION_USERNAME,
      password: POCKET_OPTION_PASSWORD,
      // Add any specific auto-trading parameters needed
    };

    await triggerUIVisionMacro(autoTradeMacroName, autoTradeParams);
    ctx.reply('✅ Auto mode activated via UI.Vision (assuming macro is configured).');
  } catch (err) {
    console.error('Error in /auto command:', err);
    ctx.reply('❌ Error enabling auto mode via UI.Vision. Check server logs.');
  }
});

// === /trade Command ===
// Executes a trade via UI.Vision RPA based on user input
bot.command('trade', async (ctx) => {
  const args = ctx.message.text.split(' ').slice(1); // Get arguments after /trade
  if (args.length !== 4) {
    return ctx.reply('⚠️ Usage: /trade <pair> <buy|sell> <amount> <expiry_minutes>\nExample: /trade EURUSD_OTC buy 100 5');
  }

  const [pair, direction, amountStr, expiryStr] = args;
  const amount = parseFloat(amountStr);
  const expiry = parseInt(expiryStr);

  // Basic input validation
  if (!['buy', 'sell'].includes(direction.toLowerCase())) {
    return ctx.reply('⚠️ Invalid direction. Must be "buy" or "sell".');
  }
  if (isNaN(amount) || amount <= 0) {
    return ctx.reply('⚠️ Invalid amount. Must be a positive number.');
  }
  if (isNaN(expiry) || expiry <= 0) {
    return ctx.reply('⚠️ Invalid expiry. Must be a positive integer in minutes.');
  }

  ctx.reply(`🟢 Trade command received: ${pair.toUpperCase()} ${direction.toUpperCase()} $${amount} for ${expiry}m. Attempting to execute via UI.Vision...`);

  try {
    const tradeParams = {
      symbol: pair.toUpperCase(),
      direction: direction.toLowerCase(),
      amount: amount,
      expiry: expiry,
      username: POCKET_OPTION_USERNAME,
      password: POCKET_OPTION_PASSWORD
    };
    await triggerUIVisionMacro(UI_VISION_MACRO_NAME, tradeParams);
    ctx.reply('✅ Trade macro executed successfully via UI.Vision!');
  } catch (err) {
    console.error('Error executing trade macro:', err);
    ctx.reply(`❌ Failed to execute trade macro via UI.Vision: ${err.message}. Check server logs.`);
  }
});

// === Handle Text (for Manual "trade" Keyword) ===
// This listener is less precise than /trade command but can be useful for quick triggers.
// It will try to execute a trade macro with default/dummy values if no specific parameters are given.
bot.on('text', async (ctx) => {
  const message = ctx.message.text.toLowerCase();
  if (message.includes('trade')) {
    ctx.reply('🟢 "Trade" keyword received. Attempting to execute default trade macro via UI.Vision...');
    try {
      // For a simple keyword trigger, you might use default values
      const defaultTradeParams = {
        symbol: 'EURUSD_OTC', // Example default
        direction: 'buy',      // Example default
        amount: 10,            // Example default
        expiry: 1,             // Example default
        username: POCKET_OPTION_USERNAME,
        password: POCKET_OPTION_PASSWORD
      };
      await triggerUIVisionMacro(UI_VISION_MACRO_NAME, defaultTradeParams);
      ctx.reply('✅ Default trade macro executed.');
    } catch (err) {
      console.error('Error executing default trade macro:', err);
      ctx.reply(`❌ Failed to execute default trade macro: ${err.message}.`);
    }
  }
});


// === Launch Bot ===
bot.launch()
  .then(() => console.log('🤖 Telegram Bot is running!'))
  .catch((err) => console.error('❌ Failed to launch Telegram bot:', err));

// === Graceful Shutdown ===
process.once('SIGINT', () => {
  console.log('Received SIGINT. Stopping bot...');
  bot.stop('SIGINT');
  process.exit(0);
});
process.once('SIGTERM', () => {
  console.log('Received SIGTERM. Stopping bot...');
  bot.stop('SIGTERM');
  process.exit(0);
});
