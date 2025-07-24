const db = require('./db');
const fetch = require('node-fetch');

async function triggerTrade({ symbol, direction, expiry, amount }) {
  const id = db.prepare('INSERT INTO trades (symbol, direction, expiry, amount) VALUES (?, ?, ?, ?)')
    .run(symbol, direction, expiry, amount).lastInsertRowid;

  // === Replace with your webhook trigger ===
  await fetch('http://localhost:3001/trade', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol, direction, expiry, amount })
  });

  return id;
}

module.exports = triggerTrade;
const pairs = require('./pairs');