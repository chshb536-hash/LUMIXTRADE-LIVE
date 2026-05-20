"""
Aurum FX — Real-time market data integrity validation.

Every candle batch passes through these checks BEFORE the strategy engine sees it.
A single failure marks the pair `data_unavailable` and blocks signal generation
for that pair until fresh, valid data arrives. We never silently fall back to
synthetic data — bad data = no trade. Period.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import statistics

from engine import Candle, atr as compute_atr


# Timeframe → minutes
TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""
    details: dict = field(default_factory=dict)

    def __bool__(self) -> bool:  # truthy = passed
        return self.ok


def _tf_minutes(tf: str) -> int:
    return TF_MINUTES.get(tf, 15)


def validate_candles(
    candles: List[Candle],
    timeframe: str,
    now_ms: int,
    *,
    max_stale_multiplier: float = 3.0,
    max_gap_multiplier: float = 5.0,
    abnormal_atr_multiplier: float = 5.0,
    min_candles: int = 60,
) -> ValidationResult:
    """Strict candle integrity check. Returns ValidationResult.

    Failures (in order):
      • insufficient candles (< min_candles)
      • non-monotonic timestamps
      • duplicate timestamps
      • zero/negative OHLC
      • OHLC inversion (h < l, h < max(o,c), l > min(o,c))
      • stale data (last candle older than max_stale_multiplier × timeframe)
      • abnormal gaps (consecutive bars > max_gap_multiplier × timeframe)
        — small 1-2 bar gaps are tolerated (broker holes are normal); only
          large gaps (> 5× tf) within a single session block trading.
      • abnormal ATR explosion (last bar TR > abnormal_atr_multiplier × median TR)
    """
    if not candles:
        return ValidationResult(False, "no_data")
    n = len(candles)
    if n < min_candles:
        return ValidationResult(False, "insufficient_data", {"count": n, "need": min_candles})

    tf_ms = _tf_minutes(timeframe) * 60_000

    # 1) OHLC sanity
    for i, c in enumerate(candles):
        if any(c[k] <= 0 for k in ("o", "h", "l", "c")):
            return ValidationResult(False, "non_positive_ohlc", {"index": i, "candle": dict(c)})
        if c["h"] < c["l"]:
            return ValidationResult(False, "h_below_l", {"index": i})
        if c["h"] < max(c["o"], c["c"]) - 1e-9 or c["l"] > min(c["o"], c["c"]) + 1e-9:
            return ValidationResult(False, "ohlc_inversion", {"index": i})

    # 2) Timestamp monotonicity + duplicates + gaps
    soft_gaps = 0
    for i in range(1, n):
        dt = candles[i]["t"] - candles[i - 1]["t"]
        if dt <= 0:
            return ValidationResult(False, "non_monotonic_timestamps" if dt < 0 else "duplicate_timestamp",
                                    {"index": i, "delta_ms": dt})
        if dt > tf_ms * max_gap_multiplier:
            # weekend/holiday gap is normal — skip if dt >= 36h (Sat 00:00 → Sun close)
            if dt < 36 * 3600 * 1000:
                return ValidationResult(False, "missing_candles",
                                        {"index": i, "gap_minutes": dt / 60_000,
                                         "tf_minutes": tf_ms / 60_000})
        elif dt > tf_ms * 2.0:
            # Tolerated small broker hole (1-2 bars). Count it for diagnostics
            # but do not block trading.
            soft_gaps += 1

    # 3) Staleness — last candle's *close* should be recent enough to act on
    last_t = candles[-1]["t"]
    age_ms = now_ms - last_t
    if age_ms > tf_ms * max_stale_multiplier and age_ms < 36 * 3600 * 1000:
        return ValidationResult(False, "stale_data", {"age_minutes": age_ms / 60_000})

    # 4) Abnormal ATR jump on the latest bar
    if n >= 20:
        trs: List[float] = []
        for i in range(1, n):
            p, c = candles[i - 1], candles[i]
            trs.append(max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])))
        last_tr = trs[-1]
        med_tr = statistics.median(trs[-21:-1]) if len(trs) >= 21 else statistics.median(trs[:-1])
        if med_tr > 0 and last_tr > med_tr * abnormal_atr_multiplier:
            return ValidationResult(False, "abnormal_atr_spike",
                                    {"last_tr": last_tr, "median_tr": med_tr,
                                     "ratio": last_tr / med_tr})

    return ValidationResult(True, "ok", {"count": n, "last_age_min": age_ms / 60_000,
                                          "soft_gaps": soft_gaps})


def validate_spread(
    spread: float, pair: str, *, recent_spreads: Optional[List[float]] = None,
) -> ValidationResult:
    """Reject execution if spread is abnormal.

    Hard caps (broker-typical):
      • XAU: 0.50 USD
      • XAG: 0.05 USD
      • JPY pairs: 3 pips
      • Others: 2 pips
    Dynamic check: spread > 3× recent median triggers a softer block.
    """
    pair = pair.upper()
    if spread <= 0:
        return ValidationResult(False, "invalid_spread", {"spread": spread})
    if pair.startswith("XAU"):
        cap = 0.50
    elif pair.startswith("XAG"):
        cap = 0.05
    elif pair.endswith("JPY"):
        cap = 0.03  # 3 pips on JPY = 0.03
    else:
        cap = 0.0002  # 2 pips on FX
    if spread > cap:
        return ValidationResult(False, "spread_over_cap",
                                {"spread": spread, "cap": cap, "pair": pair})
    if recent_spreads and len(recent_spreads) >= 10:
        med = statistics.median(recent_spreads)
        if med > 0 and spread > med * 3:
            return ValidationResult(False, "spread_spike",
                                    {"spread": spread, "median": med, "ratio": spread / med})
    return ValidationResult(True, "ok")
