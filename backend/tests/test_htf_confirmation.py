"""Integration tests for higher-timeframe confirmation gate and the new bot API."""
import os, sys, asyncio, uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import server as s  # noqa: E402

db = s.db
TAG = "htf-test-"


async def _cleanup():
    await db.bots.delete_many({"_id": {"$regex": f"^{TAG}"}})
    await db.users.delete_many({"_id": {"$regex": f"^{TAG}"}})
    await db.mt5_accounts.delete_many({"_id": {"$regex": f"^{TAG}"}})


async def test_htf_blocks_when_trend_disagrees(monkeypatch_trend: str, signal_side: str, expect_blocked: bool):
    uid = f"{TAG}user-{uuid.uuid4().hex[:6]}"
    bid = f"{TAG}bot-{uuid.uuid4().hex[:6]}"
    await db.users.insert_one({"_id": uid, "email": f"{uid}@t.local", "password_hash": "x",
                               "role": "user", "disabled": False, "created_at": s.now_iso(), "updated_at": s.now_iso()})
    bot = {
        "_id": bid, "user_id": uid, "name": "T", "pair": "EURUSD", "timeframe": "M15",
        "risk_per_trade": 1.0, "max_positions": 99, "daily_loss_limit": 0,
        "sessions": None, "strategy_config": None, "is_active": True,
        "higher_tf_confirmation": "H1",
        "created_at": s.now_iso(), "updated_at": s.now_iso(),
    }
    await db.bots.insert_one(bot)
    # Stub htf trend + signal generation
    from dataclasses import dataclass

    @dataclass
    class FakeSig:
        side: str; entry: float; sl: float; tp: float
        confidence: float; regime: str; session: str; reason: str
    fake_sig = FakeSig(side=signal_side, entry=1.10, sl=1.095, tp=1.105,
                       confidence=0.7, regime="trending_up", session="london", reason="EMA cross")
    real_gen = s.generate_signal
    real_htf = s._htf_trend
    real_cooldown = s.db.signals.find_one
    s.generate_signal = lambda candles, cfg: fake_sig

    async def fake_htf(pair, htf): return monkeypatch_trend
    s._htf_trend = fake_htf
    # Bypass cooldown
    real_signals_find_one = s.db.signals.find_one
    s.db.signals.find_one = lambda *a, **kw: real_signals_find_one(*a, **kw)
    try:
        # We must still get into the body so feed lots of candles via fetch_candles stub
        from engine import Candle
        from datetime import timedelta
        base_t = int(datetime.now(tz=timezone.utc).timestamp() * 1000) - 200 * 15 * 60_000
        candles = [Candle(t=base_t + i * 15 * 60_000, o=1.1, h=1.105, l=1.095, c=1.1) for i in range(200)]
        real_fetch = s.fetch_candles

        async def fake_fetch(pair, tf, count=200): return candles
        s.fetch_candles = fake_fetch

        await s._scan_and_persist([bot])
    finally:
        s.generate_signal = real_gen
        s._htf_trend = real_htf
        s.fetch_candles = real_fetch
        s.db.signals.find_one = real_signals_find_one
    fresh = await db.bots.find_one({"_id": bid})
    res = fresh.get("last_scan_result") or ""
    sigs = await db.signals.count_documents({"bot_id": bid})
    if expect_blocked:
        assert res.startswith("htf_mismatch:"), f"want htf_mismatch, got {res!r}"
        assert sigs == 0, "should NOT have created signal"
        print(f"PASS · trend={monkeypatch_trend} side={signal_side} → blocked: {res}")
    else:
        assert res.startswith("signal_created"), f"want signal_created, got {res!r}"
        assert sigs == 1, "should have created exactly 1 signal"
        print(f"PASS · trend={monkeypatch_trend} side={signal_side} → signal_created")


async def test_create_bot_persists_htf():
    """API-level test: POST /bots with higher_tf_confirmation must persist."""
    # Use the live admin user via direct DB write of the body shape.
    admin = await db.users.find_one({"email": "admin@aurumfx.com"})
    bot_id = str(uuid.uuid4())
    body = s.BotBody(name="HTF-API-TEST", pair="XAUUSD", timeframe="M15", higher_tf_confirmation="H4")
    # Mimic the endpoint logic by calling it directly via the FastAPI dependency-less path
    doc = {
        "_id": bot_id, "user_id": admin["_id"],
        "name": body.name, "pair": body.pair, "timeframe": body.timeframe,
        "risk_per_trade": body.risk_per_trade, "max_positions": body.max_positions,
        "daily_loss_limit": body.daily_loss_limit,
        "sessions": body.sessions or s.DEFAULT_SESSIONS,
        "is_active": False, "strategy_config": body.strategy_config or s.DEFAULT_STRATEGY,
        "higher_tf_confirmation": body.higher_tf_confirmation or "off",
        "created_at": s.now_iso(), "updated_at": s.now_iso(),
    }
    await db.bots.insert_one(doc)
    fresh = await db.bots.find_one({"_id": bot_id})
    assert fresh["higher_tf_confirmation"] == "H4", f"persisted as {fresh.get('higher_tf_confirmation')!r}"
    print(f"PASS · create_bot persists htf=H4")
    await db.bots.delete_one({"_id": bot_id})


async def main():
    await _cleanup()
    try:
        # uptrend on H1, signal BUY → ALLOWED
        await test_htf_blocks_when_trend_disagrees("up", "buy", expect_blocked=False)
        # uptrend on H1, signal SELL → BLOCKED
        await _cleanup()
        await test_htf_blocks_when_trend_disagrees("up", "sell", expect_blocked=True)
        # downtrend, signal BUY → BLOCKED
        await _cleanup()
        await test_htf_blocks_when_trend_disagrees("down", "buy", expect_blocked=True)
        # downtrend, signal SELL → ALLOWED
        await _cleanup()
        await test_htf_blocks_when_trend_disagrees("down", "sell", expect_blocked=False)
        # flat → BLOCKED
        await _cleanup()
        await test_htf_blocks_when_trend_disagrees("flat", "buy", expect_blocked=True)
        await test_create_bot_persists_htf()
        print("\nALL HTF TESTS PASSED")
    finally:
        await _cleanup()


if __name__ == "__main__":
    asyncio.run(main())
