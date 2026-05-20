import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Activity, ArrowRight, Bot, ChartCandlestick, Cpu, Download, Lock, Radio, Shield, Zap } from "lucide-react";
import { TickerTape } from "@/components/TickerTape";
import { LumixLogo } from "@/components/LumixLogo";

export default function Landing() {
  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-border surface">
        <div className="container flex items-center h-14">
          <Link to="/" className="flex items-center gap-2" data-testid="landing-logo">
            <LumixLogo size={26} showWordmark />
          </Link>
          <nav className="ml-8 hidden md:flex items-center gap-6 font-mono text-[11px] tracking-widest text-muted-foreground">
            <a href="#engine" className="hover:text-primary">ENGINE</a>
            <a href="#bridge" className="hover:text-primary">MT5 BRIDGE</a>
            <a href="#pricing" className="hover:text-primary">PRICING</a>
            <a href="#risk" className="hover:text-primary">RISK</a>
          </nav>
          <div className="ml-auto flex gap-2">
            <Button asChild variant="ghost" size="sm" className="font-mono text-xs tracking-widest">
              <Link to="/auth">SIGN IN</Link>
            </Button>
            <Button asChild size="sm" className="font-mono text-xs tracking-widest">
              <Link to="/auth">OPEN TERMINAL <ArrowRight className="w-3 h-3 ml-1" /></Link>
            </Button>
          </div>
        </div>
      </header>

      <TickerTape />

      {/* HERO */}
      <section className="container py-16 md:py-24 relative">
        <div className="grid md:grid-cols-12 gap-8 items-center">
          <div className="md:col-span-7 animate-fade-up">
            <div className="inline-flex items-center gap-2 px-3 py-1 surface terminal-border rounded font-mono text-[10px] tracking-widest text-primary mb-6">
              <span className="w-1.5 h-1.5 bg-bull rounded-full pulse-dot" /> LIVE · SESSION-AWARE · MT5 NATIVE
            </div>
            <h1 className="font-display text-4xl md:text-6xl font-bold leading-[1.05] tracking-tight">
              Automated forex<br/>& gold trading on<br/>
              <span className="text-primary">MT5, the right way.</span>
            </h1>
            <p className="mt-5 text-muted-foreground max-w-xl text-lg">
              A signal engine built around session structure, market regime and strict risk limits. Connect MT5 with a one-line bridge. Stop guessing. Start systematizing.
            </p>
            <div className="mt-8 flex flex-wrap gap-3">
              <Button asChild size="lg" className="font-mono tracking-widest">
                <Link to="/auth">START TRADING <ArrowRight className="w-4 h-4 ml-2" /></Link>
              </Button>
              <Button asChild size="lg" variant="outline" className="font-mono tracking-widest border-border-strong">
                <a href="#pricing">SEE PLANS</a>
              </Button>
            </div>
            <div className="mt-8 grid grid-cols-3 gap-6 max-w-md">
              {[
                { l: "PAIRS", v: "FX + XAUUSD" },
                { l: "BROKERS", v: "ANY MT5" },
                { l: "RISK", v: "PER-TRADE %" },
              ].map((s) => (
                <div key={s.l}>
                  <div className="font-mono text-[10px] text-muted-foreground tracking-widest">{s.l}</div>
                  <div className="font-mono text-sm text-foreground mt-0.5">{s.v}</div>
                </div>
              ))}
            </div>
          </div>

          {/* terminal preview */}
          <div className="md:col-span-5">
            <div className="surface terminal-border rounded relative overflow-hidden glow-amber animate-fade-up">
              <div className="flex items-center justify-between px-3 py-2 border-b border-border bg-surface-elevated">
                <span className="font-mono text-[10px] tracking-widest text-primary">SIGNAL · XAUUSD · M15</span>
                <span className="font-mono text-[10px] text-bull">▲ 0.42%</span>
              </div>
              <div className="p-4 font-mono text-xs space-y-2.5 scanline relative">
                <Row k="REGIME" v="TRENDING_UP" cls="text-bull" />
                <Row k="SESSION" v="LONDON · NY OVERLAP" />
                <Row k="EMA(21/55)" v="2421.3 / 2418.7" />
                <Row k="RSI(14)" v="58.2" />
                <Row k="ATR(14)" v="3.41" />
                <div className="terminal-divider my-2" />
                <Row k="SIDE" v="BUY" cls="text-bull font-bold" />
                <Row k="ENTRY" v="2422.50" />
                <Row k="STOP" v="2417.40" cls="text-bear" />
                <Row k="TARGET" v="2431.10" cls="text-bull" />
                <Row k="LOT" v="0.18" />
                <Row k="CONFIDENCE" v="0.74" cls="text-primary" />
                <div className="terminal-divider my-2" />
                <div className="flex justify-between items-center pt-1">
                  <span className="text-muted-foreground">→ DISPATCHED TO MT5 BRIDGE</span>
                  <span className="text-bull">● FILLED</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ENGINE */}
      <section id="engine" className="border-t border-border bg-surface/40">
        <div className="container py-16">
          <SectionHead label="// THE ENGINE" title="Built like a desk, not a chatbot." />
          <div className="grid md:grid-cols-3 gap-4 mt-8">
            {[
              { i: ChartCandlestick, t: "REGIME DETECTION", d: "Classifies the tape as trending, ranging or volatile from EMA structure + ATR expansion. The bot only fires when conditions match the strategy." },
              { i: Activity, t: "SESSION FILTERING", d: "Asia / London / NY / Overlap. Trade when liquidity is real. Idle through chop and news blackouts." },
              { i: Shield, t: "RISK MANAGEMENT", d: "Per-trade % risk, ATR-based stops, position cap, daily loss limit. The engine refuses trades that violate your rules." },
              { i: Cpu, t: "MULTI-INDICATOR LOGIC", d: "EMA crossover + RSI confirmation + ATR-sized SL/TP. Configurable per bot. No black-box." },
              { i: Radio, t: "LIVE SIGNALS", d: "Streaming signals to your dashboard with full reasoning. Approve manually or let the bridge auto-execute." },
              { i: Lock, t: "YOUR ACCOUNT, YOUR KEYS", d: "We never see your MT5 password. The bridge runs locally and only forwards order intents from your account." },
            ].map(({ i: Icon, t, d }) => (
              <div key={t} className="surface terminal-border rounded p-4 hover:border-border-strong transition-colors">
                <Icon className="w-5 h-5 text-primary mb-3" />
                <div className="font-mono text-[11px] tracking-widest text-primary mb-1">{t}</div>
                <p className="text-sm text-muted-foreground">{d}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* BRIDGE */}
      <section id="bridge" className="border-t border-border">
        <div className="container py-16 grid md:grid-cols-2 gap-10 items-center">
          <div>
            <SectionHead label="// MT5 BRIDGE" title="One file. One command. Connected." />
            <p className="text-muted-foreground mt-4">
              MT5 has no public cloud API — so we ship a tiny Python bridge that runs alongside your MT5 terminal (Windows or VPS). It authenticates with your account, polls your signals, and executes orders directly.
            </p>
            <ul className="mt-5 space-y-2 text-sm">
              <Bullet>Download from your dashboard</Bullet>
              <Bullet>Paste your one-time API key</Bullet>
              <Bullet>Run <code className="font-mono text-primary">python aurum_bridge.py</code></Bullet>
              <Bullet>Status turns green. Bot lives.</Bullet>
            </ul>
            <Button asChild size="lg" className="mt-7 font-mono tracking-widest">
              <Link to="/auth">GET THE BRIDGE <Download className="w-4 h-4 ml-2" /></Link>
            </Button>
          </div>
          <div className="surface terminal-border rounded p-5 font-mono text-xs scanline relative">
            <div className="text-muted-foreground">$ python aurum_bridge.py</div>
            <div className="text-primary mt-2">[LUMIX] connecting to MT5…</div>
            <div className="text-bull">[OK] account #5023149 · ICMarkets-Live · USD 12,480.55</div>
            <div className="text-primary mt-1">[LUMIX] subscribed to bots: GOLD-LON-NY, EURUSD-OVRLAP</div>
            <div className="text-muted-foreground mt-2">[..] polling signals every 5s</div>
            <div className="text-bull mt-2">[FILL] BUY XAUUSD 0.18 @ 2422.50 · ticket 8821</div>
            <div className="text-bull">[FILL] SELL EURUSD 0.25 @ 1.08412 · ticket 8822</div>
            <div className="text-muted-foreground mt-2">[LUMIX] heartbeat OK</div>
          </div>
        </div>
      </section>

      {/* PRICING */}
      <section id="pricing" className="border-t border-border bg-surface/40">
        <div className="container py-16">
          <SectionHead label="// PRICING" title="Three plans. Same engine." />
          <div className="grid md:grid-cols-3 gap-4 mt-8">
            {[
              { name: "MONTHLY", price: "$49", per: "/ month", best: false, save: null },
              { name: "QUARTERLY", price: "$129", per: "/ 3 months", best: true, save: "SAVE 12%" },
              { name: "YEARLY", price: "$449", per: "/ year", best: false, save: "SAVE 24%" },
            ].map((p) => (
              <div key={p.name} className={`surface terminal-border rounded p-5 relative ${p.best ? "border-primary/60 glow-amber" : ""}`}>
                {p.save && (
                  <span className="absolute -top-2 right-3 px-2 py-0.5 bg-primary text-primary-foreground font-mono text-[10px] tracking-widest rounded">{p.save}</span>
                )}
                <div className="font-mono text-[11px] tracking-widest text-primary">{p.name}</div>
                <div className="mt-3 flex items-baseline gap-1">
                  <span className="font-display text-4xl font-bold">{p.price}</span>
                  <span className="font-mono text-xs text-muted-foreground">{p.per}</span>
                </div>
                <ul className="mt-5 space-y-2 text-sm">
                  <Bullet>Unlimited bots</Bullet>
                  <Bullet>All FX pairs + XAUUSD</Bullet>
                  <Bullet>Session + regime engine</Bullet>
                  <Bullet>MT5 bridge & auto-execute</Bullet>
                  <Bullet>Live dashboard & history</Bullet>
                </ul>
                <Button asChild className="w-full mt-6 font-mono tracking-widest" variant={p.best ? "default" : "outline"}>
                  <Link to="/auth">START {p.name}</Link>
                </Button>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* RISK */}
      <section id="risk" className="border-t border-border">
        <div className="container py-12">
          <div className="surface terminal-border rounded p-5">
            <div className="font-mono text-[11px] tracking-widest text-warning mb-2">// RISK DISCLOSURE</div>
            <p className="text-sm text-muted-foreground leading-relaxed">
              Forex, CFD and gold trading involves substantial risk and may result in the loss of your invested capital. Past performance is not indicative of future results. LumixTrade provides software tools; it does not provide financial advice and is not a broker. You are responsible for the trades executed on your MT5 account. Use small position sizes and only risk what you can afford to lose.
            </p>
          </div>
        </div>
      </section>

      <footer className="border-t border-border">
        <div className="container py-6 flex flex-col md:flex-row gap-3 items-center justify-between font-mono text-[10px] tracking-widest text-muted-foreground">
          <span>© {new Date().getFullYear()} LUMIXTRADE</span>
          <span>BUILT FOR TRADERS WHO TREAT THE MARKET AS A SYSTEM</span>
        </div>
      </footer>
    </div>
  );
}

const SectionHead = ({ label, title }: { label: string; title: string }) => (
  <div>
    <div className="font-mono text-[11px] tracking-widest text-primary">{label}</div>
    <h2 className="font-display text-3xl md:text-4xl font-bold mt-2 tracking-tight">{title}</h2>
  </div>
);
const Row = ({ k, v, cls = "" }: { k: string; v: string; cls?: string }) => (
  <div className="flex justify-between"><span className="text-muted-foreground">{k}</span><span className={cls}>{v}</span></div>
);
const Bullet = ({ children }: { children: React.ReactNode }) => (
  <li className="flex gap-2 items-start"><Zap className="w-3.5 h-3.5 text-primary mt-0.5 shrink-0" /><span>{children}</span></li>
);
