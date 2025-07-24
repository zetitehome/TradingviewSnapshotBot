module.exports = function validatePair(pair, type) {
  const forexPairs = ['EUR/USD', 'GBP/JPY', 'USD/JPY', 'AUD/USD'];
  const otcPairs = ['EURUSD-OTC', 'GBPJPY-OTC', 'USDJPY-OTC'];

  if (type.toLowerCase() === 'forex') return forexPairs.includes(pair);
  if (type.toLowerCase() === 'otc') return otcPairs.includes(pair);
  return false;
};
