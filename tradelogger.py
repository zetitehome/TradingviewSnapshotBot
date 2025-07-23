# ---------------------------------------------------------------------------
# FILE 3/3: tradelogger.py
# ---------------------------------------------------------------------------

"""
tradelogger.py – Persistent trade & signal statistics store
-----------------------------------------------------------

Stores every trade/alert to a JSON file and computes aggregated performance stats.
Supports Quantum Level tiers.

API:
    store = TradeStatsStore(pathlib.Path('state/stats.json'))
    store.record_trade(pair, direction, amount, amount_mode, expiry, result=None, profit=None)
    summary = store.summary_for_chat(chat_id)
    txt = format_stats_summary(summary)

Result codes expected: 'win','loss','tie', None (open/unknown).
Profit: signed float in account currency if known.

Quantum Levels (adjust thresholds below):
    L1 Beginner 0+ trades
    L2 Bronze   50+ trades
    L3 Silver   150+ trades
    L4 Gold     350+ trades
    L5 Quantum  750+ trades

You can map to accuracy or profit thresholds if preferred.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ------------------------------------------------------------------
# Data structures
# ------------------------------------------------------------------
@dataclass
class TradeRecord:
    ts: float
    chat_id: int
    pair: str
    direction: str
    amount: float
    amount_mode: str  # '$' or '%'
    expiry: str
    result: Optional[str] = None  # win|loss|tie|None
    profit: Optional[float] = None  # signed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "chat_id": self.chat_id,
            "pair": self.pair,
            "direction": self.direction,
            "amount": self.amount,
            "amount_mode": self.amount_mode,
            "expiry": self.expiry,
            "result": self.result,
            "profit": self.profit,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TradeRecord":
        return cls(
            ts=float(d["ts"]),
            chat_id=int(d["chat_id"]),
            pair=str(d["pair"]),
            direction=str(d["direction"]),
            amount=float(d.get("amount", 0.0)),
            amount_mode=str(d.get("amount_mode", "$")),
            expiry=str(d.get("expiry", "")),
            result=d.get("result"),
            profit=float(d["profit"]) if d.get("profit") is not None else None,
        )


@dataclass
class StatsSummary:
    chat_id: int
    total_profit: float = 0.0
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    ties: int = 0
    max_drawdown: float = 0.0
    avg_profit: float = 0.0
    avg_loss: float = 0.0
    max_loss_single: float = 0.0
    max_consec_losses: int = 0
    signals_sent: int = 0
    signal_accuracy: float = 0.0  # 0-1
    best_signal: float = 0.0
    worst_signal: float = 0.0
    quantum_level: int = 1


# ------------------------------------------------------------------
# Quantum tiers
# ------------------------------------------------------------------
QUANTUM_THRESHOLDS = [0, 50, 150, 350, 750]  # trades


def quantum_level_for_trades(n: int) -> int:
    lvl = 1
    for i, th in enumerate(QUANTUM_THRESHOLDS, start=1):
        if n >= th:
            lvl = i
    return lvl


# ------------------------------------------------------------------
# Store
# ------------------------------------------------------------------
class TradeStatsStore:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.trades: List[TradeRecord] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.trades = [TradeRecord.from_dict(x) for x in data]
        except Exception:
            self.trades = []

    def save(self) -> None:
        try:
            out = [t.to_dict() for t in self.trades]
            self.path.write_text(json.dumps(out, indent=2), encoding='utf-8')
        except Exception:
            pass

    def record_trade(
        self,
        pair: str,
        direction: str,
        amount: float,
        amount_mode: str,
        expiry: str,
        result: Optional[str],
        profit: Optional[float] = None,
        chat_id: Optional[int] = None,
        ts: Optional[float] = None,
    ) -> None:
        import time
        rec = TradeRecord(
            ts=time.time() if ts is None else ts,
            chat_id=chat_id or 0,
            pair=pair,
            direction=direction,
            amount=amount,
            amount_mode=amount_mode,
            expiry=expiry,
            result=result,
            profit=profit,
        )
        self.trades.append(rec)
        self.save()

    # For signal stats we just treat each trade as a signal.
    def summary_for_chat(self, chat_id: int) -> StatsSummary:
        # gather recs
        recs = [t for t in self.trades if t.chat_id in (0, chat_id)]  # 0=global/unset
        s = StatsSummary(chat_id=chat_id)
        bal = 0.0
        peak = 0.0
        dd = 0.0
        consec_loss = 0
        max_consec_loss = 0
        best = 0.0
        worst = 0.0
        profs = []
        loss_vals = []
        for r in recs:
            if r.profit is not None:
                bal += r.profit
                if bal > peak:
                    peak = bal
                dd = min(dd, bal - peak)  # negative
                if r.profit > best:
                    best = r.profit
                if r.profit < worst:
                    worst = r.profit
                if r.profit < 0:
                    loss_vals.append(r.profit)
            if r.result == 'win':
                s.wins += 1
                consec_loss = 0
                profs.append(r.profit or 0.0)
            elif r.result == 'loss':
                s.losses += 1
                consec_loss += 1
                profs.append(r.profit or 0.0)
                if consec_loss > max_consec_loss:
                    max_consec_loss = consec_loss
            elif r.result == 'tie':
                s.ties += 1
                consec_loss = 0
        s.total_trades = len(recs)
        s.total_profit = bal
        s.max_drawdown = dd
        s.max_consec_losses = max_consec_loss
        s.best_signal = best
        s.worst_signal = worst
        s.avg_profit = (sum(p for p in profs if p > 0) / max(1, len([p for p in profs if p > 0]))) if profs else 0.0
        s.avg_loss = (sum(p for p in profs if p < 0) / max(1, len([p for p in profs if p < 0]))) if profs else 0.0
        s.signals_sent = s.total_trades
        denom = s.wins + s.losses
        s.signal_accuracy = (s.wins / denom) if denom > 0 else 0.0
        s.quantum_level = quantum_level_for_trades(s.total_trades)
        return s


# ------------------------------------------------------------------
# Formatting
# ------------------------------------------------------------------
QUANTUM_LEVEL_TEXT = {
    1: "Level 1 • Initiate",
    2: "Level 2 • Bronze",
    3: "Level 3 • Silver",
    4: "Level 4 • Gold",
    5: "Level 5 • Quantum",
}


def format_stats_summary(s: StatsSummary) -> str:
    lvl_text = QUANTUM_LEVEL_TEXT.get(s.quantum_level, f"Level {s.quantum_level}")
    winrate = (s.wins * 100.0 / max(1, s.wins + s.losses))
    txt = (
        "📊 *Statistics Overview*\n\n"
        f"🔘 *{lvl_text}*\n"
        "Each level unlocks more features, greater accuracy, and stronger AI performance.\n\n"
        f"• Total Profit/Loss: {s.total_profit:+.2f}\n"
        f"• Total Trades: {s.total_trades} ({s.wins} profitable/{s.losses} loss)\n"
        f"• Success Rate: {winrate:.0f}%\n"
        f"• Average Profit Per Trade: {s.avg_profit:+.2f}\n"
        f"• Maximum Drawdown: {s.max_drawdown:.2f}\n\n"
        "📉 *Risk & Loss Metrics*\n"
        f"• Average Loss Per Trade: {s.avg_loss:.2f}\n"
        f"• Max Loss on a Single Trade: {s.worst_signal:.2f}\n"
        f"• Consecutive Losing Trades: {s.max_consec_losses}\n\n"
        "📡 *Signal Analysis*\n"
        f"• Signals Sent: {s.signals_sent}\n"
        f"• Signal Accuracy: {s.signal_accuracy*100:.0f}%\n"
        f"• Best Signal Performance: {s.best_signal:+.2f}\n"
        f"• Worst Signal Performance: {s.worst_signal:+.2f}\n\n"
        "‼️ Updated when new trades logged ‼️"
    )
    return txt
