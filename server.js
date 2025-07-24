import express from "express";
import bodyParser from "body-parser";
import TelegramBot from "node-telegram-bot-api";
import puppeteer from "puppeteer";
import path from "path";
import fs from "fs";

const app = express();
app.use(bodyParser.json());
app.use(express.static("public"));

const TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN";
const TELEGRAM_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID";

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
