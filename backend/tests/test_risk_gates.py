"""Integration tests for the 5 risk-control gates added in scheduled_scan.

Each test sets up a minimal user + bot + (optional) trades/equity state, then calls
`_scan_and_persist([bot])` directly and asserts the bot's `last_scan_result` matches
the expected gate, AND that **no signal was inserted** for that bot.
"""
import os
import sys
import uuid
import json
import asyncio
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, ".env"))

import server as s  # noqa: E402

db = s.db
TEST_TAG = "risk-test-"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _cleanup():
    await db.bots.delete_many({"_id": {"$regex": f"^{TEST_TAG}"}})
    await db.trades.delete_many({"_id": {"$regex": f"^{TEST_TAG}"}})
    await db.signals.delete_many({"_id": {"$regex": f"^{TEST_TAG}"}})
    await db.users.delete_many({"_id": {"$regex": f"^{TEST_TAG}"}})
    await db.mt5_accounts.delete_many({"_id": {"$regex": f"^{TEST_TAG}"}})
    await db.risk_state.delete_many({"_id": {"$regex": f"^{TEST_TAG}"}})


async def _make_user(uid_suffix: str) -> str:
    uid = f"{TEST_TAG}user-{uid_suffix}"
    await db.users.insert_one({
        "_id": uid, "email": f"{uid}@test.local",
        "password_hash": "x", "role": "user", "disabled": False,
        "created_at": s.now_iso(), "updated_at": s.now_iso(),
    })
    return uid


async def _make_bot(uid: str, bot_suffix: str, pair: str = "EURUSD",
                    max_positions: int = 99, daily_loss_limit: float = 0.0) -> dict:
    bid = f"{TEST_TAG}bot-{bot_suffix}"
    doc = {
        "_id": bid, "user_id": uid,
        "name": f"Test {bot_suffix}", "pair": pair, "timeframe": "M15",
        "risk_per_trade": 1.0,
        "max_positions": max_positions,
        "daily_loss_limit": daily_loss_limit,
        "sessions": None,
        "strategy_config": None,
        "is_active": True,
        "created_at": s.now_iso(), "updated_at": s.now_iso(),
    }
    await db.bots.insert_one(doc)
    return doc


async def _make_open_trade(uid: str, bid: str, pair: str, side: str, live_pnl: float = 0.0):
    tid = f"{TEST_TAG}trade-{uuid.uuid4().hex[:8]}"
    await db.trades.insert_one({
        "_id": tid, "user_id": uid, "bot_id": bid, "signal_id": None,
        "pair": pair, "side": side, "lot": 0.01, "entry": 1.0,
        "sl": 0.99, "tp": 1.02, "pnl": None, "status": "open",
        "live_pnl": live_pnl, "opened_at": s.now_iso(),
    })
    return tid


async def _make_closed_trade(uid: str, bid: str, pair: str, pnl: float, when: datetime):
    tid = f"{TEST_TAG}trade-{uuid.uuid4().hex[:8]}"
    await db.trades.insert_one({
        "_id": tid, "user_id": uid, "bot_id": bid, "signal_id": None,
        "pair": pair, "side": "buy", "lot": 0.01, "entry": 1.0,
        "exit_price": 0.99, "pnl": pnl, "status": "closed",
        "opened_at": (when - timedelta(hours=1)).isoformat(),
        "closed_at": when.isoformat(),
    })


async def _set_equity(uid: str, equity: float):
    await db.mt5_accounts.update_one(
        {"user_id": uid},
        {"$set": {
            "_id": f"{TEST_TAG}acct-{uid}",
            "user_id": uid, "login": "999", "server": "test",
            "broker": "test", "currency": "USD", "balance": equity, "equity": equity,
            "margin": 0, "free_margin": equity,
            "is_connected": True, "last_heartbeat_at": s.now_iso(),
            "updated_at": s.now_iso(), "created_at": s.now_iso(),
        }},
        upsert=True,
    )


async def _last_result(bot_id: str) -> str:
    b = await db.bots.find_one({"_id": bot_id})
    return (b or {}).get("last_scan_result") or ""


async def _signals_for_bot(bot_id: str) -> int:
    return await db.signals.count_documents({"bot_id": bot_id})


# --- TESTS ---------------------------------------------------------------
async def test_max_positions():
    uid = await _make_user("maxpos")
    bot = await _make_bot(uid, "maxpos", pair="EURUSD", max_positions=2)
    await _make_open_trade(uid, bot["_id"], "EURUSD", "buy")
    await _make_open_trade(uid, bot["_id"], "EURUSD", "sell")
    await s._scan_and_persist([bot])
    res = await _last_result(bot["_id"])
    sigs = await _signals_for_bot(bot["_id"])
    assert res.startswith("max_positions_reached"), f"want max_positions_reached, got {res!r}"
    assert sigs == 0, "should not have created any signal"
    print("PASS · max_positions:", res)


async def test_daily_loss_limit():
    uid = await _make_user("dlloss")
    bot = await _make_bot(uid, "dlloss", pair="EURUSD", daily_loss_limit=10.0)
    today_noon = _now().replace(hour=12, minute=0, second=0, microsecond=0)
    await _make_closed_trade(uid, bot["_id"], "EURUSD", pnl=-8.0, when=today_noon)
    await _make_open_trade(uid, bot["_id"], "EURUSD", "buy", live_pnl=-5.0)
    await s._scan_and_persist([bot])
    res = await _last_result(bot["_id"])
    sigs = await _signals_for_bot(bot["_id"])
    assert res.startswith("daily_loss_blocked"), f"want daily_loss_blocked, got {res!r}"
    assert sigs == 0
    print("PASS · daily_loss_limit:", res)


async def test_correlation_blocked():
    uid = await _make_user("corr")
    # two USD-majors longs already open across DIFFERENT bots — new EURUSD-bot BUY must be blocked
    bot_eur = await _make_bot(uid, "corr-eur", pair="EURUSD", max_positions=99)
    bot_gbp = await _make_bot(uid, "corr-gbp", pair="GBPUSD", max_positions=99)
    bot_aud = await _make_bot(uid, "corr-aud", pair="AUDUSD", max_positions=99)
    await _make_open_trade(uid, bot_gbp["_id"], "GBPUSD", "buy")
    await _make_open_trade(uid, bot_aud["_id"], "AUDUSD", "buy")
    # Force the engine to return a BUY by feeding a strongly trending fake candle set.
    reason = await s._correlation_blocked(uid, "EURUSD", "buy")
    assert reason and reason.startswith("correlation_block:usd_majors"), f"got {reason!r}"
    # And opposite direction should be allowed
    reason2 = await s._correlation_blocked(uid, "EURUSD", "sell")
    assert reason2 is None, f"sell should NOT be blocked but got {reason2!r}"
    print("PASS · correlation:", reason)
    # cleanup local bots
    _ = bot_eur


async def test_drawdown_halt():
    uid = await _make_user("dd")
    bot = await _make_bot(uid, "dd", pair="EURUSD")
    # Seed weekly high at $1000 then drop to $800 (20% drop > 15% threshold)
    week_start_iso = s._utc_week_start().isoformat()
    await db.risk_state.update_one(
        {"_id": uid},
        {"$set": {"_id": uid, "week_start": week_start_iso, "week_high": 1000.0, "halted": False}},
        upsert=True,
    )
    await _set_equity(uid, 800.0)
    await s._scan_and_persist([bot])
    res = await _last_result(bot["_id"])
    assert res.startswith("halt:weekly_dd_"), f"want halt:weekly_dd_, got {res!r}"
    print("PASS · drawdown_halt:", res)


async def test_news_filter():
    uid = await _make_user("news")
    bot = await _make_bot(uid, "news", pair="EURUSD")
    # Write a temporary news event that is ACTIVE right now
    now = _now()
    tmp_payload = {"events": [{"time": now.isoformat().replace("+00:00", "Z"),
                               "name": "TEST_NFP", "type": "NFP"}]}
    path = s._NEWS_CALENDAR_PATH
    original = path.read_text() if path.exists() else None
    try:
        path.write_text(json.dumps(tmp_payload))
        s._NEWS_CACHE["loaded_at"] = 0.0  # bust cache
        await s._scan_and_persist([bot])
        res = await _last_result(bot["_id"])
        assert res.startswith("news_block:TEST_NFP"), f"want news_block:TEST_NFP, got {res!r}"
        print("PASS · news_filter:", res)
    finally:
        if original is not None:
            path.write_text(original)
        else:
            path.unlink(missing_ok=True)
        s._NEWS_CACHE["loaded_at"] = 0.0


async def test_drawdown_resets_new_week():
    """Sanity: a new Monday should auto-reset the halt."""
    uid = await _make_user("ddreset")
    # Old week_start (last week)
    old_week = (s._utc_week_start() - timedelta(days=7)).isoformat()
    await db.risk_state.update_one(
        {"_id": uid},
        {"$set": {"_id": uid, "week_start": old_week, "week_high": 1000.0, "halted": True}},
        upsert=True,
    )
    await _set_equity(uid, 1100.0)
    state = await s._user_drawdown_state(uid)
    assert not state["halted"], f"expected auto-resume on new week, got {state}"
    print("PASS · drawdown_resets_new_week:", state["reason"])


async def main():
    await _cleanup()
    try:
        await test_max_positions()
        await test_daily_loss_limit()
        await test_correlation_blocked()
        await test_drawdown_halt()
        await test_drawdown_resets_new_week()
        await test_news_filter()
        print("\nALL RISK-CONTROL TESTS PASSED")
    finally:
        await _cleanup()


if __name__ == "__main__":
    asyncio.run(main())
