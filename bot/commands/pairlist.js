module.exports = {
  name: 'pairlist',
  async execute(bot, msg) {
    await bot.sendMessage(msg.chat.id, `
📌 Available Pairs:
Forex: EUR/USD, GBP/JPY, USD/JPY, AUD/USD
OTC: EURUSD-OTC, GBPJPY-OTC, USDJPY-OTC
    `);
  }
};
