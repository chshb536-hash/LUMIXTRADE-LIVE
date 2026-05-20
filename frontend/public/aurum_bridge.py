#!/usr/bin/env python3
"""
Aurum FX — MT5 Bridge
---------------------
Connects your local MetaTrader 5 terminal to your Aurum FX account.

Setup (one time):
    pip install MetaTrader5 requests

Run:
    set AURUM_API_KEY=abk_xxxxxxxxxxxx
    set AURUM_API_URL=https://<project>.functions.supabase.co
    set MT5_LOGIN=12345678
    set MT5_PASSWORD=your_mt5_password
    set MT5_SERVER=YourBroker-Server
    python aurum_bridge.py

The bridge polls Aurum every 5 seconds for new signals and executes them on MT5.
Closed trades and fills are reported back automatically. Your MT5 password never leaves this machine.
"""
from __future__ import annotations
import os
import sys
import time
import json
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
API_KEY  = os.environ.get("AURUM_API_KEY")
API_URL  = os.environ.get("AURUM_API_URL", "").rstrip("/")
MT5_LOGIN    = os.environ.get("MT5_LOGIN")
MT5_PASSWORD = os.environ.get("MT5_PASSWORD")
MT5_SERVER   = os.environ.get("MT5_SERVER")
POLL_INTERVAL = float(os.environ.get("AURUM_POLL_INTERVAL", "5"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("aurum")

if not (API_KEY and API_URL and MT5_LOGIN and MT5_PASSWORD and MT5_SERVER):
    log.error("Missing required env vars. See file header for setup.")
    sys.exit(1)

HEADERS = {"x-aurum-bridge-key": API_KEY, "Content-Type": "application/json"}
TRACKED_TICKETS: Dict[int, str] = {}  # ticket -> signal_id


def mt5_init() -> bool:
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


def account_payload() -> Dict[str, Any]:
    a = mt5.account_info()
    if not a:
        return {}
    return {
        "login": a.login,
        "server": a.server,
        "broker": a.company,
        "currency": a.currency,
        "balance": a.balance,
        "equity": a.equity,
        "margin": a.margin,
        "free_margin": a.margin_free,
    }


def poll_signals() -> List[Dict[str, Any]]:
    body = {"account": account_payload()}
    try:
        r = requests.post(f"{API_URL}/bridge-poll", headers=HEADERS, json=body, timeout=15)
        if r.status_code == 401:
            log.error("Bridge key rejected. Generate a new one in the dashboard.")
            return []
        r.raise_for_status()
        return r.json().get("signals", [])
    except Exception as e:
        log.warning("poll failed: %s", e)
        return []


def report(event: str, payload: Dict[str, Any]) -> None:
    try:
        body = {"event": event, **payload}
        requests.post(f"{API_URL}/bridge-report", headers=HEADERS, json=body, timeout=15)
    except Exception as e:
        log.warning("report %s failed: %s", event, e)


def execute(sig: Dict[str, Any]) -> None:
    pair = sig["pair"]
    side = sig["side"]
    lot  = float(sig["lot"])
    sl   = float(sig["sl"])
    tp   = float(sig["tp"])
    sym = mt5.symbol_info(pair)
    if sym is None:
        log.warning("symbol %s not found", pair)
        report("reject", {"signal_id": sig["id"], "reason": f"symbol {pair} not found"})
        return
    if not sym.visible:
        mt5.symbol_select(pair, True)
    tick = mt5.symbol_info_tick(pair)
    if not tick:
        report("reject", {"signal_id": sig["id"], "reason": "no tick data"})
        return
    price = tick.ask if side == "buy" else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pair,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": 990077,
        "comment": "AurumFX",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        rc = result.retcode if result else None
        log.warning("order rejected (retcode=%s) %s", rc, result.comment if result else "no result")
        report("reject", {"signal_id": sig["id"], "reason": f"retcode {rc}"})
        return
    log.info("FILL %s %s %s @ %s · ticket %s", side.upper(), lot, pair, result.price, result.order)
    TRACKED_TICKETS[result.order] = sig["id"]
    report("fill", {
        "signal_id": sig["id"],
        "ticket": result.order,
        "pair": pair, "side": side, "lot": lot,
        "entry": result.price, "sl": sl, "tp": tp,
    })


def reconcile_closed() -> None:
    """Detect tickets that closed (no longer in positions) and report them."""
    open_now = {p.ticket for p in (mt5.positions_get() or [])}
    closed = [t for t in list(TRACKED_TICKETS) if t not in open_now]
    for t in closed:
        # Pull from history
        deals = mt5.history_deals_get(position=t)
        if not deals:
            TRACKED_TICKETS.pop(t, None)
            continue
        pnl = sum(d.profit for d in deals)
        commission = sum(d.commission for d in deals)
        swap = sum(d.swap for d in deals)
        exit_deal = max(deals, key=lambda d: d.time)
        report("close", {
            "ticket": t,
            "exit_price": exit_deal.price,
            "pnl": pnl,
            "commission": commission,
            "swap": swap,
        })
        log.info("CLOSE ticket %s · pnl %.2f", t, pnl)
        TRACKED_TICKETS.pop(t, None)


_running = True
def _stop(*_):
    global _running
    _running = False
    log.info("Shutting down…")
signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


def main():
    log.info("Aurum FX bridge starting · API %s", API_URL)
    if not mt5_init():
        sys.exit(1)
    # Pre-load existing AurumFX positions so we can report their closes
    for p in (mt5.positions_get() or []):
        if p.magic == 990077:
            TRACKED_TICKETS[p.ticket] = ""  # signal id unknown but still tracked
    log.info("Tracking %d existing position(s)", len(TRACKED_TICKETS))
    while _running:
        try:
            signals = poll_signals()
            for s in signals:
                execute(s)
            reconcile_closed()
        except Exception as e:
            log.exception("loop error: %s", e)
        for _ in range(int(POLL_INTERVAL * 10)):
            if not _running: break
            time.sleep(0.1)
    mt5.shutdown()
    log.info("Bye.")


if __name__ == "__main__":
    main()
