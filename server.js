// server.js
const express = require('express');
const bodyParser = require('body-parser');
const axios = require('axios');
const { exec } = require('child_process');

const app = express();
app.use(bodyParser.json());

const TELEGRAM_BOT_TOKEN = '8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA';
const TELEGRAM_CHAT_ID = '6337160812'; // Optional: handle this dynamically
const CONFIRM_TIMEOUT = 10000; // 10 seconds

let lastSignal = null;

app.post('/callback', async (req, res) => {
    const signal = req.body;

    if (!signal || !signal.message) {
        return res.status(400).send('Invalid signal format');
    }

    lastSignal = signal;

    const tradeMsg = `📥 *New Signal Received:*\n${signal.message}\n\nConfirm? (yes/no)`;
    await sendTelegramMessage(tradeMsg);

    // Wait for confirmation (basic version)
    setTimeout(() => {
        if (signal.autoExecute) {
            console.log('Auto-executing trade...');
            runPythonBot(signal);
        } else {
            console.log('Awaiting confirmation...');
        }
    }, CONFIRM_TIMEOUT);

    res.send('Signal received');
});

app.get('/', (req, res) => {
    res.send('📡 Telegram Bot Server is running.');
});

function sendTelegramMessage(msg) {
    const url = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`;
    return axios.post(url, {
        chat_id: TELEGRAM_CHAT_ID,
        text: msg,
        parse_mode: "Markdown"
    });
}

function runPythonBot(signal) {
    const payload = JSON.stringify(signal).replace(/"/g, '\\"');
    const command = `python tvsnapshotbot.py "${payload}"`;

    exec(command, (error, stdout, stderr) => {
        if (error) {
            console.error(`❌ Error: ${error.message}`);
            return;
        }
        if (stderr) {
            console.error(`⚠️ stderr: ${stderr}`);
            return;
        }
        console.log(`✅ Python output: ${stdout}`);
    });
}

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
    console.log(`🚀 Server running on port ${PORT}`);
});
// Ensure to replace 'YOUR_TELEGRAM_BOT_TOKEN' and 'YOUR_CHAT_ID' with actual values.
// You can also set these as environment variables for better security.