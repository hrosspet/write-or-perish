# Loore UX Redesign — Implementation Prompt

## Reference

The interactive prototype is at `loore-prototype-v2.html` in the project root. Open it in a browser and click through all tabs (Home, Reflect, Orient, Converse, Profile, Todo) to understand the target UX. Everything below refers to that prototype as the source of truth for the new design, with exceptions noted explicitly.

---

## Overview

We're shifting Loore from a power-user-first tool (where all low-level features are exposed) to a workflow-first app with three high-level modes — **Reflect**, **Orient**, **Converse** — while keeping all existing low-level features available for power users behind a "Craft mode" toggle.

The current codebase has low-level features: threading, branching, quoting/embedding nodes, manual linking of `user_profile` and `user_export`. All of these remain functional. Most users won't encounter them simply because the mainstream workflows produce clean linear threads — no code changes are needed to hide or disable these features in the Log or DetailedNodeView.

---

## 1. Navigation Bar Redesign

### What to keep from the current codebase
- The **Logo + "LOORE" wordmark** on the left side of the NavBar must stay exactly as it currently is in the codebase. Do not use the prototype's version — the existing one is correct.

### New nav link structure
The right side of the NavBar should become:

```
About ▾    Profile    Todo    Log    ⋮
```

- **About** — a dropdown (not a page link) containing the three existing public informational pages:
  - Vision (`/vision`)
  - Why Loore (`/why-loore`)
  - How To (`/how-to`)
- **Profile** — links to the Profile page (see section 4)
- **Todo** — links to the Todo page (see section 5)
- **Log** — links to the existing feed/log page, unchanged
- **⋮** — three vertical dots button that opens a dropdown menu

### About dropdown
- Triggered by clicking "About" in the nav
- Simple dropdown with three links to the existing pages
- Close on outside click or navigation
- The About pages themselves remain exactly as they are now — no changes to their content or styling

### Three-dot dropdown menu

**Default items (always visible):**
- Import data

**Craft mode items (only visible when Craft mode is on):**
- Write new entry (opens existing Write modal)
- Export data
- Model — shows currently selected model on the right side (e.g., "Claude 4.6 Opus")

**Always at the bottom, separated:**
- Craft mode — with a toggle switch (on/off)
- Logout

### Styling
- Nav links should use the same font family, weight, and size as the current codebase (`font-family: var(--sans); font-weight: 300; font-size: 0.85rem; color: var(--text-muted)`)
- Active page link gets `color: var(--accent)`
- The "About" link should have a small dropdown indicator (subtle downward chevron or triangle)
- The three-dot button should be very subtle (low opacity, three small circles stacked vertically)
- Both dropdowns should match the app's dark card aesthetic (`var(--bg-card)` background, `var(--border)` borders, subtle box-shadow)
- Close dropdowns on outside click

---

## 2. Home Screen (New — Default Authenticated View)

This is the new default page after login. It replaces the feed as the first thing authenticated users see.

**Important:** The existing landing page (marketing/public page with "Uncover your lore. Author yourself." hero, narrative sections, CTA) must remain accessible. See Routing (section 7) for how this is handled.

### Layout
- Centered vertically and horizontally
- Greeting text: "Good morning" / "Good afternoon" / "Good evening" (based on local time) — in serif font, muted color
- Main question: "What's on your mind?" — large serif font
- Three mode cards in a horizontal row (stack vertically on mobile ≤740px):

**Card 1 — Reflect**
- Icon: the Loore ECG logo (small version)
- Title: "Reflect"
- Description: "Speak what's present. Let it come back clearer."
- Clicking navigates to the Reflect flow

**Card 2 — Orient**
- Icon: compass-like SVG (see prototype)
- Title: "Orient"
- Description: "Ground your day. See what matters."
- Clicking navigates to the Orient flow

**Card 3 — Converse**
- Icon: chat bubble SVG (see prototype)
- Title: "Converse"
- Description: "Ask anything. Think out loud."
- Clicking navigates to the Converse flow

### Interactions
- Cards have hover states: slight lift (`translateY(-3px)`), border glow, accent top-line reveal
- Entrance animations: staggered fade-up for greeting → question → cards
- Ambient radial gradient background (warm, subtle)

---

## 3. Three Workflow Screens

### 3a. Reflect

**State 1 — Recording:**
- Full-screen centered layout with warm ambient radial gradient background
- Italic serif prompt: "Speak what's present…"
- Animated ECG logo (the Loore heartbeat logo):
  - Line draws on with a stroke-dashoffset animation
  - After draw-in completes, the spike section pulses with a breathing glow (opacity + drop-shadow cycling)
  - A thin vertical scanline travels left-to-right repeatedly
- Below the ECG: voice waveform visualization (animated bars)
- Timer display (e.g., "01:24")
- Stop button: circular, accent-bordered, with a rounded square inside

**State 2 — Response (after stopping):**
- Date header in small caps
- User's transcript in serif font (larger, secondary color)
- Divider line
- "Loore reflects" label with pulsing accent dot
- AI response in sans font (smaller, muted color), paragraphs streaming in with staggered fade-up
- "Pattern from your lore" insight card — accent-subtle background, left accent border. Should include a "View in profile →" link that navigates to the Profile page
- Action buttons: "Continue reflecting" (primary), "Save to log", "Branch →"

**Backend integration:**
- Recording uses existing voice recording infrastructure
- After transcription, compose the AI prompt with: transcription + current `user_profile` content
- The AI should detect what the user needs (therapeutic reflection, self-development guidance, or simple mirroring) and respond accordingly
- Save the session to the log automatically

### 3b. Orient

**After voice input (show the result state):**
- Date header centered
- Title: "Your day, oriented"
- User's sharing displayed in italic serif
- "Updated from your sharing" section showing a **diff view** against the full todo list:
  - Tasks marked as completed (checked, strikethrough)
  - New tasks extracted from the voice note (tagged "new")
  - Existing open tasks that were mentioned
  - "View full todo list →" link at the bottom that navigates to the Todo page
- "Suggested priority order" section:
  - Numbered priority cards with drag handles (⋮⋮)
  - Each shows task name, estimated time, and a qualifier (e.g., "high leverage", "deep work", "fixed")
  - Cards have hover state with slight right-shift
- AI note at the bottom — contextual advice based on the user's patterns (uses profile data)

**Backend integration:**
- Recording uses existing voice recording infrastructure
- After transcription, compose the AI prompt with: transcription + current `user_profile` + current `user_todo` (latest version markdown)
- AI returns structured data: completed tasks (to check off), new tasks (to add), priority ordering, and a contextual note
- **Create a new version of the todo** with the changes applied (see section 5 for the data model)
- Save the Orient session to the log

### 3c. Converse

This is a standard responsive chat interface:
- Messages appear immediately after sending (no "click to get AI response" behavior like current power-user mode)
- User messages: right-aligned, card background, secondary text color
- AI messages: left-aligned, transparent background with subtle border, muted text color
- Below each AI message: subtle "used profile + N entries · view context" link (low opacity, only visible on hover or for curious users). Clicking this should show what context was composed for that response (this is the progressive disclosure hook into the power-user features)
- Input bar at bottom: sticky, with text input, mic button, and send button
- The mic button starts voice recording (same infrastructure as Reflect/Orient)

**Backend integration:**
- Each message sent automatically composes context from: `user_profile` + in the future also from relevant entries (using embedding/retrieval which will be implemented later)
- AI responses stream in
- The chat thread is saved to the log as a linear thread
- All existing low-level features (branching, quoting, thread management) still work under the hood and are accessible if the user navigates to the thread in the Log/DetailedNodeView

---

## 4. Profile Page (Redesign of Existing)

The current Profile/Dashboard page needs to be redesigned to match the prototype. The underlying `user_profile` data model stays the same — this is primarily a presentation change.

### Layout
- Page title: "Profile" in large serif
- **Version indicator** next to title: shows version number and date (e.g., "v12 · Feb 14, 2026") with a green status dot
- **Clicking the version indicator** opens an inline edit view of the current profile markdown. Saving creates a new version with `generated_by: "manual"`. This is the primary way users edit their profile — no separate "Edit" button cluttering the UI.
- A subtle "history" link next to the version indicator opens the version history drawer
- Meta line: generation info (e.g., "Generated from 245,587 tokens across 43 entries · Next update in 3 days")
- Accent divider line
- Profile sections rendered from the `user_profile` markdown content, each with:
  - Section heading in small caps, accent color, with bottom border
  - Body content in sans font, with styled list items (small accent dots as bullets)
  - Bold highlights for key terms

### Version history drawer
- Slides in from the right side of the screen
- Shows all versions of the profile, most recent first
- Each entry shows: version number, date, generation method ("Auto-generated" or "Manual edit"), token count
- Current version has a small "current" badge
- Clicking a version loads that version's content in read-only view
- Future: add a "Revert to this version" button on historical versions

**Backend requirements:**
- Check if there's already an API endpoint for retrieving `user_profile` version history. If not, create one:
  - `GET /api/user-profile/versions` — returns list of all versions with metadata (version number, date, generation method, token count)
  - `GET /api/user-profile/versions/:id` — returns full content of a specific version
- The current `user_profile` is already stored with versioning in the DB. These endpoints just need to expose the version list and individual version content.

---

## 5. Todo (New Feature)

This is a **new entity** that needs to be created in the database. It should follow the same pattern as `user_profile` — each version is a full markdown document, not a normalized set of task rows.

### Data Model

Create a new `user_todo` entity modeled closely on `user_profile`:

```
user_todo {
  id: uuid
  user_id: uuid (FK to users)
  version: integer (auto-incrementing per user)
  content: text  // markdown checklist — the full todo list as markdown
  generated_by: string  // "orient_session" | "manual" | "import"
  source_session_id: uuid | null  // FK to the session that created this version, if applicable
  token_count: integer | null
  created_at: timestamp
}
```

The `content` field is a **markdown document** with checkboxes. Example:

```markdown
## Today

- [ ] Reply to investor email
- [ ] Finish landing page tweaks for alpha
- [ ] Fix favicon rendering on Safari
- [ ] Herbert's doctor appointment at 2pm
- [ ] Cook dinner

## Upcoming

- [ ] Write alpha announcement for X
- [ ] Set up analytics / error tracking for alpha
- [ ] Schedule couples therapy — February session
- [ ] Draft "Hyperstition Loop" chapter 3 outline

## Completed recently

- [x] Rebrand mockups from Write or Perish → Loore
- [x] Generate favicon and app icons
- [x] Finalize landing page copy with AI closing CTA
```

Each mutation (checking a box, adding a task, reordering, Orient session update) creates a new version with the full updated markdown.

### API Endpoints

- `GET /api/todo` — returns current (latest version) todo
- `PUT /api/todo` — creates a new version with updated content. Body: `{ content: "markdown string", generated_by: "manual" | "orient_session" }`
- `GET /api/todo/versions` — returns list of all versions with metadata (version number, date, generated_by, created_at)
- `GET /api/todo/versions/:id` — returns full content of a specific version
- `POST /api/todo/revert/:versionId` — creates a new version by copying content from a historical version

Follow the same patterns used for `user_profile` endpoints in the codebase.

### UI Layout
- Page title: "Todo" in large serif
- **Version indicator** next to title: shows version number and date (e.g., "v38 · today") with a green status dot
- **Clicking the version indicator** opens inline editing of the markdown. Saving creates a new version with `generated_by: "manual"`.
- A subtle "history" link opens the version history drawer (same drawer component as Profile, parameterized by type)
- Meta line: "Last updated by Orient session · 9:14 AM today" (or "Last edited manually · 3:22 PM yesterday")
- Accent divider
- Render the markdown checklist with these visual groups:
  - **Today** — tasks under the `## Today` heading, with count
  - **Upcoming** — tasks under `## Upcoming`, with count
  - **Completed recently** — tasks under `## Completed recently` (checked items, strikethrough, lower opacity)
- Each task shows: interactive checkbox, task text
- Checking/unchecking a task creates a new todo version (update the markdown and PUT)

### Version history drawer
- Same slide-out drawer component as Profile, parameterized
- Shows all versions with: version number, date, `generated_by` label, current badge on latest
- Clicking a version loads that version's content in read-only view

---

## 6. Craft Mode Behavior

Craft mode is a toggle in the three-dot dropdown menu. Its state should be persisted per user (in user preferences/settings).

### What Craft mode changes

**In the three-dot dropdown menu:**
When Craft mode is **off** (default), the menu shows:
- Import data
- Craft mode toggle
- Logout

When Craft mode is **on**, additional items appear:
- Write new entry (opens existing Write modal)
- Export data
- Model selector (shows current model, allows switching)

**Everything else stays the same regardless of Craft mode.** Specifically:
- The **Log** page remains exactly as it is now. No changes needed. Most mainstream users will naturally have clean linear threads because they use the three workflows. Power users who know about branching can still do it from the DetailedNodeView.
- The **Profile** and **Todo** pages are the same for both modes. Editing is available to everyone (triggered by clicking the version indicator).
- The **DetailedNodeView** (when opening a specific entry from the Log) remains exactly as it is now — branching, quoting, and linking are still available there for anyone who navigates to it.
- The three workflows (Reflect, Orient, Converse) behave the same in both modes.

### Summary
Craft mode is intentionally minimal — it only controls which items appear in the overflow menu. The philosophy is that power features aren't hidden behind a wall; they're just not promoted. Users discover them naturally through the Log and DetailedNodeView.

---

## 7. Routing

### Authenticated routes
- `/` — Home screen (three workflow cards) — **this is the new default for logged-in users**
- `/reflect` — Reflect flow
- `/orient` — Orient flow
- `/converse` — Converse chat
- `/profile` — Profile page
- `/todo` — Todo page
- `/log` — Log/feed page (existing, unchanged)
- `/login` — Login page (existing, unchanged)

### Public routes (no auth required)
- `/landing` — The existing marketing landing page ("Uncover your lore. Author yourself." hero, narrative sections, CTAs). This is the current `/` page — it needs to move to `/landing`.
- `/vision` — existing public page
- `/why-loore` — existing public page
- `/how-to` — existing public page

### Root route logic
When a user hits `/`:
- If **not authenticated** → redirect to `/landing`
- If **authenticated** → show the Home screen (three workflow cards)

Update any internal links and CTAs on the landing page accordingly (e.g., "Join the Alpha" should still point to `/login?returnUrl=%2F`).

### NavBar links
All nav links should use client-side routing (no full page reloads).

---

## 8. Design Tokens (Reference)

These already exist in the codebase. Ensure all new components use them consistently:

```css
--bg-deep: #0e0d0b;
--bg-surface: #181714;
--bg-card: #211f1b;
--bg-card-hover: #282520;
--bg-input: #151311;
--text-primary: #ede8dd;
--text-secondary: #a89f91;
--text-muted: #736b5f;
--accent: #c4956a;
--accent-dim: #a07a55;
--accent-glow: #c4956a40;
--accent-subtle: #c4956a15;
--border: #302c27;
--border-hover: #433e36;
--serif: 'Cormorant Garamond', Georgia, serif;
--sans: 'Outfit', system-ui, sans-serif;
```

---

## 9. Implementation Order

Suggested order to minimize blocking:

1. **Routing update** — move landing page from `/` to `/landing`. Set up `/` to show Home for authenticated users, redirect to `/landing` for unauthenticated. Add new route stubs.
2. **NavBar redesign** — update nav links to `About ▾ | Profile | Todo | Log | ⋮`. Implement both dropdowns (About dropdown, three-dot menu with Craft mode toggle). Keep existing logo + wordmark.
3. **Home screen** — build the three-mode-card layout at `/`.
4. **Converse** — this is closest to the existing chat functionality, just with auto-response behavior and a new wrapper UI. Wire up to existing AI + context composition.
5. **Profile page redesign** — restyle the existing profile view. Add version history endpoint if it doesn't exist. Add the version history drawer. Add edit-on-click for the version indicator.
6. **Todo — backend** — create the `user_todo` data model, migration, and API endpoints (following `user_profile` patterns).
7. **Todo — frontend** — build the Todo page UI with grouped markdown checklist rendering, version history, and edit-on-click.
8. **Reflect** — build the recording screen with ECG animation, wire up to voice recording + transcription + AI response flow.
9. **Orient** — build the result screen, wire up to voice recording + transcription + AI with todo integration. This depends on Todo backend being ready.
10. **Craft mode** — implement the toggle that controls which items appear in the three-dot dropdown. Persist per user.

---

## 10. Key Principles

- **Voice-first, zero-configuration for mainstream users.** One tap to start recording, automatic AI responses, no settings to configure before getting value.
  - voice-first means the responses are also automatically TTSed and played back immediately. That there is text stored in the DB is completely hidden from the user until they click on Log, where everything can be seen & editted
  - zero-configuration means that for the three workflows, there will be automatically set privacy: private, AI usage: chat. This can be edited via the Log / DetailedNodeView, but that's separate from the three workflows which are to be as simple as possible
- **Progressive disclosure.** Power features are not hidden behind a wall — they're just not promoted. Users discover branching, quoting, and linking naturally through the Log and DetailedNodeView. Craft mode only controls the overflow menu.
- **Profile and Todo are living documents.** They update automatically through usage (Orient sessions update Todo, weekly regeneration updates Profile). Each mutation creates a new version. Users can edit by clicking the version indicator and view history via the drawer.
- **Todo is a versioned markdown document**, not a normalized task database. Each version stores the complete checklist as markdown. This keeps it simple and consistent with how `user_profile` works.
- **The Log is unchanged.** No modifications needed. Mainstream users naturally produce linear threads through the three workflows. Power users who want branching can still access it from DetailedNodeView.
- **Preserve the aesthetic.** Dark, warm, restrained. Serif for user content and headings, sans for UI and AI responses. Generous spacing. Subtle animations. No visual clutter.
