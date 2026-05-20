import { Link, NavLink, useNavigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { Activity, BarChart3, Bot, CreditCard, Download, Gift, LogOut, Radio, Receipt, Settings, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/utils";
import { LumixLogo } from "@/components/LumixLogo";

const nav = [
  { to: "/app", label: "TERMINAL", icon: Activity, end: true },
  { to: "/app/bots", label: "BOTS", icon: Bot },
  { to: "/app/signals", label: "SIGNALS", icon: Radio },
  { to: "/app/trades", label: "TRADES", icon: BarChart3 },
  { to: "/app/bridge", label: "BRIDGE", icon: Download },
  { to: "/app/billing", label: "BILLING", icon: CreditCard },
  { to: "/app/transactions", label: "TXNS", icon: Receipt },
  { to: "/app/referrals", label: "REFER", icon: Gift },
  { to: "/app/settings", label: "SETTINGS", icon: Settings },
];

export const AppShell = ({ children }: { children: React.ReactNode }) => {
  const { user, signOut } = useAuth();
  const isAdmin = useIsAdmin();
  const navigate = useNavigate();
  const items = isAdmin ? [...nav, { to: "/app/admin", label: "ADMIN", icon: ShieldCheck }] : nav;
  const initials = (user?.display_name || user?.email || "?").slice(0, 1).toUpperCase();
  return (
    <div className="min-h-screen flex flex-col bg-background">
      <header className="border-b border-border surface sticky top-0 z-40">
        <div className="flex items-center h-12 overflow-hidden">
          <Link to="/app" className="px-3 h-full flex items-center gap-2 border-r border-border shrink-0" data-testid="shell-logo">
            <LumixLogo size={20} showWordmark data-testid="shell-logo-mark" />
          </Link>
          <nav className="flex h-full flex-1 min-w-0">
            {items.map((n: any) => (
              <NavLink key={n.to} to={n.to} end={n.end}
                title={n.label}
                data-testid={`nav-${n.label.replace(/\s+/g,'-').toLowerCase()}`}
                className={({ isActive }) => cn(
                  "px-2.5 h-full flex items-center gap-1.5 text-[10px] font-mono tracking-widest border-r border-border transition-colors shrink-0",
                  isActive ? "bg-surface-elevated text-primary border-b-2 border-b-primary -mb-px" : "text-muted-foreground hover:text-foreground hover:bg-surface-hover"
                )}>
                <n.icon className="w-3.5 h-3.5 shrink-0" />
                <span className="hidden md:inline">{n.label}</span>
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-2 px-3 shrink-0 border-l border-border h-full">
            <div className="hidden lg:flex items-center gap-2 font-mono text-[11px] text-muted-foreground max-w-[180px]" title={user?.email}>
              <span className="w-6 h-6 rounded-full bg-primary/10 border border-primary/30 text-primary flex items-center justify-center text-[10px] font-bold shrink-0">{initials}</span>
              <span className="truncate">{user?.display_name || user?.email}</span>
            </div>
            <span className="lg:hidden w-6 h-6 rounded-full bg-primary/10 border border-primary/30 text-primary flex items-center justify-center text-[10px] font-bold" title={user?.email}>{initials}</span>
            <button onClick={async () => { await signOut(); navigate("/"); }}
              className="flex items-center gap-1 text-[10px] font-mono tracking-widest text-muted-foreground hover:text-bear transition-colors"
              data-testid="shell-signout-btn">
              <LogOut className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">SIGN OUT</span>
            </button>
          </div>
        </div>
      </header>
      <main className="flex-1">{children}</main>
      <footer className="border-t border-border px-4 py-2 text-[10px] font-mono text-muted-foreground tracking-widest flex justify-between gap-4">
        <span className="shrink-0">LUMIXTRADE TERMINAL v1.4</span>
        <span className="hidden md:inline truncate">TRADING INVOLVES SUBSTANTIAL RISK · PAST RESULTS DO NOT GUARANTEE FUTURE PERFORMANCE</span>
      </footer>
    </div>
  );
};
