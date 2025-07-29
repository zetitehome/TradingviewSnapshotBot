// bot.js
const { Telegraf } = require('telegraf');

// Replace with your bot token
const bot = new Telegraf('8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA');

// Start command
bot.start((ctx) => {
  ctx.reply(`👋 Welcome, ${ctx.from.first_name}! I'm your Pocket Option trade bot.`);
});

// Ping command
bot.command('ping', (ctx) => {
  ctx.reply('🏓 Pong!');
});

// Custom signal handler
bot.command('signal', (ctx) => {
  // Example response
  ctx.reply('📈 New signal: BUY EUR/USD in 1 min (Winrate: 74%)');
});

// Listen for plain text messages
bot.on('text', (ctx) => {
  const message = ctx.message.text;
  if (message.toLowerCase().includes('trade')) {
    ctx.reply('🟢 Trade command received. Executing macro...');
    // Trigger your UI.Vision macro here via webhook or local server
  }
});

// Launch the bot
bot.launch();
console.log('🤖 Telegram bot is running...');

// Enable graceful stop
process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));
  await triggerUIVisionMacro(symbol, interval, exchange, theme);
  res.status(200).send('OK');
