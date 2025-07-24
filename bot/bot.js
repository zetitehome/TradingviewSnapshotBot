// bot.js
// Export a function that accepts TelegramBot instance and helpers

module.exports = function(bot, helpers) {
  const { addTradeLog, updateTradeResult, telegramSendMessage, telegramSendPhoto, captureTradingView } = helpers;

  // Simple commands
  bot.onText(/^\/start$/, async (msg) => {
    await telegramSendMessage(msg.chat.id, `👋 Welcome! Commands:\n/snapshot [PAIR] [interval] [theme]\n/signal PAIR DIR EXPIRY\n/help for info`);
  });

  bot.onText(/^\/help$/, async (msg) => {
    await telegramSendMessage(msg.chat.id,
      'Usage:\n' +
      '/snapshot [EXCHANGE:TICKER] [interval] [theme]\n' +
      '/signal PAIR DIR EXPIRY\n' +
      'DIR: CALL|PUT|BUY|SELL\n' +
      'EXPIRY: 1m,3m,5m,15m or minutes\n' +
      'Example: /signal FX:EURUSD CALL 5m\n'
    );
  });

  bot.onText(/^\/snapshot(?:\s+(.+))?$/, async (msg, match) => {
    const chatId = msg.chat.id;
    const arg = match[1] || '';
    await telegramSendMessage(chatId, '⏳ Capturing snapshot...');

    // Parse argument for exchange:ticker, interval, theme
    let exchange = 'FX', ticker = 'EURUSD', interval = '1', theme = 'dark';
    if (arg) {
      const parts = arg.trim().split(/\s+/);
      if (parts.length > 0) {
        if (parts[0].includes(':')) {
          [exchange, ticker] = parts[0].split(':');
        } else {
          ticker = parts[0];
        }
      }
      if (parts[1]) interval = parts[1];
      if (parts[2]) theme = parts[2];
    }

    try {
      const { screenshot, url } = await captureTradingView({ exchange, ticker, interval, theme });
      await telegramSendPhoto(chatId, screenshot, `Snapshot: ${exchange}:${ticker} interval ${interval} theme ${theme}`);
    } catch (e) {
      await telegramSendMessage(chatId, '❌ Error capturing snapshot: ' + e.message);
    }
  });

  // Example: /signal FX:EURUSD CALL 5m
  bot.onText(/^\/signal\s+(\S+)\s+(CALL|PUT|BUY|SELL)\s+(\d+m?)$/i, async (msg, match) => {
    const chatId = msg.chat.id;
    const pair = match[1];
    const direction = match[2].toLowerCase();
    const expiryRaw = match[3];
    let expiry = 1;
    if (expiryRaw.endsWith('m')) expiry = parseInt(expiryRaw.slice(0, -1));
    else expiry = parseInt(expiryRaw);

    const timestamp = Date.now();
    const amount = 5; // default fixed for demo

    // Log trade
    addTradeLog({ pair, expiry, amount, direction, timestamp, source: 'signal' });

    // Reply confirmation
    await telegramSendMessage(chatId, `✅ Signal received for ${pair} ${direction.toUpperCase()} expiry ${expiry} min\nTrade logged at ${new Date(timestamp).toLocaleString()}`);
  });

  // Manual trade logging via /trade pair expiry amount
  bot.onText(/^\/trade\s+(\S+)\s+(\d+)\s+([\d\.%]+)$/, async (msg, match) => {
    const chatId = msg.chat.id;
    const pair = match[1];
    const expiry = Number(match[2]);
    let amount = match[3];
    // parse amount: if ends with %, leave string else parse float
    if (!amount.endsWith('%')) amount = parseFloat(amount);

    const timestamp = Date.now();

    addTradeLog({ pair, expiry, amount, timestamp, source: 'manual' });

    await telegramSendMessage(chatId, `✅ Manual trade logged for ${pair} with expiry ${expiry} min and amount ${amount}`);
  });

  // Update trade result command /result timestamp win|loss
  bot.onText(/^\/result\s+(\d+)\s+(win|loss)$/i, async (msg, match) => {
    const chatId = msg.chat.id;
    const timestamp = Number(match[1]);
    const result = match[2].toLowerCase();

    const updated = updateTradeResult(timestamp, result);
    if (updated) {
      await telegramSendMessage(chatId, `✅ Trade result updated to "${result}" for trade at timestamp ${timestamp}`);
    } else {
      await telegramSendMessage(chatId, `❌ Trade not found for timestamp ${timestamp}`);
    }
  });

  // Analyze command placeholder
  bot.onText(/^\/analyze$/, async (msg) => {
    await telegramSendMessage(msg.chat.id, "🔎 Analyze command received. Not implemented yet.");
  });

  bot.on('message', (msg) => {
    // Can add general listeners or logging here
    // console.log("Message received:", msg.text);
  });
};
