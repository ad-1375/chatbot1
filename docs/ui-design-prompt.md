# UI design prompt (Google Stitch)

A copy-paste brief for [Google Stitch](https://stitch.withgoogle.com) to design the
four-tab Facts Assistant screen in Groww's visual language.

Two prompts are provided:

1. **Main prompt** — the full screen (all four tabs as sections).
2. **Follow-up prompts** — refinements to run after the first pass.

Design tokens are locked to Groww's public brand palette, so the model has no
freedom to invent blues, purples, or gradients.

## Design tokens (do not add anything outside this table)

| Token | Hex | Use |
|---|---|---|
| Primary green | `#00D09C` | primary button, active tab underline, links, step dots |
| Deep green | `#00B386` | button hover/pressed, link hover, small green text |
| Ink | `#44475B` | headings, body copy |
| Muted | `#7C7E8C` | captions, secondary labels, placeholders |
| Surface | `#FFFFFF` | cards, top bar |
| Subtle surface | `#F7F8FA` | page background, hover rows, neutral chips |
| Border | `#E6E9EB` | 1px hairlines, dividers |
| Green tint | `#E8FBF6` | "Answered" chip background, selected states |

## Main prompt

```text
Design a single, professional web app screen for "HDFC MF Facts Assistant" — a
facts-only FAQ assistant over five public Groww mutual-fund pages. Visual style:
modern Indian fintech, calm, dense but airy, trustworthy. It must look like a
real production tool an analyst uses daily, NOT a marketing landing page.

BRAND COLOURS — use ONLY these, no blues, purples, oranges, gradients or glass:
primary green #00D09C, deep green #00B386, ink #44475B, muted #7C7E8C,
surface #FFFFFF, page background #F7F8FA, border #E6E9EB, green tint #E8FBF6.
Light theme only.

TYPE + GEOMETRY: Manrope (fallback Inter). Body 15–16px, line-height 1.5.
Headings 600–700 weight, no serif. 8px spacing grid. Cards radius 12px,
buttons and chips 8px. 1px #E6E9EB borders instead of heavy shadows; at most one
very soft shadow on the composer card. Content max width 1120px, centred.

LAYOUT
1. Slim top bar (64px, white, 1px bottom border): left is a 32px rounded green
   square containing a white tick/leaf mark, next to the wordmark "HDFC MF Facts"
   in ink 600 weight. Right side: muted caption "5 sources · Build 2026-11-10"
   and a compact pill with a green dot and the label "Live".
2. Sticky tab row directly under the top bar: Ask · Pipeline · Sources ·
   Evaluation. Active tab is ink with a 2px #00D09C underline; inactive tabs are
   muted. No pill background on tabs.
3. ASK TAB (default, this is the main visual focus):
   - Centred question composer card (white, 1px border, 12px radius, ~96px tall):
     a single-line input with muted placeholder
     "Ask about expense ratio, exit load, minimum SIP, lock-in, riskometer…",
     and a green primary button "Ask" (white label, #00D09C fill, #00B386 on
     hover) aligned right inside the card. Under the input, a muted helper line:
     "Facts only. No investment advice."
   - Below it, a stack of three answer cards (8px gap), each white with a 1px
     border and 12px radius. Card anatomy: a small status chip top-left; the
     answer text at 16px ink, max 3 short sentences; a hairline divider; then a
     source row with a link icon, exactly one Groww page title as a #00B386
     underlined link, and right-aligned muted caption
     "Last updated from sources: 2026-11-09".
   - The three cards show the three real states and must be distinguishable by
     label, icon and fill intensity only — no new hues:
     "Answered" = #E8FBF6 fill, #00B386 text;
     "Not found" and "Refused" = #F7F8FA fill, #44475B text, 1px border;
     "Blocked (PII)" = same neutral chip with a small lock icon and the muted
     note "Value discarded, not stored."
4. PIPELINE TAB: two segmented sub-tabs "Retrieve" and "Answer" (active = white
   pill on #F7F8FA track, ink label). Show a vertical trace timeline: small white
   cards with step name in ink, latency in ms and a count in muted, joined by a
   thin #E6E9EB vertical line with #00D09C dots for completed steps.
5. SOURCES TAB: a clean list/table of exactly five rows — page title in ink,
   groww.in URL in #00B386, indexed-chunk count and last-fetched date in muted.
   Row hover = #F7F8FA. One muted line above the table:
   "Exactly five public Groww pages are indexed."
6. EVALUATION TAB: a summary line ("10 recorded cases · 6 answered · 1 not found
   · 2 refused · 1 blocked") then a compact table with columns: #, Question,
   Status chip, Citation, Last updated. Zebra rows in #F7F8FA.

HARD CONSTRAINTS: no charts, no graphs, no return/NAV/performance numbers
anywhere — the product refuses performance questions. No photography, no
illustrations of people or money, no badges or gamification. Never use green for
small body text on white unless it is #00B386. Ink on white must be 7:1 or
better. Desktop 1440px wide first, with a sane 390px mobile stack.
```

## Follow-up prompts

Run these one at a time after the first screen is generated:

```text
Same design, empty state: no answers yet. Keep the composer centred vertically in
the content area, add three muted example-question chips under it
("Expense ratio of HDFC ELSS?", "Exit load on Flexi Cap?",
"Lock-in for ELSS?") using #F7F8FA fill and #44475B text. Keep everything else.
```

```text
Same design, refusal state emphasis: the answer card shows a neutral chip
"Refused — advice", the muted explanatory line
"Facts-only assistant: it will not recommend buying, selling or holding.", and
no citation or source row at all. Do not add red or amber.
```

```text
Mobile 390px version: top bar collapses to wordmark plus "Live" pill, tabs become
a horizontally scrollable row, composer card full width with the Ask button
full width below the input, answer cards keep the same anatomy. Single column.
```

```text
Accessibility pass on the desktop screen: verify ink #44475B on white and on
#F7F8FA, ensure the active tab is not signalled by colour alone (underline +
weight), and make every link and button focusable with a visible 2px #00B386
focus ring.
```

## After Stitch

The exported screens (`DESIGN.md`, main/empty/refusal/mobile) are checked in under
`design/stitch/` and documented in `architecture.md` §7.11, together with the mock
content in them that must not ship.

Stitch returns a static web screen (HTML/Tailwind, Figma, or a preview link), not
Streamlit code. Two ways to use it:

1. **Design reference (low risk)** — map the tokens into `.streamlit/config.toml`
   (`[theme]` primaryColor `#00D09C`, backgroundColor `#FFFFFF`,
   secondaryBackgroundColor `#F7F8FA`, textColor `#44475B`) plus a small amount of
   `unsafe_allow_html=False`-compatible CSS for radius and borders.
2. **Pixel-accurate (higher risk)** — port the layout into Streamlit custom
   components; this breaks the existing `AppTest` UI smoke tests and the
   `unsafe_allow_html=False` constraint in `tests/security/test_ui_rendering.py`,
   so it would need a test rewrite.

Option 1 keeps the deployed app's contract intact; option 2 is a redesign project.
