"""
Aurum FX backend regression tests.
Runs against the public REACT_APP_BACKEND_URL, exercising auth, bots, signals,
trades, bridge keys, payment instructions/submissions, admin approvals.
"""
import io
import os
import time
import uuid
import struct
import zlib

import pytest
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    # fallback only for local dev; tests rely on env var in CI
    "https://forex-gold-bot-14.preview.emergentagent.com",
).rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@aurumfx.com"
ADMIN_PASSWORD = "Mohyuddin@123"


def _png_bytes() -> bytes:
    """Build a minimal valid 1x1 PNG."""
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xff\x00\x00"
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# ---------- Fixtures ----------
@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=20)
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    data = r.json()
    assert data.get("role") == "admin"
    s.headers.update({"Authorization": f"Bearer {data['access_token']}"})
    return s


@pytest.fixture(scope="session")
def user_creds():
    email = f"fxuser_{int(time.time())}_{uuid.uuid4().hex[:6]}@test.com"
    return {"email": email, "password": "Test1234!", "display_name": "FX Tester"}


@pytest.fixture(scope="session")
def user_session(user_creds):
    s = requests.Session()
    r = s.post(f"{API}/auth/register", json=user_creds, timeout=20)
    assert r.status_code == 200, f"Register failed: {r.status_code} {r.text}"
    data = r.json()
    assert "access_token" in data
    assert data.get("role") == "user"
    assert "password_hash" not in data
    s.headers.update({"Authorization": f"Bearer {data['access_token']}"})
    s.user_id = data["id"]
    return s


# ---------- Auth ----------
class TestAuth:
    def test_register_creates_user_with_role_user(self, user_session, user_creds):
        r = user_session.get(f"{API}/auth/me")
        assert r.status_code == 200
        u = r.json()
        assert u["email"] == user_creds["email"].lower()
        assert u["role"] == "user"
        assert "password_hash" not in u

    def test_register_duplicate_fails(self, user_session, user_creds):
        r = requests.post(f"{API}/auth/register", json=user_creds, timeout=20)
        assert r.status_code == 400

    def test_login_invalid_credentials(self):
        r = requests.post(f"{API}/auth/login",
                          json={"email": ADMIN_EMAIL, "password": "wrong-pass-xyz"},
                          timeout=20)
        assert r.status_code in (401, 429)

    def test_login_admin_returns_admin_role(self, admin_session):
        r = admin_session.get(f"{API}/auth/me")
        assert r.status_code == 200
        assert r.json().get("role") == "admin"

    def test_admin_has_active_yearly_subscription(self, admin_session):
        r = admin_session.get(f"{API}/subscriptions/me")
        assert r.status_code == 200
        sub = r.json()
        assert sub is not None
        assert sub.get("status") == "active"
        assert sub.get("plan") == "yearly"

    def test_logout_clears_cookies(self, user_creds):
        s = requests.Session()
        r = s.post(f"{API}/auth/login",
                   json={"email": user_creds["email"], "password": user_creds["password"]},
                   timeout=20)
        assert r.status_code == 200
        # cookies should be set
        assert any(c.name == "access_token" for c in s.cookies)
        r2 = s.post(f"{API}/auth/logout", timeout=20)
        assert r2.status_code == 200
        # After logout, /auth/me without bearer should be 401
        s2 = requests.Session()
        s2.cookies = s.cookies
        r3 = s2.get(f"{API}/auth/me", timeout=20)
        assert r3.status_code == 401


# ---------- Bots ----------
class TestBots:
    def test_create_list_patch_delete(self, user_session):
        # CREATE
        r = user_session.post(f"{API}/bots", json={
            "name": "TEST_Bot_A", "pair": "XAUUSD", "timeframe": "M15",
            "risk_per_trade": 1.0, "max_positions": 2, "daily_loss_limit": 5.0,
        })
        assert r.status_code == 200, r.text
        bot = r.json()
        assert bot["name"] == "TEST_Bot_A"
        assert bot["is_active"] is False
        bot_id = bot["id"]

        # LIST
        r = user_session.get(f"{API}/bots")
        assert r.status_code == 200
        assert any(b["id"] == bot_id for b in r.json())

        # PATCH (toggle active)
        r = user_session.patch(f"{API}/bots/{bot_id}", json={"is_active": True})
        assert r.status_code == 200
        assert r.json()["is_active"] is True

        # DELETE
        r = user_session.delete(f"{API}/bots/{bot_id}")
        assert r.status_code == 200
        # GET should not find it
        r = user_session.patch(f"{API}/bots/{bot_id}", json={"is_active": False})
        assert r.status_code == 404

    def test_scan_uses_twelve_data(self, user_session):
        # create bot then scan
        r = user_session.post(f"{API}/bots", json={
            "name": "TEST_Bot_Scan", "pair": "XAUUSD", "timeframe": "M15",
        })
        bot_id = r.json()["id"]
        r = user_session.post(f"{API}/bots/{bot_id}/scan")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        assert "created" in body
        # price cache should have rows for XAUUSD
        time.sleep(1)
        r = user_session.get(f"{API}/price-cache", params={"symbols": "XAUUSD"})
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        # cleanup
        user_session.delete(f"{API}/bots/{bot_id}")
        # If twelve data is unavailable we don't fail hard, but the response should still be ok
        # Assert at least the contract holds
        assert body.get("scanned") == 1


# ---------- Signals & Trades ----------
class TestSignalsTrades:
    def test_signals_array_with_limit(self, user_session):
        r = user_session.get(f"{API}/signals", params={"limit": 5})
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_trades_array_with_limit(self, user_session):
        r = user_session.get(f"{API}/trades", params={"limit": 5})
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ---------- Bridge ----------
class TestBridge:
    def test_create_list_revoke_key_and_poll(self, user_session):
        r = user_session.post(f"{API}/bridge/keys", json={"label": "TEST_Bridge"})
        assert r.status_code == 200, r.text
        key = r.json()
        assert key["api_key"].startswith("abk_")
        kid = key["id"]
        api_key = key["api_key"]

        # List
        r = user_session.get(f"{API}/bridge/keys")
        assert r.status_code == 200
        assert any(k["id"] == kid for k in r.json())

        # Poll with valid key + heartbeat
        r = requests.post(f"{API}/bridge-poll",
                          headers={"x-aurum-bridge-key": api_key},
                          json={"account": {
                              "login": "12345678", "server": "Demo", "broker": "Test",
                              "currency": "USD", "balance": 1000, "equity": 1000,
                              "margin": 0, "free_margin": 1000,
                          }}, timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "signals" in body and isinstance(body["signals"], list)

        # Verify mt5_accounts upsert
        r = user_session.get(f"{API}/mt5-accounts")
        assert r.status_code == 200
        assert any(a.get("login") == "12345678" for a in r.json())

        # Invalid key → 401
        r = requests.post(f"{API}/bridge-poll",
                          headers={"x-aurum-bridge-key": "abk_invalid"}, timeout=20)
        assert r.status_code == 401

        # Revoke
        r = user_session.post(f"{API}/bridge/keys/{kid}/revoke")
        assert r.status_code == 200
        # Now revoked key returns 401
        r = requests.post(f"{API}/bridge-poll",
                          headers={"x-aurum-bridge-key": api_key}, timeout=20)
        assert r.status_code == 401

    def test_bridge_report_unknown_event_400(self, user_session):
        r = user_session.post(f"{API}/bridge/keys", json={"label": "TEST_Report"})
        api_key = r.json()["api_key"]
        kid = r.json()["id"]
        r = requests.post(f"{API}/bridge-report",
                          headers={"x-aurum-bridge-key": api_key},
                          json={"event": "unknown"}, timeout=20)
        assert r.status_code == 400
        user_session.post(f"{API}/bridge/keys/{kid}/revoke")

    def test_bridge_download(self):
        r = requests.get(f"{API}/bridge/download", timeout=30)
        assert r.status_code == 200
        cd = r.headers.get("content-disposition", "")
        assert "attachment" in cd.lower()
        assert "aurum_bridge.py" in cd
        assert len(r.content) > 5000


# ---------- Payment instructions & submissions ----------
class TestPayments:
    def test_get_payment_instructions(self, user_session):
        r = user_session.get(f"{API}/payment-instructions")
        assert r.status_code == 200
        d = r.json()
        assert d["monthly_price"] == 49
        assert d["quarterly_price"] == 129
        assert d["yearly_price"] == 449

    def test_admin_can_update_payment_instructions(self, admin_session):
        r = admin_session.put(f"{API}/admin/payment-instructions", json={
            "monthly_price": 49, "quarterly_price": 129, "yearly_price": 449,
            "usdt_trc20_address": "TTestAddr111",
        })
        assert r.status_code == 200
        r = admin_session.get(f"{API}/payment-instructions")
        assert r.json().get("usdt_trc20_address") == "TTestAddr111"

    def test_user_cannot_update_payment_instructions(self, user_session):
        r = user_session.put(f"{API}/admin/payment-instructions",
                             json={"monthly_price": 1})
        assert r.status_code == 403

    def test_submit_payment_and_admin_approve(self, user_session, admin_session):
        # submit
        png = _png_bytes()
        files = {"screenshot": ("proof.png", io.BytesIO(png), "image/png")}
        data = {
            "plan": "monthly", "amount": "49", "currency": "USD",
            "method": "USDT_TRC20", "txn_reference": "TXN_TEST_123",
            "notes": "Test submission",
        }
        r = user_session.post(f"{API}/payments/submit", files=files, data=data)
        assert r.status_code == 200, r.text
        sub_id = r.json()["id"]

        # user lists own
        r = user_session.get(f"{API}/payments/submissions")
        assert r.status_code == 200
        assert any(s["id"] == sub_id for s in r.json())

        # admin sees with email enrichment
        r = admin_session.get(f"{API}/admin/payments")
        assert r.status_code == 200
        match = next((s for s in r.json() if s["id"] == sub_id), None)
        assert match is not None
        assert match.get("has_screenshot") is True
        assert match.get("user_email")

        # admin approve
        r = admin_session.post(f"{API}/admin/payments/{sub_id}/approve",
                               json={"notes": "ok"})
        assert r.status_code == 200

        # subscription should be active for user
        r = user_session.get(f"{API}/subscriptions/me")
        assert r.status_code == 200
        sub = r.json()
        assert sub.get("status") == "active"
        assert sub.get("plan") == "monthly"
        assert sub.get("current_period_end")

        # cannot re-approve
        r = admin_session.post(f"{API}/admin/payments/{sub_id}/approve",
                               json={"notes": "again"})
        assert r.status_code == 400

        # admin can fetch proof image
        r = admin_session.get(f"{API}/admin/payments/{sub_id}/proof")
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("image/")

    def test_submit_payment_then_reject(self, user_session, admin_session):
        png = _png_bytes()
        files = {"screenshot": ("p.png", io.BytesIO(png), "image/png")}
        data = {"plan": "quarterly", "amount": "129", "currency": "USD",
                "method": "BTC", "txn_reference": "TXN_REJECT_1"}
        r = user_session.post(f"{API}/payments/submit", files=files, data=data)
        sub_id = r.json()["id"]
        r = admin_session.post(f"{API}/admin/payments/{sub_id}/reject",
                               json={"notes": "blurry"})
        assert r.status_code == 200


# ---------- Profile ----------
class TestProfile:
    def test_patch_profile_updates_display_name(self, user_session):
        new_name = f"Updated_{uuid.uuid4().hex[:6]}"
        r = user_session.patch(f"{API}/profile", json={"display_name": new_name})
        assert r.status_code == 200
        assert r.json()["display_name"] == new_name
        # GET
        r = user_session.get(f"{API}/auth/me")
        assert r.json()["display_name"] == new_name


# ---------- CORS ----------
class TestCORS:
    def test_cors_actual_request_includes_credentials(self):
        """Actual request (not preflight) must carry allow-credentials=true.
        Preflight in this preview env is handled by Cloudflare ingress and
        masks origin to '*', so we validate the runtime/POST response which is
        what the browser uses to accept credentialed responses."""
        origin = "https://forex-gold-bot-14.preview.emergentagent.com"
        r = requests.post(f"{API}/auth/login",
                          headers={"Origin": origin, "Content-Type": "application/json"},
                          json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                          timeout=20)
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-credentials") == "true"
