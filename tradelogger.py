"""
tradelogger.py
==============

Persistent trade history + performance stats for the TradingView Snapshot Bot.

Features
--------
* Append trade requests & trade outcomes (win/loss/break-even).
* Track running balance, realized PnL, hit-rate, drawdown.
* Generate "Quantum Level" progressive tiers based on trade count + win rate.
* Format pretty Telegram stats blocks with emojis.
* Rolling save to JSON (atomic safe write).

Integration Flow
----------------
The bot (tvsnapshotbot.py) will:
 1. On trade signal (/trade or auto from TV webhook), call logger.record_signal(...)
 2. When actual trade is *sent* (UI.Vision requested), call logger.record_execution(...)
 3. When result arrives (manual entry or feed), call logger.record_result(...)
 4. Periodically call logger.summary() and send to Telegram (/stats)

You can also manually import the JSON into spreadsheets.

JSON Schema (root)
------------------
{
  "balance_start": 1000.0,
  "balance_current": 1033.25,
  "trades": [
    {
      "id": "uuid",
      "ts": 1700000000000,
      "symbol": "EUR/USD",
      "direction": "CALL",
      "expiry_min": 5,
      "stake": 10.0,
      "source": "user|tv|analyze",
      "status": "requested|executed|closed",
      "result": "W|L|B|?",
      "payout_pct": 80.0,
      "pnl": 8.0
    }, ...
  ]
}

"""

from __future__ import annotations

import json
import time
import uuid
import math
import os
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Any, Iterable
from datetime import datetime

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class TradeStatsStore:
    def __init__(self, file_path="trades.json"):
        self.file_path = file_path
        self.trades = []
        self.load()

    def load(self):
        if os.path.exists(self.file_path):
            with open(self.file_path, "r") as f:
                self.trades = json.load(f)
        else:
            self.trades = []

    def save(self):
        with open(self.file_path, "w") as f:
            json.dump(self.trades, f, indent=4)

    def log_trade(self, pair, signal, amount, result=None, profit_loss=0):
        trade = {
            "timestamp": datetime.utcnow().isoformat(),
            "pair": pair,
            "signal": signal,
            "amount": amount,
            "result": result,
            "profit_loss": profit_loss
        }
        self.trades.append(trade)
        self.save()

    def stats(self):
        total = len(self.trades)
        wins = len([t for t in self.trades if t.get("result") == "win"])
        losses = len([t for t in self.trades if t.get("result") == "loss"])
        winrate = (wins / total * 100) if total > 0 else 0
        profit = sum(t.get("profit_loss", 0) for t in self.trades)
        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "winrate": winrate,
            "total_profit": profit
        }


@dataclass
class TradeRecord:
    id: str
    ts: int
    symbol: str
    direction: str  # CALL/PUT
    expiry_min: int
    stake: float
    source: str = "user"  # user|tv|analyze|auto
    status: str = "requested"  # requested|executed|closed
    result: str = "?"          # W L B ?
    payout_pct: float = 0.0    # actual payout % at time of trade
    pnl: float = 0.0           # realized when closed
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QuantumLevel:
    level: int
    name: str
    min_trades: int
    min_accuracy: float  # 0..1


@dataclass
class StatsSnapshot:
    balance_start: float
    balance_current: float
    total_pnl: float
    total_trades: int
    wins: int
    losses: int
    breakeven: int
    win_rate: float
    avg_win: float
    avg_loss: float
    max_drawdown: float
    max_loss_trade: float
    consec_losses: int
    best_signal_perf: float
    worst_signal_perf: float
    quantum_level: QuantumLevel
    last_updated_ts: int
    source_info: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["quantum_level"] = {
            "level": self.quantum_level.level,
            "name": self.quantum_level.name,
            "min_trades": self.quantum_level.min_trades,
            "min_accuracy": self.quantum_level.min_accuracy,
        }
        return d


# ---------------------------------------------------------------------------
# Quantum level thresholds
# ---------------------------------------------------------------------------

DEFAULT_QUANTUM_LEVELS: List[QuantumLevel] = [
    QuantumLevel(1, "Bronze",      min_trades=0,    min_accuracy=0.00),
    QuantumLevel(2, "Silver",      min_trades=25,   min_accuracy=0.45),
    QuantumLevel(3, "Gold",        min_trades=100,  min_accuracy=0.55),
    QuantumLevel(4, "Platinum",    min_trades=250,  min_accuracy=0.65),
    QuantumLevel(5, "Quantum",     min_trades=500,  min_accuracy=0.75),
    QuantumLevel(6, "Quantum+",    min_trades=1000, min_accuracy=0.80),
]


def determine_quantum_level(total_trades: int,
                            accuracy: float,
                            levels: List[QuantumLevel] = DEFAULT_QUANTUM_LEVELS) -> QuantumLevel:
    # pick the highest level whose requirements are met
    best = levels[0]
    for lvl in levels:
        if total_trades >= lvl.min_trades and accuracy >= lvl.min_accuracy:
            best = lvl
    return best


# ---------------------------------------------------------------------------
# Trade Logger
# ---------------------------------------------------------------------------

class TradeLogger:
    """
    JSON-backed trade log. Thread-safe enough for low-volume bots (single process).
    """

    def __init__(self,
                 path: str,
                 starting_balance: float = 1000.0,
                 autosave: bool = True):
        self.path = path
        self.autosave = autosave
        self.balance_start = float(starting_balance)
        self.balance_current = float(starting_balance)
        self.trades: List[TradeRecord] = []
        self._load_if_exists()

    # --- IO ----------------------------------------------------------------

    def _load_if_exists(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return
        self.balance_start = float(data.get("balance_start", self.balance_start))
        self.balance_current = float(data.get("balance_current", self.balance_current))
        trades_in = data.get("trades", [])
        self.trades = []
        for t in trades_in:
            try:
                self.trades.append(TradeRecord(
                    id=str(t.get("id") or uuid.uuid4()),
                    ts=int(t.get("ts") or int(time.time() * 1000)),
                    symbol=str(t.get("symbol") or "?"),
                    direction=str(t.get("direction") or "CALL"),
                    expiry_min=int(t.get("expiry_min") or 5),
                    stake=float(t.get("stake") or 0),
                    source=str(t.get("source") or "user"),
                    status=str(t.get("status") or "requested"),
                    result=str(t.get("result") or "?"),
                    payout_pct=float(t.get("payout_pct") or 0.0),
                    pnl=float(t.get("pnl") or 0.0),
                    notes=str(t.get("notes") or ""),
                ))
            except Exception:
                continue

    def _save(self):
        if not self.autosave:
            return
        tmp = self.path + ".tmp"
        data = {
            "balance_start": self.balance_start,
            "balance_current": self.balance_current,
            "trades": [t.to_dict() for t in self.trades],
        }
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # --- Helpers -----------------------------------------------------------

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _find_trade(self, trade_id: str) -> Optional[TradeRecord]:
        for t in self.trades:
            if t.id == trade_id:
                return t
        return None

    # --- Record phases -----------------------------------------------------

    def record_signal(self,
                      symbol: str,
                      direction: str,
                      expiry_min: int,
                      stake: float,
                      source: str = "user",
                      notes: str = "") -> TradeRecord:
        """
        Create a new requested trade.
        """
        rec = TradeRecord(
            id=str(uuid.uuid4()),
            ts=self._now_ms(),
            symbol=symbol,
            direction=direction.upper(),
            expiry_min=int(expiry_min),
            stake=float(stake),
            source=source,
            status="requested",
            notes=notes,
        )
        self.trades.append(rec)
        self._save()
        return rec

    def record_execution(self, trade_id: str, payout_pct: float = 0.0):
        """
        Mark trade executed (order sent to broker).
        """
        t = self._find_trade(trade_id)
        if not t:
            return
        t.status = "executed"
        t.payout_pct = float(payout_pct)
        self._save()

    def record_result(self,
                      trade_id: str,
                      result: str,
                      pnl: float,
                      balance_after: Optional[float] = None,
                      notes: str = ""):
        """
        Mark trade closed with W/L/B & update running balance.
        """
        t = self._find_trade(trade_id)
        if not t:
            return
        t.status = "closed"
        t.result = result.upper()
        t.pnl = float(pnl)
        if notes:
            t.notes = notes

        self.balance_current += pnl if balance_after is None else 0.0
        if balance_after is not None:
            self.balance_current = float(balance_after)
        self._save()

    # --- Stats -------------------------------------------------------------

    def _iter_closed(self) -> Iterable[TradeRecord]:
        for t in self.trades:
            if t.status == "closed":
                yield t

    def _iter_executed_or_closed(self) -> Iterable[TradeRecord]:
        for t in self.trades:
            if t.status in ("executed", "closed"):
                yield t

    def summary(self) -> StatsSnapshot:
        closed = list(self._iter_closed())
        total_trades = len(closed)
        wins = sum(1 for t in closed if t.result == "W")
        losses = sum(1 for t in closed if t.result == "L")
        breakeven = sum(1 for t in closed if t.result == "B")
        total_pnl = sum(t.pnl for t in closed)

        win_rate = (wins / total_trades) if total_trades > 0 else 0.0

        win_pnls = [t.pnl for t in closed if t.pnl > 0]
        loss_pnls = [t.pnl for t in closed if t.pnl < 0]

        avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0

        # max drawdown (equity curve)
        eq = self.balance_start
        max_peak = eq
        drawdowns = [0.0]
        max_loss_trade = 0.0
        consec_losses = 0
        worst_consec = 0
        for t in closed:
            eq += t.pnl
            if t.pnl < max_loss_trade:
                max_loss_trade = t.pnl
            if t.pnl < 0:
                consec_losses += 1
                if consec_losses > worst_consec:
                    worst_consec = consec_losses
            else:
                consec_losses = 0
            if eq > max_peak:
                max_peak = eq
            dd = (max_peak - eq) / max_peak if max_peak else 0.0
            drawdowns.append(dd)

        max_dd = max(drawdowns) if drawdowns else 0.0

        best_signal_perf = max(win_pnls) if win_pnls else 0.0
        worst_signal_perf = min(loss_pnls) if loss_pnls else 0.0

        lvl = determine_quantum_level(total_trades, win_rate)

        return StatsSnapshot(
            balance_start=self.balance_start,
            balance_current=self.balance_current,
            total_pnl=total_pnl,
            total_trades=total_trades,
            wins=wins,
            losses=losses,
            breakeven=breakeven,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            max_drawdown=max_dd,
            max_loss_trade=max_loss_trade,
            consec_losses=worst_consec,
            best_signal_perf=best_signal_perf,
            worst_signal_perf=worst_signal_perf,
            quantum_level=lvl,
            last_updated_ts=self._now_ms(),
        )

    # --- Formatting --------------------------------------------------------

    def stats_block(self, snapshot: Optional[StatsSnapshot] = None) -> str:
        """
        Pretty block for Telegram.
        """
        s = snapshot or self.summary()
        wr_pct = s.win_rate * 100.0
        pnl_pct = ((s.balance_current - s.balance_start) / s.balance_start * 100.0
                   if s.balance_start else 0.0)
        lvl_line = f"🔘 {s.quantum_level.name} (Level {s.quantum_level.level})"

        txt = (
            "📊 *Statistics Overview*\n\n"
            f"{lvl_line}\n"
            "Each level unlocks more features, greater accuracy, and stronger AI performance.\n\n"
            f"• *Total Profit/Loss:* {pnl_pct:+.0f}%\n"
            f"• *Total Trades:* {s.total_trades} ({s.wins} profitable/{s.losses} loss)\n"
            f"• *Success Rate:* {wr_pct:.0f}%\n"
            f"• *Average Profit Per Trade:* {s.avg_win:+.2f}$\n"
            f"• *Maximum Drawdown:* {s.max_drawdown*100:.0f}%\n\n"
            "📉 *Risk & Loss Metrics*\n"
            f"• Average Loss Per Trade: {s.avg_loss:.2f}$\n"
            f"• Max Loss on a Single Trade: {s.max_loss_trade:.2f}$\n"
            f"• Consecutive Losing Trades: {s.consec_losses}\n\n"
            "📡 *Signal Analysis*\n"
            f"• Signals Closed: {s.total_trades}\n"
            f"• Best Signal PnL: {s.best_signal_perf:+.2f}$\n"
            f"• Worst Signal PnL: {s.worst_signal_perf:+.2f}$\n\n"
            "‼️ Updated every three days ‼️"
        )
        return txt


# ---------------------------------------------------------------------------
# Convenience loader
# ---------------------------------------------------------------------------

def load_logger(path: str, starting_balance: float = 1000.0, autosave: bool = True) -> TradeLogger:
    return TradeLogger(path=path, starting_balance=starting_balance, autosave=autosave)
