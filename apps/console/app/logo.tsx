/* The Lighthouse mark.
 *
 * A lighthouse tower is a stack of horizontal bands — the painted daymark that
 * makes it identifiable from sea in daylight, before the light is any use. A
 * ledger is also a stack of horizontal bands. The mark is built from that
 * coincidence: the tower *is* a stack of recorded entries, tapering as it
 * rises, standing on a base wider than itself.
 *
 * So it is the product thesis in a glyph, and it is drawn in the same language
 * as Register III and the transition line — ruled lines, nothing else. No
 * gradient, no beam sweeping, no motion. A lighthouse that flashes at you from
 * a toolbar would break rule M1 on the first frame.
 *
 * Everything is currentColor, so the mark works in both grounds without a
 * second asset.
 */

type MarkProps = {
  size?: number;
  /** The light. Drop it below ~20px, where two sub-pixel bars turn to mush. */
  beam?: boolean;
  title?: string;
};

export function LighthouseMark({
  size = 24,
  beam = true,
  title,
}: MarkProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="currentColor"
      role={title ? "img" : "presentation"}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      focusable="false"
    >
      {title ? <title>{title}</title> : null}

      {/* the light, thrown both ways */}
      {beam ? (
        <>
          <rect x="1.5" y="3.1" width="5.5" height="1.4" />
          <rect x="17" y="3.1" width="5.5" height="1.4" />
        </>
      ) : null}

      {/* lantern room — the one solid mass */}
      <rect x="9.5" y="2" width="5" height="3.75" />

      {/* The gallery: the walkway that rings the lantern and overhangs the
       * tower. It is the single feature that stops a tapered stack from
       * reading as a cone or a Christmas tree, so it is wider than everything
       * above it and everything immediately below. */}
      <rect x="8" y="6" width="8" height="1.25" />

      {/* Daymark bands, widening as they descend. Four entries in the stack —
       * the taper is gentle on purpose, because a steep one reads as a cone
       * rather than a tower, which is the wrong object entirely. */}
      <rect x="9" y="8.5" width="6" height="1.75" />
      <rect x="8.5" y="11.25" width="7" height="1.75" />
      <rect x="8" y="14" width="8" height="1.75" />
      <rect x="7.5" y="16.75" width="9" height="1.75" />

      {/* the rock it is built on */}
      <rect x="5" y="19.75" width="14" height="2.25" />
    </svg>
  );
}

/* Mark plus wordmark, locked. The wordmark is Archivo at its widest — the mark
 * is all horizontals, so the type has to be wide enough not to look pinched
 * beside it. */
export function LighthouseLockup({ size = 28 }: { size?: number }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "var(--lh-space-3)",
      }}
    >
      <LighthouseMark size={size} title="Lighthouse" />
      <span
        style={{
          fontFamily: "var(--lh-font-display)",
          fontWeight: 700,
          fontVariationSettings: '"wdth" 125',
          fontSize: `${Math.round(size * 0.82)}px`,
          letterSpacing: "0.02em",
          textTransform: "uppercase",
          lineHeight: 1,
        }}
      >
        Lighthouse
      </span>
    </span>
  );
}
