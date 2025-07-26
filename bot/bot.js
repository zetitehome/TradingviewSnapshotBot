// bot.js
const TelegramBot = require('node-telegram-bot-api');
const fs = require('fs');
const path = require('path');

const token = '8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA';
const bot = new TelegramBot(token, { polling: true });
const chatId = '6337160812';

const tradeLogPath = path.join(__dirname, 'logs', 'trade-log.json');
if (!fs.existsSync('logs')) fs.mkdirSync('logs');
if (!fs.existsSync(tradeLogPath)) fs.writeFileSync(tradeLogPath, JSON.stringify([]));

function logTrade(pair, direction, amount, expiry, result = null) {
  const trades = JSON.parse(fs.readFileSync(tradeLogPath));
  trades.push({
    pair,
    direction,
    amount,
    expiry,
    result,
    timestamp: new Date().toISOString(),
  });
  fs.writeFileSync(tradeLogPath, JSON.stringify(trades.slice(-20), null, 2));
}

function getTradeStats() {
  const trades = JSON.parse(fs.readFileSync(tradeLogPath));
  const last3 = trades.slice(-3);
  const wins = trades.filter(t => t.result === 'win').length;
  const losses = trades.filter(t => t.result === 'loss').length;
  const total = wins + losses;
  const winRate = total ? ((wins / total) * 100).toFixed(1) + '%' : 'N/A';
  return { last3, winRate };
}

function analyzeAndSuggest() {
  const pairs = ['EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD']; // Expand as needed
  const suggestions = pairs.map(pair => {
    const decision = Math.random() > 0.5 ? 'buy' : 'sell';
    return `${pair}: ${decision.toUpperCase()} (Confidence: ${Math.floor(60 + Math.random() * 40)}%)`;
  });
  return suggestions.join('\n');
}

function learnFromPast() {
  const trades = JSON.parse(fs.readFileSync(tradeLogPath));
  const last = trades.slice(-1)[0];
  if (!last) return;
  // Add learning logic here
}

// Background loop: every 3 minutes
setInterval(() => {
  const suggestion = analyzeAndSuggest();
  learnFromPast();
  bot.sendMessage(chatId, `🔍 Auto Analysis (Every 3m):\n${suggestion}`);
}, 180000);

// Telegram Commands
bot.onText(/\/trade (\w+) (buy|sell) (\d+(\.\d+)?) (\d+)/i, (msg, match) => {
  const [, pair, direction, amount, , expiry] = match;
  bot.sendMessage(msg.chat.id, `📥 Trade Received:\nPair: ${pair}\nType: ${direction.toUpperCase()}\nAmount: $${amount}\nExpiry: ${expiry}m`);
  logTrade(pair, direction, parseFloat(amount), parseInt(expiry));
});

bot.onText(/\/stats/, (msg) => {
  const { last3, winRate } = getTradeStats();
  const summary = last3.map(t => `• ${t.pair} ${t.direction.toUpperCase()} - ${t.result || 'pending'} @ ${new Date(t.timestamp).toLocaleTimeString()}`).join('\n');
  bot.sendMessage(msg.chat.id, `📊 Last 3 Trades:\n${summary}\n\n🏆 Win Rate: ${winRate}`);
});

bot.onText(/\/suggest/, (msg) => {
  const suggestion = analyzeAndSuggest();
  bot.sendMessage(msg.chat.id, `🧠 Suggested Trades:\n${suggestion}`);
});

bot.onText(/\/analyze/, (msg) => {
  const suggestion = analyzeAndSuggest();
  bot.sendMessage(msg.chat.id, `📈 Manual Analysis Triggered:\n${suggestion}`);
});