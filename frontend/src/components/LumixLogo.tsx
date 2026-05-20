import { CSSProperties } from "react";

type Props = {
  size?: number;
  showWordmark?: boolean;
  className?: string;
  style?: CSSProperties;
  "data-testid"?: string;
};

/** LumixTrade brand mark — refracted "L" with an ascending chart line piercing it.
 *  Amber gradient on near-black. Optionally renders the wordmark "LUMIX·TRADE". */
export function LumixLogo({ size = 32, showWordmark = true, className, style, ...rest }: Props) {
  return (
    <span
      className={"inline-flex items-center gap-2 " + (className ?? "")}
      style={style}
      data-testid={rest["data-testid"] ?? "lumix-logo"}
    >
      <svg
        width={size}
        height={size}
        viewBox="0 0 64 64"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        aria-label="LumixTrade"
        role="img"
      >
        <defs>
          <linearGradient id="lx-g" x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse">
            <stop offset="0%" stopColor="#FFD68A" />
            <stop offset="55%" stopColor="#F5A524" />
            <stop offset="100%" stopColor="#A06318" />
          </linearGradient>
          <linearGradient id="lx-rise" x1="0" y1="64" x2="64" y2="0" gradientUnits="userSpaceOnUse">
            <stop offset="0%" stopColor="#F5A524" stopOpacity="0.0" />
            <stop offset="60%" stopColor="#F5A524" stopOpacity="0.85" />
            <stop offset="100%" stopColor="#FFE7B5" />
          </linearGradient>
        </defs>
        <rect width="64" height="64" rx="14" fill="#0A0907" />
        <path d="M16 12 H22 V46 H46 V52 H16 Z" fill="url(#lx-g)" />
        <path
          d="M14 52 L26 40 L36 46 L52 22"
          stroke="url(#lx-rise)"
          strokeWidth="3.2"
          strokeLinecap="round"
          strokeLinejoin="round"
          fill="none"
        />
        <circle cx="52" cy="22" r="2.6" fill="#FFE7B5" />
        <path d="M44 18 L50 18 L50 24" stroke="#F5A524" strokeWidth="1.6" fill="none" strokeLinecap="round" />
      </svg>
      {showWordmark && (
        <span className="font-mono font-bold tracking-widest text-primary leading-none select-none">
          LUMIX<span className="text-foreground">·TRADE</span>
        </span>
      )}
    </span>
  );
}
