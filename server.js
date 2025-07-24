const express = require("express");
const bodyParser = require("body-parser");
const axios = require("axios");

const TELEGRAM_CHAT_ID = "6337160812";
const TELEGRAM_TOKEN = "8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA";

const app = express();
app.use(bodyParser.json());

app.post("/webhook", async (req, res) => {
    const alert = req.body;

    try {
        const msg = `📩 <b>New TradingView Signal</b>\n<b>Pair:</b> ${alert.pair}\n<b>Type:</b> ${alert.direction}\n<b>Win Rate:</b> ${alert.winrate}%\n\nReply "yes" to confirm or "no" to cancel.`;

        await axios.post(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage`, {
            chat_id: TELEGRAM_CHAT_ID,
            text: msg,
            parse_mode: "HTML"
        });

        res.status(200).send("✅ Alert sent to Telegram");
    } catch (err) {
        console.error("Telegram Error:", err.message);
        res.status(500).send("❌ Telegram send failed");
    }
});

const PORT = 3000;
app.listen(PORT, () => {
    console.log(`📡 Webhook server listening at http://localhost:${PORT}/webhook`);
});
