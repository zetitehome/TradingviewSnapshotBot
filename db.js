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

// Insert a new trade
function insertTrade(pair, direction, time) {
  return new Promise((resolve, reject) => {
    const stmt = db.prepare("INSERT INTO trades (pair, direction, time) VALUES (?, ?, ?)");
    stmt.run(pair, direction, time, function (err) {
      if (err) return reject(err);
      resolve(this.lastID);
    });
    stmt.finalize();
  });
}

// Update the result of a trade
function updateResult(id, result) {
  return new Promise((resolve, reject) => {
    db.run(`UPDATE trades SET result = ? WHERE id = ?`, [result, id], function (err) {
      if (err) return reject(err);
      resolve(this.changes > 0);
    });
  });
}

// Get latest N trades
function getLatestTrades(limit = 10) {
  return new Promise((resolve, reject) => {
    db.all(`SELECT * FROM trades ORDER BY id DESC LIMIT ?`, [limit], (err, rows) => {
      if (err) return reject(err);
      resolve(rows);
    });
  });
}

// Get win/loss stats
function getStats() {
  return new Promise((resolve, reject) => {
    db.all(`SELECT result, COUNT(*) as count FROM trades GROUP BY result`, (err, rows) => {
      if (err) return reject(err);
      resolve(rows);
    });
  });
}

module.exports = {
  insertTrade,
  updateResult,
  getLatestTrades,
  getStats
};
