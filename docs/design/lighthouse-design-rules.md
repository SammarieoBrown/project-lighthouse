# Lighthouse: Design Rules

Status: **binding** on every pixel in `apps/console` and the public portal, from the first commit.
Applies to code written by hand and code written by an agent. There is no difference.

---

## Why this document exists

Two reasons, and the second one is the real one.

**One: taste.** Generative tools converge. Every model learned from the same Dribbble shots, the same Tailwind tutorials, the same shadcn starter. Left alone they produce a look that is now recognisable on sight — a Reddit corpus of ~47k on-topic posts found "they all look the same" and "screams AI" each in roughly 13% of design discussions, ahead of any single visual feature. We are demoing to judges who have seen forty of those this year. Looking like the other thirty-nine costs us.

**Two: correctness.** This is an emergency operations console. Almost every AI-default styling habit is, in this domain, a *lie about data*. A decorative pulse means "a human must act now." A counter that animates upward means "these values are arriving." A confidence ring showing 94% with no signals means "trust this." A gradient across a severity scale means "severity is continuous." None of those are true by default, and shipping them makes the interface assert things the system does not know. In a product whose entire argument is *agents propose, humans dispose, the ledger remembers*, an interface that decorates is an interface that lies.

So: the anti-slop rules below are not a style preference. They are the visual half of the same discipline as the hash chain.

---

## The rule, in one line

**Every visual property must be traceable to something true about the data, or to a locked token. Nothing is on screen because it looked nice.**

If you cannot name what a colour, a border, a motion, or a piece of chrome *encodes*, delete it.

---

## Part 1 — Banned by default

Ranked roughly by how strongly each one reads as machine-generated. Anything here needs an explicit written exception in this file, signed with a reason, before it ships.

### Layout and structure

1. **The canned page skeleton.** Hero → three feature cards → logo strip → pricing → FAQ → footer. Not on the public portal, not on the donation page, not anywhere.
2. **Centred hero with an oversized full-sentence headline.** Especially over a gradient.
3. **A row of three cards** with rounded corners, soft shadow, and a thin-line icon on top. This is the single most-reproduced AI layout in existence.
4. **Nested cards.** A card inside a card inside a card. Pick one level of containment.
5. **Bento grids** used as a layout default rather than because the content has genuinely unequal weights.
6. **Monotonous spacing.** Every gap the same value. Rhythm is information; flat spacing says nothing has priority.

### Colour

7. **Purple/violet → blue gradients.** In any form, at any opacity. Also indigo-500 as a primary.
8. **Gradient text** in headings.
9. **Neon glow** on anything that is not literally emitting light in the data model.
10. **Cyan-on-near-black** as the accent formula.
11. **Aurora / mesh / radial-halo backgrounds.**
12. **Gradients across a discrete scale.** Severity, posture, and confidence bands are categorical or banded. A continuous ramp over them is a factual error. (One legitimate exception exists: a genuinely continuous quantity like a progress bar's fill.)

### Surfaces and ornament

13. **Glassmorphism.** Frosted panels, backdrop blur as decoration.
14. **A thick coloured border on one side of a rounded card.** The most-cited single tell. The prototype uses left-border colouring on feed entries — see §Known violations.
15. **Extreme border-radius.** No blobs. Radius is a token with two values, not a vibe.
16. **Decorative grid or dot-matrix backgrounds.**
17. **Drop shadows used to imply depth that carries no meaning.** Elevation must encode layering (modal over page), never "this is important."

### Typography

18. **Inter as the display face.** Also: Inter as the only face. Also, honestly, Inter.
19. **A single family doing every job.** Display, body, and numeric/data are three roles and need at least two families.
20. **Oversized italic serif hero headline.** The 2025–26 default.
21. **Flat hierarchy** — three sizes within 4px of each other doing four different jobs.
22. **Tracked-out uppercase eyebrow labels above every heading.** Used once with intent: fine. Used as connective tissue: slop.

### Motion

23. **Decorative pulse.** Banned outright — see §Part 3, rule M1.
24. **Count-up number animations.** The T2R counter does **not** tick up on load. It is a measurement, not a slot machine.
25. **Auto-scrolling marquees, bouncing/elastic easing, hover scale-transforms on images, fake blinking cursors on non-editable text.**
26. **Scroll-triggered reveal on everything.** An orchestrated moment can earn its place; twelve independent fade-ups cannot.

### Iconography

27. **Emoji as icons.** Non-negotiable and not only for aesthetic reasons. A 🏠 next to a collapsed house, a 🌊 next to a flood death, a 💰 on a relief disbursement — this is a product about people who have lost their homes. Emoji in that context is not casual, it is contemptuous.
28. **Generic thin-line icon sets** that could illustrate any product. If an icon is not more legible than its label, ship the label.

### Framework defaults

29. **shadcn/ui or a Tailwind component library used unmodified.** Using the primitives is fine — using them with default tokens is the leading tell in the corpus. If we adopt any component library, its theme layer is fully overwritten before the second component lands. As of Aug 2 there is no Tailwind and no component library: the substrate is plain custom properties, which is the version of this rule with nothing to get wrong. Adopting Tailwind later is allowed only with `--color-*` reset and our tokens mapped in.
30. **A ground nobody chose.** What is banned is a mode that appears because a framework shipped one — and a theme toggle on an operational screen, which asks the user to decide something the product already knows.

    **Revised Aug 30, signed.** The original decision split the grounds: dark console for a dim EOC room, light portal for daylight. Reviewed against the built screens, the split lost: the dark console read as a prototype rather than an instrument, and the product changed character at every navigation between console and portal. The revised decision is **one ground, the warm bone light ground, on every operational and public surface.** The projector argument was real but conceded to consistency — and to the observation that an EOC in the Caribbean is lit for most of a storm's approach. The dark ground stays defined in `tokens.css` because the specimen sheet must keep both checkable, and because a future surface may earn it back with a written reason here. The ban on a viewer-facing theme toggle on operational screens stands unchanged.

One radius exception is committed for rule 15, signed Aug 4. The two radii move
from 2px/4px to **8px/12px**, applied to controls and surfaces alike across the
console and the portal.

The reason is not that the corners looked nice. It is that at 2px a button was
not distinguishable at a glance from the ruled panels around it: the console's
whole substrate is hairline rules and square edges, so a control sharing that
edge treatment reads as another panel rather than as something to press. On a
screen whose argument is that a human disposes, the thing a human presses has to
look pressable from across a room.

What the exception does **not** license, and these are the parts that keep rule
15 doing its job: there are still exactly **two** values, they still live in
`tokens.css`, and no component may introduce a third. No pill, no `999px`, no
per-component radius, no blob. A radius that is not one of the two named tokens
is still a bug, and the rule's original target — six ad-hoc values in one
prototype — is still banned outright.

One narrow colour exception is committed for Register I: `--lh-structure` is a
cyan-blue mark only in the explicit **Structures** map view and its key. It is
not a product accent. It encodes mapped public-source inventory that was
illegible as grey-on-grey; the warm meaning hues already encode impact, while
the muted blue outlines encode forecast wind thresholds. It carries no glow,
chrome, status, or action affordance and must not leave that map layer.

One narrow colour exception is also committed for the simulator's modelled
radar-style precipitation. `--lh-weather-*` is a discrete light-to-extreme
sequence used only to separate modelled precipitation intensity. It is
qualitative rather than calibrated rainfall, cannot carry impact or operational
status, and must stay beside copy that says **MODELLED PRECIPITATION + WIND ·
NOT OBSERVED**. When an observed GOES frame is available it replaces the
modelled precipitation field. The sequence must not leave
`apps/console/app/simulator/` or become product chrome.

---

## Part 2 — The positive direction

Blocking the defaults is only half the job. A blocklist with no locked direction produces the *average of everything that is left*, which is its own kind of slop.

**Status: decided and committed (Aug 2).** The direction is below; the values live in [`apps/console/app/tokens.css`](../../apps/console/app/tokens.css), with the reason for each written beside it. A value in that file without a reason is a bug in the file.

### What "locked" means

A committed token file — colour, type, spacing, radius, motion — and prose in this document saying **why each value is what it is, in terms of this subject**. Not "modern and clean." Something a person could disagree with.

Required contents, all now present in `tokens.css`:

- **Type.** Three roles, three families. **Archivo** for display — a grotesque with a real width axis and signage lineage, because Register II has almost no colour to work with and hierarchy has to be carried by weight and width instead. **Public Sans** for body — the US Web Design System face, built for government information read by the public at small sizes; a civic text face rather than a brand face. **IBM Plex Mono** for data — every number here is positional, and tabular figures are mandatory on every figure that can change.
- **Ground.** Five neutrals per ground, and two grounds. The dark is a faint green-grey (instrument enamel, deliberately not midnight blue, which is where every generated dashboard lands); the light is warm bone rather than white, so a screen of ruled data does not glare. Neither is a tint of the other.
- **Meaning.** Four hues for the entire product, named for what they mean rather than what they look like: `critical`, `elevated`, `watch`, `confirmed`. Quiet has no hue — absence is the state. Each meaning carries two values, a mark tier at 3:1 and a text tier at 4.5:1, because a filled swatch and a line of type are held to different floors; collapsing them on the light ground either fails the floor or turns amber into brown.
- **Spacing and radius.** A 4px base with two tighter steps, since rule C6 puts console density ahead of comfort. Two radii, 2px and 4px — rounded cards are the most reproduced AI shape there is, and zero radius everywhere is the broadsheet default, which is the same mistake in a different coat.
- **Motion budget.** Two durations, one ease-out curve, no overshoot. The complete list of things allowed to move is rule M1 and nothing has been added to it.
- **The signature.** One element this console is remembered by. One. Everything around it stays quiet. Decided — see below.
- **The mark.** See below.

### The direction: three registers, one substrate

**Decided (Aug 2).** Lighthouse uses all three of the directions that were on the table — but as an *assignment*, not a blend. Each maps to a surface with a genuinely different job, a different reader, and a different reading speed. Mixing them on one screen would be incoherence; assigning one per surface means each surface looks like the thing it actually is.

This is the harder version of the decision, not the softer one. It only works if the guard rules below are obeyed literally.

**Register I — Synoptic chart. Owns: the map and every hazard layer.**
The visual language of the data we genuinely consume — NHC advisories, the 34/50/64kt wind probability product, wind field radii, forecast cone, watches and warnings. Hairline contours, printed-chart colour coding inherited from the source products rather than invented, heavy monospace numerics. We borrow this wholesale and deliberately, because an ODPEM officer or a meteorologist already reads it fluently and there is nothing to gain from teaching them our version. Reader: an operator scanning. Guard against the broadsheet default: every contour on our map carries a real value from a real product, or it is not drawn.

**Register II — Civil defence signage. Owns: console chrome, posture, alerts, triage queue, verification review, approval gates.**
ODPEM and Jamaican road-signage vernacular. Very high contrast, a deliberately tiny colour vocabulary, blunt heavy type, zero ornament, legible across a room and through a projector. This is the surface read under stress by someone tracking many things at once, and it is where the density rule (C6) bites hardest. Reader: an operator deciding. Guard against flatness: the type work carries the whole register here, so weight and size contrast must be doing real hierarchical work.

**Register III — The register. Owns: the ledger, the audit trail, the public portal, the donor journey.**
Ruled lines, real sequence numbering driven by the hash chain, the feel of a log that has been kept properly. Numbering is legitimate here and nowhere else, because the chain genuinely *is* an ordered sequence and its order carries information the reader needs — which is exactly the test the rest of the document applies to structural devices. Reader: an auditor or a donor, reading slowly and wanting to be convinced. Guard against skeuomorphism: typographic only. Ruled lines, not paper texture; sequence numbers, not a ledger illustration.

### The shared substrate (non-negotiable — this is what makes it one product)

Three registers is a coherent system only if everything underneath them is single. These do not vary by surface:

- **One type system.** The same three families in the same three roles everywhere. The registers differ in how they *set* type — weight, size, rule, case — never in what type they use.
- **One spacing scale and two radii.** Shared across all surfaces without exception.
- **One semantic colour vocabulary.** Posture and severity hues are defined once and obeyed identically by all three registers, under rule C1. This is the connective tissue: a hue that means `URGENT` on the map means `URGENT` on the queue and on the ledger, and means nothing else anywhere.
- **One motion budget.** Rule M1 applies unchanged across all three.

### Guard rules for the mix

1. **Exactly one register dominates per surface.** Never two on a screen. If a panel needs an element from another register, the *panel* is the boundary — a synoptic-coded element does not appear inside a register-coded panel, it appears in its own panel.
2. **A register is a way of setting the shared substrate, not a licence for new tokens.** No register may introduce a colour, family, radius, or spacing value that is not already in the substrate.
3. **When in doubt about which register owns a surface, ask who reads it and how fast.** Scanning → I. Deciding under stress → II. Being convinced, slowly → III.
4. **If the three ever need to co-exist on one screen, the design is wrong** and the screen should be split, not blended.

### The signature

One element, and it is the product thesis made visible: **the transition line.**

Every state change renders as a single ruled entry carrying three things — which agent proposed it, which human disposed of it (a signature, or explicitly none), and its position in the chain. The same object appears at three levels of detail: compressed in the operator's live feed, full in the audit trail, and narrated in the donor journey. It is the one element that crosses all three registers unchanged, which is what makes them read as one system.

It works because it is not decoration wearing a concept — it is a direct rendering of *agents propose, humans dispose, the ledger remembers*, and it is the sentence the whole platform is arguing. It also survives the deletion test by construction: remove it and the argument goes with it.

**To be proven, not assumed.** If the transition line does not hold up once it is built against real replay data, it gets replaced rather than defended.

### The mark

A lighthouse tower is a stack of horizontal bands — the painted daymark that makes it identifiable from sea in daylight, before the light is any use to anyone. A ledger is also a stack of horizontal bands. The mark is built on that coincidence: the tower *is* a stack of recorded entries, tapering as it rises, standing on a base wider than itself, drawn in the same ruled-line language as Register III and the transition line.

Rules it obeys, which are the same rules everything else obeys:

- **Nothing but rectangles.** No gradient, no glow, no rounded blob, no thin-line illustration.
- **All `currentColor`.** One asset serves both grounds. There is no dark variant to keep in sync and no second file to forget.
- **The beam is thrown, never swept.** A mark that flashed at you from a toolbar would break rule M1 on its first frame, and the whole argument of that rule is that movement in this product is a summons.
- **It degrades on purpose.** Below roughly 20px the beam comes off, because two sub-pixel bars turn to mush and a mark that is illegible at favicon size is a mark that only works in a presentation.
- **The gallery is load-bearing.** The overhanging walkway under the lantern is the single feature that stops a tapered stack from reading as a cone or a Christmas tree. It was added after looking at the first version, which read as a traffic cone.

Implementation: [`apps/console/app/logo.tsx`](../../apps/console/app/logo.tsx), favicon build in `app/icon.svg`.

---

## Part 3 — Rules specific to this product

These are the ones that do not appear in any general anti-slop guide, because they come from the domain.

**M1 — Motion means state changed.** In an operations room, movement is a summons. Animation is reserved for exactly three triggers, and any other moving pixel is a bug:
  1. Posture escalation (Quiet → Watch → Ready → Act).
  2. A human gate that is open and unactioned — an approval or a signature the system is blocked on.
  3. A live write landing (a claim arriving, a disbursement confirming).
Anything decorative that pulses makes static status look live, which is the exact failure mode that gets a real alert ignored.

A second motion exception is committed for the **EOC replay map's circulation
marks**, signed Aug 4. Short flow marks over the advisory's published wind
bands rotate continuously to show the modelled surface circulation of the
selected advisory.

The argument is M1's own thesis rather than an exemption from it. Motion means
state changed, and a cyclone's defining state is that it is turning. The three
threshold polygons are the evidence and they are not smoothed or replaced — but
an outline can only say how far a wind reaches, never which way it blows, and
an operator reading a wind field needs both. The marks supply the second half.

Scope and containment, which is what makes this bounded rather than a crack in
the rule: the exception covers the `lh-flow` source in
`apps/console/app/eoc/map/` and nothing else. The marks are drawn in the
existing hazard ramp and introduce no colour; they carry no status, no chrome
and no affordance; they are never a summons and never encode posture, severity
or a human gate. Their parameters derive only from the selected advisory —
its stated centre and intensity, its own forecast heading, the speed measured
between its own frames, and the 34 kt radius read off its own published
polygon. They must never acquire pulsing, easing, decorative turbulence, or
motion outside the map canvas, and no *second* moving element may be added to
this screen under cover of this paragraph.

Motion stops for a hidden document and for `prefers-reduced-motion`, which is
read in JS because the CSS token block only shrinks durations and cannot reach
a source update. Under reduced motion the marks still render — the circulation
is evidence, not ornament — they simply hold still.

What is explicitly **not** carried over from the simulator: its modelled
precipitation field. An advisory publishes no rainfall, so drawing one here
would claim more than the source supports, which is the exact failure the
blunt stepped polygons exist to avoid. `--lh-weather-*` stays in the simulator.

One narrowly bounded motion exception is committed for the **storm simulation
surface**. Modelled precipitation may evolve and wind particles may advect
continuously at the selected simulation hour, even while the timeline is
paused. That motion encodes the steady circulation, direction and relative
speed of the selected model field; it does **not** advance the selected hour or
the impact calculation. The layer is qualitative model output, never observed
radar or wind, and the surface must name it as such. It must stop when the
surface is hidden or when `prefers-reduced-motion` is set. The exception must
never acquire pulsing, easing, decorative turbulence, status colour,
attention-seeking chrome, or motion outside the map canvas. Its containment
boundary is `apps/console/app/simulator/`; it does not relax M1 anywhere else.

**C1 — Colour is a controlled vocabulary, not a palette.** Posture and severity own specific hues. Once a hue is assigned to `URGENT`, nothing else on any screen may use that hue for any reason — not a button, not a link, not a chart series, not a hover state. The whole value of semantic colour collapses the first time it is borrowed for decoration.

**C2 — Confidence never appears without its signals.** No lone percentage, no donut, no meter standing alone. Verification confidence renders only alongside the five signals that produced it, each individually scored. This is the screen that proves human-in-the-loop to a judge; a single number is precisely the thing we are arguing against.

**C3 — No fabricated precision.** Render the precision the data supports and no more. T2R reads in hours because that is what the replay measures. A number that implies a decimal place the pipeline cannot defend is a lie with a clean font.

**C4 — Staleness is a visual state with a design, not an afterthought.** The console is offline-first because the EOC loses power and internet in exactly the conditions we exist for. So every data surface carries an "as of" and a sync state, and stale data must be *unmistakably* distinct from live data — not merely dimmed, since dimming already means disabled. Design this once, apply it everywhere.

**C5 — The public portal names nobody.** Aggregate only: no household-resolution map dots, no avatars, no faces, no names, no photographs of beneficiaries. Same posture as the Director-only anticipatory list. A design that makes a victim into a testimonial card has lost the argument the product is making.

**C6 — Density over whitespace.** The EOC console is read at distance, under stress, sometimes on a projector, by someone tracking many things at once. Generous padding and large cards — the AI default — actively hurt here. Tabular density, small consistent gutters, high contrast. The public portal may breathe; the console may not.

**C7 — Numbers are tabular everywhere.** `font-variant-numeric: tabular-nums` on every figure that can change. Values that jitter horizontally as they update are unreadable in a live feed.

**C8 — Polish must be separable.** "Public portal styling" is item 1 on the cut list. That cut has to be *possible*: keep visual polish in a layer that can be removed without touching data rendering. If losing the polish breaks the numbers, the cut list is fiction.

**C9 — Quality floor, unannounced.** Responsive to mobile, visible keyboard focus, `prefers-reduced-motion` respected, contrast checked against the actual palette. Voice-first parity (NFR-L-02) means every household-facing flow must also work without reading — which is a design constraint on the portal, not just on WhatsApp.

---

## Part 4 — Copy

Words are design material. They give a design away as fast as a gradient does.

- **No em-dash overuse**, no "streamline," "empower," "seamless," "leverage," "unlock," "revolutionise."
- **No aphorisms.** No sentence that sounds like it wants to be on a poster.
- **Name things by what the user controls**, not by how the system is built. A Clerk *reviews a claim*; they do not *inspect a verification payload*.
- **An action keeps its name through the whole flow.** The button that says "Approve allocation" produces "Allocation approved." Not "Success!"
- **Errors state what happened and what to do.** They do not apologise and they are never vague.
- **Empty states are instructions**, not moods. "No claims awaiting review" is a status; say what to do next if there is something to do.
- **Sentence case. Active voice. One job per element** — a label labels, an example demonstrates, nothing quietly does both.

---

## Part 5 — Enforcement

A rule nobody checks is a preference.

1. **This file is loaded before any UI work.** It is referenced from the repo's agent instructions, so it is in context by default rather than by memory.
2. **Pre-merge checklist** on any PR touching `apps/console` or portal code:
   - [ ] Every colour used resolves to a locked token. No literal hex in a component.
   - [ ] Every moving element maps to M1 trigger 1, 2, or 3.
   - [ ] Nothing from Part 1 is present, or an exception is written into this file with a reason.
   - [ ] Numbers are tabular; precision matches what the data supports.
   - [ ] Reduced-motion and keyboard focus verified, not assumed.
3. **The screenshot test.** Put a screen next to three AI-generated dashboards. If a stranger cannot pick ours out in two seconds, it is not done.
4. **The deletion test.** Before shipping a screen, remove one element. If nothing is lost, it stays removed.

---

## Known violations in the existing prototype

`docs/prototype/lighthouse-prototype.html` is a *functional* prototype — it exists to prove the demo sequence, and it did its job. It is explicitly **not** the design direction, and it breaks these rules in ways worth naming so they are not copied forward:

- **Dark navy + single amber accent** is close to AI default #2 (near-black plus one bright accent). It needs a real justification or a real replacement.
- **`animation: pulse` on the ACT posture chip and on the approval button.** The posture chip arguably survives under M1 trigger 1. The approve button is decoration on a control and does not.
- **Coloured left-borders on feed entries** — Part 1 rule 14, the most-cited single tell.
- **System font stack** doing display, body, and numeric work at once — Part 1 rule 19.
- **`border-radius: 20px` chips** alongside 8px, 9px, 10px, 12px and 26px radii — no scale, six ad-hoc values.
- **Amber used for posture, the primary button, the brand mark, the T2R highlight, and the claim card** — Part 3 rule C1 violated five ways.

None of this is a criticism of the prototype's purpose. It is the list of things the real console must not inherit.

---

## Sources

- [Reddit-mined ranking of vibe-coded design tells](https://github.com/JCarterJohnson/vibecoded-design-tells) — ~47k on-topic posts, 47 subreddits; the ranked tells and the "they all look the same" finding.
- [Impeccable — Slop](https://impeccable.style/slop/) — 64 catalogued patterns across visual, type, layout, motion, and copy.
- [Why every AI-built website looks the same](https://dev.to/alanwest/why-every-ai-built-website-looks-the-same-blame-tailwinds-indigo-500-3h2p) — framework-default convergence.
- [AI slop fonts and gradients](https://www.925studios.co/blog/ai-slop-design-tells) — typography and colour tells.
- [Avoiding AI slop with a design-system approach](https://www.mindstudio.ai/blog/claude-design-avoid-ai-slop-design-system) — the lock-your-tokens argument for why a blocklist alone is insufficient.
- [AI-generated UI anti-patterns guide](https://docs.bswen.com/blog/2026-03-20-ai-generated-ui-anti-patterns/) — layout-level rather than component-level genericism.
