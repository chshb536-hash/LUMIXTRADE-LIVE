import { useEffect, useRef, useState } from "react";
import { Navigate } from "react-router-dom";
import { api, apiGet, apiPost, apiDelete, apiPatch, errMessage } from "@/api/client";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { useAuth } from "@/contexts/AuthContext";
import { AppShell } from "@/components/AppShell";
import { Panel, Stat } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Switch } from "@/components/ui/switch";
import { toast } from "sonner";
import { BarChart3, Check, Eye, Gift, Plus, Search, ShieldCheck, Trash2, Upload, Users, X, Zap } from "lucide-react";

const PLANS = ["monthly", "quarterly", "yearly"] as const;

export default function Admin() {
  const isAdmin = useIsAdmin();
  const [tab, setTab] = useState("analytics");

  if (isAdmin === null) return null;
  if (!isAdmin) return <Navigate to="/app" replace />;

  return (
    <AppShell>
      <div className="container py-6 space-y-4" data-testid="admin-page">
        <div>
          <h1 className="font-display text-2xl font-bold">Admin Console</h1>
          <p className="text-sm text-muted-foreground">User management, payments, referrals, analytics.</p>
        </div>

        <Tabs value={tab} onValueChange={setTab}>
          <TabsList className="font-mono text-[11px] tracking-widest overflow-x-auto">
            <TabsTrigger value="analytics" data-testid="admin-tab-analytics"><BarChart3 className="w-3 h-3 mr-1" /> ANALYTICS</TabsTrigger>
            <TabsTrigger value="users" data-testid="admin-tab-users"><Users className="w-3 h-3 mr-1" /> USERS</TabsTrigger>
            <TabsTrigger value="payments" data-testid="admin-tab-payments"><ShieldCheck className="w-3 h-3 mr-1" /> PAYMENTS</TabsTrigger>
            <TabsTrigger value="referrals" data-testid="admin-tab-referrals"><Gift className="w-3 h-3 mr-1" /> REFERRALS</TabsTrigger>
            <TabsTrigger value="instructions" data-testid="admin-tab-instructions"><Zap className="w-3 h-3 mr-1" /> PAYMENT INFO</TabsTrigger>
          </TabsList>

          <TabsContent value="analytics" className="mt-4"><AnalyticsTab /></TabsContent>
          <TabsContent value="users" className="mt-4"><UsersTab /></TabsContent>
          <TabsContent value="payments" className="mt-4"><PaymentsTab /></TabsContent>
          <TabsContent value="referrals" className="mt-4"><ReferralsTab /></TabsContent>
          <TabsContent value="instructions" className="mt-4"><InstructionsTab /></TabsContent>
        </Tabs>
      </div>
    </AppShell>
  );
}

/* ------------------- ANALYTICS ------------------- */
function AnalyticsTab() {
  const [s, setS] = useState<any>(null);
  useEffect(() => { apiGet<any>("/admin/stats").then(setS).catch(() => {}); }, []);
  if (!s) return <Panel><div className="py-8 text-center font-mono text-xs text-muted-foreground">— Loading… —</div></Panel>;
  return (
    <div className="space-y-4">
      <div className="grid md:grid-cols-4 gap-4">
        <Panel><Stat label="Total Users" value={s.users.total} delta={{ value: `${s.users.admins} admins`, positive: true }} /></Panel>
        <Panel><Stat label="Active Subs" value={s.subscriptions.active} /></Panel>
        <Panel><Stat label="MRR" value={`$${s.subscriptions.mrr_usd.toFixed(2)}`} delta={{ value: "recurring", positive: true }} /></Panel>
        <Panel><Stat label="Pending Payments" value={s.payments.pending} delta={{ value: `${s.payments.approved} approved`, positive: s.payments.pending === 0 }} /></Panel>
      </div>
      <div className="grid md:grid-cols-4 gap-4">
        <Panel><Stat label="Approved Volume" value={`$${s.payments.approved_volume_usd.toFixed(2)}`} /></Panel>
        <Panel><Stat label="Referral Events" value={s.referrals.events} delta={{ value: `${s.referrals.total_days_credited}d credited`, positive: true }} /></Panel>
        <Panel><Stat label="Bots (Active/Total)" value={`${s.bots.active} / ${s.bots.total}`} /></Panel>
        <Panel><Stat label="Trades (Open/Closed)" value={`${s.trades.open} / ${s.trades.closed}`} /></Panel>
      </div>
      <Panel>
        <div className="font-mono text-[11px] tracking-widest text-primary">// SIGNAL ACTIVITY</div>
        <div className="mt-2 flex gap-6">
          <KV k="SIGNALS TODAY" v={s.signals.today} />
          <KV k="DISABLED USERS" v={s.users.disabled} />
          <KV k="REJECTED PAYMENTS" v={s.payments.rejected} />
        </div>
      </Panel>
    </div>
  );
}

/* ------------------- USERS ------------------- */
function UsersTab() {
  const { user: me } = useAuth();
  const [search, setSearch] = useState("");
  const [role, setRole] = useState<string>("all");
  const [rows, setRows] = useState<any[]>([]);

  const load = async () => {
    const params: any = {};
    if (search.trim()) params.search = search.trim();
    if (role !== "all") params.role = role;
    try { setRows(await apiGet<any[]>("/admin/users", { params })); } catch (e) { toast.error(errMessage(e)); }
  };
  useEffect(() => { load(); }, []);

  const promote = async (u: any) => {
    const newRole = u.role === "admin" ? "user" : "admin";
    try {
      await apiPatch(`/admin/users/${u.id}`, { role: newRole });
      toast.success(`${u.email} is now ${newRole.toUpperCase()}`);
      load();
    } catch (e) { toast.error(errMessage(e)); }
  };
  const toggleDisabled = async (u: any) => {
    try {
      await apiPatch(`/admin/users/${u.id}`, { disabled: !u.disabled });
      toast.success(u.disabled ? "Enabled" : "Disabled");
      load();
    } catch (e) { toast.error(errMessage(e)); }
  };
  const remove = async (u: any) => {
    if (!confirm(`Delete user ${u.email}? This removes their bots/bridge/MT5 but keeps audit history.`)) return;
    try { await apiDelete(`/admin/users/${u.id}`); toast.success("Deleted"); load(); } catch (e) { toast.error(errMessage(e)); }
  };

  return (
    <div className="space-y-4">
      <Panel>
        <div className="flex flex-col md:flex-row gap-3 md:items-end">
          <div className="flex-1 space-y-1.5">
            <Label className="font-mono text-[10px] tracking-widest text-muted-foreground">SEARCH (email / name / id / ref code)</Label>
            <div className="flex gap-2">
              <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="jane@…" className="font-mono" data-testid="admin-users-search-input"
                onKeyDown={(e) => e.key === "Enter" && load()} />
              <Button onClick={load} variant="outline" className="font-mono tracking-widest" data-testid="admin-users-search-btn"><Search className="w-3.5 h-3.5" /></Button>
            </div>
          </div>
          <div className="space-y-1.5 min-w-[180px]">
            <Label className="font-mono text-[10px] tracking-widest text-muted-foreground">ROLE</Label>
            <Select value={role} onValueChange={(v) => { setRole(v); setTimeout(load, 0); }}>
              <SelectTrigger className="font-mono" data-testid="admin-users-role-select"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All</SelectItem>
                <SelectItem value="user">User</SelectItem>
                <SelectItem value="admin">Admin</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
      </Panel>

      <Panel title={`USERS (${rows.length})`}>
        <div className="overflow-x-auto">
          <table className="w-full font-mono text-xs">
            <thead className="text-muted-foreground text-[10px] tracking-widest">
              <tr>
                <th className="text-left py-2 px-2">USER</th>
                <th>ROLE</th>
                <th>SUB</th>
                <th>PERIOD END</th>
                <th>REF CODE</th>
                <th>REF'd</th>
                <th>BOTS</th>
                <th>JOINED</th>
                <th className="text-right px-2">ACTIONS</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((u) => (
                <tr key={u.id} className={`border-t border-border ${u.disabled ? "opacity-50" : ""}`}>
                  <td className="py-2 px-2">
                    <div className="font-semibold">{u.display_name ?? "—"}</div>
                    <div className="text-[10px] text-muted-foreground">{u.email}</div>
                  </td>
                  <td className="text-center uppercase">
                    <span className={u.role === "admin" ? "text-primary" : ""}>{u.role}</span>
                  </td>
                  <td className={`text-center uppercase ${u.subscription?.status === "active" ? "text-bull" : "text-muted-foreground"}`}>
                    {u.subscription?.plan ?? "—"} / {u.subscription?.status ?? "none"}
                  </td>
                  <td className="text-center text-[10px] text-muted-foreground">
                    {u.subscription?.current_period_end ? new Date(u.subscription.current_period_end).toLocaleDateString() : "—"}
                  </td>
                  <td className="text-center text-primary">{u.referral_code ?? "—"}</td>
                  <td className="text-center">{u.referred_count}</td>
                  <td className="text-center">{u.bots_count}</td>
                  <td className="text-center text-[10px] text-muted-foreground">{u.created_at ? new Date(u.created_at).toLocaleDateString() : "—"}</td>
                  <td className="text-right px-2 whitespace-nowrap">
                    <GrantSubDialog user={u} onDone={load}><Button size="sm" variant="outline" className="font-mono text-[10px] tracking-widest mr-1" data-testid={`admin-user-grant-${u.id}`}>GRANT</Button></GrantSubDialog>
                    {u.id !== me?.id && (
                      <>
                        <Button size="sm" variant="outline" onClick={() => promote(u)} className="font-mono text-[10px] tracking-widest mr-1" data-testid={`admin-user-role-${u.id}`}>{u.role === "admin" ? "DEMOTE" : "PROMOTE"}</Button>
                        <Button size="sm" variant="outline" onClick={() => toggleDisabled(u)} className="font-mono text-[10px] tracking-widest mr-1" data-testid={`admin-user-disable-${u.id}`}>{u.disabled ? "ENABLE" : "DISABLE"}</Button>
                        <Button size="sm" variant="outline" onClick={() => remove(u)} className="font-mono text-[10px] tracking-widest text-bear border-bear/40" data-testid={`admin-user-delete-${u.id}`}><Trash2 className="w-3 h-3" /></Button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr><td colSpan={9} className="py-8 text-center font-mono text-xs text-muted-foreground">— No users —</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}

function GrantSubDialog({ user, onDone, children }: { user: any; onDone: () => void; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const [plan, setPlan] = useState("monthly");
  const [extend, setExtend] = useState(true);
  const [customDays, setCustomDays] = useState("");
  const [busy, setBusy] = useState(false);

  const grant = async () => {
    setBusy(true);
    try {
      await apiPost(`/admin/users/${user.id}/grant-subscription`, {
        plan,
        extend,
        days_override: customDays.trim() ? Number(customDays) : null,
      });
      toast.success("Subscription granted");
      setOpen(false); onDone();
    } catch (e) { toast.error(errMessage(e)); }
    finally { setBusy(false); }
  };

  const cancel = async () => {
    if (!confirm("Cancel this user's subscription?")) return;
    setBusy(true);
    try {
      await apiPost(`/admin/users/${user.id}/cancel-subscription`);
      toast.success("Canceled");
      setOpen(false); onDone();
    } catch (e) { toast.error(errMessage(e)); }
    finally { setBusy(false); }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{children}</DialogTrigger>
      <DialogContent className="surface terminal-border">
        <DialogHeader><DialogTitle className="font-mono tracking-widest text-primary">// SUBSCRIPTION — {user.email}</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <div className="surface-elevated rounded p-3 text-xs">
            <div className="font-mono text-[10px] tracking-widest text-muted-foreground">CURRENT</div>
            <div className="mt-1">
              {user.subscription ? (
                <>Plan: <span className="uppercase text-primary">{user.subscription.plan ?? "none"}</span> · Status: <span className="uppercase">{user.subscription.status}</span>
                {user.subscription.current_period_end && <> · Ends: {new Date(user.subscription.current_period_end).toLocaleDateString()}</>}</>
              ) : "No subscription"}
            </div>
          </div>
          <F label="PLAN">
            <Select value={plan} onValueChange={setPlan}>
              <SelectTrigger className="font-mono" data-testid="admin-grant-plan-select"><SelectValue /></SelectTrigger>
              <SelectContent>
                {PLANS.map((p) => <SelectItem key={p} value={p} className="font-mono">{p.toUpperCase()}</SelectItem>)}
              </SelectContent>
            </Select>
          </F>
          <F label="CUSTOM DAYS (OPTIONAL — overrides plan default)">
            <Input value={customDays} onChange={(e) => setCustomDays(e.target.value)} type="number" placeholder="e.g. 14" className="font-mono" data-testid="admin-grant-days-input" />
          </F>
          <div className="flex items-center gap-2">
            <Switch checked={extend} onCheckedChange={setExtend} data-testid="admin-grant-extend-switch" />
            <span className="font-mono text-[11px] tracking-widest text-muted-foreground">EXTEND CURRENT (off = start from today)</span>
          </div>
          <div className="flex gap-2">
            <Button onClick={grant} disabled={busy} className="flex-1 font-mono tracking-widest" data-testid="admin-grant-submit-btn">{busy ? "…" : "GRANT / EXTEND"}</Button>
            <Button onClick={cancel} disabled={busy} variant="outline" className="font-mono tracking-widest text-bear border-bear/40" data-testid="admin-grant-cancel-btn">CANCEL SUB</Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/* ------------------- PAYMENTS ------------------- */
function PaymentsTab() {
  const [subs, setSubs] = useState<any[]>([]);
  const load = async () => { try { setSubs(await apiGet<any[]>("/admin/payments")); } catch (e) { toast.error(errMessage(e)); } };
  useEffect(() => { load(); }, []);

  const viewScreenshot = async (id: string, has: boolean) => {
    if (!has) return toast.info("No screenshot");
    try {
      const res = await api.get(`/admin/payments/${id}/proof`, { responseType: "blob" });
      const url = URL.createObjectURL(res.data);
      window.open(url, "_blank");
    } catch { toast.error("Failed to load"); }
  };
  const approve = async (id: string) => {
    const notes = prompt("Optional approval note:") ?? null;
    try { await apiPost(`/admin/payments/${id}/approve`, { notes }); toast.success("Approved"); load(); } catch (e) { toast.error(errMessage(e)); }
  };
  const reject = async (id: string) => {
    const notes = prompt("Reason for rejection:") ?? null;
    if (!notes) return;
    try { await apiPost(`/admin/payments/${id}/reject`, { notes }); toast.success("Rejected"); load(); } catch (e) { toast.error(errMessage(e)); }
  };

  const pending = subs.filter((s) => s.status === "pending");
  const decided = subs.filter((s) => s.status !== "pending");

  return (
    <div className="space-y-4">
      <Panel title={`PENDING (${pending.length})`}>
        {pending.length === 0 ? (
          <div className="py-8 text-center font-mono text-xs text-muted-foreground">— No pending submissions —</div>
        ) : (
          <table className="w-full font-mono text-xs">
            <thead className="text-muted-foreground text-[10px] tracking-widest">
              <tr><th className="text-left py-2">SUBMITTED</th><th>USER</th><th>PLAN</th><th>AMOUNT</th><th>METHOD</th><th>TXN REF</th><th>PROOF</th><th className="text-right">ACTION</th></tr>
            </thead>
            <tbody>
              {pending.map((s) => (
                <tr key={s.id} className="border-t border-border align-top">
                  <td className="py-2 text-muted-foreground">{new Date(s.created_at).toLocaleString()}</td>
                  <td>{s.user_display_name ?? s.user_email ?? s.user_id?.slice(0, 8)}</td>
                  <td className="text-center uppercase">{s.plan}</td>
                  <td className="text-center">${Number(s.amount).toFixed(2)}</td>
                  <td className="text-center text-[10px]">{s.method}</td>
                  <td className="text-center text-muted-foreground break-all max-w-[200px]"><div className="line-clamp-2">{s.txn_reference}</div>{s.notes && <div className="text-[10px] mt-1 text-foreground/60">{s.notes}</div>}</td>
                  <td className="text-center">
                    <button onClick={() => viewScreenshot(s.id, !!s.has_screenshot)} className="text-primary hover:text-foreground" data-testid={`admin-view-proof-${s.id}`}><Eye className="w-4 h-4" /></button>
                  </td>
                  <td className="text-right whitespace-nowrap">
                    <Button size="sm" onClick={() => approve(s.id)} className="font-mono text-[10px] tracking-widest mr-1" data-testid={`admin-approve-${s.id}`}><Check className="w-3 h-3 mr-1" /> APPROVE</Button>
                    <Button size="sm" variant="outline" onClick={() => reject(s.id)} className="font-mono text-[10px] tracking-widest text-bear border-bear/40" data-testid={`admin-reject-${s.id}`}><X className="w-3 h-3 mr-1" /> REJECT</Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <Panel title={`HISTORY (${decided.length})`}>
        {decided.length === 0 ? (
          <div className="py-8 text-center font-mono text-xs text-muted-foreground">— No decided submissions yet —</div>
        ) : (
          <table className="w-full font-mono text-xs">
            <thead className="text-muted-foreground text-[10px] tracking-widest">
              <tr><th className="text-left py-2">DATE</th><th>USER</th><th>PLAN</th><th>AMOUNT</th><th>STATUS</th><th>REVIEWED</th><th className="text-right">PROOF</th></tr>
            </thead>
            <tbody>
              {decided.map((s) => (
                <tr key={s.id} className="border-t border-border">
                  <td className="py-2 text-muted-foreground">{new Date(s.created_at).toLocaleString()}</td>
                  <td>{s.user_display_name ?? s.user_email ?? s.user_id?.slice(0, 8)}</td>
                  <td className="text-center uppercase">{s.plan}</td>
                  <td className="text-center">${Number(s.amount).toFixed(2)}</td>
                  <td className={`text-center uppercase ${s.status === "approved" ? "text-bull" : "text-bear"}`}>{s.status}</td>
                  <td className="text-center text-[10px] text-muted-foreground">{s.reviewed_at ? new Date(s.reviewed_at).toLocaleString() : "—"}</td>
                  <td className="text-right">
                    <button onClick={() => viewScreenshot(s.id, !!s.has_screenshot)} className="text-primary hover:text-foreground"><Eye className="w-4 h-4 inline" /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}

/* ------------------- REFERRALS ------------------- */
function ReferralsTab() {
  const [rows, setRows] = useState<any[]>([]);
  useEffect(() => { apiGet<any[]>("/admin/referrals").then(setRows).catch(() => {}); }, []);
  return (
    <Panel title={`REFERRAL EVENTS (${rows.length})`}>
      {rows.length === 0 ? (
        <div className="py-8 text-center font-mono text-xs text-muted-foreground">— No referral events yet —</div>
      ) : (
        <table className="w-full font-mono text-xs">
          <thead className="text-muted-foreground text-[10px] tracking-widest">
            <tr><th className="text-left py-2">DATE</th><th>REFERRER</th><th>REFEREE</th><th>PLAN</th><th>AMOUNT</th><th>%</th><th className="text-right">DAYS</th></tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <tr key={e.id} className="border-t border-border">
                <td className="py-2 text-muted-foreground">{new Date(e.created_at).toLocaleString()}</td>
                <td>{e.referrer_name ?? e.referrer_email ?? e.referrer_id?.slice(0, 8)}</td>
                <td>{e.referee_name ?? e.referee_email ?? e.referee_id?.slice(0, 8)}</td>
                <td className="text-center uppercase">{e.plan}</td>
                <td className="text-center">${Number(e.plan_amount).toFixed(2)}</td>
                <td className="text-center text-primary">{e.commission_pct}%</td>
                <td className="text-right text-primary">+{e.days_credited}d</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

/* ------------------- INSTRUCTIONS ------------------- */
function InstructionsTab() {
  // Pricing + global notes from the legacy collection
  const [pricesForm, setPricesForm] = useState<any>({
    monthly_price: 49, quarterly_price: 129, yearly_price: 449,
    notes: "", referral_commission_pct: 10,
  });
  const [savingPrices, setSavingPrices] = useState(false);
  // Admin-managed payment methods list (new collection)
  const [methods, setMethods] = useState<any[]>([]);
  const [newMethod, setNewMethod] = useState<any>({
    name: "USDT TRC-20", type: "usdt_trc20", address: "", label: "", instructions: "", enabled: true, sort_order: 0,
  });

  const loadAll = async () => {
    try {
      const [d, list] = await Promise.all([
        apiGet<any>("/payment-instructions"),
        apiGet<any[]>("/admin/payment-methods"),
      ]);
      setPricesForm({
        monthly_price: Number(d.monthly_price),
        quarterly_price: Number(d.quarterly_price),
        yearly_price: Number(d.yearly_price),
        notes: d.notes ?? "",
        referral_commission_pct: Number(d.referral_commission_pct ?? 10),
      });
      setMethods(list ?? []);
    } catch (e) { toast.error(errMessage(e)); }
  };
  useEffect(() => { loadAll(); }, []);

  const savePrices = async () => {
    setSavingPrices(true);
    try {
      // Keep legacy fields untouched so an older client read still works.
      await api.put("/admin/payment-instructions", pricesForm);
      toast.success("Pricing saved");
      loadAll();
    } catch (e) { toast.error(errMessage(e)); }
    finally { setSavingPrices(false); }
  };

  const addMethod = async () => {
    if (!newMethod.name.trim()) return toast.error("Name is required");
    try {
      await api.post("/admin/payment-methods", newMethod);
      toast.success("Method added");
      setNewMethod({ ...newMethod, address: "", label: "", instructions: "" });
      loadAll();
    } catch (e) { toast.error(errMessage(e)); }
  };

  return (
    <div className="space-y-6">
      <Panel title="// PRICING & GLOBAL NOTES">
        <div className="space-y-3 max-w-2xl">
          <div className="grid grid-cols-4 gap-3">
            <Field label="MONTHLY $" type="number" value={pricesForm.monthly_price} onChange={(v) => setPricesForm({ ...pricesForm, monthly_price: Number(v) })} testid="admin-monthly-price" />
            <Field label="QUARTERLY $" type="number" value={pricesForm.quarterly_price} onChange={(v) => setPricesForm({ ...pricesForm, quarterly_price: Number(v) })} testid="admin-quarterly-price" />
            <Field label="YEARLY $" type="number" value={pricesForm.yearly_price} onChange={(v) => setPricesForm({ ...pricesForm, yearly_price: Number(v) })} testid="admin-yearly-price" />
            <Field label="REFERRAL %" type="number" value={pricesForm.referral_commission_pct} onChange={(v) => setPricesForm({ ...pricesForm, referral_commission_pct: Number(v) })} testid="admin-ref-pct" />
          </div>
          <F label="NOTES SHOWN TO USERS (e.g. timezone for confirmations, support email)">
            <Textarea rows={3} value={pricesForm.notes} onChange={(e) => setPricesForm({ ...pricesForm, notes: e.target.value })} className="font-mono text-xs" data-testid="admin-notes" />
          </F>
          <Button onClick={savePrices} disabled={savingPrices} className="font-mono tracking-widest" data-testid="admin-save-instructions-btn">
            {savingPrices ? "SAVING…" : "SAVE PRICING & NOTES"}
          </Button>
        </div>
      </Panel>

      <Panel title="// PAYMENT METHODS — VISIBLE TO USERS" subtitle={`${methods.length} configured`}>
        <div className="space-y-3">
          {methods.length === 0 ? (
            <p className="text-sm text-muted-foreground">No methods configured yet. Add your first one below.</p>
          ) : methods.map((m) => (
            <MethodRow key={m.id} method={m} onChange={loadAll} />
          ))}
        </div>
        <div className="border-t border-border mt-6 pt-4 space-y-3">
          <div className="font-mono text-[10px] tracking-widest text-primary">ADD NEW METHOD</div>
          <div className="grid md:grid-cols-2 gap-3">
            <Field label="DISPLAY NAME" value={newMethod.name} onChange={(v) => setNewMethod({ ...newMethod, name: v })} testid="admin-new-method-name" />
            <F label="TYPE">
              <Select value={newMethod.type} onValueChange={(v) => setNewMethod({ ...newMethod, type: v })}>
                <SelectTrigger className="font-mono" data-testid="admin-new-method-type"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {Object.keys(ADMIN_METHOD_LABELS).map((t) => <SelectItem key={t} value={t}>{ADMIN_METHOD_LABELS[t]}</SelectItem>)}
                </SelectContent>
              </Select>
            </F>
            <Field label="ADDRESS / IBAN / EMAIL / PHONE" value={newMethod.address} onChange={(v) => setNewMethod({ ...newMethod, address: v })} testid="admin-new-method-address" />
            <Field label="LABEL (account holder name etc — optional)" value={newMethod.label} onChange={(v) => setNewMethod({ ...newMethod, label: v })} testid="admin-new-method-label" />
          </div>
          <F label="INSTRUCTIONS (shown below the address)">
            <Textarea rows={2} value={newMethod.instructions} onChange={(e) => setNewMethod({ ...newMethod, instructions: e.target.value })} className="font-mono text-xs" data-testid="admin-new-method-instructions" />
          </F>
          <Button onClick={addMethod} className="font-mono tracking-widest" data-testid="admin-add-method-btn"><Plus className="w-3.5 h-3.5 mr-1" /> ADD METHOD</Button>
        </div>
      </Panel>
    </div>
  );
}

const ADMIN_METHOD_LABELS: Record<string, string> = {
  usdt_trc20: "USDT · TRC-20",
  usdt_bep20: "USDT · BEP-20",
  usdt_erc20: "USDT · ERC-20",
  btc:        "Bitcoin",
  eth:        "Ethereum",
  bank:       "Bank Transfer",
  jazzcash:   "JazzCash",
  easypaisa:  "Easypaisa",
  paypal:     "PayPal",
  other:      "Other",
};

function MethodRow({ method, onChange }: { method: any; onChange: () => void }) {
  const [edit, setEdit] = useState({
    name: method.name, type: method.type, address: method.address ?? "", label: method.label ?? "",
    instructions: method.instructions ?? "", enabled: !!method.enabled, sort_order: Number(method.sort_order ?? 0),
  });
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const apiBase = process.env.REACT_APP_BACKEND_URL || "";
  const qrSrc = method.qr_url ? `${apiBase}${method.qr_url}` : null;

  const save = async () => {
    setBusy(true);
    try { await api.patch(`/admin/payment-methods/${method.id}`, edit); toast.success("Saved"); onChange(); }
    catch (e) { toast.error(errMessage(e)); }
    finally { setBusy(false); }
  };
  const remove = async () => {
    if (!window.confirm(`Delete payment method "${method.name}"? This also removes its QR.`)) return;
    try { await api.delete(`/admin/payment-methods/${method.id}`); toast.success("Deleted"); onChange(); }
    catch (e) { toast.error(errMessage(e)); }
  };
  const uploadQr = async (f: File) => {
    if (!f.type.startsWith("image/")) return toast.error("QR must be an image");
    if (f.size > 3 * 1024 * 1024) return toast.error("QR must be under 3MB");
    const fd = new FormData(); fd.append("qr", f);
    try { await api.post(`/admin/payment-methods/${method.id}/qr`, fd, { headers: { "Content-Type": "multipart/form-data" } }); toast.success("QR uploaded"); onChange(); }
    catch (e) { toast.error(errMessage(e)); }
  };
  const deleteQr = async () => {
    try { await api.delete(`/admin/payment-methods/${method.id}/qr`); toast.success("QR removed"); onChange(); }
    catch (e) { toast.error(errMessage(e)); }
  };

  return (
    <div className="surface-elevated rounded p-3 grid md:grid-cols-[120px_1fr] gap-4" data-testid={`admin-method-row-${method.id}`}>
      <div className="space-y-2">
        {qrSrc ? (
          <img src={qrSrc} alt="QR" className="w-28 h-28 rounded border border-border bg-white p-1 object-contain" />
        ) : (
          <div className="w-28 h-28 rounded border border-dashed border-border flex items-center justify-center font-mono text-[10px] text-muted-foreground tracking-widest">NO QR</div>
        )}
        <div className="flex gap-2">
          <Button size="sm" variant="outline" className="font-mono text-[10px] flex-1" onClick={() => fileRef.current?.click()} data-testid={`admin-method-qr-upload-${method.id}`}>
            <Upload className="w-3 h-3 mr-1" /> {qrSrc ? "REPLACE" : "UPLOAD"}
          </Button>
          {qrSrc && (
            <Button size="sm" variant="outline" className="font-mono text-[10px]" onClick={deleteQr} data-testid={`admin-method-qr-delete-${method.id}`}>
              <Trash2 className="w-3 h-3" />
            </Button>
          )}
          <input ref={fileRef} type="file" accept="image/*" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadQr(f); e.target.value = ""; }} />
        </div>
      </div>
      <div className="space-y-2">
        <div className="grid md:grid-cols-3 gap-2">
          <Field label="NAME" value={edit.name} onChange={(v) => setEdit({ ...edit, name: v })} testid={`admin-method-name-${method.id}`} />
          <F label="TYPE">
            <Select value={edit.type} onValueChange={(v) => setEdit({ ...edit, type: v })}>
              <SelectTrigger className="font-mono"><SelectValue /></SelectTrigger>
              <SelectContent>
                {Object.keys(ADMIN_METHOD_LABELS).map((t) => <SelectItem key={t} value={t}>{ADMIN_METHOD_LABELS[t]}</SelectItem>)}
              </SelectContent>
            </Select>
          </F>
          <Field label="SORT" type="number" value={edit.sort_order} onChange={(v) => setEdit({ ...edit, sort_order: Number(v) })} testid={`admin-method-sort-${method.id}`} />
        </div>
        <div className="grid md:grid-cols-2 gap-2">
          <Field label="ADDRESS" value={edit.address} onChange={(v) => setEdit({ ...edit, address: v })} testid={`admin-method-address-${method.id}`} />
          <Field label="LABEL" value={edit.label} onChange={(v) => setEdit({ ...edit, label: v })} testid={`admin-method-label-${method.id}`} />
        </div>
        <F label="INSTRUCTIONS">
          <Textarea rows={2} value={edit.instructions} onChange={(e) => setEdit({ ...edit, instructions: e.target.value })} className="font-mono text-xs" />
        </F>
        <div className="flex items-center justify-between">
          <label className="font-mono text-[11px] tracking-widest flex items-center gap-2 cursor-pointer">
            <Switch checked={edit.enabled} onCheckedChange={(v) => setEdit({ ...edit, enabled: v })} />
            <span className={edit.enabled ? "text-bull" : "text-muted-foreground"}>{edit.enabled ? "ENABLED" : "DISABLED"}</span>
          </label>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" className="font-mono text-[10px] tracking-widest text-bear" onClick={remove} data-testid={`admin-method-delete-${method.id}`}>
              <Trash2 className="w-3 h-3 mr-1" /> DELETE
            </Button>
            <Button size="sm" className="font-mono text-[10px] tracking-widest" onClick={save} disabled={busy} data-testid={`admin-method-save-${method.id}`}>
              {busy ? "SAVING…" : "SAVE CHANGES"}
            </Button>
          </div>
        </div>
      </div>
    </div>
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
    <div className="text-[10px] text-muted-foreground tracking-widest font-mono">{k}</div>
    <div className="font-mono text-lg text-foreground">{v}</div>
  </div>
);
const Field = ({ label, value, onChange, type = "text", testid }: { label: string; value: any; onChange: (v: string) => void; type?: string; testid?: string }) => (
  <div className="space-y-1.5">
    <Label className="font-mono text-[10px] tracking-widest text-muted-foreground">{label}</Label>
    <Input type={type} value={value} onChange={(e) => onChange(e.target.value)} className="font-mono" data-testid={testid} />
  </div>
);
