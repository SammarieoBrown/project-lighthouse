"use client";

import { useEffect, useState } from "react";

/* Ground switch for the specimen sheet.
 *
 * Real screens will not carry this control: the console is dark and the portal
 * is light because those are different rooms, and that is a product decision
 * rather than a preference (see tokens.css). It exists here so both grounds
 * can be checked against the same page without two deploys.
 *
 * No transition on the swap. Changing theme is not one of the three things
 * allowed to move under rule M1, and a cross-fading page is a good way to make
 * a static interface feel live when it isn't.
 */

type Ground = "dark" | "light";

export function ThemeToggle() {
  const [ground, setGround] = useState<Ground | null>(null);

  // Resolve against the system preference on mount rather than at render, so
  // the server and the first client paint agree.
  useEffect(() => {
    const preferred: Ground = window.matchMedia("(prefers-color-scheme: light)")
      .matches
      ? "light"
      : "dark";
    setGround(preferred);
  }, []);

  useEffect(() => {
    if (ground) document.documentElement.dataset.theme = ground;
  }, [ground]);

  const next: Ground = ground === "light" ? "dark" : "light";

  return (
    <button
      type="button"
      onClick={() => setGround(next)}
      aria-label={`Switch to the ${next} ground`}
      style={{
        font: "inherit",
        fontFamily: "var(--lh-font-data)",
        fontSize: "var(--lh-text-micro)",
        letterSpacing: "0.08em",
        textTransform: "uppercase",
        color: "var(--lh-quiet)",
        background: "transparent",
        border: "1px solid var(--lh-rule)",
        borderRadius: "var(--lh-radius)",
        padding: "var(--lh-space-2) var(--lh-space-3)",
        cursor: "pointer",
      }}
    >
      {/* Empty until mounted, so the label never claims a ground we have not
          actually resolved yet. */}
      {ground ? `Ground: ${ground}` : "Ground"}
    </button>
  );
}
