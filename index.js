const { Telegraf } = require('telegraf');
const axios = require('axios');

// Replace with your bot token
const bot = new Telegraf('8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA');

// === Start ===
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
/ping - Test if bot is online
/signal - Show a sample signal
/analyze - Trigger UI.Vision chart analysis
/auto - Enable auto-trading mode`);
});

// === Sample Signal Command ===
bot.command('signal', (ctx) => {
  ctx.reply('📈 New signal: BUY EUR/USD in 1 min (Winrate: 74%)');
});

// === Analyze Command ===
bot.command('analyze', async (ctx) => {
  ctx.reply('🔍 Analyzing chart, please wait...');
  try {
    await axios.post('http://localhost:3333/analyze');
    ctx.reply('✅ Analysis started via UI.Vision');
  } catch (err) {
    console.error(err);
    ctx.reply('❌ Failed to start analysis.');
  }
});

// === Auto-Trading Command ===
bot.command('auto', async (ctx) => {
  ctx.reply('🤖 Auto-trading enabled. Watching for signals...');
  try {
    await axios.post('http://localhost:3333/auto');
    ctx.reply('✅ Auto mode activated.');
  } catch (err) {
    console.error(err);
    ctx.reply('❌ Error enabling auto mode.');
  }
});

// === Handle Text (for Manual "trade" Keyword) ===
bot.on('text', (ctx) => {
  const message = ctx.message.text.toLowerCase();
  if (message.includes('trade')) {
    ctx.reply('🟢 Trade command received. Executing marco...');
    axios.post('http://localhost:3333/trade')
      .then(() => ctx.reply('✅ Trade macro executed.'))
      .catch(err => {
        console.error(err);
        ctx.reply('❌ Failed to execute trade macro.');
      });
  }
});

// === launch Bot ===
bot.launch();
console.log('🤖 Telegram Bot is running!');

// === Graceful Shutdown ===
process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));