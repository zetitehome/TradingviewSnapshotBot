// server.js
const TelegramBot = require('node-telegram-bot-api');
const fs = require('fs');
const path = require('path');
const schedule = require('node-schedule');
const { analyzeMarket } = require('./utils/indicators');
const { takeScreenshot } = require('./utils/screenshot');

const config = require('./config.json');
const token = config.telegramToken;
const bot = new TelegramBot(token, { polling: true });

const logPath = './tradeLog.json';
const SNAPSHOT_DIR = './snapshots';
if (!fs.existsSync(logPath)) fs.writeFileSync(logPath, '[]');
if (!fs.existsSync(SNAPSHOT_DIR)) fs.mkdirSync(SNAPSHOT_DIR);

let lastAnalysisTime = 0;
const MIN_INTERVAL = 180 * 1000; // 3 minutes

// === UTIL ===
function logTrade(entry) {
  const logs = JSON.parse(fs.readFileSync(logPath));
  logs.push(entry);
  fs.writeFileSync(logPath, JSON.stringify(logs.slice(-50), null, 2));
}

function getStats() {
  const logs = JSON.parse(fs.readFileSync(logPath));
  const last3 = logs.slice(-3);
  const wins = logs.filter(t => t.result === 'win').length;
  const rate = logs.length ? ((wins / logs.length) * 100).toFixed(1) : 'N/A';
  return { last3, winRate: rate };
}

// === ANALYZE MARKET ===
async function runAnalysis(chatId, manual = false) {
  const now = Date.now();
  if (!manual && now - lastAnalysisTime < MIN_INTERVAL) return;
  lastAnalysisTime = now;

  const result = await analyzeMarket();
  if (!result || !result.signal) return;

  const fileName = `${Date.now()}.png`;
  const filePath = path.join(SNAPSHOT_DIR, fileName);
  await takeScreenshot(filePath, result.symbol);

  const entry = {
    time: new Date().toLocaleString(),
    symbol: result.symbol,
    direction: result.signal,
    confidence: result.confidence,
    result: 'pending',
    screenshot: fileName
  };

  logTrade(entry);

  bot.sendMessage(chatId, `📈 *${result.symbol}*\nSignal: *${result.signal.toUpperCase()}*\nConfidence: ${result.confidence}%`, { parse_mode: 'Markdown' });
  bot.sendPhoto(chatId, filePath);

  // TODO: send to UI.Vision local webhook if auto-trade is enabled
}

// === COMMANDS ===
bot.onText(/\/analyze/, async (msg) => {
  await runAnalysis(msg.chat.id, true);
});

bot.onText(/\/stats/, (msg) => {
  const { last3, winRate } = getStats();
  let text = `📊 *Win Rate:* ${winRate}%\nLast 3 Trades:`;
  last3.forEach((t, i) => {
    text += `\n${i + 1}. ${t.symbol} - ${t.direction.toUpperCase()} (${t.result})`;
  });
  bot.sendMessage(msg.chat.id, text, { parse_mode: 'Markdown' });

  if (last3[0]?.screenshot) {
    const imgPath = path.join(SNAPSHOT_DIR, last3[0].screenshot);
    if (fs.existsSync(imgPath)) bot.sendPhoto(msg.chat.id, imgPath);
  }
});

// === SCHEDULED JOB ===
schedule.scheduleJob('*/3 * * * *', () => {
  runAnalysis(config.ownerChatId);
});

console.log('🤖 Bot running...');