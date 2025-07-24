module.exports = async function sendTelegramMessage(bot, chatId, message) {
  try {
    await bot.sendMessage(chatId, message);
  } catch (err) {
    console.error('Telegram send error:', err.message);
  }
};
