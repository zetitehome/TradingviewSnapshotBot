/**
 * TradingView / IQ Option Snapshot Service
 * ----------------------------------------
 * GET /healthz                    → {ok:true}
 * GET /start-browser              → ensure global browser running
 * GET /run?...                    → capture chart PNG
 *        query:
 *           source=auto|tv|iq     (default auto)
 *           base=chart            (ignored for iq)
 *           exchange=FX           (TradingView)
 *           ticker=EURUSD
 *           interval=1            (minutes or D/W/M)
 *           theme=dark|light
 *           fullpage=false|true   (optional)
 *
 * Behavior:
 *  • source=auto:
 *      if exchange === 'IQOPTION' OR ticker contains '-OTC' → IQ flow
 *      else TradingView flow
 *  • Always returns **200** with image/png (real screenshot or error placeholder).
 *    No more 404s that break upstream callers.
 *
 *  • Gracefully auto‑relaunches browser on crash.
 *
 *  • Uses 1 global page (serial) to limit memory in Render free plans.
 *
 *  • Minimal CSP bypass: we disable some site security flags in Chromium args.
 */

const express = require('express');
const bodyParser = require('body-parser');
const puppeteer = require('puppeteer');
const { createCanvas } = require('canvas');
const URL = require('url').URL;

const app = express();
app.use(bodyParser.json());

const PORT = process.env.PORT || 10000;

// ─────────────────────────────────────────────────────────
// Globals
// ─────────────────────────────────────────────────────────
let browser = null;
let page = null;
let launching = null; // promise guard

const PUPPETEER_LAUNCH_OPTS = {
  headless: true, // Change to 'new' if your Puppeteer version warns
  args: [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    '--disable-accelerated-2d-canvas',
    '--disable-gpu',
    '--no-zygote',
    '--window-size=1920,1080',
  ],
  // executablePath: process.env.CHROME_PATH || undefined, // optional override
};

// Basic UA (helps some brokers load)
const USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36';

// ─────────────────────────────────────────────────────────
// Utilities
// ─────────────────────────────────────────────────────────
function msleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function buildTradingViewURL({ base = 'chart', exchange = 'FX', ticker = 'EURUSD', interval = '1', theme = 'dark' }) {
  // Accept short intervals: numeric -> minutes; D/W/M pass through
  // TV supports: 1,3,5,...; D,W,M
  const encSym = encodeURIComponent(`${exchange}:${ticker}`);
  const encInt = encodeURIComponent(interval);
  const encTheme = encodeURIComponent(theme);
  const basePath = base.includes('?') ? base : `${base}/?`;
  return `https://www.tradingview.com/${basePath}symbol=${encSym}&interval=${encInt}&theme=${encTheme}`;
}

function buildIQOptionURL(ticker = 'EURUSD') {
  // IQ Option doesn't have clean direct chart symbol deep links that
  // are stable across sessions; we land in the traderoom and (optionally)
  // try to focus the search field. We pass the ticker via hash to help
  // detection once page loads.
  const tk = encodeURIComponent(ticker);
  return `https://eu.iqoption.com/traderoom#asset=${tk}`;
}

// Create a small placeholder PNG when capture fails
function buildErrorPng(text) {
  const w = 800;
  const h = 450;
  const canvas = createCanvas(w, h);
  const ctx = canvas.getContext('2d');

  ctx.fillStyle = '#1e1e1e';
  ctx.fillRect(0, 0, w, h);

  ctx.fillStyle = '#ff5555';
  ctx.font = 'bold 32px sans-serif';
  ctx.fillText('Snapshot Error', 40, 80);

  ctx.fillStyle = '#ffffff';
  ctx.font = '20px sans-serif';

  const lines = wrapText(ctx, text, w - 80);
  let y = 130;
  for (const line of lines) {
    ctx.fillText(line, 40, y);
    y += 28;
  }
  return canvas.toBuffer('image/png');
}

function wrapText(ctx, text, maxWidth) {
  const words = text.split(/\s+/);
  const lines = [];
  let cur = '';
  for (const w of words) {
    const test = (cur ? cur + ' ' : '') + w;
    if (ctx.measureText(test).width > maxWidth) {
      if (cur) lines.push(cur);
      cur = w;
    } else {
      cur = test;
    }
  }
  if (cur) lines.push(cur);
  return lines.slice(0, 8); // cap lines
}

// ─────────────────────────────────────────────────────────
// Browser lifecycle
// ─────────────────────────────────────────────────────────
async function ensureBrowser() {
  if (browser && page) return { browser, page };
  if (launching) {
    await launching;
    return { browser, page };
  }
  launching = (async () => {
    try {
      console.log('🔄 Launching Chromium...');
      browser = await puppeteer.launch(PUPPETEER_LAUNCH_OPTS);
      page = await browser.newPage();
      await page.setUserAgent(USER_AGENT);
      await page.setViewport({ width: 1920, height: 1080 });

      // handle page errors (avoid Node crash)
      page.on('error', err => console.error('[Page error]', err));
      page.on('pageerror', err => console.error('[Page JS error]', err));
      page.on('close', () => {
        console.warn('⚠ Puppeteer page closed; resetting handle.');
        page = null;
      });

      browser.on('disconnected', () => {
        console.warn('⚠ Puppeteer browser disconnected; resetting.');
        browser = null;
        page = null;
      });

    } catch (err) {
      console.error('❌ Browser launch failed:', err);
      browser = null;
      page = null;
      throw err;
    } finally {
      launching = null;
    }
  })();
  await launching;
  return { browser, page };
}

async function recycleBrowser() {
  try {
    if (page) await page.close().catch(() => {});
    if (browser) await browser.close().catch(() => {});
  } catch (_) {}
  page = null; browser = null;
  return ensureBrowser();
}

// Graceful shutdown
['SIGINT', 'SIGTERM'].forEach(sig => {
  process.on(sig, async () => {
    console.log(`\n${sig} received. Closing browser...`);
    try {
      if (page) await page.close();
      if (browser) await browser.close();
    } catch (_) {}
    process.exit(0);
  });
});

// ─────────────────────────────────────────────────────────
// Core capture routines
// ─────────────────────────────────────────────────────────
async function captureTradingView(opts) {
  const { page } = await ensureBrowser();
  const url = buildTradingViewURL(opts);
  console.log('📸 [TV] goto', url);
  try {
    await page.goto(url, { waitUntil: 'networkidle2', timeout: 60000 });
  } catch (err) {
    console.warn('TV goto error:', err.message || err);
  }

  // Let UI stabilize a bit
  await msleep(5000);

  // Hide in‑chart popups that sometimes obscure view
  try {
    await page.evaluate(() => {
      const selectors = [
        'div[data-name="popup"]',
        'div[data-name="dialog"]',
        '.js-rootresizer__contents [role="dialog"]',
        '[data-role="close"]',
      ];
      selectors.forEach(sel => {
        document.querySelectorAll(sel).forEach(el => el.remove());
      });
    });
  } catch (_) {}

  // screenshot chart container if present
  let clipTarget = null;
  try {
    clipTarget = await page.$('.chart-container'); // legacy class
    if (!clipTarget) {
      clipTarget = await page.$('div[data-name="pane-legend"]'); // failsafe anchor
    }
  } catch (_) {}

  if (clipTarget) {
    try {
      return await clipTarget.screenshot({ type: 'png' });
    } catch (e) {
      console.warn('clip screenshot failed, fallback full page:', e);
    }
  }
  return await page.screenshot({ type: 'png' });
}


async function captureIQOption(ticker = 'EURUSD') {
  const { page } = await ensureBrowser();
  const url = buildIQOptionURL(ticker);
  console.log('📸 [IQ] goto', url);

  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  } catch (err) {
    console.warn('IQ goto error:', err.message || err);
  }

  // Wait longer; traderoom is heavy SPA
  await msleep(8000);

  // Try to focus asset search field & type ticker (best effort; silently ignore errors)
  try {
    await page.evaluate((tk) => {
      // heuristics – may not always work; site changes often
      const search = document.querySelector('input[placeholder*="Search"]')
        || document.querySelector('input[type="search"]');
      if (search) {
        search.value = '';
        const ev = new Event('input', { bubbles: true });
        search.dispatchEvent(ev);
        search.focus();
      }
    }, ticker);
    await page.keyboard.type(ticker, { delay: 50 });
    await msleep(1500);
  } catch (_) {}

  return await page.screenshot({ type: 'png' });
}


// ─────────────────────────────────────────────────────────
// Dispatcher
// ─────────────────────────────────────────────────────────
async function doSnapshot(params) {
  /**
   * params = {source, base, exchange, ticker, interval, theme, fullpage}
   * returns Buffer PNG
   */
  const {
    source = 'auto',
    base = 'chart',
    exchange = 'FX',
    ticker = 'EURUSD',
    interval = '1',
    theme = 'dark',
  } = params;

  let src = source;
  if (src === 'auto') {
    if (exchange.toUpperCase() === 'IQOPTION' || /-OTC$/i.test(ticker)) {
      src = 'iq';
    } else {
      src = 'tv';
    }
  }

  try {
    if (src === 'iq') {
      return await captureIQOption(ticker);
    }
    // default to TradingView
    return await captureTradingView({ base, exchange, ticker, interval, theme });
  } catch (err) {
    console.error(`snapshot error ${exchange}:${ticker}`, err);
    return buildErrorPng(`${exchange}:${ticker}\n${err.message || err}`);
  }
}


// ─────────────────────────────────────────────────────────
// Routes
// ─────────────────────────────────────────────────────────
app.get('/healthz', (req, res) => {
  res.json({
    ok: true,
    browser: !!browser,
    page: !!page,
    uptime_s: process.uptime(),
  });
});

app.get('/start-browser', async (req, res) => {
  try {
    await ensureBrowser();
    res.status(200).send('✅ Browser ready');
  } catch (err) {
    console.error(err);
    res
      .status(500)
      .send('❌ Failed to start browser: ' + (err.message || String(err)));
  }
});

app.get(['/run', '/screenshot'], async (req, res) => {
  const {
    source = 'auto',
    base = 'chart',
    exchange = 'FX',
    ticker = 'EURUSD',
    interval = '1',
    theme = 'dark',
  } = req.query;

  let png;
  try {
    png = await doSnapshot({
      source,
      base,
      exchange,
      ticker,
      interval,
      theme,
    });
  } catch (err) {
    console.error('doSnapshot fatal', err);
    png = buildErrorPng(`fatal: ${err.message || err}`);
  }

  res.setHeader('Content-Type', 'image/png');
  res.status(200).end(png);
});

// root info
app.get('/', (req, res) => {
  res.type('text/plain').send(
    [
      'TradingView / IQOption Snapshot Service',
      '--------------------------------------',
      'Endpoints:',
      '  GET /healthz',
      '  GET /start-browser',
      '  GET /run?exchange=FX&ticker=EURUSD&interval=1&theme=dark',
      '  GET /run?source=iq&ticker=EURUSD-OTC',
      '',
    ].join('\n')
  );
});

// ─────────────────────────────────────────────────────────
// Start
// ─────────────────────────────────────────────────────────
app.listen(PORT, () => {
  console.log(`✅ Snapshot service listening on port ${PORT}`);
  // warm launch
  ensureBrowser().catch(err => {
    console.error('Warm browser launch failed:', err);
  });
});
