/**
 * TradingView Snapshot Bot Server
 * --------------------------------
 * Features:
 * - Puppeteer-powered chart snapshot service
 * - Routes for /healthz, /start-browser, /run
 * - Logging with timestamps
 * - Automatic browser restart on failure
 * - Supports FX, OTC, and other exchanges
 * - Designed for integration with Telegram bot
 *
 * Author: zetitehome (updated with improvements)
 */

const express = require('express');
const bodyParser = require('body-parser');
const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');
const { createCanvas } = require('canvas');

const PORT = process.env.PORT || 10000;
const HOST = '0.0.0.0';

const app = express();
app.use(bodyParser.json());

/** Global State */
let browser;
let isLaunching = false;
let lastBrowserLaunch = null;

/** Logger Utility */
function log(...args) {
    console.log(`[${new Date().toISOString()}]`, ...args);
}

/** Ensure Puppeteer Launch */
async function ensureBrowser() {
    if (browser) return browser;
    if (isLaunching) {
        log('Browser launch in progress, waiting...');
        while (isLaunching) {
            await new Promise(res => setTimeout(res, 500));
        }
        return browser;
    }
    isLaunching = true;
    try {
        log('Launching Puppeteer Chromium...');
        browser = await puppeteer.launch({
            headless: true,
            args: [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-gpu',
                '--disable-dev-shm-usage',
                '--disable-extensions',
                '--disable-software-rasterizer',
            ],
            defaultViewport: { width: 1280, height: 720 },
        });
        lastBrowserLaunch = Date.now();
        log('✅ Puppeteer launched successfully.');
    } catch (err) {
        log('❌ Puppeteer launch failed:', err);
        browser = null;
    } finally {
        isLaunching = false;
    }
    return browser;
}

/** Close Browser Safely */
async function closeBrowser() {
    if (browser) {
        log('Closing Puppeteer...');
        try {
            await browser.close();
        } catch (err) {
            log('Error closing browser:', err);
        }
        browser = null;
    }
}

/** Puppeteer Snapshot Function */
async function captureTradingViewChart(exchange, ticker, interval, theme, outPath) {
    const url = `https://www.tradingview.com/chart/?symbol=${exchange}:${ticker}`;
    log(`Opening TradingView URL: ${url}`);

    const b = await ensureBrowser();
    if (!b) throw new Error('Puppeteer browser not available.');

    const page = await b.newPage();
    try {
        await page.goto(url, { waitUntil: 'networkidle2', timeout: 30000 });
        await page.waitForTimeout(3000); // Wait for chart to fully render

        // Apply theme adjustments (dark/light)
        if (theme === 'dark') {
            await page.evaluate(() => {
                document.body.style.backgroundColor = '#000';
            });
        }

        // Screenshot full page
        await page.screenshot({ path: outPath });
        log(`Snapshot saved: ${outPath}`);
    } catch (err) {
        log('❌ Snapshot failed:', err);
        throw err;
    } finally {
        await page.close();
    }
}

/** Health Check Endpoint */
app.get('/healthz', (req, res) => {
    res.json({
        status: 'ok',
        browser: !!browser,
        uptime: process.uptime(),
        lastLaunch: lastBrowserLaunch ? new Date(lastBrowserLaunch).toISOString() : null,
    });
});

/** Start Browser Endpoint */
app.get('/start-browser', async (req, res) => {
    try {
        await ensureBrowser();
        res.json({ status: 'ok', message: 'Browser started' });
    } catch (err) {
        res.status(500).json({ status: 'error', error: err.message });
    }
});

/** Run Snapshot Endpoint */
app.get('/run', async (req, res) => {
    const { exchange = 'FX', ticker = 'EURUSD', interval = '1', theme = 'dark', base = 'chart' } = req.query;
    const fileName = `${exchange}_${ticker}_${interval}_${Date.now()}.png`;
    const outPath = path.join(__dirname, 'snapshots', fileName);

    try {
        if (!fs.existsSync(path.dirname(outPath))) {
            fs.mkdirSync(path.dirname(outPath), { recursive: true });
        }
        await captureTradingViewChart(exchange, ticker, interval, theme, outPath);
        res.sendFile(outPath);
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

/** Root Endpoint */
app.get('/', (req, res) => {
    res.send('<h1>TradingView Snapshot Bot Server</h1><p>Use /run?exchange=FX&ticker=EURUSD&interval=1&theme=dark</p>');
});

/** Auto Restart Browser (every 10 minutes) */
setInterval(async () => {
    if (browser && Date.now() - lastBrowserLaunch > 10 * 60 * 1000) {
        log('Restarting browser (10 min interval)...');
        await closeBrowser();
        await ensureBrowser();
    }
}, 60 * 1000);

/** Start Server */
app.listen(PORT, HOST, async () => {
    log(`✅ Snapshot service listening on port ${PORT}`);
    await ensureBrowser();
});

/* ----------------- EXTENSIONS & UTILITIES ----------------- */

/**
 * Advanced Snapshot with Canvas Processing
 * (Can overlay signals, win rates, etc.)
 */
async function advancedSnapshot(exchange, ticker, interval, theme, outPath) {
    const tempFile = outPath.replace('.png', '_raw.png');
    await captureTradingViewChart(exchange, ticker, interval, theme, tempFile);

    // Process with Canvas (e.g., overlay text)
    const image = fs.readFileSync(tempFile);
    const canvas = createCanvas(1280, 720);
    const ctx = canvas.getContext('2d');
    const img = new (require('canvas').Image)();
    img.src = image;
    ctx.drawImage(img, 0, 0);

    ctx.fillStyle = '#FFCC00';
    ctx.font = 'bold 28px Arial';
    ctx.fillText(`Symbol: ${exchange}:${ticker}`, 50, 50);
    ctx.fillText(`Interval: ${interval}m`, 50, 90);

    const finalBuffer = canvas.toBuffer('image/png');
    fs.writeFileSync(outPath, finalBuffer);
    fs.unlinkSync(tempFile);
    log(`Advanced snapshot processed: ${outPath}`);
}

/** Extended Run Route with Overlays */
app.get('/run-advanced', async (req, res) => {
    const { exchange = 'FX', ticker = 'EURUSD', interval = '1', theme = 'dark' } = req.query;
    const fileName = `${exchange}_${ticker}_${interval}_${Date.now()}_adv.png`;
    const outPath = path.join(__dirname, 'snapshots', fileName);

    try {
        if (!fs.existsSync(path.dirname(outPath))) {
            fs.mkdirSync(path.dirname(outPath), { recursive: true });
        }
        await advancedSnapshot(exchange, ticker, interval, theme, outPath);
        res.sendFile(outPath);
    } catch (err) {
        log('Error in run-advanced:', err);
        res.status(500).json({ error: err.message });
    }
});

/* --------------------------------------------------------- */

/**
 * Handle Graceful Shutdown
 */
process.on('SIGINT', async () => {
    log('Received SIGINT. Closing...');
    await closeBrowser();
    process.exit(0);
});

process.on('SIGTERM', async () => {
    log('Received SIGTERM. Closing...');
    await closeBrowser();
    process.exit(0);
});
    