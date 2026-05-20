// Cosmetic ticker tape for the landing page. Pulls last cached prices if available.
import { useEffect, useState } from "react";
import { apiGet } from "@/api/client";

const FALLBACK = [
  { s: "XAUUSD", p: "2421.85", d: "+0.42%" },
  { s: "EURUSD", p: "1.0841", d: "-0.11%" },
  { s: "GBPUSD", p: "1.2733", d: "+0.07%" },
  { s: "USDJPY", p: "156.21", d: "+0.18%" },
  { s: "AUDUSD", p: "0.6604", d: "-0.21%" },
  { s: "USDCAD", p: "1.3712", d: "+0.05%" },
  { s: "XAGUSD", p: "31.42", d: "+0.62%" },
  { s: "NZDUSD", p: "0.5982", d: "-0.14%" },
];

type PriceRow = { symbol: string; close: number; ts: string };

export const TickerTape = () => {
  const [items, setItems] = useState(FALLBACK);

  useEffect(() => {
    (async () => {
      try {
        const data = await apiGet<PriceRow[]>("/price-cache", {
          params: { symbols: FALLBACK.map((x) => x.s).join(","), limit: 200 },
        });
        if (!data?.length) return;
        const latest = new Map<string, number>();
        for (const row of data) {
          if (!latest.has(row.symbol)) latest.set(row.symbol, Number(row.close));
        }
        setItems((prev) => prev.map((x) => latest.has(x.s) ? { ...x, p: latest.get(x.s)!.toFixed(x.s.startsWith("XAU") ? 2 : 4) } : x));
      } catch {
        // keep fallback
      }
    })();
  }, []);

  const tape = [...items, ...items];
  return (
    <div className="border-y border-border bg-surface overflow-hidden" data-testid="ticker-tape">
      <div className="flex animate-tape-scroll whitespace-nowrap py-2">
        {tape.map((t, i) => (
          <div key={i} className="flex items-center gap-3 px-6 font-mono text-[11px]">
            <span className="text-muted-foreground tracking-widest">{t.s}</span>
            <span className="text-foreground">{t.p}</span>
            <span className={t.d.startsWith("-") ? "text-bear" : "text-bull"}>{t.d}</span>
            <span className="text-border">·</span>
          </div>
        ))}
      </div>
    </div>
  );
};
