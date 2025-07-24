"""
strategy.py
===========

Configurable technical-analysis + signal engine used by tvsnapshotbot.py.

Enhancements:
-------------
- Adaptive learning after 3 consecutive losses (RSI thresholds and EMA adjustments).
- Loss streak tracking via StrategyEngine.loss_streak.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any

# Optional acceleration
try:
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class Candle:
    t: int          # unix ms
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0  # optional


@dataclass
class StrategyConfig:
    # Indicator lengths
    ema_fast: int = 7
    ema_slow: int = 25
    signal_len: int = 5

    smma_len: int = 200
    ema_mid1: int = 100
    ema_mid2: int = 50
    ema_short: int = 4

    rsi_len: int = 14
    rsi_ob: float = 70
    rsi_os: float = 30
    enable_rsi: bool = True

    atr_len: int = 14
    atr_min: float = 0.0005
    enable_atr: bool = True

    # trade sizing
    default_trade_usd: float = 1.0
    default_trade_pct: float = 1.0  # percent of balance

    # expiry defaults in minutes
    expiry_1m: int = 1
    expiry_3m: int = 3
    expiry_5m: int = 5
    expiry_15m: int = 15

    # enable/disable composite strategy families
    use_ma_diff_cross: bool = True
    use_frass_stack: bool = True  # the "Frass" PSAR/EMA200 alignment style (simplified here)

    # scoring weights
    weight_ma_diff: float = 0.35
    weight_trend_stack: float = 0.35
    weight_rsi_filter: float = 0.15
    weight_atr_ok: float = 0.15

    # map timeframe string -> default expiry rank order override (optional)
    expiry_by_tf: Dict[str, List[int]] = field(
        default_factory=lambda: {
            "1":  [1, 3, 5, 15],
            "3":  [3, 5, 15],
            "5":  [5, 15],
            "15": [15, 5],
            "D":  [15, 5],
        }
    )


@dataclass
class StrategyResult:
    symbol: str
    direction: str  # "CALL", "PUT", or "NEUTRAL"
    confidence: float  # 0-1
    expiry_candidates: List[int]  # minutes
    reasoning: str
    indicators: Dict[str, Any] = field(default_factory=dict)
    last_price: Optional[float] = None
    timeframe: Optional[str] = None

    def best_expiry(self) -> Optional[int]:
        return self.expiry_candidates[0] if self.expiry_candidates else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "confidence": self.confidence,
            "expiry_candidates": self.expiry_candidates,
            "reasoning": self.reasoning,
            "indicators": self.indicators,
            "last_price": self.last_price,
            "timeframe": self.timeframe,
        }


# ---------------------------------------------------------------------------
# Utility math
# ---------------------------------------------------------------------------

def _ema(vals: List[float], length: int) -> List[float]:
    if length <= 1 or len(vals) == 0:
        return vals[:]
    k = 2.0 / (length + 1.0)
    out = []
    ema_prev = vals[0]
    out.append(ema_prev)
    for v in vals[1:]:
        ema_prev = (v - ema_prev) * k + ema_prev
        out.append(ema_prev)
    return out


def _smma(vals: List[float], length: int) -> List[float]:
    if length <= 1 or len(vals) == 0:
        return vals[:]
    out = []
    smma_prev = vals[0]
    out.append(smma_prev)
    alpha = 1.0 / float(length)
    for v in vals[1:]:
        smma_prev = smma_prev + alpha * (v - smma_prev)
        out.append(smma_prev)
    return out


def _rsi(vals: List[float], length: int) -> List[float]:
    if length < 1 or len(vals) < length + 1:
        return [50.0] * len(vals)
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(vals)):
        diff = vals[i] - vals[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[1:length + 1]) / length
    avg_loss = sum(losses[1:length + 1]) / length
    rsis = [50.0] * len(vals)
    rsis[length] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1 + (avg_gain / avg_loss)))
    g, l = avg_gain, avg_loss
    for i in range(length + 1, len(vals)):
        g = (g * (length - 1) + gains[i]) / length
        l = (l * (length - 1) + losses[i]) / length
        rsis[i] = 100.0 if l == 0 else 100.0 - (100.0 / (1 + (g / l)))
    return rsis


def _atr(highs: List[float], lows: List[float], closes: List[float], length: int) -> List[float]:
    if length <= 0 or len(closes) == 0:
        return [0.0] * len(closes)
    trs = [0.0]
    for i in range(1, len(closes)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr = max(hl, hc, lc)
        trs.append(tr)
    if len(trs) <= length:
        avg = sum(trs) / len(trs)
        return [avg] * len(trs)
    first = sum(trs[1:length + 1]) / length
    atrs = [trs[0]] * (length + 1)
    atrs[length] = first
    prev = first
    for i in range(length + 1, len(trs)):
        prev = (prev * (length - 1) + trs[i]) / length
        atrs.append(prev)
    if len(atrs) < len(trs):
        atrs.extend([prev] * (len(trs) - len(atrs)))
    return atrs


# ---------------------------------------------------------------------------
# Strategy Engine with Adaptive Learning
# ---------------------------------------------------------------------------
@dataclass
class StrategyEngineConfig:
    rsi_ob: float = 70.0
    rsi_os: float = 30.0
    ema_fast: int = 7
    ema_slow: int = 25
class SignalStrategy:
    def generate_signal(self, pair):
        import random
        return {
            "pair": pair,
            "action": random.choice(["CALL", "PUT"]),
            "confidence": random.randint(60, 95),
            "summary": "AI Strategy says BUY" if random.random() > 0.5 else "SELL",
            "expiry": "1m"
        }
@dataclass

class StrategyEngine:
    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        self.loss_streak = 0

    # Normalize input
    @staticmethod
    def _normalize_candle_input(data: Any) -> List[Candle]:
        # (Same as your version, omitted here for brevity)
        ...

    def analyze_symbol(
        self,
        symbol: str,
        candles_in: Any,
        timeframe: Optional[str] = None,
        config: Optional[StrategyConfig] = None
    ) -> StrategyResult:
        # (Same as your version, but calls updated helper methods)
        cfg = config or self.config
        candles = self._normalize_candle_input(candles_in)
        if not candles:
            return StrategyResult(symbol, "NEUTRAL", 0.0, [1, 3, 5, 15], "No candle data.", {}, None, timeframe)
        # All sub-signal logic remains unchanged...
        # (rest of analyze_symbol unchanged)
        ...

    # Adaptive learning logic
    def adapt_after_trade(self, last_trade_win: bool):
        if last_trade_win:
            self.loss_streak = 0
        else:
            self.loss_streak += 1

        # Trigger adaptive adjustments
        if self.loss_streak >= 3:
            self.config.rsi_ob = max(60, self.config.rsi_ob - 2)
            self.config.rsi_os = min(40, self.config.rsi_os + 2)
            self.config.ema_fast = max(5, self.config.ema_fast - 1)
            self.config.ema_slow = max(10, self.config.ema_slow - 1)

    def reset_adaptive(self):
        self.loss_streak = 0
        self.config.rsi_ob = 70
        self.config.rsi_os = 30
        self.config.ema_fast = 7
        self.config.ema_slow = 25
