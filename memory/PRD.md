# LumixTrade — PRD

## Original Problem Statement
User uploaded `Lumix-Trade-Latest-main.zip` — a complete FastAPI + React + MongoDB trading bot — and asked for as-is deployment to `lumixtrade.live`, with admin `admin@bot.com / password` seeded and an active subscription, CORS allowing `https://lumixtrade.live`, and `AURUM_API_URL=https://lumixtrade.live/api`.

## Architecture (as shipped in the ZIP)
- **Backend**: FastAPI `server.py` (2493 lines) + APScheduler (3-min market scans) + MongoDB (`lumixtrade_db`). All routes under `/api`.
- **Data layer**: MT5-native via `aurum_bridge.py` → `/api/bridge-candles` → `db.candles`. No synthetic fallback. `data_validation.py` enforces integrity.
- **Strategy engine v2** (`strategy_v2.py`): SMC with regime router (BOS retest / squeeze breakout / liquidity sweep). Composite confidence scoring.
- **Risk engine** (`risk_engine.py`): adaptive lot sizing, volatility circuit breaker, bridge heartbeat health.
- **Notifications** (`notifications.py`): Telegram async fire-and-forget, 12 templated events.
- **MT5 Bridge v1.6** (`backend/static/aurum_bridge.py`): candle streaming, force-close, spread protection, slippage telemetry, partials, trailing stop, profit lock.
- **Frontend**: React 18 + Tailwind + Radix (CRA via craco).

## User Personas
1. **Admin** — manages users/subscriptions, monitors system health.
2. **Trader** — registers, creates bots, runs bridge on Windows VPS, receives Telegram alerts.

## Core Requirements
- Algorithmic FX & XAU/USD trading on MT5
- Multi-strategy with regime router + composite confidence scoring
- Server-enforced risk: daily loss limit, weekly DD halt, correlation cap, news blackout, spread cap, slippage cap, vol circuit breaker, heartbeat monitor
- Real-time Telegram alerts for signals/fills/SL/TP/halt
- Subscription gating ($49 / $129 / $449)

## What's Been Implemented

**2026-05-20 — Deployment as requested**
- Extracted ZIP into `/app/backend` and `/app/frontend` (preserved protected `.env` slots).
- `/app/backend/.env` configured with:
  - `MONGO_URL=mongodb://localhost:27017`, `DB_NAME=lumixtrade_db`
  - `ADMIN_EMAIL=admin@bot.com`, `ADMIN_PASSWORD=password`
  - `CORS_ORIGINS` includes `https://lumixtrade.live`, `https://www.lumixtrade.live`, preview URL
  - `AURUM_API_URL=https://lumixtrade.live/api`
  - `TWELVE_DATA_API_KEY` placeholder set (server is MT5-bridge-native; key not used at runtime)
  - `JWT_SECRET` rotated; `STRATEGY_VERSION=v2`; `AURUM_BRIDGE_API_KEY` set.
- `/app/frontend/.env` preserved (`REACT_APP_BACKEND_URL`).
- Installed `pip install -r backend/requirements.txt` and `yarn install` for frontend.
- `supervisorctl restart backend frontend` — both `RUNNING`.
- Admin user auto-seeded with `yearly / active` subscription (10y) on startup. Scheduler running 3-min scans.

### Verification (all passed)
- `GET /api/health` → `{"status":"ok","service":"lumixtrade-api","version":"1.6"}` ✅
- `POST /api/auth/login` with `admin@bot.com / password` → 200 + JWT ✅
- `GET /api/subscriptions/me` → `plan=yearly, status=active` ✅
- `POST /api/bridge-poll` without key → 401 (correct, auth-protected) ✅
- `POST /api/bridge-poll` with generated key → 200 (returns signals + bridge-version warning for `unknown` bridge) ✅
- External preview URL `https://auto-scan-bot.preview.emergentagent.com/api/health` → 200 ✅
- Frontend home → 200 ✅

### Bridge key (admin)
A default admin bridge key was created and stored in `/app/memory/test_credentials.md`.

## Outstanding / User Actions Required

> **The platform does not allow agents to push the production Deploy or bind a custom domain.** These two steps are user-driven from the Emergent dashboard:

1. **Click `Deploy` → `Deploy Now`** in the Emergent UI (cost: 50 credits/month). Build takes ~10–15 min. You will get a `*.emergent.host` URL.
2. **Click `Link Domain` → enter `lumixtrade.live`** → follow the **Entri** flow to set the DNS records it prompts (remove conflicting A records first). Propagation up to 15 min.
3. After domain is live, no code change is needed — `CORS_ORIGINS` already includes `https://lumixtrade.live` and `AURUM_API_URL` already points to `https://lumixtrade.live/api`.

## Prioritized Backlog
- **P1** Add Telegram bot token + chat id to `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) to activate the notification service.
- **P1** Replace placeholder `TWELVE_DATA_API_KEY` if any v1 strategy paths are re-enabled (v2 is MT5-bridge-native — key not strictly needed).
- **P2** Wire MT5 bridge v1.6 on a Windows VPS using the bridge API key in `test_credentials.md`.
- **P2** Configure `payment_instructions` (bank / USDT-TRC20 / USDT-ERC20 / BTC / PayPal) via admin endpoint.

**2026-05-20 — Bridge v1.7 fix shipped (preview)**
Reason: Production bots reported `data_unavailable:missing_candles` because the bridge only streamed the hardcoded `AURUM_STREAM_PAIRS` default (`XAUUSD:M15,EURUSD:M15,GBPUSD:M15,USDJPY:M15`) — M5 / H1 / H4 bots starved, and even M15 occasionally tripped a too-strict 2× gap validator.

Changes:
- `MIN_BRIDGE_VERSION` bumped from `1.6` → `1.7` (`server.py` + bridge file). Old bridges receive `bridge_outdated` warning + zero signals.
- New `GET /api/bridge/stream-config` (auth: `x-aurum-bridge-key`) returns union of every (pair, timeframe) from the user's bots (active OR paused) plus any `higher_tf_confirmation` TF.
- `aurum_bridge.py` v1.7 now calls `refresh_stream_pairs()` on every `push_candles()` tick (cached `AURUM_STREAM_CFG_INTERVAL=60s`). Falls back to env list only if endpoint unreachable.
- `data_validation.py` softened: `max_gap_multiplier` default `2.0` → `5.0`. Gaps 2×–5× counted as `soft_gaps` (warning only, trading continues). Gaps > 5× still hard-fail with `missing_candles`.
- New `GET /api/diag/candles?pair=&timeframe=` (auth: any logged-in user) — returns `{count, first_t, last_t, last_age_min, gap_count_2x, gap_count_5x, tf_ms}`.
- Bridge file also synced to `/app/frontend/public/aurum_bridge.py` (the user-downloadable copy).

Verified on preview:
- `/api/bridge/stream-config` returns 5 (pair, tf) combos after creating 3 bots with mixed TFs + higher_tf_confirmation.
- `/api/bridge-candles` accepts 150-bar XAUUSD M5 push → 150 written.
- `/api/diag/candles?pair=XAUUSD&timeframe=M5` returns `count=150, gap_count_2x=0, gap_count_5x=0`.
- Validation unit test: 2-bar M15 hole → `ok=True soft_gaps=1` (previously `ok=False missing_candles`). 6-bar hole → `ok=False missing_candles` (correctly still blocked).

Production status: code is in preview only — user must hit **Deploy** to push to `auto-scan-bot.emergent.host` / `lumixtrade.live`.
