module.exports = {
  name: 'start',
  async execute(bot, msg) {
    await bot.sendMessage(msg.chat.id, `👋 Welcome! Use /analyze, /pairlist or /help to begin.`);
  }
};
