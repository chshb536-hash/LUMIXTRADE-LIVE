"""
Aurum FX — Adaptive Risk Engine.

Heuristic-only (no ML — by user choice). All rules are mathematically grounded
and run as gates in the scanner BEFORE a signal is persisted.

Components
----------
  • adaptive_lot          — modulates base risk by drawdown / volatility / confidence / streak
  • spread_gate           — per-pair spread cap + dynamic spike check
  • volatility_gate       — ATR percentile circuit breaker
  • heartbeat_gate        — bridge offline detection
  • slippage_tracker      — rolling slippage stats; auto-blacklist after N high-slip fills
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import math
import statistics


# ============================================================================
# Adaptive position sizing
# ============================================================================
def adaptive_lot(
    *,
    equity_usd: float,
    base_risk_pct: float,
    sl_distance: float,
    pair: str,
    session: Optional[str],
    mode: str,
    confidence: float,            # 0..0.99
    drawdown_pct: float,          # current weekly drawdown
    atr_ratio: float,             # cur ATR / 50-bar median  (1.0 = normal)
    recent_win_rate: Optional[float] = None,  # last 20 trades, 0..1
) -> Dict[str, Any]:
    """Returns {'lot': float, 'risk_pct_used': float, 'multipliers': {...}}
    Math: risk_pct_used = base_risk_pct × dd_mult × vol_mult × conf_mult × streak_mult
          lot = risk_usd / (sl_distance × pip_value_per_lot), clamped & step-rounded.
    """
    # 1) Drawdown multiplier — taper risk in a losing week
    if drawdown_pct >= 10:
        dd_mult = 0.40
    elif drawdown_pct >= 6:
        dd_mult = 0.60
    elif drawdown_pct >= 3:
        dd_mult = 0.80
    else:
        dd_mult = 1.0

    # 2) Volatility multiplier — pull back when ATR is hot
    if atr_ratio >= 1.8:
        vol_mult = 0.5
    elif atr_ratio >= 1.4:
        vol_mult = 0.75
    elif atr_ratio <= 0.6:
        vol_mult = 0.85       # extremely quiet markets often whipsaw
    else:
        vol_mult = 1.0

    # 3) Confidence multiplier — scale risk by signal quality (0.6 → 1.2)
    #    conf=0.55 → ~0.65×, conf=0.99 → ~1.20×
    conf_mult = max(0.5, min(1.2, 0.55 + (confidence - 0.55) * 1.6))

    # 4) Recent-streak multiplier (anti-tilt)
    if recent_win_rate is None:
        streak_mult = 1.0
    elif recent_win_rate < 0.30:
        streak_mult = 0.7
    elif recent_win_rate > 0.65:
        streak_mult = 1.10
    else:
        streak_mult = 1.0

    # 5) Session / mode tweaks
    session_mult = 1.0
    if session == "asia":
        session_mult = 0.5
    mode_mult = 0.5 if mode == "scalp" else 1.0

    final_pct = base_risk_pct * dd_mult * vol_mult * conf_mult * streak_mult * session_mult * mode_mult

    risk_usd = equity_usd * (final_pct / 100.0)

    # Pip value per 1.0 lot (FX standard)
    p = pair.upper()
    if p.startswith("XAU"):
        value_per_unit_per_lot = 100.0       # gold: $1 move per 1.0 lot ≈ $100
    elif p.startswith("XAG"):
        value_per_unit_per_lot = 5000.0
    elif p.endswith("JPY"):
        value_per_unit_per_lot = 1000.0
    else:
        value_per_unit_per_lot = 100000.0

    denom = max(sl_distance * value_per_unit_per_lot, 1e-9)
    lots_raw = risk_usd / denom
    lot = max(0.01, min(5.0, math.floor(lots_raw * 100) / 100))

    # FIX #4 — Hard lot cap for small accounts (equity < $5k). The risk-normalization
    # math is technically correct but produces 0.20+ lots on tight-SL FX pairs which
    # is a 1-shot account-wipe waiting to happen. Cap until the account can absorb it.
    capped_by = None
    if equity_usd < 5_000:
        is_metal = p.startswith("XAU") or p.startswith("XAG")
        cap = 0.02 if is_metal else 0.05
        if lot > cap:
            capped_by = f"small_account_cap:{cap}"
            lot = cap

    return {
        "lot": lot,
        "risk_pct_used": round(final_pct, 4),
        "risk_usd": round(risk_usd, 2),
        "capped_by": capped_by,
        "multipliers": {
            "dd": round(dd_mult, 2), "vol": round(vol_mult, 2),
            "conf": round(conf_mult, 2), "streak": round(streak_mult, 2),
            "session": session_mult, "mode": mode_mult,
        },
    }


# ============================================================================
# Volatility gate (circuit breaker)
# ============================================================================
def volatility_gate(atr_series: List[float], *, hard_ratio: float = 2.5) -> Optional[str]:
    """Returns block-reason if current ATR is > hard_ratio × 50-bar median, else None."""
    if not atr_series or len(atr_series) < 30:
        return None
    recent = [x for x in atr_series[-50:] if x > 0]
    if len(recent) < 20:
        return None
    med = statistics.median(recent[:-1])
    cur = atr_series[-1]
    if med > 0 and cur > med * hard_ratio:
        return f"volatility_circuit:{cur / med:.2f}x_median"
    return None


# ============================================================================
# Slippage tracking
# ============================================================================
@dataclass
class SlippageStats:
    n: int = 0
    sum_abs: float = 0.0
    max_abs: float = 0.0
    high_count: int = 0  # number of fills with slippage > threshold
    blacklisted: bool = False


def update_slippage(stats: SlippageStats, pip_slippage: float,
                    threshold: float = 5.0, blacklist_after: int = 5) -> SlippageStats:
    stats.n += 1
    stats.sum_abs += abs(pip_slippage)
    stats.max_abs = max(stats.max_abs, abs(pip_slippage))
    if abs(pip_slippage) >= threshold:
        stats.high_count += 1
    if stats.high_count >= blacklist_after:
        stats.blacklisted = True
    return stats


# ============================================================================
# Heartbeat / bridge health
# ============================================================================
def bridge_health(last_seen_iso: Optional[str], *, offline_after_sec: int = 90) -> Dict[str, Any]:
    """Returns {'online': bool, 'last_seen_min': float|None, 'reason': str}."""
    if not last_seen_iso:
        return {"online": False, "last_seen_min": None, "reason": "never_connected"}
    try:
        last = datetime.fromisoformat(last_seen_iso.replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except Exception:
        return {"online": False, "last_seen_min": None, "reason": "bad_timestamp"}
    delta_sec = (datetime.now(timezone.utc) - last).total_seconds()
    online = delta_sec < offline_after_sec
    return {
        "online": online,
        "last_seen_min": round(delta_sec / 60.0, 1),
        "reason": "ok" if online else f"no_heartbeat_{int(delta_sec)}s",
    }
