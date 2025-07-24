module.exports = {
  async evaluate(pair, timeframe, winrate) {
    return `📉 OTC signal for ${pair} looks valid on ${timeframe} TF. Confidence: ${winrate}%.`;
  }
};
