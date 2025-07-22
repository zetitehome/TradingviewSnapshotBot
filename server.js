/**
 * server.js
 * ===========================================================================
 * TradingView Snapshot Microservice
 * ===========================================================================
 * Features
 * --------
 * • Launches & reuses a single headless Chromium (Puppeteer).
 * • Captures TradingView chart screenshots for given symbols/pairs.
 * • Extended timeout + post-load settle delay (fixes 30s timeout issues).
 * • Backward-compat /run endpoint (exchange=, ticker=, interval=, theme=).
 * • Friendly /snapshot/:pair endpoint (default exchange, defaults).
 * • Flexible /snapshot?pair=EURUSD&ex=FX&tf=1&theme=dark&delay=5s.
 * • Multi-symbol /snapshotlist endpoint (returns ZIP or JSON w/base64).
 * • Optional watermark text overlay (uses node-canvas if installed).
 * • Exchange fallback list to try alternate feeds if the first 404s.
 * • Concurrency semaphore to prevent resource overload.
 * • Metrics + health endpoints (/healthz, /metrics, /version).
 * • Graceful SIGINT/SIGTERM shutdown; try auto-relaunch on browser crash.
 *
 * Intended Use
 * ------------
 * This service is meant to be paired with a Telegram bot (Python) that calls
 * the HTTP endpoints here to get chart images to relay to users or in
 * response to TradingView webhook alerts.
 *
 * Environment Variables
 * ---------------------
 * PORT              - HTTP listen port (default 10000)
 * TV_DEFAULT_EX     - Default exchange code (default 'FX')
 * TV_DEFAULT_TF     - Default timeframe/interval (default '1')
 * TV_DEFAULT_THEME  - Default chart theme: dark|light (default 'dark')
 * TV_CHART_BASE     - Base path after tradingview.com/ (default 'chart')
 * TV_EXTRA_WAIT_MS  - Extra wait after navigation (default 5000)
 * TV_NAV_TIMEOUT_MS - Navigation timeout ms (default 90000)
 * PUPPETEER_EXEC_PATH - Path to Chrome/Chromium (optional; auto-detect otherwise)
 * TV_ENABLE_WATERMARK - '1' to draw watermark text on output screenshot
 * TV_WATERMARK_TEXT  - Text to draw if watermark enabled (default 'TradingView Snapshot')
 *
 * Example URL Calls
 * -----------------
 * http://localhost:10000/start-browser
 * http://localhost:10000/run?exchange=FX&ticker=EURUSD&interval=1&theme=dark
 * http://localhost:10000/snapshot/EURUSD
 * http://localhost:10000/snapshot?pair=GBPUSD&tf=5&theme=light
 * http://localhost:10000/snapshotlist?pairs=EURUSD,GBPUSD,USDJPY&tf=15
 *
 * NOTE: TradingView requires network access. If behind a firewall or offline,
 * navigation will time out. Increase timeout or ensure connectivity.
 *
 * ---------------------------------------------------------------------------
 * Author: ChatGPT (with user collaboration)
 * License: MIT-like (adapt as needed)
 * ---------------------------------------------------------------------------
 */

/* ------------------------------------------------------------------------- *
 * Imports
 * ------------------------------------------------------------------------- */
'use strict';

const path       = require('path');
const fs         = require('fs');
const os         = require('os');
const http       = require('http');
const zlib       = require('zlib');
const crypto     = require('crypto');
const express    = require('express');
const bodyParser = require('body-parser');
const FormData   = require('form-data');

// Puppeteer
const puppeteer  = require('puppeteer');

// Optional watermark (node-canvas)
let CanvasPkg = null;
try {
  CanvasPkg = require('canvas'); // { createCanvas, loadImage }
} catch (err) {
  // not fatal; we just won't watermark
  CanvasPkg = null;
}

/* ------------------------------------------------------------------------- *
 * Config via Env
 * ------------------------------------------------------------------------- */
const PORT              = parseInt(process.env.PORT, 10)              || 10000;
const TV_DEFAULT_EX     = (process.env.TV_DEFAULT_EX     || 'FX').toUpperCase();
const TV_DEFAULT_TF     =  process.env.TV_DEFAULT_TF     || '1';
const TV_DEFAULT_THEME  = (process.env.TV_DEFAULT_THEME  || 'dark').toLowerCase();
const TV_CHART_BASE     =  process.env.TV_CHART_BASE     || 'chart'; // raw path segment
const TV_EXTRA_WAIT_MS  = parseInt(process.env.TV_EXTRA_WAIT_MS, 10)  || 5000;
const TV_NAV_TIMEOUT_MS = parseInt(process.env.TV_NAV_TIMEOUT_MS, 10) || 90000;
const PUPPETEER_EXEC_PATH = process.env.PUPPETEER_EXEC_PATH || null;

const ENABLE_WATERMARK  = process.env.TV_ENABLE_WATERMARK === '1';
const WATERMARK_TEXT    = process.env.TV_WATERMARK_TEXT  || 'TradingView Snapshot';

/* ------------------------------------------------------------------------- *
 * Logging Helpers
 * ------------------------------------------------------------------------- */
function ts() {
  return new Date().toISOString();
}

function log(...args) {
  console.log(`[${ts()}]`, ...args);
}

function logErr(...args) {
  console.error(`[${ts()}]`, ...args);
}

/* ------------------------------------------------------------------------- *
 * Metrics
 * ------------------------------------------------------------------------- */
const metrics = {
  snapshotsRequested: 0,
  snapshotsSucceeded: 0,
  snapshotsFailed: 0,
  browserLaunches: 0,
  browserCrashes: 0,
  lastBrowserLaunchTS: null,
  activeCaptures: 0,
};

/* ------------------------------------------------------------------------- *
 * Concurrency Control
 * ------------------------------------------------------------------------- */
const MAX_CONCURRENT_CAPTURES = parseInt(process.env.TV_MAX_CONCURRENT_CAPTURES, 10) || 2;
let activeCaptures = 0;
const captureQueue = [];

/**
 * Acquire concurrency slot; returns a promise that resolves when slot acquired.
 */
function acquireCaptureSlot() {
  return new Promise(resolve => {
    if (activeCaptures < MAX_CONCURRENT_CAPTURES) {
      activeCaptures++;
      metrics.activeCaptures = activeCaptures;
      resolve();
    } else {
      captureQueue.push(resolve);
    }
  });
}

/**
 * Release concurrency slot and service queue.
 */
function releaseCaptureSlot() {
  activeCaptures--;
  metrics.activeCaptures = activeCaptures;
  if (captureQueue.length > 0 && activeCaptures < MAX_CONCURRENT_CAPTURES) {
    activeCaptures++;
    metrics.activeCaptures = activeCaptures;
    const nextResolve = captureQueue.shift();
    nextResolve();
  }
}

/* ------------------------------------------------------------------------- *
 * Browser Manager
 * ------------------------------------------------------------------------- */
let browser = null;
let browserReady = false;
let browserLaunching = false;

const CHROME_ARGS = [
  '--no-sandbox',
  '--disable-setuid-sandbox',
  '--disable-dev-shm-usage',
  '--disable-accelerated-2d-canvas',
  '--disable-gpu',
  '--no-zygote',
  '--single-process',             // you can remove if unstable
  '--window-size=1920,1080',
];

// fallback if I need a user agent
const TV_USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ' +
                      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

/**
 * Launch Puppeteer if not up.
 */
async function launchBrowserIfNeeded() {
  if (browser && browserReady) {
    return browser;
  }
  if (browserLaunching) {
    // Wait until current launch finishes
    while (browserLaunching) {
      await new Promise(r => setTimeout(r, 250));
    }
    return browser;
  }

  browserLaunching = true;
  metrics.browserLaunches++;
  metrics.lastBrowserLaunchTS = Date.now();

  log('Launching Puppeteer Chromium...');

  try {
    browser = await puppeteer.launch({
      headless: true,
      args: CHROME_ARGS,
      executablePath: PUPPETEER_EXEC_PATH || undefined, // autopicked if undefined
    });

    browser.on('disconnected', () => {
      logErr('⚠ Puppeteer browser disconnected (crash?).');
      metrics.browserCrashes++;
      browserReady = false;
      browser = null;
    });

    browserReady = true;
    log('✅ Puppeteer launched successfully.');
    return browser;
  } catch (err) {
    logErr('❌ Puppeteer launch failed:', err);
    browserLaunching = false;
    browserReady = false;
    browser = null;
    throw err;
  } finally {
    browserLaunching = false;
  }
}

/**
 * Close the browser gracefully.
 */
async function closeBrowser() {
  if (!browser) return;
  try {
    await browser.close();
    log('✅ Browser closed.');
  } catch (err) {
    logErr('Error closing browser:', err);
  } finally {
    browser = null;
    browserReady = false;
  }
}

/**
 * Build a TradingView URL.
 * basePath: e.g. "chart" or "chart/?"
 * Use basePath param from user; we sanitize.
 */
function buildTradingViewUrl({
  basePath = TV_CHART_BASE,
  exchange = TV_DEFAULT_EX,
  ticker = 'EURUSD',
  interval = TV_DEFAULT_TF,  // *may* not always work, but included
  theme = TV_DEFAULT_THEME,
}) {
  // ensure base path formatting
  // accepted values: 'chart' -> 'chart/?', 'chart/' -> 'chart/?', 'chart/?' -> keep
  let base = String(basePath || '').replace(/^\/+|\/+$/g, ''); // trim slashes
  if (!base) base = 'chart';

  // Guarantee we end with '?'
  if (!base.includes('?')) {
    base = `${base}/?`;
  }

  const exTicker = encodeURIComponent(`${exchange}:${ticker}`);
  const tf = encodeURIComponent(interval);
  const th = encodeURIComponent(theme);

  // NOTE: theme param is not always honored; some advanced embed mode needed. Still pass.
  return `https://www.tradingview.com/${base}symbol=${exTicker}&interval=${tf}&theme=${th}`;
}

/* ------------------------------------------------------------------------- *
 * Symbol Normalization & Exchange Fallbacks
 * ------------------------------------------------------------------------- */

// Exchanges to try if primary fails.
const EXCHANGE_FALLBACKS = (process.env.TV_EXCHANGE_FALLBACKS || 'FX_IDC,OANDA,FOREXCOM,FXCM,IDC').split(',').map(s => s.trim().toUpperCase()).filter(Boolean);

/**
 * Normalize theme.
 */
function normTheme(t) {
  if (!t) return TV_DEFAULT_THEME;
  return /^l/i.test(t) ? 'light' : 'dark';
}

/**
 * Normalize interval/timeframe string.
 * Accept "1", "5", "15", "1m", "5m", "1h", "D", "W", "M", etc.
 */
function normInterval(tfRaw) {
  if (!tfRaw) return TV_DEFAULT_TF;
  const t = String(tfRaw).trim().toLowerCase();
  if (t === 'd' || t === '1d' || t === 'day') return 'D';
  if (t === 'w' || t === '1w' || t === 'week') return 'W';
  if (t === 'm' || t === '1m' || t === 'mo' || t === 'month') return 'M';
  if (t.endsWith('m') && /^\d+m$/.test(t)) return t.slice(0, -1); // '5m' => '5'
  if (t.endsWith('h') && /^\d+h$/.test(t)) return String(parseInt(t) * 60); // '1h' => '60'
  if (/^\d+$/.test(t)) return t; // numeric
  return TV_DEFAULT_TF;
}

/**
 * Strip OTC markers, slashes, spaces: "EUR/USD-OTC" -> "EURUSD"
 */
function stripPairCore(raw) {
  return raw.replace(/[^A-Za-z0-9]/g, '').replace(/OTC$/i, '');
}

/**
 * Determine if OTC.
 */
function isOtcPair(raw) {
  return /-OTC$/i.test(raw);
}

/**
 * Parse a pair string into {exchange, ticker, isOtc, altExchanges[]}
 * Accepts:
 *  "FX:EURUSD"
 *  "EUR/USD"
 *  "eurusd"
 *  "EUR/USD-OTC"
 *  "CURRENCY:GBPUSD"
 */
function resolveSymbol(raw) {
  if (!raw) {
    return {
      exchange: TV_DEFAULT_EX,
      ticker: 'EURUSD',
      isOtc: false,
      alts: EXCHANGE_FALLBACKS,
    };
  }
  let s = String(raw).trim();
  let ex = null;
  let tk = null;
  let isOtc = isOtcPair(s);

  if (s.includes(':')) {
    const [lhs, rhs] = s.split(':', 2);
    ex = lhs.trim().toUpperCase();
    tk = stripPairCore(rhs.trim().toUpperCase());
  } else {
    tk = stripPairCore(s.toUpperCase());
  }

  if (!ex) ex = TV_DEFAULT_EX;

  // If OTC we *could* change exchange priority, but let's just fallback
  const altList = [...EXCHANGE_FALLBACKS];
  if (!altList.includes('QUOTEX')) altList.push('QUOTEX');
  if (!altList.includes('CURRENCY')) altList.push('CURRENCY');

  return {
    exchange: ex,
    ticker: tk,
    isOtc,
    alts: altList,
  };
}

/* ------------------------------------------------------------------------- *
 * Watermark Overlay (Optional)
 * ------------------------------------------------------------------------- */
async function maybeWatermarkPng(buffer, text = WATERMARK_TEXT) {
  if (!ENABLE_WATERMARK || !CanvasPkg) return buffer;
  try {
    const img = await CanvasPkg.loadImage(buffer);
    const canvas = CanvasPkg.createCanvas(img.width, img.height);
    const ctx = canvas.getContext('2d');
    ctx.drawImage(img, 0, 0);

    ctx.font = `${Math.floor(img.width / 30)}px sans-serif`;
    ctx.fillStyle = 'rgba(255,255,255,0.85)';
    ctx.strokeStyle = 'rgba(0,0,0,0.85)';
    ctx.lineWidth = 2;
    const pad = Math.floor(img.width * 0.01);
    const x = pad;
    const y = img.height - pad;
    ctx.strokeText(text, x, y);
    ctx.fillText(text, x, y);

    return canvas.toBuffer('image/png');
  } catch (err) {
    logErr('Watermark error:', err);
    return buffer;
  }
}

/* ------------------------------------------------------------------------- *
 * Core Capture Function
 * ------------------------------------------------------------------------- */
/**
 * Capture a TradingView chart screenshot.
 * 
 * @param {Object} opts
 * @param {string} opts.exchange
 * @param {string} opts.ticker
 * @param {string} opts.interval
 * @param {string} opts.theme
 * @param {string} opts.basePath
 * @param {number} opts.width
 * @param {number} opts.height
 * @param {number} opts.delayMs
 * @returns {Buffer} PNG Buffer
 */
async function captureTradingViewChart(opts = {}) {
  const {
    exchange = TV_DEFAULT_EX,
    ticker = 'EURUSD',
    interval = TV_DEFAULT_TF,
    theme = TV_DEFAULT_THEME,
    basePath = TV_CHART_BASE,
    width = 1920,
    height = 1080,
    delayMs = TV_EXTRA_WAIT_MS,
  } = opts;

  const url = buildTradingViewUrl({ basePath, exchange, ticker, interval, theme });
  log('Opening TradingView URL:', url);

  const br = await launchBrowserIfNeeded();
  const page = await br.newPage();

  // Some sites break if user agent not "normal".
  await page.setUserAgent(TV_USER_AGENT);
  await page.setViewport({ width, height });

  try {
    // Increase nav timeout (TV_NAV_TIMEOUT_MS)
    await page.goto(url, { waitUntil: 'networkidle2', timeout: TV_NAV_TIMEOUT_MS });
  } catch (err) {
    // We'll still attempt screenshot; but likely worthless
    logErr('Navigation error:', err);
  }

  // Let chart fully render
  if (delayMs > 0) {
    await page.waitForTimeout(delayMs);
  }

  // TODO: If we wanted to crop chart only we could query a DOM element.
  // For reliability we grab full viewport.
  let screenshot = await page.screenshot({ type: 'png', fullPage: false });

  await page.close();

  // watermark if enabled
  screenshot = await maybeWatermarkPng(screenshot, WATERMARK_TEXT);

  return screenshot;
}

/* ------------------------------------------------------------------------- *
 * Capture w/ Exchange Fallback
 * ------------------------------------------------------------------------- */
async function captureWithFallback({
  exchange,
  ticker,
  interval,
  theme,
  basePath = TV_CHART_BASE,
  width = 1920,
  height = 1080,
  delayMs = TV_EXTRA_WAIT_MS,
  altExchanges = [],
}) {
  const tried = [];
  let lastErr = null;

  const allEx = [exchange, ...altExchanges];
  const dedup = [];
  const seen = new Set();
  for (const ex of allEx) {
    const exUp = ex.toUpperCase();
    if (!seen.has(exUp)) {
      dedup.push(exUp);
      seen.add(exUp);
    }
  }

  for (const ex of dedup) {
    tried.push(ex);
    try {
      const buf = await captureTradingViewChart({
        exchange: ex,
        ticker,
        interval,
        theme,
        basePath,
        width,
        height,
        delayMs,
      });
      metrics.snapshotsSucceeded++;
      return { png: buf, exchangeUsed: ex };
    } catch (err) {
      lastErr = err;
      metrics.snapshotsFailed++;
      logErr(`Snapshot failed ${ex}:${ticker} ->`, err?.message || err);
    }
  }

  throw new Error(`All exchanges failed for ${ticker}. Last error: ${lastErr?.message || lastErr}. Tried: ${JSON.stringify(tried)}`);
}

/* ------------------------------------------------------------------------- *
 * Express App Setup
 * ------------------------------------------------------------------------- */
const app = express();
app.use(bodyParser.json({ limit: '2mb' }));
app.use(bodyParser.urlencoded({ extended: true }));

/* ------------------------------------------------------------------------- *
 * Middleware: Request ID
 * ------------------------------------------------------------------------- */
app.use((req, res, next) => {
  req._rid = crypto.randomBytes(4).toString('hex');
  log(`→ [${req._rid}] ${req.method} ${req.url}`);
  res.on('finish', () => {
    log(`← [${req._rid}] ${res.statusCode} ${req.method} ${req.url}`);
  });
  next();
});

/* ------------------------------------------------------------------------- *
 * /healthz
 * ------------------------------------------------------------------------- */
app.get('/healthz', (req, res) => {
  res.json({
    ok: true,
    browserReady,
    browserLaunching,
    activeCaptures,
    metrics,
  });
});

/* ------------------------------------------------------------------------- *
 * /metrics (plain text)
 * ------------------------------------------------------------------------- */
app.get('/metrics', (req, res) => {
  res.type('text/plain').send(
    [
      `snapshots_requested ${metrics.snapshotsRequested}`,
      `snapshots_succeeded ${metrics.snapshotsSucceeded}`,
      `snapshots_failed ${metrics.snapshotsFailed}`,
      `browser_launches ${metrics.browserLaunches}`,
      `browser_crashes ${metrics.browserCrashes}`,
      `active_captures ${metrics.activeCaptures}`,
    ].join('\n')
  );
});

/* ------------------------------------------------------------------------- *
 * /version
 * ------------------------------------------------------------------------- */
app.get('/version', (req, res) => {
  res.json({
    name: 'tradingview-snapshot-bot-server',
    version: '1.0.0',
    node: process.version,
    puppeteer: require('puppeteer/package.json').version,
  });
});

/* ------------------------------------------------------------------------- *
 * /start-browser (force warm-up)
 * ------------------------------------------------------------------------- */
app.get('/start-browser', async (req, res) => {
  try {
    await launchBrowserIfNeeded();
    res.send('✅ Browser started (or already running).');
  } catch (err) {
    logErr(err);
    res.status(500).send('Failed to start browser: ' + (err?.message || err));
  }
});

/* ------------------------------------------------------------------------- *
 * /close-browser
 * ------------------------------------------------------------------------- */
app.get('/close-browser', async (req, res) => {
  try {
    await closeBrowser();
    res.send('✅ Browser closed.');
  } catch (err) {
    logErr(err);
    res.status(500).send('Failed to close browser: ' + (err?.message || err));
  }
});

/* ------------------------------------------------------------------------- *
 * Core snapshot handler used by /run, /snapshot, etc.
 * ------------------------------------------------------------------------- */
async function handleSnapshotRequest(req, res, conf) {
  metrics.snapshotsRequested++;

  await acquireCaptureSlot();
  try {
    const result = await captureWithFallback(conf);
    res.set('Content-Type', 'image/png');
    res.send(result.png);
  } catch (err) {
    res.status(500).send(err?.message || String(err));
  } finally {
    releaseCaptureSlot();
  }
}

/* ------------------------------------------------------------------------- *
 * /run  (legacy backward-compatible endpoint)
 * Query Params:
 *   base=?    (chart default)
 *   exchange=
 *   ticker=
 *   interval=
 *   theme=
 *   delay   (ms)
 *   w, h
 * ------------------------------------------------------------------------- */
app.get('/run', async (req, res) => {
  const basePath = req.query.base || TV_CHART_BASE;
  const exchange = (req.query.exchange || TV_DEFAULT_EX).toUpperCase();
  const ticker   = (req.query.ticker   || 'EURUSD').toUpperCase();
  const tfRaw    = req.query.interval  || TV_DEFAULT_TF;
  const themeRaw = req.query.theme     || TV_DEFAULT_THEME;
  const delay    = parseInt(req.query.delay, 10);
  const width    = parseInt(req.query.w, 10) || 1920;
  const height   = parseInt(req.query.h, 10) || 1080;

  const interval = normInterval(tfRaw);
  const theme    = normTheme(themeRaw);
  const delayMs  = isNaN(delay) ? TV_EXTRA_WAIT_MS : delay;

  const conf = {
    exchange,
    ticker,
    interval,
    theme,
    basePath,
    width,
    height,
    delayMs,
    altExchanges: EXCHANGE_FALLBACKS,
  };

  await handleSnapshotRequest(req, res, conf);
});

/* ------------------------------------------------------------------------- *
 * /snapshot/:pair   (nice shortcut)
 *   optional query: ex, tf, theme, delay, w, h, base
 * ------------------------------------------------------------------------- */
app.get('/snapshot/:pair', async (req, res) => {
  const rawPair = req.params.pair || 'EURUSD';
  const { exchange, ticker, alts } = resolveSymbol(rawPair);
  const exQ   = req.query.ex || exchange;
  const tfRaw = req.query.tf || req.query.interval || TV_DEFAULT_TF;
  const thRaw = req.query.theme || TV_DEFAULT_THEME;
  const base  = req.query.base || TV_CHART_BASE;
  const delay = parseInt(req.query.delay, 10);
  const width = parseInt(req.query.w, 10) || 1920;
  const height= parseInt(req.query.h, 10) || 1080;

  const conf = {
    exchange: exQ.toUpperCase(),
    ticker,
    interval: normInterval(tfRaw),
    theme: normTheme(thRaw),
    basePath: base,
    width,
    height,
    delayMs: isNaN(delay) ? TV_EXTRA_WAIT_MS : delay,
    altExchanges: alts,
  };

  await handleSnapshotRequest(req, res, conf);
});

/* ------------------------------------------------------------------------- *
 * /snapshot (query form; good for TradingView webhook fallback)
 *   pair=   OR symbol=
 *   ex=     (exchange)
 *   tf=     (interval)
 *   theme=
 *   delay=
 *   w=, h=
 *   base=
 * ------------------------------------------------------------------------- */
app.get('/snapshot', async (req, res) => {
  const rawPair = req.query.pair || req.query.symbol || req.query.ticker || 'EURUSD';
  const { exchange, ticker, alts } = resolveSymbol(rawPair);
  const exQ   = req.query.ex || exchange;
  const tfRaw = req.query.tf || req.query.interval || TV_DEFAULT_TF;
  const thRaw = req.query.theme || TV_DEFAULT_THEME;
  const base  = req.query.base || TV_CHART_BASE;
  const delay = parseInt(req.query.delay, 10);
  const width = parseInt(req.query.w, 10) || 1920;
  const height= parseInt(req.query.h, 10) || 1080;

  const conf = {
    exchange: exQ.toUpperCase(),
    ticker,
    interval: normInterval(tfRaw),
    theme: normTheme(thRaw),
    basePath: base,
    width,
    height,
    delayMs: isNaN(delay) ? TV_EXTRA_WAIT_MS : delay,
    altExchanges: alts,
  };

  await handleSnapshotRequest(req, res, conf);
});

/* ------------------------------------------------------------------------- *
 * /snapshotlist
 * Query:
 *   pairs=EURUSD,GBPUSD,USDJPY
 *   ex=FX
 *   tf=15
 *   theme=light
 *   mode=json|zip  (default json)
 * NOTE: This returns multiple images aggregated. Large memory usage!
 * Intended for small lists (<10).
 * ------------------------------------------------------------------------- */
app.get('/snapshotlist', async (req, res) => {
  const rawPairs = req.query.pairs || '';
  if (!rawPairs.trim()) {
    res.status(400).json({ ok:false, error:'pairs query required' });
    return;
  }

  const mode  = (req.query.mode || 'json').toLowerCase();
  const tfRaw = req.query.tf || req.query.interval || TV_DEFAULT_TF;
  const thRaw = req.query.theme || TV_DEFAULT_THEME;
  const base  = req.query.base || TV_CHART_BASE;
  const delay = parseInt(req.query.delay, 10);
  const width = parseInt(req.query.w, 10) || 1920;
  const height= parseInt(req.query.h, 10) || 1080;

  const interval = normInterval(tfRaw);
  const theme    = normTheme(thRaw);
  const delayMs  = isNaN(delay) ? TV_EXTRA_WAIT_MS : delay;

  const pairList = rawPairs.split(',').map(s => s.trim()).filter(Boolean);
  if (pairList.length === 0) {
    res.status(400).json({ ok:false, error:'no valid pairs' });
    return;
  }

  // We'll capture sequentially to avoid exhausting memory CPU
  const results = [];
  for (const p of pairList) {
    const { exchange, ticker, alts } = resolveSymbol(p);
    try {
      await acquireCaptureSlot();
      const r = await captureWithFallback({
        exchange,
        ticker,
        interval,
        theme,
        basePath: base,
        width,
        height,
        delayMs,
        altExchanges: alts,
      });
      results.push({
        pair: p,
        exchangeUsed: r.exchangeUsed,
        ok: true,
        png: r.png, // keep raw; handle below
      });
    } catch (err) {
      results.push({
        pair: p,
        ok: false,
        error: err?.message || String(err),
      });
    } finally {
      releaseCaptureSlot();
    }
  }

  if (mode === 'json') {
    // base64 encode
    const out = results.map(r => {
      if (!r.ok) return { pair:r.pair, ok:false, error:r.error };
      return {
        pair:r.pair,
        ok:true,
        exchangeUsed:r.exchangeUsed,
        png_b64:r.png.toString('base64'),
      };
    });
    res.json({ ok:true, results:out });
    return;
  }

  // ZIP mode
  // naive zip by-hand or via npm archiver? We'll do minimal Node zlib + tar-like buffer
  // Simpler: produce a .zip using JSZip inline (no extra install?). To avoid adding new deps,
  // we build a minimal "store" zip. For brevity we use a very basic no-compression zip builder.
  try {
    const zipBuf = buildZipFromResults(results);
    res.set('Content-Type','application/zip');
    res.set('Content-Disposition','attachment; filename="snapshotlist.zip"');
    res.send(zipBuf);
  } catch (err) {
    logErr('zip build error:', err);
    res.status(500).json({ ok:false, error:'zip build failed' });
  }
});

/* ------------------------------------------------------------------------- *
 * Minimal ZIP builder (STORE only, no compression)
 * ------------------------------------------------------------------------- */
function buildZipFromResults(results) {
  // We'll produce entries for each ok result.
  let fileOffset = 0;
  const fileRecords = [];
  const centralRecords = [];
  let totalDataLen = 0;

  const encoder = new TextEncoder();

  for (const r of results) {
    if (!r.ok) continue;
    const name = `${r.pair.replace(/[^A-Za-z0-9_\-]+/g,'_')}.png`;
    const nameBuf = encoder.encode(name);
    const dataBuf = r.png;
    const localHeader = Buffer.alloc(30);
    // local file header signature
    localHeader.writeUInt32LE(0x04034b50, 0);        // sig
    localHeader.writeUInt16LE(20, 4);                // ver needed
    localHeader.writeUInt16LE(0, 6);                 // gp bit
    localHeader.writeUInt16LE(0, 8);                 // compression 0=store
    localHeader.writeUInt16LE(0, 10);                // mod time
    localHeader.writeUInt16LE(0, 12);                // mod date
    const crc = crc32(dataBuf);
    localHeader.writeUInt32LE(crc, 14);              // crc
    localHeader.writeUInt32LE(dataBuf.length, 18);   // comp size
    localHeader.writeUInt32LE(dataBuf.length, 22);   // uncomp size
    localHeader.writeUInt16LE(nameBuf.length, 26);   // fname len
    localHeader.writeUInt16LE(0, 28);                // extra len

    const localRecord = Buffer.concat([localHeader, nameBuf, dataBuf]);

    fileRecords.push(localRecord);

    // central dir
    const centralHeader = Buffer.alloc(46);
    centralHeader.writeUInt32LE(0x02014b50, 0);      // sig
    centralHeader.writeUInt16LE(20, 4);              // ver made
    centralHeader.writeUInt16LE(20, 6);              // ver needed
    centralHeader.writeUInt16LE(0, 8);               // gp bit
    centralHeader.writeUInt16LE(0, 10);              // comp=store
    centralHeader.writeUInt16LE(0, 12);              // time
    centralHeader.writeUInt16LE(0, 14);              // date
    centralHeader.writeUInt32LE(crc, 16);            // crc
    centralHeader.writeUInt32LE(dataBuf.length, 20); // comp size
    centralHeader.writeUInt32LE(dataBuf.length, 24); // uncomp size
    centralHeader.writeUInt16LE(nameBuf.length, 28); // name len
    centralHeader.writeUInt16LE(0, 30);              // extra
    centralHeader.writeUInt16LE(0, 32);              // comment
    centralHeader.writeUInt16LE(0, 34);              // disk number
    centralHeader.writeUInt16LE(0, 36);              // internal attr
    centralHeader.writeUInt32LE(0, 38);              // external attr
    centralHeader.writeUInt32LE(fileOffset, 42);     // local header offset

    centralRecords.push(Buffer.concat([centralHeader, nameBuf]));

    // advance offset
    fileOffset += localRecord.length;
    totalDataLen += localRecord.length;
  }

  // central dir
  const centralBuf = Buffer.concat(centralRecords);
  const centralSize = centralBuf.length;
  const centralOffset = totalDataLen;

  // end of central dir (EOCD)
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);              // sig
  end.writeUInt16LE(0, 4);                       // disk
  end.writeUInt16LE(0, 6);                       // disk start
  end.writeUInt16LE(centralRecords.length, 8);   // records on this disk
  end.writeUInt16LE(centralRecords.length, 10);  // total records
  end.writeUInt32LE(centralSize, 12);            // size of central
  end.writeUInt32LE(centralOffset, 16);          // offset of central
  end.writeUInt16LE(0, 20);                      // comment len

  return Buffer.concat([...fileRecords, centralBuf, end]);
}

/* ------------------------------------------------------------------------- *
 * CRC32 utility
 * ------------------------------------------------------------------------- */
const CRC_TABLE = (() => {
  const tbl = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) {
      c = ((c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1)) >>> 0;
    }
    tbl[i] = c >>> 0;
  }
  return tbl;
})();

function crc32(buf) {
  let crc = ~0;
  for (let i = 0; i < buf.length; i++) {
    crc = CRC_TABLE[(crc ^ buf[i]) & 0xFF] ^ (crc >>> 8);
  }
  return (~crc) >>> 0;
}

/* ------------------------------------------------------------------------- *
 * Root route
 * ------------------------------------------------------------------------- */
app.get('/', (req, res) => {
  res.send(
    `<h1>TradingView Snapshot Service</h1>
     <p>Use <code>/run</code>, <code>/snapshot/:pair</code>, <code>/snapshot</code>,
     <code>/snapshotlist</code>, <code>/start-browser</code>.</p>`
  );
});

/* ------------------------------------------------------------------------- *
 * Graceful Shutdown
 * ------------------------------------------------------------------------- */
function setupSignalHandlers() {
  ['SIGINT', 'SIGTERM'].forEach(sig => {
    process.on(sig, async () => {
      log(`\n${sig} received. Closing browser & exiting...`);
      try {
        await closeBrowser();
      } finally {
        process.exit(0);
      }
    });
  });
}

/* ------------------------------------------------------------------------- *
 * Start Server
 * ------------------------------------------------------------------------- */
setupSignalHandlers();

app.listen(PORT, () => {
  log(`✅ Snapshot service listening on port ${PORT}`);
  // optional prelaunch
  launchBrowserIfNeeded().catch(err => {
    logErr('Initial browser launch failed (continuing, will retry on demand):', err);
  });
});
