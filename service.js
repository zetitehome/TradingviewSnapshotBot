// server.js
// Minimal TradingView snapshot server for Render / local testing.

const http = require('http');
const url = require('url');
const puppeteer = require('puppeteer');

const PORT = process.env.PORT || 10000;

// Cache a single Puppeteer browser instance across requests.
let browser = null;

// Launch (or reuse) headless Chromium
async function startBrowser() {
  if (browser && browser.process() !== null) {
    return browser;
  }
  console.log('[PUP] launching Chromium...');
  browser = await puppeteer.launch({
    headless: 'new',            // equivalent to true but modern mode
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-gpu',
      '--disable-accelerated-2d-canvas',
      '--no-zygote',
      '--single-process',
      '--window-size=1920,1080'
    ],
    // executablePath: process.env.CHROME_PATH || undefined, // uncomment if you bring your own Chrome
  });
  return browser;
}

// Build a TradingView chart URL
function buildTVUrl(exchange, ticker, interval, theme) {
  // Example: https://www.tradingview.com/chart/?symbol=FX:EURUSD&interval=1&theme=dark
  const sym = `${exchange}:${ticker}`;
  const qs = new URLSearchParams({
    symbol: sym,
    interval: interval,
    theme: theme
  });
  return `https://www.tradingview.com/chart/?${qs.toString()}`;
}

// Capture a PNG of the chart
async function captureSnapshot(exchange, ticker, interval, theme) {
  const b = await startBrowser();
  const page = await b.newPage();

  // Speed / reliability settings
  await page.setUserAgent('Mozilla/5.0 (X11; Linux x86_64)');
  await page.setViewport({ width: 1920, height: 1080 });

  const tvURL = buildTVUrl(exchange, ticker, interval, theme);
  console.log('[SNAP] goto:', tvURL);

  await page.goto(tvURL, { waitUntil: 'networkidle2', timeout: 60000 });

  // Give TradingView time to load indicators/layout
  await page.waitForTimeout(5000);

  // Capture the visible viewport (fast & smaller) — change to fullPage:true if needed
  const buffer = await page.screenshot({ type: 'png' });

  await page.close();
  return buffer;
}

// Graceful shutdown
async function closeBrowser() {
  if (browser) {
    try { await browser.close(); } catch (_) {}
    browser = null;
  }
}

// HTTP server
const server = http.createServer(async (req, res) => {
  const parsed = url.parse(req.url, true);
  const pathname = parsed.pathname;

  // --- homepage ---------------------------------------------------
  if (pathname === '/' || pathname === '/index.html') {
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end(`
      <html>
        <head><title>TradingView Snapshot Server</title></head>
        <body>
          <h1>Server is running ✅</h1>
          <p>Test a chart:</p>
          <code>/run?exchange=FX&ticker=EURUSD&interval=1&theme=dark</code>
          <p>Start browser manually:</p>
          <code>/start-browser</code>
        </body>
      </html>
    `);
    return;
  }

  // --- health endpoint --------------------------------------------
  if (pathname === '/healthz') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, browser: !!browser }));
    return;
  }

  // --- start-browser ----------------------------------------------
  if (pathname === '/start-browser') {
    try {
      await startBrowser();
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, msg: 'Browser started' }));
    } catch (err) {
      console.error('[ERR] start-browser:', err);
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: err.message }));
    }
    return;
  }

  // --- run snapshot -----------------------------------------------
  if (pathname === '/run') {
    // NOTE: we ignore `base` param — not needed in this simplified server
    const {
      exchange = 'FX',
      ticker = 'EURUSD',
      interval = '1',
      theme = 'dark'
    } = parsed.query;

    try {
      const png = await captureSnapshot(exchange, ticker, interval, theme);
      res.writeHead(200, { 'Content-Type': 'image/png' });
      res.end(png);
    } catch (err) {
      console.error('[ERR] snapshot /run:', err);
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: err.message }));
    }
    return;
  }

  // --- 404 fallback -----------------------------------------------
  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: 'Endpoint not found', path: pathname }));
});

// Handle container stop
['SIGINT', 'SIGTERM'].forEach(sig => {
  process.on(sig, async () => {
    console.log(`\n[SYS] ${sig} received. Closing browser...`);
    await closeBrowser();
    process.exit(0);
  });
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`Server running at http://localhost:${PORT}`);
});
