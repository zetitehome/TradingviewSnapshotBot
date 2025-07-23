"""
strategy.py
===========

Configurable technical-analysis + signal engine used by tvsnapshotbot.py.

Goals
-----
* Accept recent candle data (O/H/L/C/V arrays) from server.js (/snapshot ... fmt=json&candles=n).
* Compute lightweight indicators (EMA, SMMA, RSI, ATR, MA diff, volatility bands).
* Blend rules to produce CALL/PUT/NEUTRAL signal + confidence and recommended expiries.
* Provide trade-sizing helpers (by $ or % of balance) – high-level; final order routing handled in bot.
* Modular "Strategies" registry: you can plug in different methods without rewriting bot.
* Return structured result object the bot can render in Telegram + pass to UI.Vision macro.

Design Notes
------------
- Avoid heavyweight deps. Uses pure Python; will accelerate w/ NumPy if available.
- Work only with *recent* N candles to stay fast inside bot calls.
- Expiry suggestions tuned for binary options (1m/3m/5m/15m).
- Confidence score 0–1 (float). Map to emojis in bot UI.
- All functions pure & testable; no network I/O.

Usage
-----
from strategy import StrategyEngine, StrategyConfig
engine = StrategyEngine()
result = engine.analyze_symbol("EUR/USD", candles_dict, config=StrategyConfig())

See StrategyResult dataclass below for fields.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Callable, Any

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

    # convenience helpers
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
    # TradingView ta.rma approximation
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
    """
    Wilder RSI.
    """
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
    if avg_loss == 0.0:
        rsis[length] = 100.0
    else:
        rsis[length] = 100.0 - (100.0 / (1 + (avg_gain / avg_loss)))

    g = avg_gain
    l = avg_loss
    for i in range(length + 1, len(vals)):
        g = (g * (length - 1) + gains[i]) / length
        l = (l * (length - 1) + losses[i]) / length
        if l == 0.0:
            rsis[i] = 100.0
        else:
            rsis[i] = 100.0 - (100.0 / (1 + (g / l)))
    return rsis


def _tr(true_range_seq: List[float]) -> float:
    # unused single aggregator
    return statistics.mean(true_range_seq) if true_range_seq else 0.0


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
    # Wilder smoothing
    atrs = []
    if len(trs) <= length:
        avg = sum(trs) / len(trs)
        atrs = [avg] * len(trs)
        return atrs
    first = sum(trs[1:length + 1]) / length
    atrs = [trs[0]] * (length + 1)
    atrs[length] = first
    prev = first
    for i in range(length + 1, len(trs)):
        prev = (prev * (length - 1) + trs[i]) / length
        atrs.append(prev)
    # pad front if needed
    if len(atrs) < len(trs):
        atrs.extend([prev] * (len(trs) - len(atrs)))
    return atrs


# ---------------------------------------------------------------------------
# Signal building blocks
# ---------------------------------------------------------------------------

def _ma_diff_signal(closes: List[float], fast_len: int, slow_len: int, sig_len: int) -> Tuple[Optional[bool], float]:
    """
    Return (is_call, strength)
    where:
      is_call True => fast > signal line => bullish
      is_call False => fast < signal => bearish
      None => insufficient data
      strength: abs(diff normalized by close percentage)
    """
    if len(closes) < max(fast_len, slow_len, sig_len) + 2:
        return None, 0.0

    ema_fast = _ema(closes, fast_len)
    ema_slow = _ema(closes, slow_len)
    diff = [f - s for f, s in zip(ema_fast, ema_slow)]
    diff_sig = _ema(diff, sig_len)

    last_diff = diff[-1]
    last_sig = diff_sig[-1]
    is_call = last_diff > last_sig
    # relative magnitude
    lc = closes[-1]
    if lc == 0:
        strength = 0.0
    else:
        strength = abs(last_diff - last_sig) / lc
    return is_call, strength


def _trend_stack_signal(closes: List[float],
                        smma_len: int,
                        ema_short: int,
                        ema_mid2: int,
                        ema_mid1: int) -> Tuple[Optional[bool], float]:
    """
    Simplified 'Frass' trend stack:
    CALL if close>SMMA200 & ema_short>ema_mid2>ema_mid1
    PUT if close<SMMA200 & ema_short<ema_mid2<ema_mid1
    Returns (is_call, score)
    """
    ln = len(closes)
    if ln < max(smma_len, ema_mid1, ema_mid2, ema_short) + 1:
        return None, 0.0

    smma_val = _smma(closes, smma_len)
    e_short = _ema(closes, ema_short)
    e_mid2 = _ema(closes, ema_mid2)
    e_mid1 = _ema(closes, ema_mid1)

    c = closes[-1]
    sm = smma_val[-1]
    es = e_short[-1]
    em2 = e_mid2[-1]
    em1v = e_mid1[-1]

    if c > sm and es > em2 > em1v:
        # bullish
        dist = (c - sm) / sm if sm else 0.0
        return True, abs(dist)
    elif c < sm and es < em2 < em1v:
        dist = (sm - c) / sm if sm else 0.0
        return False, abs(dist)
    else:
        return None, 0.0


def _rsi_filter(closes: List[float], length: int, ob: float, os: float, enabled: bool) -> Tuple[bool, float, float]:
    """
    Return (ok, rsi_value, bias_score)
    If disabled => ok True, neutral bias_score 0
    If enabled:
      ok True when rsi within (os..ob) OR at extreme but direction aligns
      bias_score positive if oversold (CALL bias), negative if overbought (PUT bias)
    """
    if not enabled or length < 1 or len(closes) < length + 2:
        return True, 50.0, 0.0
    rsis = _rsi(closes, length)
    r = rsis[-1]
    if r >= os and r <= ob:
        return True, r, 0.0
    # extremes
    if r < os:
        # oversold -> CALL bias
        return True, r, +1.0
    if r > ob:
        # overbought -> PUT bias
        return True, r, -1.0
    return True, r, 0.0


def _atr_filter(highs: List[float], lows: List[float], closes: List[float],
                length: int, min_atr: float, enabled: bool) -> Tuple[bool, float]:
    """
    (ok, atr_val)
    """
    if not enabled or length < 1:
        return True, 0.0
    if len(closes) < length + 2:
        return False, 0.0
    atrs = _atr(highs, lows, closes, length)
    a = atrs[-1]
    return (a >= min_atr), a


# ---------------------------------------------------------------------------
# Expiry suggestion
# ---------------------------------------------------------------------------

def suggest_expiries(tf_str: str,
                     call_bias: Optional[bool],
                     rsi_bias: float,
                     config: StrategyConfig) -> List[int]:
    """
    Build expiry list in minutes based on timeframe + directional context.
    Basic rules:
      - Always include 1,3,5,15 but reorder.
      - If rsi extreme => shorter first.
      - If strong trend stack => longer first.
    """
    default_order = config.expiry_by_tf.get(tf_str, [1, 3, 5, 15])
    out = default_order[:]

    # RSI extremes -> push 1m or 3m earlier
    if rsi_bias != 0.0:
        if rsi_bias > 0:  # oversold => expecting bounce -> shorter
            _promote(out, 1)
            _promote(out, 3)
        else:  # overbought
            _promote(out, 1)
            _promote(out, 3)

    # If no bias but we have trend continuation (call_bias not None)
    if rsi_bias == 0.0 and call_bias is not None:
        # trending -> prefer 5 or 15
        _promote(out, 5)
        _promote(out, 15)

    # ensure uniqueness and all known
    uniq = []
    added = set()
    for e in out:
        if e not in added:
            uniq.append(e)
            added.add(e)
    # always ensure baseline pool
    for e in (1, 3, 5, 15):
        if e not in added:
            uniq.append(e)
            added.add(e)
    return uniq


def _promote(seq: List[int], val: int):
    if val in seq:
        seq.remove(val)
    seq.insert(0, val)


# ---------------------------------------------------------------------------
# Score fusion
# ---------------------------------------------------------------------------

def fuse_scores(ma_diff: Tuple[Optional[bool], float],
                trend_stack: Tuple[Optional[bool], float],
                rsi_filter_out: Tuple[bool, float, float],
                atr_filter_out: Tuple[bool, float],
                config: StrategyConfig) -> Tuple[str, float, str]:
    """
    Combine sub-signals into final direction/confidence/reasoning text.
    """

    is_call_ma, ma_strength = ma_diff
    is_call_stack, stack_strength = trend_stack
    rsi_ok, rsi_val, rsi_bias = rsi_filter_out
    atr_ok, atr_val = atr_filter_out

    # Weighted directional score: + for CALL, - for PUT
    dir_score = 0.0
    reason_bits = []

    if is_call_ma is not None:
        s = ma_strength * config.weight_ma_diff
        dir_score += s if is_call_ma else -s
        reason_bits.append(f"MA-diff:{'CALL' if is_call_ma else 'PUT'}({ma_strength:.3f})")

    if is_call_stack is not None:
        s = stack_strength * config.weight_trend_stack
        dir_score += s if is_call_stack else -s
        reason_bits.append(f"TrendStack:{'CALL' if is_call_stack else 'PUT'}({stack_strength:.3f})")

    if rsi_ok:
        # rsi_bias positive -> CALL tilt, negative -> PUT tilt
        s = abs(rsi_bias) * config.weight_rsi_filter
        if rsi_bias > 0:
            dir_score += s
            if s > 0: reason_bits.append(f"RSI OS({rsi_val:.1f})")
        elif rsi_bias < 0:
            dir_score -= s
            if s > 0: reason_bits.append(f"RSI OB({rsi_val:.1f})")
        else:
            reason_bits.append(f"RSI neutral({rsi_val:.1f})")
    else:
        reason_bits.append("RSI disabled/insufficient")

    if atr_ok:
        reason_bits.append(f"ATR ok({atr_val:.5f})")
        s = config.weight_atr_ok  # stable vol adds confidence but not direction
    else:
        reason_bits.append(f"ATR low({atr_val:.5f})")
        s = 0.0

    # Map dir_score -> direction + confidence
    dir_abs = abs(dir_score)
    if dir_abs < 1e-6:
        direction = "NEUTRAL"
    else:
        direction = "CALL" if dir_score > 0 else "PUT"

    # conf scaling: clamp 0..1; amplify mild signals to visible 0.05
    confidence = max(0.0, min(1.0, dir_abs))
    if confidence < 0.05:
        confidence = 0.05

    reason = "; ".join(reason_bits)
    return direction, confidence, reason


# ---------------------------------------------------------------------------
# Strategy Engine
# ---------------------------------------------------------------------------

class StrategyEngine:
    """
    Main entry point. Keeps no state between calls (stateless); safe for threads.
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()

    # --- Candle extraction helpers -----------------------------------------

    @staticmethod
    def _normalize_candle_input(data: Any) -> List[Candle]:
        """
        Accept various forms:
          [{'t':..., 'o':..., 'h':..., 'l':..., 'c':..., 'v':...}, ...]
          [[t,o,h,l,c,v], ...]
          or parallel arrays
        Returns list[Candle] sorted by time ascending.
        """
        if isinstance(data, dict):
            # maybe parallel arrays
            ts = data.get("t") or data.get("time") or []
            os_ = data.get("o") or data.get("open") or []
            hs = data.get("h") or data.get("high") or []
            ls = data.get("l") or data.get("low") or []
            cs = data.get("c") or data.get("close") or []
            vs = data.get("v") or data.get("volume") or [0.0] * len(cs)
            out = []
            ln = min(len(ts), len(os_), len(hs), len(ls), len(cs), len(vs))
            for i in range(ln):
                out.append(Candle(int(ts[i]), float(os_[i]), float(hs[i]), float(ls[i]), float(cs[i]), float(vs[i])))
            return sorted(out, key=lambda k: k.t)

        if isinstance(data, list):
            if len(data) == 0:
                return []
            first = data[0]
            out = []
            if isinstance(first, dict):
                for d in data:
                    out.append(Candle(
                        int(d.get("t") or d.get("time") or 0),
                        float(d.get("o") or d.get("open") or 0),
                        float(d.get("h") or d.get("high") or 0),
                        float(d.get("l") or d.get("low") or 0),
                        float(d.get("c") or d.get("close") or 0),
                        float(d.get("v") or d.get("volume") or 0),
                    ))
                return sorted(out, key=lambda k: k.t)
            else:
                # assume nested list [t,o,h,l,c,v]
                for row in data:
                    if len(row) < 5:
                        continue
                    t, o_, h_, l_, c_ = row[:5]
                    v_ = row[5] if len(row) > 5 else 0.0
                    out.append(Candle(int(t), float(o_), float(h_), float(l_), float(c_), float(v_)))
                return sorted(out, key=lambda k: k.t)

        # fallback none
        return []

    # --- Main analysis -----------------------------------------------------

    def analyze_symbol(self,
                       symbol: str,
                       candles_in: Any,
                       timeframe: Optional[str] = None,
                       config: Optional[StrategyConfig] = None) -> StrategyResult:
        cfg = config or self.config
        candles = self._normalize_candle_input(candles_in)
        if not candles:
            return StrategyResult(
                symbol=symbol,
                direction="NEUTRAL",
                confidence=0.0,
                expiry_candidates=[cfg.expiry_1m, cfg.expiry_3m, cfg.expiry_5m, cfg.expiry_15m],
                reasoning="No candle data.",
                indicators={},
                last_price=None,
                timeframe=timeframe,
            )

        closes = [c.c for c in candles]
        highs = [c.h for c in candles]
        lows = [c.l for c in candles]

        # Sub-signals
        ma_diff_out = (None, 0.0)
        if cfg.use_ma_diff_cross:
            ma_diff_out = _ma_diff_signal(closes, cfg.ema_fast, cfg.ema_slow, cfg.signal_len)

        trend_stack_out = (None, 0.0)
        if cfg.use_frass_stack:
            trend_stack_out = _trend_stack_signal(closes, cfg.smma_len, cfg.ema_short, cfg.ema_mid2, cfg.ema_mid1)

        rsi_out = _rsi_filter(closes, cfg.rsi_len, cfg.rsi_ob, cfg.rsi_os, cfg.enable_rsi)
        atr_out = _atr_filter(highs, lows, closes, cfg.atr_len, cfg.atr_min, cfg.enable_atr)

        direction, conf, reason = fuse_scores(ma_diff_out, trend_stack_out, rsi_out, atr_out, cfg)

        expiry_list = suggest_expiries(
            tf_str=(timeframe or "1"),
            call_bias={'CALL': True, 'PUT': False, 'NEUTRAL': None}[direction],
            rsi_bias=rsi_out[2],
            config=cfg,
        )

        indicators = {
            "ema_fast_len": cfg.ema_fast,
            "ema_slow_len": cfg.ema_slow,
            "smma_len": cfg.smma_len,
            "rsi": rsi_out[1],
            "atr": atr_out[1],
            "ma_diff_is_call": ma_diff_out[0],
            "ma_diff_strength": ma_diff_out[1],
            "trend_stack_is_call": trend_stack_out[0],
            "trend_stack_strength": trend_stack_out[1],
            "rsi_bias": rsi_out[2],
        }

        return StrategyResult(
            symbol=symbol,
            direction=direction,
            confidence=conf,
            expiry_candidates=expiry_list,
            reasoning=reason,
            indicators=indicators,
            last_price=closes[-1],
            timeframe=timeframe,
        )

    # ----------------------------------------------------------------------
    # Trade sizing
    # ----------------------------------------------------------------------

    def compute_trade_size(
        self,
        balance: float,
        use_pct: Optional[float] = None,
        use_usd: Optional[float] = None,
        min_usd: float = 1.0,
        max_usd: Optional[float] = None,
        config: Optional[StrategyConfig] = None,
    ) -> float:
        """
        Choose trade size from balance.
        Order of precedence:
          use_usd if passed
          else use_pct
          else config default_pct
        Enforce min_usd and optional max_usd.
        """
        cfg = config or self.config
        if use_usd is not None:
            size = use_usd
        else:
            pct = use_pct if use_pct is not None else cfg.default_trade_pct
            size = balance * (pct / 100.0)
        if size < min_usd:
            size = min_usd
        if max_usd is not None and size > max_usd:
            size = max_usd
        return round(size, 2)

    # ----------------------------------------------------------------------
    # Pocket Option payload builder
    # ----------------------------------------------------------------------

    def build_pocket_option_payload(
        self,
        symbol: str,
        direction: str,
        expiry_min: int,
        amount_usd: float,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Basic payload that UI.Vision macro (or n8n) can consume.
        """
        payload = {
            "symbol": symbol,
            "direction": direction,  # CALL | PUT
            "expiry_min": expiry_min,
            "amount": amount_usd,
        }
        if meta:
            payload["meta"] = meta
        return payload
