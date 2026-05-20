"""
Aurum FX — Market data layer (MT5-native via bridge).

DESIGN:
  The MT5 bridge streams OHLC candles to /api/bridge-candles on every poll cycle.
  This module reads ONLY from `db.candles` — the broker's own data.
  If no candles exist for a (pair, timeframe), we return [] and the scanner
  records `data_unavailable` for that bot. NEVER synthetic.

All functions are async and connect to the same Motor client created in server.py.
"""
from __future__ import annotations
import logging
from typing import List, Optional
from engine import Candle

log = logging.getLogger("aurum.marketdata")

# Lazily injected by server.py at startup
_db = None


def init_db(db_handle) -> None:
    """Called from server.py startup to inject the Motor db handle."""
    global _db
    _db = db_handle


async def fetch_candles(pair: str, timeframe: str, count: int = 200) -> List[Candle]:
    """Read candles from db.candles. Newest-last (oldest→newest)."""
    if _db is None:
        log.warning("marketdata: db handle not initialised")
        return []
    cur = _db.candles.find(
        {"pair": pair.upper(), "timeframe": timeframe},
        {"_id": 0, "t": 1, "o": 1, "h": 1, "l": 1, "c": 1},
    ).sort("t", -1).limit(count)
    rows = await cur.to_list(count)
    if not rows:
        return []
    # rows came newest→oldest; reverse to oldest→newest
    rows.reverse()
    return [Candle(t=int(r["t"]), o=float(r["o"]), h=float(r["h"]),
                   l=float(r["l"]), c=float(r["c"])) for r in rows]


async def fetch_price(pair: str) -> Optional[float]:
    """Latest close from the most recent candle of any TF for this pair."""
    if _db is None:
        return None
    row = await _db.candles.find_one(
        {"pair": pair.upper()},
        {"_id": 0, "c": 1, "t": 1},
        sort=[("t", -1)],
    )
    return float(row["c"]) if row else None


async def store_candles(pair: str, timeframe: str, rows: List[dict]) -> int:
    """Upsert a batch of candles from the bridge. Returns count actually written.
    Each row must have keys: t (unix ms), o, h, l, c. Extra fields ignored.
    """
    if _db is None or not rows:
        return 0
    pair = pair.upper()
    written = 0
    for r in rows:
        try:
            doc = {
                "pair": pair, "timeframe": timeframe,
                "t": int(r["t"]),
                "o": float(r["o"]), "h": float(r["h"]),
                "l": float(r["l"]), "c": float(r["c"]),
            }
            await _db.candles.update_one(
                {"pair": pair, "timeframe": timeframe, "t": doc["t"]},
                {"$set": doc},
                upsert=True,
            )
            written += 1
        except Exception as e:
            log.warning("store_candles row failed: %s", e)
    return written
