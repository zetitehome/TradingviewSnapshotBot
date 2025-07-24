const sqlite3 = require('sqlite3').verbose();
const db = new sqlite3.Database('./trades.db');

// Create table if it doesn't exist
db.serialize(() => {
  db.run(`CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT,
    direction TEXT,
    time TEXT,
    result TEXT DEFAULT 'pending'
  )`);
});

function insertTrade(pair, direction, time, cb) {
  const stmt = db.prepare("INSERT INTO trades (pair, direction, time) VALUES (?, ?, ?)");
  stmt.run(pair, direction, time, cb);
  stmt.finalize();
}

function updateResult(id, result) {
  db.run(`UPDATE trades SET result = ? WHERE id = ?`, [result, id]);
}

function getLatestTrades(limit = 10, cb) {
  db.all(`SELECT * FROM trades ORDER BY id DESC LIMIT ?`, [limit], cb);
}

function getStats(cb) {
  db.all(`SELECT result, COUNT(*) as count FROM trades GROUP BY result`, cb);
}

module.exports = { insertTrade, updateResult, getLatestTrades, getStats };
