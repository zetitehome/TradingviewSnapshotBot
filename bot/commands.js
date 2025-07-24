require('dotenv').config();
const TelegramBot = require('node-telegram-bot-api');
const db = require('../db');
const bot = new TelegramBot(process.env.BOT_TOKEN, { polling: true });

bot.onText(/\/start/, msg => {
  bot.sendMessage(msg.chat.id, `👋 Welcome ${msg.from.first_name}! Use /trade to log a trade.`);
});

bot.onText(/\/trade/, msg => {
  const chatId = msg.chat.id;

  bot.sendMessage(chatId, '📈 Enter trade details (pair direction time)\nExample: EUR/USD BUY 1m', {
    reply_markup: { force_reply: true }
  }).then(sent => {
    bot.once('message', reply => {
      const [pair, direction, time] = reply.text.split(' ');
      db.insertTrade(pair, direction, time, function () {
        const tradeId = this.lastID;
        const text = `🆕 Trade #${tradeId}\n📉 Pair: ${pair}\n📍 Direction: ${direction}\n⏰ Time: ${time}`;
        bot.sendMessage(chatId, text, {
          reply_markup: {
            inline_keyboard: [[
              { text: '✅ Win', callback_data: `win_${tradeId}` },
              { text: '❌ Loss', callback_data: `loss_${tradeId}` }
            ]]
          }
        });
      });
    });
  });
});

bot.on('callback_query', query => {
  const chatId = query.message.chat.id;
  const [result, id] = query.data.split('_');

  db.updateResult(id, result);
  bot.editMessageReplyMarkup({ inline_keyboard: [] }, {
    chat_id: chatId,
    message_id: query.message.message_id
  });
  bot.sendMessage(chatId, `🎯 Trade #${id} marked as ${result.toUpperCase()}`);
});

bot.onText(/\/result/, msg => {
  db.getLatestTrades(5, rows => {
    if (rows.length === 0) {
      bot.sendMessage(msg.chat.id, '📭 No recent trades found.');
      return;
    }

    const text = rows.map(r => `#${r.id} - ${r.pair} ${r.direction} [${r.time}] → ${r.result}`).join('\n');
    bot.sendMessage(msg.chat.id, `📊 Recent Trades:\n\n${text}`);
  });
});

bot.onText(/\/stats/, msg => {
  db.getStats(rows => {
    const text = rows.map(r => `${r.result.toUpperCase()}: ${r.count}`).join('\n');
    bot.sendMessage(msg.chat.id, `📈 Trade Summary:\n\n${text}`);
  });
});

bot.onText(/\/settings/, msg => {
  bot.sendMessage(msg.chat.id, `⚙️ Settings coming soon. Want auto-trading, timezones, or trade limits?`);
});
