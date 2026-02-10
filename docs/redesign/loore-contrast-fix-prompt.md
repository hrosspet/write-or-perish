# Loore Redesign — Contrast & Readability Fix

The redesign is structurally correct but has a critical readability problem: the three background tiers (--bg-deep, --bg-surface, --bg-card) are too close together, making cards invisible against the page. The Feed has no visible card containers at all. The Write modal blends into its overlay. Everything looks like dark text floating in a void.

## Updated CSS Variables

Replace the current design token values in index.css `:root` with these — the palette is the same hue family, just with wider contrast steps:

```css
:root {
  --bg-deep: #0e0d0b;
  --bg-surface: #181714;       /* was #151412 — bumped up */
  --bg-card: #211f1b;          /* was #1a1917 — bumped significantly */
  --bg-card-hover: #282520;    /* NEW — for hover states on cards */
  --bg-input: #151311;         /* NEW — for inputs/textareas, darker than card */
  --text-primary: #ede8dd;     /* was #e8e2d6 — slightly brighter */
  --text-secondary: #a89f91;   /* was #9e9688 — bumped up for readability */
  --text-muted: #736b5f;       /* was #6b655b — bumped up */
  --accent: #c4956a;           /* unchanged */
  --accent-dim: #a07a55;       /* NEW — for subtle accent uses like list bullets */
  --accent-glow: #c4956a40;    /* was #c4956a33 — slightly more visible */
  --accent-subtle: #c4956a15;  /* was #c4956a18 */
  --border: #302c27;           /* was #252320 — bumped up significantly */
  --border-hover: #433e36;     /* was #35322d — bumped up */
  --serif: 'Cormorant Garamond', Georgia, serif;
  --sans: 'Outfit', system-ui, sans-serif;
}
```

The key changes: --bg-card jumps from #1a to #21 (a visible step), --border jumps from #25 to #30, and text values all shift brighter. These are small hex changes but they cross the perceptibility threshold on standard monitors.

## Component-Specific Fixes

### Feed / Bubble.js
The feed entries currently render WITHOUT visible card containers — they're just floating text on the page background. This is the #1 readability issue. Each entry MUST be wrapped in a visible card:
- background: var(--bg-card)
- border: 1px solid var(--border)  
- border-radius: 10px
- padding: 1.6rem 1.8rem
- On hover: border-color var(--border-hover), box-shadow 0 4px 24px rgba(0,0,0,0.3), transform translateY(-1px)

Verify that the Bubble component's container element actually has these styles applied. The current implementation may have lost the background/border during the redesign.

### Write Modal / App.js + NodeForm.js
The modal container needs a visible box-shadow to separate it from the backdrop:
- box-shadow: 0 24px 80px rgba(0,0,0,0.5)
- Make sure the modal background is --bg-card (not --bg-deep or transparent)
- The textarea should use --bg-input (#151311) for its background — darker than the modal card to create an inset effect
- The textarea border needs to be --border (#302c27) — verify it's not transparent or too dark

### Dashboard
- The Profile card section needs visible background/border (same card pattern as Feed)
- Action buttons (Export Data, Import Data) should have border: 1px solid var(--border) — check they're not borderless
- Make sure the model selector dropdown has --bg-input background with --border border

### Login / LoginPage.js  
- The login card needs: background var(--bg-card), border 1px solid var(--border), border-radius 12px, box-shadow 0 16px 60px rgba(0,0,0,0.3)
- The sign-in buttons need visible borders: 1px solid var(--border)
- On hover, buttons should get border-color var(--border-hover) and a subtle background shift to rgba(255,255,255,0.03)

### NavBar
The nav is fine structurally but verify:
- Background is --bg-surface (#181714), not --bg-deep  
- Border-bottom is 1px solid var(--border) (#302c27)
- These two values need to be perceptibly different from the page background

## Testing Checklist
After making changes, verify on a standard (non-Retina) monitor or by setting display scaling to 100%:
1. Feed cards are clearly distinguishable as cards against the page background
2. The Write modal is clearly a floating panel, not a transparent overlay
3. Dashboard buttons have visible borders
4. The Login card is clearly a card on the page
5. The NavBar is distinguishable from the page below it
6. All body text (--text-secondary) is comfortable to read for extended periods
7. Metadata text (--text-muted) is legible, even if subdued

## Reference
I've attached an HTML preview file (loore-app-previews.html) showing the target state for all four views. Match these exactly.
