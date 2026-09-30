"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

function NavLink({ href, current, children }: { href: string; current: boolean; children: string }) {
  return (
    <Link
      href={href}
      aria-current={current ? "page" : undefined}
      className={`py-1 ${current ? "underline decoration-2 underline-offset-[6px]" : "text-soft hover:text-ink"}`}
    >
      {children}
    </Link>
  );
}

export function SiteHeader() {
  const path = usePathname();
  return (
    <header className="border-b border-rule">
      <div className="mx-auto flex max-w-[46rem] items-baseline justify-between px-5 py-4">
        <Link href="/" className="font-serif text-xl font-semibold tracking-tight">
          Audio Notes
        </Link>
        <nav aria-label="Main" className="flex gap-6 text-[0.9375rem]">
          <NavLink href="/" current={path === "/" || path.startsWith("/uploads")}>
            Recordings
          </NavLink>
          <NavLink href="/architecture" current={path === "/architecture"}>
            How it works
          </NavLink>
        </nav>
      </div>
    </header>
  );
}
