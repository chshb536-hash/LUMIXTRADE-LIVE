"""Smoke test for Live PNL: bridge-poll persists positions[] onto matching trade rows."""
import os
import asyncio
import uuid
import requests
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

API = os.environ["REACT_APP_BACKEND_URL"] if False else "http://localhost:8001"
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]


async def main():
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]

    # 1) Find admin user + an active bridge key (or create one)
    admin = await db.users.find_one({"email": "admin@aurumfx.com"})
    assert admin, "admin user missing"
    user_id = admin["_id"]

    key_row = await db.bridge_keys.find_one({"user_id": user_id, "revoked": False})
    if not key_row:
        api_key = "abk_" + uuid.uuid4().hex
        key_row = {
            "_id": str(uuid.uuid4()),
            "user_id": user_id,
            "mt5_account_id": None,
            "api_key": api_key,
            "label": "live-pnl-test",
            "last_seen_at": None,
            "revoked": False,
            "created_at": "2026-05-07T00:00:00Z",
        }
        await db.bridge_keys.insert_one(key_row)

    bridge_key = key_row["api_key"]
    print(f"using bridge_key={bridge_key[:14]}...")

    # 2) Seed an open trade with a known mt5_ticket
    ticket = 999000111
    trade_id = f"test-livepnl-{ticket}"
    await db.trades.delete_one({"_id": trade_id})
    await db.trades.insert_one({
        "_id": trade_id,
        "user_id": user_id,
        "bot_id": None,
        "signal_id": None,
        "pair": "XAUUSD",
        "side": "buy",
        "lot": 0.01,
        "entry": 2500.00,
        "sl": 2495.0,
        "tp": 2510.0,
        "pnl": None,
        "status": "open",
        "mt5_ticket": ticket,
        "opened_at": "2026-05-07T00:00:00Z",
    })

    # 3) Send /api/bridge-poll with a positions snapshot
    body = {
        "account": {
            "login": "12345678",
            "server": "Exness-MT5Real",
            "broker": "Exness",
            "currency": "USD",
            "balance": 1000.0, "equity": 1015.5,
            "margin": 25.0, "free_margin": 990.5,
        },
        "positions": [{
            "ticket": ticket,
            "symbol": "XAUUSDm",
            "type": "buy",
            "volume": 0.01,
            "price_open": 2500.00,
            "price_current": 2503.42,
            "sl": 2497.5,  # trailed
            "tp": 2510.0,
            "profit": 3.42,
            "swap": -0.10,
            "commission": -0.05,
        }],
    }
    r = requests.post(
        f"{API}/api/bridge-poll",
        headers={"x-aurum-bridge-key": bridge_key, "Content-Type": "application/json"},
        json=body, timeout=10,
    )
    print(f"bridge-poll status={r.status_code} body={r.text[:200]}")
    assert r.status_code == 200, r.text

    # 4) Re-read the trade row and assert live fields persisted
    after = await db.trades.find_one({"_id": trade_id})
    print("trade after poll:", {k: after.get(k) for k in ("live_pnl", "live_swap", "live_commission", "live_price", "live_at", "sl", "tp")})
    assert after["live_pnl"] == 3.42, f"live_pnl mismatch: {after.get('live_pnl')}"
    assert after["live_price"] == 2503.42
    assert after["sl"] == 2497.5  # SL trail reflected
    assert after.get("live_at"), "live_at not set"

    # cleanup
    await db.trades.delete_one({"_id": trade_id})
    print("PASS — live PnL persists from bridge-poll positions[]")


if __name__ == "__main__":
    asyncio.run(main())
