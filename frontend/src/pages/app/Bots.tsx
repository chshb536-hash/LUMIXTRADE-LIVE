import { useEffect, useState } from "react";
import { apiDelete, apiGet, apiPatch, apiPost, errMessage } from "@/api/client";
import { useAuth } from "@/contexts/AuthContext";
import { AppShell } from "@/components/AppShell";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { toast } from "sonner";
import { LineChart as ChartIcon, Plus, Power, Trash2, Zap } from "lucide-react";

const PAIRS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "XAGUSD"];
const TIMEFRAMES = ["M5", "M15", "M30", "H1", "H4"];
const HTF_OPTIONS = ["off", "H1", "H4"];

export default function Bots() {
  const { user } = useAuth();
  const [bots, setBots] = useState<any[]>([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [backtesting, setBacktesting] = useState<Record<string, boolean>>({});
  const [form, setForm] = useState({
    name: "GOLD-LON-NY",
    pair: "XAUUSD",
    timeframe: "M15",
    risk_per_trade: 1.0,
    max_positions: 2,
    daily_loss_limit: 5.0,
    higher_tf_confirmation: "off" as "off" | "H1" | "H4",
  });

  const load = async () => {
    try { setBots(await apiGet<any[]>("/bots")); } catch {}
  };
  useEffect(() => { if (user) load(); }, [user]);

  const create = async () => {
    setBusy(true);
    try {
      await apiPost("/bots", form);
      toast.success("Bot created");
      setOpen(false);
      load();
    } catch (e) { toast.error(errMessage(e)); }
    finally { setBusy(false); }
  };

  const toggle = async (b: any) => {
    try {
      await apiPatch(`/bots/${b.id}`, { is_active: !b.is_active });
      toast.success(b.is_active ? "Bot paused" : "Bot activated");
      load();
    } catch (e) { toast.error(errMessage(e)); }
  };

  const remove = async (id: string) => {
    if (!confirm("Delete this bot?")) return;
    try { await apiDelete(`/bots/${id}`); load(); } catch (e) { toast.error(errMessage(e)); }
  };

  const runScan = async (botId: string) => {
    toast.info("Running scan…");
    try {
      const data = await apiPost<any>(`/bots/${botId}/scan`);
      toast.success(data?.message ?? "Scan complete");
      load();
    } catch (e) { toast.error(errMessage(e)); }
  };

  const runBacktest = async (botId: string) => {
    setBacktesting((m) => ({ ...m, [botId]: true }));
    toast.info("Backtesting · 90 days · pulling Dukascopy data…");
    try {
      const data = await apiPost<any>(`/bots/${botId}/backtest`, { days: 90 });
      toast.success(`Done · ${data.total_trades} trades · WR ${data.win_rate_pct}% · PF ${data.profit_factor ?? "—"}`);
      load();
    } catch (e) {
      toast.error(errMessage(e));
    } finally {
      setBacktesting((m) => { const c = { ...m }; delete c[botId]; return c; });
    }
  };

  return (
    <AppShell>
      <div className="container py-6 space-y-4" data-testid="bots-page">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="font-display text-2xl font-bold">Trading Bots</h1>
            <p className="text-sm text-muted-foreground">Each bot trades one pair, one timeframe, one set of rules.</p>
          </div>
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button className="font-mono tracking-widest" data-testid="new-bot-btn"><Plus className="w-4 h-4 mr-1" /> NEW BOT</Button>
            </DialogTrigger>
            <DialogContent className="surface terminal-border">
              <DialogHeader><DialogTitle className="font-mono tracking-widest text-primary">// NEW BOT</DialogTitle></DialogHeader>
              <div className="space-y-3">
                <F label="NAME"><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="font-mono" data-testid="bot-name-input" /></F>
                <div className="grid grid-cols-2 gap-3">
                  <F label="PAIR">
                    <Select value={form.pair} onValueChange={(v) => setForm({ ...form, pair: v })}>
                      <SelectTrigger className="font-mono" data-testid="bot-pair-select"><SelectValue /></SelectTrigger>
                      <SelectContent>{PAIRS.map((p) => <SelectItem key={p} value={p} className="font-mono">{p}</SelectItem>)}</SelectContent>
                    </Select>
                  </F>
                  <F label="TIMEFRAME">
                    <Select value={form.timeframe} onValueChange={(v) => setForm({ ...form, timeframe: v })}>
                      <SelectTrigger className="font-mono" data-testid="bot-tf-select"><SelectValue /></SelectTrigger>
                      <SelectContent>{TIMEFRAMES.map((p) => <SelectItem key={p} value={p} className="font-mono">{p}</SelectItem>)}</SelectContent>
                    </Select>
                  </F>
                </div>
                <div className="grid grid-cols-3 gap-3">
                  <F label="RISK %"><Input type="number" step="0.1" value={form.risk_per_trade} onChange={(e) => setForm({ ...form, risk_per_trade: Number(e.target.value) })} className="font-mono" data-testid="bot-risk-input" /></F>
                  <F label="MAX POS"><Input type="number" value={form.max_positions} onChange={(e) => setForm({ ...form, max_positions: Number(e.target.value) })} className="font-mono" data-testid="bot-maxpos-input" /></F>
                  <F label="DAILY LOSS %"><Input type="number" step="0.1" value={form.daily_loss_limit} onChange={(e) => setForm({ ...form, daily_loss_limit: Number(e.target.value) })} className="font-mono" data-testid="bot-dll-input" /></F>
                </div>
                <F label="HIGHER-TF CONFIRMATION">
                  <Select value={form.higher_tf_confirmation} onValueChange={(v) => setForm({ ...form, higher_tf_confirmation: v as "off" | "H1" | "H4" })}>
                    <SelectTrigger className="font-mono" data-testid="bot-htf-select"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {HTF_OPTIONS.map((h) => <SelectItem key={h} value={h} className="font-mono">{h === "off" ? "OFF · accept any" : `${h} · trend must agree`}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </F>
                <Button onClick={create} disabled={busy} className="w-full font-mono tracking-widest" data-testid="bot-create-submit-btn">{busy ? "CREATING…" : "CREATE BOT"}</Button>
              </div>
            </DialogContent>
          </Dialog>
        </div>

        {bots.length === 0 ? (
          <Panel><div className="py-12 text-center font-mono text-xs text-muted-foreground tracking-wider">— No bots yet. Create one to start generating signals. —</div></Panel>
        ) : (
          <div className="grid md:grid-cols-2 gap-4">
            {bots.map((b) => (
              <Panel key={b.id} title={b.name} subtitle={`${b.pair} · ${b.timeframe}`}
                actions={
                  <div className="flex items-center gap-2">
                    {(() => {
                      const m = String(b.last_mode || "").toLowerCase();
                      const map: Record<string, { label: string; cls: string }> = {
                        swing:   { label: "SWING",    cls: "bg-bull/15 text-bull border-bull/40" },
                        scalp:   { label: "SCALP",    cls: "bg-primary/15 text-primary border-primary/40" },
                        standby: { label: "STAND-BY", cls: "bg-muted text-muted-foreground border-border" },
                      };
                      const hit = map[m];
                      if (!hit) return null;
                      return (
                        <span
                          className={`px-2 py-0.5 border rounded font-mono text-[10px] tracking-widest ${hit.cls}`}
                          title="Current mode based on regime + session"
                          data-testid={`bot-mode-badge-${b.id}`}
                        >
                          {hit.label}
                        </span>
                      );
                    })()}
                    <Switch checked={b.is_active} onCheckedChange={() => toggle(b)} data-testid={`bot-toggle-${b.id}`} />
                    <button onClick={() => remove(b.id)} className="text-muted-foreground hover:text-bear" data-testid={`bot-delete-${b.id}`}><Trash2 className="w-3.5 h-3.5" /></button>
                  </div>
                }>
                <div className="grid grid-cols-4 gap-3 font-mono text-xs">
                  <KV k="RISK" v={`${b.risk_per_trade}%`} />
                  <KV k="MAX POS" v={b.max_positions} />
                  <KV k="DAILY STOP" v={`${b.daily_loss_limit}%`} />
                  <KV k="HTF CONF" v={(b.higher_tf_confirmation && b.higher_tf_confirmation !== "off") ? b.higher_tf_confirmation : "off"} />
                </div>
                {b.last_scan_at && (
                  <div className="mt-3 surface-elevated rounded p-2 font-mono text-[10px] tracking-wider flex flex-col gap-1.5">
                    <div className="flex justify-between items-center">
                      <span className="text-muted-foreground">LAST SCAN · {new Date(b.last_scan_at).toLocaleTimeString()}</span>
                      <span className={
                        String(b.last_scan_result || "").startsWith("signal_created") ? "text-bull" :
                        String(b.last_scan_result || "").startsWith("error") ? "text-bear" :
                        "text-warning"
                      }>{b.last_scan_result || "—"}</span>
                    </div>
                    {(() => {
                      const r = String(b.last_scan_result || "");
                      const map: Record<string, { label: string; cls: string }> = {
                        daily_loss_blocked:   { label: "PAUSED · DAILY LOSS LIMIT",  cls: "bg-bear/15 text-bear border-bear/30" },
                        max_positions_reached:{ label: "PAUSED · MAX POSITIONS",      cls: "bg-warning/15 text-warning border-warning/30" },
                        correlation_block:    { label: "PAUSED · CORRELATION",       cls: "bg-warning/15 text-warning border-warning/30" },
                        halt:                 { label: "HALTED · WEEKLY DRAWDOWN",   cls: "bg-bear/15 text-bear border-bear/30" },
                        news_block:           { label: "PAUSED · NEWS WINDOW",       cls: "bg-primary/15 text-primary border-primary/30" },
                        session_filtered:     { label: "OUT OF SESSION",             cls: "bg-muted text-muted-foreground border-border" },
                        cooldown:             { label: "COOLDOWN",                   cls: "bg-muted text-muted-foreground border-border" },
                        insufficient_data:    { label: "WAITING FOR DATA",           cls: "bg-muted text-muted-foreground border-border" },
                      };
                      const key = r.split(":")[0];
                      const hit = map[key];
                      if (!hit) return null;
                      const detail = r.includes(":") ? r.slice(r.indexOf(":") + 1) : "";
                      return (
                        <div className={`inline-flex w-fit items-center gap-1.5 px-2 py-0.5 border rounded text-[10px] tracking-widest ${hit.cls}`} data-testid={`bot-pause-badge-${b.id}`}>
                          {hit.label}{detail ? <span className="opacity-70">· {detail}</span> : null}
                        </div>
                      );
                    })()}
                  </div>
                )}
                <div className="mt-3 flex gap-2">
                  <Button size="sm" variant="outline" className="font-mono text-[10px] tracking-widest flex-1" onClick={() => runScan(b.id)} data-testid={`bot-scan-${b.id}`}>
                    <Zap className="w-3 h-3 mr-1" /> RUN SCAN
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="font-mono text-[10px] tracking-widest flex-1"
                    onClick={() => runBacktest(b.id)}
                    disabled={!!backtesting[b.id]}
                    data-testid={`bot-backtest-${b.id}`}
                  >
                    <ChartIcon className="w-3 h-3 mr-1" /> {backtesting[b.id] ? "BACKTESTING…" : "BACKTEST · 90D"}
                  </Button>
                  <div className={`px-3 flex items-center font-mono text-[10px] tracking-widest ${b.is_active ? "text-bull" : "text-muted-foreground"}`}>
                    <Power className="w-3 h-3 mr-1" /> {b.is_active ? "LIVE" : "PAUSED"}
                  </div>
                </div>
                {b.last_backtest && (
                  <div className="mt-3 surface-elevated rounded p-3 font-mono text-[10px] tracking-wider" data-testid={`bot-backtest-result-${b.id}`}>
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-muted-foreground">BACKTEST · {b.last_backtest.days}D · {b.last_backtest.start} → {b.last_backtest.end}</span>
                      <span className={`px-1.5 py-0.5 border rounded ${Number(b.last_backtest.total_net_profit) >= 0 ? "bg-bull/15 text-bull border-bull/30" : "bg-bear/15 text-bear border-bear/30"}`}>
                        {Number(b.last_backtest.total_net_profit) >= 0 ? "+" : ""}${Number(b.last_backtest.total_net_profit).toFixed(2)}
                      </span>
                    </div>
                    <div className="grid grid-cols-4 gap-2 text-foreground">
                      <div><div className="text-muted-foreground">TRADES</div><div>{b.last_backtest.total_trades}</div></div>
                      <div><div className="text-muted-foreground">WIN RATE</div><div className={Number(b.last_backtest.win_rate_pct) >= 50 ? "text-bull" : "text-bear"}>{Number(b.last_backtest.win_rate_pct).toFixed(1)}%</div></div>
                      <div><div className="text-muted-foreground">PROFIT FACTOR</div><div className={Number(b.last_backtest.profit_factor) >= 1 ? "text-bull" : "text-bear"}>{b.last_backtest.profit_factor != null ? Number(b.last_backtest.profit_factor).toFixed(2) : "—"}</div></div>
                      <div><div className="text-muted-foreground">MAX DD</div><div className="text-bear">{Number(b.last_backtest.max_drawdown_pct).toFixed(1)}%</div></div>
                    </div>
                  </div>
                )}
              </Panel>
            ))}
          </div>
        )}
      </div>
    </AppShell>
  );
}

const F = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <div className="space-y-1.5">
    <Label className="font-mono text-[10px] tracking-widest text-muted-foreground">{label}</Label>
    {children}
  </div>
);
const KV = ({ k, v }: { k: string; v: any }) => (
  <div>
    <div className="text-[10px] text-muted-foreground tracking-widest">{k}</div>
    <div className="text-foreground">{v}</div>
  </div>
);
