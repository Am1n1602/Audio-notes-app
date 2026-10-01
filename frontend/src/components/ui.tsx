import Link from "next/link";
import type { ButtonHTMLAttributes, ComponentProps, ReactNode } from "react";
import type { Tone } from "@/lib/status";

// The few building blocks every screen shares, so buttons, status marks and notices look and behave the same.

type Variant = "primary" | "secondary" | "quiet";

const base =
  "inline-flex items-center justify-center rounded-control px-4 py-2 text-[0.9375rem] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50";
const variants: Record<Variant, string> = {
  primary: "bg-ink text-paper hover:bg-ink/85",
  secondary: "border border-ink/30 hover:border-ink hover:bg-sheet",
  quiet: "px-0 underline decoration-rule decoration-2 underline-offset-4 hover:decoration-ink",
};

export const buttonClass = (variant: Variant = "secondary", extra = "") => `${base} ${variants[variant]} ${extra}`;

export function Button({
  variant,
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return <button type="button" className={buttonClass(variant, className)} {...props} />;
}

export function ButtonLink({
  variant,
  className = "",
  ...props
}: ComponentProps<typeof Link> & { variant?: Variant }) {
  return <Link className={buttonClass(variant, className)} {...props} />;
}

const dot: Record<Tone, string> = {
  working: "bg-signal beat",
  done: "bg-ok",
  failed: "bg-bad",
  idle: "border border-soft",
};
const words: Record<Tone, string> = {
  working: "text-signal-ink",
  done: "text-ok",
  failed: "text-bad",
  idle: "text-soft",
};

/** A status as a coloured mark AND a word: colour alone never carries the meaning. */
export function StatusMark({ tone, label }: { tone: Tone; label: string }) {
  return (
    <span className="inline-flex items-center gap-2 whitespace-nowrap text-[0.9375rem]">
      <span aria-hidden className={`h-2 w-2 shrink-0 rounded-full ${dot[tone]}`} />
      <span className={words[tone]}>{label}</span>
    </span>
  );
}

/** Something went wrong (or needs attention). Says what happened, then what to do; `actions` are the ways out. */
export function Notice({
  title,
  children,
  actions,
  tone = "bad",
}: {
  title: string;
  children?: ReactNode;
  actions?: ReactNode;
  tone?: "bad" | "neutral";
}) {
  const colours = tone === "bad" ? "border-bad/40 bg-bad-wash" : "border-rule bg-sheet";
  return (
    <div role={tone === "bad" ? "alert" : "status"} className={`rounded-surface border p-5 ${colours}`}>
      <p className={`font-medium ${tone === "bad" ? "text-bad" : ""}`}>{title}</p>
      {children && <div className="mt-1.5 space-y-1.5 text-[0.9375rem]">{children}</div>}
      {actions && <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2">{actions}</div>}
    </div>
  );
}

/** A placeholder row while something loads. Still: nothing moves that does not mean something. */
export function SkeletonLine({ className = "" }: { className?: string }) {
  return <div aria-hidden className={`h-4 rounded-control bg-rule/70 ${className}`} />;
}

/** The skeleton's wrapper: the lines are hidden from screen readers, so this is the part that says what is loading. */
export function Loading({ label, className = "", children }: { label: string; className?: string; children: ReactNode }) {
  return (
    <div role="status" className={className}>
      <span className="sr-only">{label}</span>
      {children}
    </div>
  );
}

/** Checks are failing but an older answer is still on screen. */
export function StaleNote({ className = "" }: { className?: string }) {
  return (
    <p role="status" className={`text-sm text-soft ${className}`}>
      Can&apos;t reach the server right now. Showing what was last loaded; this page keeps trying.
    </p>
  );
}
