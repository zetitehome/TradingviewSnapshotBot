module.exports = {
  name: 'help',
  async execute(bot, msg) {
    await bot.sendMessage(msg.chat.id, `
📖 Available Commands:
/start - Welcome Message
/analyze - Analyze TradingView Alert
/settings - Configure defaults
/status - Check bot status
/pairlist - List available pairs

Send a JSON alert to trigger automatic evaluation.
    `);
  }
};
