# ---------------------------------------------------------------------------
# FILE 2/3: strategy.py
# ---------------------------------------------------------------------------

"""
strategy.py – Lightweight TA helpers for QuantumTraderBot
---------------------------------------------------------

Functions:
    quick_analyze(pair: str, js: dict) -> AnalysisResult (from tvsnapshotbot)

The JS candle dict is expected to contain lists under keys: 't','o','h','l','c','v'.
Length >= ~20 recommended.

We compute:
  • EMA fast/slow (default 7/25)
  • RSI(14)
  • ATR(14)
  • Short momentum slope (last close vs avg N)

Decision (simple):
  CALL if price above both EMAs & RSI > 50.
  PUT  if price below both EMAs & RSI < 50.
  else NEUTRAL.

Suggested expiry: 1m if ATR small, else 5m; bump to 15m if conflicting signals.

You can modify threshold constants below.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, List
import math

# Import the AnalysisResult dataclass from main if available; else define shim.
try:
    from tvsnapshotbot import AnalysisResult  # type: ignore
except Exception:
    from dataclasses import dataclass, field
    @dataclass
    class AnalysisResult:  # minimal fallback
        direction: Optional[str]
        confidence: float
        comment: str
        indicators: Dict[str, Any] = field(default_factory=dict)
        suggested_expiry: str = "5m"


# ------------------------------------------------------------------
# Core math helpers
# ------------------------------------------------------------------

def _ema(series: List[float], length: int) -> List[float]:
    if not series:
        return []
    k = 2.0 / (length + 1.0)
    out = [series[0]]
    for v in series[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def _rsi(series: List[float], length: int = 14) -> List[float]:
    if len(series) < 2:
        return [50.0] * len(series)
    gains = []
    losses = []
    for i in range(1, len(series)):
        diff = series[i] - series[i-1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:length]) / max(length, 1)
    avg_loss = sum(losses[:length]) / max(length, 1)
    out = [50.0]*(length+1)
    for i in range(length, len(gains)):
        avg_gain = (avg_gain*(length-1) + gains[i]) / length
        avg_loss = (avg_loss*(length-1) + losses[i]) / length
        rs = avg_gain / avg_loss if avg_loss > 1e-9 else 999.0
        out.append(100.0 - 100.0/(1.0+rs))
    # pad if needed
    while len(out) < len(series):
        out.append(out[-1])
    return out[:len(series)]


def _atr(o: List[float], h: List[float], l: List[float], c: List[float], length: int = 14) -> List[float]:
    if not c:
        return []
    trs = []
    prev_close = c[0]
    for hi, lo, cl in zip(h, l, c):
        tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
        prev_close = cl
    out = []
    avg = sum(trs[:length]) / max(length, 1)
    out.extend([avg]*length)
    for i in range(length, len(trs)):
        avg = (avg*(length-1) + trs[i]) / length
        out.append(avg)
    return out[:len(c)]


# ------------------------------------------------------------------
# Quick analyze
# ------------------------------------------------------------------
DEFAULT_FAST = 7
DEFAULT_SLOW = 25


def quick_analyze(pair: str, js: Dict[str, Any]) -> AnalysisResult:
    c = js.get("c") or []
    h = js.get("h") or []
    l = js.get("l") or []
    o = js.get("o") or []
    if not c:
        return AnalysisResult(direction=None, confidence=0.0, comment="No data", indicators={}, suggested_expiry="5m")

    ema_fast = _ema(c, DEFAULT_FAST)
    ema_slow = _ema(c, DEFAULT_SLOW)
    rsi_vals = _rsi(c, 14)
    atr_vals = _atr(o, h, l, c, 14)

    last_close = c[-1]
    last_fast = ema_fast[-1]
    last_slow = ema_slow[-1]
    last_rsi = rsi_vals[-1]
    last_atr = atr_vals[-1] if atr_vals else 0.0

    direction = None
    conf = 0.5
    comment_parts = []

    if last_close > last_fast > last_slow and last_rsi > 50:
        direction = "CALL"
        conf = min(1.0, max(0.55, (last_rsi-50)/50))
        comment_parts.append("Trend up above EMAs.")
    elif last_close < last_fast < last_slow and last_rsi < 50:
        direction = "PUT"
        conf = min(1.0, max(0.55, (50-last_rsi)/50))
        comment_parts.append("Trend down below EMAs.")
    else:
        direction = None
        conf = 0.25
        comment_parts.append("Mixed signals; range.")

    # ATR-based expiry suggestion
    if last_atr <= 0:
        sug = "5m"
    else:
        # relative ATR vs price
        rel = last_atr / last_close if last_close else 0
        if rel < 0.0005:  # very quiet
            sug = "1m"
        elif rel < 0.0015:
            sug = "3m"
        elif rel < 0.003:
            sug = "5m"
        else:
            sug = "15m"

    comment = " ".join(comment_parts)
    indicators = {
        "ema_fast": last_fast,
        "ema_slow": last_slow,
        "rsi": last_rsi,
        "atr": last_atr,
    }
    return AnalysisResult(direction=direction, confidence=conf, comment=comment, indicators=indicators, suggested_expiry=sug)
