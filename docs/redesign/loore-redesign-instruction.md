# Loore Full App Redesign — Claude Code Prompt

## Context

I just redesigned the landing page of Loore (loore.org), a self-authoring and journaling tool with AI reflection. The landing page is now live and establishes a strong design language. I need you to redesign the rest of the app — Feed, Dashboard, and Write — to match this aesthetic.

The app is a React frontend. You have access to the full repository.

## Design System (established by the landing page)

Carry this system into every component. Do not deviate or introduce new fonts, colors, or patterns unless absolutely necessary.

### Fonts (loaded via Google Fonts — already in the project)
```
--serif: 'Cormorant Garamond', Georgia, serif;   /* Headlines, journal entries, display text */
--sans: 'Outfit', system-ui, sans-serif;          /* Body, UI, labels, metadata */
```

### Color Palette
```
--bg-deep: #0e0d0b;         /* Page background */
--bg-surface: #151412;      /* Elevated surfaces, sidebars */
--bg-card: #1a1917;         /* Cards, panels, modals */
--text-primary: #e8e2d6;    /* Headlines, important text */
--text-secondary: #9e9688;  /* Body text */
--text-muted: #6b655b;      /* Metadata, timestamps, labels */
--accent: #c4956a;          /* CTAs, highlights, active states, key emphasis */
--accent-glow: #c4956a33;   /* Hover glows, box-shadows */
--accent-subtle: #c4956a18; /* Faint backgrounds for accent areas */
--border: #252320;          /* Card borders, dividers, separators */
--border-hover: #35322d;    /* Border on hover states */
```

### Design Principles
- **Warm, not cold.** This is NOT a tech dashboard. It's a contemplative, literary tool. Everything should feel like a leather journal, not a SaaS app.
- **Serif for the personal, sans for the structural.** Journal content, entry titles, and reflective text use Cormorant Garamond. UI chrome, buttons, labels, metadata use Outfit.
- **Restraint over decoration.** Minimal borders (1px, `--border` color). No cyan/teal. No heavy outlines. Cards are distinguished by subtle background shift, not thick colored borders.
- **Accent is amber, used sparingly.** Only for: active nav items, CTAs, important labels (like "Loore reflects"), key highlights. Never for borders around every card.
- **Typography does the heavy lifting.** Hierarchy comes from font size, weight, and color — not from boxes, borders, or background colors.
- **Generous whitespace.** Let content breathe. Padding inside cards: 1.5–2.5rem. Spacing between cards: 1.5rem+.
- **Subtle transitions.** Hover states should use 0.3–0.4s eases. Cards can lift slightly on hover (translateY(-2px), subtle box-shadow).

### Component Patterns

**Cards:**
```css
background: var(--bg-card);
border: 1px solid var(--border);
border-radius: 10px;
padding: 1.8rem 2rem;
transition: border-color 0.3s ease, box-shadow 0.3s ease;
```
On hover: `border-color: var(--border-hover); box-shadow: 0 4px 20px rgba(0,0,0,0.2);`

**Buttons (primary):**
```css
font-family: var(--sans);
font-weight: 400;
font-size: 0.9rem;
letter-spacing: 0.04em;
padding: 10px 24px;
border: 1px solid var(--accent);
background: transparent;
color: var(--accent);
cursor: pointer;
transition: all 0.3s ease;
border-radius: 6px;
```
On hover: faint amber background (`var(--accent-subtle)`), subtle glow.

**Buttons (secondary/ghost):**
Same but with `border-color: var(--border); color: var(--text-secondary);` — on hover, border lightens.

**Input fields and textareas:**
```css
background: var(--bg-deep);
border: 1px solid var(--border);
border-radius: 8px;
color: var(--text-primary);
font-family: var(--sans);
font-weight: 300;
padding: 14px 16px;
font-size: 1rem;
transition: border-color 0.3s ease;
```
On focus: `border-color: var(--accent); outline: none; box-shadow: 0 0 0 2px var(--accent-glow);`

**Labels and metadata:**
```css
font-family: var(--sans);
font-size: 0.75rem;
letter-spacing: 0.12em;
text-transform: uppercase;
color: var(--text-muted);
```

**Section dividers:** A centered 40px horizontal line in `var(--accent)` at 0.4 opacity, or a full-width 1px line in `var(--border)`.

---

## Navigation Bar

The current nav is plain text links. Redesign it as a minimal, fixed-top bar:
- Background: `var(--bg-surface)` with `border-bottom: 1px solid var(--border);`
- Left: "Loore" wordmark in Cormorant Garamond, weight 300, letter-spacing 0.25em, uppercase, small (0.85rem)
- Right: nav links in Outfit, weight 300, 0.85rem, color `var(--text-muted)`. Active link gets `color: var(--accent)`.
- Height ~56px. Padding 0 2rem. Max-width container centered inside.
- Keep it simple. No hamburger menu needed yet — these are few links.

---

## Page-by-Page Redesign Instructions

### 1. FEED PAGE (currently: stacked cards with cyan borders)

**Problems:** Cyan borders are harsh and don't match the warm palette. Cards are all the same visual weight. Raw markdown headers showing. Dense and monotonous.

**Redesign:**
- Page title "Feed" in Cormorant Garamond, weight 300, ~2rem, with a subtle amber divider beneath it.
- Each entry card uses the standard card pattern above (NO colored borders).
- **Card content hierarchy:**
  - **Title** (the `# ...` header): Cormorant Garamond, weight 400, 1.2rem, `--text-primary`. Strip the `#` markdown — render as clean text.
  - **Body preview**: Outfit, weight 300, 0.95rem, `--text-secondary`, line-height 1.7. Truncate to 2–3 lines with ellipsis.
  - **Metadata row** (handle, timestamp, comment count): Outfit, 0.75rem, `--text-muted`, spaced with a subtle `·` separator between items.
- **Voice note entries** could have a small label/tag: "Voice note" in the uppercase label style, maybe with a subtle mic icon or just the text in `--text-muted`.
- Cards should have a hover lift effect.
- Spacing between cards: 1rem gap.
- Max-width: 680px, centered.
- Consider a subtle fade-in on scroll for each card (IntersectionObserver, same pattern as landing page).

### 2. DASHBOARD PAGE (currently: profile with Surface Map, buttons, dense text)

This is the most complex page. It has: user handle, action buttons (Export, Import, Generate Profile), a model selector, and a large "Profile / Surface Map" section with structured psychological data.

**Problems:** The buttons look like Bootstrap defaults. The Surface Map is a wall of text with no visual structure. The green/red button colors clash with the warm palette.

**Redesign:**

**Header area:**
- Username/handle: Cormorant Garamond, weight 300, ~2rem, `--text-primary`.
- Action buttons row: Use the secondary/ghost button style. All buttons same style — no color-coded buttons. Space them with a small gap (0.75rem).
- Model selector dropdown: Style it to match — `var(--bg-deep)` background, `var(--border)` border, `var(--text-secondary)` text, Outfit font. On focus, amber border.
- "Generate Profile" can be the primary (amber) button style since it's the main action.

**Profile / Surface Map section:**
- Wrap the whole thing in a large card.
- "Profile" heading: Cormorant Garamond, 1.4rem, `--text-primary`.
- "Edit Profile" button: ghost button style, positioned top-right of the card.
- Generation metadata ("Generated by gpt-5.2 on..."): label style, `--text-muted`.
- **"SURFACE MAP" title**: Render as an uppercase label (Outfit, 0.7rem, letter-spacing 0.2em, `--accent` color). Think of it like a document section label, not a screaming header.

**Surface Map content structure — this is critical:**
The Surface Map has sections like "Identity Anchors", "Recurring Territories", "Stated Struggles & Desires", "Communication Fingerprint". Each of these should be:
- **Section heading**: Outfit, weight 500, 0.85rem, uppercase, letter-spacing 0.1em, `--text-muted`. A thin `var(--border)` line beneath it. Margin-top of 2.5rem between sections.
- **Body content**: Outfit, weight 300, 0.95rem, `--text-secondary`, line-height 1.75.
- **Bold terms** within the text: weight 400, `--text-primary` (so they pop but don't shout).
- **Bullet points**: If they must stay as bullets, use minimal custom bullets (a small amber dot or dash, not default browser bullets). But consider whether some of this could render as flowing prose instead — it would feel more like a profile and less like a medical chart.

The overall effect should be: reading someone's beautifully typeset character sheet, not scanning a dense clinical document.

### 3. WRITE PAGE (currently: modal dialog with textarea, dropdowns, buttons)

**Problems:** The modal is plain white/light with default form styling. It clashes completely with the dark theme. The dropdown menus are unstyled browser defaults.

**Redesign:**
- **Modal overlay**: Dark, semi-transparent (`rgba(0,0,0,0.7)`) with a subtle backdrop blur (`backdrop-filter: blur(8px)`).
- **Modal container**: `var(--bg-card)` background, `var(--border)` border, border-radius 12px, padding 2rem. Max-width 640px. Subtle box-shadow for depth.
- **"Write New Entry" title**: Cormorant Garamond, weight 400, 1.4rem, `--text-primary`.
- **Close button (×)**: `var(--text-muted)`, hover to `var(--text-primary)`. No visible border/background.
- **Textarea**: Use the input field style from the design system. Make it generous in height (~250px min). Placeholder text in `--text-muted`, italic (Cormorant Garamond) — something like "What's present for you right now..." instead of "Write your thoughts here..." to match the product tone.
- **Privacy Level dropdown**: Style as a custom select or at minimum override the default with dark styling matching inputs. Show the label "Privacy Level" in the uppercase label style above it. The description text ("This note is private...") in `--text-muted`, 0.8rem.
- **AI Usage section**: Same label treatment. The radio/checkbox options should use custom-styled controls (amber accent for selected state). Option text in Outfit, 0.9rem, `--text-secondary`.
- **Bottom button row** (Submit, Record, Upload): Primary style for Submit. Secondary style for Record and Upload. Space evenly. Consider small icons (optional — a pen for Submit, mic for Record, arrow-up for Upload) but text-only is fine too.

---

## General Implementation Notes

- All styles should be done inline or via CSS-in-JS to match the existing pattern in the codebase (check how current components are styled and match that approach).
- If the app uses a global CSS file, consider creating a design-tokens file or adding CSS variables to `:root` so they're reusable everywhere.
- Check for any existing shared component library or style utilities and extend those rather than creating one-off styles.
- Make sure the Google Fonts import (`Cormorant Garamond` weights 300,400,500,600 + italics, and `Outfit` weights 300,400,500) is loaded in the HTML entry point if not already.
- Preserve all existing functionality — this is purely a visual/styling pass. Don't change state management, API calls, routing, or component structure unless necessary for the visual changes.
- Test that text remains readable. Body text should never be darker than `--text-secondary` (#9e9688). If anything feels hard to read, bump it toward `--text-primary`.
- The app should feel like one cohesive product with the landing page, not like a different app behind the login wall.

---

## Reference

Look at `LandingPage.js` in the codebase — it is the canonical reference for this design system. Match its feel, its rhythm, its warmth.

The guiding aesthetic: a contemplative, editorial, warm dark-mode interface that feels like a beautifully typeset private journal — not a tech dashboard, not a SaaS product, not a coding tool. Think: if a boutique press designed a digital journaling experience.
