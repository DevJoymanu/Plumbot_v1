---
name: Aetheris Clinical
colors:
  surface: '#f8f9ff'
  surface-dim: '#cbdbf5'
  surface-bright: '#f8f9ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#eff4ff'
  surface-container: '#e5eeff'
  surface-container-high: '#dce9ff'
  surface-container-highest: '#d3e4fe'
  on-surface: '#0b1c30'
  on-surface-variant: '#3e4850'
  inverse-surface: '#213145'
  inverse-on-surface: '#eaf1ff'
  outline: '#6e7881'
  outline-variant: '#bec8d2'
  surface-tint: '#006591'
  primary: '#006591'
  on-primary: '#ffffff'
  primary-container: '#0ea5e9'
  on-primary-container: '#003751'
  inverse-primary: '#89ceff'
  secondary: '#4648d4'
  on-secondary: '#ffffff'
  secondary-container: '#6063ee'
  on-secondary-container: '#fffbff'
  tertiary: '#576065'
  on-tertiary: '#ffffff'
  tertiary-container: '#949da3'
  on-tertiary-container: '#2c3539'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#c9e6ff'
  primary-fixed-dim: '#89ceff'
  on-primary-fixed: '#001e2f'
  on-primary-fixed-variant: '#004c6e'
  secondary-fixed: '#e1e0ff'
  secondary-fixed-dim: '#c0c1ff'
  on-secondary-fixed: '#07006c'
  on-secondary-fixed-variant: '#2f2ebe'
  tertiary-fixed: '#dbe4ea'
  tertiary-fixed-dim: '#bfc8ce'
  on-tertiary-fixed: '#141d21'
  on-tertiary-fixed-variant: '#3f484d'
  background: '#f8f9ff'
  on-background: '#0b1c30'
  surface-variant: '#d3e4fe'
typography:
  display-lg:
    fontFamily: Inter
    fontSize: 48px
    fontWeight: '300'
    lineHeight: 56px
    letterSpacing: 0.02em
  headline-lg:
    fontFamily: Inter
    fontSize: 32px
    fontWeight: '400'
    lineHeight: 40px
    letterSpacing: 0.01em
  headline-lg-mobile:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '400'
    lineHeight: 32px
    letterSpacing: 0.01em
  headline-md:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '400'
    lineHeight: 32px
    letterSpacing: 0.01em
  body-lg:
    fontFamily: Inter
    fontSize: 18px
    fontWeight: '300'
    lineHeight: 28px
    letterSpacing: 0.01em
  body-md:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '300'
    lineHeight: 24px
    letterSpacing: 0px
  label-md:
    fontFamily: Geist
    fontSize: 14px
    fontWeight: '500'
    lineHeight: 20px
    letterSpacing: 0.05em
  label-sm:
    fontFamily: Geist
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
    letterSpacing: 0.05em
rounded:
  sm: 0.5rem
  DEFAULT: 1rem
  md: 1.5rem
  lg: 2rem
  xl: 3rem
  full: 9999px
spacing:
  base: 8px
  section-gap: 64px
  element-gap: 24px
  container-padding: 32px
  max-width-desktop: 1440px
---

## Brand & Style
The design system embodies a futuristic, medical-grade minimalism that merges high-tech precision with a premium, airy aesthetic. It moves away from the rigid structures of traditional clinical software toward a "Sci-Fi Medical" interface — one that feels advanced yet approachable.

The visual language is rooted in **Glassmorphism**, utilizing layered translucency, subtle backdrop blurs, and ethereal depth to create a sense of weightlessness. The target experience is one of absolute clarity and high-end sophistication, evoking the feeling of a glass-based diagnostic interface found in a near-future medical suite.

## Colors
The palette is centered around an "Electric Clinical" primary blue, providing a vibrant, high-energy focal point against a backdrop of pristine, clinical whites and soft, translucent grays.

- **Primary (#0ea5e9):** A high-vibrancy cyan-blue used for critical actions, active states, and data highlights.
- **Secondary (#6366f1):** An indigo-tinted secondary for subtle accents and depth within data visualizations.
- **Backgrounds:** Primarily ultra-light tints (#F8FAFC) and pure white to maintain the minimalist, airy feel.
- **Glass Surfaces:** Semi-transparent white layers (80-90% opacity) with a heavy background blur (20px+) to create the signature glassmorphic depth.

## Typography
The typographic system relies on **Inter** for its technical clarity and **Geist** for labels to inject a developer-grade, precise feel.

Headlines utilize lighter weights (300-400) and increased letter spacing to create an "airy" and expansive feeling. Body text is strictly set in Light (300) weight for a sleek, modern look that avoids the visual "clutter" of heavier strokes. To ensure legibility, line heights are generous, and tracking is slightly opened across all display and body roles.

## Layout & Spacing
This design system employs a fluid, open-layout philosophy that prioritizes whitespace as a functional element. Instead of rigid containers, spacing is used to group related information.

- **Grid:** A 12-column fluid grid with wide 32px gutters to prevent information density fatigue.
- **Margins:** Large outer margins (up to 80px on desktop) focus the eye toward the center of the clinical data.
- **Hierarchy:** Use vertical rhythm (8px increments) rather than dividers. A minimum of 64px spacing is required between major content sections to maintain the minimalist aesthetic.

## Elevation & Depth
Depth is the primary communicator of hierarchy, replacing borders and solid fills.

1.  **Surfaces:** Use "Glass" surfaces — white containers with 85% opacity and a `backdrop-filter: blur(24px)`.
2.  **Borders:** Use ultra-thin (1px) borders with very low opacity (10-15%) in the primary or neutral color to catch the light on glass edges.
3.  **Shadows:** Use large, highly diffused "Ambient" shadows. These should have a wide spread (40px+) and low opacity (5-8%) with a subtle blue tint (#0ea5e9) to simulate glowing light reflecting off clinical surfaces.
4.  **Layers:** Higher elevation levels are indicated by increased translucency and more pronounced background blurs rather than darker shadows.

## Shapes
The shape language is organic and highly rounded to soften the technical nature of the interface.

Interactive elements like buttons, chips, and input fields utilize a **pill-shaped** (Full) corner radius. Cards and larger containers use a significant `rounded-xl` (24px+) radius. This lack of sharp corners creates a "liquid" feel that aligns with modern, high-tech hardware aesthetics.

## Components

- **Buttons:** Primary buttons are pill-shaped with a vibrant #0ea5e9 gradient or solid fill. Secondary buttons should be "Ghost-Glass" with a 1px light border and backdrop blur.
- **Chips & Tags:** Small, fully rounded pill shapes. Use light tints of the primary color with 10% opacity for the background and high-contrast text.
- **Inputs:** Floating labels with no bottom border or heavy box. Use a subtle, translucent pill-shaped background that becomes slightly more opaque on focus.
- **Cards:** No heavy borders. Use the glassmorphic surface (blur + transparency) and a soft ambient shadow.
- **Lists:** Define list items through vertical spacing and subtle typography shifts. Avoid horizontal dividers where possible; use "hover-glass" states to indicate interactivity.
- **Data Visualizations:** Use "Neon" glow effects on line charts and progress bars. Lines should be thin but vibrant, utilizing the primary electric cyan.
