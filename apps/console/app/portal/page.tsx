import type { Metadata } from "next";

import { PublicPortal } from "./public-portal";

export const metadata: Metadata = {
  title: "Lighthouse · public ledger",
  description:
    "Where relief went, what it cost, and how long it took. Aggregate only.",
};

export default function PortalPage() {
  return <PublicPortal />;
}
