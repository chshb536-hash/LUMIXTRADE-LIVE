import { useEffect, useState } from "react";
import { api, apiGet, apiPost, errMessage } from "@/api/client";
import { useAuth } from "@/contexts/AuthContext";
import { AppShell } from "@/components/AppShell";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Check, Copy, Upload, Zap } from "lucide-react";
import { toast } from "sonner";
import { z } from "zod";

const PLAN_META: Record<string, { label: string; per: string; days: number; save?: string; best?: boolean }> = {
  monthly:   { label: "MONTHLY",   per: "/ month",     days: 30 },
  quarterly: { label: "QUARTERLY", per: "/ 3 months",  days: 90, save: "SAVE 12%", best: true },
  yearly:    { label: "YEARLY",    per: "/ year",      days: 365, save: "SAVE 24%" },
};

const submitSchema = z.object({
  txn_reference: z.string().trim().min(4, "TXN reference is required").max(200),
  notes: z.string().trim().max(1000).optional().or(z.literal("")),
});

export default function Billing() {
  const { user } = useAuth();
  const [sub, setSub] = useState<any>(null);
  const [instr, setInstr] = useState<any>(null);
  const [methods, setMethods] = useState<any[]>([]);
  const [submissions, setSubmissions] = useState<any[]>([]);

  const load = async () => {
    if (!user) return;
    try {
      const [s, i, pm, ps] = await Promise.all([
        apiGet<any>("/subscriptions/me"),
        apiGet<any>("/payment-instructions"),
        apiGet<any[]>("/payment-methods"),
        apiGet<any[]>("/payments/submissions"),
      ]);
      setSub(s);
      setInstr(i);
      setMethods(pm ?? []);
      setSubmissions(ps ?? []);
    } catch { /* ignore */ }
  };
  useEffect(() => { load(); }, [user]);

  const prices = instr ? {
    monthly: Number(instr.monthly_price),
    quarterly: Number(instr.quarterly_price),
    yearly: Number(instr.yearly_price),
  } : { monthly: 49, quarterly: 129, yearly: 449 };

  const activeStatus = sub?.status === "active" || sub?.status === "trialing";
  const pendingForPlan = (plan: string) => submissions.find((x) => x.plan === plan && x.status === "pending");

  return (
    <AppShell>
      <div className="container py-6 space-y-6" data-testid="billing-page">
        <div>
          <h1 className="font-display text-2xl font-bold">Billing & Plans</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Plan: <span className="font-mono text-primary">{sub?.plan?.toUpperCase() ?? "NONE"}</span>
            {" · "}
            Status: <span className={`font-mono ${activeStatus ? "text-bull" : "text-warning"}`}>{sub?.status?.toUpperCase() ?? "INACTIVE"}</span>
            {sub?.current_period_end && activeStatus && (
              <> · Renews <span className="font-mono">{new Date(sub.current_period_end).toLocaleDateString()}</span></>
            )}
          </p>
        </div>

        {/* Plans */}
        <div className="grid md:grid-cols-3 gap-4">
          {(Object.keys(PLAN_META) as Array<keyof typeof PLAN_META>).map((p) => {
            const meta = PLAN_META[p];
            const pending = pendingForPlan(p);
            const isCurrent = sub?.plan === p && activeStatus;
            return (
              <Panel key={p} className={meta.best ? "border-primary/60 glow-amber" : ""}>
                {meta.save && <div className="font-mono text-[10px] tracking-widest text-warning">{meta.save}</div>}
                <div className="font-mono text-[11px] tracking-widest text-primary mt-1">{meta.label}</div>
                <div className="mt-2 flex items-baseline gap-1">
                  <span className="font-display text-4xl font-bold">${prices[p]}</span>
                  <span className="font-mono text-xs text-muted-foreground">{meta.per}</span>
                </div>
                <ul className="mt-4 space-y-1.5 text-sm">
                  {["Unlimited bots","All FX + XAUUSD","Session + regime engine","MT5 bridge auto-execute","Live dashboard"].map((f) => (
                    <li key={f} className="flex gap-2 items-start"><Check className="w-3.5 h-3.5 text-bull mt-0.5 shrink-0" />{f}</li>
                  ))}
                </ul>
                <div className="mt-5">
                  {isCurrent ? (
                    <Button disabled className="w-full font-mono tracking-widest">CURRENT PLAN</Button>
                  ) : pending ? (
                    <Button disabled variant="outline" className="w-full font-mono tracking-widest">REVIEW PENDING…</Button>
                  ) : (
                    <SubmitPaymentDialog plan={p} amount={prices[p]} methods={methods} onDone={load}>
                      <Button className="w-full font-mono tracking-widest" variant={meta.best ? "default" : "outline"} data-testid={`billing-subscribe-${p}-btn`}>
                        SUBSCRIBE <Zap className="w-3.5 h-3.5 ml-1" />
                      </Button>
                    </SubmitPaymentDialog>
                  )}
                </div>
              </Panel>
            );
          })}
        </div>

        {/* Payment methods grid (admin-managed) */}
        {(methods.length > 0 || instr) && (
          <Panel title="// HOW TO PAY" subtitle={methods.length ? `${methods.length} method(s) available` : undefined}>
            {methods.length === 0 ? (
              <p className="text-muted-foreground text-sm">Payment methods haven't been published yet. Contact support.</p>
            ) : (
              <div className="grid md:grid-cols-2 gap-4">
                {methods.map((m) => <PaymentMethodCard key={m.id} method={m} />)}
              </div>
            )}
            {instr?.notes && (
              <div className="mt-4 text-muted-foreground text-xs whitespace-pre-wrap" data-testid="billing-instr-notes">{instr.notes}</div>
            )}
          </Panel>
        )}

        {/* My submissions */}
        <Panel title="MY PAYMENT SUBMISSIONS" subtitle={`${submissions.length}`}>
          {submissions.length === 0 ? (
            <div className="py-6 text-center font-mono text-xs text-muted-foreground">— No submissions yet —</div>
          ) : (
            <table className="w-full font-mono text-xs">
              <thead className="text-muted-foreground text-[10px] tracking-widest">
                <tr><th className="text-left py-2">DATE</th><th>PLAN</th><th>AMOUNT</th><th>METHOD</th><th>TXN REF</th><th className="text-right">STATUS</th></tr>
              </thead>
              <tbody>
                {submissions.map((s) => (
                  <tr key={s.id} className="border-t border-border">
                    <td className="py-2 text-muted-foreground">{new Date(s.created_at).toLocaleString()}</td>
                    <td className="text-center uppercase">{s.plan}</td>
                    <td className="text-center">${Number(s.amount).toFixed(2)} {s.currency}</td>
                    <td className="text-center text-[10px]">{s.method}</td>
                    <td className="text-center text-muted-foreground truncate max-w-[160px]">{s.txn_reference}</td>
                    <td className={`text-right uppercase ${
                      s.status === "approved" ? "text-bull" :
                      s.status === "rejected" ? "text-bear" : "text-warning"
                    }`}>{s.status}</td>
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

const METHOD_LABELS: Record<string, string> = {
  usdt_trc20: "USDT · TRC-20",
  usdt_bep20: "USDT · BEP-20",
  usdt_erc20: "USDT · ERC-20",
  btc: "Bitcoin",
  eth: "Ethereum",
  bank: "Bank Transfer",
  jazzcash: "JazzCash",
  easypaisa: "Easypaisa",
  paypal: "PayPal",
  other: "Other",
};

const PaymentMethodCard = ({ method }: { method: any }) => {
  // Resolve the QR image to an absolute URL so <img> uses the backend, not the SPA route.
  const apiBase = process.env.REACT_APP_BACKEND_URL || "";
  const qrSrc = method.qr_url ? `${apiBase}${method.qr_url}` : null;
  const typeLabel = METHOD_LABELS[method.type] ?? method.type?.toUpperCase();
  return (
    <div className="surface-elevated rounded p-3 space-y-2" data-testid={`pay-method-${method.id}`}>
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-[10px] tracking-widest text-primary">{typeLabel}</div>
          <div className="font-mono text-sm font-semibold mt-0.5">{method.name}</div>
        </div>
      </div>
      {qrSrc && (
        <div className="flex justify-center pt-1">
          <img
            src={qrSrc}
            alt={`${method.name} QR`}
            className="w-32 h-32 rounded border border-border bg-white p-1 object-contain"
            data-testid={`pay-method-qr-${method.id}`}
          />
        </div>
      )}
      {method.address && (
        <div>
          <div className="font-mono text-[9px] tracking-widest text-muted-foreground">
            {method.label ? `${method.label} · ` : ""}ADDRESS
          </div>
          <div className="flex items-center gap-2 mt-1">
            <code className="font-mono text-xs flex-1 break-all surface rounded p-2 border border-border">{method.address}</code>
            <button
              onClick={() => { navigator.clipboard.writeText(method.address); toast.success("Address copied"); }}
              className="text-primary hover:text-foreground shrink-0"
              data-testid={`pay-method-copy-${method.id}`}
              aria-label="Copy address"
            >
              <Copy className="w-4 h-4" />
            </button>
          </div>
        </div>
      )}
      {method.instructions && (
        <pre className="font-mono text-[10px] whitespace-pre-wrap text-muted-foreground border-l-2 border-primary/40 pl-2">{method.instructions}</pre>
      )}
    </div>
  );
};

function SubmitPaymentDialog({
  plan, amount, methods, children, onDone,
}: { plan: string; amount: number; methods: any[]; children: React.ReactNode; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  // Default to the first enabled admin-configured method (preserves "other" fallback if list is empty)
  const fallback = methods?.[0]?.type ? methods[0].type : "other";
  const [method, setMethod] = useState<string>(fallback);
  const [txn, setTxn] = useState("");
  const [notes, setNotes] = useState("");
  const [file, setFile] = useState<File | null>(null);
  // Keep `method` valid when methods load after the dialog mounts
  useEffect(() => {
    if (!methods?.length) return;
    const exists = methods.some((m) => m.type === method);
    if (!exists) setMethod(methods[0].type);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [methods.length]);

  const submit = async () => {
    try { submitSchema.parse({ txn_reference: txn, notes }); }
    catch (e: any) { return toast.error(e.issues?.[0]?.message ?? "Invalid input"); }
    if (!file) return toast.error("Please attach a payment screenshot");
    if (file.size > 5 * 1024 * 1024) return toast.error("Screenshot must be under 5MB");
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("plan", plan);
      fd.append("amount", String(amount));
      fd.append("currency", "USD");
      fd.append("method", method);
      fd.append("txn_reference", txn.trim());
      if (notes.trim()) fd.append("notes", notes.trim());
      fd.append("screenshot", file);
      await api.post("/payments/submit", fd, { headers: { "Content-Type": "multipart/form-data" } });
      toast.success("Submitted. Admin will review shortly.");
      setOpen(false); setTxn(""); setNotes(""); setFile(null);
      onDone();
    } catch (e: any) {
      toast.error(errMessage(e));
    } finally { setBusy(false); }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{children}</DialogTrigger>
      <DialogContent className="surface terminal-border max-w-lg">
        <DialogHeader>
          <DialogTitle className="font-mono tracking-widest text-primary">// SUBMIT PAYMENT — {plan.toUpperCase()} · ${amount}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="surface-elevated rounded p-3 text-xs space-y-1">
            <div className="font-mono text-[10px] tracking-widest text-warning">STEP 1 — SEND PAYMENT</div>
            <div>Send <span className="font-mono text-primary">${amount} USD</span> using one of the addresses on the Billing page, then come back with your transaction reference and a screenshot.</div>
          </div>
          <F label="PAYMENT METHOD">
            <Select value={method} onValueChange={setMethod}>
              <SelectTrigger className="font-mono" data-testid="billing-method-select"><SelectValue /></SelectTrigger>
              <SelectContent>
                {methods?.length ? methods.map((m) => (
                  <SelectItem key={m.id} value={m.type}>{METHOD_LABELS[m.type] ?? m.type} — {m.name}</SelectItem>
                )) : (
                  <SelectItem value="other">Other</SelectItem>
                )}
              </SelectContent>
            </Select>
          </F>
          <F label="TRANSACTION REFERENCE / TXID">
            <Input value={txn} onChange={(e) => setTxn(e.target.value)} placeholder="e.g. 0x9f… or bank wire reference" className="font-mono" data-testid="billing-txn-ref-input" />
          </F>
          <F label="SCREENSHOT (PNG/JPG · max 5MB)">
            <label className="flex items-center gap-3 surface-elevated terminal-border rounded px-3 py-2 cursor-pointer hover:bg-surface-hover">
              <Upload className="w-4 h-4 text-primary" />
              <span className="font-mono text-xs flex-1 truncate">{file ? file.name : "Choose image…"}</span>
              <input type="file" accept="image/*" className="hidden" onChange={(e) => setFile(e.target.files?.[0] ?? null)} data-testid="billing-screenshot-input" />
            </label>
          </F>
          <F label="NOTES (OPTIONAL)">
            <Textarea value={notes} onChange={(e) => setNotes(e.target.value)} className="font-mono text-xs" rows={3} data-testid="billing-notes-input" />
          </F>
          <Button onClick={submit} disabled={busy} className="w-full font-mono tracking-widest" data-testid="billing-submit-payment-btn">
            {busy ? "SUBMITTING…" : "SUBMIT FOR REVIEW"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

const F = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <div className="space-y-1.5">
    <Label className="font-mono text-[10px] tracking-widest text-muted-foreground">{label}</Label>
    {children}
  </div>
);
