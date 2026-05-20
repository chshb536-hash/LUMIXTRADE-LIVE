#!/usr/bin/env python3
"""
Aurum FX — Backtester (Dukascopy data)
======================================

Walks the exact same `engine.generate_signal` used in live trading bar-by-bar over
historical OHLC bars fetched from Dukascopy, simulates fills against next bar high/low,
applies the same SL/TP logic, and emits a metrics report.

Conservative bar-by-bar fill model:
  • Signal is generated on the close of bar N (only data up to N is visible).
  • Entry happens at bar N close.
  • From bar N+1 onwards, each bar's [low, high] range is scanned:
      - If both SL and TP would be touched in the same bar, **SL wins** (worst-case fill).
      - If only one is touched, that side fills at the SL/TP price.
  • If no SL/TP touched within `max_bars_in_trade` (default 240 bars ≈ 60h on M15), the
    trade is force-closed at the bar's close ("timeout").
  • One trade at a time per symbol (matches single-bot reality).
  • Lot sizing uses the same `calc_lot()` from engine.py with the running balance.

Outputs:
  • metrics: total_trades, win_rate, profit_factor, max_drawdown, sharpe, avg_win/loss,
             total_net_profit, expectancy, equity_curve
  • JSON file at `/app/backend/backtest_results/<symbol>_<tf>_<start>_<end>.json`
  • Optional CSV of every closed trade with `--csv`

Examples:
  python backtest.py --symbol XAUUSD --timeframe M15 --start 2025-01-01 --end 2025-06-30
  python backtest.py --symbol EURUSD --timeframe H1  --start 2024-01-01 --end 2024-12-31 --initial_balance 5000 --csv
"""
from __future__ import annotations
import argparse
import csv
import json
import math
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import dukascopy_python as dp

# Reuse the LIVE engine — single source of truth.
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from engine import Candle, StrategyConfig, generate_signal, calc_lot  # noqa: E402

# Dukascopy symbol mapping
DUKAS_SYM = {
    "XAUUSD": "XAU/USD", "XAGUSD": "XAG/USD",
    "EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "USDJPY": "USD/JPY",
    "AUDUSD": "AUD/USD", "USDCAD": "USD/CAD", "NZDUSD": "NZD/USD",
}
DUKAS_INTERVAL = {
    "M5":  dp.INTERVAL_MIN_5,
    "M15": dp.INTERVAL_MIN_15,
    "M30": dp.INTERVAL_MIN_30,
    "H1":  dp.INTERVAL_HOUR_1,
    "H4":  dp.INTERVAL_HOUR_4,
    "D1":  dp.INTERVAL_DAY_1,
}


# ---------- Domain ----------
@dataclass
class ClosedTrade:
    idx: int
    pair: str
    side: str             # buy/sell
    entry_time: str
    exit_time: str
    entry: float
    exit_price: float
    initial_sl: float
    initial_tp: float
    lot: float
    pnl: float
    pnl_pct: float        # vs balance at entry
    confidence: float
    signal_reason: str
    regime: str
    session: str
    exit_reason: str      # tp_hit / sl_hit / timeout
    duration_bars: int
    mfe: float            # max favourable excursion in USD
    mae: float            # max adverse excursion in USD
    balance_after: float


def _usd_per_price_per_lot(pair: str) -> float:
    """Standard MT5 contract sizes (mirrors engine.calc_lot)."""
    if pair.startswith("XAU"):
        return 100.0
    if pair.startswith("XAG"):
        return 5000.0
    if pair.endswith("JPY"):
        return 1000.0
    return 100000.0


def _pip_value_usd(pair: str, lot: float, current_price: Optional[float] = None) -> float:
    """USD value of a 1-unit price move for `lot`. For USD-quoted pairs this is constant;
    for USD-base pairs (USDJPY, USDCAD, USDCHF), it depends on current price.
    """
    units = _usd_per_price_per_lot(pair) * lot
    if pair.startswith("USD") and current_price and current_price > 0:
        return units / current_price
    return units


# ---------- Data fetch ----------
def fetch_history(pair: str, timeframe: str, start: datetime, end: datetime) -> List[Candle]:
    sym = DUKAS_SYM.get(pair.upper())
    if sym is None:
        raise SystemExit(f"unsupported symbol {pair}; supported: {list(DUKAS_SYM)}")
    iv = DUKAS_INTERVAL.get(timeframe.upper())
    if iv is None:
        raise SystemExit(f"unsupported timeframe {timeframe}; supported: {list(DUKAS_INTERVAL)}")
    print(f"[fetch] dukascopy {sym} {timeframe} {start.date()} → {end.date()} …", flush=True)
    df = dp.fetch(sym, iv, dp.OFFER_SIDE_BID, start, end, max_retries=5)
    if df is None or df.empty:
        raise SystemExit("dukascopy returned no data for this range")
    out: List[Candle] = []
    # df.index is a DatetimeIndex (tz-aware) → convert to ms
    for ts, row in df.iterrows():
        ms = int(ts.timestamp() * 1000)
        out.append(Candle(t=ms, o=float(row["open"]), h=float(row["high"]),
                          l=float(row["low"]), c=float(row["close"])))
    print(f"[fetch] got {len(out)} bars", flush=True)
    return out


# ---------- Backtest core ----------
@dataclass
class BacktestArgs:
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    initial_balance: float
    risk_per_trade: float          # %
    min_confidence: float
    max_bars_in_trade: int
    warmup_bars: int


@dataclass
class Result:
    symbol: str
    timeframe: str
    start: str
    end: str
    initial_balance: float
    final_balance: float
    total_net_profit: float
    total_trades: int
    wins: int
    losses: int
    breakeven: int
    win_rate_pct: float
    profit_factor: float           # gross_profit / gross_loss (abs)
    expectancy_usd: float
    avg_win_usd: float
    avg_loss_usd: float            # negative number
    largest_win_usd: float
    largest_loss_usd: float
    max_drawdown_usd: float
    max_drawdown_pct: float
    sharpe_per_trade: float        # avg / stdev of trade returns
    equity_curve: list             # [{time, equity}]
    trades: list = field(default_factory=list)


def run_backtest(args: BacktestArgs) -> Result:
    candles = fetch_history(args.symbol, args.timeframe, args.start, args.end)
    if len(candles) < args.warmup_bars + 50:
        raise SystemExit(f"not enough bars ({len(candles)}) for backtesting")

    cfg = StrategyConfig(min_confidence=args.min_confidence)
    balance = args.initial_balance
    equity_peak = balance
    max_dd_usd = 0.0
    max_dd_pct = 0.0
    trades: List[ClosedTrade] = []
    equity_curve = [{"time": _iso(candles[args.warmup_bars]["t"]), "equity": round(balance, 2)}]

    i = args.warmup_bars
    n = len(candles)
    while i < n - 1:
        # Generate signal using only data up to and including bar i.
        sig = generate_signal(candles[: i + 1], cfg)
        if sig is None:
            i += 1
            continue
        # Lot sizing using current balance, same as live.
        sl_dist_price = abs(sig.entry - sig.sl)
        if sl_dist_price <= 0:
            i += 1
            continue
        lot = calc_lot(balance, args.risk_per_trade, sl_dist_price, args.symbol)
        usd_per_price = _pip_value_usd(args.symbol, lot, current_price=sig.entry)
        # Simulate the trade forward bar-by-bar.
        entry_idx = i
        entry_price = sig.entry
        sl = sig.sl
        tp = sig.tp
        side = sig.side
        mfe_usd = 0.0
        mae_usd = 0.0
        exit_idx = None
        exit_price = None
        exit_reason = "timeout"
        end_idx = min(n - 1, i + args.max_bars_in_trade)
        for j in range(i + 1, end_idx + 1):
            bar = candles[j]
            # Track MFE / MAE in USD this bar
            if side == "buy":
                fav = (bar["h"] - entry_price) * usd_per_price
                adv = (bar["l"] - entry_price) * usd_per_price  # negative if went against
            else:
                fav = (entry_price - bar["l"]) * usd_per_price
                adv = (entry_price - bar["h"]) * usd_per_price
            if fav > mfe_usd:
                mfe_usd = fav
            if adv < mae_usd:
                mae_usd = adv
            # Touch detection — conservative: if SL & TP both touched in same bar, SL wins.
            sl_hit = (side == "buy" and bar["l"] <= sl) or (side == "sell" and bar["h"] >= sl)
            tp_hit = (side == "buy" and bar["h"] >= tp) or (side == "sell" and bar["l"] <= tp)
            if sl_hit and tp_hit:
                exit_price = sl
                exit_reason = "sl_hit"
                exit_idx = j
                break
            if sl_hit:
                exit_price = sl
                exit_reason = "sl_hit"
                exit_idx = j
                break
            if tp_hit:
                exit_price = tp
                exit_reason = "tp_hit"
                exit_idx = j
                break
        if exit_idx is None:
            # Force close at end of window.
            exit_idx = end_idx
            exit_price = candles[end_idx]["c"]
            exit_reason = "timeout"

        # Realized USD PnL
        if side == "buy":
            pnl_usd = (exit_price - entry_price) * usd_per_price
        else:
            pnl_usd = (entry_price - exit_price) * usd_per_price
        balance_before = balance
        balance += pnl_usd
        # Drawdown tracking
        if balance > equity_peak:
            equity_peak = balance
        dd_usd = equity_peak - balance
        if dd_usd > max_dd_usd:
            max_dd_usd = dd_usd
            max_dd_pct = (dd_usd / equity_peak) * 100.0 if equity_peak > 0 else 0.0

        trades.append(ClosedTrade(
            idx=len(trades) + 1,
            pair=args.symbol, side=side,
            entry_time=_iso(candles[entry_idx]["t"]),
            exit_time=_iso(candles[exit_idx]["t"]),
            entry=round(entry_price, 5), exit_price=round(exit_price, 5),
            initial_sl=round(sl, 5), initial_tp=round(tp, 5),
            lot=lot,
            pnl=round(pnl_usd, 2),
            pnl_pct=round((pnl_usd / balance_before) * 100.0, 3) if balance_before > 0 else 0.0,
            confidence=round(sig.confidence, 2),
            signal_reason=sig.reason, regime=sig.regime, session=sig.session,
            exit_reason=exit_reason,
            duration_bars=exit_idx - entry_idx,
            mfe=round(mfe_usd, 2), mae=round(mae_usd, 2),
            balance_after=round(balance, 2),
        ))
        equity_curve.append({"time": _iso(candles[exit_idx]["t"]), "equity": round(balance, 2)})
        # Move to bar after exit so we never overlap trades (single-position model).
        i = exit_idx + 1

    return _summarize(args, trades, equity_curve, balance, max_dd_usd, max_dd_pct)


# ---------- Metrics ----------
def _summarize(a: BacktestArgs, trades: List[ClosedTrade], equity_curve: list,
               final_balance: float, max_dd_usd: float, max_dd_pct: float) -> Result:
    n = len(trades)
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    bes = [t.pnl for t in trades if t.pnl == 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)  # negative
    win_rate = (len(wins) / n * 100.0) if n else 0.0
    profit_factor = (gross_profit / abs(gross_loss)) if gross_loss else (float("inf") if gross_profit else 0.0)
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    expectancy = ((len(wins) / n) * avg_win + (len(losses) / n) * avg_loss) if n else 0.0
    largest_win = max(wins) if wins else 0.0
    largest_loss = min(losses) if losses else 0.0
    # Sharpe per-trade (no annualisation — trade count varies by TF/period)
    rets = [t.pnl_pct for t in trades]
    if len(rets) >= 2:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        stdev = math.sqrt(var)
        sharpe = (mean / stdev) if stdev > 0 else 0.0
    else:
        sharpe = 0.0

    return Result(
        symbol=a.symbol, timeframe=a.timeframe,
        start=a.start.date().isoformat(), end=a.end.date().isoformat(),
        initial_balance=a.initial_balance, final_balance=round(final_balance, 2),
        total_net_profit=round(final_balance - a.initial_balance, 2),
        total_trades=n, wins=len(wins), losses=len(losses), breakeven=len(bes),
        win_rate_pct=round(win_rate, 2),
        profit_factor=round(profit_factor, 3) if profit_factor != float("inf") else "inf",
        expectancy_usd=round(expectancy, 2),
        avg_win_usd=round(avg_win, 2), avg_loss_usd=round(avg_loss, 2),
        largest_win_usd=round(largest_win, 2), largest_loss_usd=round(largest_loss, 2),
        max_drawdown_usd=round(max_dd_usd, 2),
        max_drawdown_pct=round(max_dd_pct, 2),
        sharpe_per_trade=round(sharpe, 3),
        equity_curve=equity_curve,
        trades=[asdict(t) for t in trades],
    )


# ---------- I/O ----------
def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def save_outputs(res: Result, write_csv: bool) -> dict:
    out_dir = ROOT / "backtest_results"
    out_dir.mkdir(exist_ok=True)
    base = f"{res.symbol}_{res.timeframe}_{res.start}_{res.end}"
    json_path = out_dir / f"{base}.json"
    with open(json_path, "w") as f:
        json.dump(asdict(res), f, indent=2, default=str)
    paths = {"json": str(json_path)}
    if write_csv and res.trades:
        csv_path = out_dir / f"{base}_trades.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(res.trades[0].keys()))
            writer.writeheader()
            for t in res.trades:
                writer.writerow(t)
        paths["csv"] = str(csv_path)
    return paths


def print_report(res: Result):
    bar = "─" * 56
    print()
    print(bar)
    print(f"  AURUM FX BACKTEST · {res.symbol} {res.timeframe} · {res.start} → {res.end}")
    print(bar)
    print(f"  Initial balance       : ${res.initial_balance:,.2f}")
    print(f"  Final balance         : ${res.final_balance:,.2f}")
    pct = ((res.final_balance / res.initial_balance) - 1) * 100 if res.initial_balance else 0
    print(f"  Total net profit      : ${res.total_net_profit:+,.2f}  ({pct:+.2f}%)")
    print(bar)
    print(f"  Total trades          : {res.total_trades}")
    print(f"   • wins / losses / be : {res.wins} / {res.losses} / {res.breakeven}")
    print(f"  Win rate              : {res.win_rate_pct:.2f}%")
    print(f"  Profit factor         : {res.profit_factor}")
    print(f"  Expectancy            : ${res.expectancy_usd:+,.2f} per trade")
    print(f"  Avg win / Avg loss    : ${res.avg_win_usd:+,.2f} / ${res.avg_loss_usd:+,.2f}")
    print(f"  Largest win / loss    : ${res.largest_win_usd:+,.2f} / ${res.largest_loss_usd:+,.2f}")
    print(f"  Max drawdown          : ${res.max_drawdown_usd:,.2f} ({res.max_drawdown_pct:.2f}%)")
    print(f"  Sharpe per-trade      : {res.sharpe_per_trade:+.3f}")
    print(bar)


# ---------- CLI ----------
def _parse_date(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit(f"bad date {s!r}; use YYYY-MM-DD")


def main():
    ap = argparse.ArgumentParser(description="Aurum FX backtester (Dukascopy)")
    ap.add_argument("--symbol", required=True, help="e.g. XAUUSD")
    ap.add_argument("--timeframe", default="M15", choices=list(DUKAS_INTERVAL))
    ap.add_argument("--start", required=True, type=_parse_date, help="YYYY-MM-DD UTC")
    ap.add_argument("--end",   required=True, type=_parse_date, help="YYYY-MM-DD UTC")
    ap.add_argument("--initial_balance", type=float, default=10000.0)
    ap.add_argument("--risk_per_trade",  type=float, default=1.0, help="percent of balance per trade")
    ap.add_argument("--min_confidence",  type=float, default=0.5)
    ap.add_argument("--max_bars",        type=int,   default=240, help="auto-close trade after N bars without TP/SL")
    ap.add_argument("--warmup",          type=int,   default=80,  help="bars to skip before signal generation")
    ap.add_argument("--csv", action="store_true", help="also save trade-by-trade CSV")
    a = ap.parse_args()
    if a.end <= a.start:
        raise SystemExit("--end must be after --start")
    args = BacktestArgs(
        symbol=a.symbol.upper(), timeframe=a.timeframe.upper(),
        start=a.start, end=a.end,
        initial_balance=a.initial_balance,
        risk_per_trade=a.risk_per_trade,
        min_confidence=a.min_confidence,
        max_bars_in_trade=a.max_bars,
        warmup_bars=a.warmup,
    )
    res = run_backtest(args)
    print_report(res)
    paths = save_outputs(res, write_csv=a.csv)
    print(f"  Saved JSON            : {paths['json']}")
    if "csv" in paths:
        print(f"  Saved CSV             : {paths['csv']}")
    print()


if __name__ == "__main__":
    main()
