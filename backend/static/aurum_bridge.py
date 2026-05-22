#!/usr/bin/env python3
"""
Aurum FX — MT5 Bridge (v1.5)
----------------------------
Connects your local MetaTrader 5 terminal to your Aurum FX account.

Setup (one time):
    pip install MetaTrader5 requests

Run:
    set AURUM_API_KEY=abk_xxxxxxxxxxxx
    set AURUM_API_URL=https://<your-aurumfx-host>/api
    set MT5_LOGIN=12345678
    set MT5_PASSWORD=your_mt5_password
    set MT5_SERVER=YourBroker-Server
    python aurum_bridge.py

The bridge polls Aurum every 5 seconds for new signals and executes them on MT5.
Closed trades and fills are reported back automatically. Your MT5 password never leaves this machine.

NEW IN v1.4:
  - Partial close at +1R: once floating profit reaches the initial risk (1R = USD risked
    on the trade based on initial SL distance and initial volume), the bridge closes
    AURUM_PARTIAL_CLOSE_FRACTION of the position (default 50%) and moves SL to break-even
    on the remainder. One-shot per ticket — never re-fires after.
  - Heartbeat now sends `version` to the server. Outdated bridges receive zero signals
    and a clear log warning.

v1.3 (still active):
  - Trailing stop (peak - distance lock, never moves backwards, respects stops_level)
  - Profit-lock close (close on drawdown from peak once peak >= MIN_PROFIT)
  - All trade-management runs only on magic 990077 positions, independent of polling.


Optional env vars (all have safe defaults):
  AURUM_TRAILING_ENABLED            (default true)
  AURUM_TRAILING_START_PROFIT       (default 30   — USD)
  AURUM_TRAILING_DISTANCE           (default 20   — USD; lock = peak - distance)
  AURUM_PROFIT_LOCK_ENABLED         (default true)
  AURUM_PROFIT_LOCK_DRAWDOWN_PERCENT (default 20  — %% drop from peak that triggers close)
  AURUM_PROFIT_LOCK_MIN_PROFIT      (default 50   — USD; only kicks in once peak reaches this)
  AURUM_TRAIL_MIN_INTERVAL          (default 8    — seconds between SL modifications)

NEW IN v1.1:
  - Auto-detects broker symbol suffix (XAUUSD -> XAUUSDm on Exness, EURUSD -> EURUSD.pro on FTMO, etc.)
  - Falls back to multiple filling modes (IOC -> FOK -> RETURN) so the order is never silently rejected.
  - Robust connection: reconnects MT5 on transient drops, surfaces broker error codes clearly.
"""
from __future__ import annotations
import os
import sys
import time
import signal
import logging
from typing import Any, Dict, List, Optional

try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERROR: MetaTrader5 package not installed. Run: pip install MetaTrader5")
    sys.exit(1)
import requests

# ----- config -----
BRIDGE_VERSION = "1.8.1"
API_KEY  = os.environ.get("AURUM_API_KEY")
API_URL  = (os.environ.get("AURUM_API_URL") or "").rstrip("/")
MT5_LOGIN    = os.environ.get("MT5_LOGIN")
MT5_PASSWORD = os.environ.get("MT5_PASSWORD")
MT5_SERVER   = os.environ.get("MT5_SERVER")
POLL_INTERVAL = float(os.environ.get("AURUM_POLL_INTERVAL", "5"))
# v1.8: persistent rotating file log next to the script. Configurable via env.
LOG_DIR  = os.environ.get("AURUM_LOG_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "aurum_bridge.log")
LOG_LEVEL = os.environ.get("AURUM_LOG_LEVEL", "INFO").upper()

# Root logger: stream to console + rotating file (10 MB × 5 backups).
os.makedirs(LOG_DIR, exist_ok=True)
from logging.handlers import RotatingFileHandler  # noqa: E402
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_root = logging.getLogger()
_root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
# Clean any pre-existing handlers so we don't double-log when supervisor restarts the process.
for _h in list(_root.handlers):
    _root.removeHandler(_h)
_sh = logging.StreamHandler(sys.stdout); _sh.setFormatter(_fmt); _root.addHandler(_sh)
_fh = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
_fh.setFormatter(_fmt); _root.addHandler(_fh)
log = logging.getLogger("aurum")

if not (API_KEY and API_URL and MT5_LOGIN and MT5_PASSWORD and MT5_SERVER):
    log.error("Missing required env vars. See file header for setup.")
    sys.exit(2)  # exit 2 = config error (do NOT auto-restart for this)

HEADERS = {"x-aurum-bridge-key": API_KEY, "Content-Type": "application/json"}
TRACKED_TICKETS: Dict[int, str] = {}        # ticket -> signal_id
SYMBOL_MAP: Dict[str, str] = {}              # base symbol (XAUUSD) -> broker symbol (XAUUSDm)
BRIDGE_START_TS = time.time()
# v1.8 — health counters/telemetry for heartbeat enrichment
LAST_CANDLES_PUSH_AT: float = 0.0
LAST_SIGNAL_RECEIVED_AT: float = 0.0
LAST_MT5_RECONNECT_AT: float = 0.0
MT5_RECONNECT_COUNT: int = 0
LAST_LOOP_ERROR: str = ""

# ---- v1.6: candle streaming + max-hold + spread/slippage protection ----
# Per-pair max spread (in PRICE units, not pips). 0 disables. Override via env: AURUM_MAX_SPREAD_XAUUSD=0.5
MAX_SPREAD_DEFAULTS = {
    "XAUUSD": 0.50, "XAGUSD": 0.05,
    "USDJPY": 0.03, "EURJPY": 0.03, "GBPJPY": 0.04, "AUDJPY": 0.03,
    # 2-pip cap on 5-digit FX (= 0.00020)
    "EURUSD": 0.00020, "GBPUSD": 0.00025, "AUDUSD": 0.00020,
    "USDCAD": 0.00025, "USDCHF": 0.00025, "NZDUSD": 0.00025,
}
MAX_SLIPPAGE_PIPS = float(os.environ.get("AURUM_MAX_SLIPPAGE_PIPS", "8"))   # blocks execution if expected slip > N pips
# How often to push OHLC candles to the server (seconds)
CANDLES_PUSH_INTERVAL = float(os.environ.get("AURUM_CANDLES_PUSH_INTERVAL", "60"))
CANDLES_PER_PAIR     = int(os.environ.get("AURUM_CANDLES_PER_PAIR", "200"))
# Pairs / timeframes to stream. v1.7: auto-discovered from the user's active bots via
# /api/bridge/stream-config every STREAM_CFG_INTERVAL seconds. Falls back to the static
# AURUM_STREAM_PAIRS env list ONLY when the endpoint is unreachable.
STREAM_PAIRS_RAW = os.environ.get("AURUM_STREAM_PAIRS", "XAUUSD:M15,EURUSD:M15,GBPUSD:M15,USDJPY:M15")
FALLBACK_STREAM_PAIRS: List[tuple] = [tuple(s.strip().split(":")) for s in STREAM_PAIRS_RAW.split(",") if ":" in s]
STREAM_PAIRS: List[tuple] = list(FALLBACK_STREAM_PAIRS)
STREAM_CFG_INTERVAL = float(os.environ.get("AURUM_STREAM_CFG_INTERVAL", "60"))
LAST_STREAM_CFG_FETCH: float = 0.0


def refresh_stream_pairs() -> None:
    """v1.7: Pull the active (pair, timeframe) list from the server every 60s.
    Bridge stays in sync with bot creation/deletion without VPS reconfig.
    Silently keeps the previous list on transient failures."""
    global STREAM_PAIRS, LAST_STREAM_CFG_FETCH
    if time.time() - LAST_STREAM_CFG_FETCH < STREAM_CFG_INTERVAL:
        return
    LAST_STREAM_CFG_FETCH = time.time()
    try:
        r = requests.get(f"{API_URL}/bridge/stream-config",
                         headers=HEADERS, timeout=10)
        if r.status_code != 200:
            log.warning("stream-config HTTP %s — keeping previous list (%d pairs)",
                        r.status_code, len(STREAM_PAIRS))
            return
        data = r.json() or {}
        items = data.get("pairs") or []
        if not items:
            # No active bots — fall back to env list so the bridge doesn't go silent.
            STREAM_PAIRS = list(FALLBACK_STREAM_PAIRS)
            log.info("stream-config: no active bots — using env fallback (%d pairs)",
                     len(STREAM_PAIRS))
            return
        new_list = [(it["pair"].upper(), it["timeframe"].upper())
                    for it in items if it.get("pair") and it.get("timeframe")]
        if new_list and new_list != STREAM_PAIRS:
            log.info("stream-config: refreshed — streaming %d (pair, tf) combos: %s",
                     len(new_list),
                     ", ".join(f"{p}:{tf}" for (p, tf) in new_list))
            STREAM_PAIRS = new_list
    except Exception as e:
        log.warning("stream-config fetch failed: %s — keeping previous list", e)
LAST_CANDLES_PUSH: float = 0.0
OPEN_TICKET_OPENED_AT: Dict[int, float] = {}   # ticket -> unix ts when first seen
TICKET_MAX_HOLD: Dict[int, int] = {}            # ticket -> max_hold_minutes (from signal)
SIGNAL_MAX_HOLD: Dict[str, int] = {}            # signal_id -> max_hold_minutes (carry-over)
# Pip size helpers
def _pip_size(symbol_info) -> float:
    """Returns the 'pip' (not point) for slippage math. JPY pairs: 0.01. FX: 0.0001. Metals: 0.1."""
    if not symbol_info:
        return 0.0001
    nm = (symbol_info.name or "").upper()
    if "JPY" in nm:
        return 0.01
    if "XAU" in nm or "XAG" in nm:
        return 0.1
    return 0.0001

# ----- Trade management (independent of execution / polling) -----
def _bool_env(k: str, default: bool) -> bool:
    v = os.environ.get(k)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

# Trailing stop: once profit reaches START, lock floor of (peak - DISTANCE)
TRAILING_ENABLED       = _bool_env("AURUM_TRAILING_ENABLED", True)
TRAILING_START_PROFIT  = float(os.environ.get("AURUM_TRAILING_START_PROFIT", "30"))   # USD
TRAILING_DISTANCE      = float(os.environ.get("AURUM_TRAILING_DISTANCE", "20"))       # USD
# Profit-lock close: if peak >= MIN_PROFIT and profit drops by DRAWDOWN_PCT from peak, close
PROFIT_LOCK_ENABLED          = _bool_env("AURUM_PROFIT_LOCK_ENABLED", True)
PROFIT_LOCK_DRAWDOWN_PERCENT = float(os.environ.get("AURUM_PROFIT_LOCK_DRAWDOWN_PERCENT", "20"))
PROFIT_LOCK_MIN_PROFIT       = float(os.environ.get("AURUM_PROFIT_LOCK_MIN_PROFIT", "50"))
# Throttle SL modifications (avoid spamming MT5)
TRAIL_MIN_INTERVAL_SEC = float(os.environ.get("AURUM_TRAIL_MIN_INTERVAL", "8"))

# Partial close at +1R (one-shot per ticket): close FRACTION of volume and move SL to break-even
PARTIAL_CLOSE_ENABLED  = _bool_env("AURUM_PARTIAL_CLOSE_ENABLED", True)
PARTIAL_CLOSE_FRACTION = max(0.05, min(0.95, float(os.environ.get("AURUM_PARTIAL_CLOSE_FRACTION", "0.5"))))

PEAK_PROFITS: Dict[int, float] = {}         # ticket -> peak floating profit ever observed
LAST_TRAIL_TS: Dict[int, float] = {}         # ticket -> last trail-modify timestamp
CLOSING_TICKETS: set = set()                 # tickets we just sent a close for (one-shot guard)
# Partial-close state — cached on first valid observation, single-fire
INITIAL_RISK_USD: Dict[int, float] = {}      # ticket -> 1R in USD computed at open
INITIAL_OPEN_PRICE: Dict[int, float] = {}    # ticket -> open price
PARTIAL_DONE: set = set()                    # tickets that have had their +1R partial close
# Phase-1 (v1.8.1) state
BE_DONE: set = set()                         # tickets that have had their +0.5R BE SL move
CLOSE_REASONS: Dict[int, str] = {}            # ticket -> reason string (max_hold, profit_lock, trail, manual)
# Phase-1: default max_hold for legacy tickets observed at startup
DEFAULT_MAX_HOLD_MIN = int(os.environ.get("AURUM_DEFAULT_MAX_HOLD_MIN", "480"))


def mt5_init() -> bool:
    """Initialize MT5 connection. Returns True on success.
    v1.8: structured error logs so the watchdog can decide whether to restart."""
    if not mt5.initialize(login=int(MT5_LOGIN), password=MT5_PASSWORD, server=MT5_SERVER):
        log.error("MT5 init failed: %s", mt5.last_error())
        return False
    info = mt5.account_info()
    if info is None:
        log.error("MT5 account_info() returned None: %s", mt5.last_error())
        return False
    log.info("Connected to MT5 #%s on %s · balance %s %s · equity %s",
             info.login, info.server, info.balance, info.currency, info.equity)
    return True


def mt5_is_healthy() -> bool:
    """v1.8: best-effort liveness check on the MT5 terminal connection.
    Considered unhealthy if terminal_info or account_info return None, or if the
    terminal reports `connected=False` (broker disconnect / weekend gap)."""
    try:
        ti = mt5.terminal_info()
        if ti is None:
            return False
        if hasattr(ti, "connected") and not ti.connected:
            return False
        ai = mt5.account_info()
        if ai is None:
            return False
        return True
    except Exception as e:
        log.warning("mt5_is_healthy probe error: %s", e)
        return False


def mt5_reconnect_if_needed() -> bool:
    """v1.8: daemon that re-initializes MT5 with exponential backoff if the
    terminal is unhealthy. Capped at 60s between attempts. Updates telemetry
    counters for the heartbeat. Returns True if currently connected."""
    global LAST_MT5_RECONNECT_AT, MT5_RECONNECT_COUNT
    if mt5_is_healthy():
        return True
    log.warning("MT5 connection unhealthy — attempting reconnect…")
    backoff = 2.0
    for attempt in range(1, 6):  # ~ up to ~62s of attempts before bailing to watchdog
        try:
            mt5.shutdown()
        except Exception:
            pass
        time.sleep(backoff)
        if mt5_init():
            MT5_RECONNECT_COUNT += 1
            LAST_MT5_RECONNECT_AT = time.time()
            log.info("MT5 reconnected on attempt %d (total reconnects this session: %d)",
                     attempt, MT5_RECONNECT_COUNT)
            return True
        log.warning("MT5 reconnect attempt %d failed; backing off %.0fs", attempt, backoff)
        backoff = min(backoff * 2, 30.0)
    # Give up — let the watchdog (.bat loop) restart us cleanly.
    log.error("MT5 reconnect exhausted retries. Exiting so watchdog can restart the process.")
    return False


# ----- broker symbol auto-detection -----
def _norm(s: str) -> str:
    return "".join(c for c in s.upper() if c.isalnum())


def build_symbol_map() -> None:
    """Scan all symbols offered by the broker and map our base names (XAUUSD, EURUSD,
    USDJPY, ...) to whatever the broker actually calls them (XAUUSDm, EURUSD.pro,
    XAUUSD#, EURUSD-ZERO, etc.)."""
    SYMBOL_MAP.clear()
    bases = ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY",
             "AUDUSD", "USDCAD", "NZDUSD", "USDCHF", "EURGBP",
             "EURJPY", "GBPJPY", "AUDJPY"]
    all_syms = mt5.symbols_get() or []
    if not all_syms:
        log.warning("symbols_get returned 0 symbols — using base names as-is")
        for b in bases:
            SYMBOL_MAP[b] = b
        return
    # Build a normalized -> raw lookup, preferring shorter / more generic names.
    by_norm: Dict[str, List[str]] = {}
    for s in all_syms:
        by_norm.setdefault(_norm(s.name), []).append(s.name)

    for base in bases:
        nb = _norm(base)
        # 1) Exact match
        if nb in by_norm:
            SYMBOL_MAP[base] = sorted(by_norm[nb], key=len)[0]
            continue
        # 2) Symbol that starts with the base (covers XAUUSDm, XAUUSD.pro, XAUUSD-c, XAUUSD#, etc.)
        candidates: List[str] = []
        for s in all_syms:
            if _norm(s.name).startswith(nb):
                candidates.append(s.name)
        if candidates:
            # prefer ones that are exactly base + 1 suffix char (m, c, z, r) ahead of multi-char suffixes
            candidates.sort(key=lambda x: (len(_norm(x)) - len(nb), len(x)))
            SYMBOL_MAP[base] = candidates[0]
            continue
        # 3) Symbol that contains the base (covers prefixed names like .EURUSD, ZEROEURUSD)
        for s in all_syms:
            if nb in _norm(s.name):
                SYMBOL_MAP[base] = s.name
                break

    # Log the resolution map (only for symbols we actually found)
    if SYMBOL_MAP:
        log.info("Broker symbol map: %s",
                 ", ".join(f"{k}→{v}" for k, v in SYMBOL_MAP.items() if k != v) or "(no suffixes detected)")
    missing = [b for b in bases if b not in SYMBOL_MAP]
    if missing:
        log.warning("Could not resolve broker symbols for: %s", ", ".join(missing))


def resolve_symbol(pair: str) -> Optional[str]:
    """Return the broker-side symbol name for a base pair (e.g. XAUUSD -> XAUUSDm)."""
    if not SYMBOL_MAP:
        return pair  # haven't built the map yet
    if pair in SYMBOL_MAP:
        return SYMBOL_MAP[pair]
    # Last-chance: try a fresh lookup against current symbols
    nb = _norm(pair)
    for s in (mt5.symbols_get() or []):
        if _norm(s.name) == nb or _norm(s.name).startswith(nb):
            SYMBOL_MAP[pair] = s.name
            return s.name
    return None


def account_payload() -> Dict[str, Any]:
    a = mt5.account_info()
    if not a:
        return {}
    return {
        "login": a.login, "server": a.server, "broker": a.company,
        "currency": a.currency, "balance": a.balance, "equity": a.equity,
        "margin": a.margin, "free_margin": a.margin_free,
    }


def positions_payload() -> List[Dict[str, Any]]:
    """List of open Aurum-magic positions for live PnL streaming to the dashboard."""
    out: List[Dict[str, Any]] = []
    for p in (mt5.positions_get() or []):
        try:
            if int(p.magic) != 990077:
                continue
            out.append({
                "ticket": int(p.ticket),
                "symbol": p.symbol,
                "profit": float(p.profit),
                "swap": float(getattr(p, "swap", 0) or 0),
                "commission": float(getattr(p, "commission", 0) or 0),
                "price_current": float(p.price_current or 0),
                "sl": float(p.sl or 0),
                "tp": float(p.tp or 0),
            })
        except Exception:
            continue
    return out


def poll_signals() -> List[Dict[str, Any]]:
    global LAST_SIGNAL_RECEIVED_AT
    # v1.8 — enriched heartbeat: include terminal/account snapshot + telemetry counters
    ti = None
    try:
        ti = mt5.terminal_info()
    except Exception:
        ti = None
    body = {
        "account": account_payload(),
        "positions": positions_payload(),
        "version": BRIDGE_VERSION,
        "telemetry": {
            "uptime_sec": int(time.time() - BRIDGE_START_TS),
            "mt5_connected": bool(ti and getattr(ti, "connected", True)),
            "last_candles_push_at": LAST_CANDLES_PUSH_AT or None,
            "last_signal_received_at": LAST_SIGNAL_RECEIVED_AT or None,
            "mt5_reconnects": MT5_RECONNECT_COUNT,
            "last_mt5_reconnect_at": LAST_MT5_RECONNECT_AT or None,
            "last_loop_error": LAST_LOOP_ERROR or None,
            "streaming_pairs": [f"{p}:{tf}" for (p, tf) in STREAM_PAIRS],
        },
    }
    try:
        r = requests.post(f"{API_URL}/bridge-poll", headers=HEADERS, json=body, timeout=15)
        if r.status_code == 401:
            log.error("Bridge key rejected. Generate a new one in the dashboard.")
            return []
        r.raise_for_status()
        data = r.json() or {}
        warn = data.get("warning")
        if warn == "bridge_outdated":
            log.warning("OUTDATED BRIDGE · server requires >= %s · you are %s · %s",
                        data.get("min_version"), data.get("your_version"), data.get("message", ""))
        sigs = data.get("signals", [])
        if sigs:
            LAST_SIGNAL_RECEIVED_AT = time.time()
        return sigs
    except Exception as e:
        log.warning("poll failed: %s", e)
        return []


def report(event: str, payload: Dict[str, Any]) -> None:
    try:
        body = {"event": event, **payload}
        requests.post(f"{API_URL}/bridge-report", headers=HEADERS, json=body, timeout=15)
    except Exception as e:
        log.warning("report %s failed: %s", event, e)


# ----- order execution with broker-symbol resolution + filling-mode fallback -----
FILLING_MODES = (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN)


def execute(sig: Dict[str, Any]) -> None:
    base_pair = sig["pair"]
    side = sig["side"]
    lot  = float(sig["lot"])
    sig_entry = float(sig.get("entry") or 0)
    sig_sl   = float(sig["sl"])
    sig_tp   = float(sig["tp"])

    broker_sym = resolve_symbol(base_pair)
    if not broker_sym:
        log.warning("symbol %s not offered by this broker", base_pair)
        report("reject", {"signal_id": sig["id"], "reason": f"symbol {base_pair} not on broker"})
        return

    sym_info = mt5.symbol_info(broker_sym)
    if sym_info is None:
        log.warning("symbol_info(%s) returned None", broker_sym)
        report("reject", {"signal_id": sig["id"], "reason": f"{broker_sym} info missing"})
        return
    if not sym_info.visible:
        if not mt5.symbol_select(broker_sym, True):
            log.warning("symbol_select(%s) failed: %s", broker_sym, mt5.last_error())
            report("reject", {"signal_id": sig["id"], "reason": f"{broker_sym} not selectable"})
            return
        sym_info = mt5.symbol_info(broker_sym)  # refresh

    tick = mt5.symbol_info_tick(broker_sym)
    if not tick or (tick.ask == 0 and tick.bid == 0):
        report("reject", {"signal_id": sig["id"], "reason": f"{broker_sym} no tick data"})
        return

    # ---- v1.6 SPREAD PROTECTION ----
    spread_price = max(0.0, float(tick.ask) - float(tick.bid))
    max_spread = float(os.environ.get(f"AURUM_MAX_SPREAD_{base_pair}",
                                       str(MAX_SPREAD_DEFAULTS.get(base_pair, 0))))
    if max_spread > 0 and spread_price > max_spread:
        log.warning("SPREAD BLOCK %s · spread=%.5f > cap=%.5f", broker_sym, spread_price, max_spread)
        report("reject", {"signal_id": sig["id"],
                          "reason": f"spread_block:{spread_price:.5f}>{max_spread:.5f}"})
        return

    # Clamp lot to broker constraints
    vol_min  = float(getattr(sym_info, "volume_min", 0.01) or 0.01)
    vol_max  = float(getattr(sym_info, "volume_max", 100.0) or 100.0)
    vol_step = float(getattr(sym_info, "volume_step", 0.01) or 0.01)
    lot = max(vol_min, min(vol_max, round(round(lot / vol_step) * vol_step, 2)))

    point = float(getattr(sym_info, "point", 0.0) or 0.0) or 0.01
    digits = int(getattr(sym_info, "digits", 2) or 2)
    stops_level = int(getattr(sym_info, "trade_stops_level", 0) or 0)
    freeze_level = int(getattr(sym_info, "trade_freeze_level", 0) or 0)
    min_dist = max(stops_level, freeze_level) * point

    # Compute the signal's intended risk + reward distances (engine-side ATR multiples).
    # Re-anchor them to the CURRENT price so volatile symbols (XAU/JPY) don't get rejected
    # for "Invalid stops" when the market has moved a few candles since the signal fired.
    if side == "buy":
        price = tick.ask
        sl_dist = abs(sig_entry - sig_sl) if sig_entry else (price - sig_sl)
        tp_dist = abs(sig_tp - sig_entry) if sig_entry else (sig_tp - price)
        sl = price - sl_dist
        tp = price + tp_dist
        # Push outside broker's stops_level if needed
        if min_dist > 0:
            if (price - sl) < min_dist + point:
                sl = price - (min_dist + 5 * point)
            if (tp - price) < min_dist + point:
                tp = price + (min_dist + 5 * point)
        order_type = mt5.ORDER_TYPE_BUY
    else:
        price = tick.bid
        sl_dist = abs(sig_sl - sig_entry) if sig_entry else (sig_sl - price)
        tp_dist = abs(sig_entry - sig_tp) if sig_entry else (price - sig_tp)
        sl = price + sl_dist
        tp = price - tp_dist
        if min_dist > 0:
            if (sl - price) < min_dist + point:
                sl = price + (min_dist + 5 * point)
            if (price - tp) < min_dist + point:
                tp = price - (min_dist + 5 * point)
        order_type = mt5.ORDER_TYPE_SELL

    sl = round(sl, digits)
    tp = round(tp, digits)
    price = round(price, digits)

    base_request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": broker_sym,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 50,
        "magic": 990077,
        "comment": "AurumFX",
        "type_time": mt5.ORDER_TIME_GTC,
    }

    last_result = None
    for mode in FILLING_MODES:
        req = dict(base_request, type_filling=mode)
        last_result = mt5.order_send(req)
        if last_result is not None and last_result.retcode == mt5.TRADE_RETCODE_DONE:
            break
        rc = last_result.retcode if last_result else None
        # 10030 = unsupported filling mode -> retry next; 10016 = invalid stops -> retry once with wider buffer
        if rc == 10016:
            # widen one more time and retry
            buf = (min_dist + 20 * point) if min_dist > 0 else 20 * point
            if order_type == mt5.ORDER_TYPE_BUY:
                req["sl"] = round(price - max(sl_dist, buf), digits)
                req["tp"] = round(price + max(tp_dist, buf), digits)
            else:
                req["sl"] = round(price + max(sl_dist, buf), digits)
                req["tp"] = round(price - max(tp_dist, buf), digits)
            last_result = mt5.order_send(req)
            if last_result is not None and last_result.retcode == mt5.TRADE_RETCODE_DONE:
                sl = req["sl"]; tp = req["tp"]
                break
        if rc != 10030:
            break

    if last_result is None or last_result.retcode != mt5.TRADE_RETCODE_DONE:
        rc = last_result.retcode if last_result else None
        cmt = last_result.comment if last_result else "no result"
        log.warning("order rejected (retcode=%s) %s — sym=%s price=%s sl=%s tp=%s lot=%s",
                    rc, cmt, broker_sym, price, sl, tp, lot)
        report("reject", {"signal_id": sig["id"], "reason": f"retcode {rc}: {cmt}"})
        return

    log.info("FILL %s %s %s @ %s · SL %s · TP %s · ticket %s",
             side.upper(), lot, broker_sym, last_result.price, sl, tp, last_result.order)
    TRACKED_TICKETS[last_result.order] = sig["id"]
    # v1.6: remember max_hold_minutes (from signal) so we can force-close on expiry
    mhm = int(sig.get("max_hold_minutes") or 0)
    if mhm > 0:
        TICKET_MAX_HOLD[last_result.order] = mhm
        OPEN_TICKET_OPENED_AT[last_result.order] = time.time()
    # v1.6: compute slippage in pips for reporting + dashboard analytics
    pip = _pip_size(sym_info)
    slippage_pips = abs(float(last_result.price) - float(price)) / max(pip, 1e-9)
    report("fill", {
        "signal_id": sig["id"],
        "ticket": last_result.order,
        "pair": base_pair, "side": side, "lot": lot,
        "entry": last_result.price, "sl": sl, "tp": tp,
        # extra telemetry fields (server stores into trades — backward compatible)
        "slippage_pips": round(slippage_pips, 2),
        "spread_at_fill": round(spread_price, 6),
        "requested_price": price,
    })


def _calc_sl_for_target_profit(pos, target_usd: float) -> Optional[float]:
    """Compute the SL price that yields exactly target_usd in floating profit on `pos`.
    Returns None when current price = open (profit ratio undefined)."""
    price_now = float(pos.price_current or 0)
    price_open = float(pos.price_open or 0)
    if price_now == price_open:
        return None
    # USD per unit of price movement (signed for sells via pos.profit relationship)
    usd_per_price = float(pos.profit) / (price_now - price_open)
    if abs(usd_per_price) < 1e-9:
        return None
    return price_open + (target_usd / usd_per_price)


def _modify_sl(pos, new_sl: float, target_lock: float) -> bool:
    sym_info = mt5.symbol_info(pos.symbol)
    if sym_info is None:
        return False
    digits = int(getattr(sym_info, "digits", 2) or 2)
    point = float(getattr(sym_info, "point", 0.0) or 0.0) or 0.01
    stops_level = int(getattr(sym_info, "trade_stops_level", 0) or 0)
    min_dist = stops_level * point

    # Respect broker's stops_level — push outside if needed
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick:
        if pos.type == mt5.POSITION_TYPE_BUY:
            if (tick.bid - new_sl) < min_dist + point:
                new_sl = tick.bid - (min_dist + 5 * point)
        else:
            if (new_sl - tick.ask) < min_dist + point:
                new_sl = tick.ask + (min_dist + 5 * point)
    new_sl = round(new_sl, digits)

    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": int(pos.ticket),
        "symbol": pos.symbol,
        "sl": new_sl,
        "tp": float(pos.tp) if pos.tp else 0.0,
        "magic": 990077,
    }
    res = mt5.order_send(req)
    if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
        log.info("TRAIL ticket %s · SL %s -> %s · locks ~$%.2f",
                 pos.ticket, pos.sl, new_sl, target_lock)
        LAST_TRAIL_TS[pos.ticket] = time.time()
        return True
    rc = res.retcode if res else None
    log.debug("trail SL modify ticket=%s rc=%s sl=%s", pos.ticket, rc, new_sl)
    return False


def _close_position(pos, reason: str) -> bool:
    """Close a position via reverse market order with filling-mode fallback."""
    if int(pos.ticket) in CLOSING_TICKETS:
        return False  # already closing
    tick = mt5.symbol_info_tick(pos.symbol)
    if not tick:
        return False
    if pos.type == mt5.POSITION_TYPE_BUY:
        price, order_type = tick.bid, mt5.ORDER_TYPE_SELL
    else:
        price, order_type = tick.ask, mt5.ORDER_TYPE_BUY

    base = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": int(pos.ticket),
        "symbol": pos.symbol,
        "volume": float(pos.volume),
        "type": order_type,
        "price": price,
        "deviation": 50,
        "magic": 990077,
        "comment": "AurumLock",
        "type_time": mt5.ORDER_TIME_GTC,
    }
    CLOSING_TICKETS.add(int(pos.ticket))
    # v1.8.1: remember the *real* close reason so reconcile_closed() can ship it to the server.
    CLOSE_REASONS[int(pos.ticket)] = reason
    for mode in FILLING_MODES:
        req = dict(base, type_filling=mode)
        res = mt5.order_send(req)
        if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
            log.info("PROFIT-LOCK CLOSE ticket %s %s · %s", pos.ticket, pos.symbol, reason)
            return True
        rc = res.retcode if res else None
        if rc != 10030:
            log.warning("profit-lock close ticket %s failed: rc=%s", pos.ticket, rc)
            break
    # Failed — remove from set so a later attempt can retry
    CLOSING_TICKETS.discard(int(pos.ticket))
    return False


def _compute_initial_1r_usd(pos) -> Optional[float]:
    """Return the initial 1R risk in USD (open price vs initial SL × USD-per-price × volume).
    Returns None if the snapshot doesn't yet have a usable price delta to derive the conversion."""
    if not pos.sl or pos.volume == 0 or pos.price_current == pos.price_open:
        return None
    sign = 1.0 if pos.type == mt5.POSITION_TYPE_BUY else -1.0
    usd_per_price_per_lot = float(pos.profit) / (sign * (pos.price_current - pos.price_open) * pos.volume)
    if usd_per_price_per_lot <= 0:
        return None
    risk_price = abs(pos.price_open - pos.sl)
    return risk_price * usd_per_price_per_lot * pos.volume


def _partial_close(pos, fraction: float) -> bool:
    """Close `fraction` of `pos` via reverse market order. Respects min/step volume."""
    sym = mt5.symbol_info(pos.symbol)
    if sym is None:
        return False
    vol_min = float(getattr(sym, "volume_min", 0.01) or 0.01)
    vol_step = float(getattr(sym, "volume_step", 0.01) or 0.01)
    raw = float(pos.volume) * float(fraction)
    # Round DOWN to the nearest step, then clamp at min and never larger than current volume
    steps = max(1, int(raw / vol_step))
    vol = round(steps * vol_step, 8)
    if vol < vol_min or vol >= float(pos.volume):
        return False
    tick = mt5.symbol_info_tick(pos.symbol)
    if not tick:
        return False
    if pos.type == mt5.POSITION_TYPE_BUY:
        price, order_type = tick.bid, mt5.ORDER_TYPE_SELL
    else:
        price, order_type = tick.ask, mt5.ORDER_TYPE_BUY
    base = {
        "action":    mt5.TRADE_ACTION_DEAL,
        "position":  int(pos.ticket),
        "symbol":    pos.symbol,
        "volume":    vol,
        "type":      order_type,
        "price":     price,
        "deviation": 50,
        "magic":     990077,
        "comment":   "AurumTP1R",
        "type_time": mt5.ORDER_TIME_GTC,
    }
    for mode in FILLING_MODES:
        req = dict(base, type_filling=mode)
        res = mt5.order_send(req)
        if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
            log.info("PARTIAL-CLOSE +1R ticket %s · closed %.2f of %.2f lot · %s",
                     pos.ticket, vol, float(pos.volume), pos.symbol)
            return True
        rc = res.retcode if res else None
        if rc != 10030:
            log.warning("partial-close ticket %s failed rc=%s vol=%s", pos.ticket, rc, vol)
            break
    return False


def _move_sl_to_breakeven(pos, open_price: float) -> bool:
    """Set SL exactly at the open price (break-even) on the remainder."""
    sym_info = mt5.symbol_info(pos.symbol)
    if sym_info is None:
        return False
    digits = int(getattr(sym_info, "digits", 2) or 2)
    point = float(getattr(sym_info, "point", 0.0) or 0.0) or 0.01
    stops_level = int(getattr(sym_info, "trade_stops_level", 0) or 0)
    min_dist = stops_level * point
    new_sl = round(float(open_price), digits)
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick:
        if pos.type == mt5.POSITION_TYPE_BUY:
            if (tick.bid - new_sl) < min_dist + point:
                new_sl = round(tick.bid - (min_dist + 5 * point), digits)
        else:
            if (new_sl - tick.ask) < min_dist + point:
                new_sl = round(tick.ask + (min_dist + 5 * point), digits)
    req = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": int(pos.ticket),
        "symbol":   pos.symbol,
        "sl":       new_sl,
        "tp":       float(pos.tp) if pos.tp else 0.0,
        "magic":    990077,
    }
    res = mt5.order_send(req)
    if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
        log.info("BREAK-EVEN SL ticket %s · SL -> %s", pos.ticket, new_sl)
        return True
    log.warning("break-even SL modify ticket=%s rc=%s sl=%s",
                pos.ticket, res.retcode if res else None, new_sl)
    return False


def _force_close_expired() -> None:
    """v1.6: close positions whose max_hold_minutes has elapsed since fill (scalp setups).
    Phase-1 (v1.8.1): also handles legacy tickets (no per-ticket max_hold cached) using
    DEFAULT_MAX_HOLD_MIN. No more 13.5h XAU bleed trades after bridge restarts."""
    now = time.time()
    for pos in (mt5.positions_get() or []):
        try:
            if int(pos.magic) != 990077:
                continue
            t = int(pos.ticket)
            # Hydrate fallback values for legacy tickets that the bridge inherited.
            mhm = TICKET_MAX_HOLD.get(t)
            if not mhm:
                mhm = DEFAULT_MAX_HOLD_MIN
                TICKET_MAX_HOLD[t] = mhm
            opened = OPEN_TICKET_OPENED_AT.get(t)
            if opened is None:
                # Wasn't opened during this bridge session — use MT5's open time
                opened = float(getattr(pos, "time", 0) or 0)
                if opened > 0:
                    OPEN_TICKET_OPENED_AT[t] = opened
            if not opened:
                continue
            elapsed_min = (now - opened) / 60.0
            if elapsed_min >= mhm:
                log.info("MAX-HOLD CLOSE ticket %s · %.1f min >= %d min cap",
                         t, elapsed_min, mhm)
                _close_position(pos, f"max_hold {mhm}m")
        except Exception as e:
            log.warning("_force_close_expired ticket=%s error: %s",
                        getattr(pos, "ticket", "?"), e)


_MT5_TF = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


def push_candles() -> None:
    """v1.7: stream OHLC bars for the user's bot symbols to the server every CANDLES_PUSH_INTERVAL sec.
    Pair list is auto-refreshed from /api/bridge/stream-config (see refresh_stream_pairs)."""
    global LAST_CANDLES_PUSH, LAST_CANDLES_PUSH_AT
    # v1.7: keep STREAM_PAIRS in sync with the user's active bots on every tick.
    refresh_stream_pairs()
    if time.time() - LAST_CANDLES_PUSH < CANDLES_PUSH_INTERVAL:
        return
    LAST_CANDLES_PUSH = time.time()
    for base, tf in STREAM_PAIRS:
        try:
            broker_sym = resolve_symbol(base)
            if not broker_sym:
                continue
            mt_tf = _MT5_TF.get(tf.upper())
            if mt_tf is None:
                continue
            bars = mt5.copy_rates_from_pos(broker_sym, mt_tf, 0, CANDLES_PER_PAIR)
            if bars is None or len(bars) == 0:
                continue
            rows = [{
                "t": int(b["time"]) * 1000,
                "o": float(b["open"]), "h": float(b["high"]),
                "l": float(b["low"]),  "c": float(b["close"]),
            } for b in bars]
            try:
                requests.post(f"{API_URL}/bridge-candles",
                              headers=HEADERS,
                              json={"pair": base.upper(), "timeframe": tf.upper(), "rows": rows},
                              timeout=15)
                LAST_CANDLES_PUSH_AT = time.time()
            except Exception as e:
                log.warning("push_candles POST failed for %s/%s: %s", base, tf, e)
        except Exception as e:
            log.warning("push_candles loop error %s/%s: %s", base, tf, e)


def manage_open_positions() -> None:
    """Walk every Aurum-magic position and apply partial-close + trailing + profit-lock rules.
    Pure side-effect on broker; never touches signals, polling or order execution paths."""
    if not (TRAILING_ENABLED or PROFIT_LOCK_ENABLED or PARTIAL_CLOSE_ENABLED):
        return
    positions = mt5.positions_get() or []
    for pos in positions:
        try:
            if int(pos.magic) != 990077:
                continue
            ticket = int(pos.ticket)
            if ticket in CLOSING_TICKETS:
                continue
            current = float(pos.profit)
            peak = PEAK_PROFITS.get(ticket, current)
            if current > peak:
                peak = current
                PEAK_PROFITS[ticket] = peak

            # 0a) Phase-1 (v1.8.1): MOVE-SL-TO-BREAKEVEN at +0.5R (BEFORE 1R partial).
            # Reason: live-forward data showed many trades reaching ~50% to TP, then
            # reversing all the way to SL for a full loss (e.g. XAG +$5.75 MFE → -$8.35).
            # Moving SL to entry at half-R eliminates that class of loss entirely.
            if ticket not in BE_DONE:
                if ticket not in INITIAL_RISK_USD:
                    r = _compute_initial_1r_usd(pos)
                    if r and r > 0:
                        INITIAL_RISK_USD[ticket] = r
                        INITIAL_OPEN_PRICE[ticket] = float(pos.price_open)
                risk_usd = INITIAL_RISK_USD.get(ticket)
                if risk_usd and current >= risk_usd * 0.5:
                    if _move_sl_to_breakeven(pos, INITIAL_OPEN_PRICE[ticket]):
                        BE_DONE.add(ticket)
                        log.info("BE-MOVE ticket %s · profit $%.2f reached 0.5R ($%.2f) — SL → entry",
                                 ticket, current, risk_usd * 0.5)
                    # Don't continue — let the same cycle also evaluate trailing/partial.

            # 0) Partial close at +1R (one-shot per ticket). Compute initial 1R once,
            # then close FRACTION of volume and move SL to break-even on the remainder.
            if PARTIAL_CLOSE_ENABLED and ticket not in PARTIAL_DONE:
                if ticket not in INITIAL_RISK_USD:
                    r = _compute_initial_1r_usd(pos)
                    if r and r > 0:
                        INITIAL_RISK_USD[ticket] = r
                        INITIAL_OPEN_PRICE[ticket] = float(pos.price_open)
                else:
                    risk_usd = INITIAL_RISK_USD[ticket]
                    if current >= risk_usd:
                        if _partial_close(pos, PARTIAL_CLOSE_FRACTION):
                            PARTIAL_DONE.add(ticket)
                            # Re-fetch the position so we see the new (smaller) volume,
                            # then move SL to break-even on the remainder.
                            remaining = mt5.positions_get(ticket=ticket) or ()
                            if remaining:
                                _move_sl_to_breakeven(remaining[0], INITIAL_OPEN_PRICE[ticket])
                            continue  # done for this cycle — let next cycle reapply trail/lock

            # 1) Profit-lock: peak high enough but pulled back by DRAWDOWN_PERCENT — close.
            if (PROFIT_LOCK_ENABLED
                    and peak >= PROFIT_LOCK_MIN_PROFIT
                    and current > 0
                    and current <= peak * (1.0 - PROFIT_LOCK_DRAWDOWN_PERCENT / 100.0)):
                reason = f"peak ${peak:.2f} -> now ${current:.2f} ({PROFIT_LOCK_DRAWDOWN_PERCENT:.0f}% drawdown)"
                if _close_position(pos, reason):
                    PEAK_PROFITS.pop(ticket, None)
                continue  # closed, skip trailing

            # 2) Trailing stop: lock peak - TRAILING_DISTANCE once we're past START.
            if TRAILING_ENABLED and current >= TRAILING_START_PROFIT:
                # Throttle to avoid hammering MT5
                if time.time() - LAST_TRAIL_TS.get(ticket, 0.0) < TRAIL_MIN_INTERVAL_SEC:
                    continue
                target_lock = max(0.0, peak - TRAILING_DISTANCE)
                if target_lock <= 0:
                    continue
                proposed_sl = _calc_sl_for_target_profit(pos, target_lock)
                if proposed_sl is None:
                    continue
                cur_sl = float(pos.sl) if pos.sl else None
                sym_info = mt5.symbol_info(pos.symbol)
                point = float(getattr(sym_info, "point", 0.0001) or 0.0001) if sym_info else 0.0001
                # NEVER move SL backwards.
                if pos.type == mt5.POSITION_TYPE_BUY:
                    if cur_sl is not None and proposed_sl <= cur_sl + point:
                        continue
                else:  # SELL
                    if cur_sl is not None and proposed_sl >= cur_sl - point:
                        continue
                _modify_sl(pos, proposed_sl, target_lock)
        except Exception as e:
            log.warning("manage_open_positions ticket=%s error: %s", getattr(pos, "ticket", "?"), e)


def reconcile_closed() -> None:
    """Detect tickets that closed (no longer in positions) and report them."""
    open_now = {p.ticket for p in (mt5.positions_get() or [])}
    closed = [t for t in list(TRACKED_TICKETS) if t not in open_now]
    for t in closed:
        deals = mt5.history_deals_get(position=t)
        if not deals:
            TRACKED_TICKETS.pop(t, None)
            continue
        pnl = sum(d.profit for d in deals)
        commission = sum(d.commission for d in deals)
        swap = sum(d.swap for d in deals)
        exit_deal = max(deals, key=lambda d: d.time)
        # Phase-1: surface the true close reason (max_hold / profit_lock / trail / manual)
        # so the server stops mis-classifying force-closes as sl_hit.
        reason = CLOSE_REASONS.pop(t, None)
        if reason and reason.startswith("max_hold"):
            close_reason = "max_hold"
        elif reason and "drawdown" in reason:
            close_reason = "profit_lock"
        elif reason:
            close_reason = "manager_close"
        else:
            close_reason = None  # let server's SL/TP-distance heuristic decide for organic closes
        payload = {
            "ticket": t,
            "exit_price": exit_deal.price,
            "pnl": pnl,
            "commission": commission,
            "swap": swap,
        }
        if close_reason:
            payload["reason"] = close_reason
        report("close", payload)
        log.info("CLOSE ticket %s · pnl %.2f · reason=%s", t, pnl, close_reason or "organic")
        TRACKED_TICKETS.pop(t, None)
        # Clean up trade-management state for closed tickets
        PEAK_PROFITS.pop(t, None)
        LAST_TRAIL_TS.pop(t, None)
        CLOSING_TICKETS.discard(t)
        INITIAL_RISK_USD.pop(t, None)
        INITIAL_OPEN_PRICE.pop(t, None)
        PARTIAL_DONE.discard(t)
        BE_DONE.discard(t)
        TICKET_MAX_HOLD.pop(t, None)
        OPEN_TICKET_OPENED_AT.pop(t, None)


_running = True
def _stop(*_):
    global _running
    _running = False
    log.info("Shutting down…")
signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


def main():
    log.info("Aurum FX bridge v%s starting · API %s · log %s", BRIDGE_VERSION, API_URL, LOG_FILE)
    if not mt5_init():
        log.error("Initial MT5 connect failed — exiting (watchdog will retry).")
        sys.exit(1)

    build_symbol_map()

    # Pre-load existing AurumFX positions so we can report their closes
    # Phase-1 (v1.8.1): also hydrate OPEN_TICKET_OPENED_AT + TICKET_MAX_HOLD from each open
    # position's broker-side fields. This fixes the bug where bridge restarts caused legacy
    # tickets to never get force-closed by max_hold (was: held a XAU trade for 13.5h instead of 8h).
    for p in (mt5.positions_get() or []):
        if p.magic == 990077:
            TRACKED_TICKETS[p.ticket] = ""
            # MT5 position `time` field = unix seconds the position was opened.
            opened_at = float(getattr(p, "time", 0) or 0)
            if opened_at > 0:
                OPEN_TICKET_OPENED_AT[p.ticket] = opened_at
            # Without the original signal, use the configurable default (AURUM_DEFAULT_MAX_HOLD_MIN=480).
            # If you want different per-pair defaults, set them after this loop.
            TICKET_MAX_HOLD[p.ticket] = DEFAULT_MAX_HOLD_MIN
    log.info("Tracking %d existing position(s) · hydrated max_hold=%d min from env",
             len(TRACKED_TICKETS), DEFAULT_MAX_HOLD_MIN)
    log.info("Trade management · trailing=%s start=$%.2f distance=$%.2f · profit-lock=%s drawdown=%.0f%% min=$%.2f · partial-1R=%s frac=%.0f%%",
             "ON" if TRAILING_ENABLED else "OFF", TRAILING_START_PROFIT, TRAILING_DISTANCE,
             "ON" if PROFIT_LOCK_ENABLED else "OFF", PROFIT_LOCK_DRAWDOWN_PERCENT, PROFIT_LOCK_MIN_PROFIT,
             "ON" if PARTIAL_CLOSE_ENABLED else "OFF", PARTIAL_CLOSE_FRACTION * 100.0)

    global LAST_LOOP_ERROR
    health_check_every = 30  # seconds
    last_health_check = 0.0
    while _running:
        try:
            # v1.8: MT5 health probe every 30s. If unhealthy, run reconnect daemon.
            if time.time() - last_health_check > health_check_every:
                last_health_check = time.time()
                if not mt5_is_healthy():
                    if not mt5_reconnect_if_needed():
                        sys.exit(3)  # watchdog will restart cleanly
            push_candles()                 # v1.7: stream OHLC bars to server
            signals = poll_signals()
            for s in signals:
                # Carry over max_hold_minutes from the signal so we can enforce it post-fill
                mhm = int(s.get("max_hold_minutes") or 0)
                if mhm > 0 and s.get("id"):
                    SIGNAL_MAX_HOLD[s["id"]] = mhm
                execute(s)
            _force_close_expired()         # v1.6: close scalps past their TTL
            manage_open_positions()
            reconcile_closed()
            LAST_LOOP_ERROR = ""
        except SystemExit:
            raise
        except Exception as e:
            LAST_LOOP_ERROR = f"{type(e).__name__}: {str(e)[:200]}"
            log.exception("loop error: %s", e)
        for _ in range(int(POLL_INTERVAL * 10)):
            if not _running:
                break
            time.sleep(0.1)
    try:
        mt5.shutdown()
    except Exception:
        pass
    log.info("Bye.")


if __name__ == "__main__":
    main()
