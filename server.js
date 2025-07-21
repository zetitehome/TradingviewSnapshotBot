const express = require('express');
const puppeteer = require('puppeteer');
const bodyParser = require('body-parser');

const app = express();
app.use(bodyParser.json());

app.get('/', (req, res) => {
  res.send('TradingView Snapshot Bot is running!');
});

// Example endpoint to take a snapshot
app.get('/run', async (req, res) => {
  const { exchange = 'FX', ticker = 'EURUSD', interval = '1', theme = 'dark' } = req.query;

  try {
    const browser = await puppeteer.launch({
      headless: 'new',
      args: ['--no-sandbox', '--disable-setuid-sandbox']
    });
    const page = await browser.newPage();

    const url = `https://www.tradingview.com/chart/?symbol=${exchange}:${ticker}`;
    await page.goto(url, { waitUntil: 'networkidle2' });
    await page.waitForTimeout(3000);

    const buffer = await page.screenshot({ fullPage: true });
    await browser.close();

    res.set('Content-Type', 'image/png');
    res.send(buffer);
  } catch (err) {
    console.error(err);
    res.status(500).send('Error generating snapshot');
  }
});

const PORT = process.env.PORT || 10000;
app.listen(PORT, () => {
  console.log(`Server running on http://localhost:${PORT}`);
});
