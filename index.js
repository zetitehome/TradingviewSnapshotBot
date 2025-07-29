// index.js
const express = require('express');
const bodyParser = require('body-parser');
const { exec } = require('child_process');
const axios = require('axios');
const path = require('path');
const app = express();

const PORT = 3333;
const TELEGRAM_BOT_TOKEN = '8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA';
const TELEGRAM_CHAT_ID = '6337160812';

app.use(bodyParser.json());
app.use(express.static(path.join(__dirname, 'public')));

// Web dashboard for quick view
app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// Webhook for trade signal
app.post('/signal', async (req, res) => {
  const { pair, action, expiry, amount, winrate } = req.body;

  if (!pair || !action || !expiry || !amount || !winrate) {
    return res.status(400).send('Missing parameters');
  }

  const cmd = `cscript run_macro.vbs "${pair}" "${action}" "${expiry}" "${amount}" "${winrate}"`;

  exec(cmd, async (error, stdout, stderr) => {
    if (error) {
      console.error(`❌ Macro Error: ${error.message}`);
      await sendTelegram(`❌ Trade Failed\nPair: ${pair}\nAction: ${action}`);
      return res.status(500).send('Macro execution failed');
    }

    console.log(`✅ Trade Executed: ${pair} | ${action} | $${amount} | ${expiry}min`);
    await sendTelegram(`✅ Trade Placed\n📈 Pair: ${pair}\n📌 Action: ${action}\n💰 Amount: $${amount}\n⏱ Expiry: ${expiry} min\n📊 Winrate: ${winrate}%`);
    res.send('Macro executed');
  });
});

// Telegram alert function
async function sendTelegram(message) {
  const url = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`;
  try {
    await axios.post(url, {
      chat_id: TELEGRAM_CHAT_ID,
      text: message,
      parse_mode: 'Markdown',
    });
  } catch (err) {
    console.error('❌ Telegram Send Error:', err.message);
  }
}

app.listen(PORT, () => {
  console.log(`🚀 UI.Vision Webhook Server running on http://localhost:${PORT}`);
});
