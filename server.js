/**
 * TradingView Snapshot & Analysis Service
 * =======================================
 * Provides PNG screenshots, lightweight TA JSON, and instrument lists
 * for your Telegram bot + TradingView alert workflows.
 *
 * Endpoints
 * ---------
 * GET  /healthz
 * GET  /start-browser
 * GET  /pairs                -> JSON list of instruments by category
 * GET  /snapshot/:pair       -> PNG (default) OR JSON if fmt=json or candles=1
 * GET  /analyze/:pair        -> JSON TA + (optional) image w/ ?img=1
 * GET  /run                  -> Legacy interface (exchange,ticker,interval,theme)
 *
 * Notes
 * -----
 * - pair format: "FX:EURUSD", "EURUSD", "NASDAQ:AAPL", "BINANCE:BTCUSDT".
 * - timeframe param: `tf` (minutes) or TradingView tokens (1,5,15,60,D,W,M).
 * - theme param: "dark" | "light".
 * - PNG >2KB enforced (retry).
 * - Candle JSON is *demo stub* unless you wire a real data provider (see TODO).
 */

const express = require("express");
const cors = require("cors");
const path = require("path");
const fs = require("fs");
const fetch = require("node-fetch"); // v2
const puppeteer = require("puppeteer");

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
const PORT = Number(process.env.SNAPSHOT_PORT || process.env.PORT || 10000);
const MIN_PNG_SIZE = 2048;            // 2KB
const PNG_RETRIES = 3;
const PNG_RETRY_DELAY_MS = 3500;

const DEFAULT_TF = String(process.env.DEFAULT_INTERVAL || "1");
const DEFAULT_THEME = (process.env.DEFAULT_THEME || "dark").toLowerCase().startsWith("l") ? "light" : "dark";
const ALLOW_ORIGIN = process.env.ALLOW_ORIGIN || "*"; // CORS
const DEBUG_SAVE = !!process.env.DEBUG_SAVE;          // save PNGs to /cache for debugging

// ---------------------------------------------------------------------------
// Data: Instrument Lists (should mirror Python bot; update there too)
// ---------------------------------------------------------------------------
const FX_PAIRS = [
  "FX:EURUSD","FX:GBPUSD","FX:USDJPY","FX:USDCHF","FX:AUDUSD",
  "FX:NZDUSD","FX:USDCAD","FX:EURGBP","FX:EURJPY","FX:GBPJPY",
  "FX:AUDJPY","FX:NZDJPY","FX:EURAUD","FX:GBPAUD","FX:EURCAD",
  "FX:USDMXN","FX:USDTRY","FX:USDZAR","FX:AUDCHF","FX:EURCHF",
];

const OTC_PAIRS = [
  "QUOTEX:EURUSD","QUOTEX:GBPUSD","QUOTEX:USDJPY","QUOTEX:USDCHF","QUOTEX:AUDUSD",
  "QUOTEX:NZDUSD","QUOTEX:USDCAD","QUOTEX:EURGBP","QUOTEX:EURJPY","QUOTEX:GBPJPY",
  "QUOTEX:AUDCHF","QUOTEX:EURCHF","QUOTEX:USDKES","QUOTEX:USDMAD","QUOTEX:USDBDT",
  "QUOTEX:USDMXN","QUOTEX:USDMYR","QUOTEX:USDPKR",
];

const INDEX_SYMBOLS = [
  "TVC:US30", "TVC:SPX", "TVC:NSDQ", "TVC:UKX", "TVC:DAX", "TVC:NI225", "TVC:HSI",
];

const CRYPTO_SYMBOLS = [
  "BINANCE:BTCUSDT", "BINANCE:ETHUSDT", "BINANCE:XRPUSDT",
  "BINANCE:SOLUSDT", "BINANCE:DOGEUSDT", "BINANCE:ADAUSDT",
];

const ALL_PAIRS = [
  ...FX_PAIRS,
  ...OTC_PAIRS,
  ...INDEX_SYMBOLS,
  ...CRYPTO_SYMBOLS,
];

// ---------------------------------------------------------------------------
// Express Setup
// ---------------------------------------------------------------------------
const app = express();
app.use(express.json({ limit: "1mb" }));
app.use(cors({ origin: ALLOW_ORIGIN }));

// debug cache dir
const CACHE_DIR = path.join(__dirname, "cache");
if (!fs.existsSync(CACHE_DIR)) fs.mkdirSync(CACHE_DIR, { recursive: true });

// ---------------------------------------------------------------------------
// Puppeteer Life‑Cycle
// ---------------------------------------------------------------------------
let browser = null;
let launchPromise = null;

async function launchBrowser() {
  if (browser) return browser;
  if (launchPromise) return launchPromise;

  launchPromise = (async () => {
    try {
      browser = await puppeteer.launch({
        headless: "new",
        args: [
          "--no-sandbox",
          "--disable-setuid-sandbox",
          "--disable-dev-shm-usage",
          "--disable-gpu",
          "--single-process",
        ],
        defaultViewport: { width: 1280, height: 720 },
      });
      console.log(`[${ts()}] ✅ Puppeteer launched.`);
      return browser;
    } catch (err) {
      console.error(`[${ts()}] ❌ Puppeteer launch failed:`, err);
      browser = null;
      throw err;
    } finally {
      launchPromise = null;
    }
  })();

  return launchPromise;
}

async function withPage(cb) {
  const b = await launchBrowser();
  const page = await b.newPage();
  try {
    return await cb(page);
  } finally {
    try { await page.close(); } catch (_) {}
  }
}

// ---------------------------------------------------------------------------
// Helpers: Time, Sleep, Logging, File Save
// ---------------------------------------------------------------------------
function ts() {
  return new Date().toISOString();
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function safeFilename(name) {
  return name.replace(/[^-_.A-Za-z0-9]/g, "_");
}

function debugSavePNG(buffer, prefix) {
  if (!DEBUG_SAVE) return;
  try {
    const fname = path.join(CACHE_DIR, `${prefix}_${Date.now()}.png`);
    fs.writeFileSync(fname, buffer);
    console.log(`[${ts()}] Saved debug PNG -> ${fname} (${buffer.length} bytes)`);
  } catch (err) {
    console.warn(`[${ts()}] debugSavePNG error:`, err);
  }
}

// ---------------------------------------------------------------------------
// Normalize input
// ---------------------------------------------------------------------------
function normTF(tf) {
  if (!tf) return DEFAULT_TF;
  const t = String(tf).trim().toLowerCase();
  if (/^\d+$/.test(t)) return t;
  if (t === "d" || t === "1d" || t === "day") return "D";
  if (t === "w" || t === "1w" || t === "week") return "W";
  if (t === "m" || t === "mo" || t === "1mth" || t === "month") return "M";
  return DEFAULT_TF;
}

function normTheme(theme) {
  if (!theme) return DEFAULT_THEME;
  return theme.toLowerCase().startsWith("l") ? "light" : "dark";
}

// Accept user pair like "EUR/USD", "FX:EURUSD", "eurusd", etc.
function normPair(input) {
  if (!input) return "FX:EURUSD";
  let s = String(input).trim().toUpperCase();
  s = s.replace(/\s+/g, "");
  // Add default prefix if missing
  if (!s.includes(":")) {
    // guess if includes slash e.g. EUR/USD
    s = s.replace("/", "");
    s = `FX:${s}`;
  }
  return s;
}

// ---------------------------------------------------------------------------
// TradingView Snapshot Capture (core)
// ---------------------------------------------------------------------------
async function captureTradingViewChart(pair, tf, theme) {
  const url = `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(pair)}&interval=${encodeURIComponent(tf)}&theme=${encodeURIComponent(theme)}`;
  console.log(`[${ts()}] Opening TradingView URL: ${url}`);

  return withPage(async (page) => {
    // speed/permissions
    await page.setBypassCSP(true);
    await page.setJavaScriptEnabled(true);
    await page.setRequestInterception(true);

    page.on("request", (req) => {
      // allow essential
      const type = req.resourceType();
      if (["image", "stylesheet", "script", "document", "xhr", "fetch"].includes(type)) {
        req.continue();
      } else {
        req.abort();
      }
    });

    // nav
    await page.goto(url, { waitUntil: "networkidle2", timeout: 90000 });

    // Wait some DOM hints (chart container). Best‑effort.
    try {
      await page.waitForSelector("div[data-name='legend-series-item']", { timeout: 15000 });
    } catch (_) {}

    // Hide UI overlays to shrink noise
    try {
      await page.evaluate(() => {
        const hideSel = [
          "[data-name='left-toolbar']",
          "[data-name='header-toolbar-symbol-search']",
          "[data-name='header-toolbar-intervals']",
          "[data-name='header-toolbar-style']",
          "[data-name='header-toolbar-save-load']",
          "[data-name='header-toolbar-properties']",
          "[data-name='header-toolbar-fullscreen-button']",
          ".layout__area--left",
          ".layout__area--right",
        ];
        hideSel.forEach((sel) => {
          document.querySelectorAll(sel).forEach((el) => (el.style.display = "none"));
        });
        // shrink margins
        document.body.style.margin = "0";
        document.body.style.padding = "0";
      });
    } catch (_) {}

    // screenshot bounding box (chart area)
    let clipRect = null;
    try {
      clipRect = await page.evaluate(() => {
        const el = document.querySelector("div[data-name='chart-area']") ||
                   document.querySelector(".chart-container") ||
                   document.querySelector("tv-chart-view") ||
                   document.body;
        const r = el.getBoundingClientRect();
        return { x: Math.max(0, r.x), y: Math.max(0, r.y), width: r.width, height: r.height };
      });
      // ensure minimum dims
      if (!clipRect || clipRect.width < 200 || clipRect.height < 200) {
        clipRect = { x: 0, y: 0, width: 1280, height: 720 };
      }
    } catch (_) {
      clipRect = { x: 0, y: 0, width: 1280, height: 720 };
    }

    const buf = await page.screenshot({ type: "png", clip: clipRect });
    return buf;
  });
}

// Wrapper w/ multi retry + size guarantee
async function getTradingViewSnapshot(pair, tf = DEFAULT_TF, theme = DEFAULT_THEME) {
  let lastErr = null;
  for (let i = 1; i <= PNG_RETRIES; i++) {
    try {
      const png = await captureTradingViewChart(pair, tf, theme);
      if (png && png.length >= MIN_PNG_SIZE) {
        debugSavePNG(png, `snap_${safeFilename(pair)}_${tf}_${theme}`);
        return png;
      }
      lastErr = `PNG too small (${png ? png.length : 0} bytes)`;
      console.warn(`[${ts()}] Snapshot attempt ${i} small -> retry…`);
    } catch (err) {
      lastErr = err.message || String(err);
      console.warn(`[${ts()}] Snapshot attempt ${i} failed: ${lastErr}`);
    }
    if (i < PNG_RETRIES) await sleep(PNG_RETRY_DELAY_MS);
  }
  throw new Error(lastErr || "snapshot failed");
}

// ---------------------------------------------------------------------------
// Candle Data (stub & optional remote feed)
// ---------------------------------------------------------------------------

// TODO: Replace stub with real provider: Polygon, TwelveData, Tiingo, AlphaVantage, TradingView unofficial.
async function getCandlesJSON(pair, tf = DEFAULT_TF, limit = 50) {
  // stub: random OHLC near 1.0 for FX; near 100 for indices.
  const scale = pair.includes("BTC") || pair.includes("ETH") ? 1000 : pair.includes("US30") ? 50000 : 1;
  const candles = [];
  let last = scale;
  for (let i = 0; i < limit; i++) {
    const open = last;
    const chg = (Math.random() - 0.5) * scale * 0.001;
    const close = open + chg;
    const high = Math.max(open, close) + Math.random() * scale * 0.0005;
    const low = Math.min(open, close) - Math.random() * scale * 0.0005;
    const vol = Math.floor(Math.random() * 1000);
    candles.unshift({
      time: Date.now() - (limit - i) * 60 * 1000,
      open, high, low, close, volume: vol,
    });
    last = close;
  }
  return {
    source: "server.js",
    ts: Date.now(),
    pair,
    tf,
    candles,
  };
}

// ---------------------------------------------------------------------------
// Lightweight TA (for /analyze & Python bot)
// ---------------------------------------------------------------------------

// simple EMA
function ema(values, length) {
  if (!values.length) return [];
  const k = 2 / (length + 1);
  let prev = values[0];
  const out = [prev];
  for (let i = 1; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k);
    out.push(prev);
  }
  // align to last N
  return out.map((v) => v);
}

// RSI simplified (Wilder style approx)
function rsi(values, length = 14) {
  if (values.length < 2) return Array(values.length).fill(50);
  let gains = 0, losses = 0;
  for (let i = 1; i <= length && i < values.length; i++) {
    const diff = values[i] - values[i - 1];
    if (diff >= 0) gains += diff; else losses -= diff;
  }
  gains /= length;
  losses /= length;
  const arr = [];
  let rs = losses === 0 ? 0 : gains / losses;
  arr[length] = 100 - 100 / (1 + rs);
  let avgGain = gains, avgLoss = losses;
  for (let i = length + 1; i < values.length; i++) {
    const diff = values[i] - values[i - 1];
    const gain = diff > 0 ? diff : 0;
    const loss = diff < 0 ? -diff : 0;
    avgGain = (avgGain * (length - 1) + gain) / length;
    avgLoss = (avgLoss * (length - 1) + loss) / length;
    rs = avgLoss === 0 ? 0 : avgGain / avgLoss;
    arr[i] = 100 - 100 / (1 + rs);
  }
  // fill head
  for (let i = 0; i < length; i++) arr[i] = arr[length];
  return arr;
}

// ATR (high-low true range only approx)
function atr(candles, length = 14) {
  if (!candles.length) return [];
  const trs = [];
  for (let i = 0; i < candles.length; i++) {
    const c = candles[i];
    const tr = c.high - c.low;
    trs.push(tr);
  }
  // Wilder smoothing rough
  let acc = 0;
  for (let i = 0; i < length && i < trs.length; i++) acc += trs[i];
  const out = [];
  let prev = acc / length;
  out[length - 1] = prev;
  for (let i = length; i < trs.length; i++) {
    prev = (prev * (length - 1) + trs[i]) / length;
    out[i] = prev;
  }
  for (let i = 0; i < length - 1; i++) out[i] = out[length - 1];
  return out;
}

// generate quick TA summary + signal
function analyzeCandles(candles, opts = {}) {
  const fastLen = opts.fastLen || 7;
  const slowLen = opts.slowLen || 25;
  const rsiLen = opts.rsiLen || 14;
  const atrLen = opts.atrLen || 14;

  const closes = candles.map((c) => c.close);
  const highs = candles.map((c) => c.high);
  const lows = candles.map((c) => c.low);

  const emaFast = ema(closes, fastLen);
  const emaSlow = ema(closes, slowLen);
  const rsiArr = rsi(closes, rsiLen);
  const atrArr = atr(candles, atrLen);

  const lastIdx = closes.length - 1;
  const lastClose = closes[lastIdx];
  const lastEmaFast = emaFast[lastIdx];
  const lastEmaSlow = emaSlow[lastIdx];
  const lastRSI = rsiArr[lastIdx];
  const lastATR = atrArr[lastIdx];

  // direction heuristics
  let direction = "NEUTRAL";
  let confidence = 50;

  if (lastEmaFast > lastEmaSlow) {
    direction = "CALL";
    confidence += 15;
  } else if (lastEmaFast < lastEmaSlow) {
    direction = "PUT";
    confidence += 15;
  }

  if (lastRSI <= 30) {
    // oversold → call bias
    if (direction === "PUT") {
      direction = "NEUTRAL";
      confidence -= 10;
    } else {
      direction = "CALL";
      confidence += 10;
    }
  } else if (lastRSI >= 70) {
    // overbought → put bias
    if (direction === "CALL") {
      direction = "NEUTRAL";
      confidence -= 10;
    } else {
      direction = "PUT";
      confidence += 10;
    }
  }

  // last candle body direction
  const lastC = candles[lastIdx];
  if (lastC.close > lastC.open) {
    if (direction === "PUT") confidence -= 5; else confidence += 3;
  } else if (lastC.close < lastC.open) {
    if (direction === "CALL") confidence -= 5; else confidence += 3;
  }

  // clamp
  if (confidence < 1) confidence = 1;
  if (confidence > 99) confidence = 99;

  // expiry suggestions (rough: ATR scaling)
  const tickRange = lastATR || (lastC.high - lastC.low);
  let baseMin = 1;
  if (tickRange > 0) {
    // bigger volatility → longer expiry
    if (tickRange > Math.abs(lastClose) * 0.002) baseMin = 5;
    else if (tickRange > Math.abs(lastClose) * 0.001) baseMin = 3;
    else baseMin = 1;
  }
  const expiries = [baseMin, baseMin + 2, baseMin + 4, 15].map((m) => `${m}m`);

  return {
    indicators: {
      emaFast: lastEmaFast,
      emaSlow: lastEmaSlow,
      rsi: lastRSI,
      atr: lastATR,
    },
    signal: {
      direction,
      confidence,
      expiries,
    },
  };
}

// ---------------------------------------------------------------------------
// Endpoint: /healthz
// ---------------------------------------------------------------------------
app.get("/healthz", (req, res) => {
  res.json({
    status: "ok",
    browser: !!browser,
    ts: Date.now(),
  });
});

// ---------------------------------------------------------------------------
// Endpoint: /start-browser (manual spin‑up)
// ---------------------------------------------------------------------------
app.get("/start-browser", async (req, res) => {
  try {
    await launchBrowser();
    res.json({ status: "browser ready", ts: Date.now() });
  } catch (err) {
    res.status(500).json({ error: "launch failed", details: err.message });
  }
});

// ---------------------------------------------------------------------------
// Endpoint: /pairs  (category lists)
//   ?cat=fx|otc|indices|crypto|all
// ---------------------------------------------------------------------------
app.get("/pairs", (req, res) => {
  const cat = String(req.query.cat || "all").toLowerCase();
  let list;
  switch (cat) {
    case "fx": list = FX_PAIRS; break;
    case "otc": list = OTC_PAIRS; break;
    case "indices": list = INDEX_SYMBOLS; break;
    case "crypto": list = CRYPTO_SYMBOLS; break;
    default: list = ALL_PAIRS; break;
  }
  res.json({
    source: "server.js",
    ts: Date.now(),
    category: cat,
    count: list.length,
    pairs: list,
  });
});

// ---------------------------------------------------------------------------
// Endpoint: /snapshot/:pair
//   -> PNG (default) or JSON (fmt=json || candles=1)
// ---------------------------------------------------------------------------
app.get("/snapshot/:pair", async (req, res) => {
  const pairRaw = req.params.pair;
  const { tf = DEFAULT_TF, theme = DEFAULT_THEME, fmt, candles, limit } = req.query;
  const pair = normPair(pairRaw);
  const nTF = normTF(tf);
  const nTheme = normTheme(theme);

  try {
    if (fmt === "json" || candles === "1") {
      const data = await getCandlesJSON(pair, nTF, Number(limit) || 50);
      return res.json(data);
    }

    const png = await getTradingViewSnapshot(pair, nTF, nTheme);
    res.setHeader("Content-Type", "image/png");
    res.send(png);
  } catch (err) {
    console.error(`[${ts()}] Snapshot error (${pair}):`, err);
    res.status(500).json({ error: "snapshot failed", details: err.message });
  }
});

// ---------------------------------------------------------------------------
// Endpoint: /analyze/:pair
//   -> JSON TA; if ?img=1 also return base64 png
//   query: tf, theme, limit
// ---------------------------------------------------------------------------
app.get("/analyze/:pair", async (req, res) => {
  const pairRaw = req.params.pair;
  const { tf = DEFAULT_TF, theme = DEFAULT_THEME, limit = 100, img = "0" } = req.query;
  const pair = normPair(pairRaw);
  const nTF = normTF(tf);
  const nTheme = normTheme(theme);
  const lim = Math.min(Math.max(parseInt(limit, 10) || 100, 10), 1000);

  try {
    // get candles
    const data = await getCandlesJSON(pair, nTF, lim);
    const ta = analyzeCandles(data.candles);

    let imgB64 = null;
    if (String(img) === "1") {
      try {
        const png = await getTradingViewSnapshot(pair, nTF, nTheme);
        imgB64 = png.toString("base64");
      } catch (pngErr) {
        console.warn(`[${ts()}] analyze() snapshot optional image error:`, pngErr);
      }
    }

    res.json({
      source: "server.js",
      ts: Date.now(),
      pair,
      tf: nTF,
      theme: nTheme,
      candles: data.candles.length,
      ...ta,
      image_b64: imgB64,
    });
  } catch (err) {
    console.error(`[${ts()}] Analyze error (${pair}):`, err);
    res.status(500).json({ error: "analyze failed", details: err.message });
  }
});

// ---------------------------------------------------------------------------
// Legacy: /run?exchange=FX&ticker=EURUSD&interval=1&theme=dark
// ---------------------------------------------------------------------------
app.get("/run", async (req, res) => {
  const { exchange = "FX", ticker = "EURUSD", interval = DEFAULT_TF, theme = DEFAULT_THEME } = req.query;
  const pair = normPair(`${exchange}:${ticker}`);
  const nTF = normTF(interval);
  const nTheme = normTheme(theme);

  try {
    const png = await getTradingViewSnapshot(pair, nTF, nTheme);
    res.setHeader("Content-Type", "image/png");
    res.send(png);
  } catch (err) {
    console.error(`[${ts()}] /run error:`, err);
    res.status(500).json({ error: "run snapshot failed", details: err.message });
  }
});

// ---------------------------------------------------------------------------
// Static cache (debug PNG saves, if enabled)
// ---------------------------------------------------------------------------
app.use("/cache", express.static(CACHE_DIR));

// ---------------------------------------------------------------------------
// Start Server
// ---------------------------------------------------------------------------
app.listen(PORT, async () => {
  console.log(`[${ts()}] ✅ TradingView Snapshot Server running on http://localhost:${PORT}`);
  try {
    await launchBrowser();
  } catch (_) {} // ignore
});

// ---------------------------------------------------------------------------
// Graceful shutdown
// ---------------------------------------------------------------------------
process.on("SIGINT", async () => {
  console.log(`[${ts()}] Closing browser…`);
  try { if (browser) await browser.close(); } catch (_) {}
  process.exit(0);
});
