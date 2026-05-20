import { useEffect, useState } from "react";
import { apiGet } from "@/api/client";
import { useAuth } from "@/contexts/AuthContext";
import { AppShell } from "@/components/AppShell";
import { Panel, Stat } from "@/components/Panel";

type Item = {
  id: string;
  kind: "payment" | "referral";
  plan?: string | null;
  amount: number;
  currency: string;
  method?: string;
  txn_reference?: string;
  status: string;
  created_at: string;
  reviewed_at?: string | null;
  review_notes?: string | null;
  days_credited?: number;
  commission_pct?: number;
  referee_display_name?: string | null;
  referee_email_masked?: string | null;
};

export default function Transactions() {
  const { user } = useAuth();
  const [rows, setRows] = useState<Item[]>([]);
  const [filter, setFilter] = useState<"all" | "payment" | "referral">("all");

  useEffect(() => {
    if (!user) return;
    apiGet<Item[]>("/transactions/me").then(setRows).catch(() => {});
  }, [user]);

  const filtered = rows.filter((r) => filter === "all" || r.kind === filter);
  const paymentCount = rows.filter((r) => r.kind === "payment").length;
  const refCount = rows.filter((r) => r.kind === "referral").length;
  const approvedPaid = rows
    .filter((r) => r.kind === "payment" && r.status === "approved")
    .reduce((a, r) => a + r.amount, 0);
  const daysEarned = rows
    .filter((r) => r.kind === "referral")
    .reduce((a, r) => a + (r.days_credited || 0), 0);

  return (
    <AppShell>
      <div className="container py-6 space-y-4" data-testid="transactions-page">
        <div>
          <h1 className="font-display text-2xl font-bold">Transactions</h1>
          <p className="text-sm text-muted-foreground">Payments and referral credits.</p>
        </div>

        <div className="grid md:grid-cols-4 gap-4">
          <Panel><Stat label="Payments" value={paymentCount} /></Panel>
          <Panel><Stat label="Approved Paid" value={`$${approvedPaid.toFixed(2)}`} /></Panel>
          <Panel><Stat label="Referral Credits" value={refCount} /></Panel>
          <Panel><Stat label="Days Earned" value={`+${daysEarned}d`} delta={{ value: "auto-credited", positive: true }} /></Panel>
        </div>

        <div className="flex gap-2 font-mono text-[11px] tracking-widest">
          {(["all", "payment", "referral"] as const).map((f) => (
            <button key={f} onClick={() => setFilter(f)}
              data-testid={`txn-filter-${f}`}
              className={`px-3 py-1.5 terminal-border rounded transition-colors ${filter === f ? "bg-primary text-primary-foreground border-primary" : "text-muted-foreground hover:text-primary"}`}>
              {f === "all" ? "ALL" : f === "payment" ? "PAYMENTS" : "REFERRALS"}
            </button>
          ))}
        </div>

        <Panel title="HISTORY" subtitle={`${filtered.length}`}>
          {filtered.length === 0 ? (
            <div className="py-10 text-center font-mono text-xs text-muted-foreground">— No transactions yet —</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full font-mono text-xs">
                <thead className="text-muted-foreground text-[10px] tracking-widest">
                  <tr>
                    <th className="text-left py-2 px-2">DATE</th>
                    <th>TYPE</th>
                    <th>PLAN</th>
                    <th>AMOUNT</th>
                    <th>METHOD / DETAILS</th>
                    <th>REF</th>
                    <th className="text-right px-2">STATUS</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((r) => (
                    <tr key={r.id} className="border-t border-border hover:bg-surface-hover">
                      <td className="py-2 px-2 text-muted-foreground">{new Date(r.created_at).toLocaleString()}</td>
                      <td className={`text-center uppercase ${r.kind === "referral" ? "text-primary" : "text-foreground"}`}>
                        {r.kind === "referral" ? "REFERRAL" : "PAYMENT"}
                      </td>
                      <td className="text-center uppercase">{r.plan ?? "—"}</td>
                      <td className="text-center">
                        {r.kind === "payment" ? `$${r.amount.toFixed(2)}` : `+${r.days_credited ?? 0} days`}
                      </td>
                      <td className="text-center text-[10px] text-muted-foreground">
                        {r.kind === "payment" ? r.method?.replace(/_/g, " ") :
                          `from ${r.referee_display_name || r.referee_email_masked || "—"} @ ${r.commission_pct}%`}
                      </td>
                      <td className="text-center text-muted-foreground truncate max-w-[160px]">{r.txn_reference ?? "—"}</td>
                      <td className={`text-right px-2 uppercase ${
                        r.status === "approved" || r.status === "credited" ? "text-bull" :
                        r.status === "rejected" ? "text-bear" : "text-warning"
                      }`}>{r.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
    </AppShell>
  );
}
