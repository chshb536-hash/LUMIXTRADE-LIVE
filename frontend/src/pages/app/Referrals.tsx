import { useEffect, useState } from "react";
import { apiGet } from "@/api/client";
import { useAuth } from "@/contexts/AuthContext";
import { AppShell } from "@/components/AppShell";
import { Panel, Stat } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { Copy, Gift, Link2, Share2, Users } from "lucide-react";
import { toast } from "sonner";

type ReferralStats = {
  referral_code: string;
  commission_pct: number;
  total_referred: number;
  total_conversions: number;
  total_days_earned: number;
  total_referred_volume_usd: number;
  referees: Array<{ id: string; display_name?: string; email_masked?: string; joined_at?: string }>;
  events: Array<{ id: string; plan: string; plan_amount: number; commission_pct: number; days_credited: number; referee_id: string; created_at: string }>;
};

export default function Referrals() {
  const { user } = useAuth();
  const [data, setData] = useState<ReferralStats | null>(null);

  useEffect(() => {
    if (!user) return;
    apiGet<ReferralStats>("/referrals/me").then(setData).catch(() => {});
  }, [user]);

  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const link = data?.referral_code ? `${origin}/auth?ref=${data.referral_code}` : "";

  const copy = (text: string, what: string) => {
    navigator.clipboard.writeText(text);
    toast.success(`${what} copied`);
  };
  const share = async () => {
    if (!link) return;
    try {
      if ((navigator as any).share) {
        await (navigator as any).share({ title: "Join LumixTrade", text: "Smart auto-trading. Real risk control. Forex & gold on MT5.", url: link });
      } else {
        copy(link, "Link");
      }
    } catch {}
  };

  return (
    <AppShell>
      <div className="container py-6 space-y-4" data-testid="referrals-page">
        <div>
          <h1 className="font-display text-2xl font-bold">Referrals</h1>
          <p className="text-sm text-muted-foreground">
            Share your link. When someone signs up and their payment is approved, you earn{" "}
            <span className="font-mono text-primary">{data?.commission_pct ?? 10}%</span> of the plan period added to your own subscription.
          </p>
        </div>

        <div className="grid md:grid-cols-4 gap-4">
          <Panel><Stat label="Your Code" value={<span className="text-primary">{data?.referral_code ?? "—"}</span>} /></Panel>
          <Panel><Stat label="Signups" value={data?.total_referred ?? 0} /></Panel>
          <Panel><Stat label="Paid Conversions" value={data?.total_conversions ?? 0} /></Panel>
          <Panel><Stat label="Days Earned" value={`+${data?.total_days_earned ?? 0}d`} delta={{ value: `$${(data?.total_referred_volume_usd ?? 0).toFixed(2)} referred`, positive: true }} /></Panel>
        </div>

        <Panel title="// YOUR REFERRAL LINK">
          <div className="space-y-3">
            <div className="flex items-center gap-2 surface-elevated rounded p-3">
              <Link2 className="w-4 h-4 text-primary shrink-0" />
              <code className="font-mono text-xs flex-1 break-all" data-testid="referral-link">{link || "…"}</code>
              <Button size="sm" variant="outline" onClick={() => copy(link, "Link")} disabled={!link} className="font-mono text-[10px] tracking-widest" data-testid="referral-copy-link-btn">
                <Copy className="w-3.5 h-3.5 mr-1" /> COPY
              </Button>
              <Button size="sm" onClick={share} disabled={!link} className="font-mono text-[10px] tracking-widest" data-testid="referral-share-btn">
                <Share2 className="w-3.5 h-3.5 mr-1" /> SHARE
              </Button>
            </div>
            <div className="flex items-center gap-2 surface-elevated rounded p-3">
              <Gift className="w-4 h-4 text-primary shrink-0" />
              <code className="font-mono text-xs flex-1" data-testid="referral-code">{data?.referral_code ?? "…"}</code>
              <Button size="sm" variant="outline" onClick={() => copy(data?.referral_code ?? "", "Code")} disabled={!data?.referral_code} className="font-mono text-[10px] tracking-widest" data-testid="referral-copy-code-btn">
                <Copy className="w-3.5 h-3.5 mr-1" /> COPY CODE
              </Button>
            </div>
            <p className="text-[11px] text-muted-foreground font-mono tracking-wider">
              HOW IT WORKS — (1) Share this link, (2) friend signs up via this link, (3) friend pays and admin approves → you get{" "}
              <span className="text-primary">{data?.commission_pct ?? 10}%</span> of their plan period added to your subscription automatically.
            </p>
          </div>
        </Panel>

        <div className="grid lg:grid-cols-2 gap-4">
          <Panel title="YOUR REFEREES" subtitle={`${data?.referees.length ?? 0}`} actions={<Users className="w-3.5 h-3.5 text-primary" />}>
            {(!data || data.referees.length === 0) ? (
              <div className="py-8 text-center font-mono text-xs text-muted-foreground">— No referrals yet. Share your link! —</div>
            ) : (
              <table className="w-full font-mono text-xs">
                <thead className="text-muted-foreground text-[10px] tracking-widest">
                  <tr><th className="text-left py-2">NAME</th><th>EMAIL</th><th className="text-right">JOINED</th></tr>
                </thead>
                <tbody>
                  {data.referees.map((r) => (
                    <tr key={r.id} className="border-t border-border">
                      <td className="py-2">{r.display_name ?? "—"}</td>
                      <td className="text-center text-muted-foreground">{r.email_masked}</td>
                      <td className="text-right text-muted-foreground">{r.joined_at ? new Date(r.joined_at).toLocaleDateString() : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>

          <Panel title="EARNINGS LEDGER" subtitle={`${data?.events.length ?? 0}`}>
            {(!data || data.events.length === 0) ? (
              <div className="py-8 text-center font-mono text-xs text-muted-foreground">— No earnings yet —</div>
            ) : (
              <table className="w-full font-mono text-xs">
                <thead className="text-muted-foreground text-[10px] tracking-widest">
                  <tr><th className="text-left py-2">DATE</th><th>PLAN</th><th>AMOUNT</th><th className="text-right">CREDIT</th></tr>
                </thead>
                <tbody>
                  {data.events.map((e) => (
                    <tr key={e.id} className="border-t border-border">
                      <td className="py-2 text-muted-foreground">{new Date(e.created_at).toLocaleDateString()}</td>
                      <td className="text-center uppercase">{e.plan}</td>
                      <td className="text-center">${Number(e.plan_amount).toFixed(2)}</td>
                      <td className="text-right text-primary">+{e.days_credited}d @ {e.commission_pct}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
        </div>
      </div>
    </AppShell>
  );
}
