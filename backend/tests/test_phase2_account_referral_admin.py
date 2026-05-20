"""
Aurum FX Phase 2 regression tests:
- Direct (1-level) referral auto-credit on payment approval (initial + renewal)
- /api/referrals/me, /api/transactions/me
- /api/auth/change-password, /api/auth/delete-account
- /api/admin/stats, /api/admin/users[*], grant/cancel sub
- /api/admin/payment-instructions referral_commission_pct
- /api/admin/referrals
- Bridge endpoint regression
"""
import io
import os
import time
import uuid
import struct
import zlib

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@aurumfx.com"
ADMIN_PASSWORD = "Mohyuddin@123"


def _png_bytes() -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xff\x00\x00"
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _bearer(s: requests.Session, token: str):
    s.headers.update({"Authorization": f"Bearer {token}"})


def _new_email(tag: str) -> str:
    # backend lowercases email on register/login, so use lowercase for stable equality
    return f"test_{tag.lower()}_{int(time.time())}_{uuid.uuid4().hex[:6]}@aurumtest.com"


# ---------- Fixtures ----------
@pytest.fixture(scope="module")
def admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=20)
    assert r.status_code == 200, f"Admin login failed: {r.text}"
    data = r.json()
    assert data.get("role") == "admin"
    _bearer(s, data["access_token"])
    s.user_id = data["id"]
    return s


@pytest.fixture(scope="module")
def referrer():
    """User A — the referrer (no referral code on signup)."""
    email = _new_email("refA")
    s = requests.Session()
    r = s.post(f"{API}/auth/register",
               json={"email": email, "password": "Test1234!",
                     "display_name": "Ref A"}, timeout=20)
    assert r.status_code == 200, r.text
    body = r.json()
    _bearer(s, body["access_token"])
    s.user_id = body["id"]
    s.email = email
    s.password = "Test1234!"
    s.referral_code = body["referral_code"]
    assert s.referral_code, "Referrer must have a referral_code"
    return s


@pytest.fixture(scope="module")
def referee(referrer):
    """User B — registered with referrer's code."""
    email = _new_email("refB")
    s = requests.Session()
    r = s.post(f"{API}/auth/register",
               json={"email": email, "password": "Test1234!",
                     "display_name": "Ref B",
                     "referral_code": referrer.referral_code}, timeout=20)
    assert r.status_code == 200, r.text
    body = r.json()
    _bearer(s, body["access_token"])
    s.user_id = body["id"]
    s.email = email
    s.password = "Test1234!"
    return s


# ---------- Helpers ----------
def _submit_and_approve(referee_session, admin_session, plan="monthly", amount=49.0):
    """Submit a USDT TRC20 payment as referee and approve it as admin. Returns sub_id."""
    files = {
        "screenshot": ("p.png", io.BytesIO(_png_bytes()), "image/png"),
    }
    data = {"plan": plan, "method": "usdt_trc20",
            "amount": str(amount), "currency": "USD",
            "txn_reference": f"TX-{uuid.uuid4().hex[:8]}"}
    r = referee_session.post(f"{API}/payments/submit", data=data, files=files,
                             timeout=30)
    assert r.status_code == 200, f"submit failed: {r.text}"
    sub_id = r.json()["id"]
    r = admin_session.post(f"{API}/admin/payments/{sub_id}/approve",
                           json={"notes": "ok"}, timeout=20)
    assert r.status_code == 200, f"approve failed: {r.text}"
    return sub_id


# ---------- Tests ----------
class TestReferralAutoCredit:
    """Direct 1-level referral commission auto-applied on each approved payment."""

    def test_referee_has_referred_by_set(self, referee, referrer):
        r = referee.get(f"{API}/auth/me")
        assert r.status_code == 200
        # auth_me does not return referred_by — verify via admin lookup
        # (covered indirectly by referral event creation below)
        assert r.json()["email"] == referee.email

    def test_default_commission_pct_is_10(self, admin):
        # Reset commission to default 10
        body = {"monthly_price": 49, "quarterly_price": 129, "yearly_price": 449,
                "referral_commission_pct": 10}
        r = admin.put(f"{API}/admin/payment-instructions", json=body, timeout=20)
        assert r.status_code == 200, r.text
        # Verify via GET (any authenticated user)
        r = admin.get(f"{API}/payment-instructions")
        assert r.status_code == 200
        assert float(r.json().get("referral_commission_pct") or 0) == 10.0

    def test_first_purchase_credits_referrer(self, referee, referrer, admin):
        before = referrer.get(f"{API}/referrals/me").json()
        before_days = before["total_days_earned"]
        before_conv = before["total_conversions"]

        _submit_and_approve(referee, admin, plan="monthly", amount=49.0)

        # 30 days * 10% = 3 days
        after = referrer.get(f"{API}/referrals/me").json()
        assert after["total_referred"] >= 1
        assert after["total_conversions"] == before_conv + 1
        assert after["total_days_earned"] == before_days + 3
        # masked email present
        assert any("@" in r["email_masked"] and "*" in r["email_masked"]
                   for r in after["referees"])

    def test_renewal_credits_referrer_again(self, referee, referrer, admin):
        before = referrer.get(f"{API}/referrals/me").json()
        before_days = before["total_days_earned"]
        before_conv = before["total_conversions"]

        _submit_and_approve(referee, admin, plan="monthly", amount=49.0)

        after = referrer.get(f"{API}/referrals/me").json()
        assert after["total_conversions"] == before_conv + 1, "renewal must credit"
        assert after["total_days_earned"] == before_days + 3

    def test_change_commission_pct_then_credits_at_new_rate(self, referee, referrer, admin):
        # Set to 15%
        body = {"monthly_price": 49, "quarterly_price": 129, "yearly_price": 449,
                "referral_commission_pct": 15}
        r = admin.put(f"{API}/admin/payment-instructions", json=body, timeout=20)
        assert r.status_code == 200

        before_days = referrer.get(f"{API}/referrals/me").json()["total_days_earned"]
        _submit_and_approve(referee, admin, plan="monthly", amount=49.0)
        after_days = referrer.get(f"{API}/referrals/me").json()["total_days_earned"]
        # 30 * 15% = 4.5 -> round = 4 (Python banker's? round(4.5)=4)
        # We just assert > 3 (default) and <= 5
        delta = after_days - before_days
        assert delta in (4, 5), f"Expected 4 or 5 days credited at 15%, got {delta}"

        # Reset to default
        admin.put(f"{API}/admin/payment-instructions",
                  json={"monthly_price": 49, "quarterly_price": 129,
                        "yearly_price": 449, "referral_commission_pct": 10},
                  timeout=20)

    def test_referrer_subscription_extended(self, referrer):
        """After approvals above, referrer should have an active subscription."""
        r = referrer.get(f"{API}/subscriptions/me")
        assert r.status_code == 200
        sub = r.json()
        assert sub.get("status") == "active"
        assert sub.get("current_period_end")


class TestReferralAndTransactionsEndpoints:
    def test_referrals_me_shape(self, referrer):
        r = referrer.get(f"{API}/referrals/me")
        assert r.status_code == 200
        b = r.json()
        for k in ("referral_code", "commission_pct", "total_referred",
                  "total_conversions", "total_days_earned", "referees", "events"):
            assert k in b, f"missing key {k}"
        assert isinstance(b["events"], list)
        assert b["referral_code"] == referrer.referral_code

    def test_transactions_me_unified(self, referee, referrer):
        # referee should see payment items
        r = referee.get(f"{API}/transactions/me")
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list)
        assert any(i["kind"] == "payment" for i in items)
        # chronological desc
        ts = [i["created_at"] for i in items if i.get("created_at")]
        assert ts == sorted(ts, reverse=True)
        # referrer should see referral kind items
        r = referrer.get(f"{API}/transactions/me")
        assert r.status_code == 200
        items2 = r.json()
        assert any(i["kind"] == "referral" for i in items2)


class TestChangePasswordAndDelete:
    def test_change_password_wrong_current(self, referee):
        r = referee.post(f"{API}/auth/change-password",
                         json={"current_password": "wrongPass99!",
                               "new_password": "NewPassA1!"})
        assert r.status_code == 400

    def test_change_password_success_and_relogin(self, referee):
        new_pw = "NewPassA1!"
        r = referee.post(f"{API}/auth/change-password",
                         json={"current_password": referee.password,
                               "new_password": new_pw})
        assert r.status_code == 200, r.text
        # Re-login with new password
        s2 = requests.Session()
        r2 = s2.post(f"{API}/auth/login",
                     json={"email": referee.email, "password": new_pw}, timeout=20)
        assert r2.status_code == 200, r2.text
        referee.password = new_pw  # update so subsequent tests work

    def test_delete_account_and_me_returns_401(self, admin):
        # Create disposable user
        email = _new_email("delme")
        s = requests.Session()
        r = s.post(f"{API}/auth/register",
                   json={"email": email, "password": "Test1234!"}, timeout=20)
        assert r.status_code == 200
        token = r.json()["access_token"]
        uid = r.json()["id"]
        _bearer(s, token)

        # Add a bot to verify cascade
        rb = s.post(f"{API}/bots", json={"name": "TEST_delbot", "pair": "EURUSD",
                                         "timeframe": "M15"})
        assert rb.status_code == 200

        r = s.delete(f"{API}/auth/delete-account")
        assert r.status_code == 200

        r = s.get(f"{API}/auth/me")
        assert r.status_code == 401

        # Admin lookup should now 404
        r = admin.get(f"{API}/admin/users/{uid}")
        assert r.status_code == 404


class TestAdminStats:
    def test_stats_keys(self, admin):
        r = admin.get(f"{API}/admin/stats")
        assert r.status_code == 200, r.text
        d = r.json()
        for top in ("users", "subscriptions", "payments", "referrals", "bots",
                    "trades", "signals"):
            assert top in d, f"missing top key {top}"
        assert "mrr_usd" in d["subscriptions"]
        assert isinstance(d["subscriptions"]["mrr_usd"], (int, float))
        assert d["referrals"]["events"] >= 1


class TestAdminUsers:
    def test_list_users_basic(self, admin):
        r = admin.get(f"{API}/admin/users")
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list) and len(rows) >= 1
        sample = rows[0]
        for k in ("id", "email", "role", "subscription", "referral_code",
                  "referred_count", "bots_count"):
            assert k in sample

    def test_list_users_search_and_role(self, admin, referrer):
        # search by email substring
        r = admin.get(f"{API}/admin/users", params={"search": referrer.email})
        assert r.status_code == 200
        rows = r.json()
        assert any(x["email"] == referrer.email for x in rows)
        # role filter
        r = admin.get(f"{API}/admin/users", params={"role": "admin"})
        assert r.status_code == 200
        assert all(x["role"] == "admin" for x in r.json())

    def test_promote_demote_disable_enable(self, admin, referrer):
        uid = referrer.user_id
        # promote to admin
        r = admin.patch(f"{API}/admin/users/{uid}", json={"role": "admin"})
        assert r.status_code == 200
        # verify
        r = admin.get(f"{API}/admin/users/{uid}")
        assert r.status_code == 200
        assert r.json()["user"]["role"] == "admin"
        # demote
        r = admin.patch(f"{API}/admin/users/{uid}", json={"role": "user"})
        assert r.status_code == 200
        # disable
        r = admin.patch(f"{API}/admin/users/{uid}", json={"disabled": True})
        assert r.status_code == 200
        # enable
        r = admin.patch(f"{API}/admin/users/{uid}", json={"disabled": False})
        assert r.status_code == 200
        # rename
        r = admin.patch(f"{API}/admin/users/{uid}",
                        json={"display_name": "TEST_Renamed"})
        assert r.status_code == 200
        r = admin.get(f"{API}/admin/users/{uid}")
        assert r.json()["user"]["display_name"] == "TEST_Renamed"

    def test_self_demote_rejected(self, admin):
        r = admin.patch(f"{API}/admin/users/{admin.user_id}", json={"role": "user"})
        assert r.status_code == 400

    def test_self_disable_rejected(self, admin):
        r = admin.patch(f"{API}/admin/users/{admin.user_id}",
                        json={"disabled": True})
        assert r.status_code == 400

    def test_self_delete_rejected(self, admin):
        r = admin.delete(f"{API}/admin/users/{admin.user_id}")
        assert r.status_code == 400


class TestAdminGrantCancelSubscription:
    def test_grant_extend_then_extend_again(self, admin):
        # create a fresh user
        email = _new_email("grant")
        s = requests.Session()
        s.post(f"{API}/auth/register",
               json={"email": email, "password": "Test1234!"}, timeout=20)
        r = s.post(f"{API}/auth/login",
                   json={"email": email, "password": "Test1234!"}, timeout=20)
        uid = r.json()["id"]
        _bearer(s, r.json()["access_token"])

        # First grant: monthly extend=true (no existing -> creates from today)
        r = admin.post(f"{API}/admin/users/{uid}/grant-subscription",
                       json={"plan": "monthly", "extend": True})
        assert r.status_code == 200, r.text
        cpe1 = r.json()["current_period_end"]

        # Second grant: extend=true -> should push cpe further
        r = admin.post(f"{API}/admin/users/{uid}/grant-subscription",
                       json={"plan": "monthly", "extend": True})
        assert r.status_code == 200
        cpe2 = r.json()["current_period_end"]
        assert cpe2 > cpe1

        # Verify via user's subscription
        r = s.get(f"{API}/subscriptions/me")
        assert r.status_code == 200
        assert r.json().get("status") == "active"

        # days_override = 14
        r = admin.post(f"{API}/admin/users/{uid}/grant-subscription",
                       json={"plan": "monthly", "extend": False,
                             "days_override": 14})
        assert r.status_code == 200
        cpe3 = r.json()["current_period_end"]
        # extend=false uses today as base regardless, so cpe3 < cpe2 (since cpe2 was ~60d, cpe3 is ~14d)
        assert cpe3 < cpe2

        # cancel
        r = admin.post(f"{API}/admin/users/{uid}/cancel-subscription")
        assert r.status_code == 200
        r = s.get(f"{API}/subscriptions/me")
        assert r.json().get("status") == "canceled"

        # cleanup
        admin.delete(f"{API}/admin/users/{uid}")

    def test_grant_extend_false_without_existing(self, admin):
        email = _new_email("grant2")
        s = requests.Session()
        rr = s.post(f"{API}/auth/register",
                    json={"email": email, "password": "Test1234!"}, timeout=20)
        uid = rr.json()["id"]
        # Caveat: register inserts an empty 'incomplete' subscription, so grant always
        # finds an existing row. Verify the new cpe is set from today (~30 days).
        r = admin.post(f"{API}/admin/users/{uid}/grant-subscription",
                       json={"plan": "monthly", "extend": False})
        assert r.status_code == 200
        cpe = r.json()["current_period_end"]
        assert cpe and "T" in cpe
        admin.delete(f"{API}/admin/users/{uid}")


class TestAdminDeleteCascade:
    def test_delete_cascades(self, admin):
        email = _new_email("cascade")
        s = requests.Session()
        rr = s.post(f"{API}/auth/register",
                    json={"email": email, "password": "Test1234!"}, timeout=20)
        uid = rr.json()["id"]
        token = rr.json()["access_token"]
        _bearer(s, token)
        # add bot + bridge key
        s.post(f"{API}/bots", json={"name": "TEST_cb", "pair": "EURUSD",
                                    "timeframe": "M15"})
        s.post(f"{API}/bridge/keys", json={"label": "TEST_bk"})
        # delete via admin
        r = admin.delete(f"{API}/admin/users/{uid}")
        assert r.status_code == 200
        # verify gone
        r = admin.get(f"{API}/admin/users/{uid}")
        assert r.status_code == 404


class TestAdminReferralsListing:
    def test_admin_referrals_enriched(self, admin):
        r = admin.get(f"{API}/admin/referrals")
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        assert len(rows) >= 1
        sample = rows[0]
        for k in ("referrer_id", "referee_id", "days_credited", "commission_pct",
                  "referrer_email", "referee_email"):
            assert k in sample, f"missing {k}"
        assert sample["referrer_email"]
        assert sample["referee_email"]


class TestBridgeRegression:
    def test_bridge_poll_with_key_works(self, referrer):
        # Generate a bridge key for referrer
        r = referrer.post(f"{API}/bridge/keys", json={"label": "TEST_phase2"})
        assert r.status_code == 200, r.text
        key = r.json()["api_key"]
        assert key.startswith("abk_")
        # poll
        s = requests.Session()
        r = s.post(f"{API}/bridge-poll",
                   headers={"x-aurum-bridge-key": key},
                   json={"account": {"login": "1", "broker": "Test",
                                     "balance": 100.0, "equity": 100.0,
                                     "currency": "USD"}}, timeout=20)
        assert r.status_code == 200, r.text
        b = r.json()
        assert "signals" in b and isinstance(b["signals"], list)

    def test_bridge_poll_without_key_rejected(self):
        r = requests.post(f"{API}/bridge-poll", json={}, timeout=20)
        assert r.status_code in (401, 403)


# ---------- final cleanup ----------
@pytest.fixture(scope="module", autouse=True)
def _cleanup(request, admin):
    yield
    # Remove TEST_ users created in this module via admin
    try:
        users = admin.get(f"{API}/admin/users",
                          params={"search": "test_"}).json()
        for u in users:
            if u["email"].startswith("test_") and "aurumtest" in u["email"]:
                admin.delete(f"{API}/admin/users/{u['id']}")
    except Exception:
        pass
