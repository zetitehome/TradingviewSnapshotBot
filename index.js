const express = require('express');
const puppeteer = require('puppeteer');
const app = express();
const PORT = process.env.PORT || 3000;

let browser, page;

const chromeOptions = {
    headless: true,
    args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-accelerated-2d-canvas',
        '--disable-gpu',
        '--no-zygote',
        '--single-process',
        '--disable-dev-shm-usage',
        '--window-size=1920x1080'
    ],
};

app.get('/start-browser', async function (req, res) {
    try {
        browser = await puppeteer.launch(chromeOptions);
        page = await browser.newPage();

        await page.setUserAgent("Mozilla/5.0 (X11; Linux x86_64)");
        await page.setViewport({ width: 1920, height: 1080 });

        res.send("✅ Browser started");
    } catch (err) {
        console.error(err);
        res.status(500).send("Failed to start browser: " + err.message);
    }
});

app.get('/run', async function (req, res) {
    const { base = "chart", exchange = "FX", ticker = "EURUSD", interval = "1", theme = "dark" } = req.query;
    const url = `https://www.tradingview.com/${base}?symbol=${exchange}:${ticker}&interval=${interval}&theme=${theme}`;

    try {
        if (!browser || !page) {
            return res.status(500).send("Browser not started. Use /start-browser first.");
        }

        await page.goto(url, { waitUntil: 'networkidle2', timeout: 30000 });
        await page.waitForTimeout(5000);

        const screenshot = await page.screenshot({ type: 'png' });
        res.set('Content-Type', 'image/png');
        res.send(screenshot);
    } catch (err) {
        console.error(err);
        res.status(500).send("Error taking screenshot: " + err.message);
    }
});

app.listen(PORT, () => {
    console.log(`✅ App listening on port ${PORT}`);
});
