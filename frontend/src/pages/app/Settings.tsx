import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiGet, apiPatch, apiPost, api, errMessage } from "@/api/client";
import { useAuth } from "@/contexts/AuthContext";
import { AppShell } from "@/components/AppShell";
import { Panel } from "@/components/Panel";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { toast } from "sonner";
import { Copy, KeyRound, Trash2 } from "lucide-react";
import { z } from "zod";

const pwSchema = z.string().min(8, "Min 8 chars").max(72);

export default function Settings() {
  const { user, refresh, signOut } = useAuth();
  const navigate = useNavigate();
  const [displayName, setDisplayName] = useState("");
  const [savingProfile, setSavingProfile] = useState(false);

  useEffect(() => {
    if (!user) return;
    apiGet<any>("/profile").then((p) => setDisplayName(p?.display_name ?? "")).catch(() => {});
  }, [user]);

  const saveProfile = async () => {
    setSavingProfile(true);
    try {
      await apiPatch("/profile", { display_name: displayName });
      await refresh();
      toast.success("Profile saved");
    } catch (e) { toast.error(errMessage(e)); }
    finally { setSavingProfile(false); }
  };

  return (
    <AppShell>
      <div className="container py-6 space-y-4 max-w-2xl" data-testid="settings-page">
        <Panel title="ACCOUNT">
          <div className="space-y-3">
            <div>
              <Label className="font-mono text-[10px] tracking-widest text-muted-foreground">EMAIL</Label>
              <div className="font-mono text-sm mt-1">{user?.email}</div>
            </div>
            <div>
              <Label className="font-mono text-[10px] tracking-widest text-muted-foreground">ROLE</Label>
              <div className="font-mono text-sm mt-1 uppercase">{user?.role}</div>
            </div>
            <div>
              <Label className="font-mono text-[10px] tracking-widest text-muted-foreground">DISPLAY NAME</Label>
              <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} className="font-mono mt-1" data-testid="settings-displayname-input" />
            </div>
            <Button onClick={saveProfile} disabled={savingProfile} className="font-mono tracking-widest" data-testid="settings-save-btn">
              {savingProfile ? "SAVING…" : "SAVE"}
            </Button>
          </div>
        </Panel>

        <Panel title="YOUR REFERRAL CODE">
          <div className="flex items-center gap-2 surface-elevated rounded p-3">
            <code className="font-mono text-sm text-primary flex-1">{user?.referral_code ?? "—"}</code>
            <Button size="sm" variant="outline" onClick={() => { navigator.clipboard.writeText(user?.referral_code ?? ""); toast.success("Copied"); }}
              className="font-mono text-[10px] tracking-widest" data-testid="settings-copy-ref-btn">
              <Copy className="w-3.5 h-3.5 mr-1" /> COPY
            </Button>
          </div>
          <p className="text-[10px] text-muted-foreground font-mono tracking-wider mt-2">
            Go to REFERRALS to see your link and earnings.
          </p>
        </Panel>

        <Panel title="SECURITY">
          <div className="space-y-3">
            <ChangePasswordDialog />
          </div>
        </Panel>

        <Panel title="// DANGER ZONE" className="border-bear/40">
          <div className="space-y-3">
            <p className="text-xs text-muted-foreground">Deleting your account will remove your bots, bridge keys, MT5 links and your subscription. Payment and trade history are kept for audit.</p>
            <DeleteAccountDialog onDone={async () => { await signOut(); navigate("/"); }} />
          </div>
        </Panel>
      </div>
    </AppShell>
  );
}

function ChangePasswordDialog() {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [cur, setCur] = useState("");
  const [nw, setNw] = useState("");
  const [cf, setCf] = useState("");

  const submit = async () => {
    try { pwSchema.parse(nw); } catch (e: any) { return toast.error(e.issues?.[0]?.message ?? "Invalid password"); }
    if (nw !== cf) return toast.error("New passwords don't match");
    setBusy(true);
    try {
      await apiPost("/auth/change-password", { current_password: cur, new_password: nw });
      toast.success("Password updated");
      setOpen(false); setCur(""); setNw(""); setCf("");
    } catch (e) { toast.error(errMessage(e)); }
    finally { setBusy(false); }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" className="font-mono tracking-widest" data-testid="settings-change-pw-btn">
          <KeyRound className="w-3.5 h-3.5 mr-1" /> CHANGE PASSWORD
        </Button>
      </DialogTrigger>
      <DialogContent className="surface terminal-border">
        <DialogHeader><DialogTitle className="font-mono tracking-widest text-primary">// CHANGE PASSWORD</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <F label="CURRENT PASSWORD"><Input type="password" value={cur} onChange={(e) => setCur(e.target.value)} className="font-mono" data-testid="settings-cur-pw" /></F>
          <F label="NEW PASSWORD (min 8)"><Input type="password" value={nw} onChange={(e) => setNw(e.target.value)} className="font-mono" data-testid="settings-new-pw" /></F>
          <F label="CONFIRM NEW PASSWORD"><Input type="password" value={cf} onChange={(e) => setCf(e.target.value)} className="font-mono" data-testid="settings-cf-pw" /></F>
          <Button onClick={submit} disabled={busy} className="w-full font-mono tracking-widest" data-testid="settings-submit-pw-btn">
            {busy ? "UPDATING…" : "UPDATE PASSWORD"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function DeleteAccountDialog({ onDone }: { onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState("");

  const submit = async () => {
    if (confirm !== "DELETE") return toast.error('Type DELETE to confirm');
    setBusy(true);
    try {
      await api.delete("/auth/delete-account");
      toast.success("Account deleted");
      onDone();
    } catch (e) { toast.error(errMessage(e)); }
    finally { setBusy(false); }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" className="font-mono tracking-widest text-bear border-bear/40 hover:bg-bear/10" data-testid="settings-delete-btn">
          <Trash2 className="w-3.5 h-3.5 mr-1" /> DELETE ACCOUNT
        </Button>
      </DialogTrigger>
      <DialogContent className="surface terminal-border">
        <DialogHeader><DialogTitle className="font-mono tracking-widest text-bear">// DELETE ACCOUNT</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <p className="text-xs text-muted-foreground">Type <span className="font-mono text-bear">DELETE</span> to confirm. This cannot be undone.</p>
          <Input value={confirm} onChange={(e) => setConfirm(e.target.value)} className="font-mono" placeholder="DELETE" data-testid="settings-delete-confirm-input" />
          <Button onClick={submit} disabled={busy || confirm !== "DELETE"} className="w-full font-mono tracking-widest bg-bear text-white hover:bg-bear/80" data-testid="settings-submit-delete-btn">
            {busy ? "DELETING…" : "PERMANENTLY DELETE MY ACCOUNT"}
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
