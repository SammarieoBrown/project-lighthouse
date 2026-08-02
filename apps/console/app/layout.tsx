import type { Metadata } from "next";
import { Archivo, IBM_Plex_Mono, Public_Sans } from "next/font/google";

import "./tokens.css";
import "./globals.css";

// Three families, three roles. See tokens.css for why each one is here.
// Loaded through next/font so they are self-hosted at build time — no request
// to a third party, which matters for a console that has to work on a bad
// connection in a room that has just lost power.

const display = Archivo({
  subsets: ["latin"],
  axes: ["wdth"],
  variable: "--lh-font-display-loaded",
  display: "swap",
});

const body = Public_Sans({
  subsets: ["latin"],
  variable: "--lh-font-body-loaded",
  display: "swap",
});

const data = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--lh-font-data-loaded",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Lighthouse — design substrate",
  description:
    "The committed token system for the Lighthouse EOC console and public transparency portal.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      lang="en"
      className={`${display.variable} ${body.variable} ${data.variable}`}
    >
      <body>{children}</body>
    </html>
  );
}
