import express from "express";
import bodyParser from "body-parser";
import TelegramBot from "node-telegram-bot-api";
import puppeteer from "puppeteer";
import path from "path";
import fs from "fs";

const app = express();
app.use(bodyParser.json());
app.use(express.static("public"));

const TELEGRAM_TOKEN = "8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA";
const TELEGRAM_CHAT_ID = "6337160812";

const bot = new TelegramBot(TELEGRAM_TOKEN);

const PORT = 3000;

// In-memory status store keyed by chatId or sessionId (here single user demo)
let captureStatus = {
  state: "idle", // idle, capturing, sending, done, error
  message: "",
  lastImagePath: null,
  error: null,
};

// Helper Puppeteer capture function with cropping
async function captureChartScreenshot(url, outputPath) {
  const browser = await puppeteer.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1200, height: 800 });
    await page.goto(url, { waitUntil: "networkidle2" });

    // Wait for chart element to load - adapt selector to your chart container
    await page.waitForSelector("#chart-container", { timeout: 10000 });

    const chartElement = await page.$("#chart-container");
    if (!chartElement) throw new Error("Chart container not found");

    // Screenshot only the chart area
    await chartElement.screenshot({ path: outputPath });

    await browser.close();
    return outputPath;
  } catch (e) {
    await browser.close();
    throw e;
  }
}

// POST /start-capture
// body: { url: string }
app.post("/start-capture", async (req, res) => {
  if (captureStatus.state === "capturing") {
    return res.status(400).json({ error: "Capture already in progress" });
  }

  const { url } = req.body;
  if (!url) {
    return res.status(400).json({ error: "Missing URL" });
  }

  captureStatus = { state: "capturing", message: "Starting capture...", error: null };

  // Generate filename
  const filename = `chart_${Date.now()}.png`;
  const filepath = path.join(process.cwd(), "public", "captures", filename);

  try {
    await captureChartScreenshot(url, filepath);
    captureStatus = { ...captureStatus, state: "sending", message: "Sending Telegram photo...", lastImagePath: `/captures/${filename}` };

    // Send photo to Telegram
    await bot.sendPhoto(TELEGRAM_CHAT_ID, filepath, {
      caption: "Chart capture 📈",
    });

    captureStatus = { state: "done", message: "Capture and send complete", lastImagePath: `/captures/${filename}`, error: null };
    res.json({ status: "success", imageUrl: captureStatus.lastImagePath });
  } catch (err) {
    captureStatus = { state: "error", message: err.message, error: err };
    res.status(500).json({ error: err.message });
  }
});

// GET /capture-status
app.get("/capture-status", (req, res) => {
  res.json(captureStatus);
});

// Create captures folder if missing
const capturesDir = path.join(process.cwd(), "public", "captures");
if (!fs.existsSync(capturesDir)) {
  fs.mkdirSync(capturesDir, { recursive: true });
}

app.listen(PORT, () => {
  console.log(`Server running on http://localhost:${PORT}`);
});

// In-memory trade logs store (you can switch to DB later)
let tradeLogs = [];

// POST /trade-result
// Body: { pair, expiry, amount, entryTime, exitTime, result: "win"|"loss"|"pending" }
app.post("/trade-result", (req, res) => {
  const { pair, expiry, amount, entryTime, exitTime, result } = req.body;
  if (!pair || !expiry || !amount || !entryTime || !exitTime || !result) {
    return res.status(400).json({ error: "Missing trade result data" });
  }

  const trade = { pair, expiry, amount, entryTime, exitTime, result, id: Date.now() };
  tradeLogs.push(trade);

  // Limit logs to last 100 entries
  if (tradeLogs.length > 100) tradeLogs.shift();

  res.json({ status: "success", trade });
});

// GET /trade-logs
app.get("/trade-logs", (req, res) => {
  res.json(tradeLogs);
});

// GET /trade-stats
app.get("/trade-stats", (req, res) => {
  const total = tradeLogs.length;
  const wins = tradeLogs.filter(t => t.result === "win").length;
  const losses = tradeLogs.filter(t => t.result === "loss").length;
  const winRate = total > 0 ? ((wins / total) * 100).toFixed(2) : 0;

  res.json({ total, wins, losses, winRate });
});
