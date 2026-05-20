import { useEffect, useState } from "react";
import { apiGet } from "@/api/client";
import { AppShell } from "@/components/AppShell";
import { Panel } from "@/components/Panel";

export default function Signals() {
  const [rows, setRows] = useState<any[]>([]);
  useEffect(() => {
    let mounted = true;
    const load = async () => {
      try {
        const data = await apiGet<any[]>("/signals", { params: { limit: 200 } });
        if (mounted) setRows(data ?? []);
      } catch {}
    };
    load();
    const iv = setInterval(load, 8000);
    return () => { mounted = false; clearInterval(iv); };
  }, []);

  return (
    <AppShell>
      <div className="container py-6" data-testid="signals-page">
        <Panel title="ALL SIGNALS" subtitle={`${rows.length} records`}>
          {rows.length === 0 ? (
            <div className="py-12 text-center font-mono text-xs text-muted-foreground">— No signals generated yet —</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full font-mono text-xs">
                <thead className="text-muted-foreground text-[10px] tracking-widest">
                  <tr><th className="text-left py-2 px-2">TIME</th><th>PAIR</th><th>SIDE</th><th>ENTRY</th><th>SL</th><th>TP</th><th>LOT</th><th>CONF</th><th>REGIME</th><th>SESSION</th><th className="text-right px-2">STATUS</th></tr>
                </thead>
                <tbody>
                  {rows.map((s) => (
                    <tr key={s.id} className="border-t border-border hover:bg-surface-hover">
                      <td className="py-2 px-2 text-muted-foreground">{new Date(s.created_at).toLocaleString()}</td>
                      <td className="text-center">{s.pair}</td>
                      <td className={`text-center ${s.side === "buy" ? "text-bull" : "text-bear"}`}>{s.side.toUpperCase()}</td>
                      <td className="text-center">{Number(s.entry).toFixed(s.pair.startsWith("XAU") ? 2 : 5)}</td>
                      <td className="text-center text-bear">{Number(s.sl).toFixed(s.pair.startsWith("XAU") ? 2 : 5)}</td>
                      <td className="text-center text-bull">{Number(s.tp).toFixed(s.pair.startsWith("XAU") ? 2 : 5)}</td>
                      <td className="text-center">{Number(s.lot).toFixed(2)}</td>
                      <td className="text-center text-primary">{Number(s.confidence).toFixed(2)}</td>
                      <td className="text-center text-[10px]">{s.regime ?? "—"}</td>
                      <td className="text-center text-[10px]">{s.session ?? "—"}</td>
                      <td className="text-right px-2 uppercase text-[10px]">{s.status}</td>
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
