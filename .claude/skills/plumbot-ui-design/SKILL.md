---
name: plumbot-ui-design
description: The "Aetheris Clinical" front-end design system for Plumbot's admin UI — glassmorphic, Material-3-token-based, electric clinical blue. Use this skill BEFORE writing or editing ANY user interface in this repo — dashboard pages, Django templates under bot/templates/, new admin screens, HTML mockups/wireframes, artifacts that preview UI, emails with layout, or CSS/Tailwind styling of any kind. Also use it when the user says "make it look like the wireframe", mentions glassmorphism, the command center, bento grids, design tokens, or asks for any new page, panel, card, chart, or component. Do not invent a different aesthetic — every screen must read as this one system.
---

# Plumbot UI Design — "Aetheris Clinical"

Futuristic, medical-grade minimalism: layered glass, airy light typography,
one vibrant clinical blue doing all the talking against near-white tints.
Advanced but approachable — a glass diagnostic interface, not a spreadsheet.

**Reference files (read before building):**
- `references/design-system.md` — the full token set (colors, type scale,
  radii, spacing) as YAML frontmatter plus the design rationale. Tokens are
  law; do not invent hex values, sizes, or radii outside it.
- `references/wireframe-command-center.html` — the canonical implementation
  (the approved dashboard wireframe). Copy its Tailwind config block, glass
  CSS, and component patterns verbatim rather than re-deriving them. Open it
  in a browser when you need to see the target.

For general design craft (avoiding templated defaults, typography judgment),
also load the `frontend-design` skill; for any chart or stat visualization,
load `dataviz` — but this system's tokens and mark styling override generic
palettes.

## The five moves that make it this system

If a screen doesn't have these, it isn't Aetheris Clinical:

1. **Glass surfaces, not solid cards.** Every container is
   `background: rgba(248,249,255,0.85); backdrop-filter: blur(24px);
   border: 1px solid rgba(0,101,145,0.1)` (the `.glass-surface` class) with a
   wide, blue-tinted ambient glow: `box-shadow: 0 20px 40px -15px
   rgba(14,165,233,0.08)` (`.command-glow`). Depth = more translucency and
   blur at higher elevation, never darker shadows or heavier borders.
2. **One electric blue.** `primary #006591` for text/actions,
   `primary-container #0ea5e9` for vibrant fills, glows, and data highlights;
   `secondary #4648d4` (indigo) only for secondary data accents; `error
   #ba1a1a` reserved for genuine urgency (hot leads, alerts). Everything else
   is the surface-container tint ladder (`#ffffff → #d3e4fe`). Always pair a
   color with its `on-*` counterpart for text — that's what keeps contrast safe.
3. **Airy type with a technical voice.** Inter Light (300) for display and
   body, Inter 400 for headlines, and **Geist 500, uppercase, wide tracking
   (0.05em) for every label/section header**. The big number in a stat card is
   `display-lg` (48px/300). Use the scale in the tokens file only — no ad-hoc
   sizes.
4. **Liquid shapes.** Interactive elements (buttons, chips, inputs, badges)
   are full pills. Cards are 16–24px radius (`rounded-2xl` in the wireframe).
   Nothing square, nothing sharp.
5. **Whitespace instead of lines.** 8px rhythm; 24px between elements, 32px
   container padding, 64px between major sections; 12-column grid, 24px
   gutters, 1440px max width. Group by spacing and label typography — avoid
   horizontal dividers; where a separator is unavoidable use `primary/5–10`
   hairlines.

## Component recipes (from the wireframe — reuse, don't reinvent)

- **Shell:** fixed 288px (`w-72`) glass sidebar + sticky 80px glass top bar,
  both `bg-surface/85 backdrop-blur-3xl` with `border-primary/10` edges and a
  long soft blue shadow. Active nav item: `text-primary font-bold bg-primary/5
  rounded-r-full`; hover: `bg-primary/10`. Count badges are tiny primary pills.
- **Primary button:** pill, solid primary, `shadow-lg shadow-primary/20`,
  icon + Geist label, `hover:opacity-90 active:scale-95`. Secondary buttons
  are ghost-glass: transparent with a 1px `primary/10` border that brightens
  to `primary/40` on hover.
- **Stat card (bento metric):** glass card `h-40`, three rows — uppercase
  Geist label + trailing icon; `display-lg` number with a small context note
  beside the baseline; a 4px (`h-1`) rounded progress bar or thin 1px-stroke
  sparkline. Urgent variant: `border-error/20`, an `error/5` wash overlay,
  filled icon, segmented error bars.
- **List/table panels:** glass card, `px-6 py-4` header with an uppercase
  Geist title (optionally a 2px status dot), `divide-primary/5` rows,
  hover rows brighten toward `surface-container-lowest/60` with a chevron that
  nudges right (`group-hover:translate-x-1`).
- **Empty states:** centered oversized Material Symbol + one `body-md` line at
  40% opacity inside a `border-2 border-dashed border-primary/5 rounded-xl`
  well. Never a bare "No data".
- **Alert strip:** `bg-error-container/20 border-error/10 rounded-2xl`, filled
  error icon, bold uppercase Geist message, underlined action link pushed
  right. May pulse — urgency is the ONE place animation gets loud.
- **Inputs:** pill-shaped, borderless, on a `surface-container-low` tint that
  firms up on focus (`focus:ring-2 ring-primary/20`); icon inset left.
- **Quick actions:** 2-column grid of square-ish tiles — icon over Geist
  label, 1px primary/10 border, icon scales up on hover, `active:scale-95`.
- **Charts/progress:** thin (1px stroke) vibrant lines with neon glow
  (`shadow-[0_0_15px_...]` on markers), 4px bars in primary/secondary on
  `surface-container` tracks. Animate line draw-in with the dash-offset
  keyframe from the wireframe.
- **Icons:** Material Symbols Outlined, weight 300, unfilled — `FILL 1` only
  for urgency/active emphasis.
- **Atmosphere:** one or two huge (`w-96`) primary/secondary 5%-opacity blurred
  circles fixed behind the canvas (`-z-10 pointer-events-none`).
- **Motion defaults:** 200–300ms ease transitions on hover/press only;
  `active:scale-95` on anything clickable; staggered width transitions when
  metrics load. Calm by default — `animate-pulse`/`animate-ping` are reserved
  for live alerts.

## Fitting it into this repo

Two front-end worlds exist; pick deliberately:

- **New standalone pages, mockups, artifacts:** copy the `<head>` of the
  wireframe wholesale — Google Fonts (Inter 300–900 + Geist), Material
  Symbols, Tailwind CDN, and the `tailwind.config` token block, plus the
  `.glass-surface` / `.command-glow` styles. That config IS the design system
  in code; don't re-type tokens by hand.
- **Existing panel templates** (`bot/templates/**`) run Bootstrap 5 + the CSS
  variables in `bot/templates/bot/includes/plumbot_variables_css.html` with
  `pb-*` classes. Do NOT bolt Tailwind onto those pages or mix icon sets
  (they use Font Awesome). To move an existing page toward this system,
  restyle through the variables file — map `--color-header → primary #006591`,
  `--color-blue-mid` is already `#0ea5e9`, raise radii toward the liquid
  scale, swap the shadow set for the blue-tinted ambient glow, and add the
  glass treatment as `pb-` utility classes. A full migration of the shared
  layout is its own task — propose it, don't smuggle it into a page fix.
- No build tooling, npm, or new packages — CDN links only, consistent with
  the repo's no-new-dependencies rule.

## Judgment calls the wireframe gets wrong (don't copy these)

- **Sci-fi placeholder copy.** "Schedule Telemetry", "Sector 7", "Deploy
  Response", "System Latency: 12ms", "Standby Mode" are moodboard flavor. Real
  screens use the plumbing domain's own words: appointments, leads, suburbs,
  follow-ups. The aesthetic is futuristic; the language stays Homebase's.
- **Fake data.** Never render invented metrics (processing load, latency,
  "Conversion AI: Optimal"). Every number on a real dashboard binds to a real
  queryset; if a stat doesn't exist yet, leave the card out.
- **Hotlinked avatars/images.** The original mock hotlinked a Google-hosted
  portrait; use initials-in-a-circle or a local static asset.
- **Light-weight legibility.** Inter 300 is fine at 16px+ on `on-surface`
  (#0b1c30). Below 14px, or on `on-surface-variant`, step up to 400–500 —
  never light AND small AND low-contrast. Labels already handle this (Geist
  500).
- **Dark mode isn't designed yet.** The config declares `darkMode: "class"`
  but only light tokens exist. Don't ship a theme toggle or guess dark values;
  flag it as future work.
- **`backdrop-filter` cost.** Blur is expensive on low-end hardware (much of
  the audience is on modest machines/phones). Reserve glass for the shell and
  top-level cards — don't nest blurred surfaces inside blurred surfaces — and
  give every glass element a solid-enough rgba fallback so the page still
  reads if the filter is unsupported.
- Emojis are acceptable in this internal admin UI, but prefer Material
  Symbols; the no-emoji rule applies to customer-facing WhatsApp/email copy,
  not dashboards.

## Definition of done for any UI change

Tokens only (no invented values) · glass + glow on containers · pills on
interactive elements · Geist uppercase labels · empty states designed · real
data bound · hover/active states present · spacing on the 8px rhythm ·
checked at 1440px and narrow widths · screenshot compared against the
wireframe for family resemblance. End with a suggested `git commit -m`.
