# First cohort experiment: build specification

Status: experiment, on branch `experiment/first-cohort` (Circles merged with main). Dark behind `COHORT_V1` (default off). Web only. Not for merge to main without Peter's review.

The story this implements is in `docs/experiments/FIRST-COHORT.md` (the walkthrough). This file is the technical part. Where this spec and the existing code disagree about a name or a mechanism, follow the existing code's patterns and note the deviation in the PR body.

## Hard rules (Peter's, not negotiable)

- Nobody but the author reads a user's content. The founder/admin gets NO path to the interview thread, to an unsent report, or to any private node. Admin cohort routes return states and timestamps only. No `or is_admin` on any content route.
- Nothing leaves without the person's explicit, previewed act. The report reaches the founder only when the person presses Send; before that it is a private node of theirs. They can take it back (normal audience edit).
- A user's provider/model is never switched. Background drafts use the same model-selection path that `tasks/poll_draft.py` uses.
- "No AI means no AI": nothing marked ai_usage none reaches a model.
- A model acting for a person never reads another person's shared entries (Circles rule, unchanged).
- Loore sends nothing to anyone outside (no emails, no X messages) in this experiment.
- The repo is public: no production infrastructure details in code comments, commits or PR text.

## Flag

`COHORT_V1` env var, default false (config.py, same pattern as `CIRCLES_V1`). Off: every `/api/cohort/*`, `/api/invitation/*` and `/api/admin/cohorts*` route answers 404, and no cohort hook changes any other behaviour.

## Data model (backend/models.py; no manual migrations)

`Cohort`
- id, slug (unique, url-safe), name, about (Text, plaintext: the founder's public description, shown on invitation pages)
- owner_user_id (FK user; the founder)
- booking_url, call_url (String, nullable)
- next_call_at (DateTime, nullable), question (Text, nullable, plaintext, founder's), question_set_at
- free_until (Date, nullable)
- created_at

`CohortInvitation`
- id, cohort_id (FK), code (unique, `secrets.token_urlsafe(24)`), x_handle (String, stored without @, compared case-insensitively)
- user_id (FK user, nullable): the account bound to the invitation
- letter (Text): the founder's letter to this person. Stored with the same encryption helpers as node content (`set_letter`/`get_letter`); it is the founder's text about a person.
- timestamps, all nullable: created_at, first_opened_at, claimed_at, interview_started_at, report_drafted_at, report_sent_at, report_skipped_at, call_done_at, admitted_at, declined_at, closed_at
- interview_node_id, report_node_id, intro_node_id (FK node, nullable)
- report_task_status / intro_task_status (String, nullable) for polling
- unique (cohort_id, x_handle)

`Circle.cohort_id` (FK cohort, nullable): marks the circle that the system keeps for a cohort member. Such a circle cannot be renamed or deleted by its owner and its members cannot be edited by hand (routes answer 409 with a plain sentence); it is shown in the circles list with the cohort's name.

State of an invitation (derived, one function in `backend/utils/cohort.py`): `declined` > `closed` > `admitted` > `call_done` > `report_sent` | `report_skipped` > `report_drafted` > `interview_started` > `claimed` > `opened` > `invited`.

## Access hooks

- **Subscription gate.** An account bound to an invitation that is not declined/closed, whose cohort has `free_until` today or later, is let in with full access, AI included, without a card, as if its plan had been set by hand. Find the single place where "plan set by hand" is decided (`backend/utils/subscriptions.py`) and add the cohort case there, so web (`notLetIn`), iPhone and the 402 gate all follow. If the existing trial model-cost cap can be applied to these accounts with a small change, apply it and say so; otherwise list it under "Needs Peter".
- **Approval.** Creating an invitation approves/whitelists the X handle through the same function the admin "whitelist" route uses, so that the person's X sign-in finds or creates an approved account. Do not duplicate that logic.
- **Circles for cohort people.** `circles` is enabled for: the cohort owner, and every account bound to a live invitation (they need it to share the report with the founder before admission). Add this to the one function that decides whether Circles is on for a user (`backend/utils/circles.py`), only when `COHORT_V1` is on.

## Public invitation (no sign-in)

- `GET /api/invitation/<code>` → `{cohort: {name, about}, founder: {display name or username}, letter, state: "open"|"claimed"|"declined"|"closed"}`. Sets `first_opened_at` once. Unknown code: 404 with no detail. Response header `X-Robots-Tag: noindex, nofollow`. Rate-limit by IP if a limiter exists in the codebase; otherwise a simple per-code counter is not needed.
- `POST /api/invitation/<code>/decline` → sets `declined_at` (only when not yet claimed, or when the caller is the bound user). No reason is asked.
- Once claimed, the public GET no longer returns the letter (`state: "claimed"`, letter omitted); the bound user reads it through `/api/cohort/me`.

## Signed-in person

- `POST /api/cohort/claim {code}`: binds the invitation to the current user. Refuse (403, plain sentence naming the handle) when the invitation has an `x_handle` and the current account's X username differs or the account has no X identity (then the sentence says to sign in with X). Idempotent for the bound user. Sets `claimed_at`.
- `GET /api/cohort/me` → the caller's invitation (state, letter, cohort name/about, booking_url once the report is sent or skipped, ids of interview/report/intro nodes, task statuses), or `{invitation: null}`. For the cohort owner without an invitation: `{owner_of: [...]}`.
- `POST /api/cohort/interview/start {mode: "text"|"voice"}`: idempotent. Creates the interview thread as the user's private thread with prompt key `cohort_interview`, forced private, and has the AI write the first message (see how `POST /api/voice/from-node` and Text mode start create a root with `session_helpers.create_agentic_root`, and reuse that machinery; PR 227's `/api/alchemy/start` is prior art for "AI speaks first from a product-chosen prompt", read it with `git show origin/feat/alchemical-mode:backend/routes/alchemy.py`). The interview's context must contain: the cohort's `about`, the person's letter, and whatever the standard agentic prompt already includes about the user (profile). Returns the ids needed for the existing Text-mode or Voice-mode page to open that thread. Sets `interview_started_at`, `interview_node_id`.
- `POST /api/cohort/report/draft`: starts a Celery task (`backend/tasks/cohort_report.py`, REGISTER it in `backend/celery_app.py`) that reads the person's own interview thread and writes the report draft with prompt `cohort_report` into a new private node owned by the person (authored as the person's own document, so that their edit is a normal node edit and "the user's edit wins"). Re-running replaces nothing: it creates a new draft only if the person asks to regenerate and the current one is unsent. Follow `tasks/poll_draft.py` for model choice, cost account, token cap (cap the report at about 700 words) and failure handling. Sets `report_node_id`, `report_drafted_at`.
- `GET /api/cohort/report` → task status and node id (the content is fetched through the normal node route).
- `POST /api/cohort/report/send`: shares the report node with exactly one named reader, the cohort owner, through the Circles audience machinery (`node_reader`; the same code path as `PUT /api/nodes/<id>/audience`), so the founder finds it under "Shared with you" with the usual Updates note. Refuse if the node is empty after stripping whitespace (the person should use "Send nothing" instead). Sets `report_sent_at`.
- `POST /api/cohort/report/skip`: sets `report_skipped_at`; nothing is shared.
- `POST /api/cohort/intro/draft`: Celery task; drafts a short introduction for the group (prompt `cohort_intro`) from the CURRENT text of the person's report node (their edited version, never the interview), into a new private node. Only for admitted members.
- `POST /api/cohort/intro {node_id}`: the member marks one of their own nodes as their introduction; it must be shared with their cohort circle (otherwise 409 with a sentence telling them to share it with the group first). A convenience parameter `share: true` performs that share in the same call.
- `GET /api/cohort/page`: members only (admitted, or the owner). Returns cohort name, about, question, question_set_at, next_call_at, call_url, members `[{username, intro_node_id}]` (admitted members and the owner), and `entries`: the newest 50 entries shared with the caller through cohort circles of this cohort by its members, plus the caller's own entries shared with their cohort circle, each `{node_id, author_username, shared_at, title/preview as the existing shared-with-me route returns it}`. Reuse the shared-with-me query; do not write a second content path.

## Founder (admin routes; founder = cohort owner, who must also be an admin)

- `GET/POST /api/admin/cohorts`, `PUT /api/admin/cohorts/<id>` (name, about, booking_url, call_url, next_call_at, question, free_until).
- `GET /api/admin/cohorts/<id>/invitations` → per invitation: x_handle, username if bound, state, all timestamps, the invitation link path, and the founder's own letter. No node content, no node ids of the interview.
- `POST /api/admin/cohorts/<id>/invitations {x_handle, letter}` → creates, approves the handle, returns the link. `PUT .../invitations/<iid> {letter}` while unclaimed.
- `POST .../invitations/<iid>/call_done`, `/admit`, `/close`.
- Admit: sets `admitted_at`, then `sync_cohort_circles(cohort)`: for every admitted member and the owner, make sure a circle owned by them exists with `cohort_id` set and the cohort's name, whose members are exactly all other admitted members and the owner. Run the same sync on close of an admitted member (removes them). Use the Circles functions for adding/removing members so their side effects (notifications, `entries_from`) stay correct; choose `entries_from` so that only entries shared from now on are visible, and say what you chose.

## Prompts

Three new prompt keys in `PROMPT_DEFAULTS` with files in `backend/prompts/`: `cohort_interview`, `cohort_report`, `cohort_intro`. The texts are provided in `docs/experiments/first-cohort-prompts/` on the branch; wire them in the way existing keys are wired (placeholders, caching rules: keep prefixes byte-identical where the codebase requires it). The person can read these prompts where they can read other prompts; they are not editable per user in this experiment if that is simple to arrange, else leave the default behaviour and note it.

## Web frontend

- `/invitation/:code` (public, no nav): the letter page. Typography of the existing public pages (Cormorant Garamond headings, Outfit body, CSS variables). Sections: the letter (markdown), "What this is" (cohort about), two buttons: "Start the interview" and "Not for me". "Start the interview" stores the code and sends the person to X sign-in with a next URL of `/cohort`; if already signed in it claims and goes to `/cohort`. `<meta name="robots" content="noindex">`. States `claimed`/`declined`/`closed` show one plain sentence instead of the letter.
- `/cohort`: one page that shows the person's current step and only that step's action.
  - not claimed but a stored code exists: claim, then continue.
  - claimed: the letter again (collapsed), and "Start the interview" with a choice of voice or text; opens the existing Voice or Text-mode page on the interview thread. On return, "Continue the interview" and "I'm done: write the report".
  - report: status while drafting; then an editor for the report node (plain textarea with markdown preview is enough; saving uses the normal node edit route), a sentence "Only <founder> will be able to read this. You can take it back later.", and buttons "Send to <founder>" and "Send nothing".
  - after send/skip: the booking link ("Choose a time for a call with <founder>") and what happens next.
  - admitted: the group page: members with their introductions, the question of the week, next call time and link, entries shared in the group (links open the normal node page), and for a member without an introduction: "Draft my introduction from my report" → editor → "Share with the group".
- A signed-in account with a live invitation that has not finished the report step is sent to `/cohort` when it lands on Home right after sign-in (once per session; they can navigate away). NavBar shows a link named after the cohort for invitees and members.
- Admin page: a "Cohorts" section: create/edit the cohort, add an invitation (handle + letter textarea), a table of invitations with state, timestamps, a copy-link button, and the buttons Call done / Admit / Close. No content anywhere.
- The subscription wall (`notLetIn`) must follow the backend's answer for cohort accounts (no separate frontend rule if the backend already reports the state).

## Tests (backend)

At least: flag off → 404s and no behaviour change; unknown invitation code → 404; letter not returned after claim; claim with a different X handle → 403; the founder/admin gets 403/404 on the interview thread and on the unsent report through every content route they could try (node get, thread, export, search, shared-with-me); after Send the founder can read exactly that node and nothing else of the person; Send nothing shares nothing; take-back removes the founder's access; admit creates circles in both directions with the right members, and close removes; a non-admitted invitee gets 403 on `/api/cohort/page`; the subscription gate lets a live invitee in and stops after `free_until`; cohort circles cannot be renamed/deleted/edited by hand; the report task reads only the author's own thread and respects ai_usage none.

## PR body sections

"Decided under Peter's rules", "Heuristics introduced" (every number: caps, limits, counts), "Needs Peter" (only real judgement calls), and a short "How to try it locally" (env vars, creating a cohort and an invitation, walking the steps with two local accounts).
