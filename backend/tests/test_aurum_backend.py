"""
Backend tests for Aurum FX (LumixTrade) — full validation per review_request.
Covers: health, auth (login/me/register), bots CRUD + default min_confidence,
signals, bridge keys, bridge-poll (X-Aurum-Bridge-Key), bridge download
(subscription gated -> expected 402), Twelve Data integration via scan.
"""
import os
import time
import uuid
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://aurum-signals-5.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@aurumfx.com"
ADMIN_PASSWORD = "Mohyuddin@123"


# ---- session-scoped fixtures ----
@pytest.fixture(scope="session")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def admin_token(session):
    r = session.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    data = r.json()
    assert "access_token" in data
    return data["access_token"]


@pytest.fixture(scope="session")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}


# ---- shared state for chained tests ----
_state = {}


# ---- Health ----
class TestHealth:
    def test_health_ok(self, session):
        r = session.get(f"{API}/health")
        assert r.status_code == 200
        data = r.json()
        assert data.get("status") == "ok"
        assert data.get("service") == "lumixtrade-api"


# ---- Auth ----
class TestAuth:
    def test_login_admin(self, session):
        r = session.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["email"] == ADMIN_EMAIL
        assert data["role"] == "admin"
        assert "access_token" in data and len(data["access_token"]) > 20
        _state["admin_token"] = data["access_token"]
        _state["admin_id"] = data["id"]

    def test_login_wrong_password(self, session):
        r = session.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "WRONG_PASS_xyz"})
        assert r.status_code == 401

    def test_auth_me(self, session, admin_headers):
        r = session.get(f"{API}/auth/me", headers=admin_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["email"] == ADMIN_EMAIL
        assert data["role"] == "admin"

    def test_auth_me_no_token(self, session):
        r = requests.get(f"{API}/auth/me")  # no cookies/header
        assert r.status_code == 401

    def test_register_new_user(self, session):
        unique = uuid.uuid4().hex[:8]
        email = f"test_user_{unique}@example.com"
        r = session.post(f"{API}/auth/register", json={
            "email": email,
            "password": "TestPass123!",
            "display_name": f"TEST_User_{unique}",
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["email"] == email
        assert data["role"] == "user"
        assert "access_token" in data
        _state["regular_user_token"] = data["access_token"]
        _state["regular_user_email"] = email
        _state["regular_user_id"] = data["id"]

    def test_register_duplicate(self, session):
        email = _state.get("regular_user_email")
        if not email:
            pytest.skip("Need prior registered user")
        r = session.post(f"{API}/auth/register", json={
            "email": email, "password": "TestPass123!",
        })
        assert r.status_code == 400


# ---- Bots ----
class TestBots:
    def test_list_bots_admin(self, session, admin_headers):
        r = session.get(f"{API}/bots", headers=admin_headers)
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)

    def test_create_bot_default_min_confidence(self, session, admin_headers):
        payload = {
            "name": "TEST_AurumBot_001",
            "pair": "XAUUSD",
            "timeframe": "M15",
        }
        r = session.post(f"{API}/bots", json=payload, headers=admin_headers)
        assert r.status_code == 200, r.text
        bot = r.json()
        assert bot["name"] == "TEST_AurumBot_001"
        assert bot["pair"] == "XAUUSD"
        assert "id" in bot
        sc = bot.get("strategy_config") or {}
        assert sc.get("min_confidence") == 0.5, f"Expected min_confidence=0.5, got {sc}"
        _state["bot_id"] = bot["id"]

    def test_get_bot_persistence(self, session, admin_headers):
        bot_id = _state.get("bot_id")
        if not bot_id:
            pytest.skip("No bot created")
        r = session.get(f"{API}/bots", headers=admin_headers)
        assert r.status_code == 200
        bots = r.json()
        found = next((b for b in bots if b["id"] == bot_id), None)
        assert found is not None, "Created bot not found in list"
        assert found["strategy_config"]["min_confidence"] == 0.5


# ---- Signals ----
class TestSignals:
    def test_list_signals_admin(self, session, admin_headers):
        r = session.get(f"{API}/signals", headers=admin_headers)
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)


# ---- Bridge keys ----
class TestBridgeKeys:
    def test_create_bridge_key(self, session, admin_headers):
        r = session.post(f"{API}/bridge/keys", json={"label": "TEST_BridgeKey_1"}, headers=admin_headers)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("label") == "TEST_BridgeKey_1"
        assert d.get("api_key", "").startswith("abk_")
        assert d.get("revoked") is False
        _state["bridge_api_key"] = d["api_key"]
        _state["bridge_key_id"] = d["id"]

    def test_list_bridge_keys(self, session, admin_headers):
        r = session.get(f"{API}/bridge/keys", headers=admin_headers)
        assert r.status_code == 200, r.text
        items = r.json()
        assert isinstance(items, list) and len(items) >= 1
        keys = [k.get("api_key") for k in items]
        assert _state.get("bridge_api_key") in keys

    def test_bridge_poll_with_valid_key(self, session):
        api_key = _state.get("bridge_api_key")
        if not api_key:
            pytest.skip("No bridge key")
        # bridge-poll uses X-Aurum-Bridge-Key, no JSON Content-Type required, but body OK
        r = requests.post(
            f"{API}/bridge-poll",
            headers={"X-Aurum-Bridge-Key": api_key, "Content-Type": "application/json"},
            json={"account": {
                "login": "12345678", "server": "TestBroker-Demo", "broker": "TestBroker",
                "currency": "USD", "balance": 10000, "equity": 10000, "margin": 0, "free_margin": 10000,
            }, "positions": [], "bridge_version": "1.4"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        # Expect a dict with signals key (likely empty)
        assert isinstance(data, dict)
        assert "signals" in data
        assert isinstance(data["signals"], list)

    def test_bridge_poll_invalid_key(self):
        r = requests.post(
            f"{API}/bridge-poll",
            headers={"X-Aurum-Bridge-Key": "abk_invalid_key_xyz", "Content-Type": "application/json"},
            json={"account": {}},
        )
        assert r.status_code == 401

    def test_bridge_poll_missing_key(self):
        r = requests.post(f"{API}/bridge-poll", json={"account": {}})
        assert r.status_code == 401


# ---- Bridge download ----
class TestBridgeDownload:
    def test_bridge_download_no_subscription(self, session, admin_headers):
        """Admin has no active subscription on fresh deploy — expect 402."""
        r = session.get(f"{API}/bridge/download", headers=admin_headers)
        # Bridge file is gated on subscription. Fresh deploy => 402 expected.
        # If admin somehow has a sub, we accept 200 with python content-type.
        if r.status_code == 200:
            ct = r.headers.get("content-type", "")
            assert "python" in ct
        else:
            assert r.status_code == 402, f"Expected 402 (no sub) or 200, got {r.status_code}: {r.text[:200]}"

    def test_bridge_download_unauthenticated(self):
        r = requests.get(f"{API}/bridge/download")
        assert r.status_code == 401


# ---- Twelve Data integration ----
class TestTwelveDataIntegration:
    def test_scan_uses_market_data(self, session, admin_headers):
        """Trigger a scan on the created bot — verifies Twelve Data call path."""
        bot_id = _state.get("bot_id")
        if not bot_id:
            pytest.skip("No bot to scan")
        r = session.post(f"{API}/bots/{bot_id}/scan", headers=admin_headers, timeout=60)
        # Scan may return 200 (ok) or 400 (insufficient data) but NOT 500 (auth error)
        assert r.status_code in (200, 400), f"Scan failed (possible TD auth error): {r.status_code} {r.text}"
        if r.status_code == 200:
            data = r.json()
            assert "scanned" in data or "ok" in data


# ---- Final health re-check ----
class TestFinalHealth:
    def test_health_still_ok(self, session):
        r = session.get(f"{API}/health")
        assert r.status_code == 200
        assert r.json().get("status") == "ok"


# ---- Cleanup ----
@pytest.fixture(scope="session", autouse=True)
def cleanup(request, session):
    yield
    token = _state.get("admin_token")
    if not token:
        return
    headers = {"Authorization": f"Bearer {token}"}
    # Revoke bridge key
    key_id = _state.get("bridge_key_id")
    if key_id:
        try:
            session.post(f"{API}/bridge/keys/{key_id}/revoke", headers=headers)
        except Exception:
            pass
    # Delete test bot
    bot_id = _state.get("bot_id")
    if bot_id:
        try:
            session.delete(f"{API}/bots/{bot_id}", headers=headers)
        except Exception:
            pass
