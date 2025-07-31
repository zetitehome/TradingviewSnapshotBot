/**
 * Telegram Bot for Pocket Option Trade Automation
 * -----------------------------------------------
 * This bot integrates with UI.Vision RPA to automate actions on Pocket Option.
 * It now retrieves all configuration directly from environment variables.
 */

// === MODULE IMPORTS ===
const { Telegraf } = require('telegraf'); // Telegram Bot API framework
const axios = require('axios'); // Promise-based HTTP client for making requests to UI.Vision

// === CONFIGURATION ===
// Retrieve sensitive information and configuration from environment variables
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN; "8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA" 
const DEFAULT_TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID; "6637160812"

// UI.Vision RPA Configuration
const UI_VISION_URL = process.env.UI_VISION_URL;
const UI_VISION_MACRO_NAME = process.env.UI_VISION_MACRO_NAME;
const UI_VISION_MACRO_PARAMS_JSON = process.env.UI_VISION_MACRO_PARAMS_JSON;

// Pocket Option Credentials
const POCKET_OPTION_USERNAME = process.env.POCKET_OPTION_USERNAME;
const POCKET_OPTION_PASSWORD = process.env.POCKET_OPTION_PASSWORD;

// Validate essential environment variables
if (!TELEGRAM_BOT_TOKEN) {
  console.error('❌ ERROR: TELEGRAM_BOT_TOKEN is not defined in your environment.');
  process.exit(1); // Exit if the bot token is missing
}
if (!UI_VISION_URL || !UI_VISION_MACRO_NAME) {
  console.warn('⚠️ WARNING: UI.Vision configuration is incomplete. UI.Vision calls might fail.');
}
if (!POCKET_OPTION_USERNAME || !POCKET_OPTION_PASSWORD) {
  console.warn('⚠️ WARNING: Pocket Option credentials are not fully defined. UI.Vision trades might fail.');
}

// Initialize the Telegraf bot
const bot = new Telegraf(TELEGRAM_BOT_TOKEN);

// === UTILITY FUNCTIONS ===

/**
 * Triggers a UI.Vision RPA macro with dynamic parameters.
 * This function is designed to send commands to a locally running UI.Vision XModule.
 * @param {string} macroToRun - The specific macro name to run.
 * @param {Object} params - An object containing all parameters for the macro.
 */
const triggerUIVisionMacro = async (macroToRun, params) => {
  if (!UI_VISION_URL) {
    console.error('❌ UI.Vision URL is not configured. Cannot trigger macro.');
    throw new Error('UI.Vision URL missing.');
  }

  try {
    const payload = {
      macro: macroToRun,
      params: params
    };

    await axios.post(UI_VISION_URL, payload);

    console.log(`✅ UI.Vision macro "${macroToRun}" triggered with params:`, params);
  } catch (error) {
    console.error(`❌ Failed to trigger UI.Vision macro "${macroToRun}":`, error.message);
    if (error.response) {
      console.error('UI.Vision Response Data:', error.response.data);
      console.error('UI.Vision Response Status:', error.response.status);
    } else if (error.request) {
      console.error('No response received from UI.Vision:', error.request);
    }
    throw new Error(`Failed to trigger UI.Vision: ${error.message}`);
  }
};


// === Telegram Commands ===

bot.start((ctx) => {
  ctx.reply(`👋 Welcome, ${ctx.from.first_name}! I'm your Pocket Option trade bot.`);
});

bot.command('ping', (ctx) => {
  ctx.reply('🏓 Pong!');
});

bot.command('help', (ctx) => {
  ctx.reply(`📖 Available commands:
/ping - Test if bot is online.
/signal - Show a sample signal (placeholder).
/analyze - Trigger UI.Vision for chart analysis (placeholder).
/auto - Enable auto-trading mode (placeholder).
/trade <pair> <buy|sell> <amount> <expiry_minutes> - Execute a trade via UI.Vision.
  Example: /trade EURUSD_OTC buy 100 5`);
});

bot.command('signal', (ctx) => {
  ctx.reply('📈 New signal: BUY EUR/USD in 1 min (Winrate: 74%)');
});

bot.command('analyze', async (ctx) => {
  ctx.reply('🔍 Analyzing chart, please wait...');
  try {
    const analysisMacroName = 'AnalyzeChart' || UI_VISION_MACRO_NAME;
    const analysisParams = {
      username: POCKET_OPTION_USERNAME,
      password: POCKET_OPTION_PASSWORD,
    };

    await triggerUIVisionMacro(analysisMacroName, analysisParams);
    ctx.reply('✅ Analysis started via UI.Vision (assuming macro is configured).');
  } catch (err) {
    console.error('Error in /analyze command:', err);
    ctx.reply('❌ Failed to start analysis via UI.Vision. Check server logs.');
  }
});

bot.command('auto', async (ctx) => {
  ctx.reply('🤖 Auto-trading enabled. Watching for signals...');
  try {
    const autoTradeMacroName = 'StartAutoTrade' || UI_VISION_MACRO_NAME;
    const autoTradeParams = {
      username: POCKET_OPTION_USERNAME,
      password: POCKET_OPTION_PASSWORD,
    };

    await triggerUIVisionMacro(autoTradeMacroName, autoTradeParams);
    ctx.reply('✅ Auto mode activated via UI.Vision (assuming macro is configured).');
  } catch (err) {
    console.error('Error in /auto command:', err);
    ctx.reply('❌ Error enabling auto mode via UI.Vision. Check server logs.');
  }
});

bot.command('trade', async (ctx) => {
  const args = ctx.message.text.split(' ').slice(1);
  if (args.length !== 4) {
    return ctx.reply('⚠️ Usage: /trade <pair> <buy|sell> <amount> <expiry_minutes>\nExample: /trade EURUSD_OTC buy 100 5');
  }

  const [pair, direction, amountStr, expiryStr] = args;
  const amount = parseFloat(amountStr);
  const expiry = parseInt(expiryStr);

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

bot.on('text', async (ctx) => {
  const message = ctx.message.text.toLowerCase();
  if (message.includes('trade')) {
    ctx.reply('🟢 "Trade" keyword received. Attempting to execute default trade macro via UI.Vision...');
    try {
      const defaultTradeParams = {
        symbol: 'EURUSD_OTC',
        direction: 'buy',
        amount: 10,
        expiry: 1,
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
