# Design rules — brand-neutral quality baseline

These rules keep generated screens looking designed rather than assembled.
They are defaults, not a brand: when the user supplies brand cues (a
screenshot, a palette, an existing system like the Plumbot repo's "Aetheris
Clinical"), those override the specifics here — but the *discipline* (one
accent, one type pairing, one radius scale, an 8px rhythm) always applies.

## Spacing & layout

- Everything sits on a 4/8px rhythm. Component padding: 12–24px. Gaps between
  siblings: 8–24px. Between sections: 48–96px. When in doubt, add more space —
  cramped is the #1 amateur tell.
- Web: 12-column grid, max content width 1120–1280px inside a 1440px canvas,
  24–32px gutters. Mobile: single column, 16–20px side padding, 390×844 frame.
- Group by whitespace and typography shifts, not boxes-inside-boxes. Max one
  level of visible container nesting; avoid borders where a background tint or
  spacing can do the job.
- Align to the grid ruthlessly. Mixed alignments on one axis read as broken.

## Typography

- Exactly one or two families per design: a workhorse (Inter, Geist, Söhne-like,
  system) and optionally a display face for hero/headlines. Load from Google
  Fonts.
- Build a real scale and stick to it — e.g. 12 / 14 / 16 / 20 / 28 / 40 / 56.
  No ad-hoc sizes between steps.
- Hierarchy comes from size + weight + color together. Body 400–500 at
  15–16px; labels/overlines 11–12px, 500–600, uppercase with 0.04–0.08em
  tracking; display sizes drop to 300–400 weight with tighter (−0.01 to
  −0.03em) tracking.
- Line-height: ~1.1–1.2 for display, 1.5–1.6 for body. Line length 45–75
  characters.
- Never use pure black text; use a near-black with a hue that matches the
  palette temperature.

## Color

- Structure: a neutral ladder (5–7 tints from background to strongest text) +
  ONE accent + a semantic set (success/warn/error) used only for meaning.
  A second accent is allowed only for data-viz or explicit brand reasons.
- Derive neutrals from the accent's temperature (a blue product gets cool
  grays, a warm brand gets warm grays) — pure #f5f5f5 grays next to a
  saturated accent look cheap.
- Every fill color has a paired text color chosen for contrast (Material-style
  on-color thinking). Body text ≥ 4.5:1 against its background; large display
  text ≥ 3:1. Placeholder/disabled text still ≥ 3:1.
- Dark UIs: never pure #000 backgrounds; raise elevation by lightening
  surfaces, not by shadows.

## Shape, depth, texture

- One radius scale per design (e.g. 8 / 12 / 16 / full) applied consistently:
  inputs and buttons share a radius; cards one step larger. Never mix sharp
  and fully-round on sibling elements.
- Shadows: large, soft, low-opacity (e.g. `0 12px 32px -12px` at 8–15%),
  optionally tinted toward the accent. No harsh `0 2px 4px rgba(0,0,0,.5)`.
  Pick shadows OR borders as the main separator, not both at full strength.
- Give the page one atmospheric touch so it doesn't feel flat: a soft radial
  tint, a subtle gradient on the hero, or a large blurred accent field behind
  content. Keep it under ~8% opacity.

## Components

- **Buttons:** primary = solid accent; secondary = ghost/outline; destructive
  = semantic red. Consistent height (40–48px), padding ≥ 16px horizontal,
  hover + active states always defined. One primary button per view region.
- **Inputs:** 44px+ tall, generous padding, visible focus ring in the accent
  (ring, not just border-color), labels always present (floating or above).
- **Cards:** padding ≥ 20px, title/meta/body zones with distinct type roles.
  Clickable cards get a hover elevation or border shift.
- **Nav:** active state must be unmistakable (fill or indicator, not just
  color). Icon + label preferred over icon-only; icon-only gets aria-label.
- **Tables/lists:** uppercase micro-labels for headers, hairline separators or
  row-hover tint instead of full grid lines, right-align numbers, tabular-nums.
- **Empty states:** designed, never blank — icon/illustration, one line of
  guidance, and the action that fills the state.
- **Stat/metric blocks:** big light-weight number, small label, one contextual
  cue (trend, target, sparkline). Don't stack more than 4 in a row.

## Content

- Realistic, domain- and locale-appropriate sample data: real-sounding names,
  plausible prices in the right currency, coherent dates (all in the same
  week/season), copy written in the product's voice. Numbers must be
  internally consistent across the screen.
- Imagery: CSS gradients, patterns, inline SVG, or initial-avatars. No
  hotlinked stock photos or images behind auth. `https://placehold.co` style
  services only as a last resort, colored to the palette.
- Icons: one set per design (Material Symbols, Font Awesome, or inline
  SVG/Lucide paths) at one weight. Never mix sets on a screen.

## Motion

- 150–300ms ease transitions on hover/press/focus only; `transform: scale`
  or elevation shifts for press feedback. Entrance animations: at most one
  subtle fade/slide on page load. Looping/pulsing animation is reserved for
  genuinely live or alerting elements. Respect `prefers-reduced-motion`.

## Self-check before delivering

Squint test: clear focal point and visual hierarchy? One accent doing the
talking? All spacing on the rhythm? Type scale respected? States (hover,
focus, active, empty) present? Sample data believable? Contrast AA? If the
screen could pass as a Dribbble shot of a real product, ship it.
