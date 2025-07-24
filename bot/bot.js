// bot.js

module.exports = function setupBot(bot, { addTradeLog, updateTradeResult, telegramSendMessage, telegramSendPhoto, captureTradingView }) {
  bot.onText(/\/start/, (msg) => {
    bot.sendMessage(msg.chat.id, `👋 Welcome to the Trading Bot, ${msg.from.first_name || 'Trader'}!
    
Use /stats to see your win rate.
Use /last to get last trade.
Use /help to see more commands.`);
  });

  bot.onText(/\/stats/, async (msg) => {
    const { total, wins, losses, winRate } = require('./server').calculateStats();
    bot.sendMessage(msg.chat.id, `📊 Stats:\nTotal: ${total}\nWins: ${wins}\nLosses: ${losses}\nWin Rate: ${winRate}%`);
  });

  bot.onText(/\/last/, (msg) => {
    const trades = require('./server').loadTrades();
    const last = trades[trades.length - 1];
    if (!last) {
      bot.sendMessage(msg.chat.id, 'No trades logged yet.');
      return;
    }
    bot.sendMessage(msg.chat.id, `📌 Last Trade:\nPair: ${last.pair}\nAmount: ${last.amount}\nExpiry: ${last.expiry}\nTime: ${new Date(last.timestamp).toLocaleString()}`);
  });

  bot.onText(/\/capture (.+)/, async (msg, match) => {
    const url = match[1];
    try {
      const { screenshot } = await captureTradingView({ exchange: 'FX', ticker: 'EURUSD', interval: '1', theme: 'dark' });
      await telegramSendPhoto(msg.chat.id, screenshot, '📸 TradingView Snapshot');
    } catch (err) {
      telegramSendMessage(msg.chat.id, '❌ Error capturing chart: ' + err.message);
    }
  });

  bot.onText(/\/help/, (msg) => {
    bot.sendMessage(msg.chat.id, `📖 Commands:
/start - Welcome
/stats - Show win stats
/last - Show last trade
/capture [url] - Snapshot a TradingView chart
/help - Show this help menu`);
  });
};
