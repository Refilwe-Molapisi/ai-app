# CAIOS Design System — Tokens & Component Standards

**Document ID:** CAIOS-DESIGN-001
**Source:** Corporate Brand Identity v1.0 (approved) + UI direction screenshots (4, 14 Nov 2026)
**Applies to:** CAIOS user interfaces, dashboards, generated deliverables (reports/workbooks), and future web properties

---

## 1. Foundations

### 1.1 Colour palette (approved — Palette 1)

| Token | Hex | Role |
|---|---|---|
| `--brand-blue` | `#136CFC` | Primary brand colour: primary buttons, links, active nav, chart primary series, focus rings |
| `--brand-dark` | `#152641` | Structural chrome: sidebar background, primary headings, formal backgrounds, top-level text |
| `--brand-green` | `#C3F84A` | Accent only: positive status, "approved/complete" indicators, small highlights. **Never** for large surfaces or body text |
| `--brand-light` | `#E8F4FC` | Page background tint, selected-row highlight, info panels, hover fills |

Derived support tokens (used sparingly, derived from the palette — no new hues):

| Token | Value | Role |
|---|---|---|
| `--surface` | `#FFFFFF` | Cards, panels |
| `--page-bg` | `#F4F8FC` | App background (light blue neutral, between white and Light Blue) |
| `--text` | `#152641` | Body text = Dark Blue (palette doubles as text colour) |
| `--text-muted` | `#5A6B85` | Secondary text (Dark Blue at reduced emphasis) |
| `--border` | `#DCE7F3` | Hairlines, card borders, table dividers |
| `--green-text` | `#3D5A00` | Readable green for text-on-light (Green `#C3F84A` alone fails contrast on white) |
| `--blue-tint-90/10` | mixes of `#136CFC` with white | Chart ramps, hover states, selected states |

**Status pills** map to the palette first:

| Status | Pill style |
|---|---|
| Approved / Complete / Healthy | Green `#C3F84A` fill, Dark Blue text |
| In progress / Processing | Light Blue `#E8F4FC` fill, Blue text |
| Pending review / Action needed | White fill, Blue border, Blue text |
| Blocked / Failed / Overdue | Dark Blue `#152641` fill, Green text (used sparingly — the one high-attention combination) |
| Draft / Archived | White fill, `--border` border, `--text-muted` text |

### 1.2 Typography

- **Display / brand:** Libre Baskerville — logo wordmark, page titles (H1), major
  brand statements. Regular (400) and Bold (700); used large, never for body text
  or small labels (its x-height is too small at UI sizes).
- **Interface / everything else:** Montserrat — section headings (500/600), body
  (400), dashboard labels and table headers (500), buttons (500), data (400/500).

| Level | Typeface | Size / line-height | Weight |
|---|---|---|---|
| Page title (H1) | Libre Baskerville | 28–32 px / 1.25 | 700 |
| Card title (H2) | Montserrat | 18 px / 1.4 | 600 |
| Section heading (H3) | Montserrat | 15 px / 1.5 | 600 |
| Body | Montserrat | 14 px / 1.6 | 400 |
| Small / meta | Montserrat | 12–13 px / 1.5 | 400 |
| Table headers, dashboard labels | Montserrat | 12 px, uppercase, +0.06 em tracking | 500 |
| Buttons | Montserrat | 14 px | 500 |
| Stat value | Libre Baskerville | 30–36 px / 1.1 | 700 |

### 1.3 Spacing scale (extracted from screenshots — 4 px base grid)

`4 · 8 · 12 · 16 · 20 · 24 · 32 · 40 · 48`

- Card internal padding: **24 px** (20 px on dense tables).
- Card-to-card gap: **16–24 px** (grid gap 24 px on dashboard, 16 px in sidebars/panels).
- Section vertical rhythm: **32 px** between major blocks, **16 px** between related items.
- Page gutter: **32 px** desktop / 16 px mobile.
- Sidebar item height: **40–44 px**, icon-to-label gap 12 px.

### 1.4 Radii & elevation (extracted)

| Element | Radius |
|---|---|
| Cards / panels | 16 px |
| Buttons / inputs | 10 px |
| Status pills / tags / avatars | full radius (999 px) |
| Modals | 20 px |

Elevation is **soft and single-layer**: `0 1px 2px rgba(21,38,65,.06), 0 8px 24px rgba(21,38,65,.08)`.
Hover raises cards by one step. No hard shadows anywhere.

## 2. Layout & component inventory (from screenshots)

1. **Sidebar** — Dark Blue `#152641`, full height, fixed width 240–260 px.
   Brand wordmark in Libre Baskerville white at top; nav groups with small
   uppercase Montserrat 500 section labels; active item = Blue left-bar/soft
   Blue-tinted pill; muted white (`rgba(255,255,255,.65)`) for inactive text;
   Green used only for the single "live/alert" indicator dot.
2. **Topbar** — white, hairline bottom border: page context / search input
   (Light Blue fill on focus), notification bell, user avatar + name.
3. **Dashboard grid** — welcome row with Libre Baskerville greeting + primary
   Blue action button on the right; **stat tiles** (white, radius 16, small
   uppercase Montserrat label, huge Libre Baskerville value, delta chip in
   green-text/blue); 2:1 main/side split — main column for charts and tables,
   side column for activity/status panels.
4. **Charts** — Blue `#136CFC` primary series, Light Blue `#E8F4FC` comparison
   series, Dark Blue axis text, hairline gridlines; single-series charts may use
   the blue→light-blue tint ramp. Green reserved for "target met" markers.
5. **Tables** — white card, 12 px uppercase Montserrat headers in
   `--text-muted`, 44 px rows with hairline dividers, status pill column,
   row hover = Light Blue tint; numeric columns Montserrat 500, right-aligned.
6. **Buttons** — Primary: Blue fill, white text, radius 10, subtle blue shadow on
   hover; Secondary: white fill, `--border` border, Dark Blue text; Accent (rare):
   Green fill with Dark Blue text for the single most positive confirmation action;
   Ghost: text-only Blue for tertiary actions.
7. **Forms** — inputs radius 10, white fill, `--border` border, Blue focus ring
   (`0 0 0 3px rgba(19,108,252,.25)`); labels Montserrat 500 13 px.
8. **Workflow state chips** — the 12-state assignment lifecycle renders as pills
   using the §1.1 status mapping; CONNECTOR arrows use `--border` grey.

## 3. Rules

1. Palette 1 is the only colour source. No new hues; tints/shades of Blue and
   Dark Blue are permitted for data ramps and states.
2. Green `#C3F84A` is an **accent**: small surfaces, indicators, positive deltas.
   Never body text on white (use `--green-text`), never a large background.
3. Libre Baskerville never appears below ~24 px. If it must be small, it isn't
   Libre Baskerville.
4. One elevation system, one radius scale, one spacing scale — no per-screen
   exceptions.
5. Generated deliverables (Word/PDF/Excel) follow the same tokens: Dark Blue
   headings (Libre Baskerville for titles), Montserrat body, Blue tables headers
   on Light Blue fill, Green only for positive summary rows.

## 4. Open items screenshots cannot answer (send only if you have them)

1. **Logo asset** — SVG/PNG wordmark file and whether the monogram variant exists.
2. **Icon style** — screenshots show outline icons; preferred set (e.g. Lucide)
   and stroke weight if you have one.
3. **Density preference** — current screens are "comfortable"; confirm for
   data-heavy views.
4. **Chart series beyond 3** — the blue tint ramp is my default; confirm.
5. **Favicon / email header banner** assets.
6. Anything not visible in the four screens: empty states, error pages,
   onboarding illustrations — I'll design within the tokens unless you supply art.

Until provided, defaults are: Lucide-style 1.5 px outline icons, comfortable
density, blue tint ramp for charts — encoded in the prototype.
