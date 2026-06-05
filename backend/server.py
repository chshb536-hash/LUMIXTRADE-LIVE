"""
Aurum FX — FastAPI backend.
All routes are mounted under /api. UI/design of the frontend is preserved as-is.
"""
from __future__ import annotations

# ---- env must load first ----
from dotenv import load_dotenv
from pathlib import Path
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os
import re
import uuid
import json
import asyncio
import logging
import secrets
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Literal, Optional

import bcrypt
import jwt as pyjwt
import aiofiles
from fastapi import (
    FastAPI, APIRouter, HTTPException, Depends, Request, Response,
    UploadFile, File, Form, Query, Header,
)
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field, ConfigDict
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from engine import Candle, StrategyConfig, generate_signal, calc_lot, ema, atr, detect_regime
from marketdata import fetch_candles, fetch_price, store_candles, init_db as _md_init
from data_validation import validate_candles
from strategy_v2 import generate_signal_v2, StrategyV2Config, conservative_config
from risk_engine import adaptive_lot, volatility_gate, bridge_health
import notifications as notify_svc

# ---------- Config ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("aurum")

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
JWT_SECRET = os.environ.get("JWT_SECRET") or "CHANGE-ME-ONLY-FOR-DEV"
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = 60 * 24 * 7  # 7 days (matches UX of Supabase persistSession)
REFRESH_TOKEN_DAYS = 30
# Minimum bridge version that's allowed to receive signals. Older bridges still get a
# 200 OK heartbeat so they don't crash, but receive zero signals + a `warning` field.
MIN_BRIDGE_VERSION = "1.8.1"
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@aurumfx.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Mohyuddin@123")
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
# Allow lumixtrade.live and any *.emergent.host / *.emergentagent.com subdomain by default
CORS_ALLOW_REGEX = r"^https://([a-z0-9-]+\.)*(lumixtrade\.live|emergent\.host|emergentagent\.com)$"

UPLOAD_DIR = ROOT_DIR / "uploads" / "payment-proofs"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
QR_DIR = ROOT_DIR / "uploads" / "payment-qrs"
QR_DIR.mkdir(parents=True, exist_ok=True)

BRIDGE_SCRIPT_PATH = ROOT_DIR / "static" / "aurum_bridge.py"

# Mongo
client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

# Wire marketdata module to share the Motor db handle (MT5-bridge-native data flow)
_md_init(db)

# Strategy v2 toggle. Default ON — set STRATEGY_VERSION=v1 in .env to fall back.
STRATEGY_VERSION = os.environ.get("STRATEGY_VERSION", "v2").lower()
# v1.8 — Conservative live-forward preset. Default ON. Set STRATEGY_CONSERVATIVE=false
# in backend/.env when you want full A/B/C signal volume back.
_CONSERVATIVE = os.environ.get("STRATEGY_CONSERVATIVE", "true").lower() in ("1", "true", "yes", "on")
STRATEGY_V2_CFG = conservative_config() if _CONSERVATIVE else StrategyV2Config()
log.info("strategy_v2 config: conservative=%s · min_confidence=%.2f · require_displacement=%s · require_htf=%s",
         _CONSERVATIVE, STRATEGY_V2_CFG.min_confidence,
         STRATEGY_V2_CFG.require_displacement, STRATEGY_V2_CFG.require_htf_alignment)


# ---------- helpers: password, jwt, time ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


def create_access_token(user_id: str, email: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": now_utc() + timedelta(minutes=ACCESS_TOKEN_MINUTES),
        "type": "access",
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": now_utc() + timedelta(days=REFRESH_TOKEN_DAYS),
        "type": "refresh",
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def set_auth_cookies(resp: Response, access_token: str, refresh_token: str):
    resp.set_cookie(
        key="access_token", value=access_token, httponly=True,
        secure=True, samesite="none",
        max_age=ACCESS_TOKEN_MINUTES * 60, path="/",
    )
    resp.set_cookie(
        key="refresh_token", value=refresh_token, httponly=True,
        secure=True, samesite="none",
        max_age=REFRESH_TOKEN_DAYS * 86400, path="/",
    )


def clear_auth_cookies(resp: Response):
    resp.delete_cookie("access_token", path="/")
    resp.delete_cookie("refresh_token", path="/")


def user_to_public(u: dict) -> dict:
    return {
        "id": u["_id"],
        "email": u["email"],
        "display_name": u.get("display_name"),
        "role": u.get("role", "user"),
        "avatar_url": u.get("avatar_url"),
        "referral_code": u.get("referral_code"),
        "referred_by": u.get("referred_by"),
        "disabled": bool(u.get("disabled", False)),
        "created_at": u.get("created_at"),
    }


def generate_referral_code() -> str:
    import string
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


# ---------- Auth dependency ----------
async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user = await db.users.find_one({"_id": payload["sub"]}, {"password_hash": 0})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        if user.get("disabled"):
            raise HTTPException(status_code=403, detail="Account disabled")
        return user
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


# ---------- Models (request bodies) ----------
class RegisterBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    display_name: Optional[str] = None
    referral_code: Optional[str] = None


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class ProfileUpdateBody(BaseModel):
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None


class BotBody(BaseModel):
    name: str = "My Bot"
    pair: str = "XAUUSD"
    timeframe: str = "M15"
    risk_per_trade: float = 1.0
    max_positions: int = 2
    daily_loss_limit: float = 5.0
    sessions: Optional[List[str]] = None
    strategy_config: Optional[dict] = None
    higher_tf_confirmation: Optional[Literal["off", "H1", "H4"]] = "off"
    enable_scalping_in_ranges: bool = True


class BotPatch(BaseModel):
    name: Optional[str] = None
    pair: Optional[str] = None
    timeframe: Optional[str] = None
    risk_per_trade: Optional[float] = None
    max_positions: Optional[int] = None
    daily_loss_limit: Optional[float] = None
    sessions: Optional[List[str]] = None
    strategy_config: Optional[dict] = None
    is_active: Optional[bool] = None
    higher_tf_confirmation: Optional[Literal["off", "H1", "H4"]] = None
    enable_scalping_in_ranges: Optional[bool] = None


class BridgeKeyBody(BaseModel):
    label: str = "My Bridge"


class ApproveRejectBody(BaseModel):
    notes: Optional[str] = None


class PaymentInstructionsBody(BaseModel):
    monthly_price: float = 49
    quarterly_price: float = 129
    yearly_price: float = 449
    bank_details: Optional[str] = None
    usdt_trc20_address: Optional[str] = None
    usdt_erc20_address: Optional[str] = None
    btc_address: Optional[str] = None
    paypal_email: Optional[str] = None
    notes: Optional[str] = None
    referral_commission_pct: Optional[float] = None


# New flexible payment-method model — admin manages a list of payment methods,
# each with its own address/label/instructions and an optional QR image upload.
class PaymentMethodBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)             # e.g. "USDT BEP20"
    type: Literal[
        "usdt_trc20", "usdt_bep20", "usdt_erc20",
        "btc", "eth",
        "bank", "jazzcash", "easypaisa",
        "paypal", "other",
    ] = "other"
    address: Optional[str] = None                              # wallet / IBAN / phone / email
    label: Optional[str] = None                                # display name for the account
    instructions: Optional[str] = None                         # free-text below the address
    enabled: bool = True
    sort_order: int = 0


class RegisterBodyV2(BaseModel):
    referral_code: Optional[str] = None


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=72)


class GrantSubscriptionBody(BaseModel):
    plan: str  # monthly | quarterly | yearly
    extend: bool = True  # True = add to current; False = override
    days_override: Optional[int] = None  # if set, use this instead of plan default


class AdminUserPatch(BaseModel):
    role: Optional[str] = None  # admin | user
    disabled: Optional[bool] = None
    display_name: Optional[str] = None


# ---------- Constants ----------
DEFAULT_STRATEGY = {
    "ema_fast": 21, "ema_slow": 55, "rsi_period": 14,
    "atr_period": 14, "sl_atr": 1.5, "tp_atr": 2.5, "min_confidence": 0.4,
}
DEFAULT_SESSIONS = ["london", "new_york", "overlap"]
PLAN_DAYS = {"monthly": 30, "quarterly": 90, "yearly": 365}


# ---------- App + router ----------
app = FastAPI(title="Aurum FX API")
api = APIRouter(prefix="/api")


# ---------- Auth routes ----------
@api.post("/auth/register")
async def register(body: RegisterBody, response: Response):
    email = body.email.lower().strip()
    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    # Resolve referrer (if a valid code was provided)
    referred_by = None
    if body.referral_code:
        ref_user = await db.users.find_one(
            {"referral_code": body.referral_code.strip().upper()},
            {"_id": 1},
        )
        if ref_user:
            referred_by = ref_user["_id"]
    # Unique referral_code for this new user (retry on collision)
    for _ in range(5):
        code = generate_referral_code()
        if not await db.users.find_one({"referral_code": code}):
            break
    user_id = str(uuid.uuid4())
    doc = {
        "_id": user_id,
        "email": email,
        "password_hash": hash_password(body.password),
        "display_name": body.display_name or email.split("@")[0],
        "avatar_url": None,
        "role": "user",
        "disabled": False,
        "referral_code": code,
        "referred_by": referred_by,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    await db.users.insert_one(doc)
    # Create empty subscription row for tracking
    await db.subscriptions.insert_one({
        "_id": str(uuid.uuid4()),
        "user_id": user_id, "plan": None, "status": "incomplete",
        "current_period_end": None, "cancel_at_period_end": False,
        "created_at": now_iso(), "updated_at": now_iso(),
    })
    access = create_access_token(user_id, email, "user")
    refresh = create_refresh_token(user_id)
    set_auth_cookies(response, access, refresh)
    public = user_to_public(doc)
    public["access_token"] = access
    return public


@api.post("/auth/login")
async def login(body: LoginBody, response: Response, request: Request):
    email = body.email.lower().strip()
    ip = request.client.host if request.client else "?"
    identifier = f"{ip}:{email}"
    # brute force check
    attempts = await db.login_attempts.find_one({"_id": identifier})
    if attempts and attempts.get("count", 0) >= 5:
        locked_until = attempts.get("locked_until")
        if locked_until and datetime.fromisoformat(locked_until) > now_utc():
            raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in 15 min.")
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user["password_hash"]):
        new_count = (attempts.get("count", 0) if attempts else 0) + 1
        update = {"count": new_count, "last_attempt": now_iso()}
        if new_count >= 5:
            update["locked_until"] = (now_utc() + timedelta(minutes=15)).isoformat()
        await db.login_attempts.update_one(
            {"_id": identifier}, {"$set": update}, upsert=True,
        )
        raise HTTPException(status_code=401, detail="Invalid email or password")
    await db.login_attempts.delete_one({"_id": identifier})
    access = create_access_token(user["_id"], user["email"], user.get("role", "user"))
    refresh = create_refresh_token(user["_id"])
    set_auth_cookies(response, access, refresh)
    public = user_to_public(user)
    public["access_token"] = access
    return public


@api.post("/auth/logout")
async def logout(response: Response):
    clear_auth_cookies(response)
    return {"ok": True}


@api.get("/auth/me")
async def auth_me(user: dict = Depends(get_current_user)):
    return user_to_public(user)


# Aliases requested for production launch — same handlers, friendlier paths
@api.get("/health")
async def health():
    """Liveness probe — used by uptime monitors and custom-domain proxies."""
    return {"status": "ok", "service": "lumixtrade-api", "version": MIN_BRIDGE_VERSION}


@api.post("/auth/signup")
async def auth_signup(body: RegisterBody, response: Response):
    return await register(body, response)


@api.get("/auth/status")
async def auth_status(user: dict = Depends(get_current_user)):
    return user_to_public(user)


@api.post("/auth/refresh")
async def auth_refresh(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(status_code=401, detail="No refresh token")
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Bad token type")
        user = await db.users.find_one({"_id": payload["sub"]}, {"password_hash": 0})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        access = create_access_token(user["_id"], user["email"], user.get("role", "user"))
        new_refresh = create_refresh_token(user["_id"])
        set_auth_cookies(response, access, new_refresh)
        return user_to_public(user)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh expired")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")


# ---------- Profile ----------
@api.get("/profile")
async def get_profile(user: dict = Depends(get_current_user)):
    return user_to_public(user)


@api.patch("/profile")
async def patch_profile(body: ProfileUpdateBody, user: dict = Depends(get_current_user)):
    update = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if update:
        update["updated_at"] = now_iso()
        await db.users.update_one({"_id": user["_id"]}, {"$set": update})
    fresh = await db.users.find_one({"_id": user["_id"]}, {"password_hash": 0})
    return user_to_public(fresh)


# ---------- Subscriptions ----------
@api.get("/subscriptions/me")
async def subscription_me(user: dict = Depends(get_current_user)):
    sub = await db.subscriptions.find_one(
        {"user_id": user["_id"]}, {"_id": 0}, sort=[("updated_at", -1)],
    )
    return sub


# ---------- Bots ----------
def _bot_public(b: dict) -> dict:
    out = {k: v for k, v in b.items() if k != "_id"}
    out["id"] = b["_id"]
    return out


@api.get("/bots")
async def list_bots(user: dict = Depends(get_current_user)):
    rows = await db.bots.find({"user_id": user["_id"]}).sort("created_at", -1).to_list(500)
    return [_bot_public(b) for b in rows]


@api.post("/bots")
async def create_bot(body: BotBody, user: dict = Depends(get_current_user)):
    bot_id = str(uuid.uuid4())
    doc = {
        "_id": bot_id,
        "user_id": user["_id"],
        "name": body.name,
        "pair": body.pair,
        "timeframe": body.timeframe,
        "risk_per_trade": body.risk_per_trade,
        "max_positions": body.max_positions,
        "daily_loss_limit": body.daily_loss_limit,
        "sessions": body.sessions or DEFAULT_SESSIONS,
        "is_active": False,
        "strategy_config": body.strategy_config or DEFAULT_STRATEGY,
        "higher_tf_confirmation": body.higher_tf_confirmation or "off",
        "enable_scalping_in_ranges": bool(body.enable_scalping_in_ranges),
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    await db.bots.insert_one(doc)
    return _bot_public(doc)


@api.patch("/bots/{bot_id}")
async def update_bot(bot_id: str, body: BotPatch, user: dict = Depends(get_current_user)):
    update = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not update:
        raise HTTPException(400, "Nothing to update")
    update["updated_at"] = now_iso()
    r = await db.bots.update_one({"_id": bot_id, "user_id": user["_id"]}, {"$set": update})
    if r.matched_count == 0:
        raise HTTPException(404, "Bot not found")
    fresh = await db.bots.find_one({"_id": bot_id})
    return _bot_public(fresh)


@api.delete("/bots/{bot_id}")
async def delete_bot(bot_id: str, user: dict = Depends(get_current_user)):
    r = await db.bots.delete_one({"_id": bot_id, "user_id": user["_id"]})
    if r.deleted_count == 0:
        raise HTTPException(404, "Bot not found")
    return {"ok": True}


@api.post("/bots/{bot_id}/scan")
async def scan_one_bot(bot_id: str, user: dict = Depends(get_current_user)):
    bot = await db.bots.find_one({"_id": bot_id, "user_id": user["_id"]})
    if not bot:
        raise HTTPException(404, "Bot not found")
    created = await _scan_and_persist([bot])
    return {"ok": True, "scanned": 1, "created": created, "message": f"Scanned 1 bot, created {created} signal(s)."}


class StrategyConfigBody(BaseModel):
    ema_fast: Optional[int] = Field(default=None, ge=2, le=200)
    ema_slow: Optional[int] = Field(default=None, ge=2, le=400)
    rsi_period: Optional[int] = Field(default=None, ge=2, le=100)
    atr_period: Optional[int] = Field(default=None, ge=2, le=100)
    sl_atr: Optional[float] = Field(default=None, gt=0, le=10)
    tp_atr: Optional[float] = Field(default=None, gt=0, le=20)
    min_confidence: Optional[float] = Field(default=None, ge=0.0, le=0.99)


@api.patch("/bots/{bot_id}/strategy")
async def patch_bot_strategy(bot_id: str, body: StrategyConfigBody, user: dict = Depends(get_current_user)):
    """Merge-update a bot's strategy_config. Only owner (or admin) can edit."""
    bot = await db.bots.find_one({"_id": bot_id})
    if not bot:
        raise HTTPException(404, "Bot not found")
    if bot["user_id"] != user["_id"] and user.get("role") != "admin":
        raise HTTPException(403, "Forbidden")
    current = dict(bot.get("strategy_config") or DEFAULT_STRATEGY)
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "No fields to update")
    current.update(changes)
    await db.bots.update_one({"_id": bot_id}, {"$set": {"strategy_config": current, "updated_at": now_iso()}})
    return {"ok": True, "strategy_config": current}


@api.post("/bots/{bot_id}/backtest")
async def bot_backtest(bot_id: str, days: int = 90, user: dict = Depends(get_current_user)):
    """Run the live engine against `days` of Dukascopy history for this bot's pair/timeframe.
    Synchronous fetch is offloaded to a thread executor so the event loop stays free.
    Result is cached on the bot doc for persistence across page refresh.
    """
    bot = await db.bots.find_one({"_id": bot_id, "user_id": user["_id"]})
    if not bot:
        raise HTTPException(404, "Bot not found")
    days = max(7, min(int(days), 730))
    from backtest import BacktestArgs, run_backtest  # local import — keeps cold startup fast
    end = now_utc()
    start = end - timedelta(days=days)
    args = BacktestArgs(
        symbol=bot["pair"], timeframe=bot["timeframe"],
        start=start, end=end,
        initial_balance=10000.0,
        risk_per_trade=float(bot.get("risk_per_trade") or 1.0),
        min_confidence=0.5, max_bars_in_trade=240, warmup_bars=80,
    )
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, run_backtest, args)
    except SystemExit as e:
        raise HTTPException(400, str(e))
    summary = {
        "days": days,
        "symbol": result.symbol, "timeframe": result.timeframe,
        "start": result.start, "end": result.end,
        "total_trades": result.total_trades,
        "win_rate_pct": result.win_rate_pct,
        "profit_factor": result.profit_factor if isinstance(result.profit_factor, (int, float)) else None,
        "total_net_profit": result.total_net_profit,
        "max_drawdown_pct": result.max_drawdown_pct,
        "expectancy_usd": result.expectancy_usd,
        "sharpe_per_trade": result.sharpe_per_trade,
        "ran_at": now_iso(),
    }
    await db.bots.update_one({"_id": bot_id}, {"$set": {"last_backtest": summary}})
    return summary


# ---------- Signals & Trades ----------
@api.get("/signals")
async def list_signals(user: dict = Depends(get_current_user), limit: int = 200):
    # Auto-expire stale pending signals so dashboard reflects reality
    now = now_iso()
    await db.signals.update_many(
        {"user_id": user["_id"], "status": "pending", "expires_at": {"$lt": now}},
        {"$set": {"status": "expired"}},
    )
    rows = await db.signals.find({"user_id": user["_id"]}).sort("created_at", -1).limit(min(limit, 500)).to_list(limit)
    return [_strip_id(r) for r in rows]


@api.get("/trades")
async def list_trades(user: dict = Depends(get_current_user), limit: int = 500):
    rows = await db.trades.find({"user_id": user["_id"]}).sort("opened_at", -1).limit(min(limit, 1000)).to_list(limit)
    rows = [_strip_id(r) for r in rows]
    # Server-side fallback for live PnL — covers users running older bridge versions
    # that don't stream `positions[]` to /bridge-poll. Computes approximate floating PnL
    # from the latest market quote so the dashboard P&L column always shows a value.
    await _augment_open_trades_with_live_pnl(rows)
    return rows


# ---- Live PnL helpers (server-side fallback for older bridges) ----
_PRICE_CACHE: Dict[str, tuple] = {}  # pair -> (price, monotonic_ts)
_PRICE_TTL = 10.0  # seconds


async def _cached_price(pair: str) -> Optional[float]:
    import time as _time
    now = _time.monotonic()
    cached = _PRICE_CACHE.get(pair)
    if cached and (now - cached[1]) < _PRICE_TTL:
        return cached[0]
    try:
        p = await fetch_price(pair)
    except Exception:
        p = None
    if p is not None:
        _PRICE_CACHE[pair] = (p, now)
    return p


def _approx_pnl_usd(pair: str, side: str, entry: float, current: float, lot: float) -> Optional[float]:
    """Approximate floating PnL in USD for an MT5-style position.
    Standard contract sizes: FX = 100,000 base; XAU/USD = 100 oz; XAG/USD = 5,000 oz.
    Quote-USD pairs (XAUUSD, EURUSD, GBPUSD, AUDUSD, NZDUSD, XAGUSD): direct.
    USD-quote pairs (USDJPY, USDCAD, USDCHF): convert via current price.
    """
    if not entry or not current or not lot:
        return None
    direction = 1.0 if str(side).lower() == "buy" else -1.0
    diff = (current - entry) * direction
    pair = pair.upper()
    if pair == "XAUUSD":
        contract = 100.0  # ounces per lot
        return diff * contract * lot
    if pair == "XAGUSD":
        contract = 5000.0
        return diff * contract * lot
    contract = 100000.0  # standard FX lot
    if pair.endswith("USD"):
        return diff * contract * lot
    if pair.startswith("USD") and current > 0:
        return (diff * contract * lot) / current
    # Cross pairs not directly supported — return raw quote-currency value as best effort
    return diff * contract * lot


async def _augment_open_trades_with_live_pnl(rows: List[dict]) -> None:
    open_rows = [r for r in rows if r.get("status") == "open" and r.get("live_pnl") is None]
    if not open_rows:
        return
    pairs = list({r.get("pair") for r in open_rows if r.get("pair")})
    prices: Dict[str, Optional[float]] = {}
    # Fetch sequentially via the cache (Twelve Data is rate-limited; cache covers repeat calls)
    for p in pairs:
        prices[p] = await _cached_price(p)
    for r in open_rows:
        cp = prices.get(r.get("pair"))
        if cp is None:
            continue
        try:
            pnl = _approx_pnl_usd(r["pair"], r.get("side", "buy"), float(r.get("entry") or 0), float(cp), float(r.get("lot") or 0))
        except Exception:
            pnl = None
        if pnl is None:
            continue
        r["live_pnl"] = round(pnl, 2)
        r["live_price"] = cp
        r["live_pnl_source"] = "server_estimate"


# ---------- Bridge (user-facing) ----------
@api.get("/bridge/keys")
async def list_bridge_keys(user: dict = Depends(get_current_user)):
    rows = await db.bridge_keys.find({"user_id": user["_id"]}).sort("created_at", -1).to_list(200)
    return [_strip_id(r) for r in rows]


@api.post("/bridge/keys")
async def create_bridge_key(body: BridgeKeyBody, user: dict = Depends(get_current_user)):
    key_id = str(uuid.uuid4())
    api_key = "abk_" + uuid.uuid4().hex + secrets.token_hex(8)
    doc = {
        "_id": key_id,
        "user_id": user["_id"],
        "mt5_account_id": None,
        "api_key": api_key,
        "label": body.label,
        "last_seen_at": None,
        "revoked": False,
        "created_at": now_iso(),
    }
    await db.bridge_keys.insert_one(doc)
    return _strip_id(doc)


@api.post("/bridge/keys/{key_id}/revoke")
async def revoke_bridge_key(key_id: str, user: dict = Depends(get_current_user)):
    r = await db.bridge_keys.update_one(
        {"_id": key_id, "user_id": user["_id"]}, {"$set": {"revoked": True}},
    )
    if r.matched_count == 0:
        raise HTTPException(404, "Key not found")
    return {"ok": True}


@api.get("/mt5-accounts")
async def list_mt5_accounts(user: dict = Depends(get_current_user)):
    rows = await db.mt5_accounts.find({"user_id": user["_id"]}).sort("created_at", -1).to_list(50)
    # Join with bridge_keys so the UI can show "v1.4 OK" / "v1.2 — update available"
    keys = await db.bridge_keys.find(
        {"user_id": user["_id"]},
        {"mt5_account_id": 1, "bridge_version": 1, "last_seen_at": 1, "revoked": 1},
    ).to_list(200)
    # For each MT5 account, take the most-recent (by last_seen_at) non-revoked key referencing it
    by_acct: Dict[str, dict] = {}
    for k in keys:
        if k.get("revoked"):
            continue
        aid = k.get("mt5_account_id")
        if not aid:
            continue
        prev = by_acct.get(aid)
        if not prev or (k.get("last_seen_at") or "") > (prev.get("last_seen_at") or ""):
            by_acct[aid] = k
    out = []
    for r in rows:
        d = _strip_id(r)
        k = by_acct.get(r["_id"])
        d["bridge_version"] = (k or {}).get("bridge_version") or None
        d["bridge_outdated"] = bool(
            (k or {}).get("bridge_version")
            and tuple(int(x) for x in str(k["bridge_version"]).split(".") if x.isdigit())
                < tuple(int(x) for x in MIN_BRIDGE_VERSION.split(".") if x.isdigit())
        )
        d["min_bridge_version"] = MIN_BRIDGE_VERSION
        out.append(d)
    return out


@api.get("/risk/me")
async def risk_me(user: dict = Depends(get_current_user)):
    """Weekly equity drawdown health for the dashboard tile.
    Always returns a payload — never 404 — even if the user has no equity yet.
    """
    state = await _user_drawdown_state(user["_id"])
    return {
        "week_start":      _utc_week_start().isoformat(),
        "week_high":       round(float(state.get("week_high") or 0), 2),
        "current_equity":  round(float(state.get("current_equity") or 0), 2),
        "drawdown_pct":    round(float(state.get("drawdown_pct") or 0), 2),
        "health_pct":      round(max(0.0, 100.0 - float(state.get("drawdown_pct") or 0)), 2),
        "halted":          bool(state.get("halted")),
        "halt_reason":     state.get("reason") if state.get("halted") else None,
        "halt_threshold":  DRAWDOWN_HALT_PERCENT,
    }


@api.get("/equity-curve")
async def equity_curve(user: dict = Depends(get_current_user), days: int = 30):
    """Daily cumulative realized PnL series for the dashboard chart.
    Returns at most `days` daily points. First point = $0 cumulative on the earliest
    closed-trade day (or today if no trades yet). Latest point includes today's PnL.
    """
    days = max(7, min(int(days), 365))
    cutoff = (now_utc() - timedelta(days=days)).isoformat()
    rows = await db.trades.find(
        {"user_id": user["_id"], "status": "closed", "closed_at": {"$gte": cutoff}},
        {"pnl": 1, "closed_at": 1},
    ).sort("closed_at", 1).to_list(5000)
    if not rows:
        today = _utc_day_start().date().isoformat()
        return {"points": [{"date": today, "pnl": 0.0, "cumulative": 0.0, "trades": 0}],
                "total_pnl": 0.0, "best_day": None, "worst_day": None}

    by_day: Dict[str, dict] = {}
    for r in rows:
        d = str(r.get("closed_at") or "")[:10]
        if not d:
            continue
        slot = by_day.setdefault(d, {"pnl": 0.0, "trades": 0})
        slot["pnl"] += float(r.get("pnl") or 0)
        slot["trades"] += 1
    ordered = sorted(by_day.items())
    cumulative = 0.0
    points = []
    best = None
    worst = None
    for d, v in ordered:
        cumulative += v["pnl"]
        points.append({
            "date": d,
            "pnl": round(v["pnl"], 2),
            "cumulative": round(cumulative, 2),
            "trades": v["trades"],
        })
        if best is None or v["pnl"] > best["pnl"]:
            best = {"date": d, "pnl": round(v["pnl"], 2)}
        if worst is None or v["pnl"] < worst["pnl"]:
            worst = {"date": d, "pnl": round(v["pnl"], 2)}
    return {
        "points": points,
        "total_pnl": round(cumulative, 2),
        "best_day": best,
        "worst_day": worst,
    }


# ---------- Bridge (bridge-facing, authenticates with X-Aurum-Bridge-Key) ----------
async def _bridge_key_auth(request: Request) -> dict:
    key = request.headers.get("x-aurum-bridge-key")
    if not key:
        raise HTTPException(401, "Missing bridge key")
    row = await db.bridge_keys.find_one({"api_key": key, "revoked": False})
    if not row:
        raise HTTPException(401, "Invalid bridge key")
    return row


@api.post("/bridge-poll")
async def bridge_poll(request: Request):
    """MT5 bridge polls here every ~5s.
    Auth: x-aurum-bridge-key header.
    Body: {"account": {login, server, broker, currency, balance, equity, margin, free_margin}}
    Returns: {"signals": [{id, pair, side, lot, sl, tp}]} (and also includes 'entry' for convenience).
    Signals are marked 'sent' on return so they are not re-issued.
    """
    key_row = await _bridge_key_auth(request)
    user_id = key_row["user_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    a = (body or {}).get("account") or {}
    if a.get("login") and a.get("server"):
        existing = await db.mt5_accounts.find_one({
            "user_id": user_id, "login": str(a["login"]),
        })
        payload = {
            "user_id": user_id,
            "login": str(a["login"]),
            "server": str(a["server"]),
            "broker": a.get("broker"),
            "currency": a.get("currency") or "USD",
            "balance": float(a.get("balance") or 0),
            "equity": float(a.get("equity") or 0),
            "margin": float(a.get("margin") or 0),
            "free_margin": float(a.get("free_margin") or 0),
            "is_connected": True,
            "last_heartbeat_at": now_iso(),
            "updated_at": now_iso(),
        }
        if existing:
            await db.mt5_accounts.update_one({"_id": existing["_id"]}, {"$set": payload})
            acct_id = existing["_id"]
        else:
            acct_id = str(uuid.uuid4())
            payload["_id"] = acct_id
            payload["created_at"] = now_iso()
            await db.mt5_accounts.insert_one(payload)
            if not key_row.get("mt5_account_id"):
                await db.bridge_keys.update_one(
                    {"_id": key_row["_id"]}, {"$set": {"mt5_account_id": acct_id}},
                )
    await db.bridge_keys.update_one(
        {"_id": key_row["_id"]}, {"$set": {"last_seen_at": now_iso()}},
    )
    # Live PnL streaming: bridge sends open-position snapshots; persist on the matching trade rows.
    positions = (body or {}).get("positions") or []
    if positions:
        for p in positions:
            try:
                ticket = int(p.get("ticket") or 0)
                if not ticket:
                    continue
                live_pnl_now = float(p.get("profit") or 0)
                upd = {
                    "live_pnl": live_pnl_now,
                    "live_swap": float(p.get("swap") or 0),
                    "live_commission": float(p.get("commission") or 0),
                    "live_price": float(p.get("price_current") or 0),
                    "live_at": now_iso(),
                }
                # Reflect any SL/TP changes from trade-management trailing
                if p.get("sl") is not None:
                    upd["sl"] = float(p["sl"])
                if p.get("tp") is not None:
                    upd["tp"] = float(p["tp"])
                # Atomically update MFE/MAE — clamp on extremes so the journal shows true highs/lows
                await db.trades.update_one(
                    {"user_id": user_id, "mt5_ticket": ticket, "status": "open"},
                    [{"$set": {**upd,
                               "mfe_pnl": {"$max": [{"$ifNull": ["$mfe_pnl", 0]}, live_pnl_now]},
                               "mae_pnl": {"$min": [{"$ifNull": ["$mae_pnl", 0]}, live_pnl_now]}}}],
                )
            except Exception:
                continue
    # Bridge version gating — refuse to dispatch signals to outdated bridges, but still
    # accept the heartbeat so the bridge stays "connected" in the UI and the warning
    # surfaces in its log. Bridges that don't send a version are treated as outdated.
    bridge_version = str((body or {}).get("version") or "")

    def _ver_tuple(v: str) -> tuple:
        try:
            return tuple(int(x) for x in v.split(".") if x.isdigit())
        except Exception:
            return ()
    outdated = (not bridge_version) or (_ver_tuple(bridge_version) < _ver_tuple(MIN_BRIDGE_VERSION))
    if bridge_version:
        await db.bridge_keys.update_one(
            {"_id": key_row["_id"]}, {"$set": {"bridge_version": bridge_version}},
        )
    # Fetch pending signals (not expired) — marked sent atomically so never re-issued.
    if outdated:
        return {
            "signals": [],
            "warning": "bridge_outdated",
            "min_version": MIN_BRIDGE_VERSION,
            "your_version": bridge_version or "unknown",
            "message": f"Aurum bridge {bridge_version or '<old>'} is below minimum {MIN_BRIDGE_VERSION}. Re-download aurum_bridge.py from the BRIDGE page.",
        }
    now = now_iso()
    cursor = db.signals.find({
        "user_id": user_id,
        "status": "pending",
        "expires_at": {"$gte": now},
    }).sort("created_at", 1).limit(10)
    rows = await cursor.to_list(10)
    if rows:
        ids = [r["_id"] for r in rows]
        await db.signals.update_many(
            {"_id": {"$in": ids}},
            {"$set": {"status": "sent", "picked_at": now_iso()}},
        )
    out = [{
        "id": r["_id"],
        "pair": r["pair"],
        "side": r["side"],
        "lot": r["lot"],
        "sl": r["sl"],
        "tp": r["tp"],
        "entry": r["entry"],            # extra (bridge may reference; ignored if unused)
        "mode": r.get("mode", "swing"), # "swing" | "scalp"
        "max_hold_minutes": int(r.get("max_hold_minutes") or 0),
    } for r in rows]
    return {"signals": out}


@api.post("/bridge-report")
async def bridge_report(request: Request):
    """MT5 bridge reports trade execution events.
    Auth: x-aurum-bridge-key header.
    Body: {"event": "fill"|"close"|"reject", "signal_id"?, "ticket"?, "pair"?, "side"?, "lot"?, "entry"?, "exit_price"?, "pnl"?, "commission"?, "swap"?, "reason"?}
    Returns: {"status": "ok"}.
    """
    key_row = await _bridge_key_auth(request)
    user_id = key_row["user_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    event = (body or {}).get("event")
    if not event:
        raise HTTPException(400, "event required")

    if event == "fill":
        signal_id = body.get("signal_id")
        bot_id = None
        sig_doc = None
        if signal_id:
            sig_doc = await db.signals.find_one({"_id": signal_id})
            if sig_doc:
                bot_id = sig_doc.get("bot_id")
            await db.signals.update_one({"_id": signal_id}, {"$set": {"status": "filled"}})
        trade_id = str(uuid.uuid4())
        initial_sl = float(body["sl"]) if body.get("sl") is not None else None
        initial_tp = float(body["tp"]) if body.get("tp") is not None else None
        await db.trades.insert_one({
            "_id": trade_id,
            "user_id": user_id, "bot_id": bot_id, "signal_id": signal_id,
            "mt5_ticket": int(body["ticket"]) if body.get("ticket") else None,
            "pair": body.get("pair"), "side": body.get("side"),
            "lot": float(body.get("lot") or 0),
            "entry": float(body.get("entry") or 0),
            "sl": initial_sl, "tp": initial_tp,
            # Snapshot of the original SL/TP for the journal — these never get rewritten
            # when the bridge trails the live `sl`/`tp` fields above.
            "initial_sl": initial_sl, "initial_tp": initial_tp,
            # Denormalised signal context for the journal
            "signal_reason": (sig_doc or {}).get("reason"),
            "confidence":    (sig_doc or {}).get("confidence"),
            "regime":        (sig_doc or {}).get("regime"),
            "session":       (sig_doc or {}).get("session"),
            # v1.6 execution telemetry (slippage / spread at fill / requested vs got)
            "slippage_pips":  float(body.get("slippage_pips") or 0),
            "spread_at_fill": float(body.get("spread_at_fill") or 0),
            "requested_price": float(body.get("requested_price") or 0),
            "exit_price": None, "pnl": None,
            "commission": 0.0, "swap": 0.0,
            "status": "open",
            "mfe_pnl": 0.0, "mae_pnl": 0.0,  # max favourable / adverse excursion in USD
            "exit_reason": None,
            "opened_at": now_iso(),
            "closed_at": None,
        })
        # Telegram fill alert
        try:
            notify_svc.notify("fill", pair=body.get("pair"), side=body.get("side"),
                              price=float(body.get("entry") or 0),
                              lot=float(body.get("lot") or 0),
                              ticket=int(body["ticket"]) if body.get("ticket") else None)
        except Exception:
            pass
        return {"status": "ok"}

    if event == "close":
        ticket = body.get("ticket")
        if not ticket:
            raise HTTPException(400, "ticket required")
        tr = await db.trades.find_one({"user_id": user_id, "mt5_ticket": int(ticket)})
        if not tr:
            raise HTTPException(404, "Trade not found")
        # Best-effort exit-reason classification.
        exit_price = float(body["exit_price"]) if body.get("exit_price") is not None else None
        exit_reason = body.get("reason") or None
        # Phase-1: trust explicit force-close reasons from the bridge (max_hold, profit_lock,
        # manager_close, trail). Only fall back to SL/TP-distance heuristic when no reason was sent.
        _force_reasons = {"max_hold", "profit_lock", "manager_close", "trail"}
        if not exit_reason and exit_price is not None:
            isl = tr.get("initial_sl")
            itp = tr.get("initial_tp")
            if isl is not None and itp is not None:
                # Compare proximity to original SL vs TP using relative distance.
                d_sl = abs(exit_price - float(isl))
                d_tp = abs(exit_price - float(itp))
                exit_reason = "tp_hit" if d_tp < d_sl else "sl_hit"
        await db.trades.update_one({"_id": tr["_id"]}, {"$set": {
            "status": "closed",
            "exit_price": exit_price,
            "pnl": float(body["pnl"]) if body.get("pnl") is not None else None,
            "commission": float(body.get("commission") or 0),
            "swap": float(body.get("swap") or 0),
            "exit_reason": exit_reason,
            "closed_at": now_iso(),
        }})
        # Telegram alert on TP/SL
        try:
            ev = "tp_hit" if exit_reason == "tp_hit" else ("sl_hit" if exit_reason == "sl_hit" else None)
            if ev:
                notify_svc.notify(ev, pair=tr.get("pair"), side=tr.get("side"),
                                  pnl=float(body.get("pnl") or 0),
                                  exit_price=exit_price, ticket=tr.get("mt5_ticket"))
        except Exception:
            pass
        return {"status": "ok"}

    if event == "reject":
        sid = body.get("signal_id")
        if sid:
            await db.signals.update_one(
                {"_id": sid},
                {"$set": {"status": "rejected", "reason": body.get("reason") or "rejected by broker"}},
            )
        return {"status": "ok"}

    if event == "candles":
        # Bridge can stream OHLC bars alongside heartbeats: {"event":"candles", "pair":"XAUUSD", "timeframe":"M15", "rows":[{t,o,h,l,c}, ...]}
        pair = body.get("pair")
        tf   = body.get("timeframe")
        rows = body.get("rows") or []
        if not (pair and tf and rows):
            raise HTTPException(400, "pair, timeframe, rows required")
        written = await store_candles(pair, tf, rows)
        return {"status": "ok", "written": written}

    raise HTTPException(400, "unknown event")


@api.get("/bridge/download")
async def bridge_download(user: dict = Depends(get_current_user)):
    """Bridge file is gated behind an active subscription."""
    sub = await db.subscriptions.find_one({"user_id": user["_id"]}, sort=[("updated_at", -1)])
    now = now_iso()
    active = (
        sub
        and sub.get("status") in ("active", "trialing")
        and (sub.get("current_period_end") or "") >= now
    )
    if not active:
        # 402 Payment Required — frontend uses this to redirect to /app/billing
        raise HTTPException(status_code=402, detail="Active subscription required to download the bridge")
    if not BRIDGE_SCRIPT_PATH.exists():
        raise HTTPException(404, "Bridge script not found")
    return FileResponse(
        path=str(BRIDGE_SCRIPT_PATH),
        media_type="text/x-python",
        filename="aurum_bridge.py",
    )


# ---------- Price cache ----------
@api.get("/price-cache")
async def price_cache(symbols: str = "", limit: int = 200):
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    q: Dict[str, Any] = {}
    if syms:
        q["symbol"] = {"$in": syms}
    rows = await db.price_cache.find(q, {"_id": 0}).sort("ts", -1).limit(min(limit, 500)).to_list(limit)
    return rows


# ---------- Payment instructions (public read for authed users; admin write) ----------
@api.get("/payment-instructions")
async def get_payment_instructions(user: dict = Depends(get_current_user)):
    doc = await db.payment_instructions.find_one({"_id": "main"}, {"_id": 0})
    if not doc:
        # seed default
        doc = {
            "monthly_price": 49, "quarterly_price": 129, "yearly_price": 449,
            "bank_details": None, "usdt_trc20_address": None, "usdt_erc20_address": None,
            "btc_address": None, "paypal_email": None, "notes": None,
            "updated_at": now_iso(),
        }
        await db.payment_instructions.insert_one({**doc, "_id": "main"})
    return doc


@api.put("/admin/payment-instructions")
async def update_payment_instructions(body: PaymentInstructionsBody, admin: dict = Depends(get_current_admin)):
    data = body.model_dump()
    data["updated_at"] = now_iso()
    await db.payment_instructions.update_one(
        {"_id": "main"}, {"$set": data}, upsert=True,
    )
    return {"ok": True}


# ---------- Payment Methods (admin-managed list with optional QR uploads) ----------
def _public_method(doc: dict) -> dict:
    d = _strip_id(doc)
    qr = doc.get("qr_filename")
    d["qr_url"] = f"/api/payment-methods/qr/{qr}" if qr else None
    d["id"] = doc["_id"]
    return d


@api.get("/payment-methods")
async def list_payment_methods(user: dict = Depends(get_current_user)):
    """Visible to logged-in users: only enabled methods, in sort_order."""
    rows = await db.payment_methods.find({"enabled": True}).sort([("sort_order", 1), ("created_at", 1)]).to_list(50)
    return [_public_method(r) for r in rows]


@api.get("/admin/payment-methods")
async def admin_list_payment_methods(admin: dict = Depends(get_current_admin)):
    rows = await db.payment_methods.find({}).sort([("sort_order", 1), ("created_at", 1)]).to_list(200)
    return [_public_method(r) for r in rows]


@api.post("/admin/payment-methods")
async def admin_create_payment_method(body: PaymentMethodBody, admin: dict = Depends(get_current_admin)):
    pm_id = str(uuid.uuid4())
    doc = {**body.model_dump(), "_id": pm_id, "qr_filename": None,
           "created_at": now_iso(), "updated_at": now_iso()}
    await db.payment_methods.insert_one(doc)
    return _public_method(doc)


@api.patch("/admin/payment-methods/{pm_id}")
async def admin_update_payment_method(pm_id: str, body: PaymentMethodBody, admin: dict = Depends(get_current_admin)):
    res = await db.payment_methods.find_one_and_update(
        {"_id": pm_id},
        {"$set": {**body.model_dump(), "updated_at": now_iso()}},
        return_document=True,
    )
    if not res:
        raise HTTPException(404, "Method not found")
    return _public_method(res)


@api.delete("/admin/payment-methods/{pm_id}")
async def admin_delete_payment_method(pm_id: str, admin: dict = Depends(get_current_admin)):
    doc = await db.payment_methods.find_one({"_id": pm_id})
    if not doc:
        raise HTTPException(404, "Method not found")
    qr = doc.get("qr_filename")
    if qr:
        try:
            (QR_DIR / qr).unlink(missing_ok=True)
        except Exception:
            pass
    await db.payment_methods.delete_one({"_id": pm_id})
    return {"ok": True}


@api.post("/admin/payment-methods/{pm_id}/qr")
async def admin_upload_qr(pm_id: str, qr: UploadFile = File(...), admin: dict = Depends(get_current_admin)):
    doc = await db.payment_methods.find_one({"_id": pm_id})
    if not doc:
        raise HTTPException(404, "Method not found")
    ctype = (qr.content_type or "").lower()
    if not ctype.startswith("image/"):
        raise HTTPException(400, "QR must be an image (png/jpg/webp)")
    raw = await qr.read()
    if not raw or len(raw) > 3 * 1024 * 1024:
        raise HTTPException(400, "QR image too large (max 3 MB) or empty")
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(ctype, ".png")
    # Remove any previous QR file for this method to avoid orphans
    old = doc.get("qr_filename")
    if old:
        try:
            (QR_DIR / old).unlink(missing_ok=True)
        except Exception:
            pass
    fname = f"{pm_id}{ext}"
    (QR_DIR / fname).write_bytes(raw)
    await db.payment_methods.update_one({"_id": pm_id}, {"$set": {"qr_filename": fname, "updated_at": now_iso()}})
    return {"ok": True, "qr_url": f"/api/payment-methods/qr/{fname}"}


@api.delete("/admin/payment-methods/{pm_id}/qr")
async def admin_delete_qr(pm_id: str, admin: dict = Depends(get_current_admin)):
    doc = await db.payment_methods.find_one({"_id": pm_id})
    if not doc:
        raise HTTPException(404, "Method not found")
    qr = doc.get("qr_filename")
    if qr:
        try:
            (QR_DIR / qr).unlink(missing_ok=True)
        except Exception:
            pass
    await db.payment_methods.update_one({"_id": pm_id}, {"$set": {"qr_filename": None, "updated_at": now_iso()}})
    return {"ok": True}


@api.get("/payment-methods/qr/{filename}")
async def serve_qr(filename: str):
    """Public-but-authed image serve. We don't require auth here because <img src>
    can't send Authorization headers; the URL itself is unguessable (UUID-prefixed)."""
    # Defense in depth: only allow names that match our own pattern.
    if not re.match(r"^[a-f0-9-]{36}\.(png|jpg|jpeg|webp|gif)$", filename, flags=re.I):
        raise HTTPException(404, "Not found")
    p = QR_DIR / filename
    if not p.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(str(p))


# ---------- Payment submissions ----------
@api.post("/payments/submit")
async def submit_payment(
    plan: str = Form(...),
    amount: float = Form(...),
    currency: str = Form("USD"),
    method: str = Form(...),
    txn_reference: str = Form(...),
    notes: Optional[str] = Form(None),
    screenshot: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    if plan not in PLAN_DAYS:
        raise HTTPException(400, "Invalid plan")
    if screenshot.size and screenshot.size > 5 * 1024 * 1024:
        raise HTTPException(400, "Screenshot must be < 5MB")
    # store file under uploads/payment-proofs/<user_id>/<uuid>.<ext>
    ext = (screenshot.filename or "png").rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "webp", "gif", "bmp"):
        ext = "png"
    user_dir = UPLOAD_DIR / user["_id"]
    user_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}.{ext}"
    fpath = user_dir / fname
    async with aiofiles.open(fpath, "wb") as f:
        await f.write(await screenshot.read())
    sub_id = str(uuid.uuid4())
    await db.payment_submissions.insert_one({
        "_id": sub_id,
        "user_id": user["_id"],
        "plan": plan, "amount": amount, "currency": currency,
        "method": method, "txn_reference": txn_reference.strip(),
        "screenshot_path": str(fpath.relative_to(ROOT_DIR)),
        "screenshot_filename": fname,
        "screenshot_mime": screenshot.content_type or "image/png",
        "notes": (notes or "").strip() or None,
        "status": "pending",
        "reviewed_by": None, "reviewed_at": None, "review_notes": None,
        "created_at": now_iso(), "updated_at": now_iso(),
    })
    return {"ok": True, "id": sub_id}


@api.get("/payments/submissions")
async def list_my_submissions(user: dict = Depends(get_current_user)):
    rows = await db.payment_submissions.find(
        {"user_id": user["_id"]},
        {"screenshot_path": 0, "screenshot_mime": 0},
    ).sort("created_at", -1).to_list(200)
    return [_strip_id(r) for r in rows]


# ---------- Admin: payment submissions ----------
@api.get("/admin/payments")
async def admin_list_payments(admin: dict = Depends(get_current_admin)):
    rows = await db.payment_submissions.find().sort("created_at", -1).to_list(500)
    # enrich with user display name
    user_ids = list({r["user_id"] for r in rows})
    users = await db.users.find({"_id": {"$in": user_ids}}, {"email": 1, "display_name": 1}).to_list(len(user_ids))
    umap = {u["_id"]: u for u in users}
    out = []
    for r in rows:
        obj = _strip_id(r)
        obj.pop("screenshot_path", None)
        obj.pop("screenshot_mime", None)
        obj["has_screenshot"] = bool(r.get("screenshot_path"))
        u = umap.get(r["user_id"]) or {}
        obj["user_email"] = u.get("email")
        obj["user_display_name"] = u.get("display_name")
        out.append(obj)
    return out


@api.get("/admin/payments/{sub_id}/proof")
async def admin_get_proof(sub_id: str, admin: dict = Depends(get_current_admin)):
    sub = await db.payment_submissions.find_one({"_id": sub_id})
    if not sub:
        raise HTTPException(404, "Not found")
    path = sub.get("screenshot_path")
    if not path:
        raise HTTPException(404, "No screenshot")
    abs_path = ROOT_DIR / path
    if not abs_path.exists():
        raise HTTPException(404, "File missing")
    return FileResponse(str(abs_path), media_type=sub.get("screenshot_mime") or "image/png")


@api.post("/admin/payments/{sub_id}/approve")
async def admin_approve(sub_id: str, body: ApproveRejectBody, admin: dict = Depends(get_current_admin)):
    sub = await db.payment_submissions.find_one({"_id": sub_id})
    if not sub:
        raise HTTPException(404, "Not found")
    if sub["status"] != "pending":
        raise HTTPException(400, f"Submission already {sub['status']}")
    plan = sub["plan"]
    days = PLAN_DAYS[plan]
    existing = await db.subscriptions.find_one(
        {"user_id": sub["user_id"]}, sort=[("updated_at", -1)],
    )
    now = now_utc()
    cpe_existing = None
    if existing and existing.get("current_period_end"):
        try:
            cpe_existing = datetime.fromisoformat(existing["current_period_end"])
        except Exception:
            cpe_existing = None
    base = max(cpe_existing or now, now)
    new_cpe = (base + timedelta(days=days)).isoformat()
    if existing:
        await db.subscriptions.update_one(
            {"_id": existing["_id"]},
            {"$set": {
                "plan": plan, "status": "active",
                "current_period_end": new_cpe,
                "cancel_at_period_end": False,
                "updated_at": now_iso(),
            }},
        )
    else:
        await db.subscriptions.insert_one({
            "_id": str(uuid.uuid4()),
            "user_id": sub["user_id"], "plan": plan, "status": "active",
            "current_period_end": new_cpe, "cancel_at_period_end": False,
            "created_at": now_iso(), "updated_at": now_iso(),
        })
    await db.payment_submissions.update_one(
        {"_id": sub_id},
        {"$set": {
            "status": "approved", "reviewed_by": admin["_id"],
            "reviewed_at": now_iso(), "review_notes": body.notes,
            "updated_at": now_iso(),
        }},
    )
    # --- Referral commission (direct, 1-level) ---
    await _credit_referrer_for_payment(sub, plan, days)
    return {"ok": True}


async def _credit_referrer_for_payment(sub: dict, plan: str, plan_days: int):
    """If the paying user was referred by someone, extend the referrer's
    subscription by (commission_pct × plan_days) days and log a referral_event."""
    user = await db.users.find_one({"_id": sub["user_id"]}, {"referred_by": 1})
    if not user or not user.get("referred_by"):
        return
    referrer_id = user["referred_by"]
    instr = await db.payment_instructions.find_one({"_id": "main"}) or {}
    pct = float(instr.get("referral_commission_pct") or 10.0)
    days_credit = max(1, round(plan_days * pct / 100))
    # Extend referrer's sub (only if they have one; otherwise create a monthly-style credit row)
    rsub = await db.subscriptions.find_one({"user_id": referrer_id}, sort=[("updated_at", -1)])
    now = now_utc()
    cpe = None
    if rsub and rsub.get("current_period_end"):
        try:
            cpe = datetime.fromisoformat(rsub["current_period_end"])
        except Exception:
            cpe = None
    base = max(cpe or now, now)
    new_cpe = (base + timedelta(days=days_credit)).isoformat()
    if rsub:
        await db.subscriptions.update_one(
            {"_id": rsub["_id"]},
            {"$set": {
                "status": "active",
                "plan": rsub.get("plan") or plan,
                "current_period_end": new_cpe,
                "cancel_at_period_end": False,
                "updated_at": now_iso(),
            }},
        )
    else:
        await db.subscriptions.insert_one({
            "_id": str(uuid.uuid4()),
            "user_id": referrer_id, "plan": plan, "status": "active",
            "current_period_end": new_cpe, "cancel_at_period_end": False,
            "created_at": now_iso(), "updated_at": now_iso(),
        })
    await db.referral_events.insert_one({
        "_id": str(uuid.uuid4()),
        "referrer_id": referrer_id,
        "referee_id": sub["user_id"],
        "submission_id": sub["_id"],
        "plan": plan,
        "plan_amount": float(sub.get("amount") or 0),
        "commission_pct": pct,
        "days_credited": days_credit,
        "created_at": now_iso(),
    })


@api.post("/admin/payments/{sub_id}/reject")
async def admin_reject(sub_id: str, body: ApproveRejectBody, admin: dict = Depends(get_current_admin)):
    sub = await db.payment_submissions.find_one({"_id": sub_id})
    if not sub:
        raise HTTPException(404, "Not found")
    if sub["status"] != "pending":
        raise HTTPException(400, f"Submission already {sub['status']}")
    await db.payment_submissions.update_one(
        {"_id": sub_id},
        {"$set": {
            "status": "rejected", "reviewed_by": admin["_id"],
            "reviewed_at": now_iso(), "review_notes": body.notes,
            "updated_at": now_iso(),
        }},
    )
    return {"ok": True}


# ---------- Referrals (user-facing) ----------
@api.get("/referrals/me")
async def referrals_me(user: dict = Depends(get_current_user)):
    events = await db.referral_events.find({"referrer_id": user["_id"]}).sort("created_at", -1).to_list(500)
    referees = await db.users.find(
        {"referred_by": user["_id"]}, {"email": 1, "display_name": 1, "created_at": 1},
    ).to_list(1000)
    instr = await db.payment_instructions.find_one({"_id": "main"}) or {}
    pct = float(instr.get("referral_commission_pct") or 10.0)
    total_days = sum(int(e.get("days_credited") or 0) for e in events)
    total_volume = sum(float(e.get("plan_amount") or 0) for e in events)
    return {
        "referral_code": user.get("referral_code"),
        "commission_pct": pct,
        "total_referred": len(referees),
        "total_conversions": len(events),
        "total_days_earned": total_days,
        "total_referred_volume_usd": total_volume,
        "referees": [
            {
                "id": r["_id"],
                "display_name": r.get("display_name"),
                "email_masked": _mask_email(r.get("email") or ""),
                "joined_at": r.get("created_at"),
            } for r in referees
        ],
        "events": [_strip_id(e) for e in events],
    }


def _mask_email(e: str) -> str:
    if "@" not in e:
        return e
    name, dom = e.split("@", 1)
    if len(name) <= 2:
        return name[0] + "*@" + dom
    return name[0] + "*" * (len(name) - 2) + name[-1] + "@" + dom


# ---------- Transactions (unified view for user) ----------
@api.get("/transactions/me")
async def transactions_me(user: dict = Depends(get_current_user)):
    subs = await db.payment_submissions.find(
        {"user_id": user["_id"]},
        {"screenshot_path": 0, "screenshot_mime": 0},
    ).sort("created_at", -1).to_list(500)
    events = await db.referral_events.find({"referrer_id": user["_id"]}).sort("created_at", -1).to_list(500)
    items: List[dict] = []
    for s in subs:
        items.append({
            "id": s["_id"],
            "kind": "payment",
            "plan": s.get("plan"),
            "amount": float(s.get("amount") or 0),
            "currency": s.get("currency") or "USD",
            "method": s.get("method"),
            "txn_reference": s.get("txn_reference"),
            "status": s.get("status"),
            "created_at": s.get("created_at"),
            "reviewed_at": s.get("reviewed_at"),
            "review_notes": s.get("review_notes"),
        })
    # enrich referral events with referee name
    ref_map: Dict[str, dict] = {}
    if events:
        ids = list({e["referee_id"] for e in events})
        users = await db.users.find({"_id": {"$in": ids}}, {"display_name": 1, "email": 1}).to_list(len(ids))
        ref_map = {u["_id"]: u for u in users}
    for e in events:
        u = ref_map.get(e["referee_id"]) or {}
        items.append({
            "id": e["_id"],
            "kind": "referral",
            "plan": e.get("plan"),
            "amount": float(e.get("plan_amount") or 0),
            "currency": "USD",
            "days_credited": int(e.get("days_credited") or 0),
            "commission_pct": float(e.get("commission_pct") or 0),
            "referee_display_name": u.get("display_name"),
            "referee_email_masked": _mask_email(u.get("email") or ""),
            "status": "credited",
            "created_at": e.get("created_at"),
        })
    items.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return items


# ---------- Account management (user-facing) ----------
@api.post("/auth/change-password")
async def change_password(body: ChangePasswordBody, user: dict = Depends(get_current_user)):
    fresh = await db.users.find_one({"_id": user["_id"]})
    if not fresh or not verify_password(body.current_password, fresh["password_hash"]):
        raise HTTPException(400, "Current password is incorrect")
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"password_hash": hash_password(body.new_password), "updated_at": now_iso()}},
    )
    return {"ok": True}


@api.delete("/auth/delete-account")
async def delete_account(user: dict = Depends(get_current_user), response: Response = None):
    # Soft-disable first, then hard delete user + related data (not trades/signals for audit)
    uid = user["_id"]
    await db.users.delete_one({"_id": uid})
    await db.subscriptions.delete_many({"user_id": uid})
    await db.bots.delete_many({"user_id": uid})
    await db.bridge_keys.delete_many({"user_id": uid})
    await db.mt5_accounts.delete_many({"user_id": uid})
    # keep payment_submissions + signals + trades + referral_events for audit
    if response is not None:
        clear_auth_cookies(response)
    return {"ok": True}


# ---------- Admin: user management ----------
@api.get("/admin/users")
async def admin_list_users(
    search: Optional[str] = None,
    role: Optional[str] = None,
    admin: dict = Depends(get_current_admin),
):
    q: Dict[str, Any] = {}
    if search:
        q["$or"] = [
            {"email": {"$regex": search, "$options": "i"}},
            {"display_name": {"$regex": search, "$options": "i"}},
            {"_id": search},
            {"referral_code": search.upper()},
        ]
    if role in ("admin", "user"):
        q["role"] = role
    rows = await db.users.find(q, {"password_hash": 0}).sort("created_at", -1).to_list(1000)
    # join subscription, referral stats
    user_ids = [r["_id"] for r in rows]
    subs = await db.subscriptions.find({"user_id": {"$in": user_ids}}).to_list(len(user_ids) * 2)
    sub_map: Dict[str, dict] = {}
    for s in subs:
        curr = sub_map.get(s["user_id"])
        if not curr or (s.get("updated_at") or "") > (curr.get("updated_at") or ""):
            sub_map[s["user_id"]] = s
    out = []
    for u in rows:
        sub = sub_map.get(u["_id"])
        # count referees
        ref_count = await db.users.count_documents({"referred_by": u["_id"]})
        bot_count = await db.bots.count_documents({"user_id": u["_id"]})
        out.append({
            "id": u["_id"],
            "email": u.get("email"),
            "display_name": u.get("display_name"),
            "role": u.get("role", "user"),
            "disabled": bool(u.get("disabled", False)),
            "referral_code": u.get("referral_code"),
            "referred_by": u.get("referred_by"),
            "created_at": u.get("created_at"),
            "subscription": {
                "plan": sub.get("plan") if sub else None,
                "status": sub.get("status") if sub else "none",
                "current_period_end": sub.get("current_period_end") if sub else None,
            } if sub else None,
            "referred_count": ref_count,
            "bots_count": bot_count,
        })
    return out


@api.get("/admin/users/{user_id}")
async def admin_get_user(user_id: str, admin: dict = Depends(get_current_admin)):
    u = await db.users.find_one({"_id": user_id}, {"password_hash": 0})
    if not u:
        raise HTTPException(404, "User not found")
    sub = await db.subscriptions.find_one({"user_id": user_id}, sort=[("updated_at", -1)])
    submissions = await db.payment_submissions.find(
        {"user_id": user_id},
        {"screenshot_path": 0, "screenshot_mime": 0},
    ).sort("created_at", -1).to_list(100)
    referees = await db.users.find(
        {"referred_by": user_id}, {"email": 1, "display_name": 1, "created_at": 1},
    ).to_list(500)
    referrer = None
    if u.get("referred_by"):
        r = await db.users.find_one({"_id": u["referred_by"]}, {"email": 1, "display_name": 1, "referral_code": 1})
        if r:
            referrer = {"id": r["_id"], "email": r.get("email"), "display_name": r.get("display_name"), "referral_code": r.get("referral_code")}
    ref_events = await db.referral_events.find({"referrer_id": user_id}).sort("created_at", -1).to_list(200)
    return {
        "user": user_to_public(u),
        "subscription": sub and {k: v for k, v in sub.items() if k != "_id"},
        "submissions": [_strip_id(s) for s in submissions],
        "referees": [{"id": r["_id"], "email": r.get("email"), "display_name": r.get("display_name"), "created_at": r.get("created_at")} for r in referees],
        "referrer": referrer,
        "referral_events": [_strip_id(e) for e in ref_events],
    }


@api.patch("/admin/users/{user_id}")
async def admin_patch_user(user_id: str, body: AdminUserPatch, admin: dict = Depends(get_current_admin)):
    if user_id == admin["_id"] and body.role and body.role != "admin":
        raise HTTPException(400, "Cannot demote yourself")
    if user_id == admin["_id"] and body.disabled:
        raise HTTPException(400, "Cannot disable yourself")
    update: Dict[str, Any] = {}
    if body.role in ("admin", "user"):
        update["role"] = body.role
    if body.disabled is not None:
        update["disabled"] = bool(body.disabled)
    if body.display_name is not None:
        update["display_name"] = body.display_name
    if not update:
        raise HTTPException(400, "Nothing to update")
    update["updated_at"] = now_iso()
    r = await db.users.update_one({"_id": user_id}, {"$set": update})
    if r.matched_count == 0:
        raise HTTPException(404, "User not found")
    return {"ok": True}


@api.delete("/admin/users/{user_id}")
async def admin_delete_user(user_id: str, admin: dict = Depends(get_current_admin)):
    if user_id == admin["_id"]:
        raise HTTPException(400, "Cannot delete yourself")
    u = await db.users.find_one({"_id": user_id})
    if not u:
        raise HTTPException(404, "User not found")
    await db.users.delete_one({"_id": user_id})
    await db.subscriptions.delete_many({"user_id": user_id})
    await db.bots.delete_many({"user_id": user_id})
    await db.bridge_keys.delete_many({"user_id": user_id})
    await db.mt5_accounts.delete_many({"user_id": user_id})
    return {"ok": True}


@api.post("/admin/users/{user_id}/grant-subscription")
async def admin_grant_sub(user_id: str, body: GrantSubscriptionBody, admin: dict = Depends(get_current_admin)):
    if body.plan not in PLAN_DAYS:
        raise HTTPException(400, "Invalid plan")
    days = int(body.days_override) if body.days_override else PLAN_DAYS[body.plan]
    if days <= 0:
        raise HTTPException(400, "days must be > 0")
    u = await db.users.find_one({"_id": user_id})
    if not u:
        raise HTTPException(404, "User not found")
    existing = await db.subscriptions.find_one({"user_id": user_id}, sort=[("updated_at", -1)])
    now = now_utc()
    cpe_existing = None
    if body.extend and existing and existing.get("current_period_end"):
        try:
            cpe_existing = datetime.fromisoformat(existing["current_period_end"])
        except Exception:
            cpe_existing = None
    base = max(cpe_existing or now, now)
    new_cpe = (base + timedelta(days=days)).isoformat()
    if existing:
        await db.subscriptions.update_one(
            {"_id": existing["_id"]},
            {"$set": {
                "plan": body.plan, "status": "active",
                "current_period_end": new_cpe,
                "cancel_at_period_end": False,
                "updated_at": now_iso(),
            }},
        )
    else:
        await db.subscriptions.insert_one({
            "_id": str(uuid.uuid4()),
            "user_id": user_id, "plan": body.plan, "status": "active",
            "current_period_end": new_cpe, "cancel_at_period_end": False,
            "created_at": now_iso(), "updated_at": now_iso(),
        })
    return {"ok": True, "current_period_end": new_cpe}


@api.post("/admin/users/{user_id}/cancel-subscription")
async def admin_cancel_sub(user_id: str, admin: dict = Depends(get_current_admin)):
    existing = await db.subscriptions.find_one({"user_id": user_id}, sort=[("updated_at", -1)])
    if not existing:
        raise HTTPException(404, "No subscription for user")
    await db.subscriptions.update_one(
        {"_id": existing["_id"]},
        {"$set": {"status": "canceled", "cancel_at_period_end": True, "updated_at": now_iso()}},
    )
    return {"ok": True}


# ---------- Admin: analytics & referrals ----------
@api.get("/admin/stats")
async def admin_stats(admin: dict = Depends(get_current_admin)):
    now = now_iso()
    total_users = await db.users.count_documents({})
    disabled_users = await db.users.count_documents({"disabled": True})
    admin_users = await db.users.count_documents({"role": "admin"})
    active_subs = await db.subscriptions.count_documents({
        "status": {"$in": ["active", "trialing"]},
        "current_period_end": {"$gte": now},
    })
    # MRR: sum of (plan_price / plan_days) × 30 for each active sub
    instr = await db.payment_instructions.find_one({"_id": "main"}) or {}
    monthly_price = float(instr.get("monthly_price") or 49)
    quarterly_price = float(instr.get("quarterly_price") or 129)
    yearly_price = float(instr.get("yearly_price") or 449)
    plan_mrr = {"monthly": monthly_price, "quarterly": quarterly_price / 3, "yearly": yearly_price / 12}
    mrr = 0.0
    async for s in db.subscriptions.find({
        "status": {"$in": ["active", "trialing"]},
        "current_period_end": {"$gte": now},
    }):
        mrr += plan_mrr.get(s.get("plan") or "monthly", monthly_price)
    pending_payments = await db.payment_submissions.count_documents({"status": "pending"})
    approved_payments = await db.payment_submissions.count_documents({"status": "approved"})
    rejected_payments = await db.payment_submissions.count_documents({"status": "rejected"})
    approved_volume = 0.0
    async for s in db.payment_submissions.find({"status": "approved"}):
        approved_volume += float(s.get("amount") or 0)
    referral_events = await db.referral_events.count_documents({})
    total_days_credited = 0
    async for e in db.referral_events.find({}):
        total_days_credited += int(e.get("days_credited") or 0)
    bots_total = await db.bots.count_documents({})
    bots_active = await db.bots.count_documents({"is_active": True})
    trades_open = await db.trades.count_documents({"status": "open"})
    trades_closed = await db.trades.count_documents({"status": "closed"})
    signals_today = await db.signals.count_documents({
        "created_at": {"$gte": now_utc().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()},
    })
    return {
        "users": {"total": total_users, "disabled": disabled_users, "admins": admin_users},
        "subscriptions": {"active": active_subs, "mrr_usd": round(mrr, 2)},
        "payments": {
            "pending": pending_payments, "approved": approved_payments,
            "rejected": rejected_payments,
            "approved_volume_usd": round(approved_volume, 2),
        },
        "referrals": {"events": referral_events, "total_days_credited": total_days_credited},
        "bots": {"total": bots_total, "active": bots_active},
        "trades": {"open": trades_open, "closed": trades_closed},
        "signals": {"today": signals_today},
    }


@api.get("/admin/referrals")
async def admin_referrals(admin: dict = Depends(get_current_admin)):
    events = await db.referral_events.find({}).sort("created_at", -1).to_list(1000)
    if not events:
        return []
    ids = list({e["referrer_id"] for e in events} | {e["referee_id"] for e in events})
    users = await db.users.find({"_id": {"$in": ids}}, {"email": 1, "display_name": 1}).to_list(len(ids))
    umap = {u["_id"]: u for u in users}
    out = []
    for e in events:
        r = umap.get(e["referrer_id"]) or {}
        e2 = umap.get(e["referee_id"]) or {}
        obj = _strip_id(e)
        obj["referrer_email"] = r.get("email")
        obj["referrer_name"] = r.get("display_name")
        obj["referee_email"] = e2.get("email")
        obj["referee_name"] = e2.get("display_name")
        out.append(obj)
    return out


# ---------- Utility ----------
def _strip_id(doc: dict) -> dict:
    if not doc:
        return doc
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["id"] = doc["_id"]
    return out


# ---------- Signal scanning ----------
# ---- Risk / Safety helpers (Phase 3 P0) ----
def _utc_day_start(dt: Optional[datetime] = None) -> datetime:
    dt = dt or now_utc()
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _utc_week_start(dt: Optional[datetime] = None) -> datetime:
    """Monday 00:00 UTC of the week containing `dt`."""
    dt = dt or now_utc()
    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


# Correlation groups: within each group, max N same-direction trades are allowed.
# (BUY EURUSD + BUY GBPUSD are "long EUR/short USD" exposure stacked.)
CORRELATION_GROUPS: Dict[str, set] = {
    "usd_majors": {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"},
    "metals": {"XAUUSD", "XAGUSD"},
    "usd_base":  {"USDJPY", "USDCAD", "USDCHF"},
}
CORRELATION_LIMIT = 2

DRAWDOWN_HALT_PERCENT = 15.0
NEWS_BLOCK_BEFORE_MIN = 30
NEWS_BLOCK_AFTER_MIN = 15
_NEWS_CACHE: Dict[str, Any] = {"events": [], "loaded_at": 0.0}
_NEWS_CALENDAR_PATH = ROOT_DIR / "news_calendar.json"


async def _bot_daily_pnl(bot_id: str) -> float:
    """Realized closed-today + floating on currently open trades for this bot (USD)."""
    day_start_iso = _utc_day_start().isoformat()
    realized = 0.0
    async for t in db.trades.find({
        "bot_id": bot_id, "status": "closed",
        "closed_at": {"$gte": day_start_iso},
    }, {"pnl": 1}):
        realized += float(t.get("pnl") or 0)
    floating = 0.0
    async for t in db.trades.find({"bot_id": bot_id, "status": "open"}, {"live_pnl": 1}):
        floating += float(t.get("live_pnl") or 0)
    return realized + floating


async def _bot_open_count(bot_id: str) -> int:
    return await db.trades.count_documents({"bot_id": bot_id, "status": "open"})


async def _correlation_blocked(user_id: str, pair: str, side: str) -> Optional[str]:
    """Return a block-reason string if a new (pair, side) would push a correlation
    group past CORRELATION_LIMIT same-direction open trades; else None."""
    pair = pair.upper()
    for gname, members in CORRELATION_GROUPS.items():
        if pair not in members:
            continue
        same_dir = await db.trades.count_documents({
            "user_id": user_id, "status": "open",
            "pair": {"$in": list(members)}, "side": side,
        })
        if same_dir >= CORRELATION_LIMIT:
            return f"correlation_block:{gname}({same_dir}/{CORRELATION_LIMIT})"
    return None


async def _user_drawdown_state(user_id: str) -> dict:
    """Track weekly equity high. Halt user when current equity drops >= DRAWDOWN_HALT_PERCENT.
    Resets every Monday 00:00 UTC. Returns dict {halted, reason, week_high, current_equity, drawdown_pct}."""
    week_start_iso = _utc_week_start().isoformat()
    acct = await db.mt5_accounts.find_one(
        {"user_id": user_id}, sort=[("updated_at", -1)],
    )
    equity = float(acct["equity"]) if acct and acct.get("equity") else 0.0
    state = await db.risk_state.find_one({"_id": user_id})
    if not state or state.get("week_start") != week_start_iso:
        # New week — reset baseline using whatever equity we have (or 0)
        await db.risk_state.update_one(
            {"_id": user_id},
            {"$set": {
                "_id": user_id,
                "week_start": week_start_iso,
                "week_high": equity,
                "halted": False,
                "halt_reason": None,
                "updated_at": now_iso(),
            }}, upsert=True,
        )
        return {"halted": False, "reason": "week_reset", "week_high": equity, "current_equity": equity, "drawdown_pct": 0.0}
    if equity <= 0:
        # No fresh equity data — keep prior state, do not halt new bots solely on this
        return {"halted": bool(state.get("halted")), "reason": state.get("halt_reason") or "no_equity",
                "week_high": float(state.get("week_high") or 0), "current_equity": 0.0, "drawdown_pct": 0.0}
    prev_high = float(state.get("week_high") or 0)
    new_high = max(prev_high, equity)
    if new_high > prev_high:
        await db.risk_state.update_one({"_id": user_id}, {"$set": {"week_high": new_high, "updated_at": now_iso()}})
    dd_pct = ((new_high - equity) / new_high * 100.0) if new_high > 0 else 0.0
    halted = dd_pct >= DRAWDOWN_HALT_PERCENT
    if halted and not state.get("halted"):
        await db.risk_state.update_one(
            {"_id": user_id},
            {"$set": {
                "halted": True,
                "halt_reason": f"weekly_dd_{dd_pct:.2f}%",
                "halted_at": now_iso(), "updated_at": now_iso(),
            }},
        )
        # Fire one-time halt alert
        try:
            notify_svc.notify("dd_halt", drawdown_pct=dd_pct, limit_pct=DRAWDOWN_HALT_PERCENT)
        except Exception:
            pass
    return {"halted": halted, "reason": f"weekly_dd_{dd_pct:.2f}%" if halted else "ok",
            "week_high": new_high, "current_equity": equity, "drawdown_pct": round(dd_pct, 2)}


def _load_news_calendar() -> list:
    """Read news_calendar.json with 60s in-memory cache. Tolerates malformed files."""
    import time as _t
    if _t.monotonic() - _NEWS_CACHE["loaded_at"] < 60.0:
        return _NEWS_CACHE["events"]
    events: list = []
    if _NEWS_CALENDAR_PATH.exists():
        try:
            with open(_NEWS_CALENDAR_PATH) as f:
                data = json.load(f)
            events = data.get("events") or []
        except Exception as e:
            log.warning("news calendar parse failed: %s", e)
    _NEWS_CACHE["events"] = events
    _NEWS_CACHE["loaded_at"] = _t.monotonic()
    return events


def _news_active() -> Optional[str]:
    """Return 'news_block:<NAME>' if we are inside any blackout window; else None.
    Window: [event_time - NEWS_BLOCK_BEFORE_MIN, event_time + NEWS_BLOCK_AFTER_MIN]."""
    now = now_utc()
    for ev in _load_news_calendar():
        try:
            raw = str(ev.get("time") or "")
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            start = dt - timedelta(minutes=NEWS_BLOCK_BEFORE_MIN)
            end = dt + timedelta(minutes=NEWS_BLOCK_AFTER_MIN)
            if start <= now <= end:
                return f"news_block:{ev.get('name', 'event')}"
        except Exception:
            continue
    return None


async def _htf_trend(pair: str, htf: str) -> Optional[str]:
    """Return 'up' / 'down' / 'flat' / None based on EMA(21) vs EMA(55) on the higher TF."""
    try:
        candles = await fetch_candles(pair, htf, 100)
    except Exception:
        return None
    if len(candles) < 60:
        return None
    closes = [c["c"] for c in candles]
    ef = ema(closes, 21)
    es = ema(closes, 55)
    if not ef or not es:
        return None
    diff = ef[-1] - es[-1]
    atr_proxy = abs(closes[-1]) * 0.0005  # ~5bp; if EMAs are within this band, treat as flat
    if diff > atr_proxy:
        return "up"
    if diff < -atr_proxy:
        return "down"
    return "flat"


async def _scan_and_persist(bots: List[dict]) -> int:
    created = 0
    cached_symbols: set[str] = set()
    # ---- Global / per-user gates evaluated once per scan ----
    news_reason = _news_active()
    dd_cache: Dict[str, dict] = {}
    for bot in bots:
        try:
            user_id = bot["user_id"]
            # 1) NEWS FILTER (global blackout)
            if news_reason:
                await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                    "last_scan_at": now_iso(),
                    "last_scan_result": news_reason,
                }})
                continue
            # 2) EQUITY DRAWDOWN HALT (per user — auto-resumes Monday UTC)
            if user_id not in dd_cache:
                dd_cache[user_id] = await _user_drawdown_state(user_id)
            if dd_cache[user_id].get("halted"):
                await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                    "last_scan_at": now_iso(),
                    "last_scan_result": f"halt:{dd_cache[user_id].get('reason')}",
                }})
                continue
            # 3) MAX POSITIONS per bot
            if bot.get("max_positions"):
                open_n = await _bot_open_count(bot["_id"])
                if open_n >= int(bot["max_positions"]):
                    await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                        "last_scan_at": now_iso(),
                        "last_scan_result": f"max_positions_reached:{open_n}/{int(bot['max_positions'])}",
                    }})
                    continue
            # 4) DAILY LOSS LIMIT per bot (realized + floating, USD, UTC day)
            if bot.get("daily_loss_limit") is not None:
                limit = float(bot["daily_loss_limit"])
                if limit > 0:
                    daily = await _bot_daily_pnl(bot["_id"])
                    if daily <= -limit:
                        await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                            "last_scan_at": now_iso(),
                            "last_scan_result": f"daily_loss_blocked:{daily:.2f}/-{limit:.2f}",
                        }})
                        continue
            cfg = StrategyConfig.from_dict(bot.get("strategy_config") or {})
            candles = await fetch_candles(bot["pair"], bot["timeframe"], 200)
            # ---- Data validation (NEVER trade on bad/stale data) ----
            now_ms = int(now_utc().timestamp() * 1000)
            v = validate_candles(candles, bot["timeframe"], now_ms)
            if not v.ok:
                await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                    "last_scan_at": now_iso(),
                    "last_scan_result": f"data_unavailable:{v.reason}",
                }})
                notify_svc.notify("data_bad", pair=bot["pair"], reason=v.reason)
                continue
            # ---- Volatility circuit breaker ----
            _a_pre = atr(candles, cfg.atr_period)
            vol_block = volatility_gate(_a_pre)
            if vol_block:
                await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                    "last_scan_at": now_iso(),
                    "last_scan_result": vol_block,
                }})
                notify_svc.notify("vol_halt", pair=bot["pair"],
                                  ratio=(_a_pre[-1] / max(_a_pre[-2], 1e-9)))
                continue
            if len(candles) < 60:
                await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                    "last_scan_at": now_iso(),
                    "last_scan_result": "insufficient_data",
                }})
                continue
            if bot["pair"] not in cached_symbols:
                cached_symbols.add(bot["pair"])
                rows = []
                from datetime import datetime, timezone
                for c in candles[-50:]:
                    rows.append({
                        "symbol": bot["pair"],
                        "ts": datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc).isoformat(),
                        "open": c["o"], "high": c["h"], "low": c["l"], "close": c["c"],
                    })
                if rows:
                    # upsert on (symbol, ts)
                    for r in rows:
                        await db.price_cache.update_one(
                            {"symbol": r["symbol"], "ts": r["ts"]},
                            {"$set": r}, upsert=True,
                        )
            # Phase-1: per-bot signal cooldown = 1 closed bar of the bot's timeframe.
            # Prevents the scheduler (3-min ticks) from re-firing the same setup
            # multiple times inside the SAME candle (root cause of duplicate-signal losses).
            tf_min_map = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}
            cooldown_min = tf_min_map.get((bot.get("timeframe") or "M15").upper(), 15)
            since = (now_utc() - timedelta(minutes=cooldown_min)).isoformat()
            recent = await db.signals.find_one(
                {"bot_id": bot["_id"], "created_at": {"$gte": since}},
            )
            if recent:
                await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                    "last_scan_at": now_iso(),
                    "last_scan_result": f"cooldown:{cooldown_min}min",
                }})
                continue

            # FIX #2 — Per-(pair, direction) cooldown across ALL the user's bots.
            # Even if THIS bot is past its own bar, refuse to fire a second BUY (or SELL)
            # on the same pair within 1 closed bar. Prevents the "fire 3 XAU sells in 15 min"
            # cluster pattern that opens 3× the risk on a single idea.
            pair = (bot.get("pair") or "").upper()
            pair_cd_since = (now_utc() - timedelta(minutes=cooldown_min)).isoformat()
            recent_any_dir = await db.signals.find(
                {"user_id": bot["user_id"], "pair": pair,
                 "created_at": {"$gte": pair_cd_since}},
                {"_id": 0, "side": 1},
            ).to_list(20)
            # We'll allow this scan to compute a signal, but later we check side-match.
            # Stash the recent-sides list in a local for the post-generation gate.
            _recent_sides_same_pair: set = {s["side"] for s in recent_any_dir}
            # Route to strategy engine (v2 by default)
            v2_ctx = None
            if STRATEGY_VERSION == "v2":
                # Pre-fetch HTF trend if configured (engine v2 uses it as a confluence factor)
                htf_label = bot.get("higher_tf_confirmation") or "off"
                htf_trend = await _htf_trend(bot["pair"], htf_label) if htf_label not in (None, "off") else None
                v2_out = generate_signal_v2(candles, STRATEGY_V2_CFG, htf_trend=htf_trend)
                if v2_out:
                    sig, v2_ctx = v2_out
                else:
                    sig = None
            else:
                sig = generate_signal(
                    candles, cfg,
                    enable_scalping_in_ranges=bool(bot.get("enable_scalping_in_ranges", True)),
                )
            # Probe the regime + session separately so we can populate the BOTS-page
            # mode badge (SWING / SCALP / STAND-BY) even on bars where no setup fired.
            from engine import current_session as _cur_session
            _session_now = _cur_session()
            _ef = ema([c["c"] for c in candles], cfg.ema_fast)
            _es = ema([c["c"] for c in candles], cfg.ema_slow)
            _a  = atr(candles, cfg.atr_period)
            _regime = detect_regime(candles, _ef, _es, _a) if candles else "ranging"
            if _session_now == "off":
                _mode_badge = "standby"
            elif _regime == "volatile":
                _mode_badge = "standby"
            elif _regime == "ranging":
                _mode_badge = "scalp" if bot.get("enable_scalping_in_ranges", True) else "standby"
            else:
                _mode_badge = "swing"
            if not sig:
                await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                    "last_scan_at": now_iso(),
                    "last_scan_result": f"no_setup ({_regime})",
                    "last_mode": _mode_badge,
                }})
                continue
            if sig.session not in (bot.get("sessions") or DEFAULT_SESSIONS):
                await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                    "last_scan_at": now_iso(),
                    "last_scan_result": f"session_filtered:{sig.session}",
                    "last_mode": _mode_badge,
                }})
                continue
            # FIX #2 — Block same-(pair, direction) duplicates fired by sibling bots
            # within 1 closed bar window. If user has a SELL fired ≤ TF minutes ago,
            # the next SELL on the same pair is silently skipped.
            if sig.side in _recent_sides_same_pair:
                await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                    "last_scan_at": now_iso(),
                    "last_scan_result": f"pair_dir_cooldown:{pair}:{sig.side}",
                    "last_mode": _mode_badge,
                }})
                continue
            # FIX #2 — If an OPEN position exists on this pair in the OPPOSITE
            # direction, the new signal must have meaningfully higher confidence
            # (>= 0.10 above the open position's signal) to fire. This is the
            # "adapt in real-time" guard: a fresh BUY of equal strength while a
            # SELL is open is noise; only let the BUY through if it's clearly a
            # stronger setup (reversal candidate).
            opposite = "sell" if sig.side == "buy" else "buy"
            existing_opp = await db.trades.find_one(
                {"user_id": bot["user_id"], "pair": pair,
                 "side": opposite, "status": "open"},
                {"_id": 0, "confidence": 1, "signal_id": 1},
            )
            if existing_opp:
                existing_conf = float(existing_opp.get("confidence") or 0.0)
                if sig.confidence < existing_conf + 0.10:
                    await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                        "last_scan_at": now_iso(),
                        "last_scan_result": f"opposite_open:{opposite}@{existing_conf:.2f}_need_+0.10",
                        "last_mode": _mode_badge,
                    }})
                    continue
            # 5) CORRELATION FILTER — block if group already has CORRELATION_LIMIT same-direction trades
            corr_reason = await _correlation_blocked(bot["user_id"], bot["pair"], sig.side)
            if corr_reason:
                await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                    "last_scan_at": now_iso(),
                    "last_scan_result": corr_reason,
                    "last_mode": _mode_badge,
                }})
                continue
            # 6) HIGHER-TF CONFIRMATION — drop signals that contradict the higher timeframe trend
            htf = bot.get("higher_tf_confirmation")
            if htf and htf != "off":
                trend = await _htf_trend(bot["pair"], htf)
                want = "up" if sig.side == "buy" else "down"
                if trend != want:
                    await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                        "last_scan_at": now_iso(),
                        "last_scan_result": f"htf_mismatch:{htf}({trend or 'unknown'} vs need {want})",
                        "last_mode": _mode_badge,
                    }})
                    continue
            # equity
            acct = await db.mt5_accounts.find_one(
                {"user_id": bot["user_id"]},
                sort=[("updated_at", -1)],
            )
            equity = float(acct["equity"]) if acct and acct.get("equity") else 10000.0
            sl_dist = abs(sig.entry - sig.sl)
            # Adaptive sizing (drawdown-aware, vol-aware, confidence-weighted)
            dd_pct = float(dd_cache.get(user_id, {}).get("drawdown_pct") or 0.0)
            _atr_arr = atr(candles, cfg.atr_period)
            _atr_now = _atr_arr[-1] if _atr_arr else 0.0
            _atr_med = (sorted([x for x in _atr_arr[-50:] if x > 0])
                        [max(1, len([x for x in _atr_arr[-50:] if x > 0]) // 2) - 1]
                        if any(x > 0 for x in _atr_arr[-50:]) else _atr_now)
            atr_ratio = (_atr_now / _atr_med) if _atr_med > 0 else 1.0
            sizing = adaptive_lot(
                equity_usd=equity, base_risk_pct=float(bot["risk_per_trade"]),
                sl_distance=sl_dist, pair=bot["pair"],
                session=sig.session, mode=sig.mode,
                confidence=sig.confidence, drawdown_pct=dd_pct,
                atr_ratio=atr_ratio, recent_win_rate=None,
            )
            lot = sizing["lot"]
            # Scalp signals expire fast — they're meant to be picked up by the next bridge poll
            # (every 5 s). Swing signals get the standard 30-min TTL.
            ttl_min = 5 if sig.mode == "scalp" else 30
            sid = str(uuid.uuid4())
            await db.signals.insert_one({
                "_id": sid,
                "user_id": bot["user_id"], "bot_id": bot["_id"],
                "pair": bot["pair"], "side": sig.side,
                "entry": sig.entry, "sl": sig.sl, "tp": sig.tp,
                "lot": lot, "confidence": sig.confidence,
                "regime": sig.regime, "session": sig.session, "reason": sig.reason,
                "mode": sig.mode, "max_hold_minutes": sig.max_hold_minutes,
                "status": "pending",
                "expires_at": (now_utc() + timedelta(minutes=ttl_min)).isoformat(),
                "created_at": now_iso(),
                "picked_at": None,
                # Adaptive-sizing audit trail (used by ML feature store later)
                "sizing": sizing,
                "atr_ratio": round(atr_ratio, 3),
                "v2_scores": (v2_ctx.scores if v2_ctx else None),
                "v2_context": ({
                    "atr": v2_ctx.atr, "htf_aligned": v2_ctx.htf_aligned,
                    "squeeze": v2_ctx.squeeze,
                    "bos": v2_ctx.bos, "sweep": v2_ctx.sweep,
                    "displacement": v2_ctx.displacement,
                } if v2_ctx else None),
            })
            # Fire-and-forget Telegram alert (admin-level channel for v1)
            try:
                notify_svc.notify(
                    "signal", pair=bot["pair"], side=sig.side,
                    entry=sig.entry, sl=sig.sl, tp=sig.tp, lot=lot,
                    confidence=sig.confidence, regime=sig.regime,
                    session=sig.session, mode=sig.mode, reason=sig.reason,
                )
            except Exception:
                pass
            await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                "last_scan_at": now_iso(),
                "last_scan_result": f"signal_created:{sig.side} ({sig.confidence:.2f}) [{sig.mode}]",
                "last_mode": _mode_badge,
            }})
            created += 1
        except Exception as e:
            log.warning("scan error for bot %s: %s", bot.get("_id"), e)
            try:
                await db.bots.update_one({"_id": bot["_id"]}, {"$set": {
                    "last_scan_at": now_iso(),
                    "last_scan_result": f"error: {str(e)[:80]}",
                }})
            except Exception:
                pass
    return created


async def scheduled_scan():
    """Runs every 3 minutes. Scans all active bots of users with active subscription."""
    try:
        # find active subscriptions
        subs_cursor = db.subscriptions.find(
            {"status": {"$in": ["active", "trialing"]}}, {"user_id": 1},
        )
        active_users = {s["user_id"] async for s in subs_cursor}
        if not active_users:
            log.info("[scheduler] tick — no active subscriptions, skipping")
            return
        bots = await db.bots.find({
            "is_active": True, "user_id": {"$in": list(active_users)},
        }).to_list(1000)
        if not bots:
            log.info("[scheduler] tick — %d active user(s), 0 active bots, skipping", len(active_users))
            return
        log.info("[scheduler] tick — scanning %d active bot(s) across %d user(s)", len(bots), len(active_users))
        for b in bots:
            log.info("[scheduler]   → scan triggered for bot '%s' (%s %s, id=%s)",
                     b.get("name", "?"), b.get("pair", "?"), b.get("timeframe", "?"), b.get("_id", "?"))
        created = await _scan_and_persist(bots)
        log.info("[scheduler] tick complete — %d bot(s) scanned, %d signal(s) created", len(bots), created)
    except Exception as e:
        log.exception("[scheduler] tick failed: %s", e)


# ---------- Startup: seeding, indexes, scheduler ----------
scheduler = AsyncIOScheduler(timezone="UTC")


# ---------- Bridge candle ingestion (MT5 → server data feed) ----------
class BridgeCandleBatch(BaseModel):
    pair: str
    timeframe: str
    rows: List[dict]   # [{t (ms), o, h, l, c}, ...]


@api.post("/bridge-candles")
async def bridge_candles(body: BridgeCandleBatch, request: Request):
    """The MT5 bridge POSTs OHLC bars here. Auth: x-aurum-bridge-key header.
    Body schema: {pair, timeframe, rows:[{t,o,h,l,c}, ...]}. Returns {written:int}.
    """
    await _bridge_key_auth(request)
    if not body.rows:
        return {"written": 0}
    written = await store_candles(body.pair, body.timeframe, body.rows)
    return {"written": written, "pair": body.pair.upper(), "timeframe": body.timeframe}


@api.get("/bridge/stream-config")
async def bridge_stream_config(request: Request):
    """Bridge calls this every ~60s to discover which (pair, timeframe) pairs to
    push candles for. Returns the union of every active bot's (pair, timeframe)
    + any higher_tf_confirmation TF, for the bridge-key's owner.
    Auth: x-aurum-bridge-key header.
    """
    key_row = await _bridge_key_auth(request)
    user_id = key_row["user_id"]
    pairs: set = set()
    # Stream candles for ALL the user's bots (active OR paused) so that data
    # is already populated the moment the user flips a bot active. Cheap on bw.
    async for b in db.bots.find(
        {"user_id": user_id},
        {"_id": 0, "pair": 1, "timeframe": 1, "higher_tf_confirmation": 1},
    ):
        pair = (b.get("pair") or "").upper().strip()
        tf = (b.get("timeframe") or "").upper().strip()
        if pair and tf:
            pairs.add((pair, tf))
        htf = (b.get("higher_tf_confirmation") or "").upper().strip()
        if pair and htf and htf != "OFF":
            pairs.add((pair, htf))
    items = [{"pair": p, "timeframe": tf} for (p, tf) in sorted(pairs)]
    return {
        "min_bridge_version": MIN_BRIDGE_VERSION,
        "pairs": items,
        "count": len(items),
    }


@api.get("/diag/candles")
async def diag_candles(
    pair: str,
    timeframe: str,
    user: dict = Depends(get_current_user),
):
    """Diagnostic: returns candle stats for a (pair, timeframe) in db.candles.
    Open to any authenticated user — they can only see global storage shape,
    not per-user data (candles are shared across all users of the same broker).
    Returns: count, last_t, last_age_min, gap_count_2x, gap_count_5x, first_t.
    """
    pair = pair.upper().strip()
    timeframe = timeframe.upper().strip()
    tf_ms_map = {"M1": 60_000, "M5": 300_000, "M15": 900_000, "M30": 1_800_000,
                 "H1": 3_600_000, "H4": 14_400_000, "D1": 86_400_000}
    tf_ms = tf_ms_map.get(timeframe, 900_000)
    cur = db.candles.find(
        {"pair": pair, "timeframe": timeframe},
        {"_id": 0, "t": 1},
    ).sort("t", 1).limit(500)
    ts = [int(r["t"]) async for r in cur]
    if not ts:
        return {
            "pair": pair, "timeframe": timeframe, "count": 0,
            "last_t": None, "last_age_min": None,
            "first_t": None, "gap_count_2x": 0, "gap_count_5x": 0,
            "message": "No candles in db for this (pair, timeframe). "
                       "Bridge needs to push them — check AURUM_STREAM_PAIRS "
                       "or use bridge v1.7 auto-discovery.",
        }
    gap2 = gap5 = 0
    for i in range(1, len(ts)):
        dt = ts[i] - ts[i - 1]
        if dt > tf_ms * 5:
            gap5 += 1
        elif dt > tf_ms * 2:
            gap2 += 1
    last_t = ts[-1]
    now_ms = int(now_utc().timestamp() * 1000)
    last_age_min = (now_ms - last_t) / 60_000
    return {
        "pair": pair, "timeframe": timeframe,
        "count": len(ts),
        "first_t": ts[0], "last_t": last_t,
        "last_age_min": round(last_age_min, 2),
        "gap_count_2x": gap2, "gap_count_5x": gap5,
        "tf_ms": tf_ms,
    }


@api.get("/diag/bot/{bot_id}")
async def diag_bot(bot_id: str, user: dict = Depends(get_current_user)):
    """v1.8 — Full diagnostic snapshot for a single bot.
    Returns: bot config, last_scan_at, last_scan_result, candle freshness, bridge heartbeat age,
    open trade count, signals_last_24h.
    """
    bot = await db.bots.find_one({"_id": bot_id})
    if not bot:
        raise HTTPException(404, "Bot not found")
    if bot["user_id"] != user["_id"] and user.get("role") != "admin":
        raise HTTPException(403, "Forbidden")
    pair = (bot.get("pair") or "").upper()
    tf = (bot.get("timeframe") or "").upper()
    tf_ms_map = {"M1": 60_000, "M5": 300_000, "M15": 900_000, "M30": 1_800_000,
                 "H1": 3_600_000, "H4": 14_400_000, "D1": 86_400_000}
    tf_ms = tf_ms_map.get(tf, 900_000)
    candle_count = await db.candles.count_documents({"pair": pair, "timeframe": tf})
    last_candle = await db.candles.find_one(
        {"pair": pair, "timeframe": tf}, {"_id": 0, "t": 1, "c": 1},
        sort=[("t", -1)],
    )
    now_ms = int(now_utc().timestamp() * 1000)
    last_candle_age_min = (now_ms - int(last_candle["t"])) / 60_000 if last_candle else None
    candle_health = "ok"
    if not last_candle:
        candle_health = "no_data"
    elif candle_count < 80:
        candle_health = "insufficient"
    elif last_candle_age_min is not None and last_candle_age_min > (tf_ms / 60_000) * 3:
        candle_health = "stale"
    bk = await db.bridge_keys.find_one(
        {"user_id": bot["user_id"], "revoked": False},
        {"_id": 0, "last_seen_at": 1, "bridge_version": 1, "label": 1},
        sort=[("last_seen_at", -1)],
    )
    bridge_age_min = None
    if bk and bk.get("last_seen_at"):
        try:
            t = datetime.fromisoformat(bk["last_seen_at"].replace("Z", "+00:00"))
            bridge_age_min = (now_utc() - t).total_seconds() / 60
        except Exception:
            pass
    open_n = await db.trades.count_documents(
        {"user_id": bot["user_id"], "bot_id": bot_id, "status": "open"},
    )
    closed_n = await db.trades.count_documents(
        {"user_id": bot["user_id"], "bot_id": bot_id, "status": "closed"},
    )
    recent_signals = await db.signals.count_documents(
        {"bot_id": bot_id, "created_at": {"$gte": (now_utc() - timedelta(hours=24)).isoformat()}},
    )
    return {
        "bot_id": bot_id,
        "name": bot.get("name"),
        "pair": pair, "timeframe": tf,
        "is_active": bool(bot.get("is_active")),
        "higher_tf_confirmation": bot.get("higher_tf_confirmation"),
        "last_scan_at": bot.get("last_scan_at"),
        "last_scan_result": bot.get("last_scan_result"),
        "candle_count": candle_count,
        "last_candle_t": last_candle["t"] if last_candle else None,
        "last_candle_age_min": round(last_candle_age_min, 2) if last_candle_age_min is not None else None,
        "candle_health": candle_health,
        "bridge_version": (bk or {}).get("bridge_version"),
        "bridge_last_seen_at": (bk or {}).get("last_seen_at"),
        "bridge_age_min": round(bridge_age_min, 2) if bridge_age_min is not None else None,
        "open_trades": open_n,
        "closed_trades": closed_n,
        "signals_last_24h": recent_signals,
    }


@api.get("/admin/system-health")
async def admin_system_health(admin: dict = Depends(get_current_admin)):
    """v1.8 — Aggregated health snapshot. Suitable for 10s polling from a dashboard."""
    now_ms = int(now_utc().timestamp() * 1000)
    bridge_keys = await db.bridge_keys.find(
        {"revoked": False},
        {"_id": 0, "user_id": 1, "last_seen_at": 1, "bridge_version": 1, "label": 1},
    ).to_list(500)
    bridges_online = 0
    for k in bridge_keys:
        ls = k.get("last_seen_at")
        if not ls:
            continue
        try:
            t = datetime.fromisoformat(ls.replace("Z", "+00:00"))
            if (now_utc() - t).total_seconds() < 300:
                bridges_online += 1
        except Exception:
            continue
    pair_tf_index: Dict[str, Any] = {}
    async for row in db.candles.aggregate([
        {"$group": {
            "_id": {"pair": "$pair", "tf": "$timeframe"},
            "count": {"$sum": 1},
            "last_t": {"$max": "$t"},
        }},
        {"$sort": {"_id": 1}},
    ]):
        k = f"{row['_id']['pair']}:{row['_id']['tf']}"
        pair_tf_index[k] = {
            "count": int(row["count"]),
            "last_t": int(row["last_t"]),
            "last_age_min": round((now_ms - int(row["last_t"])) / 60_000, 2),
        }
    bots_total = await db.bots.count_documents({})
    bots_active = await db.bots.count_documents({"is_active": True})
    signals_24h = await db.signals.count_documents(
        {"created_at": {"$gte": (now_utc() - timedelta(hours=24)).isoformat()}},
    )
    trades_open = await db.trades.count_documents({"status": "open"})
    trades_24h = await db.trades.count_documents(
        {"opened_at": {"$gte": (now_utc() - timedelta(hours=24)).isoformat()}},
    )
    recent_reasons: Dict[str, int] = {}
    async for b in db.bots.find(
        {"last_scan_result": {"$exists": True}},
        {"_id": 0, "last_scan_result": 1},
    ).limit(200):
        rsn = (b.get("last_scan_result") or "").split(":")[0]
        recent_reasons[rsn] = recent_reasons.get(rsn, 0) + 1
    scans_recent = await db.bots.count_documents({
        "last_scan_at": {"$gte": (now_utc() - timedelta(minutes=10)).isoformat()},
    })
    return {
        "ts": now_iso(),
        "min_bridge_version": MIN_BRIDGE_VERSION,
        "strategy": {
            "version": STRATEGY_VERSION,
            "conservative": _CONSERVATIVE,
            "min_confidence": STRATEGY_V2_CFG.min_confidence,
        },
        "bridges": {"total": len(bridge_keys), "online_5min": bridges_online},
        "candles": pair_tf_index,
        "bots": {"total": bots_total, "active": bots_active,
                 "scans_in_last_10_min": scans_recent},
        "signals_24h": signals_24h,
        "trades": {"open": trades_open, "opened_24h": trades_24h},
        "recent_scan_reasons": recent_reasons,
    }


@api.get("/admin/trade-postmortem/{trade_id}")
async def admin_trade_postmortem(trade_id: str, admin: dict = Depends(get_current_admin)):
    """v1.8 — Full postmortem for a single trade. Joins trade + signal + bot + v2_context."""
    tr = await db.trades.find_one({"_id": trade_id})
    if not tr:
        raise HTTPException(404, "Trade not found")
    sig = None
    if tr.get("signal_id"):
        sig = await db.signals.find_one({"_id": tr["signal_id"]}, {"_id": 0})
    bot = None
    if tr.get("bot_id"):
        bot = await db.bots.find_one(
            {"_id": tr["bot_id"]},
            {"_id": 0, "name": 1, "pair": 1, "timeframe": 1, "higher_tf_confirmation": 1},
        )
    verdict_flags: List[str] = []
    pnl = tr.get("pnl")
    if pnl is not None and float(pnl) < 0:
        verdict_flags.append("losing_trade")
        sl_p = tr.get("slippage_pips") or 0
        sp = tr.get("spread_at_fill") or 0
        if sl_p > 5:
            verdict_flags.append(f"high_slippage:{sl_p:.1f}p")
        if sp > 0.0003:
            verdict_flags.append(f"wide_spread:{sp:.5f}")
        if tr.get("confidence") is not None and float(tr["confidence"]) < 0.65:
            verdict_flags.append(f"low_confidence:{float(tr['confidence']):.2f}")
        v2c = (sig or {}).get("v2_context") or {}
        if v2c.get("htf_aligned") is False:
            verdict_flags.append("contra_htf")
        if v2c.get("displacement") is None:
            verdict_flags.append("no_displacement")
        if tr.get("exit_reason") == "sl_hit":
            verdict_flags.append("sl_hit")
    return {
        "trade": _strip_id(tr),
        "signal": sig,
        "bot": bot,
        "verdict_flags": verdict_flags,
    }



# ---------- Notifications ----------
@api.post("/notifications/test")
async def notifications_test(user: dict = Depends(get_current_admin)):
    """Admin-only: sends a test Telegram message using the server-side token."""
    ok = await notify_svc.send_test()
    return {"ok": ok}


@api.get("/notifications/status")
async def notifications_status(user: dict = Depends(get_current_user)):
    return {
        "telegram_configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
    }


# ---------- Scanner / bridge health status ----------
@api.get("/system/scanner-status")
async def system_scanner_status(user: dict = Depends(get_current_user)):
    """Per-user system health: data freshness per pair + bridge health."""
    # Bridge health from last bridge_key.last_seen_at for this user
    key = await db.bridge_keys.find_one(
        {"user_id": user["_id"]},
        sort=[("last_seen_at", -1)],
    )
    bh = bridge_health(key.get("last_seen_at") if key else None)
    # Per-pair data freshness — last candle ts per pair/tf seen by THIS user's bots
    pair_status: List[dict] = []
    bots = await db.bots.find({"user_id": user["_id"], "is_active": True}).to_list(50)
    seen: set = set()
    for b in bots:
        k = (b["pair"], b["timeframe"])
        if k in seen:
            continue
        seen.add(k)
        last = await db.candles.find_one(
            {"pair": b["pair"], "timeframe": b["timeframe"]},
            {"_id": 0, "t": 1}, sort=[("t", -1)],
        )
        if not last:
            pair_status.append({"pair": b["pair"], "timeframe": b["timeframe"],
                                "fresh": False, "age_min": None, "reason": "no_data"})
        else:
            age_min = (now_utc().timestamp() * 1000 - int(last["t"])) / 60000.0
            pair_status.append({"pair": b["pair"], "timeframe": b["timeframe"],
                                "fresh": age_min < 60.0, "age_min": round(age_min, 1)})
    return {
        "bridge": bh,
        "data": pair_status,
        "strategy_version": STRATEGY_VERSION,
    }


@app.on_event("startup")
async def on_startup():
    # indexes
    await db.users.create_index("email", unique=True)
    await db.bots.create_index([("user_id", 1), ("is_active", 1)])
    await db.signals.create_index([("user_id", 1), ("created_at", -1)])
    await db.trades.create_index([("user_id", 1), ("opened_at", -1)])
    await db.price_cache.create_index([("symbol", 1), ("ts", -1)])
    await db.price_cache.create_index([("symbol", 1), ("ts", 1)], unique=True)
    await db.bridge_keys.create_index("api_key", unique=True)
    await db.mt5_accounts.create_index([("user_id", 1), ("login", 1)], unique=True)
    await db.users.create_index("referral_code", unique=True, sparse=True)
    await db.users.create_index("referred_by")
    await db.referral_events.create_index([("referrer_id", 1), ("created_at", -1)])
    await db.referral_events.create_index("referee_id")
    await db.risk_state.create_index([("week_start", 1)])
    await db.trades.create_index([("bot_id", 1), ("status", 1)])
    await db.trades.create_index([("bot_id", 1), ("closed_at", -1)])
    # Bridge-fed candle store
    await db.candles.create_index([("pair", 1), ("timeframe", 1), ("t", -1)])
    await db.candles.create_index([("pair", 1), ("timeframe", 1), ("t", 1)], unique=True)

    # Backfill min_confidence on bots that still have 0.6 stored (engine-tuning v2 default = 0.5)
    backfill_res = await db.bots.update_many(
        {"strategy_config.min_confidence": {"$gte": 0.6}},
        {"$set": {"strategy_config.min_confidence": 0.5, "updated_at": now_iso()}},
    )
    if backfill_res.modified_count:
        log.info("backfilled min_confidence=0.5 on %d bots", backfill_res.modified_count)

    # Backfill referral_code for existing users lacking it
    async for u in db.users.find({"referral_code": {"$in": [None, ""]}}, {"_id": 1}):
        for _ in range(5):
            code = generate_referral_code()
            if not await db.users.find_one({"referral_code": code}):
                await db.users.update_one({"_id": u["_id"]}, {"$set": {"referral_code": code}})
                break

    # seed admin
    existing = await db.users.find_one({"email": ADMIN_EMAIL.lower()})
    if not existing:
        uid = str(uuid.uuid4())
        for _ in range(5):
            code = generate_referral_code()
            if not await db.users.find_one({"referral_code": code}):
                break
        await db.users.insert_one({
            "_id": uid,
            "email": ADMIN_EMAIL.lower(),
            "password_hash": hash_password(ADMIN_PASSWORD),
            "display_name": "Admin",
            "avatar_url": None,
            "role": "admin",
            "disabled": False,
            "referral_code": code,
            "referred_by": None,
            "created_at": now_iso(), "updated_at": now_iso(),
        })
        await db.subscriptions.insert_one({
            "_id": str(uuid.uuid4()),
            "user_id": uid, "plan": "yearly", "status": "active",
            "current_period_end": (now_utc() + timedelta(days=3650)).isoformat(),
            "cancel_at_period_end": False,
            "created_at": now_iso(), "updated_at": now_iso(),
        })
        log.info("Admin seeded: %s", ADMIN_EMAIL)
    else:
        # ensure admin role + password sync
        if not verify_password(ADMIN_PASSWORD, existing["password_hash"]):
            await db.users.update_one(
                {"_id": existing["_id"]},
                {"$set": {"password_hash": hash_password(ADMIN_PASSWORD), "role": "admin"}},
            )
            log.info("Admin password refreshed")
        elif existing.get("role") != "admin":
            await db.users.update_one({"_id": existing["_id"]}, {"$set": {"role": "admin"}})

    # seed payment instructions
    if not await db.payment_instructions.find_one({"_id": "main"}):
        await db.payment_instructions.insert_one({
            "_id": "main",
            "monthly_price": 49, "quarterly_price": 129, "yearly_price": 449,
            "bank_details": None, "usdt_trc20_address": None, "usdt_erc20_address": None,
            "btc_address": None, "paypal_email": None, "notes": None,
            "updated_at": now_iso(),
        })

    # scheduler
    scheduler.add_job(scheduled_scan, "interval", minutes=3, id="aurum-scan", replace_existing=True)
    scheduler.start()
    log.info("scheduler started — scans every 3 min")
    # Smoke-test Telegram (one-shot)
    try:
        notify_svc.notify("startup", version=MIN_BRIDGE_VERSION, mode=STRATEGY_VERSION)
    except Exception:
        pass


@app.on_event("shutdown")
async def on_shutdown():
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        pass
    client.close()


# ---------- Mount router + CORS ----------
app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    # Use explicit origins from env + a regex covering lumixtrade.live + emergent.* subdomains.
    # `allow_credentials=True` forbids the literal "*", so the regex is the safe wildcard.
    allow_origins=CORS_ORIGINS if CORS_ORIGINS and CORS_ORIGINS != ["*"] else [],
    allow_origin_regex=CORS_ALLOW_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
