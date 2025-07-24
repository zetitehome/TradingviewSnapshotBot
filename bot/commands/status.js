module.exports = {
  name: 'status',
  async execute(bot, msg) {
    await bot.sendMessage(msg.chat.id, `🤖 Bot is running.\nTime: ${new Date().toLocaleString()}`);
  }
};
