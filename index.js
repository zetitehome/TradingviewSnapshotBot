// index.js

const express = require('express');
const fs = require('fs');
const path = require('path');
const { exec } = require('child_process');
const TelegramBot = require('node-telegram-bot-api');

// === CONFIG ===
const PORT = process.env.PORT || 3333;
const TELEGRAM_TOKEN = '8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA';
const bot = new TelegramBot(TELEGRAM_TOKEN, { polling: true });
const LOG_FILE = path.join(__dirname, 'logs', 'trades.log');
const MACRO_PATH = path.join(__dirname, 'run_macro.vbs'); // or .kantu

// === START EXPRESS SERVER ===
const app = express();
app.use(express.json());

app.post('/signal', (req, res) => {
  const { pair, action, expiry, amount, winrate } = req.body;

  if (!pair || !action || !expiry || !amount || !winrate) {
    return res.status(400).send('Missing one or more required fields.');
  }

  const command = `cscript "${MACRO_PATH}" "${pair}" "${action}" "${expiry}" "${amount}" "${winrate}"`;

  exec(command, (err, stdout, stderr) => {
    const result = err ? '❌ Failed' : '✅ Success';
    const log = `[${new Date().toISOString()}] ${result} - ${pair} ${action} $${amount} (${expiry}m @ ${winrate}%)\n`;

    fs.appendFileSync(LOG_FILE, log);

    // Notify bot owner if needed
    bot.sendMessage(6337160812, `📥 Trade Signal Received:\n\n${log}`);

    res.status(200).json({ status: result, log });
  });
});

app.listen(PORT, () => {
  console.log(`UI.Vision server running on port ${PORT}`);
});