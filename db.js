const Database = require('better-sqlite3');
const db = new Database('trading-bot.db');

// Create trades table if not exists
db.prepare(`
  CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT,
    signal TEXT,
    expiry INTEGER,
    amount REAL,
    risk_type TEXT,
    timestamp INTEGER,
    status TEXT,
    result TEXT,
    screenshot TEXT
  )
`).run();

function addTrade(trade) {
  const stmt = db.prepare(`INSERT INTO trades 
    (pair, signal, expiry, amount, risk_type, timestamp, status) 
    VALUES (?, ?, ?, ?, ?, ?, ?)`);
  const info = stmt.run(
    trade.pair, trade.signal, trade.expiry, trade.amount, trade.risk_type,
    trade.timestamp, trade.status || 'pending'
  );
  return info.lastInsertRowid;
}

function updateTradeResult(id, result, screenshot) {
  const stmt = db.prepare(`UPDATE trades SET result = ?, status = 'closed', screenshot = ? WHERE id = ?`);
  stmt.run(result, screenshot, id);
}

function getAllTrades() {
  return db.prepare(`SELECT * FROM trades ORDER BY timestamp DESC`).all();
}

module.exports = {
  addTrade,
  updateTradeResult,
  getAllTrades
};
