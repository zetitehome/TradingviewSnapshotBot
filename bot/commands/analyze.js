module.exports = {
  name: 'analyze',
  async execute(bot, msg) {
    await bot.sendMessage(msg.chat.id, `📊 Send a signal alert in JSON format to analyze it automatically.`);
  }
};
