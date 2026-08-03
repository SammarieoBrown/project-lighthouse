"use client";

import { useEffect } from "react";

/**
 * Install the read-only offline shell after a successful production visit.
 *
 * Development deliberately stays uncontrolled: a stale service worker can make
 * a map fix look broken after the code has changed, which is exactly the class
 * of silent failure documented in lighthouse-map-stack.md.
 */
export function ServiceWorkerRegister() {
  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;

    if (process.env.NODE_ENV !== "production") {
      // A worker registered by `next start` survives a later `next dev` on the
      // same origin. Remove only Lighthouse's own worker and caches so local
      // edits cannot be shadowed by a previous production build.
      void navigator.serviceWorker
        .getRegistrations()
        .then((registrations) =>
          Promise.all(
            registrations
              .filter((registration) =>
                [registration.active, registration.waiting, registration.installing].some(
                  (worker) => worker?.scriptURL.endsWith("/sw.js"),
                ),
              )
              .map((registration) => registration.unregister()),
          ),
        )
        .catch((error: unknown) => console.warn("[offline] development cleanup failed", error));
      if ("caches" in window) {
        void caches
          .keys()
          .then((keys) =>
            Promise.all(
              keys
                .filter((key) => key.startsWith("lighthouse-console-"))
                .map((key) => caches.delete(key)),
            ),
          )
          .catch((error: unknown) => console.warn("[offline] cache cleanup failed", error));
      }
      return;
    }

    void navigator.serviceWorker
      .register("/sw.js")
      .then(async () => {
        const registration = await navigator.serviceWorker.ready;
        const resourceUrls = performance
          .getEntriesByType("resource")
          .map((entry) => entry.name)
          .filter((entry): entry is string => typeof entry === "string");

        (navigator.serviceWorker.controller ?? registration.active)?.postMessage({
          type: "cache-urls",
          urls: resourceUrls,
        });
      })
      .catch((error: unknown) => {
        // Registration failure does not take down the live console. It remains
        // visible in diagnostics rather than being mistaken for offline support.
        console.error("[offline] service worker registration failed", error);
      });
  }, []);

  return null;
}
