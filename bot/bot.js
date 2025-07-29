/**
 * Telegram Trade Bot
 * -------------------
 * This bot provides basic functionalities for logging trades,
 * getting trade statistics, and suggesting trades (placeholder logic).
 * It uses Telegraf for Telegram bot interactions.
 */

// === MODULE IMPORTS ===
const { Telegraf } = require('telegraf'); // Telegram Bot API framework
const fs = require('fs'); // File system module for reading/writing files
const path = require('path'); // Path module for working with file and directory paths
require('dotenv').config(); // Loads environment variables from a .env file into process.env

// === CONFIGURATION ===
// Retrieve sensitive information and configuration from environment variables
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN; // Your Telegram bot's API token
const DEFAULT_TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID; // Default chat ID for background alerts

// Validate essential environment variables
if (!TELEGRAM_BOT_TOKEN) {
  console.error('❌ ERROR: TELEGRAM_BOT_TOKEN is not defined in your .env file.');
  process.exit(1); // Exit if the bot token is missing
}

// Initialize the Telegraf bot
const bot = new Telegraf(TELEGRAM_BOT_TOKEN);

// Define path for the trade log file
const TRADE_LOG_DIR = path.join(__dirname, 'logs');
const TRADE_LOG_PATH = path.join(TRADE_LOG_DIR, 'trade-log.json');

// Ensure the logs directory exists
try {
  if (!fs.existsSync(TRADE_LOG_DIR)) {
    fs.mkdirSync(TRADE_LOG_DIR);
    console.log(`✅ Created logs directory: ${TRADE_LOG_DIR}`);
  }
} catch (error) {
  console.error(`❌ Error creating logs directory: ${error.message}`);
  process.exit(1); // Exit if directory cannot be created
}

// Ensure the trade log file exists and is initialized as an empty array
try {
  if (!fs.existsSync(TRADE_LOG_PATH)) {
    fs.writeFileSync(TRADE_LOG_PATH, JSON.stringify([]));
    console.log(`✅ Initialized trade log file: ${TRADE_LOG_PATH}`);
  }
} catch (error) {
  console.error(`❌ Error initializing trade log file: ${error.message}`);
  process.exit(1); // Exit if file cannot be created
}

// === UTILITY FUNCTIONS ===

/**
 * Reads the trade log from the JSON file.
 * @returns {Array<Object>} An array of trade objects.
 */
function readTradeLog() {
  try {
    const data = fs.readFileSync(TRADE_LOG_PATH, 'utf8');
    return JSON.parse(data);
  } catch (error) {
    console.error(`❌ Error reading trade log: ${error.message}`);
    return []; // Return empty array on error
  }
}

/**
 * Writes the trade log to the JSON file.
 * @param {Array<Object>} trades - The array of trade objects to write.
 */
function writeTradeLog(trades) {
  try {
    // Keep only the last 20 trades to prevent the log from growing too large
    const recentTrades = trades.slice(-20);
    fs.writeFileSync(TRADE_LOG_PATH, JSON.stringify(recentTrades, null, 2));
  } catch (error) {
    console.error(`❌ Error writing trade log: ${error.message}`);
  }
}

/**
 * Logs a new trade entry.
 * @param {string} pair - The trading pair (e.g., 'EURUSD').
 * @param {string} direction - 'buy' or 'sell'.
 * @param {number} amount - The trade amount.
 * @param {number} expiry - The expiry time in minutes.
 * @param {string|null} result - 'win', 'loss', or null if pending.
 */
function logTrade(pair, direction, amount, expiry, result = null) {
  const trades = readTradeLog();
  trades.push({
    pair,
    direction,
    amount,
    expiry,
    result,
    timestamp: new Date().toISOString(),
  });
  writeTradeLog(trades);
  console.log(`📝 Logged trade: ${pair} ${direction.toUpperCase()} $${amount} ${expiry}m`);
}

/**
 * Retrieves trade statistics (last 3 trades and win rate).
 * @returns {Object} An object containing last 3 trades and win rate.
 */
function getTradeStats() {
  const trades = readTradeLog();
  const last3 = trades.slice(-3); // Get the last 3 trades
  const wins = trades.filter(t => t.result === 'win').length;
  const losses = trades.filter(t => t.result === 'loss').length;
  const total = wins + losses;
  const winRate = total ? ((wins / total) * 100).toFixed(1) + '%' : 'N/A';
  return { last3, winRate };
}

/**
 * Placeholder function for analysis and trade suggestions.
 * In a real scenario, this would involve fetching real-time data,
 * applying indicators, and making decisions.
 * @returns {string} A formatted string of trade suggestions.
 */
function analyzeAndSuggest() {
  const pairs = ['EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'NZDUSD']; // Expand as needed
  const suggestions = pairs.map(pair => {
    // Simple random decision for demonstration
    const decision = Math.random() > 0.5 ? 'buy' : 'sell';
    const confidence = Math.floor(60 + Math.random() * 40); // Random confidence between 60-99%
    return `• ${pair}: ${decision.toUpperCase()} (Confidence: ${confidence}%)`;
  });
  return suggestions.join('\n');
}

/**
 * Placeholder function for learning from past trades.
 * In a real scenario, this would update a model or adjust parameters
 * based on previous trade outcomes.
 */
function learnFromPast() {
  const trades = readTradeLog();
  const lastTrade = trades.slice(-1)[0];
  if (!lastTrade) {
    console.log('🧠 No past trades to learn from yet.');
    return;
  }
  // TODO: Implement actual learning logic here.
  // Example: Adjust strategy parameters based on if the last trade was a win or loss.
  console.log(`🧠 Learning from last trade (${lastTrade.pair} - ${lastTrade.result || 'pending'})...`);
}

// === BACKGROUND LOOP ===
// This loop runs every 3 minutes (180000 milliseconds)
setInterval(() => {
  if (DEFAULT_TELEGRAM_CHAT_ID) {
    const suggestion = analyzeAndSuggest();
    learnFromPast(); // Call learning function
    bot.sendMessage(DEFAULT_TELEGRAM_CHAT_ID, `📊 Auto Analysis (Every 3m):\n${suggestion}`)
      .then(() => console.log('✅ Sent auto analysis message.'))
      .catch(error => console.error('❌ Error sending auto analysis message:', error.message));
  } else {
    console.warn('⚠️ DEFAULT_TELEGRAM_CHAT_ID is not set. Auto analysis messages will not be sent.');
  }
}, 180000);

// === TELEGRAM BOT COMMAND HANDLERS ===

// /start command
bot.start((ctx) => {
  ctx.reply(`👋 Welcome, ${ctx.from.first_name}! I'm your Pocket Option trade bot.
Use /help to see available commands.`);
  console.log(`Received /start from ${ctx.from.username || ctx.from.first_name}`);
});

// /help command
bot.help((ctx) => {
  ctx.reply(`📚 Available commands:
/trade <pair> <buy|sell> <amount> <expiry_minutes> - Log a trade.
  Example: /trade EURUSD buy 100 5
/stats - Show last 3 trades and overall win rate.
/suggest - Get suggested trades (based on placeholder analysis).
/analyze - Trigger a manual analysis and get suggestions.
/ping - Test if bot is online.
`);
  console.log(`Received /help from ${ctx.from.username || ctx.from.first_name}`);
});

// /ping command
bot.command('ping', (ctx) => {
  ctx.reply('🏓 Pong!');
  console.log(`Received /ping from ${ctx.from.username || ctx.from.first_name}`);
});

// /trade command: /trade EURUSD buy 100 5
bot.command('trade', (ctx) => {
  const args = ctx.message.text.split(' ').slice(1); // Get arguments after /trade
  if (args.length !== 4) {
    return ctx.reply('⚠️ Usage: /trade <pair> <buy|sell> <amount> <expiry_minutes>\nExample: /trade EURUSD buy 100 5');
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

  logTrade(pair.toUpperCase(), direction.toLowerCase(), amount, expiry);
  ctx.reply(`✅ Trade Received and Logged:\nPair: ${pair.toUpperCase()}\nType: ${direction.toUpperCase()}\nAmount: $${amount}\nExpiry: ${expiry}m`);
  console.log(`Received /trade ${pair} ${direction} ${amount} ${expiry} from ${ctx.from.username || ctx.from.first_name}`);
});

// /stats command
bot.command('stats', (ctx) => {
  const { last3, winRate } = getTradeStats();
  let summary = '📊 Last 3 Trades:\n';
  if (last3.length === 0) {
    summary += 'No trades logged yet.';
  } else {
    summary += last3.map(t => {
      const tradeResult = t.result ? ` - ${t.result.toUpperCase()}` : ' - pending';
      return `• ${t.pair} ${t.direction.toUpperCase()} $${t.amount} ${t.expiry}m${tradeResult} @ ${new Date(t.timestamp).toLocaleTimeString()}`;
    }).join('\n');
  }
  ctx.reply(`${summary}\n\n🏆 Overall Win Rate: ${winRate}`);
  console.log(`Received /stats from ${ctx.from.username || ctx.from.first_name}`);
});

// /suggest command
bot.command('suggest', (ctx) => {
  const suggestion = analyzeAndSuggest();
  ctx.reply(`💡 Suggested Trades:\n${suggestion}`);
  console.log(`Received /suggest from ${ctx.from.username || ctx.from.first_name}`);
});

// /analyze command (manual trigger for analysis)
bot.command('analyze', (ctx) => {
  const suggestion = analyzeAndSuggest();
  ctx.reply(`📈 Manual Analysis Triggered:\n${suggestion}`);
  console.log(`Received /analyze from ${ctx.from.username || ctx.from.first_name}`);
});

// === START THE BOT ===
bot.launch()
  .then(() => console.log('🚀 Telegram bot started (polling mode).'))
  .catch((err) => console.error('❌ Failed to start Telegram bot:', err));

// Enable graceful stop
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
