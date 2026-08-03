import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The EOC loses power and internet in exactly the conditions we exist for,
  // so the read-only console caches its shell and replay after a successful
  // visit, then degrades to the replay-backed SVG map when PMTiles ranges are
  // unavailable. An IndexedDB write queue belongs to the first screen that
  // actually writes; this one deliberately has no mutation path to queue.
};

export default nextConfig;
