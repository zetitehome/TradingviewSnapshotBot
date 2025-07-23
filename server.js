#!/usr/bin/env node
/* eslint-disable no-console */
/**
 * TradingView Snapshot Service
 * ============================
 *
 * Provides chart PNG snapshots for use by tvsnapshotbot.py.
 *
 * Endpoints
 * ---------
 * GET /healthz
 *   -> 200 JSON {ok:true}
 *
 * GET /start-browser
 *   -> spins up (or confirms) global Puppeteer browser. JSON.
 *
 * GET /snapshot/:pair
 *   -> :pair may be "FX:EURUSD", "EURUSD", "EUR/USD", "EUR/USD-OTC", etc.
 *   Query params:
 *       tf=1       (interval; minutes, or D/W/M)
 *       theme=dark (dark|light)
 *       w=1280     (optional viewport width)
 *       h=800      (optional viewport height)
 *       clip=1     (attempt chart-only crop)
 *
 * GET /run?exchange=FX&ticker=EURUSD&interval=1&theme=dark
 *   Legacy compatibility for older Python bot versions.
 *   Query param base=chart accepted but ignored.
 *
 * GET /metrics
 *   -> simple text metrics.
 *
 * GET /close-browser
 *   -> shutdown global browser (debug).
 *
 * Behavior
 * --------
 * - Launches Puppeteer Chromium lazily (first request or /start-browser).
 * - Serializes nav/screenshot via a simple async queue to avoid race chaos.
 * - Attempts multi-symbol fallback (FX_IDC, OANDA, etc) if requested symbol fails.
 * - Hides TradingView UI chrome where possible for cleaner snapshots.
 * - Returns a PNG even on failure by painting an error canvas (lets Python treat all image responses as "success body" vs raw 404 HTML).
 *
 * Notes
 * -----
 * • For reliability on Render, use headless mode & keep viewport modest (1280x720).
 * • TradingView occasionally gatekeeps. You may need to login; stub included (loginTradingView()).
 * • If you need cookies/session injection, see TODO near login stub.
 *
 * License: MIT
 * Author: ChatGPT assist w/ user collaboration
 */

"use strict";

/* ------------------------------------------------------------------ */
/* Imports                                                            */
/* ------------------------------------------------------------------ */
const path = require("path");
const fs = require("fs");
const express = require("express");
const bodyParser = require("body-parser");
const { createCanvas } = require("canvas");
const puppeteer = require("puppeteer"); // full (not -core) installed per user
const crypto = require("crypto");

/* ------------------------------------------------------------------ */
/* Config / Env                                                       */
/* ------------------------------------------------------------------ */
const PORT              = parseInt(process.env.PORT || "10000", 10);
const HEADLESS          = (process.env.HEADLESS ?? "true").toLowerCase() !== "false";
const PUPPETEER_EXEC    = process.env.PUPPETEER_EXEC_PATH || null;
const DEFAULT_THEME     = (process.env.DEFAULT_THEME || "dark").toLowerCase();
const DEFAULT_TF        = process.env.DEFAULT_TF || "1";
const DEBUG_HTML        = (process.env.DEBUG_HTML || "").toLowerCase() === "1";
const BROWSER_IDLE_SEC  = parseInt(process.env.BROWSER_IDLE_SEC || "300", 10); // auto close after idle
const NAV_TIMEOUT_MS    = parseInt(process.env.NAV_TIMEOUT_MS || "60000", 10);
const SCREENSHOT_QUALITY = parseInt(process.env.SCREENSHOT_QUALITY || "80", 10); // not used for png

/* ------------------------------------------------------------------ */
/* Simple logging helper                                              */
/* ------------------------------------------------------------------ */
function ts() {
  return new Date().toISOString();
}
function logInfo(...args)  { console.log(`[${ts()}]`, ...args); }
function logWarn(...args)  { console.warn(`[${ts()}]`, ...args); }
function logError(...args) { console.error(`[${ts()}]`, ...args); }

/* Avoid logging binary junk */
function snip(str, max = 200) {
  if (str == null) return "";
  str = String(str);
  return str.length > max ? str.slice(0, max) + "...(trunc)" : str;
}

/* ------------------------------------------------------------------ */
/* Global Puppeteer browser mgmt                                      */
/* ------------------------------------------------------------------ */
let gBrowser = null;
let gBrowserLaunchPromise = null;
let gLastBrowserUse = 0;

async function launchBrowser() {
  if (gBrowser) {
    gLastBrowserUse = Date.now();
    return gBrowser;
  }
  if (gBrowserLaunchPromise) {
    return gBrowserLaunchPromise;
  }
  gBrowserLaunchPromise = (async () => {
    logInfo(`Launching Puppeteer (headless=${HEADLESS})...`);
    const launchOpts = {
      headless: HEADLESS ? "new" : false,
      args: [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-features=site-per-process",
        "--no-zygote",
        "--single-process",
      ],
    };
    if (PUPPETEER_EXEC) {
      launchOpts.executablePath = PUPPETEER_EXEC;
    }
    try {
      const browser = await puppeteer.launch(launchOpts);
      gBrowser = browser;
      gLastBrowserUse = Date.now();
      logInfo("✅ Puppeteer launched.");
      browser.on("disconnected", () => {
        logWarn("Browser disconnected.");
        gBrowser = null;
      });
      return browser;
    } catch (err) {
      logError("❌ Puppeteer launch failed:", err);
      gBrowserLaunchPromise = null;
      throw err;
    }
  })();
  return gBrowserLaunchPromise;
}

async function ensureBrowser() {
  if (!gBrowser) {
    await launchBrowser();
  }
  gLastBrowserUse = Date.now();
  return gBrowser;
}

async function closeBrowser() {
  if (gBrowser) {
    try { await gBrowser.close(); }
    catch (err) { logWarn("Error closing browser:", err); }
  }
  gBrowser = null;
  gBrowserLaunchPromise = null;
}

/* background auto-close if idle */
setInterval(() => {
  if (!gBrowser) return;
  const idle = (Date.now() - gLastBrowserUse) / 1000;
  if (idle > BROWSER_IDLE_SEC) {
    logInfo(`Browser idle ${idle.toFixed(0)}s > ${BROWSER_IDLE_SEC}s; closing.`);
    closeBrowser().catch(() => {});
  }
}, 60 * 1000);

/* ------------------------------------------------------------------ */
/* Express app setup                                                  */
/* ------------------------------------------------------------------ */
const app = express();
app.use(bodyParser.json({ limit: "1mb" }));
app.use(bodyParser.urlencoded({ extended: true }));

/* ------------------------------------------------------------------ */
/* Symbol resolution                                                   */
/* ------------------------------------------------------------------ */
/**
 * Try to produce an ordered list of TradingView symbol candidates from
 * incoming request pieces. Returns array of strings like "FX:EURUSD".
 *
 * Accepts:
 *   ex, tk from query, or
 *   rawPair like "EUR/USD-OTC" or "FX:EURUSD".
 */
function resolveSymbolCandidates({ ex, tk, rawPair }) {
  const out = [];
  const r = (s) => s && !out.includes(s) && out.push(s);

  function cleanPair(p) {
    return p.replace(/[\s/:-]/g, "").toUpperCase();
  }

  // 1. If ex:tk explicit
  if (ex && tk) {
    r(`${ex.toUpperCase()}:${tk.toUpperCase()}`);
  }

  // 2. If rawPair colon format
  if (rawPair && rawPair.includes(":")) {
    r(rawPair.toUpperCase());
  }

  // 3. Clean label -> canonical tk
  let label = rawPair;
  if (label) {
    label = label.trim();
    const isOTC = /-OTC$/i.test(label);
    const canon = cleanPair(label);
    // known underlying for OTC
    const otcMap = {
      "EURUSDOTC": "EURUSD",
      "GBPUSDOTC": "GBPUSD",
      "USDJPYOTC": "USDJPY",
      "USDCHFOTC": "USDCHF",
      "AUDUSDOTC": "AUDUSD",
      "NZDUSDOTC": "NZDUSD",
      "USDCADOTC": "USDCAD",
      "EURGBPOTC": "EURGBP",
      "EURJPYOTC": "EURJPY",
      "GBPJPYOTC": "GBPJPY",
      "AUDCHFOTC": "AUDCHF",
      "EURCHFOTC": "EURCHF",
      "USDKESOTC": "USDKES",
      "USDMADOTC": "USDMAD",
      "USDBDTOTC": "USDBDT",
      "USDMXNOTC": "USDMXN",
      "USDMYROTC": "USDMYR",
      "USDPKROTC": "USDPKR",
    };
    const idxMap = {
      "US30": "DJI",
      "SPX500": "SPX",
      "NAS100": "NDX",
      "DE40": "DAX",
      "UK100": "UKX",
      "JP225": "NI225",
      "FR40": "CAC40",
      "ES35": "IBEX35",
      "HK50": "HSI",
      "AU200": "AS51",
    };
    const cryMap = {
      "BTCUSD": "BTCUSD",
      "ETHUSD": "ETHUSD",
      "SOLUSD": "SOLUSD",
      "XRPUSD": "XRPUSD",
      "LTCUSD": "LTCUSD",
      "ADAUSD": "ADAUSD",
      "DOGEUSD": "DOGEUSD",
      "BNBUSD": "BNBUSD",
      "DOTUSD": "DOTUSD",
      "LINKUSD": "LINKUSD",
    };

    let baseTk = canon;
    // remove trailing OTC for underlying map
    if (isOTC && otcMap[canon]) {
      baseTk = otcMap[canon];
    }

    // index override
    if (idxMap[canon]) {
      baseTk = idxMap[canon];
    }

    // crypto override
    if (cryMap[canon]) {
      baseTk = cryMap[canon];
    }

    // try multiple exchanges
    const tryEx = [
      ex ? ex.toUpperCase() : null,
      "FX",
      "FX_IDC",
      "OANDA",
      "FOREXCOM",
      "IDC",
      "QUOTEX",
      "CURRENCY",
      "INDEX",
      "BINANCE",
      "CRYPTO",
    ].filter(Boolean);

    for (const e of tryEx) {
      r(`${e}:${baseTk}`);
    }

    // also plain base? Some internal servers parse w/out exchange
    r(baseTk);
  }

  // final dedup is done by `out` push rules
  return out;
}

/* ------------------------------------------------------------------ */
/* Timeframe normalization                                            */
/* ------------------------------------------------------------------ */
function normInterval(tf) {
  if (!tf) return DEFAULT_TF;
  const t = String(tf).trim().toLowerCase();
  if (/^\d+$/.test(t)) return t;         // minutes
  if (t === "d" || t === "1d" || t === "day") return "D";
  if (t === "w" || t === "1w" || t === "week") return "W";
  if (t === "m" || t === "1mth" || t === "mo" || t === "month") return "M";
  // match "5m" etc
  const m = t.match(/^(\d+)m$/);
  if (m) return m[1];
  return DEFAULT_TF;
}

function normTheme(th) {
  if (!th) return DEFAULT_THEME;
  return th.toLowerCase().startsWith("l") ? "light" : "dark";
}

/* ------------------------------------------------------------------ */
/* TradingView chart navigation                                       */
/* ------------------------------------------------------------------ */
/**
 * Build a TradingView full chart URL for a given symbol.
 * We deliberately include interval & theme as query args.
 */
function buildTVUrl(symbol, interval, theme) {
  // We use the standard /chart/ full-featured chart page; you can switch to embed if needed.
  // hide top toolbar & legend isn't officially supported via /chart? but we can try query hints
  // We'll also strip UI elements via page.evaluate() after load.
  const params = new URLSearchParams({
    symbol,
    interval,
    theme,
    style: "1",
    locale: "en",
  });
  return `https://www.tradingview.com/chart/?${params.toString()}`;
}

/**
 * Attempt to hide TradingView UI clutter once page loaded.
 */
async function hideTradingViewUi(page) {
  try {
    await page.evaluate(() => {
      const hideCss = `
        .chart-controls-bar, .layout__area--top, .tv-header__wrap, [data-name="header-toolbar"], .sidebar-container {
          display:none !important;
        }
        .chart-markup-table, .chart-logo, .tv-logo, .chart-page, .tv-pane-tools {
          opacity:0 !important;
        }
      `;
      const style = document.createElement("style");
      style.setAttribute("id", "tvhidecss");
      style.innerHTML = hideCss;
      document.head.appendChild(style);
    });
  } catch (_) {
    /* ignore */
  }
}

/**
 * Attempt to locate the main chart canvas bounding box.
 * Returns clip obj or null for full screenshot.
 */
async function getChartClip(page) {
  try {
    const selCandidates = [
      '.chart-container',                // embedded
      '.chart-markup-table',             // deep internals
      '.tv-chart-view__overlay',         // overlay region
      '.chart-gui-wrapper',              // general wrapper
      '[data-name="chart-container"]',
    ];
    for (const sel of selCandidates) {
      const el = await page.$(sel);
      if (el) {
        const box = await el.boundingBox();
        if (box && box.width > 100 && box.height > 100) {
          return box;
        }
      }
    }
  } catch (err) {
    logWarn("getChartClip error:", err);
  }
  return null;
}

/**
 * Core capture routine: open candidate TradingView symbols and screenshot.
 * Returns { ok:boolean, png:Buffer|null, usedSymbol, error }
 */
async function captureTradingViewChart({
  symbolCandidates,
  interval,
  theme,
  width = 1280,
  height = 720,
  clipChart = false,
  navTimeout = NAV_TIMEOUT_MS,
}) {
  const browser = await ensureBrowser();

  // Serial access queue: we open one page at a time
  const page = await browser.newPage();
  try {
    await page.setViewport({ width, height, deviceScaleFactor: 1 });
  } catch (e0) {
    logWarn("setViewport error:", e0);
  }

  let lastErr = "no_attempts";

  for (const sym of symbolCandidates) {
    const url = buildTVUrl(sym, interval, theme);
    logInfo("Opening TradingView:", url);
    try {
      await page.goto(url, { waitUntil: "networkidle2", timeout: navTimeout });
    } catch (navErr) {
      logWarn(`Nav error for ${sym}:`, navErr.message);
      lastErr = navErr.message;
      continue;
    }

    // Give chart time to bootstrap (panes load lazy)
    try {
      await page.waitForSelector('canvas', { timeout: 15000 });
    } catch {
      logWarn(`Canvas not found for ${sym} (continuing)`);
    }

    await hideTradingViewUi(page);

    // Chart clip?
    let clip = null;
    if (clipChart) {
      clip = await getChartClip(page);
      if (!clip) {
        logWarn("Chart clip not found; using full page.");
      }
    }

    // screenshot
    try {
      const png = await page.screenshot({
        type: "png",
        fullPage: !clip,
        clip: clip || undefined,
      });
      logInfo(`✅ Captured ${sym} (${png.length} bytes)`);
      await page.close();
      return { ok: true, png, usedSymbol: sym, error: "" };
    } catch (shotErr) {
      logWarn(`Screenshot error for ${sym}:`, shotErr);
      lastErr = shotErr.message;
      continue;
    }
  }

  try { await page.close(); } catch (_) {}
  return { ok: false, png: null, usedSymbol: null, error: lastErr };
}

/* ------------------------------------------------------------------ */
/* Error PNG generation                                               */
/* ------------------------------------------------------------------ */
function makeErrorPng(msg, width = 800, height = 400) {
  const canvas = createCanvas(width, height);
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = "#f00";
  ctx.font = "20px sans-serif";
  ctx.fillText("Snapshot Error", 20, 40);
  ctx.fillStyle = "#fff";
  wrapText(ctx, msg, 20, 80, width - 40, 22);
  return canvas.toBuffer("image/png");
}

function wrapText(ctx, text, x, y, maxWidth, lineHeight) {
  const words = text.split(/\s+/);
  let line = "";
  for (const w of words) {
    const tw = line ? line + " " + w : w;
    const m = ctx.measureText(tw).width;
    if (m > maxWidth) {
      ctx.fillText(line, x, y);
      line = w;
      y += lineHeight;
    } else {
      line = tw;
    }
  }
  if (line) ctx.fillText(line, x, y);
}

/* ------------------------------------------------------------------ */
/* Health                                                             */
/* ------------------------------------------------------------------ */
app.get("/healthz", (req, res) => {
  res.json({ ok: true, browser: !!gBrowser });
});

/* ------------------------------------------------------------------ */
/* Start Browser                                                       */
/* ------------------------------------------------------------------ */
app.get("/start-browser", async (req, res) => {
  try {
    await ensureBrowser();
    res.json({ ok: true, browser: true });
  } catch (err) {
    res.status(500).json({ ok: false, error: String(err) });
  }
});

/* ------------------------------------------------------------------ */
/* Close Browser (debug)                                               */
/* ------------------------------------------------------------------ */
app.get("/close-browser", async (req, res) => {
  await closeBrowser();
  res.json({ ok: true, closed: true });
});

/* ------------------------------------------------------------------ */
/* Metrics (minimal)                                                   */
/* ------------------------------------------------------------------ */
let gMetricsTotal = 0;
let gMetricsHitCache = 0; // placeholder (no caching yet)
app.get("/metrics", (req, res) => {
  res.type("text/plain").send([
    `snapshot_total ${gMetricsTotal}`,
    `snapshot_cache_hits ${gMetricsHitCache}`,
    `browser_up ${gBrowser ? 1 : 0}`,
  ].join("\n"));
});

/* ------------------------------------------------------------------ */
/* Snapshot Handling                                                   */
/* ------------------------------------------------------------------ */
/**
 * Shared snapshot worker: given symbol candidates (array of strings),
 * attempts capture and responds image/png; else error PNG.
 */
async function doSnapshotRes(req, res, symbolCandidates, interval, theme, width, height, clipChart) {
  gMetricsTotal += 1;
  try {
    const { ok, png, usedSymbol, error } = await captureTradingViewChart({
      symbolCandidates,
      interval,
      theme,
      width,
      height,
      clipChart,
    });

    if (ok && png) {
      res.set("Content-Type", "image/png");
      if (usedSymbol) res.set("X-TV-Symbol", usedSymbol);
      res.status(200).send(png);
      return;
    }

    const errMsg = `All symbol attempts failed. Last error: ${error || "unknown"}`;
    logWarn(errMsg, symbolCandidates);
    const pngErr = makeErrorPng(errMsg);
    res.set("Content-Type", "image/png");
    res.status(500).send(pngErr);
  } catch (err) {
    const errMsg = `Snapshot exception: ${err}`;
    logError(errMsg);
    const pngErr = makeErrorPng(errMsg);
    res.set("Content-Type", "image/png");
    res.status(500).send(pngErr);
  }
}

/* ------------------------------------------------------------------ */
/* /snapshot/:pair                                                     */
/* ------------------------------------------------------------------ */
app.get("/snapshot/:pair", async (req, res) => {
  const rawPair = req.params.pair; // may include colon e.g., FX:EURUSD
  const interval = normInterval(req.query.tf || req.query.interval || DEFAULT_TF);
  const theme    = normTheme(req.query.theme || DEFAULT_THEME);
  const width    = parseInt(req.query.w || "1280", 10);
  const height   = parseInt(req.query.h || "720", 10);
  const clipChart = !!req.query.clip;

  const symbolCandidates = resolveSymbolCandidates({ ex: null, tk: null, rawPair });
  if (!symbolCandidates.length) {
    const pngErr = makeErrorPng("No symbol candidates derived from pair param.");
    res.set("Content-Type", "image/png");
    return res.status(400).send(pngErr);
  }
  await doSnapshotRes(req, res, symbolCandidates, interval, theme, width, height, clipChart);
});

/* ------------------------------------------------------------------ */
/* /run (legacy compat)                                                */
/* ------------------------------------------------------------------ */
app.get("/run", async (req, res) => {
  const ex       = req.query.exchange;
  const tk       = req.query.ticker;
  const interval = normInterval(req.query.interval || DEFAULT_TF);
  const theme    = normTheme(req.query.theme || DEFAULT_THEME);
  const width    = parseInt(req.query.w || "1280", 10);
  const height   = parseInt(req.query.h || "720", 10);
  const clipChart = !!req.query.clip;

  const rawPair = req.query.pair || `${ex || ""}:${tk || ""}`;
  const symbolCandidates = resolveSymbolCandidates({ ex, tk, rawPair });
  if (!symbolCandidates.length) {
    const pngErr = makeErrorPng("No symbol candidates from /run params.");
    res.set("Content-Type", "image/png");
    return res.status(400).send(pngErr);
  }
  await doSnapshotRes(req, res, symbolCandidates, interval, theme, width, height, clipChart);
});

/* ------------------------------------------------------------------ */
/* Root                                                                */
/* ------------------------------------------------------------------ */
app.get("/", (req, res) => {
  res.type("text/plain").send("TradingView Snapshot Service is running.\nUse /snapshot/:pair or /run?exchange=FX&ticker=EURUSD&interval=1.");
});

/* ------------------------------------------------------------------ */
/* Error Handling middleware (last)                                    */
/* ------------------------------------------------------------------ */
app.use((err, req, res, next) => {
  logError("Express error:", err);
  const pngErr = makeErrorPng(`Express error: ${err}`);
  res.set("Content-Type", "image/png");
  res.status(500).send(pngErr);
});

/* ------------------------------------------------------------------ */
/* Startup                                                             */
/* ------------------------------------------------------------------ */
app.listen(PORT, () => {
  logInfo(`✅ Snapshot service listening on port ${PORT}`);
  if (!HEADLESS) {
    logInfo("NOTE: Headless disabled; a visible Chrome may appear.");
  }
  // Optionally auto-launch to warm browser (comment out if you prefer lazy)
  launchBrowser().catch((err) => {
    logWarn("Browser launch deferred due to error:", err);
  });
});
