import { namedFlavor } from "@protomaps/basemaps";
import type { Flavor } from "@protomaps/basemaps";

/* The basemap, dressed in our tokens.
 *
 * A stock basemap would put half the pixels on this screen outside our control:
 * colours we did not choose, road hierarchies shouting over the data, place
 * labels sized for a phone rather than a room. The map is Register I, and the
 * whole point of Register I is that it looks like the instrument it is.
 *
 * MapLibre styles cannot read CSS custom properties, so the tokens are resolved
 * once at map init and handed in. tokens.css stays the single source — change a
 * value there and the basemap follows, which is the only arrangement that does
 * not drift.
 *
 * Base is the `black` flavor rather than `dark`: `dark` is a designed dark-grey
 * cartography with its own opinions, and we are overriding almost all of them
 * anyway. Starting from the quieter one means fewer fights.
 */

export type Tokens = {
  ground: string;
  panel: string;
  rule: string;
  quiet: string;
  figure: string;
};

/** Read the design tokens off the document, so there is one source of truth. */
export function readTokens(el: HTMLElement = document.documentElement): Tokens {
  const style = getComputedStyle(el);
  const get = (name: string, fallback: string) =>
    style.getPropertyValue(name).trim() || fallback;
  return {
    ground: get("--lh-ground", "#101413"),
    panel: get("--lh-panel", "#191e1c"),
    rule: get("--lh-rule", "#2e3532"),
    quiet: get("--lh-quiet", "#7f8b85"),
    figure: get("--lh-figure", "#e9eae4"),
  };
}

/** Blend two hex colours. Small, and it keeps derived tones tied to the tokens
 * rather than adding two more hard-coded values to the palette. */
function mix(a: string, b: string, amount: number): string {
  const parse = (h: string) => {
    const v = h.replace("#", "");
    const n = v.length === 3 ? v.split("").map((c) => c + c).join("") : v;
    return [0, 2, 4].map((i) => parseInt(n.slice(i, i + 2), 16));
  };
  const [r1, g1, b1] = parse(a);
  const [r2, g2, b2] = parse(b);
  const c = (x: number, y: number) => Math.round(x + (y - x) * amount);
  return `#${[c(r1, r2), c(g1, g2), c(b1, b2)]
    .map((n) => n.toString(16).padStart(2, "0"))
    .join("")}`;
}

/* Land is the lit surface and sea recedes — the opposite of a consumer map, and
 * the right way round for a screen where the land is the subject.
 *
 * The two tones are pushed apart rather than left at panel-over-ground: at
 * country zoom those sit close enough that Jamaica did not read as an island at
 * all, just a slightly different patch of dark.
 *
 * Roads are all one quiet value. A basemap that ranks motorways above lanes is
 * answering a driving question; the question here is which settlement a
 * household sits in, so the road network is texture, not hierarchy. */
export function lighthouseFlavor(t: Tokens): Flavor {
  const base = namedFlavor("black");
  // Land has to read as land at a glance, from across a room, with a
  // half-transparent wind field lying over it. At a tenth of the way to the
  // figure colour it was a slightly different shade of dark, and Cuba and
  // Hispaniola were indistinguishable from the sea they sit in.
  const land = mix(t.panel, t.figure, 0.26);
  const sea = mix(t.ground, "#000000", 0.55);
  const road = mix(t.rule, t.figure, 0.15);
  const casing = sea;

  return {
    ...base,

    background: sea,
    earth: land,
    water: sea,

    // Land cover: all one step off the earth tone. Parks and scrub carrying
    // their own greens would compete with the impact marks for attention.
    park_a: land, park_b: land,
    wood_a: land, wood_b: land,
    scrub_a: land, scrub_b: land,
    sand: land, beach: land, glacier: land,
    hospital: land, school: land, industrial: land,
    military: land, zoo: land, aerodrome: land,
    pedestrian: land, pier: land, runway: t.rule,

    buildings: mix(land, t.figure, 0.12),

    // One road colour, casings the ground so they read as separation not as ink.
    other: road, minor_a: road, minor_b: road, minor_service: road,
    link: road, major: road, highway: road, railway: road,
    tunnel_other: road, tunnel_minor: road, tunnel_link: road,
    tunnel_major: road, tunnel_highway: road,
    bridges_other: road, bridges_minor: road, bridges_link: road,
    bridges_major: road, bridges_highway: road,

    minor_service_casing: casing, minor_casing: casing, link_casing: casing,
    major_casing_early: casing, major_casing_late: casing,
    highway_casing_early: casing, highway_casing_late: casing,
    tunnel_other_casing: casing, tunnel_minor_casing: casing,
    tunnel_link_casing: casing, tunnel_major_casing: casing,
    tunnel_highway_casing: casing,
    bridges_other_casing: casing, bridges_minor_casing: casing,
    bridges_link_casing: casing, bridges_major_casing: casing,
    bridges_highway_casing: casing,

    // Parish and country lines carry more weight than roads: they are the units
    // relief is actually allocated in.
    boundaries: t.quiet,

    // Labels quiet, halos the ground. A place name is orientation, not content.
    city_label: t.figure, city_label_halo: sea,
    state_label: t.quiet, state_label_halo: sea,
    country_label: t.quiet,
    subplace_label: t.quiet, subplace_label_halo: sea,
    roads_label_major: t.quiet, roads_label_major_halo: sea,
    roads_label_minor: t.quiet, roads_label_minor_halo: sea,
    address_label: t.quiet, address_label_halo: sea,
    ocean_label: t.rule,
  };
}

/* Layer classes we drop outright.
 *
 * Every one of these is a real map feature and none of them answers a question
 * an operations room is asking during a landfall. Rule from the design doc:
 * cut anything that does not serve the brief, and an EOC screen is read at
 * distance by somebody tracking many things at once. */
const DROPPED = /^(pois|place_label_other|address_label|landuse_(pedestrian|aerodrome|zoo|military))/;

export function pruneLayers<T extends { id: string }>(layers: T[]): T[] {
  return layers.filter((l) => !DROPPED.test(l.id));
}
