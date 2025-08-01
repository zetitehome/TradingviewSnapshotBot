/**
 * Telegram Bot for Pocket Option Trade Automation
 * -----------------------------------------------
 * This bot integrates with UI.Vision RPA (via its command-line interface) to automate actions.
 * It retrieves all configuration directly from environment variables.
 */

// === MODULE IMPORTS ===
const { Telegraf } = require('telegraf');
const { exec } = require('child_process'); // Import exec for running shell commands

// === CONFIGURATION ===
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const DEFAULT_TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID;
const UI_VISION_CLI_PATH = process.env.UI_VISION_CLI_PATH || 'kantu-cli'; // Assumes kantu-cli is in your PATH
const UI_VISION_MACRO_NAME = process.env.UI_VISION_MACRO_NAME || 'TradeMacro';
const POCKET_OPTION_USERNAME = process.env.POCKET_OPTION_USERNAME;
const POCKET_OPTION_PASSWORD = process.env.POCKET_OPTION_PASSWORD;

if (!TELEGRAM_BOT_TOKEN) {
  console.error('❌ ERROR: TELEGRAM_BOT_TOKEN is not defined in your environment.');
  process.exit(1);
}
if (!UI_VISION_CLI_PATH || !UI_VISION_MACRO_NAME) {
  console.warn('⚠️ WARNING: UI.Vision CLI configuration is incomplete. UI.Vision calls might fail.');
}
if (!POCKET_OPTION_USERNAME || !POCKET_OPTION_PASSWORD) {
  console.warn('⚠️ WARNING: Pocket Option credentials are not fully defined. UI.Vision trades might fail.');
}

const bot = new Telegraf(TELEGRAM_BOT_TOKEN);

// === UTILITY FUNCTIONS ===
const triggerUIVisionMacro = async (macroToRun, params) => {
  if (!UI_VISION_CLI_PATH) {
    console.error('❌ UI.Vision CLI path is not configured. Cannot trigger macro.');
    throw new Error('UI.Vision CLI path missing.');
  }

  // Construct the command-line string
  let command = `${UI_VISION_CLI_PATH} -macro "${macroToRun}"`;

  // Add parameters as -var arguments
  for (const [key, value] of Object.entries(params)) {
    command += ` -var ${key} "${value}"`;
  }

  return new Promise((resolve, reject) => {
    console.log(`🤖 Executing UI.Vision command: ${command}`);
    exec(command, (error, stdout, stderr) => {
      if (error) {
        console.error(`❌ Failed to execute UI.Vision macro "${macroToRun}":`);
        console.error('Error:', error);
        console.error('Stderr:', stderr);
        return reject(new Error(`Failed to execute UI.Vision: ${error.message}`));
      }
      if (stderr) {
        console.warn(`⚠️ UI.Vision macro "${macroToRun}" completed with warnings/errors:`);
        console.warn('Stderr:', stderr);
      }
      console.log(`✅ UI.Vision macro "${macroToRun}" executed successfully.`);
      console.log('Stdout:', stdout);
      resolve(stdout);
    });
  });
};


// === Telegram Commands ===
bot.start((ctx) => {
  ctx.reply(`🎉 Welcome, ${ctx.from.first_name}! I'm your Pocket Option trade bot. Use /help to see my commands.`);
});

bot.command('ping', (ctx) => {
  ctx.reply('🏓 Pong!');
});

bot.command('help', (ctx) => {
  ctx.reply(`📖 Available commands:
/ping - Test if bot is online.
/signal - Show a sample signal.
/analyze - Trigger UI.Vision for chart analysis.
/auto - Enable auto-trading mode.
/trade <pair> <buy|sell> <amount> <expiry_minutes> - Execute a trade.
  Example: /trade EURUSD_OTC buy 100 5`);
});

bot.command('signal', (ctx) => {
  // This is a placeholder. You can integrate your signal generation logic here.
  ctx.reply('📈 New signal: BUY EUR/USD in 1 min (Winrate: 74%)');
});

bot.command('analyze', async (ctx) => {
  ctx.reply('🔍 Analyzing chart, please wait...');
  try {
    const analysisMacroName = 'AnalyzeChart';
    const analysisParams = {
      username: POCKET_OPTION_USERNAME,
      password: POCKET_OPTION_PASSWORD,
    };
    await triggerUIVisionMacro(analysisMacroName, analysisParams);
    ctx.reply('✅ Analysis macro executed successfully via UI.Vision!');
  } catch (err) {
    console.error('Error in /analyze command:', err);
    ctx.reply('❌ Failed to start analysis via UI.Vision. Check server logs.');
  }
});

bot.command('auto', async (ctx) => {
  ctx.reply('🤖 Auto-trading enabled. Watching for signals...');
  try {
    const autoTradeMacroName = 'StartAutoTrade';
    const autoTradeParams = {
      username: POCKET_OPTION_USERNAME,
      password: POCKET_OPTION_PASSWORD,
    };
    await triggerUIVisionMacro(autoTradeMacroName, autoTradeParams);
    ctx.reply('✅ Auto mode activated via UI.Vision!');
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

// The bot.on('text') section is left as-is, but it will now use the new triggerUIVisionMacro function
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
