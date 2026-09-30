import type { Metadata } from "next";
import { Literata, Schibsted_Grotesk } from "next/font/google";
import { SiteHeader } from "@/components/site-header";
import { UploadGuard } from "@/components/upload-guard";
import "./globals.css";

const ui = Schibsted_Grotesk({ subsets: ["latin"], variable: "--font-ui", display: "swap" });
const read = Literata({ subsets: ["latin"], variable: "--font-read", display: "swap" });

export const metadata: Metadata = {
  title: { default: "Audio Notes", template: "%s · Audio Notes" },
  description: "Upload a recording and get a transcript and a short summary.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${ui.variable} ${read.variable} h-full`}>
      <body className="flex min-h-full flex-col">
        <a
          href="#main"
          className="sr-only rounded-control bg-ink px-3 py-2 text-paper focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-10"
        >
          Skip to content
        </a>
        <SiteHeader />
        <main id="main" className="mx-auto w-full max-w-[46rem] flex-1 px-5 pb-24 pt-10 sm:pt-14">
          {children}
        </main>
        <UploadGuard />
      </body>
    </html>
  );
}
