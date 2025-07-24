const express = require('express');
const fs = require('fs');
const bodyParser = require('body-parser');
const cors = require('cors');
const app = express();
const port = 3000;

const logsFile = './logs.json';
if (!fs.existsSync(logsFile)) fs.writeFileSync(logsFile, '[]');

app.use(cors());
app.use(bodyParser.json());
app.use(express.static('public'));

function logTrade(entry) {
  const logs = JSON.parse(fs.readFileSync(logsFile));
  logs.unshift(`[${new Date().toISOString()}] ${entry}`);
  fs.writeFileSync(logsFile, JSON.stringify(logs.slice(0, 100), null, 2));
}

app.get('/logs', (req, res) => {
  const logs = JSON.parse(fs.readFileSync(logsFile));
  res.json(logs);
});

app.post('/analyze', (req, res) => {
  const { pair, expiry } = req.body;
  logTrade(`Manual analysis requested for ${pair} with ${expiry} min expiry.`);
  res.sendStatus(200);
});

app.post('/trade', (req, res) => {
  const { pair, expiry, amount } = req.body;
  logTrade(`Manual trade initiated: ${pair} | Expiry: ${expiry}min | Amount: ${amount}`);
  // Placeholder for UI.Vision webhook logic
  res.sendStatus(200);
});

app.listen(port, () => {
  console.log(`✅ Dashboard backend running at http://localhost:${port}`);
});
