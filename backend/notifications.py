"""
Aurum FX — Telegram notifications service.

Async, fire-and-forget, rate-limited. Notification failures are logged but NEVER
block the trading flow. Single-recipient model (one chat per platform — admin/owner).
Uses raw Telegram Bot API over httpx (no extra deps).

Events supported:
  signal       — new signal generated
  fill         — trade filled at broker
  sl_hit       — stop loss closed
  tp_hit       — take profit closed
  trail        — trailing stop moved
  partial      — partial close at +1R
  dd_halt      — weekly drawdown halt triggered
  daily_loss   — bot hit daily loss limit
  bridge_off   — bridge offline (no ping > threshold)
  data_bad     — market data validation failure
  vol_halt     — volatility circuit breaker tripped
  startup      — server started (smoke test)
"""
from __future__ import annotations
import os
import asyncio
import logging
import time
from typing import Any, Dict, Optional

import httpx

log = logging.getLogger("aurum.notify")

TELEGRAM_API = "https://api.telegram.org"
_RATE_LIMIT_SEC = 1.0  # 1 msg / sec / chat (Telegram limit)
_last_send_ts: Dict[str, float] = {}
_lock = asyncio.Lock()


def _enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _esc(s: str) -> str:
    """HTML escape for Telegram parseMode=HTML (much safer than MarkdownV2)."""
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _send_raw(text: str, chat_id: Optional[str] = None) -> bool:
    """Direct send. Returns True on 200 OK. Never raises."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    # Throttle per chat
    async with _lock:
        prev = _last_send_ts.get(chat, 0.0)
        wait = _RATE_LIMIT_SEC - (time.monotonic() - prev)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_send_ts[chat] = time.monotonic()
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(url, json={
                "chat_id": chat, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            })
        if r.status_code != 200:
            log.warning("telegram send failed %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("telegram send exception: %s", e)
        return False


def notify(event: str, **kwargs: Any) -> None:
    """Fire-and-forget notification. Schedules send on the running loop and returns
    immediately. NEVER blocks the caller. Drops silently if no event loop or no token."""
    if not _enabled():
        return
    try:
        text = _render(event, kwargs)
    except Exception as e:
        log.warning("notify render failed for %s: %s", event, e)
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send_raw(text))
    except RuntimeError:
        # No running loop — last-ditch: spin a one-off
        try:
            asyncio.run(_send_raw(text))
        except Exception:
            pass


# ---------- Templates ----------
def _render(event: str, k: Dict[str, Any]) -> str:
    """Templates use HTML (Telegram parse_mode='HTML')."""
    if event == "signal":
        return (
            f"🎯 <b>SIGNAL · {_esc(k.get('pair'))} {_esc(k.get('side','').upper())}</b>\n"
            f"<i>{_esc(k.get('reason',''))}</i>\n\n"
            f"Entry:   <code>{k.get('entry'):.5f}</code>\n"
            f"SL:      <code>{k.get('sl'):.5f}</code>\n"
            f"TP:      <code>{k.get('tp'):.5f}</code>\n"
            f"Lot:     <code>{k.get('lot'):.2f}</code>\n"
            f"Conf:    <b>{(k.get('confidence',0)*100):.0f}%</b>\n"
            f"Regime:  {_esc(k.get('regime'))} · Session: {_esc(k.get('session'))}\n"
            f"Mode:    {_esc(k.get('mode','swing').upper())}"
        )
    if event == "fill":
        return (f"✅ <b>FILLED · {_esc(k.get('pair'))} {_esc(k.get('side','').upper())}</b>\n"
                f"Price: <code>{k.get('price'):.5f}</code> · Lot: <code>{k.get('lot'):.2f}</code> · "
                f"Ticket: <code>{k.get('ticket')}</code>")
    if event in ("tp_hit", "sl_hit"):
        emoji = "💰" if event == "tp_hit" else "🛑"
        label = "TP HIT" if event == "tp_hit" else "SL HIT"
        pnl = k.get("pnl") or 0
        sign = "+" if pnl >= 0 else ""
        return (f"{emoji} <b>{label} · {_esc(k.get('pair'))} {_esc(k.get('side','').upper())}</b>\n"
                f"PnL: <b>{sign}${pnl:.2f}</b> · Exit: <code>{(k.get('exit_price') or 0):.5f}</code>\n"
                f"Ticket: <code>{k.get('ticket')}</code>")
    if event == "trail":
        return (f"📈 <b>TRAIL · {_esc(k.get('pair'))}</b>\n"
                f"SL → <code>{k.get('new_sl'):.5f}</code> "
                f"(locked <code>${k.get('locked'):.2f}</code>) · "
                f"Ticket: <code>{k.get('ticket')}</code>")
    if event == "partial":
        return (f"✂️ <b>PARTIAL +1R · {_esc(k.get('pair'))}</b>\n"
                f"Closed <code>{k.get('closed_lot'):.2f}</code> of "
                f"<code>{k.get('total_lot'):.2f}</code> · SL → break-even\n"
                f"Ticket: <code>{k.get('ticket')}</code>")
    if event == "dd_halt":
        return (f"⛔ <b>DRAWDOWN HALT</b>\n"
                f"Weekly DD: <b>{k.get('drawdown_pct'):.2f}%</b> "
                f"(limit {k.get('limit_pct')}%) — all bots paused until Monday UTC.")
    if event == "daily_loss":
        return (f"🚧 <b>DAILY LOSS LIMIT · {_esc(k.get('bot_name'))}</b>\n"
                f"PnL today: <b>${k.get('pnl'):.2f}</b> / limit -${k.get('limit'):.2f}\n"
                f"Bot paused until UTC midnight.")
    if event == "bridge_off":
        last = k.get("last_seen_min", "?")
        return (f"🔌 <b>BRIDGE OFFLINE</b>\n"
                f"No heartbeat for <b>{last}</b> min. New signals will not execute.")
    if event == "bridge_on":
        return f"🟢 <b>BRIDGE ONLINE</b> · {_esc(k.get('account',''))}"
    if event == "data_bad":
        return (f"⚠️ <b>DATA UNAVAILABLE · {_esc(k.get('pair'))}</b>\n"
                f"Reason: <i>{_esc(k.get('reason'))}</i>\n"
                f"Signal generation paused for this pair.")
    if event == "vol_halt":
        return (f"🌪 <b>VOLATILITY CIRCUIT · {_esc(k.get('pair'))}</b>\n"
                f"ATR ratio <b>{k.get('ratio'):.1f}×</b> median — pair paused.")
    if event == "spread_block":
        return (f"💱 <b>SPREAD BLOCK · {_esc(k.get('pair'))}</b>\n"
                f"Spread <code>{k.get('spread')}</code> > cap <code>{k.get('cap')}</code>. "
                f"Signal dropped.")
    if event == "startup":
        return (f"🚀 <b>Aurum FX server online</b>\n"
                f"Version: <code>{_esc(k.get('version'))}</code> · "
                f"Scanner: <b>{_esc(k.get('mode'))}</b>")
    # Generic fallback
    return f"<b>{_esc(event)}</b>\n<pre>{_esc(str(k))[:500]}</pre>"


async def send_test(chat_id: Optional[str] = None) -> bool:
    """Used by /api/notifications/test endpoint."""
    text = ("🧪 <b>Test message from Aurum FX</b>\n"
            "If you see this, your Telegram integration is working. "
            "You'll get real-time alerts for signals, fills, SL/TP and risk events.")
    return await _send_raw(text, chat_id)
