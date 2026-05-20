import { ReactNode } from "react";
import { cn } from "@/lib/utils";

export const Panel = ({
  title, subtitle, children, className, actions,
}: {
  title?: string; subtitle?: string; children: ReactNode; className?: string; actions?: ReactNode;
}) => (
  <section className={cn("surface terminal-border rounded relative overflow-hidden", className)}>
    {(title || actions) && (
      <header className="flex items-center justify-between px-3 py-2 border-b border-border bg-surface-elevated">
        <div className="flex items-baseline gap-2">
          {title && <h2 className="font-mono text-[11px] tracking-widest text-primary">{title}</h2>}
          {subtitle && <span className="font-mono text-[10px] text-muted-foreground">{subtitle}</span>}
        </div>
        {actions}
      </header>
    )}
    <div className="p-3">{children}</div>
  </section>
);

export const Stat = ({ label, value, delta, mono = true }: { label: string; value: ReactNode; delta?: { value: string; positive: boolean }; mono?: boolean }) => (
  <div className="flex flex-col">
    <span className="font-mono text-[10px] tracking-widest text-muted-foreground uppercase">{label}</span>
    <span className={cn("text-2xl leading-tight", mono && "font-mono font-semibold")}>{value}</span>
    {delta && (
      <span className={cn("font-mono text-xs", delta.positive ? "text-bull" : "text-bear")}>
        {delta.positive ? "▲" : "▼"} {delta.value}
      </span>
    )}
  </div>
);
