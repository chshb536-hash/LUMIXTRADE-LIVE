import { useEffect, useState } from "react";
import { apiGet } from "@/api/client";
import { useAuth } from "@/contexts/AuthContext";
import { AppShell } from "@/components/AppShell";
import { Panel, Stat } from "@/components/Panel";
import { TickerTape } from "@/components/TickerTape";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { ArrowRight } from "lucide-react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ReferenceLine, ResponsiveContainer } from "recharts";

// Pair-aware decimals helper
const decimals = (pair: string) => (pair?.startsWith("XAU") ? 2 : pair?.startsWith("XAG") ? 4 : pair?.endsWith("JPY") ? 3 : 5);

type SignalRow = { id: string; pair: string; side: string; entry: number; sl: number; tp: number; confidence: number; status: string; created_at: string };
type TradeRow = { id: string; pair: string; side: string; lot: number; entry: number; pnl: number | null; live_pnl?: number | null; live_at?: string | null; status: string; opened_at: string };
type SubRow = { plan: string | null; status: string; current_period_end: string | null };
type AcctRow = { id: string; login: string; server: string; balance: number; equity: number; is_connected: boolean; last_heartbeat_at: string | null };
type BotRow = { id: string; name: string; pair: string; is_active: boolean; last_scan_at?: string | null };
type KeyRow = { id: string; api_key: string; revoked: boolean; last_seen_at: string | null };
type RiskRow = { week_high: number; current_equity: number; drawdown_pct: number; health_pct: number; halted: boolean; halt_reason: string | null; halt_threshold: number };
type EquityPoint = { date: string; pnl: number; cumulative: number; trades: number };
type EquityResp = { points: EquityPoint[]; total_pnl: number; best_day: { date: string; pnl: number } | null; worst_day: { date: string; pnl: number } | null };

export default function Dashboard() {
  const { user } = useAuth();
  const [signals, setSignals] = useState<SignalRow[]>([]);
  const [trades, setTrades] = useState<TradeRow[]>([]);
  const [accts, setAccts] = useState<AcctRow[]>([]);
  const [sub, setSub] = useState<SubRow | null>(null);
  const [bots, setBots] = useState<BotRow[]>([]);
  const [keys, setKeys] = useState<KeyRow[]>([]);
  const [risk, setRisk] = useState<RiskRow | null>(null);
  const [equity, setEquity] = useState<EquityResp | null>(null);

  useEffect(() => {
    if (!user) return;
    let mounted = true;
    const load = async () => {
      try {
        const [s, t, a, sb, b, k, r, eq] = await Promise.all([
          apiGet<SignalRow[]>("/signals", { params: { limit: 8 } }),
          apiGet<TradeRow[]>("/trades", { params: { limit: 8 } }),
          apiGet<AcctRow[]>("/mt5-accounts"),
          apiGet<SubRow | null>("/subscriptions/me"),
          apiGet<BotRow[]>("/bots"),
          apiGet<KeyRow[]>("/bridge/keys"),
          apiGet<RiskRow>("/risk/me"),
          apiGet<EquityResp>("/equity-curve", { params: { days: 30 } }),
        ]);
        if (!mounted) return;
        setSignals(s ?? []);
        setTrades(t ?? []);
        setAccts(a ?? []);
        setSub(sb ?? null);
        setBots(b ?? []);
        setKeys(k ?? []);
        setRisk(r ?? null);
        setEquity(eq ?? null);
      } catch {
        // ignore — handled at global level
      }
    };
    load();
    const iv = setInterval(load, 8000);
    return () => { mounted = false; clearInterval(iv); };
  }, [user]);

  const openTrades = trades.filter((t) => t.status === "open");
  const closedToday = trades.filter((t) => t.status === "closed" && new Date(t.opened_at).toDateString() === new Date().toDateString());
  const pnlClosed = closedToday.reduce((a, t) => a + (Number(t.pnl) || 0), 0);
  const pnlFloating = openTrades.reduce((a, t) => a + (Number(t.live_pnl ?? 0) || 0), 0);
  const pnlToday = pnlClosed + pnlFloating;
  const totalEquity = accts.reduce((a, x) => a + Number(x.equity || 0), 0);

  // Bridge health: any active key seen in the last 90 seconds counts as live
  const cutoff = Date.now() - 90_000;
  const liveKeys = keys.filter((k) => !k.revoked && k.last_seen_at && new Date(k.last_seen_at).getTime() >= cutoff);
  const anyKey = keys.some((k) => !k.revoked);
  const activeBots = bots.filter((b) => b.is_active);
  const subActive = sub?.status === "active" || sub?.status === "trialing";
  const showBridgeWarning = !!user && subActive && activeBots.length > 0 && liveKeys.length === 0;

  return (
    <AppShell>
      <TickerTape />
      <div className="container py-6 space-y-6" data-testid="dashboard-root">
        <div className="grid md:grid-cols-4 gap-4">
          <Panel><Stat label="Total Equity" value={`$${totalEquity.toFixed(2)}`} /></Panel>
          <Panel><Stat label="Open Trades" value={openTrades.length} /></Panel>
          <Panel><Stat label="P&L Today" value={`${pnlToday >= 0 ? "+" : ""}$${pnlToday.toFixed(2)}`} delta={{ value: `${closedToday.length} closed`, positive: pnlToday >= 0 }} /></Panel>
          <Panel>
            <div className="flex flex-col">
              <span className="font-mono text-[10px] tracking-widest text-muted-foreground uppercase">Subscription</span>
              <span className="font-mono text-2xl font-semibold uppercase" data-testid="dashboard-sub-plan">{sub?.plan ?? "NONE"}</span>
              <span className={`font-mono text-xs ${sub?.status === "active" || sub?.status === "trialing" ? "text-bull" : "text-warning"}`}>
                {sub?.status?.toUpperCase() ?? "INACTIVE"}
              </span>
            </div>
          </Panel>
        </div>

        {risk && (risk.week_high > 0 || risk.halted) && (
          <Panel
            title="WEEK HEALTH"
            subtitle={`baseline reset · Monday UTC`}
            actions={
              <span
                className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border rounded ${
                  risk.halted
                    ? "bg-bear/15 text-bear border-bear/30"
                    : risk.drawdown_pct >= risk.halt_threshold * 0.66
                    ? "bg-warning/15 text-warning border-warning/30"
                    : "bg-bull/15 text-bull border-bull/30"
                }`}
                data-testid="dashboard-week-health-status"
              >
                {risk.halted ? "● HALTED" : "● ACTIVE"}
              </span>
            }
          >
            <div className="grid md:grid-cols-4 gap-3 font-mono">
              <Stat
                label="Health"
                value={`${risk.health_pct.toFixed(1)}%`}
                delta={{
                  value: risk.halted ? "trading halted" : `safe ≥ ${(100 - risk.halt_threshold).toFixed(0)}%`,
                  positive: !risk.halted,
                }}
              />
              <Stat label="Drawdown" value={`${risk.drawdown_pct.toFixed(2)}%`} />
              <Stat label="Week High" value={`$${risk.week_high.toFixed(2)}`} />
              <Stat label="Current Equity" value={`$${risk.current_equity.toFixed(2)}`} />
            </div>
            <div className="mt-3 h-1.5 surface-elevated rounded overflow-hidden">
              <div
                className={`h-full transition-all ${
                  risk.halted
                    ? "bg-bear"
                    : risk.drawdown_pct >= risk.halt_threshold * 0.66
                    ? "bg-warning"
                    : "bg-bull"
                }`}
                style={{ width: `${Math.max(0, Math.min(100, 100 - (risk.drawdown_pct / risk.halt_threshold) * 100))}%` }}
              />
            </div>
            <div className="mt-2 font-mono text-[10px] tracking-widest text-muted-foreground">
              halt threshold · −{risk.halt_threshold.toFixed(0)}% from week-high · auto-resumes Monday 00:00 UTC
              {risk.halted && risk.halt_reason ? <span className="text-bear"> · {risk.halt_reason}</span> : null}
            </div>
          </Panel>
        )}

        {equity && equity.points.length > 0 && (
          <Panel
            title="EQUITY CURVE"
            subtitle={`cumulative realized P&L · last ${equity.points.length} day(s)`}
            actions={
              <span className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border rounded ${equity.total_pnl >= 0 ? "bg-bull/15 text-bull border-bull/30" : "bg-bear/15 text-bear border-bear/30"}`} data-testid="dashboard-equity-total">
                {equity.total_pnl >= 0 ? "+" : ""}${equity.total_pnl.toFixed(2)}
              </span>
            }
          >
            <div className="h-56" data-testid="dashboard-equity-chart">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={equity.points} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                  <XAxis
                    dataKey="date"
                    tick={{ fontFamily: "monospace", fontSize: 10, fill: "#888" }}
                    stroke="#444"
                    tickFormatter={(v: string) => v.slice(5)}
                    interval="preserveStartEnd"
                  />
                  <YAxis
                    tick={{ fontFamily: "monospace", fontSize: 10, fill: "#888" }}
                    stroke="#444"
                    width={60}
                    tickFormatter={(v: number) => `$${v.toFixed(0)}`}
                  />
                  <ReferenceLine y={0} stroke="#666" strokeDasharray="3 3" />
                  <Tooltip
                    contentStyle={{ background: "#111", border: "1px solid #333", fontFamily: "monospace", fontSize: 11 }}
                    labelStyle={{ color: "#aaa" }}
                    formatter={(value: any, key: string) => {
                      if (key === "cumulative") return [`$${Number(value).toFixed(2)}`, "Cumulative"];
                      if (key === "pnl") return [`${Number(value) >= 0 ? "+" : ""}$${Number(value).toFixed(2)}`, "Day P&L"];
                      return [value, key];
                    }}
                  />
                  <Line
                    type="monotone"
                    dataKey="cumulative"
                    stroke={equity.total_pnl >= 0 ? "#22c55e" : "#ef4444"}
                    strokeWidth={2}
                    dot={false}
                    activeDot={{ r: 4 }}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <div className="mt-2 grid grid-cols-3 gap-3 font-mono text-[10px] tracking-widest">
              <div><span className="text-muted-foreground">BEST DAY · </span><span className="text-bull">{equity.best_day ? `+$${equity.best_day.pnl.toFixed(2)} · ${equity.best_day.date.slice(5)}` : "—"}</span></div>
              <div><span className="text-muted-foreground">WORST DAY · </span><span className="text-bear">{equity.worst_day ? `$${equity.worst_day.pnl.toFixed(2)} · ${equity.worst_day.date.slice(5)}` : "—"}</span></div>
              <div className="text-right"><span className="text-muted-foreground">TRADES · </span><span className="text-foreground">{equity.points.reduce((a, p) => a + p.trades, 0)}</span></div>
            </div>
          </Panel>
        )}

        {!sub || (sub.status !== "active" && sub.status !== "trialing") ? (
          <div className="surface terminal-border rounded p-4 flex flex-col md:flex-row md:items-center justify-between gap-3">
            <div>
              <div className="font-mono text-[11px] tracking-widest text-warning">// SUBSCRIPTION REQUIRED</div>
              <p className="text-sm text-muted-foreground mt-1">Pick a plan to activate signal generation and the MT5 bridge.</p>
            </div>
            <Button asChild className="font-mono tracking-widest" data-testid="dashboard-view-plans-btn"><Link to="/app/billing">VIEW PLANS <ArrowRight className="w-4 h-4 ml-2" /></Link></Button>
          </div>
        ) : null}

        {showBridgeWarning && (
          <div className="surface terminal-border rounded p-4 flex flex-col md:flex-row md:items-center justify-between gap-3 border-warning/40" data-testid="dashboard-bridge-warning">
            <div>
              <div className="font-mono text-[11px] tracking-widest text-warning">// BRIDGE NOT CONNECTED</div>
              <p className="text-sm text-muted-foreground mt-1">
                You have <span className="font-mono text-primary">{activeBots.length}</span> live bot(s) but no MT5 bridge is reporting.
                {anyKey
                  ? " The bridge running on your machine hasn't checked in within the last 90 seconds — check it's still running."
                  : " Generate a bridge API key, download aurum_bridge.py, and run it on a Windows machine with MT5."}
                {" "}Signals will queue but won't execute on MT5 until the bridge is live.
              </p>
            </div>
            <Button asChild variant="outline" className="font-mono tracking-widest border-warning/40 text-warning hover:bg-warning/10" data-testid="dashboard-bridge-fix-btn">
              <Link to="/app/bridge">SETUP BRIDGE <ArrowRight className="w-4 h-4 ml-2" /></Link>
            </Button>
          </div>
        )}

        <div className="grid lg:grid-cols-2 gap-4">
          <Panel title="LIVE SIGNALS" subtitle={`last ${signals.length}`} actions={<Link to="/app/signals" className="font-mono text-[10px] tracking-widest text-muted-foreground hover:text-primary" data-testid="dashboard-view-all-signals-link">VIEW ALL →</Link>}>
            {signals.length === 0 ? (
              <Empty msg="Waiting for next setup. Engine scans every 5 minutes when London/NY/overlap session is active." />
            ) : (
              <table className="w-full font-mono text-xs">
                <thead className="text-muted-foreground text-[10px] tracking-widest">
                  <tr><th className="text-left py-1">PAIR</th><th>SIDE</th><th>ENTRY</th><th>SL/TP</th><th>CONF</th><th className="text-right">STATUS</th></tr>
                </thead>
                <tbody>
                  {signals.map((s) => (
                    <tr key={s.id} className="border-t border-border">
                      <td className="py-2">{s.pair}</td>
                      <td className={`text-center ${s.side === "buy" ? "text-bull" : "text-bear"}`}>{s.side.toUpperCase()}</td>
                      <td className="text-center">{Number(s.entry).toFixed(decimals(s.pair))}</td>
                      <td className="text-center text-[10px] text-muted-foreground">{Number(s.sl).toFixed(decimals(s.pair))} / {Number(s.tp).toFixed(decimals(s.pair))}</td>
                      <td className="text-center text-primary">{Number(s.confidence).toFixed(2)}</td>
                      <td className="text-right uppercase text-[10px]">{s.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>

          <Panel title="OPEN POSITIONS" subtitle={`${openTrades.length}`} actions={<Link to="/app/trades" className="font-mono text-[10px] tracking-widest text-muted-foreground hover:text-primary" data-testid="dashboard-view-all-trades-link">VIEW ALL →</Link>}>
            {openTrades.length === 0 ? (
              <Empty msg="No open positions." />
            ) : (
              <table className="w-full font-mono text-xs">
                <thead className="text-muted-foreground text-[10px] tracking-widest">
                  <tr><th className="text-left py-1">PAIR</th><th>SIDE</th><th>LOT</th><th>ENTRY</th><th className="text-right">P&L</th></tr>
                </thead>
                <tbody>
                  {openTrades.map((t) => {
                    const pnl = t.pnl != null ? Number(t.pnl) : (t.live_pnl != null ? Number(t.live_pnl) : null);
                    return (
                    <tr key={t.id} className="border-t border-border">
                      <td className="py-2">{t.pair}</td>
                      <td className={`text-center ${t.side === "buy" ? "text-bull" : "text-bear"}`}>{t.side.toUpperCase()}</td>
                      <td className="text-center">{Number(t.lot).toFixed(2)}</td>
                      <td className="text-center">{Number(t.entry).toFixed(decimals(t.pair))}</td>
                      <td className={`text-right ${(pnl ?? 0) >= 0 ? "text-bull" : "text-bear"}`}>
                        {pnl == null ? "…" : `${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}`}
                      </td>
                    </tr>
                  );})}
                </tbody>
              </table>
            )}
          </Panel>
        </div>

        <Panel title="MT5 ACCOUNTS" actions={<Link to="/app/bridge" className="font-mono text-[10px] tracking-widest text-muted-foreground hover:text-primary" data-testid="dashboard-connect-bridge-link">CONNECT BRIDGE →</Link>}>
          {accts.length === 0 ? (
            <Empty msg="No MT5 account linked yet. Set up the bridge to connect." />
          ) : (
            <table className="w-full font-mono text-xs">
              <thead className="text-muted-foreground text-[10px] tracking-widest">
                <tr><th className="text-left py-1">LOGIN</th><th>SERVER</th><th>BALANCE</th><th>EQUITY</th><th className="text-right">STATUS</th></tr>
              </thead>
              <tbody>
                {accts.map((a) => (
                  <tr key={a.id} className="border-t border-border">
                    <td className="py-2">#{a.login}</td>
                    <td className="text-center">{a.server}</td>
                    <td className="text-center">${Number(a.balance).toFixed(2)}</td>
                    <td className="text-center">${Number(a.equity).toFixed(2)}</td>
                    <td className={`text-right ${a.is_connected ? "text-bull" : "text-muted-foreground"}`}>
                      {a.is_connected ? "● CONNECTED" : "○ OFFLINE"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>
    </AppShell>
  );
}

const Empty = ({ msg }: { msg: string }) => (
  <div className="py-8 text-center font-mono text-xs text-muted-foreground tracking-wider">— {msg} —</div>
);
