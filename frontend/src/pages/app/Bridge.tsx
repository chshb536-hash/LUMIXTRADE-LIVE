import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, apiGet, apiPost, errMessage } from "@/api/client";
import { useAuth } from "@/contexts/AuthContext";
import { AppShell } from "@/components/AppShell";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { toast } from "sonner";
import { Copy, Download, Key, Lock, RefreshCw, Trash2 } from "lucide-react";

// Resolve the bridge API base URL dynamically from the browser's current origin.
// This makes the instructions match whichever domain the user is visiting
// (lumixtrade.live in prod, forex-gold-bot-14.preview... in preview, localhost in dev)
// instead of leaking a build-time env var that may be stale after a domain change.
const BACKEND_URL = (typeof window !== "undefined" && window.location?.origin)
  ? window.location.origin
  : (process.env.REACT_APP_BACKEND_URL || "");

export default function Bridge() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [keys, setKeys] = useState<any[]>([]);
  const [accts, setAccts] = useState<any[]>([]);
  const [sub, setSub] = useState<any>(null);
  const [label, setLabel] = useState("My MT5");

  const isSubActive =
    !!sub &&
    (sub.status === "active" || sub.status === "trialing") &&
    (!sub.current_period_end || new Date(sub.current_period_end).getTime() >= Date.now());

  const load = async () => {
    try {
      const [k, a, s] = await Promise.all([
        apiGet<any[]>("/bridge/keys"),
        apiGet<any[]>("/mt5-accounts"),
        apiGet<any>("/subscriptions/me"),
      ]);
      setKeys(k ?? []);
      setAccts(a ?? []);
      setSub(s ?? null);
    } catch {}
  };
  useEffect(() => { if (user) load(); }, [user]);

  const generateKey = async () => {
    try {
      await apiPost("/bridge/keys", { label });
      toast.success("Bridge key created. Copy it now — you'll see it again here too.");
      load();
    } catch (e) { toast.error(errMessage(e)); }
  };
  const revoke = async (id: string) => {
    if (!confirm("Revoke this key? Bridge using it will stop working.")) return;
    try { await apiPost(`/bridge/keys/${id}/revoke`); load(); } catch (e) { toast.error(errMessage(e)); }
  };
  const copy = (s: string) => { navigator.clipboard.writeText(s); toast.success("Copied"); };

  const downloadBridge = async () => {
    // Client-side gate first (instant feedback)
    if (!isSubActive) {
      toast.error("Active subscription required to download the bridge.");
      navigate("/app/billing");
      return;
    }
    // Server-authoritative download via authenticated fetch (so cookies are sent + 402 gates it)
    try {
      const res = await api.get("/bridge/download", { responseType: "blob" });
      const url = URL.createObjectURL(res.data);
      const link = document.createElement("a");
      link.href = url;
      link.download = "aurum_bridge.py";
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e: any) {
      if (e?.response?.status === 402) {
        toast.error("Active subscription required to download the bridge.");
        navigate("/app/billing");
        return;
      }
      toast.error(errMessage(e));
    }
  };

  const apiBase = `${BACKEND_URL}/api`;

  return (
    <AppShell>
      <div className="container py-6 space-y-4" data-testid="bridge-page">
        {!isSubActive && (
          <div className="surface terminal-border rounded p-4 flex flex-col md:flex-row md:items-center justify-between gap-3" data-testid="bridge-sub-gate-banner">
            <div className="flex gap-3 items-start">
              <Lock className="w-5 h-5 text-warning shrink-0 mt-0.5" />
              <div>
                <div className="font-mono text-[11px] tracking-widest text-warning">// SUBSCRIPTION REQUIRED</div>
                <p className="text-sm text-muted-foreground mt-1">The MT5 bridge file is part of your subscription. Activate a plan to download it and start auto-trading on MT5.</p>
              </div>
            </div>
            <Button onClick={() => navigate("/app/billing")} className="font-mono tracking-widest" data-testid="bridge-activate-sub-btn">
              VIEW PLANS →
            </Button>
          </div>
        )}

        <Panel title="MT5 BRIDGE — STEP BY STEP">
          <ol className="space-y-3 text-sm">
            <Step n={1} title="Download the bridge script">
              <Button onClick={downloadBridge}
                className={`font-mono tracking-widest mt-2 ${!isSubActive ? "opacity-60" : ""}`}
                data-testid="bridge-download-btn">
                {isSubActive ? <><Download className="w-4 h-4 mr-2" /> aurum_bridge.py</> : <><Lock className="w-4 h-4 mr-2" /> SUBSCRIBE TO DOWNLOAD</>}
              </Button>
              <p className="text-muted-foreground text-xs mt-2">Runs on Windows with MT5 installed (or any Windows VPS where you'd normally run an EA).</p>
            </Step>
            <Step n={2} title="Install dependencies (one time)">
              <pre className="bg-input rounded p-3 font-mono text-xs mt-2 overflow-x-auto">pip install MetaTrader5 requests</pre>
            </Step>
            <Step n={3} title="Generate a bridge API key">
              <div className="flex gap-2 mt-2">
                <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Label" className="font-mono max-w-xs" data-testid="bridge-key-label-input" />
                <Button onClick={generateKey} className="font-mono tracking-widest" data-testid="bridge-generate-key-btn"><Key className="w-4 h-4 mr-1" /> GENERATE</Button>
              </div>
            </Step>
            <Step n={4} title="Run the bridge">
              <pre className="bg-input rounded p-3 font-mono text-xs mt-2 overflow-x-auto" data-testid="bridge-run-cmd">{`set AURUM_API_KEY=<paste your key>
set AURUM_API_URL=${apiBase}
set MT5_LOGIN=<your account number>
set MT5_PASSWORD=<your account password>
set MT5_SERVER=<your broker server>
python aurum_bridge.py`}</pre>
              <p className="text-muted-foreground text-xs mt-2">Your MT5 password is read by the local bridge only — it never touches our servers. The URL above already matches the site you're on right now ({apiBase}).</p>
            </Step>
          </ol>
        </Panel>

        <Panel title="ACTIVE BRIDGE KEYS">
          {keys.length === 0 ? (
            <div className="py-8 text-center font-mono text-xs text-muted-foreground">— No keys generated yet —</div>
          ) : (
            <table className="w-full font-mono text-xs">
              <thead className="text-muted-foreground text-[10px] tracking-widest">
                <tr><th className="text-left py-2">LABEL</th><th>KEY</th><th>LAST SEEN</th><th>STATUS</th><th className="text-right">ACTION</th></tr>
              </thead>
              <tbody>
                {keys.map((k) => (
                  <tr key={k.id} className="border-t border-border">
                    <td className="py-2">{k.label}</td>
                    <td className="text-muted-foreground">
                      <span className="select-all">{k.api_key.slice(0, 12)}…{k.api_key.slice(-6)}</span>
                      <button onClick={() => copy(k.api_key)} className="ml-2 text-primary hover:text-foreground" data-testid={`bridge-copy-${k.id}`}><Copy className="inline w-3 h-3" /></button>
                    </td>
                    <td className="text-center">{k.last_seen_at ? new Date(k.last_seen_at).toLocaleString() : "—"}</td>
                    <td className={`text-center ${k.revoked ? "text-bear" : "text-bull"}`}>{k.revoked ? "REVOKED" : "ACTIVE"}</td>
                    <td className="text-right">
                      {!k.revoked && <button onClick={() => revoke(k.id)} className="text-muted-foreground hover:text-bear" data-testid={`bridge-revoke-${k.id}`}><Trash2 className="w-3.5 h-3.5" /></button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel title="LINKED MT5 ACCOUNTS" actions={<button onClick={load} className="text-muted-foreground hover:text-primary" data-testid="bridge-refresh-btn"><RefreshCw className="w-3.5 h-3.5" /></button>}>
          {accts.length === 0 ? (
            <div className="py-8 text-center font-mono text-xs text-muted-foreground">— Run the bridge to register your MT5 account here —</div>
          ) : (
            <table className="w-full font-mono text-xs">
              <thead className="text-muted-foreground text-[10px] tracking-widest">
                <tr><th className="text-left py-2">LOGIN</th><th>SERVER</th><th>BROKER</th><th>BRIDGE</th><th>BAL</th><th>EQUITY</th><th className="text-right">STATUS</th></tr>
              </thead>
              <tbody>
                {accts.map((a) => (
                  <tr key={a.id} className="border-t border-border">
                    <td className="py-2">#{a.login}</td>
                    <td className="text-center">{a.server}</td>
                    <td className="text-center">{a.broker ?? "—"}</td>
                    <td className="text-center">
                      {a.bridge_version ? (
                        <span
                          className={`inline-block px-2 py-0.5 border rounded text-[10px] tracking-widest ${
                            a.bridge_outdated
                              ? "bg-bear/15 text-bear border-bear/30"
                              : "bg-bull/15 text-bull border-bull/30"
                          }`}
                          data-testid={`mt5-bridge-version-${a.id}`}
                          title={a.bridge_outdated ? `Re-download aurum_bridge.py from the BRIDGE section — minimum required is v${a.min_bridge_version}` : "Up to date"}
                        >
                          v{a.bridge_version}{a.bridge_outdated ? ` · UPDATE` : ""}
                        </span>
                      ) : (
                        <span className="text-muted-foreground text-[10px] tracking-widest">unknown</span>
                      )}
                    </td>
                    <td className="text-center">${Number(a.balance).toFixed(2)}</td>
                    <td className="text-center">${Number(a.equity).toFixed(2)}</td>
                    <td className={`text-right ${a.is_connected ? "text-bull" : "text-muted-foreground"}`}>
                      {a.is_connected ? "● LIVE" : "○ OFFLINE"}
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

const Step = ({ n, title, children }: { n: number; title: string; children: React.ReactNode }) => (
  <li className="flex gap-3">
    <span className="flex-shrink-0 w-6 h-6 rounded bg-primary text-primary-foreground font-mono text-xs flex items-center justify-center font-bold">{n}</span>
    <div className="flex-1">
      <div className="font-mono text-[11px] tracking-widest text-primary">{title}</div>
      {children}
    </div>
  </li>
);
