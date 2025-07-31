/**
 * Telegram Bot for Pocket Option Trade Automation with Enhanced UI
 * -----------------------------------------------
 * This bot integrates with UI.Vision RPA to automate actions on Pocket Option.
 * It now features a more user-friendly interface with inline keyboards and Markdown.
 */

// === MODULE IMPORTS ===
// Telegraf is a modern framework for Telegram bots in Node.js
const { Telegraf, Markup } = require('telegraf');
// Axios is a promise-based HTTP client for making API requests
const axios = require('axios');

// === CONFIGURATION ===
// Retrieve sensitive information and configuration from environment variables
// It's a best practice to use environment variables for sensitive data
// and configuration that changes between environments (e.g., development, production).
const config = {
  TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN,
  UI_VISION_URL: process.env.UI_VISION_URL,
  POCKET_OPTION_USERNAME: process.env.POCKET_OPTION_USERNAME,
  POCKET_OPTION_PASSWORD: process.env.POCKET_OPTION_PASSWORD,
  // Define macros here for easy management
  MACROS: {
    TRADE: 'PocketOptionTrade',
    ANALYZE: 'AnalyzeChart',
    AUTO_TRADE: 'StartAutoTrade'
  }
};

// Validate essential environment variables and provide helpful warnings
if (!config.TELEGRAM_BOT_TOKEN) {
  console.error('❌ ERROR: TELEGRAM_BOT_TOKEN is not defined in your environment.');
  process.exit(1);
}
if (!config.UI_VISION_URL) {
  console.warn('⚠️ WARNING: UI_VISION_URL is not configured. UI.Vision calls might fail.');
}
if (!config.POCKET_OPTION_USERNAME || !config.POCKET_OPTION_PASSWORD) {
  console.warn('⚠️ WARNING: Pocket Option credentials are not fully defined. UI.Vision trades might fail.');
}

// === BOT INITIALIZATION ===
// Initialize the Telegraf bot with the token from the configuration
const bot = new Telegraf(config.TELEGRAM_BOT_TOKEN);

// === BOT MIDDLEWARE ===
// Log all incoming messages and commands for debugging purposes
bot.use((ctx, next) => {
  const messageText = ctx.message?.text || 'Non-text message';
  const username = ctx.from?.username || ctx.from?.id;
  console.log(`[${new Date().toISOString()}] Received message from @${username}: "${messageText}"`);
  return next();
});

// === UTILITY FUNCTIONS ===

/**
 * Triggers a UI.Vision RPA macro with dynamic parameters.
 * @param {string} macroToRun - The specific macro name to run.
 * @param {Object} params - An object containing all parameters for the macro.
 */
const triggerUIVisionMacro = async (macroToRun, params) => {
  if (!config.UI_VISION_URL) {
    throw new Error('UI.Vision URL is not configured. Cannot trigger macro.');
  }

  try {
    const payload = { macro: macroToRun, params: params };
    await axios.post(config.UI_VISION_URL, payload, {
      timeout: 30000 // Set a timeout for the UI.Vision request
    });
    console.log(`✅ UI.Vision macro "${macroToRun}" triggered successfully with params:`, params);
  } catch (error) {
    console.error(`❌ Failed to trigger UI.Vision macro "${macroToRun}":`, error.message);
    if (error.response) {
      console.error('UI.Vision Response Data:', error.response.data);
      console.error('UI.Vision Response Status:', error.response.status);
    }
    throw new Error(`Failed to trigger UI.Vision: ${error.message}`);
  }
};

// === COMMAND HANDLERS ===

/**
 * Handles the /start command.
 * Welcomes the user and provides a brief introduction.
 */
bot.start((ctx) => {
  const userName = ctx.from.first_name || 'there';
  ctx.replyWithMarkdown(`👋 Hello, ${userName}!
I'm your **Pocket Option Trading Bot**.
Use the commands below to control me:
- /help for a list of commands.
- /trade to execute a trade.`);
});

/**
 * Handles the /help command.
 * Provides a list of all available commands and an example.
 */
bot.command('help', (ctx) => {
  ctx.replyWithMarkdown(`📖 **Available commands:**
\`\`\`
/ping
/signal
/analyze
/auto
/trade <pair> <buy|sell> <amount> <expiry_minutes>
\`\`\`
**Example:** \`/trade EURUSD_OTC buy 100 5\`
This will ask for confirmation before executing the trade.`);
});

/**
 * Handles the /ping command.
 * A simple command to check if the bot is responsive.
 */
bot.command('ping', (ctx) => ctx.reply('🏓 Pong!'));

/**
 * Handles the /signal command.
 * This is a placeholder for a signal generation feature.
 */
bot.command('signal', (ctx) => {
  ctx.replyWithMarkdown('📈 **New Signal:**\nBUY *EUR/USD* in 1 min (Winrate: 74%)');
});

/**
 * Handles the /analyze command.
 * Triggers the UI.Vision macro to analyze the chart.
 */
bot.command('analyze', async (ctx) => {
  try {
    // Ensure credentials are set before attempting to run the macro
    if (!config.POCKET_OPTION_USERNAME || !config.POCKET_OPTION_PASSWORD) {
      return ctx.reply('⚠️ Cannot perform analysis. Pocket Option credentials are not configured.');
    }
    
    ctx.reply('🔍 Analyzing chart...');
    await triggerUIVisionMacro(config.MACROS.ANALYZE, {
      username: config.POCKET_OPTION_USERNAME,
      password: config.POCKET_OPTION_PASSWORD
    });
    ctx.reply('✅ Analysis started via UI.Vision. Please check the RPA client for status.');
  } catch (err) {
    console.error('Error in /analyze command:', err);
    ctx.reply(`❌ Failed to start analysis: ${err.message}. Please check the server logs.`);
  }
});

/**
 * Handles the /auto command.
 * Triggers the UI.Vision macro to start the auto-trading process.
 */
bot.command('auto', async (ctx) => {
  try {
    // Ensure credentials are set before attempting to run the macro
    if (!config.POCKET_OPTION_USERNAME || !config.POCKET_OPTION_PASSWORD) {
      return ctx.reply('⚠️ Cannot enable auto-trading. Pocket Option credentials are not configured.');
    }
    
    ctx.reply('🤖 Auto-trading enabled. Watching for signals...');
    await triggerUIVisionMacro(config.MACROS.AUTO_TRADE, {
      username: config.POCKET_OPTION_USERNAME,
      password: config.POCKET_OPTION_PASSWORD
    });
    ctx.reply('✅ Auto mode activated via UI.Vision. The bot will now place trades automatically.');
  } catch (err) {
    console.error('Error in /auto command:', err);
    ctx.reply(`❌ Error enabling auto mode: ${err.message}. Please check the server logs.`);
  }
});

/**
 * Handles the /trade command.
 * Parses user input, validates it, and sends a confirmation message with an inline keyboard.
 * The trade details are encoded directly into the callback data. This makes the bot stateless
 * and doesn't require session management.
 */
bot.command('trade', async (ctx) => {
  const args = ctx.message.text.split(' ').slice(1);
  if (args.length !== 4) {
    return ctx.replyWithMarkdown('⚠️ **Usage:** \`/trade <pair> <buy|sell> <amount> <expiry_minutes>\`\nExample: \`/trade EURUSD_OTC buy 100 5\`');
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

  // Encode the trade details into the callback data string
  const callbackData = `confirm_${pair.toUpperCase()}_${direction.toLowerCase()}_${amount}_${expiry}`;
  const keyboard = Markup.inlineKeyboard([
    Markup.button.callback('✅ Execute Trade', callbackData),
    Markup.button.callback('❌ Cancel', 'cancel_trade')
  ]);

  await ctx.replyWithMarkdown(`🟢 **Trade Confirmation:**
Pair: *${pair.toUpperCase()}*
Direction: *${direction.toUpperCase()}*
Amount: *$${amount}*
Expiry: *${expiry} minutes*`, keyboard);
});

// === CALLBACK QUERY HANDLERS ===

/**
 * Handles the 'confirm_...' callback query.
 * This is triggered when the user clicks 'Execute Trade'.
 * It parses the trade details from the callback data and executes the UI.Vision macro.
 */
bot.action(/confirm_/, async (ctx) => {
  // Acknowledge the query to remove the "loading" state from the button
  await ctx.answerCbQuery('Executing trade...');
  
  // Edit the message to show that the trade is being processed
  await ctx.editMessageText('✅ Executing trade... please wait.');

  // Parse the callback data to get the trade parameters
  // The data string will be in the format: "confirm_EURUSD_OTC_buy_100_5"
  const parts = ctx.callbackQuery.data.split('_');
  const [, , pair, direction, amountStr, expiryStr] = parts;
  const amount = parseFloat(amountStr);
  const expiry = parseInt(expiryStr);

  try {
    // Ensure credentials are set before attempting to run the macro
    if (!config.POCKET_OPTION_USERNAME || !config.POCKET_OPTION_PASSWORD) {
      return ctx.editMessageText('⚠️ Cannot execute trade. Pocket Option credentials are not configured.');
    }
    
    const tradeParams = {
      symbol: pair,
      direction: direction,
      amount: amount,
      expiry: expiry,
      username: config.POCKET_OPTION_USERNAME,
      password: config.POCKET_OPTION_PASSWORD
    };
    
    // Trigger the UI.Vision macro
    await triggerUIVisionMacro(config.MACROS.TRADE, tradeParams);
    
    // Send a final success message
    await ctx.editMessageText(`✅ **Trade Executed!**\n*${pair}* ${direction.toUpperCase()} *$${amount}* for *${expiry}m*.`);
  } catch (err) {
    console.error('Error executing trade macro:', err);
    // Send a final failure message
    await ctx.editMessageText(`❌ Failed to execute trade macro: ${err.message}. Please check the server logs.`);
  }
});

/**
 * Handles the 'cancel_trade' callback query.
 * This is triggered when the user clicks 'Cancel'.
 * It simply removes the confirmation message and the inline keyboard.
 */
bot.action('cancel_trade', async (ctx) => {
  await ctx.answerCbQuery('Trade cancelled.');
  // Edit the message to show the cancellation and remove the inline keyboard
  await ctx.editMessageText('🛑 Trade cancelled.', Markup.removeKeyboard());
});

// === BOT LAUNCH ===

// Launch the bot and log a message to the console
bot.launch()
  .then(() => console.log('🤖 Telegram Bot is running!'))
  .catch((err) => console.error('❌ Failed to launch Telegram bot:', err));

// === GRACEFUL SHUTDOWN ===
// These handlers ensure that the bot stops gracefully when the process is terminated
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
