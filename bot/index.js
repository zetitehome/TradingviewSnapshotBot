/**
 * Telegram Bot for Pocket Option Trade Automation with Enhanced UI
 * -----------------------------------------------
 * This bot integrates with UI.Vision RPA to automate actions on Pocket Option.
 * It now features a more user-friendly interface with inline keyboards and Markdown.
 */

// === MODULE IMPORTS ===
const { Telegraf, Markup } = require('telegraf');
const axios = require('axios');

// === CONFIGURATION ===
// Retrieve sensitive information and configuration from environment variables
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const UI_VISION_URL = process.env.UI_VISION_URL;
const UI_VISION_MACRO_NAME = process.env.UI_VISION_MACRO_NAME;
const POCKET_OPTION_USERNAME = process.env.POCKET_OPTION_USERNAME;
const POCKET_OPTION_PASSWORD = process.env.POCKET_OPTION_PASSWORD;

// Validate essential environment variables
if (!TELEGRAM_BOT_TOKEN) {
  console.error('❌ ERROR: TELEGRAM_BOT_TOKEN is not defined in your environment.');
  process.exit(1);
}
if (!UI_VISION_URL || !UI_VISION_MACRO_NAME) {
  console.warn('⚠️ WARNING: UI.Vision configuration is incomplete. UI.Vision calls might fail.');
}
if (!POCKET_OPTION_USERNAME || !POCKET_OPTION_PASSWORD) {
  console.warn('⚠️ WARNING: Pocket Option credentials are not fully defined. UI.Vision trades might fail.');
}

// Initialize the Telegraf bot
const bot = new Telegraf(TELEGRAM_BOT_TOKEN);

// === BOT MIDDLEWARE ===
// This middleware logs all incoming messages and commands for debugging.
bot.use((ctx, next) => {
  console.log(`[${new Date().toISOString()}] Received message from @${ctx.from.username} (${ctx.from.id}): "${ctx.message?.text || 'Non-text message'}"`);
  return next(); // Pass control to the next middleware or command handler.
});

// === UTILITY FUNCTIONS ===

/**
 * Triggers a UI.Vision RPA macro with dynamic parameters.
 * @param {string} macroToRun - The specific macro name to run.
 * @param {Object} params - An object containing all parameters for the macro.
 */
const triggerUIVisionMacro = async (macroToRun, params) => {
  if (!UI_VISION_URL) {
    console.error('❌ UI.Vision URL is not configured. Cannot trigger macro.');
    throw new Error('UI.Vision URL missing.');
  }

  try {
    const payload = { macro: macroToRun, params: params };
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
  ctx.replyWithMarkdown(`👋 Hello, ${ctx.from.first_name}!
I'm your **Pocket Option Trading Bot**.
Use the commands below to control me:
- /help for a list of commands.
- /trade to execute a trade.`);
});

bot.command('help', (ctx) => {
  ctx.replyWithMarkdown(`📖 **Available commands:**
\`\`\`
/ping
/signal
/analyze
/auto
/trade <pair> <buy|sell> <amount> <expiry_minutes>
\`\`\`
**Example:** \`/trade EURUSD_OTC buy 100 5\``);
});

bot.command('ping', (ctx) => ctx.reply('🏓 Pong!'));

bot.command('signal', (ctx) => {
  ctx.replyWithMarkdown('📈 **New Signal:**\nBUY *EUR/USD* in 1 min (Winrate: 74%)');
});

bot.command('analyze', async (ctx) => {
  ctx.reply('🔍 Analyzing chart...');
  try {
    const analysisMacroName = 'AnalyzeChart' || UI_VISION_MACRO_NAME;
    await triggerUIVisionMacro(analysisMacroName, { username: POCKET_OPTION_USERNAME, password: POCKET_OPTION_PASSWORD });
    ctx.reply('✅ Analysis started via UI.Vision.');
  } catch (err) {
    console.error('Error in /analyze command:', err);
    ctx.reply('❌ Failed to start analysis. Check server logs.');
  }
});

bot.command('auto', async (ctx) => {
  ctx.reply('🤖 Auto-trading enabled. Watching for signals...');
  try {
    const autoTradeMacroName = 'StartAutoTrade' || UI_VISION_MACRO_NAME;
    await triggerUIVisionMacro(autoTradeMacroName, { username: POCKET_OPTION_USERNAME, password: POCKET_OPTION_PASSWORD });
    ctx.reply('✅ Auto mode activated via UI.Vision.');
  } catch (err) {
    console.error('Error in /auto command:', err);
    ctx.reply('❌ Error enabling auto mode. Check server logs.');
  }
});

bot.command('trade', async (ctx) => {
  const args = ctx.message.text.split(' ').slice(1);
  if (args.length !== 4) {
    return ctx.reply('⚠️ **Usage:** \`/trade <pair> <buy|sell> <amount> <expiry_minutes>\`\nExample: \`/trade EURUSD_OTC buy 100 5\`');
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

  // Store trade details in the context for the callback query
  ctx.session = {
    pair: pair.toUpperCase(),
    direction: direction.toLowerCase(),
    amount: amount,
    expiry: expiry
  };

  // Create inline keyboard buttons for confirmation
  const keyboard = Markup.inlineKeyboard([
    Markup.button.callback('✅ Execute Trade', `confirm_${pair.toUpperCase()}_${direction.toLowerCase()}_${amount}_${expiry}`),
    Markup.button.callback('❌ Cancel', 'cancel_trade')
  ]);

  await ctx.replyWithMarkdown(`🟢 **Trade Confirmation:**
Pair: *${pair.toUpperCase()}*
Direction: *${direction.toUpperCase()}*
Amount: *$${amount}*
Expiry: *${expiry} minutes*`, keyboard);
});

// === CALLBACK QUERY HANDLERS ===
bot.action(/confirm_/, async (ctx) => {
  await ctx.answerCbQuery('Executing trade...');
  await ctx.editMessageText('✅ Executing trade... please wait.');

  const parts = ctx.callbackQuery.data.split('_');
  const [, , pair, direction, amountStr, expiryStr] = parts;
  const amount = parseFloat(amountStr);
  const expiry = parseInt(expiryStr);

  try {
    const tradeParams = {
      symbol: pair,
      direction: direction,
      amount: amount,
      expiry: expiry,
      username: POCKET_OPTION_USERNAME,
      password: POCKET_OPTION_PASSWORD
    };
    await triggerUIVisionMacro(UI_VISION_MACRO_NAME, tradeParams);
    await ctx.editMessageText(`✅ **Trade Executed!**\n*${pair}* ${direction.toUpperCase()} *$${amount}* for *${expiry}m*.`);
  } catch (err) {
    console.error('Error executing trade macro:', err);
    await ctx.editMessageText(`❌ Failed to execute trade macro: ${err.message}. Check server logs.`);
  }
});

bot.action('cancel_trade', async (ctx) => {
  await ctx.answerCbQuery('Trade cancelled.');
  await ctx.editMessageText('🛑 Trade cancelled.');
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
