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

**2026-05-21 — Bridge v1.8 + production hardening pass shipped (preview)**

Triggered by: production reported `data_unavailable`, zero auto-signals for 18h, and 5–7 manual-scan signals all losing. Bridge still on v1.6 on FXSVPS while server runs v1.7 → all v1.6 polls returned `signals=[]` with `bridge_outdated`.

Changes:
- `MIN_BRIDGE_VERSION` bumped 1.7 → 1.8 (`server.py`).
- **`strategy_v2.py`** — new `conservative_config()` preset. New `StrategyV2Config` fields: `require_displacement`, `require_fvg_for_bos`, `require_htf_alignment`, `max_atr_ratio`, `min_displacement_body_atr`. Enforced in `_setup_bos_retest`. `_displacement()` now accepts `body_mult` parameter.
- **`server.py`** — new env flag `STRATEGY_CONSERVATIVE` (default `true`). Selects between full v2 config and `conservative_config()`. Logged at startup.
- **New endpoints**:
  - `GET /api/diag/bot/{bot_id}` — full diagnostic snapshot: candle freshness, bridge heartbeat age, scan history, open/closed trade count, signals in last 24h, candle_health verdict (`ok`/`stale`/`insufficient`/`no_data`).
  - `GET /api/admin/system-health` — single aggregated snapshot for dashboard polling: bridges online_5min, candle counts per (pair, tf), bot scans in last 10 min, signals_24h, trades open/24h, recent scan-reason histogram, strategy config.
  - `GET /api/admin/trade-postmortem/{trade_id}` — joins trade + signal + bot + v2_context. Computes `verdict_flags`: `losing_trade`, `high_slippage:Xp`, `wide_spread:X`, `low_confidence:X`, `contra_htf`, `no_displacement`, `sl_hit`.
- **Bridge v1.8** (`backend/static/aurum_bridge.py` + synced to `frontend/public/`):
  - Rotating file log to `<bridge_dir>/logs/aurum_bridge.log` (10 MB × 5).
  - `mt5_is_healthy()` probe + `mt5_reconnect_if_needed()` daemon — exponential backoff 2s→30s, up to 5 attempts before exiting with code 3 for watchdog restart.
  - Health probe runs every 30s in the main loop.
  - Heartbeat enriched with `telemetry`: `uptime_sec`, `mt5_connected`, `last_candles_push_at`, `last_signal_received_at`, `mt5_reconnects`, `last_mt5_reconnect_at`, `last_loop_error`, `streaming_pairs`.
  - Distinct exit codes: 0=clean, 1=initial MT5 fail, 2=config error (no restart), 3=reconnect exhausted.
- **`run_aurum_bridge.bat`** — Windows watchdog wrapper. Restarts the bridge on crash with code-aware backoff. Exits cleanly on 0/2 (config error), retries on 1/3/other.

Verified on preview:
- `GET /api/health` → version `1.8` ✅
- `GET /api/admin/system-health` returns full payload incl. strategy={conservative:true, min_confidence:0.7} ✅
- `GET /api/diag/bot/{id}` returns candle_health=`no_data`, bridge_age_min, etc ✅
- `GET /api/admin/trade-postmortem/{id}` on seeded losing trade returns 7 verdict flags including `contra_htf`, `no_displacement`, `sl_hit`, `high_slippage:6.2p`, `wide_spread:0.00045`, `low_confidence:0.58` ✅
- Backend startup log confirms `strategy_v2 config: conservative=True · min_confidence=0.70 · require_displacement=True · require_htf=True` ✅

Production status: code in preview only — user must hit **Deploy** to push to `lumixtrade.live`. After deploy, user must replace VPS bridge with v1.8 (download from `lumixtrade.live/aurum_bridge.py` + `run_aurum_bridge.bat`).

Deferred to next iteration (explicitly):
- Full React "Live Analytics Dashboard" UI consuming the new endpoints.
- Telegram `no_candles_for_X_min` and `no_scan_for_X_min` alerters (needs `TELEGRAM_BOT_TOKEN` env var first).
- Per-trade postmortem write-up of the user's actual production losing trades (requires sharing `/api/admin/trade-postmortem/{id}` JSON from production).

**2026-05-22 — Phase-1 stabilisation pass shipped (preview)**

Trigger: user shared 3 production trade-postmortem JSONs. Analysis revealed:
- All 3 losers had confidence 0.71–0.74 — just clearing the 0.70 floor.
- 2/3 were liquidity-sweep ranging setups, both contra-HTF (HTF-alignment was not enforced in `_setup_liquidity_reversal` — only `_setup_bos_retest`).
- 1/3 was an XAU buy held 13h 35m past the `max_hold_minutes_swing=480` cap → bridge dropped `TICKET_MAX_HOLD` state after restart.
- 1/3 was a max_hold force-close mis-tagged `exit_reason="sl_hit"` because bridge didn't propagate `reason="max_hold"` to server.

Changes:
- **`strategy_v2.py`**:
  - `conservative_config().min_confidence` 0.70 → **0.78**.
  - `conservative_config().max_hold_minutes_scalp` 30 → **45** (gives in-progress trades 1 ATR of room).
  - New `disable_liquidity_sweep` field — `_setup_liquidity_reversal` returns `None` when True. Default ON in conservative.
  - `_setup_liquidity_reversal` now also enforces `require_htf_alignment` (blocks Trade-#2-style contra-HTF sweeps).
- **`server.py`**:
  - `MIN_BRIDGE_VERSION` bumped 1.8 → **1.8.1**.
  - Per-bot signal cooldown is now **timeframe-aware**: M1→1min, M5→5min, M15→15min, M30→30min, H1→60min, etc. Was hardcoded 3 min, which let the 3-min scheduler re-fire the same setup multiple times inside one M15 bar (the root cause of duplicate-signal losses in the screenshot).
  - `bridge_report` close-event now respects bridge-supplied `reason` for known force-close codes (`max_hold`, `profit_lock`, `manager_close`, `trail`) — only falls back to SL/TP-distance heuristic for organic SL/TP fills.
- **`aurum_bridge.py` v1.8.1**:
  - On startup, hydrate `OPEN_TICKET_OPENED_AT` from `pos.time` and `TICKET_MAX_HOLD` from new env `AURUM_DEFAULT_MAX_HOLD_MIN` (default 480 min) for **every existing magic-990077 position**. Legacy tickets now get force-closed correctly after bridge restart.
  - `_force_close_expired()` no longer bails early on empty `TICKET_MAX_HOLD` dict — uses `DEFAULT_MAX_HOLD_MIN` as fallback.
  - New `CLOSE_REASONS: Dict[int, str]` cache: `_close_position(pos, reason)` stores reason, `reconcile_closed()` reads it and ships normalised `reason` field (`max_hold`/`profit_lock`/`manager_close`) in the close report.
  - New **MOVE-SL-TO-BREAKEVEN at +0.5R** (one-shot per ticket, `BE_DONE` set). Eliminates the "MFE +$5.75 → SL hit −$8.35" pattern observed on the XAG trade. Triggers BEFORE the 1R partial close logic.

Verified on preview:
- Backend startup log: `strategy_v2 config: conservative=True · min_confidence=0.78 · require_displacement=True · require_htf=True` ✅
- Unit test: `_setup_liquidity_reversal` returns None when `disable_liquidity_sweep=True` ✅
- Unit test: contra-HTF sweep blocked when `require_htf_alignment=True` (would have prevented Trade #2 loss) ✅
- Integration test: `POST /api/bridge-report {reason:"max_hold"}` → trade.exit_reason = "max_hold" (NOT mis-tagged sl_hit) ✅
- Integration test: M5 bot cooldown reports `"cooldown:5min"`; M15 bot reports `"cooldown:15min"` ✅

Production status: code in preview only — user must hit **Deploy** and replace VPS bridge with v1.8.1.

Defer to Phase-2:
- Correlation cap on USD-cluster (max 2 concurrent USD-correlated trades across XAU+EUR+GBP+AUD)
- Bar-close-only scanner (only evaluate setups on freshly-closed bars)
- EURUSD TP-floor fix (enforce TP-distance ≥ 2× SL-distance regardless of ATR)
- Lot-cap during drawdown (halve base lot when weekly DD > 10%)
- React Live Analytics Dashboard

Defer to Phase-3:
- Narrow bots to XAU-only + BOS-retest-only for 50-trade edge validation
- Per-pair session-window restriction (London/NY open only)
