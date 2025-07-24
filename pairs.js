const POCKET_OPTION_PAIRS = [
  "EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD",
  "USD/CHF", "NZD/USD", "EUR/GBP", "EUR/JPY", "GBP/JPY",
  "USD/RUB", "USD/TRY", "EUR/TRY", "USD/ZAR",
  "BTC/USD", "ETH/USD", "LTC/USD", "XRP/USD",
  "OTC/EURJPY", "OTC/GBPUSD", "OTC/USDRUB", "OTC/USDJPY"
  // add all OTC pairs Pocket Option supports
];

// Backend: simple validation example
function isValidPair(pair) {
  return POCKET_OPTION_PAIRS.includes(pair);
}

module.exports = pairs;
