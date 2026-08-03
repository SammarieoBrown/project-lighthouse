import { EocConsole } from "./console";

/* The route. The screen itself is a client component because the replay is
 * fetched at runtime rather than imported at build time — the generated file is
 * legitimately absent on a fresh clone and in CI, and a build that needs it is
 * a build that breaks for everyone who has not run the exporter.
 *
 * Metadata cannot be exported from a client component, which is the whole
 * reason this file is separate from console.tsx.
 */

export const metadata = {
  title: "Lighthouse — EOC console",
  description: "Posture, wind field and expected damage across the Melissa replay.",
};

export default function EocPage() {
  return <EocConsole />;
}
