import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Lighthouse EOC console",
    short_name: "Lighthouse",
    description: "Hurricane replay, forecast exposure and decision context for Jamaica.",
    id: "/eoc",
    start_url: "/eoc",
    scope: "/eoc",
    display: "standalone",
    // Locked dark-ground tokens from tokens.css. A web-app manifest cannot read
    // CSS variables, so the values are duplicated here with their source named.
    background_color: "#101413",
    theme_color: "#101413",
    icons: [{ src: "/icon.svg", sizes: "any", type: "image/svg+xml" }],
  };
}
