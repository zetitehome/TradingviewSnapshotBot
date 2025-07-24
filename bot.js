// bot.js
require('dotenv').config();
const { Telegraf } = require('telegraf');
const commands = require('./commands');

const bot = new Telegraf(process.env.TELEGRAM_BOT_TOKEN);

// Register commands
bot.command('start', commands.start);
bot.command('analyze', commands.analyze);
bot.command('stats', commands.stats);
bot.command('trade', commands.trade);

bot.launch()
  .then(() => console.log('🤖 Bot started'))
  .catch(err => console.error('❌ Bot launch error:', err));

// Graceful stop
process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));
