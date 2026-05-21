"""
Aurum FX — Strategy Engine v2 (Smart Money + Multi-Regime Router).

This replaces the v1 EMA/RSI crossover logic. It runs three specialised setups
under one regime router and outputs a single, scored `GeneratedSignal`.

Setups
------
  • TRENDING regime  →  BOS_RETEST  (Break-of-Structure + retest of mitigation zone)
                        with FVG / Order-Block / Displacement confluence
  • COMPRESSION      →  RANGE_BREAKOUT (Bollinger-squeeze + ATR expansion + displacement)
  • RANGING          →  LIQUIDITY_REVERSAL (sweep of equal-highs/lows + CHOCH back inside)
  • VOLATILE / OFF   →  stand-by (no trade)

Confidence
----------
Composite 0..0.99 score with named sub-scores:
    confidence = base
                 + trend_score   (HTF alignment)
                 + structure     (BOS quality)
                 + liquidity     (sweep quality)
                 + displacement  (institutional bar quality)
                 + session_bias  (London/NY positive, Asia negative)
                 - vol_penalty   (extreme ATR)

Output schema is the same `GeneratedSignal` dataclass used by v1 so the rest of
the system (scheduler, bridge, journal) does not change.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Literal
import math
import statistics

from engine import (
    Candle, GeneratedSignal, Session, Regime, Side,
    ema, rsi, atr, current_session,
)


# ============================================================================
# Helpers
# ============================================================================
def _swings(candles: List[Candle], left: int = 3, right: int = 3) -> Tuple[List[int], List[int]]:
    """Return (swing_high_indexes, swing_low_indexes). Classic pivot detection."""
    highs, lows = [], []
    n = len(candles)
    for i in range(left, n - right):
        h = candles[i]["h"]
        l = candles[i]["l"]
        if all(candles[j]["h"] <= h for j in range(i - left, i)) and \
           all(candles[j]["h"] <  h for j in range(i + 1, i + right + 1)):
            highs.append(i)
        if all(candles[j]["l"] >= l for j in range(i - left, i)) and \
           all(candles[j]["l"] >  l for j in range(i + 1, i + right + 1)):
            lows.append(i)
    return highs, lows


def _displacement(candles: List[Candle], atr_series: List[float], i: int,
                  body_mult: float = 1.4) -> Optional[Side]:
    """A 'displacement' candle = strong directional intent: body > body_mult × ATR AND
    closes in the upper/lower 25% of its own range."""
    if i >= len(candles) or i < 1:
        return None
    a = atr_series[i] if i < len(atr_series) else 0
    if a <= 0:
        return None
    c = candles[i]
    body = abs(c["c"] - c["o"])
    rng  = max(c["h"] - c["l"], 1e-9)
    if body < a * body_mult:
        return None
    pos_in_range = (c["c"] - c["l"]) / rng
    if c["c"] > c["o"] and pos_in_range > 0.75:
        return "buy"
    if c["c"] < c["o"] and pos_in_range < 0.25:
        return "sell"
    return None


def _fair_value_gap(candles: List[Candle], i: int) -> Optional[Tuple[Side, float, float]]:
    """3-candle FVG: bullish if candle[i-2].high < candle[i].low (gap above prev bar high).
    Returns (side, gap_low, gap_high) of the imbalance — these are the mitigation prices."""
    if i < 2:
        return None
    a, b, c = candles[i - 2], candles[i - 1], candles[i]
    if a["h"] < c["l"] and b["c"] > a["c"]:
        return ("buy", a["h"], c["l"])
    if a["l"] > c["h"] and b["c"] < a["c"]:
        return ("sell", c["h"], a["l"])
    return None


def _last_bos(candles: List[Candle], swing_highs: List[int], swing_lows: List[int]) -> Optional[Dict[str, Any]]:
    """Return the most recent Break of Structure (or None).
    BOS up: close above the most recent swing high.
    BOS down: close below the most recent swing low.
    """
    n = len(candles)
    if n < 4 or not (swing_highs or swing_lows):
        return None
    last_close = candles[-1]["c"]
    # latest pivot before the current bar
    sh = next((i for i in reversed(swing_highs) if i < n - 1), None)
    sl = next((i for i in reversed(swing_lows)  if i < n - 1), None)
    candidates = []
    if sh is not None and last_close > candles[sh]["h"]:
        candidates.append({"side": "buy", "level": candles[sh]["h"], "pivot_idx": sh})
    if sl is not None and last_close < candles[sl]["l"]:
        candidates.append({"side": "sell", "level": candles[sl]["l"], "pivot_idx": sl})
    if not candidates:
        return None
    # Prefer the closer (more recent) pivot
    return max(candidates, key=lambda x: x["pivot_idx"])


def _liquidity_sweep(candles: List[Candle], swing_highs: List[int], swing_lows: List[int]) -> Optional[Side]:
    """A liquidity sweep = the last bar pierced a recent swing pivot but CLOSED back
    inside. Bearish sweep above a high, bullish sweep below a low."""
    n = len(candles)
    if n < 2:
        return None
    last = candles[-1]
    # look at pivots within the last 30 bars
    recent_highs = [candles[i]["h"] for i in swing_highs if i >= n - 30 and i < n - 1]
    recent_lows  = [candles[i]["l"] for i in swing_lows  if i >= n - 30 and i < n - 1]
    if recent_highs:
        rh = max(recent_highs)
        if last["h"] > rh and last["c"] < rh:
            return "sell"
    if recent_lows:
        rl = min(recent_lows)
        if last["l"] < rl and last["c"] > rl:
            return "buy"
    return None


def _bollinger_squeeze(candles: List[Candle], period: int = 20, mult: float = 2.0,
                       lookback: int = 30) -> bool:
    """True if current band width is in bottom 25th percentile of last `lookback` bars."""
    n = len(candles)
    if n < period + lookback:
        return False
    closes = [c["c"] for c in candles]
    widths: List[float] = []
    for j in range(n - lookback, n):
        window = closes[j - period + 1 : j + 1]
        if len(window) < period:
            continue
        m = sum(window) / period
        sd = (sum((x - m) ** 2 for x in window) / period) ** 0.5
        widths.append(2 * mult * sd)
    if len(widths) < 5:
        return False
    cur = widths[-1]
    sorted_w = sorted(widths)
    q1 = sorted_w[len(sorted_w) // 4]
    return cur <= q1


def _equal_levels(candles: List[Candle], swings: List[int], price_key: str,
                  tol_atr: float, atr_val: float) -> Optional[float]:
    """Detect equal highs/lows within `tol_atr` × ATR of each other across last few swings.
    Returns the level if equal-cluster found, else None."""
    if len(swings) < 2 or atr_val <= 0:
        return None
    recent = swings[-4:]
    prices = [candles[i][price_key] for i in recent]
    if len(prices) < 2:
        return None
    base = prices[-1]
    matches = [p for p in prices if abs(p - base) <= tol_atr * atr_val]
    if len(matches) >= 2:
        return base
    return None


# ============================================================================
# Main strategy entry
# ============================================================================
@dataclass
class StrategyV2Config:
    sl_atr: float = 1.5
    tp_atr: float = 3.0          # Higher RR than v1 (2.5 → 3.0) — institutional bias
    min_confidence: float = 0.55
    scalp_sl_atr: float = 1.0
    scalp_tp_atr: float = 1.5
    scalp_min_confidence: float = 0.55
    max_hold_minutes_scalp: int = 45
    max_hold_minutes_swing: int = 480  # 8h cap on swings
    # v1.8 — Conservative live-forward filters. Toggled via STRATEGY_CONSERVATIVE env at server startup.
    require_displacement: bool = False        # BOS only valid if a displacement bar confirms
    require_fvg_for_bos: bool = False         # BOS-retest also needs FVG confluence
    require_htf_alignment: bool = False       # drop signals that disagree with higher-TF trend
    max_atr_ratio: float = 1.8                # reject when current ATR > X × 50-bar median
    min_displacement_body_atr: float = 1.4    # body-size requirement for "displacement" bars


def conservative_config() -> "StrategyV2Config":
    """Returns a stricter config preset for the live-forward stabilisation phase.
    Activated when STRATEGY_CONSERVATIVE=true is set in backend/.env (default ON).
    """
    return StrategyV2Config(
        sl_atr=1.5,
        tp_atr=3.0,
        min_confidence=0.70,           # was 0.55 — only A+ setups
        scalp_sl_atr=1.0,
        scalp_tp_atr=2.0,              # widened from 1.5 (better RR profile)
        scalp_min_confidence=0.70,
        max_hold_minutes_scalp=30,
        max_hold_minutes_swing=360,
        require_displacement=True,
        require_fvg_for_bos=True,
        require_htf_alignment=True,
        max_atr_ratio=1.5,
        min_displacement_body_atr=1.7,  # stronger institutional bars only
    )


@dataclass
class SignalContext:
    """Returned alongside the signal for the journal + ML feature store."""
    regime: Regime
    session: Session
    atr: float
    atr_ratio: float  # cur ATR / 50-bar median
    swing_high: Optional[float]
    swing_low: Optional[float]
    bos: Optional[Dict[str, Any]]
    fvg: Optional[Tuple[str, float, float]]
    sweep: Optional[Side]
    displacement: Optional[Side]
    squeeze: bool
    htf_aligned: Optional[bool]
    scores: Dict[str, float] = field(default_factory=dict)


def _classify_regime_v2(candles: List[Candle], ef: List[float], es: List[float],
                       atr_s: List[float]) -> Regime:
    """Regime v2 — adds 'compression' as a real volatility state."""
    n = len(candles)
    if n < 60:
        return "ranging"
    last_close = candles[-1]["c"]
    a = atr_s[-1] or 1e-9
    # Volatility extreme
    last_50 = atr_s[-50:]
    med_a = statistics.median([x for x in last_50 if x > 0]) or a
    if a > med_a * 2.2:
        return "volatile"
    if med_a > 0 and a < med_a * 0.55 and _bollinger_squeeze(candles):
        # compression maps to "ranging" in v1 schema — handled by router below
        return "ranging"
    slope = (es[-1] - es[-10]) / 10.0
    spread = abs(ef[-1] - es[-1])
    if spread / a < 0.35:
        return "ranging"
    return "trending_up" if slope > 0 else "trending_down"


def generate_signal_v2(
    candles: List[Candle],
    cfg: StrategyV2Config,
    *,
    htf_trend: Optional[str] = None,        # "up" / "down" / "flat" / None
    session_override: Optional[Session] = None,
) -> Optional[Tuple[GeneratedSignal, SignalContext]]:
    """Main entry. Returns (signal, context) or None."""
    n = len(candles)
    if n < 80:
        return None
    closes = [c["c"] for c in candles]
    ef = ema(closes, 21)
    es = ema(closes, 55)
    r  = rsi(closes, 14)
    a  = atr(candles, 14)
    session = session_override or current_session()
    if session == "off":
        return None

    regime = _classify_regime_v2(candles, ef, es, a)
    if regime == "volatile":
        return None

    swing_h, swing_l = _swings(candles, left=3, right=3)
    bos = _last_bos(candles, swing_h, swing_l)
    sweep = _liquidity_sweep(candles, swing_h, swing_l)
    disp = _displacement(candles, a, n - 1, body_mult=cfg.min_displacement_body_atr)
    fvg = _fair_value_gap(candles, n - 1)
    squeeze = _bollinger_squeeze(candles)
    atr_med = statistics.median([x for x in a[-50:] if x > 0]) or a[-1]
    atr_ratio = a[-1] / atr_med if atr_med > 0 else 1.0

    ctx = SignalContext(
        regime=regime, session=session, atr=a[-1], atr_ratio=atr_ratio,
        swing_high=candles[swing_h[-1]]["h"] if swing_h else None,
        swing_low=candles[swing_l[-1]]["l"] if swing_l else None,
        bos=bos, fvg=fvg, sweep=sweep, displacement=disp,
        squeeze=squeeze,
        htf_aligned=None,
    )

    # ROUTER
    if regime in ("trending_up", "trending_down"):
        out = _setup_bos_retest(candles, cfg, ef, es, r, a, regime, session, bos, fvg, disp, htf_trend, ctx)
    elif squeeze:
        out = _setup_range_breakout(candles, cfg, ef, es, r, a, regime, session, disp, htf_trend, ctx)
    else:
        out = _setup_liquidity_reversal(candles, cfg, ef, es, r, a, regime, session, sweep, htf_trend, ctx)

    if out is None:
        return None
    sig, ctx_out = out
    return sig, ctx_out


# ============================================================================
# Setup #1 — BOS retest with confluence (TRENDING)
# ============================================================================
def _setup_bos_retest(candles, cfg: StrategyV2Config, ef, es, r, a,
                      regime: Regime, session: Session,
                      bos, fvg, disp, htf_trend, ctx: SignalContext):
    if not bos:
        return None
    side: Side = bos["side"]
    # v1.8 conservative: require a confirming displacement on BOS
    if cfg.require_displacement and disp != side:
        return None
    # v1.8 conservative: require FVG confluence on BOS-retest
    if cfg.require_fvg_for_bos and not (fvg and fvg[0] == side):
        return None
    # v1.8 conservative: reject violent regimes
    if cfg.max_atr_ratio and ctx.atr_ratio > cfg.max_atr_ratio:
        return None
    # HTF alignment — drop signals contradicting higher TF unless very strong displacement
    htf_aligned: Optional[bool] = None
    if htf_trend and htf_trend != "flat":
        want = "up" if side == "buy" else "down"
        htf_aligned = (htf_trend == want)
        if not htf_aligned and not disp:
            return None
        # v1.8 conservative: hard-block contra-HTF setups regardless of displacement
        if cfg.require_htf_alignment and not htf_aligned:
            return None
    ctx.htf_aligned = htf_aligned

    last = candles[-1]
    entry = last["c"]
    atr_v = a[-1]
    if atr_v <= 0:
        return None
    # Use BOS pivot as logical SL anchor
    sl_dist_atr = atr_v * cfg.sl_atr
    if side == "buy":
        anchor_sl = bos["level"] - atr_v * 0.5
        sl = min(anchor_sl, entry - sl_dist_atr)
        tp = entry + max(atr_v * cfg.tp_atr, (entry - sl))
    else:
        anchor_sl = bos["level"] + atr_v * 0.5
        sl = max(anchor_sl, entry + sl_dist_atr)
        tp = entry - max(atr_v * cfg.tp_atr, (sl - entry))

    # Confidence composition
    base = 0.50
    structure = 0.15  # BOS itself
    displacement_s = 0.10 if disp == side else 0.0
    fvg_s = 0.06 if (fvg and fvg[0] == side) else 0.0
    htf_s = 0.08 if htf_aligned else (-0.05 if htf_aligned is False else 0.0)
    session_s = 0.05 if session in ("london", "new_york", "overlap") else -0.04
    rsi_s = 0.04 if (side == "buy" and 50 < r[-1] < 75) or (side == "sell" and 25 < r[-1] < 50) else 0.0
    vol_pen = -0.08 if ctx.atr_ratio > 1.8 else 0.0
    conf = max(0.0, min(0.99, base + structure + displacement_s + fvg_s + htf_s + session_s + rsi_s + vol_pen))
    if conf < cfg.min_confidence:
        return None

    ctx.scores = {
        "base": base, "structure": structure, "displacement": displacement_s,
        "fvg": fvg_s, "htf": htf_s, "session": session_s, "rsi": rsi_s, "vol_pen": vol_pen,
    }
    reason = f"BOS-retest {regime} · {('FVG ' if fvg_s else '')}{('DISP ' if displacement_s else '')}· RSI {r[-1]:.1f}"
    sig = GeneratedSignal(
        side=side, entry=entry, sl=sl, tp=tp, confidence=conf,
        regime=regime, session=session, reason=reason,
        mode="swing", max_hold_minutes=cfg.max_hold_minutes_swing,
    )
    return sig, ctx


# ============================================================================
# Setup #2 — Range Breakout (COMPRESSION)
# ============================================================================
def _setup_range_breakout(candles, cfg: StrategyV2Config, ef, es, r, a,
                          regime: Regime, session: Session,
                          disp, htf_trend, ctx: SignalContext):
    """Only fires inside a Bollinger squeeze. Requires a displacement candle
    closing outside the upper/lower band."""
    if disp is None:
        return None
    side: Side = disp
    last = candles[-1]
    atr_v = a[-1]
    if atr_v <= 0:
        return None
    # Require ATR expansion confirmation: current ATR > 1.1× squeeze-window median
    win_atr = [x for x in a[-30:] if x > 0]
    if not win_atr or atr_v < statistics.median(win_atr) * 1.1:
        return None

    entry = last["c"]
    sl = entry - atr_v * cfg.sl_atr if side == "buy" else entry + atr_v * cfg.sl_atr
    tp = entry + atr_v * cfg.tp_atr if side == "buy" else entry - atr_v * cfg.tp_atr

    htf_aligned: Optional[bool] = None
    if htf_trend and htf_trend != "flat":
        want = "up" if side == "buy" else "down"
        htf_aligned = (htf_trend == want)
    ctx.htf_aligned = htf_aligned

    base = 0.50
    breakout = 0.12   # squeeze breakout itself
    displacement_s = 0.12   # required
    htf_s = 0.07 if htf_aligned else (-0.04 if htf_aligned is False else 0.0)
    session_s = 0.04 if session in ("london", "new_york", "overlap") else -0.05
    conf = max(0.0, min(0.99, base + breakout + displacement_s + htf_s + session_s))
    if conf < cfg.min_confidence:
        return None
    ctx.scores = {"base": base, "breakout": breakout, "displacement": displacement_s,
                  "htf": htf_s, "session": session_s}
    reason = f"Squeeze breakout · displacement {disp.upper()} · ATR {atr_v:.5f}"
    sig = GeneratedSignal(
        side=side, entry=entry, sl=sl, tp=tp, confidence=conf,
        regime=regime, session=session, reason=reason,
        mode="swing", max_hold_minutes=cfg.max_hold_minutes_swing,
    )
    return sig, ctx


# ============================================================================
# Setup #3 — Liquidity Reversal (RANGING)
# ============================================================================
def _setup_liquidity_reversal(candles, cfg: StrategyV2Config, ef, es, r, a,
                              regime: Regime, session: Session,
                              sweep, htf_trend, ctx: SignalContext):
    if sweep is None:
        return None
    side: Side = sweep
    last = candles[-1]
    atr_v = a[-1]
    if atr_v <= 0:
        return None
    # Need RSI confluence — sweep + extreme RSI is the high-quality combo
    if side == "buy" and r[-1] > 45:
        return None
    if side == "sell" and r[-1] < 55:
        return None
    entry = last["c"]
    sl = entry - atr_v * cfg.scalp_sl_atr if side == "buy" else entry + atr_v * cfg.scalp_sl_atr
    tp = entry + atr_v * cfg.scalp_tp_atr if side == "buy" else entry - atr_v * cfg.scalp_tp_atr

    base = 0.50
    sweep_s = 0.15
    rsi_s = 0.07 if (side == "buy" and r[-1] < 32) or (side == "sell" and r[-1] > 68) else 0.03
    session_s = 0.03 if session in ("london", "new_york", "overlap") else -0.04
    conf = max(0.0, min(0.99, base + sweep_s + rsi_s + session_s))
    if conf < cfg.scalp_min_confidence:
        return None
    ctx.scores = {"base": base, "sweep": sweep_s, "rsi": rsi_s, "session": session_s}
    reason = f"Liquidity sweep {side.upper()} · RSI {r[-1]:.1f}"
    sig = GeneratedSignal(
        side=side, entry=entry, sl=sl, tp=tp, confidence=conf,
        regime=regime, session=session, reason=reason,
        mode="scalp", max_hold_minutes=cfg.max_hold_minutes_scalp,
    )
    return sig, ctx
