import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The EOC loses power and internet in exactly the conditions we exist for,
  // so the console is offline-first: service worker, IndexedDB write queue,
  // sync on reconnect. None of that is wired yet — it lands with the first
  // screen that writes.
};

export default nextConfig;
