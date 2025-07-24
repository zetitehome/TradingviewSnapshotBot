module.exports = {
  name: 'settings',
  async execute(bot, msg) {
    await bot.sendMessage(msg.chat.id, `⚙️ Settings feature coming soon. You'll be able to save default timeframes, amounts, etc.`);
  }
};
