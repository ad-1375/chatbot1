---
name: Facts Assistant System
colors:
  surface: '#fbf8ff'
  surface-dim: '#d7d8f1'
  surface-bright: '#fbf8ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f3f2ff'
  surface-container: '#ececff'
  surface-container-high: '#e5e6ff'
  surface-container-highest: '#dfe1fa'
  on-surface: '#171a2c'
  on-surface-variant: '#3c4a43'
  inverse-surface: '#2c2f42'
  inverse-on-surface: '#f0efff'
  outline: '#6b7b72'
  outline-variant: '#bacac1'
  surface-tint: '#006c4f'
  primary: '#006c4f'
  on-primary: '#ffffff'
  primary-container: '#00d09c'
  on-primary-container: '#00533c'
  inverse-primary: '#2fe0aa'
  secondary: '#006c4f'
  on-secondary: '#ffffff'
  secondary-container: '#6ef7c6'
  on-secondary-container: '#007153'
  tertiary: '#934b07'
  on-tertiary: '#ffffff'
  tertiary-container: '#ffa15b'
  on-tertiary-container: '#733800'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#59fdc5'
  primary-fixed-dim: '#2fe0aa'
  on-primary-fixed: '#002116'
  on-primary-fixed-variant: '#00513b'
  secondary-fixed: '#71fac8'
  secondary-fixed-dim: '#50ddad'
  on-secondary-fixed: '#002116'
  on-secondary-fixed-variant: '#00513b'
  tertiary-fixed: '#ffdcc6'
  tertiary-fixed-dim: '#ffb785'
  on-tertiary-fixed: '#301400'
  on-tertiary-fixed-variant: '#713700'
  background: '#fbf8ff'
  on-background: '#171a2c'
  surface-variant: '#dfe1fa'
typography:
  headline-xl:
    fontFamily: Manrope
    fontSize: 32px
    fontWeight: '700'
    lineHeight: 40px
    letterSpacing: -0.02em
  headline-xl-mobile:
    fontFamily: Manrope
    fontSize: 26px
    fontWeight: '700'
    lineHeight: 34px
    letterSpacing: -0.01em
  headline-lg:
    fontFamily: Manrope
    fontSize: 24px
    fontWeight: '700'
    lineHeight: 32px
    letterSpacing: -0.015em
  headline-md:
    fontFamily: Manrope
    fontSize: 20px
    fontWeight: '600'
    lineHeight: 28px
    letterSpacing: -0.01em
  headline-sm:
    fontFamily: Manrope
    fontSize: 16px
    fontWeight: '600'
    lineHeight: 24px
  body-lg:
    fontFamily: Manrope
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  body-md:
    fontFamily: Manrope
    fontSize: 15px
    fontWeight: '400'
    lineHeight: 22.5px
  body-sm:
    fontFamily: Manrope
    fontSize: 13px
    fontWeight: '400'
    lineHeight: 19.5px
  label-lg:
    fontFamily: Manrope
    fontSize: 14px
    fontWeight: '600'
    lineHeight: 20px
  label-md:
    fontFamily: Manrope
    fontSize: 12px
    fontWeight: '600'
    lineHeight: 16px
    letterSpacing: 0.02em
  label-sm:
    fontFamily: Manrope
    fontSize: 11px
    fontWeight: '600'
    lineHeight: 14px
    letterSpacing: 0.04em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  gutter: 1rem
  gutter-desktop: 1.5rem
  margin: 1rem
  margin-desktop: 1.5rem
  space-xs: 0.25rem
  space-sm: 0.5rem
  space-md: 1rem
  space-lg: 1.5rem
  space-xl: 2rem
  space-2xl: 3rem
---

## Brand & Style

This design system delivers a focused, institutional-grade internal research workspace inspired by clean Indian retail fintech ergonomics. The interface embodies the poise of an analytical advisor: exceptionally composed, calm, structurally disciplined, and uncluttered. It trades visual excess for crisp information density, deliberate spacing, and immediate clarity.

### Aesthetics & Philosophy
- **Restrained Density:** Balances dense tabular facts, metrics, and mutual fund portfolios with breathable margins and clean rhythmic spacing.
- **Strict Monochromatic Foundation:** Interfaces rely strictly on white surfaces, cool stone page canvases, and varying weights of neutral ink text, energized solely by an authoritative signature emerald green.
- **Tactile Structuralism:** High utility is achieved through crisp 1px hair-thin divider boundaries, flat structural planes, and zero diffuse blur artifacts or glassmorphism.
- **Trust & Precision:** Evokes institutional rigor, audit-readiness, and cognitive quietude for financial analysts parsing high-volume mutual fund disclosures.

## Colors

The palette operates under absolute constraint. No supplementary accent hues (blues, purples, oranges), color transitions, or gradients are permitted. Every element draws strictly from this defined role matrix:

### Brand & Interactive Accents
- **Primary Green (`#00D09C`):** Primary action buttons, active tab indicators, selected step markers, active links, and primary interactive focus indicators.
- **Deep Green (`#00B386`):** Primary button hover/active states, inline citations, micro-labels requiring AA compliance against white, and link hover states.
- **Green Tint (`#E8FBF6`):** Light surface fills for resolved analytical states, 'Answered' query chips, and active record selections.

### Neutrals & Typography
- **Ink (`#44475B`):** Dominant dark neutral for all headers, body copy, tabular numeric values, and emphasized text.
- **Muted (`#7C7E8C`):** Secondary metadata, column descriptors, timestamps, helper labels, empty state subtitles, and form input placeholders.

### Surfaces & Boundaries
- **Surface (`#FFFFFF`):** Workcards, modal dialogs, flyouts, dropdown sheets, and sticky header bars.
- **Subtle Surface (`#F7F8FA`):** Primary application shell canvas, alternating table rows, hovered actionable rows, and inert filter chips.
- **Border (`#E6E9EB`):** Structural 1px hairline dividers, data grid segmentations, and component perimeter strokes.

## Typography

The type system is set in **Manrope** (with **Inter** as a systematic system fallback), leveraging its balanced, geometric legibility across large financial data spreads.

### Hierarchy & Typesetting Rules
- **Headings (600–700 Weight):** Scaled tight with subtle negative letter-spacing for large metrics and document titles. Color is anchored to `#44475B`.
- **Body Text (15px–16px, 1.5 Line-Height):** Optimized specifically for dense narrative reading, analyst notes, and executive summaries. Standardized to 1.5 line-height ratio for high eye tracking comfort.
- **Tabular Numerics:** For fund performance stats, AUM figures, and expense ratios, apply tabular number styling (`font-variant-numeric: tabular-nums;`) to maintain clean columnar alignments.
- **Labels & Micro-Copy:** Small tags and metadata labels use uppercase or title-case styling with increased letter spacing (`0.02em` to `0.04em`) rendered in `#7C7E8C` or `#00B386`.

## Layout & Spacing

Layout geometry follows an explicit 8px base rhythm (`0.5rem` = 8px) with a strict central frame:
- **Maximum Width:** Content frames terminate at `1120px` centered on wide viewports, eliminating extreme eye-sweep fatigue on ultra-wide desktop monitors.
- **Grid Structure:** A 12-column responsive fluid grid operating with 24px (`space-lg`) desktop gutters and 16px (`space-md`) mobile gutters.
- **Canvas Margins:** Fixed 16px lateral padding on mobile viewports scaling to 24px on desktop breakpoints.
- **Rhythm Anchors:**
  - `space-xs` (4px): Micro offsets between chips, badges, and inline symbols.
  - `space-sm` (8px): Form input inner vertical padding, button spacing, icon-label offsets.
  - `space-md` (16px): Standard interior card padding and compact vertical stack offsets.
  - `space-lg` (24px): Standard spacious card interior and separation between structural content groups.
  - `space-xl` (32px): Major panel headers and module breaks.

## Elevation & Depth

Visual hierarchy is constructed purely through planar layering and 1px hairlines without cast shadows or synthetic blurs:

- **Flat Architecture:** No ambient, directional, or dropped drop-shadows are permitted. Depth is established through intentional background contrast: `#FFFFFF` panels sit flush over the baseline `#F7F8FA` application backdrop.
- **Hairline Enclosures:** Every discrete structural element (cards, panels, input bounds, table cells) is framed by a 1px solid border in `#E6E9EB`.
- **Transient Surfaces:** Overlays, autocomplete menus, and confirmation modals share the pure `#FFFFFF` background bound by a 1px `#E6E9EB` boundary, paired with a non-diffused, semi-opaque backdrop scrim (`rgba(68, 71, 91, 0.25)`) to focus user attention without heavy shading.

## Shapes

The interface embraces clean, geometric contours that balance modern consumer software approachable styling with operational structure:

- **Containers & Cards:** Exactly `12px` border-radius (`rounded-[12px]`) to frame mutual fund fact sheets, query response modules, and analytical viewports.
- **Interactive Elements & Micro-Surfaces:** Exactly `8px` border-radius (`rounded-[8px]`) applied to buttons, filter chips, form inputs, citations, and status tags.
- **Indicators:** Fully circular pills (`9999px`) are restricted solely to step progress indicator nodes and numeric counter badges.
- **Stroke Uniformity:** All borders maintain a uniform stroke weight of exactly `1px`.

## Components

### Buttons
- **Primary:** Background `#00D09C`, text `#44475B` (or deep contrast variant), height `40px` (or `36px` compact), radius `8px`, typography `label-lg`. Hover/active shifts to `#00B386` with crisp state transition.
- **Secondary / Ghost:** Background `#FFFFFF`, border `1px solid #E6E9EB`, text `#44475B`, radius `8px`. Hover background transitions to `#F7F8FA`.
- **Inline / Text:** Pure text action in `#00D09C` hovering to `#00B386` with standard font weight `600`.

### Chips & Filter Tags
- **Interactive Neutral Chip:** Background `#F7F8FA`, border `1px solid #E6E9EB`, radius `8px`, padding `6px 12px`, text `#44475B`.
- **Answered / Selected Chip:** Background `#E8FBF6`, border `1px solid #00D09C`, text `#00B386`, radius `8px`, font weight `600`.

### Cards & Analytical Panels
- Background `#FFFFFF`, 1px border `#E6E9EB`, radius `12px`. Interior padding set to `16px` (compact) or `24px` (analytical narrative). Zero box shadows.

### Tables & Data Grids
- **Header Cells:** Background `#F7F8FA`, text `#7C7E8C`, font size `12px`, weight `600`, border-bottom `1px solid #E6E9EB`.
- **Data Rows:** Background `#FFFFFF`, border-bottom `1px solid #E6E9EB`, text `#44475B`. Hover background `#F7F8FA`.
- **Values:** Numbers set tabular with right-alignment.

### Inputs & Search Bars
- Background `#FFFFFF`, border `1px solid #E6E9EB`, radius `8px`, height `40px`, padding `8px 12px`, text `#44475B`, placeholder `#7C7E8C`.
- Focus state: Border transitions to `#00D09C` (1px, zero diffuse focus rings).

### Checkboxes & Radios
- Border `1px solid #E6E9EB`, background `#FFFFFF`. Selected background `#00D09C` with white checkmark. Radius `4px` for checkboxes, circular for radios.

### Citations & Footnotes
- Micro-pill links (`font-size: 11px`, `radius: 4px`, `padding: 2px 6px`) in `#E8FBF6` with `#00B386` text. Hover shifts to `#00B386` background with `#FFFFFF` text.