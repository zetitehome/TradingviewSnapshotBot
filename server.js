const express = require("express");
const bodyParser = require("body-parser");
const fs = require("fs");
const path = require("path");
const fetch = require("node-fetch"); // For webhook calls
const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.static(path.join(__dirname, "public")));
app.use(bodyParser.json());

// Pocket Option pairs allowed (same as front-end)
const validPairs = new Set([
  "EUR/USD","GBP/USD","USD/JPY","USD/CHF","AUD/USD","USD/CAD","NZD/USD","EUR/GBP","EUR/JPY","GBP/JPY",
  "AUD/JPY","CHF/JPY","EUR/AUD","EUR/CAD","GBP/CAD","AUD/CAD","NZD/JPY","NZD/CAD",
  "OTC/EURJPY","OTC/EURUSD","OTC/GBPUSD","OTC/USDJPY","OTC/USDCAD","OTC/USDCHF","OTC/AUDUSD","OTC/NZDUSD","OTC/EURGBP","OTC/GBPJPY",
  "BTC/USD","ETH/USD","LTC/USD","XAU/USD","XAG/USD"
]);

const TRADE_LOG_PATH = path.join(__dirname, "trade_logs.json");

// Load or init trade logs
let tradeLogs = [];
try {
  tradeLogs = JSON.parse(fs.readFileSync(TRADE_LOG_PATH));
} catch {
  tradeLogs = [];
}

// Save logs helper
function saveLogs() {
  fs.writeFileSync(TRADE_LOG_PATH, JSON.stringify(tradeLogs, null, 2));
}

// Simple trade stats calculation
function calcStats() {
  const total = tradeLogs.length;
  const wins = tradeLogs.filter(t => t.result === "win").length;
  const losses = tradeLogs.filter(t => t.result === "loss").length;
  const winRate = total ? Math.round((wins / total) * 100) : 0;
  return { total, wins, losses, winRate };
}

// Helper to validate amount format (fixed or percent)
function parseAmount(amountStr) {
  if (typeof amountStr !== "string") return null;
  const pctMatch = amountStr.match(/^(\d+)%$/);
  if (pctMatch) return { type: "pct", value: Number(pctMatch[1]) };
  const numMatch = amountStr.match(/^(\d+)$/);
  if (numMatch) return { type: "fixed", value: Number(numMatch[1]) };
  return null;
}

// UI.Vision webhook trigger (example, adapt to your actual URL and data)
async function triggerUIVisionMacro(tradeData) {
  const webhookUrl = "http://localhost:8000/api/triggerMacro"; // Update to your UI.Vision webhook URL
  try {
    const res = await fetch(webhookUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(tradeData),
    });
    if (!res.ok) throw new Error(`Webhook error: ${res.statusText}`);
    return true;
  } catch (err) {
    console.error("UI.Vision webhook error:", err.message);
    return false;
  }
}

app.post("/analyze", (req, res) => {
  const { pair, expiry, amount } = req.body;
  if (!validPairs.has(pair)) {
    return res.status(400).json({ success: false, message: "Invalid trading pair" });
  }
  if (![1,3,5,15].includes(Number(expiry))) {
    return res.status(400).json({ success: false, message: "Invalid expiry time" });
  }
  const parsedAmount = parseAmount(amount);
  if (!parsedAmount || parsedAmount.value <= 0) {
    return res.status(400).json({ success: false, message: "Invalid amount format" });
  }

  // Placeholder: Add your analysis logic here or call your trading algo
  // For demo, just respond success and log analyze event
  tradeLogs.unshift({
    timestamp: Date.now(),
    action: "analyze",
    pair,
    expiry: Number(expiry),
    amount,
    result: "pending"
  });
  saveLogs();

  res.json({
    success: true,
    message: `Analysis started for ${pair} expiring in ${expiry} min with amount ${amount}`,
    stats: calcStats(),
  });
});

app.post("/trade", async (req, res) => {
  const { pair, expiry, amount } = req.body;
  if (!validPairs.has(pair)) {
    return res.status(400).json({ success: false, message: "Invalid trading pair" });
  }
  if (![1,3,5,15].includes(Number(expiry))) {
    return res.status(400).json({ success: false, message: "Invalid expiry time" });
  }
  const parsedAmount = parseAmount(amount);
  if (!parsedAmount || parsedAmount.value <= 0) {
    return res.status(400).json({ success: false, message: "Invalid amount format" });
  }

  // Prepare trade payload for UI.Vision webhook
  const tradeData = {
    pair,
    expiry: Number(expiry),
    amount,
    timestamp: Date.now(),
  };

  const triggered = await triggerUIVisionMacro(tradeData);

  // Log trade regardless of webhook success
  tradeLogs.unshift({
    timestamp: Date.now(),
    action: "trade",
    pair,
    expiry: Number(expiry),
    amount,
    result: triggered ? "sent" : "failed",
  });
  saveLogs();

  if (triggered) {
    res.json({
      success: true,
      message: `Trade triggered for ${pair} at expiry ${expiry} min with amount ${amount}`,
      stats: calcStats(),
    });
  } else {
    res.status(500).json({ success: false, message: "Failed to trigger trade macro" });
  }
});

// Endpoint for frontend to get current stats and logs
app.get("/stats", (req, res) => {
  const stats = calcStats();
  const logs = tradeLogs.slice(0, 50).map(log => {
    const time = new Date(log.timestamp).toLocaleString();
    return `[${time}] ${log.action.toUpperCase()} - Pair: ${log.pair}, Expiry: ${log.expiry}, Amount: ${log.amount}, Result: ${log.result}`;
  });
  res.json({ stats, logs });
});

app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});
