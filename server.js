// server.js
const express = require('express');
const axios = require('axios');
const bodyParser = require('body-parser');
const { spawn } = require('child_process');
const app = express();
const port = 3000;

// Replace with your bot token and chat ID
const TELEGRAM_BOT_TOKEN = "8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA";
const CHAT_ID = "6337160812";

app.use(bodyParser.json());

app.post("/webhook", async (req, res) => {
  const data = req.body;

  if (!data || !data.signal) {
    return res.status(400).send("Missing 'signal' in request body.");
  }

  const signal = data.signal.toUpperCase();
  const message = `📡 New Trading Signal Received:\n\n💹 Signal: *${signal}*\n📈 Source: TradingView\n\nConfirm? [yes/no]`;

  try {
    await axios.post(`https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`, {
      chat_id: CHAT_ID,
      text: message,
      parse_mode: "Markdown"
    });
    res.send("Signal sent to Telegram ✅");
  } catch (err) {
    console.error("Telegram sendMessage error:", err.message);
    res.status(500).send("Failed to send Telegram message ❌");
  }
});

app.listen(port, () => {
  console.log(`🚀 Server running on http://localhost:${port}`);
});
// Start the Python script to handle Telegram bot interactions
const pythonProcess = spawn('python', ['tvsnapshotbot.py']);