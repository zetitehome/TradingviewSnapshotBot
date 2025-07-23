// ✅ Updated server.js (Pocket Option + TradingView + Telegram Integration)
// This version includes: advanced Telegram menu, auto-trade confirmation logic, JSON webhook handler, and trading logic trigger

const express = require('express');
const bodyParser = require('body-parser');
const axios = require('axios');
const { exec } = require('child_process');
const fs = require('fs');
const app = express();
const PORT = 3333;

// Load Telegram Bot Settings
const TELEGRAM_TOKEN = '8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA';
const TELEGRAM_CHAT_ID = '6337160812';

// Default trade config
const DEFAULT_TRADE_AMOUNT = 1; // USD
const DEFAULT_MAX_AMOUNT = 100;
const BALANCE_PERCENTAGE_MODE = true; // If true, 5%–100% of balance used if auto
const AUTO_EXECUTE_CONFIDENCE_THRESHOLD = 70; // %

app.use(bodyParser.json());

// === Util: Send Telegram Message ===
async function sendTelegramMessage(message, replyMarkup = null) {
  try {
    const payload = {
      chat_id: TELEGRAM_CHAT_ID,
      text: message,
      parse_mode: 'HTML',
    };
    if (replyMarkup) payload.reply_markup = replyMarkup;
    await axios.post(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage`, payload);
  } catch (err) {
    console.error('Telegram send error:', err.message);
  }
}

// === Util: Trigger UI.Vision Macro ===
function triggerMacro(symbol, action, expiry, amount = DEFAULT_TRADE_AMOUNT) {
  const command = `curl http://localhost:8080/?macro=trade&symbol=${symbol}&action=${action}&expiry=${expiry}&amount=${amount}`;
  exec(command, (err, stdout, stderr) => {
    if (err) return console.error('Macro Trigger Error:', stderr);
    console.log('Macro Triggered:', stdout);
  });
}

// === Webhook Receiver (TradingView → Bot) ===
app.post('/webhook', async (req, res) => {
  const data = req.body;
  if (!data || !data.symbol || !data.action || !data.confidence) {
    return res.status(400).send('Missing required fields');
  }

  const { symbol, action, confidence, expiry = 1, amount = DEFAULT_TRADE_AMOUNT, winrate, snapshot_url } = data;

  // Format message
  const message = `📥 <b>Signal Received</b>\n
<b>Pair:</b> ${symbol}
<b>Action:</b> ${action.toUpperCase()}
<b>Confidence:</b> ${confidence}%
<b>Expiry:</b> ${expiry} min
<b>Expected Winrate:</b> ${winrate || '?'}%
<b>Amount:</b> $${amount}
${snapshot_url ? `📸 <a href='${snapshot_url}'>View Chart</a>` : ''}`;

  // Auto-Execute if Confidence >= Threshold
  if (confidence >= AUTO_EXECUTE_CONFIDENCE_THRESHOLD) {
    await sendTelegramMessage(`${message}\n\n🚀 Auto-trade will execute now.`);
    triggerMacro(symbol, action, expiry, amount);
  } else {
    await sendTelegramMessage(
      `${message}\n\n⚠️ Confidence under ${AUTO_EXECUTE_CONFIDENCE_THRESHOLD}%. Confirm trade?`,
      {
        inline_keyboard: [
          [
            { text: '✅ Yes', callback_data: `confirm|${symbol}|${action}|${expiry}|${amount}` },
            { text: '❌ No', callback_data: 'cancel' }
          ]
        ]
      }
    );
  }

  res.status(200).send('Received');
});

// === Telegram Callback for Confirmation ===
app.post(`/callback`, async (req, res) => {
  const { callback_query } = req.body;
  if (!callback_query) return res.sendStatus(400);

  const { id, data: cbData, message } = callback_query;

  if (cbData.startsWith('confirm')) {
    const [, symbol, action, expiry, amount] = cbData.split('|');
    triggerMacro(symbol, action, expiry, amount);
    await sendTelegramMessage(`📍 Trade Executed

<b>Pair:</b> ${symbol}
<b>Action:</b> ${action.toUpperCase()}
<b>Expiry:</b> ${expiry} min
<b>Amount:</b> $${amount}`);
  } else {
    await sendTelegramMessage('❌ Trade Cancelled');
  }
  res.sendStatus(200);
});

// === Menu Command (/menu) ===
app.get('/menu', async (req, res) => {
  await sendTelegramMessage(
    `📊 <b>Quantum Bot Menu</b>

<b>/menu</b> - Show this menu
<b>/stats</b> - View trading stats
<b>/analyze</b> - Analyze top pairs
<b>/help</b> - Strategy Help

🟢 Auto-trading: <code>On</code>
🔘 Confidence Threshold: ${AUTO_EXECUTE_CONFIDENCE_THRESHOLD}%
💵 Default Amount: $${DEFAULT_TRADE_AMOUNT}-${DEFAULT_MAX_AMOUNT}
📍 Entry Mode: ${BALANCE_PERCENTAGE_MODE ? 'Balance %' : 'Fixed $'}`
  );
  res.send('Menu Sent');
});

// === Start Server ===
app.listen(PORT, () => console.log(`✅ Webhook server running on http://localhost:${PORT}`));
