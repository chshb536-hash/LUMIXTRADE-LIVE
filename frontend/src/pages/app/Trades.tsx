import { useEffect, useMemo, useState } from "react";
import { apiGet } from "@/api/client";
import { AppShell } from "@/components/AppShell";
import { Panel, Stat } from "@/components/Panel";

type Trade = {
  id: string; pair: string; side: string; lot: number;
  entry: number; sl: number | null; tp: number | null;
  initial_sl: number | null; initial_tp: number | null;
  exit_price: number | null; pnl: number | null;
  live_pnl?: number | null;
  mfe_pnl?: number | null; mae_pnl?: number | null;
  confidence?: number | null; signal_reason?: string | null;
  regime?: string | null; session?: string | null;
  exit_reason?: string | null;
  status: string; mt5_ticket?: number | null;
  opened_at: string; closed_at?: string | null;
};

const dec = (pair: string) => (pair?.startsWith("XAU") ? 2 : pair?.startsWith("XAG") ? 4 : pair?.endsWith("JPY") ? 3 : 5);

function fmtDur(open: string, close: string | null | undefined): string {
  if (!close) return "—";
  const ms = new Date(close).getTime() - new Date(open).getTime();
  if (!isFinite(ms) || ms < 0) return "—";
  const m = Math.floor(ms / 60000);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

function reasonBadge(t: Trade): { label: string; cls: string } | null {
  const r = t.exit_reason;
  if (!r) return null;
  if (r === "tp_hit") return { label: "TP HIT", cls: "bg-bull/15 text-bull border-bull/30" };
  if (r === "sl_hit") return { label: "SL HIT", cls: "bg-bear/15 text-bear border-bear/30" };
  if (r.startsWith("Aurum") || r.includes("partial") || r.includes("TP1R")) return { label: "PARTIAL/LOCK", cls: "bg-primary/15 text-primary border-primary/30" };
  return { label: r.toUpperCase().slice(0, 16), cls: "bg-muted text-muted-foreground border-border" };
}

export default function Trades() {
  const [rows, setRows] = useState<Trade[]>([]);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    let mounted = true;
    const load = async () => {
      try {
        const data = await apiGet<Trade[]>("/trades", { params: { limit: 500 } });
        if (mounted) setRows(data ?? []);
      } catch { /* ignored */ }
    };
    load();
    const iv = setInterval(load, 8000);
    return () => { mounted = false; clearInterval(iv); };
  }, []);

  const closed = useMemo(() => rows.filter((t) => t.status === "closed"), [rows]);
  const open = useMemo(() => rows.filter((t) => t.status === "open"), [rows]);
  const wins = closed.filter((t) => Number(t.pnl) > 0).length;
  const winrate = closed.length ? (wins / closed.length) * 100 : 0;
  const totalPnl = closed.reduce((a, t) => a + (Number(t.pnl) || 0), 0);
  const avgWin = (() => {
    const w = closed.filter((t) => Number(t.pnl) > 0).map((t) => Number(t.pnl));
    return w.length ? w.reduce((a, b) => a + b, 0) / w.length : 0;
  })();
  const avgLoss = (() => {
    const l = closed.filter((t) => Number(t.pnl) < 0).map((t) => Number(t.pnl));
    return l.length ? l.reduce((a, b) => a + b, 0) / l.length : 0;
  })();

  const display = showAll ? rows : [...rows].sort((a, b) => (a.status === b.status ? 0 : a.status === "open" ? -1 : 1)).slice(0, 100);

  return (
    <AppShell>
      <div className="container py-6 space-y-4" data-testid="trades-page">
        <div className="grid md:grid-cols-5 gap-4">
          <Panel><Stat label="Closed Trades" value={closed.length} /></Panel>
          <Panel><Stat label="Win Rate" value={`${winrate.toFixed(1)}%`} delta={{ value: `${wins}W / ${closed.length - wins}L`, positive: winrate >= 50 }} /></Panel>
          <Panel><Stat label="Total P&L" value={`${totalPnl >= 0 ? "+" : ""}$${totalPnl.toFixed(2)}`} delta={{ value: closed.length ? `${closed.length} closed` : "no data", positive: totalPnl >= 0 }} /></Panel>
          <Panel><Stat label="Avg Win / Loss" value={`$${avgWin.toFixed(0)} / $${avgLoss.toFixed(0)}`} /></Panel>
          <Panel><Stat label="Open" value={open.length} /></Panel>
        </div>
        <Panel
          title="TRADE JOURNAL"
          subtitle={`${rows.length} records · open + last 100 by default`}
          actions={
            <button onClick={() => setShowAll(!showAll)} className="font-mono text-[10px] tracking-widest text-muted-foreground hover:text-primary" data-testid="trades-toggle-all">
              {showAll ? "SHOW LATEST 100 →" : `SHOW ALL ${rows.length} →`}
            </button>
          }
        >
          {rows.length === 0 ? (
            <div className="py-12 text-center font-mono text-xs text-muted-foreground">— No trades yet —</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full font-mono text-xs" data-testid="trades-journal-table">
                <thead className="text-muted-foreground text-[10px] tracking-widest">
                  <tr className="border-b border-border">
                    <th className="text-left py-2 px-2">OPENED</th>
                    <th>PAIR</th>
                    <th>SIDE</th>
                    <th>LOT</th>
                    <th>ENTRY</th>
                    <th>EXIT</th>
                    <th>iSL / iTP</th>
                    <th>SETUP</th>
                    <th>CONF</th>
                    <th>MFE / MAE</th>
                    <th>DURATION</th>
                    <th>EXIT</th>
                    <th className="text-right px-2">P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {display.map((t) => {
                    const d = dec(t.pair);
                    const pnl = t.pnl != null ? Number(t.pnl) : (t.live_pnl != null ? Number(t.live_pnl) : null);
                    const reason = t.signal_reason || "—";
                    const setup = reason.split("·")[0]?.trim().slice(0, 26) || "—";
                    const badge = reasonBadge(t);
                    return (
                      <tr key={t.id} className="border-t border-border hover:bg-surface-hover">
                        <td className="py-2 px-2 text-muted-foreground whitespace-nowrap">{new Date(t.opened_at).toLocaleString()}</td>
                        <td className="text-center">{t.pair}</td>
                        <td className={`text-center ${t.side === "buy" ? "text-bull" : "text-bear"}`}>{t.side?.toUpperCase()}</td>
                        <td className="text-center">{Number(t.lot).toFixed(2)}</td>
                        <td className="text-center">{Number(t.entry).toFixed(d)}</td>
                        <td className="text-center">{t.exit_price != null ? Number(t.exit_price).toFixed(d) : (t.status === "open" ? "open" : "—")}</td>
                        <td className="text-center text-muted-foreground text-[10px] whitespace-nowrap">
                          {(t.initial_sl ?? t.sl) != null ? Number(t.initial_sl ?? t.sl).toFixed(d) : "—"} / {(t.initial_tp ?? t.tp) != null ? Number(t.initial_tp ?? t.tp).toFixed(d) : "—"}
                        </td>
                        <td className="text-center text-[10px] text-muted-foreground" title={reason}>{setup}</td>
                        <td className="text-center text-primary">{t.confidence != null ? Number(t.confidence).toFixed(2) : "—"}</td>
                        <td className="text-center text-[10px] whitespace-nowrap">
                          <span className="text-bull">+${Number(t.mfe_pnl ?? 0).toFixed(2)}</span>
                          <span className="text-muted-foreground"> / </span>
                          <span className="text-bear">${Number(t.mae_pnl ?? 0).toFixed(2)}</span>
                        </td>
                        <td className="text-center text-[10px] text-muted-foreground">{t.status === "open" ? "open" : fmtDur(t.opened_at, t.closed_at)}</td>
                        <td className="text-center">
                          {badge ? <span className={`inline-block px-1.5 py-0.5 border rounded text-[10px] tracking-widest ${badge.cls}`}>{badge.label}</span> : (t.status === "open" ? <span className="text-muted-foreground text-[10px]">—</span> : <span className="text-muted-foreground text-[10px]">closed</span>)}
                        </td>
                        <td className={`text-right px-2 ${(pnl ?? 0) >= 0 ? "text-bull" : "text-bear"}`}>
                          {pnl == null ? "…" : `${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}`}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
    </AppShell>
  );
}
