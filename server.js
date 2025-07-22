#!/usr/bin/env node
/**
 * TradingView Snapshot Service (server.js)
 * ----------------------------------------
 * Express + Puppeteer headless Chromium screenshot microservice.
 *
 * Features
 * --------
 * • /healthz                         → quick health JSON
 * • /start-browser                   → ensure browser up (idempotent)
 * • /close-browser                   → shutdown browser
 * • /run?exchange=FX&ticker=EURUSD&interval=1&theme=dark[&base=chart]
 *      Returns PNG of TradingView chart.
 * • /snapshot/:pair                  → convenience; pair like EURUSD, EUR/USD, FX:EURUSD, gbpusd-otc
 * • Symbol normalization + base URL builder (/chart/?symbol=EX:TK...)
 * • Theme (dark|light), interval min or D/W/M.
 * • Serial navigation lock (avoid concurrent nav races).
 * • Retry nav + networkidle fallback + screenshot wait delay.
 * • Graceful SIGINT/SIGTERM cleanup.
 * • Optional cache of last PNG per (ex,tk,interval,theme) (disabled by default; togglable).
 * • Basic metrics (success/fail counts).
 *
 * Env Vars
 * --------
 * PORT=10000
 * PUPPETEER_TIMEOUT_MS=45000
 * SNAPSHOT_WAIT_MS=5000          (extra settle wait after goto)
 * DEFAULT_EXCHANGE=FX            (fallback if no exchange provided)
 * DEFAULT_INTERVAL=1             (minutes, or D/W/M)
 * DEFAULT_THEME=dark
 * BROWSER_HEADLESS=true|false    (override)
 * PUPPETEER_EXECUTABLE_PATH=...  (optional custom Chrome path)
 *
 * NOTE: This service does not authenticate inbound calls; front with firewall if public.
 */

"use strict";

const path = require("path");
const fs = require("fs");
const os = require("os");
const express = require("express");
const bodyParser = require("body-parser");
const crypto = require("crypto");
const { performance } = require("perf_hooks");
const puppeteer = require("puppeteer");
const { createCanvas, loadImage } = require("canvas"); // used only for placeholder images

// ---------------------------------------------------------------------------
// Config / Env
// ---------------------------------------------------------------------------
const PORT                 = parseInt(process.env.PORT || "10000", 10);
const DEFAULT_EXCHANGE     = (process.env.DEFAULT_EXCHANGE || "FX").toUpperCase();
const DEFAULT_INTERVAL     = process.env.DEFAULT_INTERVAL || "1";
const DEFAULT_THEME        = (process.env.DEFAULT_THEME || "dark").toLowerCase();
const PUPPETEER_TIMEOUT_MS = parseInt(process.env.PUPPETEER_TIMEOUT_MS || "45000", 10);
const SNAPSHOT_WAIT_MS     = parseInt(process.env.SNAPSHOT_WAIT_MS || "5000", 10);
const HEADLESS_OVERRIDE    = process.env.BROWSER_HEADLESS;
const EXEC_PATH            = process.env.PUPPETEER_EXECUTABLE_PATH || null;
const ENABLE_CACHE         = /^true$/i.test(process.env.SNAPSHOT_CACHE || "false");

// Accept e.g. "FX,FX_IDC,OANDA,FOREXCOM,FXCM,IDC,QUOTEX,CURRENCY"
const FALLBACK_EXCHANGES   = (process.env.FALLBACK_EXCHANGES || "FX_IDC,OANDA,FOREXCOM,FXCM,IDC,QUOTEX,CURRENCY")
  .split(",")
  .map(s => s.trim().toUpperCase())
  .filter(Boolean);

const app = express();
app.use(bodyParser.json());

// ---------------------------------------------------------------------------
// Logger (minimal console only — you can redirect at shell level)
// ---------------------------------------------------------------------------
function log(...args) {
  const ts = new Date().toISOString();
  console.log(`[${ts}]`, ...args);
}
function logErr(...args) {
  const ts = new Date().toISOString();
  console.error(`[${ts}]`, ...args);
}

// ---------------------------------------------------------------------------
// Browser Lifecycle
// ---------------------------------------------------------------------------
let browser = null;
let page = null;
let launchPromise = null;
let closing = false;

// A simple queue lock to ensure that only one navigation happens at a time.
// Avoids collisions where multiple requests clobber the same page.
let navLock = Promise.resolve();

async function ensureBrowser() {
  if (browser && page) return;

  if (launchPromise) {
    await launchPromise;
    return;
  }

  launchPromise = (async () => {
    try {
      log("Launching Puppeteer Chromium...");
      const launchOpts = {
        headless: HEADLESS_OVERRIDE
          ? /^true$/i.test(HEADLESS_OVERRIDE)
          : "new", // Puppeteer recommended modern headless (or true)
        // If you installed Chrome via `npx puppeteer browsers install chrome`, set env path:
        executablePath: EXEC_PATH || undefined,
        args: [
          "--no-sandbox",
          "--disable-setuid-sandbox",
          "--disable-dev-shm-usage",
          "--disable-accelerated-2d-canvas",
          "--disable-gpu",
          "--no-zygote",
          "--single-process",
          "--window-size=1920,1080",
        ],
      };

      browser = await puppeteer.launch(launchOpts);
      page = await browser.newPage();
      await page.setUserAgent("Mozilla/5.0 (X11; Linux x86_64)");
      await page.setViewport({ width: 1920, height: 1080 });

      log("✅ Puppeteer launched.");
    } catch (err) {
      logErr("❌ Puppeteer launch failed:", err);
      browser = null;
      page = null;
      throw err;
    } finally {
      launchPromise = null;
    }
  })();

  await launchPromise;
}

async function closeBrowser() {
  if (closing) return;
  closing = true;
  try {
    if (page) {
      await page.close().catch(() => {});
      page = null;
    }
    if (browser) {
      await browser.close().catch(() => {});
      browser = null;
    }
    log("✅ Browser closed.");
  } catch (err) {
    logErr("Error closing browser:", err);
  } finally {
    closing = false;
  }
}

process.on("SIGINT", async () => {
  log("SIGINT; closing browser...");
  await closeBrowser();
  process.exit(0);
});
process.on("SIGTERM", async () => {
  log("SIGTERM; closing browser...");
  await closeBrowser();
  process.exit(0);
});

// ---------------------------------------------------------------------------
// Utility: Build TradingView URL
// ---------------------------------------------------------------------------
function normInterval(tf) {
  if (!tf) return DEFAULT_INTERVAL;
  const t = String(tf).trim().toLowerCase();
  if (/^\d+$/.test(t)) return t; // minutes
  if (t.endsWith("m") && /^\d+m$/.test(t)) return t.slice(0, -1);
  if (t.endsWith("h") && /^\d+h$/.test(t)) return String(parseInt(t) * 60);
  if (["d", "1d", "day"].includes(t)) return "D";
  if (["w", "1w", "week"].includes(t)) return "W";
  if (["m", "1m", "mo", "month"].includes(t)) return "M";
  return DEFAULT_INTERVAL;
}

function normTheme(th) {
  return th && th.toLowerCase().startsWith("l") ? "light" : "dark";
}

// Accept base=chart|ideas|...
function buildTradingViewUrl({ base = "chart", exchange, ticker, interval, theme }) {
  // ensure base ends with /?
  const safeBase = base.includes("?") ? base : `${base}/?`;
  // EX:TK
  const sym = encodeURIComponent(`${exchange}:${ticker}`);
  const intv = encodeURIComponent(interval);
  const th = encodeURIComponent(theme);
  return `https://www.tradingview.com/${safeBase}symbol=${sym}&interval=${intv}&theme=${th}`;
}

// ---------------------------------------------------------------------------
// Symbol Normalization
// ---------------------------------------------------------------------------
/**
 * Accept forms:
 *  - EURUSD
 *  - EUR/USD
 *  - FX:EURUSD
 *  - eurusd-otc
 *  - currency:eurusd
 * Return {exchange, ticker, isOtc}
 */
function normalizePair(raw) {
  if (!raw) {
    return { exchange: DEFAULT_EXCHANGE, ticker: "EURUSD", isOtc: false };
  }
  let s = String(raw).trim().toUpperCase();
  let isOtc = false;
  if (s.endsWith("-OTC") || s.endsWith("_OTC")) {
    isOtc = true;
    s = s.replace(/-OTC|_OTC$/, "");
  }
  if (s.includes(":")) {
    const [ex, tk] = s.split(":", 2);
    return { exchange: ex || DEFAULT_EXCHANGE, ticker: tk || "EURUSD", isOtc };
  }
  // remove slash
  s = s.replace("/", "");
  // remove spaces
  s = s.replace(/\s+/g, "");
  return { exchange: DEFAULT_EXCHANGE, ticker: s, isOtc };
}

// ---------------------------------------------------------------------------
// Optional Simple In‑Memory Cache
// ---------------------------------------------------------------------------
/*
 * Cache key: `${exchange}|${ticker}|${interval}|${theme}`
 * Stores {ts, buf}
 */
const snapshotCache = new Map();
const CACHE_TTL_MS = 30_000; // 30s

function cacheGet(ex, tk, iv, th) {
  if (!ENABLE_CACHE) return null;
  const key = `${ex}|${tk}|${iv}|${th}`;
  const ent = snapshotCache.get(key);
  if (!ent) return null;
  if (Date.now() - ent.ts > CACHE_TTL_MS) {
    snapshotCache.delete(key);
    return null;
  }
  return ent.buf;
}
function cachePut(ex, tk, iv, th, buf) {
  if (!ENABLE_CACHE) return;
  const key = `${ex}|${tk}|${iv}|${th}`;
  snapshotCache.set(key, { ts: Date.now(), buf });
}

// ---------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------
let metricRequests = 0;
let metricSuccess = 0;
let metricFail = 0;

// ---------------------------------------------------------------------------
// Core Screenshot Worker (protected by navLock)
// ---------------------------------------------------------------------------
async function _doCapture(exchange, ticker, interval, theme, base) {
  await ensureBrowser();

  const cached = cacheGet(exchange, ticker, interval, theme);
  if (cached) return cached;

  const url = buildTradingViewUrl({ base, exchange, ticker, interval, theme });
  log("Opening TradingView URL:", url);

  const start = performance.now();
  try {
    // navLock ensures sequential navigation
    navLock = navLock.then(async () => {
      await page.goto(url, {
        waitUntil: "domcontentloaded",
        timeout: PUPPETEER_TIMEOUT_MS,
      });
      // Try waiting for chart root; fallback to manual delay
      try {
        await page.waitForSelector(".tv-chart-view", { timeout: 10000 });
      } catch {
        // ignore; fallback to timed wait
      }
      await page.waitForTimeout(SNAPSHOT_WAIT_MS);
    });
    await navLock; // wait for our place in queue

    const buf = await page.screenshot({ type: "png" });
    cachePut(exchange, ticker, interval, theme, buf);
    const ms = Math.round(performance.now() - start);
    log(`✅ Snapshot captured (${ms}ms) ${exchange}:${ticker} TF=${interval} theme=${theme}`);
    metricSuccess += 1;
    return buf;
  } catch (err) {
    metricFail += 1;
    logErr("❌ Snapshot failed:", err);
    throw err;
  }
}

// Retry wrapper; tries once, then 2 more w/ short backoff.
async function captureTradingViewChart(exchange, ticker, interval, theme, base = "chart") {
  const attempts = 3;
  let lastErr;
  for (let i = 1; i <= attempts; i++) {
    try {
      return await _doCapture(exchange, ticker, interval, theme, base);
    } catch (err) {
      lastErr = err;
      logErr(`Attempt ${i} failed for ${exchange}:${ticker} ->`, err.message || err);
      if (i < attempts) {
        await new Promise(r => setTimeout(r, 2000));
      }
    }
  }
  throw lastErr;
}

// Try multiple exchanges fallback
async function captureWithFallback(primaryEx, ticker, interval, theme, base = "chart", extras = []) {
  const tries = [primaryEx, ...extras, ...FALLBACK_EXCHANGES];
  const tried = new Set();
  let lastErr = null;
  for (const ex of tries) {
    const exU = (ex || "").toUpperCase();
    if (!exU || tried.has(exU)) continue;
    tried.add(exU);
    try {
      const buf = await captureTradingViewChart(exU, ticker, interval, theme, base);
      return { buf, used: exU };
    } catch (err) {
      lastErr = err;
      logErr(`Snapshot fallback fail ${exU}:${ticker}`, err.message || err);
    }
  }
  throw new Error(`All exchanges failed for ${ticker}. Last error: ${lastErr ? lastErr.message : "Unknown"}. Tried: ${[...tried].join(",")}`);
}

// ---------------------------------------------------------------------------
// Placeholder PNG Generator (used on error if requested)
// ---------------------------------------------------------------------------
function makeErrorPng(text) {
  const w = 800, h = 400;
  const canvas = createCanvas(w, h);
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#1e1e1e";
  ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = "#ff5555";
  ctx.font = "bold 26px sans-serif";
  ctx.fillText("Snapshot Error", 30, 60);
  ctx.fillStyle = "#ccc";
  ctx.font = "16px sans-serif";
  wrapText(ctx, text, 30, 100, w - 60, 22);
  return canvas.toBuffer("image/png");
}
function wrapText(ctx, text, x, y, maxWidth, lineHeight) {
  const words = text.split(/\s+/);
  let line = "";
  for (let n = 0; n < words.length; n++) {
    const testLine = line ? line + " " + words[n] : words[n];
    const metrics = ctx.measureText(testLine);
    if (metrics.width > maxWidth && n > 0) {
      ctx.fillText(line, x, y);
      line = words[n];
      y += lineHeight;
    } else {
      line = testLine;
    }
  }
  ctx.fillText(line, x, y);
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

// Health
app.get("/healthz", (req, res) => {
  res.json({
    ok: true,
    browser: !!browser,
    page: !!page,
    metrics: {
      requests: metricRequests,
      success: metricSuccess,
      fail: metricFail,
      cache_entries: snapshotCache.size,
    },
  });
});

// Start browser (idempotent)
app.get("/start-browser", async (req, res) => {
  try {
    await ensureBrowser();
    res.send("✅ Browser ready.");
  } catch (err) {
    logErr("start-browser error:", err);
    res.status(500).send("Failed to start browser: " + err.message);
  }
});

// Close browser
app.get("/close-browser", async (req, res) => {
  try {
    await closeBrowser();
    res.send("✅ Browser closed.");
  } catch (err) {
    res.status(500).send("Error closing browser: " + err.message);
  }
});

// Core /run
app.get("/run", async (req, res) => {
  metricRequests += 1;
  const base = req.query.base || "chart";
  const exchange = (req.query.exchange || DEFAULT_EXCHANGE).toUpperCase();
  const ticker = (req.query.ticker || "EURUSD").toUpperCase();
  const interval = normInterval(req.query.interval);
  const theme = normTheme(req.query.theme);

  const fallback = req.query.fallback
    ? req.query.fallback.split(",").map(s => s.trim().toUpperCase()).filter(Boolean)
    : [];

  try {
    const { buf, used } = await captureWithFallback(exchange, ticker, interval, theme, base, fallback);
    res.set("Content-Type", "image/png");
    res.set("X-Exchange-Used", used);
    res.send(buf);
  } catch (err) {
    logErr("Final snapshot error:", err);
    const png = makeErrorPng(err.message || "Unknown error");
    res.set("Content-Type", "image/png");
    res.set("X-Error", "true");
    res.status(500).send(png);
  }
});

// Friendly /snapshot/:pair
//   GET /snapshot/EURUSD?interval=5&theme=light
//   GET /snapshot/EUR/USD-OTC?interval=1
// Accept fallback=FX_IDC,OANDA
app.get("/snapshot/:pair", async (req, res) => {
  metricRequests += 1;
  const { exchange, ticker } = normalizePair(req.params.pair);
  const interval = normInterval(req.query.interval);
  const theme = normTheme(req.query.theme);

  const fallback = req.query.fallback
    ? req.query.fallback.split(",").map(s => s.trim().toUpperCase()).filter(Boolean)
    : [];

  try {
    const { buf, used } = await captureWithFallback(exchange, ticker, interval, theme, "chart", fallback);
    res.set("Content-Type", "image/png");
    res.set("X-Exchange-Used", used);
    res.send(buf);
  } catch (err) {
    logErr("Snapshot route error:", err);
    const png = makeErrorPng(err.message || "Unknown error");
    res.set("Content-Type", "image/png");
    res.set("X-Error", "true");
    res.status(500).send(png);
  }
});

// Root help
app.get("/", (req, res) => {
  res.type("text/plain").send(
`TradingView Snapshot Service
----------------------------
Use:
/healthz
/start-browser
/run?exchange=FX&ticker=EURUSD&interval=1&theme=dark
/snapshot/EURUSD?interval=1&theme=dark
/snapshot/EUR/USD-OTC?interval=5
/close-browser
`);
});

// 404 fallback
app.use((req, res) => {
  res.status(404).send("Not Found");
});

// ---------------------------------------------------------------------------
// Start server
// ---------------------------------------------------------------------------
app.listen(PORT, () => {
  log(`✅ Snapshot service listening on port ${PORT}`);
  // Launch in background
  ensureBrowser().catch((e) => logErr("initial browser launch fail:", e));
});
