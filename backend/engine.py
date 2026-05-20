"""
Aurum FX — trading engine (Python port of supabase/functions/_shared/engine.ts).
Pure functions: indicators, regime detection, session, signal generation, lot sizing.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, TypedDict
import math


# ---------- Data types ----------
class Candle(TypedDict):
    t: int  # unix ms
    o: float
    h: float
    l: float  # noqa: E741
    c: float


Session = Literal["asia", "london", "new_york", "overlap", "off"]
Regime = Literal["trending_up", "trending_down", "ranging", "volatile"]
Side = Literal["buy", "sell"]


@dataclass
class StrategyConfig:
    ema_fast: int = 21
    ema_slow: int = 55
    rsi_period: int = 14
    atr_period: int = 14
    sl_atr: float = 1.5
    tp_atr: float = 2.5
    min_confidence: float = 0.3

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyConfig":
        if not d:
            return cls()
        return cls(
            ema_fast=int(d.get("ema_fast", 21)),
            ema_slow=int(d.get("ema_slow", 55)),
            rsi_period=int(d.get("rsi_period", 14)),
            atr_period=int(d.get("atr_period", 14)),
            sl_atr=float(d.get("sl_atr", 1.5)),
            tp_atr=float(d.get("tp_atr", 2.5)),
            min_confidence=float(d.get("min_confidence", 0.5)),
        )


@dataclass
class GeneratedSignal:
    side: Side
    entry: float
    sl: float
    tp: float
    confidence: float
    regime: Regime
    session: Session
    reason: str
    mode: str = "swing"           # "swing" | "scalp"
    max_hold_minutes: int = 0     # 0 = use bot/global TTL; >0 = bridge auto-close after N min


@dataclass
class ScalpConfig:
    """Range-mode configuration (used when regime == 'ranging').
    RSI mean-reversion: RSI < rsi_buy → BUY, RSI > rsi_sell → SELL.
    Symmetric 1.0× ATR SL and TP. Lot is halved at sizing time.
    """
    rsi_period: int = 14
    rsi_buy: float = 32.0         # RSI strictly below → BUY
    rsi_sell: float = 68.0        # RSI strictly above → SELL
    sl_atr: float = 1.0
    tp_atr: float = 1.0
    min_confidence: float = 0.50
    max_hold_minutes: int = 60    # auto-close after 60 min — range setups don't sit forever

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "ScalpConfig":
        d = d or {}
        return cls(
            rsi_period=int(d.get("rsi_period", 14)),
            rsi_buy=float(d.get("rsi_buy", 32.0)),
            rsi_sell=float(d.get("rsi_sell", 68.0)),
            sl_atr=float(d.get("sl_atr", 1.0)),
            tp_atr=float(d.get("tp_atr", 1.0)),
            min_confidence=float(d.get("min_confidence", 0.50)),
            max_hold_minutes=int(d.get("max_hold_minutes", 60)),
        )


# ---------- Indicators ----------
def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out: List[float] = []
    prev = values[0]
    for i, v in enumerate(values):
        prev = v if i == 0 else v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(values: List[float], period: int = 14) -> List[float]:
    n = len(values)
    out = [50.0] * n
    if n < period + 1:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_g = gains / period
    avg_l = losses / period
    out[period] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period + 1, n):
        d = values[i] - values[i - 1]
        g = d if d > 0 else 0
        loss = -d if d < 0 else 0
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + loss) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def atr(candles: List[Candle], period: int = 14) -> List[float]:
    n = len(candles)
    if n == 0:
        return []
    trs: List[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["h"] - c["l"])
            continue
        p = candles[i - 1]
        trs.append(max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])))
    out = [0.0] * n
    if n < period:
        return out
    s = sum(trs[:period])
    out[period - 1] = s / period
    for i in range(period, len(trs)):
        out[i] = (out[i - 1] * (period - 1) + trs[i]) / period
    return out


# ---------- Sessions ----------
def current_session(d: Optional[datetime] = None) -> Session:
    d = d or datetime.now(timezone.utc)
    h = d.hour
    if 13 <= h < 15:
        return "overlap"
    if 7 <= h < 15:
        return "london"
    if 13 <= h < 21:
        return "new_york"
    if 0 <= h < 7:
        return "asia"
    return "off"


# ---------- Regime ----------
def detect_regime(
    candles: List[Candle],
    ema_fast: List[float],
    ema_slow: List[float],
    atr_series: List[float],
) -> Regime:
    i = len(candles) - 1
    if i < 30:
        return "ranging"
    slope = (ema_slow[i] - ema_slow[i - 10]) / 10
    spread = abs(ema_fast[i] - ema_slow[i])
    atr_val = atr_series[i] or 1e-9
    last20 = candles[-20:]
    avg_close = sum(c["c"] for c in last20) / 20
    vol_ratio = atr_val / avg_close if avg_close else 0
    if vol_ratio > 0.005:
        return "volatile"
    if spread / atr_val < 0.4:
        return "ranging"
    return "trending_up" if slope > 0 else "trending_down"


# ---------- Signal generation ----------
def bollinger(values: List[float], period: int = 20, mult: float = 2.0) -> tuple[list, list, list]:
    """Returns (mid, upper, lower) bands. Naive SMA-based — matches MT5 default."""
    n = len(values)
    mid = [0.0] * n
    up = [0.0] * n
    lo = [0.0] * n
    if n < period:
        return mid, up, lo
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / period
        sd = var ** 0.5
        mid[i] = m
        up[i]  = m + mult * sd
        lo[i]  = m - mult * sd
    return mid, up, lo


def _generate_scalp(
    candles: List[Candle], r: List[float], a: List[float], sc: ScalpConfig,
    session: Session, regime: Regime,
) -> Optional[GeneratedSignal]:
    """Range Mode — pure RSI mean-reversion for choppy markets.
      • RSI < sc.rsi_buy  → BUY  (confirmed by current bar closing bullish vs prev)
      • RSI > sc.rsi_sell → SELL (confirmed by current bar closing bearish vs prev)
      • SL = sc.sl_atr × ATR  (default 1.0× ATR)
      • TP = sc.tp_atr × ATR  (default 1.0× ATR)
    Lot is halved automatically at sizing time (see calc_lot mode='scalp').
    Bridge will force-close after `max_hold_minutes` regardless of P/L.
    """
    if len(candles) < max(sc.rsi_period + 2, 3):
        return None
    i = len(candles) - 1
    if a[i] <= 0:
        return None
    last = candles[i]
    prev = candles[i - 1]
    side: Optional[Side] = None
    confidence = 0.0
    reason = ""

    if r[i] < sc.rsi_buy and last["c"] > prev["c"]:
        side = "buy"
        # Deeper oversold + stronger bullish close = higher confidence
        depth = (sc.rsi_buy - r[i]) / sc.rsi_buy  # 0..1
        confidence = 0.55 + min(0.25, depth * 0.5)
        reason = f"Range-mode BUY · RSI {r[i]:.1f} < {sc.rsi_buy:.0f} (oversold mean-reversion)"
    elif r[i] > sc.rsi_sell and last["c"] < prev["c"]:
        side = "sell"
        depth = (r[i] - sc.rsi_sell) / (100 - sc.rsi_sell)
        confidence = 0.55 + min(0.25, depth * 0.5)
        reason = f"Range-mode SELL · RSI {r[i]:.1f} > {sc.rsi_sell:.0f} (overbought mean-reversion)"
    if side is None:
        return None

    # Session boost — Asia gets a small confidence penalty (lower lot already handled in calc_lot)
    if session in ("london", "new_york", "overlap"):
        confidence += 0.03
    elif session == "asia":
        confidence -= 0.04
    # session=="off" is hard-blocked earlier in generate_signal — never reached here
    confidence = max(0.0, min(0.99, confidence))
    if confidence < sc.min_confidence:
        return None

    entry = last["c"]
    sl_dist = a[i] * sc.sl_atr
    tp_dist = a[i] * sc.tp_atr
    sl = entry - sl_dist if side == "buy" else entry + sl_dist
    tp = entry + tp_dist if side == "buy" else entry - tp_dist
    return GeneratedSignal(
        side=side, entry=entry, sl=sl, tp=tp, confidence=confidence,
        regime=regime, session=session, reason=reason,
        mode="scalp", max_hold_minutes=sc.max_hold_minutes,
    )


def generate_signal(
    candles: List[Candle], cfg: StrategyConfig,
    scalp_cfg: Optional[ScalpConfig] = None,
    enable_scalping_in_ranges: bool = True,
) -> Optional[GeneratedSignal]:
    """Regime-aware router:
      • trending_up / trending_down → swing setups (EMA cross / pullback / continuation).
      • ranging                     → range-scalping (when enable_scalping_in_ranges is True).
      • volatile                    → stand by, no trades.
    Returns the same `GeneratedSignal` shape regardless, with a `mode` field set."""
    need = max(cfg.ema_slow, cfg.rsi_period, cfg.atr_period) + 5
    if len(candles) < need:
        return None
    closes = [c["c"] for c in candles]
    ef = ema(closes, cfg.ema_fast)
    es = ema(closes, cfg.ema_slow)
    r = rsi(closes, cfg.rsi_period)
    a = atr(candles, cfg.atr_period)
    session = current_session()
    regime = detect_regime(candles, ef, es, a)

    # Off session → no trading whatsoever (session-based hard block).
    if session == "off":
        return None
    # Volatile → stand-by, no trades regardless of toggles.
    if regime == "volatile":
        return None
    # Ranging → scalp if enabled, otherwise stand-by.
    if regime == "ranging":
        if not enable_scalping_in_ranges:
            return None
        return _generate_scalp(candles, r, a, scalp_cfg or ScalpConfig(), session, regime)
    # Trending → original swing path (in-line below for backwards-compat).
    return _generate_swing(candles, cfg, ef, es, r, a, session, regime)


def _generate_swing(
    candles: List[Candle], cfg: StrategyConfig,
    ef: List[float], es: List[float], r: List[float], a: List[float],
    session: Session, regime: Regime,
) -> Optional[GeneratedSignal]:
    i = len(candles) - 1
    last = candles[i]
    prev = candles[i - 1]

    ef_prev, es_prev = ef[i - 1], es[i - 1]
    ef_now, es_now = ef[i], es[i]
    bull_cross = ef_prev <= es_prev and ef_now > es_now
    bear_cross = ef_prev >= es_prev and ef_now < es_now
    above_slow = last["c"] > es_now
    below_slow = last["c"] < es_now

    side: Optional[Side] = None
    confidence = 0.0
    reason = ""

    # --- Setup 1: EMA cross (strongest) ---
    if bull_cross and above_slow and 45 < r[i] < 80 and regime != "trending_down":
        side = "buy"
        confidence = 0.6 + min(0.2, (r[i] - 50) / 100) + (0.15 if regime == "trending_up" else 0)
        reason = f"EMA({cfg.ema_fast})↑{cfg.ema_slow} cross · RSI {r[i]:.1f} · regime {regime}"
    elif bear_cross and below_slow and 20 < r[i] < 55 and regime != "trending_up":
        side = "sell"
        confidence = 0.6 + min(0.2, (50 - r[i]) / 100) + (0.15 if regime == "trending_down" else 0)
        reason = f"EMA({cfg.ema_fast})↓{cfg.ema_slow} cross · RSI {r[i]:.1f} · regime {regime}"

    # --- Setup 2: Pullback to EMA in trending regime ---
    if side is None:
        if regime == "trending_up" and above_slow and ef_now > es_now:
            dist = (last["c"] - ef_now) / (a[i] or 1e-9)
            if -0.5 <= dist <= 0.5 and 40 < r[i] < 70 and last["c"] >= prev["c"]:
                side = "buy"
                confidence = 0.55 + (0.05 if last["c"] > prev["c"] else 0)
                reason = f"Pullback to EMA{cfg.ema_fast} in uptrend · RSI {r[i]:.1f}"
        elif regime == "trending_down" and below_slow and ef_now < es_now:
            dist = (ef_now - last["c"]) / (a[i] or 1e-9)
            if -0.5 <= dist <= 0.5 and 30 < r[i] < 60 and last["c"] <= prev["c"]:
                side = "sell"
                confidence = 0.55 + (0.05 if last["c"] < prev["c"] else 0)
                reason = f"Pullback to EMA{cfg.ema_fast} in downtrend · RSI {r[i]:.1f}"

    # --- Setup 3: Trend continuation (new) — keeps signals flowing on directional markets ---
    if side is None:
        # short-trend: last 5 candles direction
        last5 = candles[-5:]
        up5 = sum(1 for k in range(1, 5) if last5[k]["c"] > last5[k - 1]["c"])
        down5 = sum(1 for k in range(1, 5) if last5[k]["c"] < last5[k - 1]["c"])
        ef_slope_5 = (ef[i] - ef[i - 5]) / 5 if i >= 5 else 0
        if ef_now > es_now and last["c"] > ef_now and r[i] > 50 and (up5 >= 3 or ef_slope_5 > 0):
            side = "buy"
            confidence = 0.5 + min(0.1, (r[i] - 50) / 200) + (0.05 if regime == "trending_up" else 0)
            reason = f"Trend-follow (EMA{cfg.ema_fast}>EMA{cfg.ema_slow}, close>EMA{cfg.ema_fast}) · RSI {r[i]:.1f}"
        elif ef_now < es_now and last["c"] < ef_now and r[i] < 50 and (down5 >= 3 or ef_slope_5 < 0):
            side = "sell"
            confidence = 0.5 + min(0.1, (50 - r[i]) / 200) + (0.05 if regime == "trending_down" else 0)
            reason = f"Trend-follow (EMA{cfg.ema_fast}<EMA{cfg.ema_slow}, close<EMA{cfg.ema_fast}) · RSI {r[i]:.1f}"

    # --- Setup 4: RSI extreme mean-reversion (ranging markets) ---
    if side is None and regime == "ranging":
        if r[i] < 32 and last["c"] > prev["c"]:
            side = "buy"
            confidence = 0.5 + (32 - r[i]) / 100
            reason = f"Mean-reversion (oversold, ranging) · RSI {r[i]:.1f}"
        elif r[i] > 68 and last["c"] < prev["c"]:
            side = "sell"
            confidence = 0.5 + (r[i] - 68) / 100
            reason = f"Mean-reversion (overbought, ranging) · RSI {r[i]:.1f}"

    # Setup 4 (RSI mean-reversion) is no longer reachable from this path because
    # ranging regimes are routed to `_generate_scalp`. The block above remains in
    # the file purely as historical reference — `regime == "ranging"` won't enter here.

    if side is None:
        return None

    if session == "overlap":
        confidence += 0.05
    elif session in ("london", "new_york"):
        confidence += 0.02
    elif session == "asia":
        confidence -= 0.05      # swing setups on Asia → slightly de-rated
    elif session == "off":
        confidence -= 0.1
    confidence = max(0.0, min(0.99, confidence))
    if confidence < cfg.min_confidence:
        return None

    entry = last["c"]
    sl_dist = a[i] * cfg.sl_atr
    # Enforce minimum 1:1 risk:reward — never risk more than we can win.
    tp_dist = max(a[i] * cfg.tp_atr, sl_dist)
    sl = entry - sl_dist if side == "buy" else entry + sl_dist
    tp = entry + tp_dist if side == "buy" else entry - tp_dist

    return GeneratedSignal(
        side=side, entry=entry, sl=sl, tp=tp, confidence=confidence,
        regime=regime, session=session, reason=reason,
        mode="swing", max_hold_minutes=0,
    )


# ---------- Position sizing ----------
def calc_lot(equity_usd: float, risk_pct: float, sl_distance: float, pair: str,
             session: Optional[Session] = None, mode: str = "swing") -> float:
    """Risk-based lot sizing.
      • Asia session    → 0.5× lot multiplier (thinner liquidity).
      • London/NY/Ovlap → normal (1.0×).
      • Off session     → never reached (engine hard-blocks it upstream).
      • Range / scalp mode → additional 0.5× multiplier (half normal risk).
    """
    risk_usd = equity_usd * (risk_pct / 100)
    if session == "asia":
        risk_usd *= 0.5
    if mode == "scalp":
        risk_usd *= 0.5
    if pair.startswith("XAU"):
        value_per_unit_per_lot = 100  # 1.0 lot = 100 oz, $1 move = $100
    elif pair.endswith("JPY"):
        value_per_unit_per_lot = 1000
    else:
        value_per_unit_per_lot = 100000
    denom = max(sl_distance * value_per_unit_per_lot, 1e-9)
    lots_raw = risk_usd / denom
    return max(0.01, min(5.0, math.floor(lots_raw * 100) / 100))
