"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { LighthouseMark } from "./logo";
import styles from "./app-nav.module.css";

/* One way around the product, on every screen. Names say what you do there,
 * not what the system calls it. */
const SCREENS = [
  { href: "/", label: "Overview" },
  { href: "/eoc", label: "Storm map" },
  { href: "/operations", label: "Claims and payments" },
  { href: "/simulator", label: "Simulator" },
  { href: "/portal", label: "Public ledger" },
] as const;

export function AppNav() {
  const path = usePathname() ?? "/";
  return (
    <nav className={styles.bar} aria-label="Lighthouse screens">
      <Link className={styles.home} href="/" aria-label="Lighthouse overview">
        <LighthouseMark size={20} title="" />
        <span>Lighthouse</span>
      </Link>
      <ul className={styles.list}>
        {SCREENS.map((screen) => {
          const here = screen.href === "/" ? path === "/" : path.startsWith(screen.href);
          return (
            <li key={screen.href}>
              <Link
                className={styles.link}
                href={screen.href}
                aria-current={here ? "page" : undefined}
              >
                {screen.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
