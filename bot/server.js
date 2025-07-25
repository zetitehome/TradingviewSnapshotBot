const express = require("express");
const bodyParser = require("body-parser");
const fs = require("fs");
const axios = require("axios");

const app = express();
const PORT = 3000;

const TELEGRAM_TOKEN = "8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA";
const TELEGRAM_CHAT_ID = "6337160812";
const UI_VISION_WEBHOOK = "http://localhost:5000/run-macro";

let loopCount = 0;
let tradeHistory = loadTradeHistory(); // Load from file

app.use(bodyParser.json());

// === Command Handlers ===
app.post("/telegram", async (req, res) => {
  const message = req.body.message?.text || "";
  const chatId = req.body.message?.chat.id;

  if (!chatId) return res.sendStatus(200);

  if (message.startsWith("/trade")) {
    const parts = message.split(" ");
    if (parts.length < 5) {
      return sendTelegram("Usage: /trade EURUSD buy 5 3", chatId);
    }

    const [_, pair, direction, amount, expiry] = parts;

    const tradeData = {
      pair,
      direction,
      amount,
      expiry,
      confidence: 100,
      auto: false,
    };

    await runTrade(tradeData);
    saveTrade(tradeData, "manual");

    return sendTelegram(`📊 Manual Trade: ${pair} ${direction.toUpperCase()} ($${amount}, ${expiry}m)`, chatId);
  }

  if (message.startsWith("/stats")) {
    const stats = getStats();
    return sendTelegram(stats, chatId);
  }

  res.sendStatus(200);
});

// === Incoming Signal Endpoint from n8n or Hookdeck ===
app.post("/signal", async (req, res) => {
  loopCount++;

  const signal = req.body;

  if (!signal || !signal.pair || !signal.direction) return res.sendStatus(200);

  // Adjust based on past results
  const confidence = adjustConfidence(signal);

  const tradeData = {
    pair: signal.pair,
    direction: signal.direction,
    amount: 1,
    expiry: 3,
    confidence,
    auto: true,
  };

  if (confidence >= 60) {
    await runTrade(tradeData);
    saveTrade(tradeData, "auto");
    sendTelegram(`⚡ Auto Trade: ${tradeData.pair} ${tradeData.direction.toUpperCase()} | Confidence: ${confidence}%`, TELEGRAM_CHAT_ID);
  }

  // Run full OTC scan every 3 loops
  if (loopCount % 3 === 0) {
    // Here you'd loop through OTC pairs and re-analyze
    sendTelegram("🔁 Running full OTC analysis...", TELEGRAM_CHAT_ID);
  }

  res.sendStatus(200);
});

// === Helpers ===
function sendTelegram(message, chatId = TELEGRAM_CHAT_ID) {
  return axios.post(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage`, {
    chat_id: chatId,
    text: message,
  });
}

function runTrade(tradeData) {
  return axios.post(UI_VISION_WEBHOOK, tradeData);
}

function saveTrade(tradeData, type) {
  const record = {
    ...tradeData,
    type,
    time: new Date().toISOString(),
    result: null,
  };
  tradeHistory.push(record);
  if (tradeHistory.length > 100) tradeHistory.shift(); // Keep last 100
  fs.writeFileSync("trades.json", JSON.stringify(tradeHistory, null, 2));
}

function loadTradeHistory() {
  try {
    const data = fs.readFileSync("trades.json", "utf8");
    return JSON.parse(data);
  } catch {
    return [];
  }
}

function getStats() {
  const last3 = tradeHistory.slice(-3).reverse();
  const results = last3.map((t, i) => {
    const res = t.result === "win" ? "✅" : t.result === "loss" ? "❌" : "⏳";
    return `${i + 1}. ${t.pair} - ${res}`;
  });

  const total = tradeHistory.filter(t => t.result).length;
  const wins = tradeHistory.filter(t => t.result === "win").length;
  const winrate = total > 0 ? ((wins / total) * 100).toFixed(2) : "N/A";

  return `📈 Last 3 Trades:\n${results.join("\n")}\n\n⚙️ Current Winrate: ${winrate}%`;
}

function adjustConfidence(signal) {
  const similar = tradeHistory
    .filter(t => t.pair === signal.pair && t.direction === signal.direction)
    .slice(-3);

  const recentLoss = similar.find(t => t.result === "loss");

  return recentLoss ? 60 : 100;
}

// === Start Server ===
app.listen(PORT, () => {
  console.log(`Bot running on http://localhost:${PORT}`);
});