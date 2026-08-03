import type { Metadata } from "next";

import { StormSimulator } from "./simulator";

export const metadata: Metadata = {
  title: "Storm simulator — Lighthouse",
  description:
    "Author a hurricane track, set its intensity, size and speed, and preview modelled building impact across Jamaica.",
};

export default function SimulatorPage() {
  return <StormSimulator />;
}
