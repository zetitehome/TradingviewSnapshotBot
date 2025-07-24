const express = require("express");
const bodyParser = require("body-parser");
const TelegramBot = require("node-telegram-bot-api");
const puppeteer = require("puppeteer");
const fs = require("fs");
const path = require("path");

const app = express();
app.use(bodyParser.json());

const TELEGRAM_TOKEN = "8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA";
const TELEGRAM_CHAT_ID = "6337160812"; // for sending chart images
const bot = new TelegramBot(TELEGRAM_TOKEN, { polling: true });

let tradeLogs = [];
const LOG_FILE = path.join(__dirname, "tradeLogs.json");

// Load logs from disk on start
if (fs.existsSync(LOG_FILE)) {
  tradeLogs = JSON.parse(fs.readFileSync(LOG_FILE));
}

function saveLogs() {
  fs.writeFileSync(LOG_FILE, JSON.stringify(tradeLogs, null, 2));
}

function calcStats() {
  const total = tradeLogs.length;
  const wins = tradeLogs.filter(t => t.result === "win").length;
  const losses = tradeLogs.filter(t => t.result === "loss").length;
  const winRate = total ? ((wins / total) * 100).toFixed(2) : "0.00";
  return { total, wins, losses, winRate };
}

// Trade result update endpoint
app.post("/trade-result", (req, res) => {
  const { timestamp, result } = req.body;

  if (!timestamp || !["win", "loss"].includes(result)) {
    return res.status(400).json({ success: false, message: "Invalid timestamp or result" });
  }

  const index = tradeLogs.findIndex(t => t.timestamp === timestamp);
  if (index === -1) {
    return res.status(404).json({ success: false, message: "Trade entry not found" });
  }

  tradeLogs[index].result = result;
  saveLogs();

  return res.json({ success: true, message: `Trade result updated to ${result}`, stats: calcStats() });
});

// Example POST /trade to log trades (simplified)
app.post("/trade", (req, res) => {
  const { pair, expiry, amount, stopLoss, takeProfit } = req.body;
  if (!pair || !expiry || !amount) {
    return res.status(400).json({ success: false, message: "Missing required trade parameters" });
  }
  const timestamp = Date.now();

  tradeLogs.push({ timestamp, pair, expiry, amount, stopLoss, takeProfit, result: null });
  saveLogs();

  // Optionally trigger UI.Vision macro webhook here with trade data

  return res.json({ success: true, message: "Trade logged", timestamp, stats: calcStats() });
});

// Trade logs fetch for frontend
app.get("/logs", (req, res) => {
  res.json(tradeLogs);
});

// Stats fetch for frontend
app.get("/stats", (req, res) => {
  res.json(calcStats());
});

// Telegram bot listens for /result command to update trade results
bot.on("message", (msg) => {
  const chatId = msg.chat.id;
  const text = msg.text;

  if (!text) return;

  if (text.startsWith("/result")) {
    // Format: /result <timestamp> <win|loss>
    const parts = text.split(" ");
    if (parts.length !== 3) {
      bot.sendMessage(chatId, "Usage: /result <timestamp> <win|loss>");
      return;
    }
    const timestamp = Number(parts[1]);
    const result = parts[2].toLowerCase();

    if (!timestamp || !["win", "loss"].includes(result)) {
      bot.sendMessage(chatId, "Invalid timestamp or result.");
      return;
    }

    const index = tradeLogs.findIndex(t => t.timestamp === timestamp);
    if (index === -1) {
      bot.sendMessage(chatId, "Trade not found.");
      return;
    }

    tradeLogs[index].result = result;
    saveLogs();
    bot.sendMessage(chatId, `Trade result updated to ${result} for timestamp ${timestamp}`);
  }
});

// Puppeteer chart capture + send to Telegram
async function captureChartAndSend(chatUrl) {
  const browser = await puppeteer.launch({ headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage();

  await page.goto(chatUrl, { waitUntil: "networkidle2" });
  // Adjust selector for your chart container!
  const chartElement = await page.$("#chart-container") || await page.$("body");
  const screenshotBuffer = await chartElement.screenshot({ type: "png" });

  await bot.sendPhoto(TELEGRAM_CHAT_ID, screenshotBuffer, { caption: "Latest Chart Capture" });

  await browser.close();
}

// Start Express server
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Server listening on port ${PORT}`);
});
