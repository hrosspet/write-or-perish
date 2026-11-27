# First cohort: the backend API

For the web frontend of the experiment (`docs/experiments/FIRST-COHORT.md`, `FIRST-COHORT-SPEC.md`). Everything here is dark behind `COHORT_V1` (default off). With it off, every route below answers `404 {"error": "Not found"}` and no field marked "COHORT_V1 only" appears.

Conventions:

- Bodies are JSON. Times are ISO 8601 UTC with a `Z` (`"2026-12-03T18:00:00Z"`), or `null`. Days are `"YYYY-MM-DD"`.
- A refused request answers `{"error": "<a sentence to show as it is>", "code": "<code>"}`. Some answers carry more fields; they are listed. Show `error` to the person; branch on `code` or the status, never on the text.
- "Signed in" routes answer `401 {"error": "Authentication required"}` when nobody is signed in.
- The existing app-wide answers can come from any route below: `402 {"error": "no_subscription", "message"}` (payments on, account not let in), `402 {"error": "subscription_required", "message"}`, the spend-cap `402` (`"error": "monthly_spend_limit_reached"`, see `utils/spend.spend_cap_payload`), and `403` for an account waiting for approval.

## Invitation states

One state per invitation, from its timestamps (`utils/cohort.invitation_state`), in this order of precedence:

`declined` > `closed` > `admitted` > `call_done` > `report_sent` | `report_skipped` (the later of the two) > `report_drafted` > `interview_started` > `claimed` > `opened` > `invited`

`report_sent` stays after the person takes the report back (it records the act). Whether the founder can read it now is `report.shared_with_founder` on the person's side.

The page at the link has its own, coarser state: `open` | `claimed` | `declined` | `closed`.

Draft task statuses (`report.task_status`, `intro.task_status`): `null` (never asked) | `"pending"` | `"processing"` | `"done"` | `"failed"` (refused, empty, an error, or AI switched off meanwhile: offer to ask again) | `"nothing_to_draft"` (the interview has nothing the model may read, or the report is empty). Poll the GET route while it is `pending` or `processing`.

---

## 1. The page at the link (no sign-in)

### GET /api/invitation/&lt;code&gt;

Anyone holding the link. Records `first_opened_at` once (a time, nothing else about the visitor). Response headers: `X-Robots-Tag: noindex, nofollow`, `Cache-Control: private, no-store`.

200:

```json
{
  "cohort": {"name": "First Eight", "about": "markdown text or null"},
  "founder": "Peter",
  "state": "open",
  "letter": "markdown text"
}
```

- `founder`: the name the founder signs with (`founder_name`), else his username.
- `letter` is present only while `state` is `"open"`. For `claimed`, `declined`, `closed` it is absent: show one plain sentence. The bound person reads the letter through `GET /api/cohort/me`.

Errors: `404 {"error": "Not found"}` for an unknown code (no other detail).

### POST /api/invitation/&lt;code&gt;/decline

"Not for me". No body, no reason asked. Before the invitation is accepted, anyone holding the link may decline; afterwards only the signed-in account it is bound to.

200: `{"state": "declined"}` (also when it was declined already).

Errors:
- 404 `{"error": "Not found"}`: unknown code.
- 409 `{"error": "This invitation is no longer open.", "code": "cohort_not_open"}`: closed by the founder.
- 403 `{"error": "Only the person who accepted this invitation can decline it.", "code": "cohort_not_yours"}`.

Declining an admitted member takes them out of the group's circles.

---

## 2. The invited person (signed in)

The account must be signed in. Before the claim, `POST /api/cohort/claim` and `GET /api/cohort/me` (and the two invitation routes) are reachable also for an account that is not let in otherwise (payments on, no subscription).

### GET /api/cohort/me

200:

```json
{
  "invitation": { ...Invitation, see below... } ,
  "owner_of": [{"id": 1, "name": "First Eight", "slug": "first-eight"}]
}
```

- `invitation`: the caller's invitation (a live one first, else the newest), or `null`.
- `owner_of`: the cohorts the caller founded (empty list for everyone else).

**Invitation** object (also returned by `/claim`):

```json
{
  "id": 7,
  "state": "claimed",
  "cohort": {
    "name": "First Eight",
    "slug": "first-eight",
    "about": "markdown or null",
    "founder": "Peter",
    "founder_username": "hrosspet",
    "free_until": "2027-01-08"
  },
  "letter": "markdown",
  "booking_url": null,
  "claimed_at": "…Z", "call_done_at": null, "admitted_at": null,
  "declined_at": null, "closed_at": null,
  "interview": {
    "node_id": 123,
    "started_at": "…Z",
    "tip_node_id": 130,
    "tip_is_ai": true,
    "tip_status": "completed",
    "open": {"text": "/node/130", "voice": "/voice?parent=130"}
  },
  "report": {
    "node_id": 140,
    "task_status": "done",
    "drafted_at": "…Z",
    "sent_at": null,
    "skipped_at": null,
    "shared_with_founder": false
  },
  "intro": {"node_id": null, "task_status": null, "shared_with_group": false}
}
```

- `booking_url`: the founder's calendar link, only once the report was sent or skipped; else `null`.
- `interview.node_id`: the interview thread's prompt node (root), or `null` before it starts. `tip_node_id`: the newest node of the person's own in that thread (`null` when there is none). `tip_is_ai`: the tip is an AI reply. `tip_status`: its `llm_task_status` (`"pending"`, `"processing"`, `"completed"`, `"failed"`), `null` for a person's message. `open`: where to send the person to continue (see section 5), or `null`.
- `report.node_id` / `intro.node_id`: the person's own node, `null` when there is none (or it was deleted).
- `report.shared_with_founder`: the founder can read the report right now (sent and not taken back).
- `intro.shared_with_group`: the introduction is shared with the member's group circle.

### POST /api/cohort/claim

Body: `{"code": "<code from the link>"}`. Binds the invitation to the signed-in account. Idempotent for the account it is bound to.

200: `{"invitation": Invitation}`.

Errors:
- 404 `{"error": "Not found"}`: unknown code.
- 409 `cohort_not_open`: "This invitation is no longer open." (declined or closed)
- 409 `cohort_taken`: "This invitation was accepted from another account."
- 409 `cohort_owner`: "You started this group. The invitation is for someone else."
- 409 `cohort_other`: "You have already accepted another invitation."
- 403 `cohort_sign_in_with_x`: "This invitation is for @mara. Sign in with X as @mara to accept it." (the account has no X sign-in)
- 403 `cohort_wrong_account`: "This invitation is for @mara. Sign in with that X account to accept it."

The X check: when the invitation was made for a handle, the account's X id must be the one the handle resolved to when the invitation was made (or, if that id is unknown, the X handle seen at the account's last X sign-in, case ignored). An invitation made without a handle can be accepted by any signed-in account holding the code.

To send someone to X sign-in and back to `/cohort`: `GET /auth/login?next=/cohort` (the existing X sign-in keeps `next` through the round trip).

### POST /api/cohort/interview/start

Body: `{"mode": "text" | "voice"}` (default `"text"`).

Starts the interview: a private thread of the person's own whose root is a prompt node with key `cohort_interview` (their own Voice or Text agentic prompt, then the interview section with the cohort's about, the founder's name and their letter filled in), and the AI writes the first message on the person's own model. Idempotent: once the thread has anything below its root, it answers that thread and writes nothing.

202 (started now, or a failed opening asked again):

```json
{
  "created": true,
  "interview_node_id": 123,
  "mode": "text",
  "opening_node_id": 124,
  "task_id": "…",
  "open": {"text": "/node/124?awaitLlm=124", "voice": "/voice?resume=124"}
}
```

200 (already started): same keys with `"created": false`, `"opening_node_id": null`, `"task_id": null`, and `open` pointing at the thread's tip.

202 with the thread kept and the opening skipped: `"opening_node_id": null`, `"task_id": null`, `"open": null`, plus either `"llm_error": "<text>"` (the person's own prompt asks for an export their plan does not allow) or `"llm_error": "<text>", "llm_error_code": "<code>"` (thread too long, or too many replies in progress: `thread_size.reply_skipped_fields`). Show the text; the person can try again.

Errors:
- 404 `cohort_none`: "You have no invitation to a group."
- 409 `cohort_not_open`: "This invitation is no longer open."
- 400 `{"error": "mode must be text or voice"}`.
- 403 `ai_usage_none`: "Your account is set to No AI, so Loore can't hold the interview. You can still send a report you write yourself, or send nothing." Offer `POST /report/draft {"blank": true}` and `POST /report/skip`.
- 403 `voice_unavailable`: "Voice mode is not available on this account. Choose text."
- 402 `subscription_required` / spend cap, as everywhere (nothing is kept).

### POST /api/cohort/report/draft

Body (all optional): `{"regenerate": true}` for a new draft over an unsent one (the old node stays among the person's entries; it is not deleted), `{"blank": true}` for no model call: a node with the report's headings for the person to write themselves.

- With a report already there and no `regenerate`: 200 with the current report, nothing new (also with `blank`).
- While a draft is being written: 202 with the current status, nothing new queued.
- Model draft queued: 202. Poll `GET /api/cohort/report`.
- Blank: 201, `task_status` `"done"`.

Response (all cases): `{"report": Report}` where Report is the `report` object of the Invitation above.

The draft reads only the person's own interview thread (their messages and the interviewer's on the thread's latest path, without anything marked No AI) and is saved as a new private node owned by the person (`ai_usage` `"chat"`).

Errors:
- 404 `cohort_none`, 409 `cohort_not_open`.
- 409 `cohort_report_sent`: "The report has been sent. Take it back first (set it to Only me) to draft a new one."
- 409 `cohort_interview_first`: "Start the interview first." (model draft only)
- 403 `ai_usage_none`: "Your account is set to No AI, so Loore drafts nothing. You can write it yourself."
- 402 `subscription_required` / spend cap.
- 503 `{"error": "Loore could not start the draft. Try again in a moment."}` (the queue could not be reached; status set to `"failed"`).

### GET /api/cohort/report

200: `{"report": Report, "founder": "Peter", "founder_username": "hrosspet"}`.

Errors: 404 `cohort_none`.

The report's text is read and saved through the normal node routes (section 6).

### POST /api/cohort/report/send

No body. "Send to Peter": shares the report node with exactly one named reader, the founder, through the same Circles step as `PUT /api/nodes/<id>/audience` (any other audience the node had is replaced). He finds it under Shared with you, with the usual note. Sets `report_sent_at`.

200: `{"report": Report, "booking_url": "https://…" | null}`.

Errors:
- 404 `cohort_none`, 409 `cohort_not_open`.
- 409 `cohort_no_report`: "There is no report yet."
- 400 `cohort_report_empty`: "The report is empty. To send nothing, choose Send nothing."
- 409 `cohort_no_founder`: "This group has no founder to send it to."
- Any answer of the Circles share step, passed through as it is (it runs the audience route's own gate first, answering 404 `{"error": "Node not found"}` when it refuses; for example 409 `"code": "quotes_not_shared"` when the report quotes an entry the founder cannot read, 400 `circle_unknown_name`).

The sentence under the button ("Only Peter will be able to read this. You can take it back later.") is the frontend's.

Take back: set the node to Only me with the normal edit (section 6). `report.shared_with_founder` then reads `false`; the state stays `report_sent`.

### POST /api/cohort/report/skip

No body. "Send nothing": nothing is shared. Sets `report_skipped_at` (each press moves it to now).

200: `{"report": Report, "booking_url": "https://…" | null}`.

Errors:
- 404 `cohort_none`, 409 `cohort_not_open`.
- 409 `cohort_report_sent`: "The report has been sent. Take it back first (set it to Only me) if you want to send nothing."

### POST /api/cohort/intro/draft

Admitted members only. No body. Drafts a short introduction from the CURRENT text of the member's report node (their edited version), into a new private node of theirs. Poll `GET /api/cohort/intro`.

202: `{"intro": Intro}` (Intro is the `intro` object of the Invitation above). Also 202, nothing new queued, while one is being written.

Errors:
- 404 `cohort_none`, 409 `cohort_not_open`.
- 403 `cohort_members_only`: "Only members of the group can do this."
- 409 `cohort_intro_shared`: "Your introduction is already shared with the group."
- 409 `cohort_no_report`: "There is no report to draft an introduction from. You can write your introduction yourself."
- 403 `ai_usage_none`: "The report is set to No AI, so Loore can't draft from it. You can write your introduction yourself." or the account's No AI sentence above.
- 402, 503 as for the report.

### GET /api/cohort/intro

200: `{"intro": Intro}`. Errors: 404 `cohort_none`, 409 `cohort_not_open`.

### POST /api/cohort/intro

Admitted members only. Body: `{"node_id": 150, "share": true}`. Marks one of the member's own entries as their introduction. It must be shared with their group circle; `share: true` shares it there in the same call (added to whatever the entry is shared with already).

200: `{"intro": Intro}`.

Errors:
- 400 `{"error": "node_id is required"}`.
- 404 `{"error": "Not found"}`: no such node, or deleted.
- 403 `cohort_not_yours`: "That entry is not yours." (also a prompt node)
- 403 `cohort_members_only`.
- 409 `cohort_intro_not_shared`: "Share it with the group first."
- Any answer of the Circles share step, passed through: for example 404 `{"error": "Node not found"}` for an entry the audience route would not share (an AI reply from a thread now closed to the member), 400 `circle_reply_audience` for a node written in someone else's thread.

### GET /api/cohort/page

The group page, for its members: admitted members and the founder. Query: `?cohort=<slug>` (optional; without it, the caller's own group: their admitted invitation's, else the first one they founded).

200:

```json
{
  "cohort": {
    "id": 1, "slug": "first-eight", "name": "First Eight",
    "about": "markdown or null",
    "founder": "Peter", "founder_username": "hrosspet",
    "question": "text or null", "question_set_at": "…Z" ,
    "next_call_at": "…Z", "call_url": "https://…",
    "free_until": "2027-01-08"
  },
  "is_founder": false,
  "circle_id": 33,
  "members": [
    {"username": "hrosspet", "intro_node_id": null, "is_founder": true},
    {"username": "mara", "intro_node_id": 150, "is_founder": false}
  ],
  "entries": [
    {"node_id": 160, "author_username": "mara", "shared_at": "…Z",
     "created_at": "…Z", "title": "First line", "preview": "Up to 160 characters…",
     "last_reply_by": "ben"}
  ]
}
```

- `circle_id`: the caller's own group circle. To share an entry with the group, share it with this circle id (it is also in `GET /api/circles`, marked `cohort`).
- `members`: the founder first, then by username. `intro_node_id` only when the introduction is shared with the group and the caller can open it; else `null`.
- `entries`: newest share first, at most 50: what members shared through the group's circles that the caller reads, and the caller's own entries shared with their group circle. `title`/`preview` as Shared with you computes them. Open an entry at `/node/<node_id>`.

Errors: 403 `cohort_members_only`: "Only members of the group can do this." (also for an unknown slug).

---

## 3. The founder (admin routes)

Signed in, admin, and the cohort's owner. A non-admin gets `403 {"error": "Forbidden"}`; a cohort the caller does not own (or that does not exist) answers `404 {"error": "Not found"}`. These routes return states, timestamps and the founder's own letters: never a node's text, never the id of an interview, report or introduction.

**Cohort** object:

```json
{
  "id": 1, "slug": "first-eight", "name": "First Eight",
  "about": "markdown or null",
  "founder_name": "Peter", "founder": "Peter",
  "booking_url": "https://…", "call_url": "https://…",
  "next_call_at": "…Z", "question": "…", "question_set_at": "…Z",
  "free_until": "2027-01-08", "created_at": "…Z",
  "state_counts": {"claimed": 2, "invited": 5}
}
```

`founder_name` is the stored field (may be `null`); `founder` is what pages show.

**AdminInvitation** object:

```json
{
  "id": 7, "x_handle": "mara", "username": "mara",
  "state": "report_sent",
  "link": "/invitation/Q2x…",
  "letter": "markdown",
  "created_at": "…Z", "first_opened_at": "…Z", "claimed_at": "…Z",
  "interview_started_at": "…Z", "report_drafted_at": "…Z",
  "report_sent_at": "…Z", "report_skipped_at": null,
  "call_done_at": null, "admitted_at": null,
  "declined_at": null, "closed_at": null
}
```

`link` is a path: the full link is the site's origin + `link`. `username` is the bound account's (`null` before the claim). `x_handle` is stored lowercased, without `@` (`null` for an invitation made without one).

### GET /api/admin/cohorts

200: `{"cohorts": [Cohort, …]}` (the caller's own, oldest first).

### POST /api/admin/cohorts

Body: `{"name", "slug"?, "about"?, "founder_name"?, "booking_url"?, "call_url"?, "next_call_at"?, "question"?, "free_until"?}`.

- `name`: required, at most 60 characters (the members' circles carry it).
- `slug`: optional, `[a-z0-9][a-z0-9-]{0,63}`; made from the name when absent.
- `about`: at most 20,000 characters. `question`: at most 5,000. `founder_name`: at most 64.
- `booking_url`, `call_url`: must start with `https://` or `http://`, at most 512.
- `next_call_at`: ISO date and time (a `Z` or offset is converted to UTC). `free_until`: `YYYY-MM-DD`, the last free day; without it nobody is let in for free (payments on).
- Empty strings and `null` clear a field.

201: `{"cohort": Cohort}`. Errors: 400 `{"error": "<field> …"}` (the sentence names the field), 400 `{"error": "slug can hold a-z, 0-9 and -."}`, 409 `{"error": "That slug is taken."}`.

### GET /api/admin/cohorts/&lt;id&gt;

200: `{"cohort": Cohort}`.

### PUT /api/admin/cohorts/&lt;id&gt;

Body: any of the POST fields except `slug`; only the fields present change. A changed `question` sets `question_set_at` to now. A changed `name` renames the members' group circles.

200: `{"cohort": Cohort}`. Errors: 400 as for POST.

### GET /api/admin/cohorts/&lt;id&gt;/invitations

200: `{"invitations": [AdminInvitation, …]}` (oldest first).

### POST /api/admin/cohorts/&lt;id&gt;/invitations

Body: `{"x_handle": "mara", "letter": "markdown", "x_lookup": false}`.

- `letter`: required, at most 20,000 characters.
- `x_handle`: optional (with or without `@`). With it, the handle is approved through the admin whitelist's own function: an account with the handle's X id is made (or the existing one is used) and approved, so the person's X sign-in lands in it. Without it, any signed-in account holding the link may accept (for trying it locally).
- `x_lookup`: `true` only after the founder agreed to the paid X lookup (the same dialog as the whitelist; the price comes in the 404 below).

201: `{"invitation": AdminInvitation, "link": "/invitation/<code>"}`. Loore sends nothing: the founder sends the link himself.

Errors:
- 400 `{"error": "letter is required."}` / length.
- 409 `{"error": "There is already an invitation for @mara in this group."}`.
- The whitelist's answers, with `"code": "cohort_handle"` added: 400 (bad or reserved handle, or a username taken by an account that is not this X account), 404 `{"error", "reason": "not-in-archive", "x_lookup_cost_usd": <price in USD>, "code"}` (offer the paid lookup), 404 `not-on-x`, 409 `ambiguous`, 502 `archive-error` / `x-error`, 409 (a concurrent request).
- 409 `cohort_account_blocked`: "@mara's account is blocked. Unblock it in Users first."
- 409 `cohort_account_deleted`: "@mara's account is being deleted."

### PUT /api/admin/cohorts/&lt;id&gt;/invitations/&lt;iid&gt;

Body: `{"letter": "markdown"}`. Only while nobody has accepted the invitation.

200: `{"invitation": AdminInvitation}`. Errors: 409 `cohort_not_open`, 409 `cohort_claimed` ("The letter can't be changed after the invitation was accepted."), 400 letter required / length.

### POST /api/admin/cohorts/&lt;id&gt;/invitations/&lt;iid&gt;/call_done

No body. Sets `call_done_at` (once). 200: `{"invitation": AdminInvitation}`.

Errors: 409 `cohort_not_open`; 409 `cohort_not_claimed` ("Nobody has accepted this invitation yet.").

### POST /api/admin/cohorts/&lt;id&gt;/invitations/&lt;iid&gt;/admit

No body. Sets `admitted_at` (once) and syncs the group's circles: every member and the founder get a circle named after the cohort holding all the other members; the person added reads only what is shared with the group from then on. 200: `{"invitation": AdminInvitation}`.

Errors: as `call_done`.

### POST /api/admin/cohorts/&lt;id&gt;/invitations/&lt;iid&gt;/close

No body. Sets `closed_at` (once; a declined invitation stays declined). An admitted member leaves every circle of the group and their own group circle is deleted. Loore tells nobody. 200: `{"invitation": AdminInvitation}`.

---

## 4. Changes to existing routes (COHORT_V1 only)

- `GET /api/dashboard/` → `user.cohort`: `null`, or `{"name", "slug", "role": "invited" | "member" | "founder", "state": <invitation state or null for a founder>, "report_step_done": bool}`. Present for an account with a live invitation, or a founder. Use it for the NavBar link (named after the cohort) and for the once-per-session redirect from Home to `/cohort` while `role` is `"invited"` or `"member"` and `report_step_done` is `false`.
- `user.subscription.cohort_free_until` (payments on): the free period's last day, or `null`. During it `user.subscription.state` is `"set_by_hand"`, so `notLetIn` already lets the account in; no separate frontend rule is needed.
- `user.circles_enabled` is `true` for a founder and for every account bound to a live invitation.
- `GET /api/circles`: a circle Loore keeps for a group has `"cohort": {"name", "slug"}`. Renaming or deleting it, adding or removing its people (and the add preview) answer `409 {"error": "Loore keeps this circle for First Eight: its name and its people follow the group.", "code": "cohort_circle"}`. Hide those controls.
- `POST /api/circles/leave` and `/leave/preview`, for an entry read through a group circle: `409 {"error": "You read this as a member of First Eight. To stop seeing someone's entries, hide them. To leave the group, tell Peter.", "code": "cohort_circle"}`.
- `GET /api/prompts/` and `GET /api/prompts/<key>`: every prompt now has `"editable": bool`. The keys `cohort_interview` ("Interview"), `cohort_report` ("Interview report"), `cohort_intro` ("Group introduction") are listed for a founder and invited people only, with `"editable": false`; `PUT`, revert and acknowledge answer `403 {"error": "This prompt can't be changed."}`. A person's `cohort_interview` version (made when their interview starts) has `"generated_by": "cohort"` and holds their whole interview prompt.

## 5. Opening the interview in the existing Text and Voice pages

The interview is an ordinary agentic thread (prompt key `cohort_interview`, treated like `voice` / `textmode`: tools, mode notes). Use the `open` paths the API returns; they follow the existing pages:

- Just started, text: `/node/<opening_node_id>?awaitLlm=<opening_node_id>` (the thread page waits for the AI's first message, as Write does after `/textmode/start`).
- Just started, voice: `/voice?resume=<opening_node_id>` (Voice plays the first message, then the record button continues under it).
- Continuing (`GET /api/cohort/me` → `invitation.interview.open`), when the tip is finished: text `/node/<tip_node_id>` (reply from the thread page), voice `/voice?parent=<tip_node_id>`. When the tip is an AI reply still being written: `?awaitLlm=` / `?resume=` as above.
- Text replies from the thread page use the existing reply flow (`POST /api/nodes/` with `parent_id`, then the automatic reply). Voice turns use `POST /api/voice/from-node/<id>` through the Voice page. Neither attaches another prompt: the interview prompt above counts as the thread's agentic prompt.
- The thread page shows the root's prompt (the person's own prompt and the interview section). The thread is private; the person may share parts of it like any entry.

"I'm done: write the report" = `POST /api/cohort/report/draft`, then poll `GET /api/cohort/report`.

## 6. Reading, saving and taking back the report (existing node routes)

- Read: `GET /api/nodes/<report.node_id>` → `content` (markdown), plus the usual node fields (`privacy_level`, `audience` once shared).
- Save the person's edit: `PUT /api/nodes/<id>` with `{"content": "<new text>", "base_content": "<the text the editor loaded>"}`. `content` is required. With `base_content`, a text changed elsewhere meanwhile answers `409 {"code": "content_changed"}` and nothing is written. Earlier texts are kept as versions. The draft never overwrites an edit: a regenerated report is a new node.
- Blank a section or all of it: save the text without it. Sending an empty report is refused (use Send nothing).
- Take back after Send: `PUT /api/nodes/<id>` with `{"content": "<current text>", "privacy_level": "private"}`. The founder can no longer open it, and it leaves his Shared with you.
- The introduction is read and saved the same way, and shared through `POST /api/cohort/intro {"node_id", "share": true}` (or the normal share step with the member's `circle_id`, followed by `POST /api/cohort/intro {"node_id"}`).
