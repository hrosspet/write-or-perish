# First cohort experiment: PR notes

Branch `experiment/first-cohort` (Circles merged with main). Dark behind `COHORT_V1`, default off. The design is `FIRST-COHORT.md`, the build specification `FIRST-COHORT-SPEC.md`, the API for the frontend `FIRST-COHORT-API.md`. The backend was built first; the web frontend second (its notes are the "Frontend" parts of each section below).

What is built (backend): the flag; the `Cohort` and `CohortInvitation` tables and `Circle.cohort_id`; the public invitation page's two routes; the invited person's steps (claim, interview, report draft / send / send nothing, introduction, group page); the founder's admin routes (cohorts, invitations, call done, admit, close); the circles Loore keeps for a group; the access hooks (Circles, free period, approval); the three prompts; the draft task module (`backend/tasks/cohort_report.py`, registered in `celery_app.py` and given the interactive queue); `backend/tests/test_cohort.py`.

What is built (web): `/invitation/<code>` (`pages/InvitationPage.js`), `/cohort` (`pages/CohortPage.js` with `components/cohort/`: the steps, the group page, the introduction, a textarea with a markdown preview), Admin → Cohorts (`components/AdminCohorts.js`), the once-per-session send to `/cohort` (`components/cohort/CohortArrival.js`), the NavBar link, and the step logic in `utils/cohort.js`; jest tests for the step logic, both pages and the admin table.

## Decided under Peter's rules

- **The person's own model, for the interview and both drafts** ("A user's model is theirs"). The interview's opening and the drafts use `llm_nodes.pick_model_for_generation`, the path every reply takes: the thread's model, else the person's preferred model. The spec said to follow `tasks/poll_draft.py` for model choice; a poll draft runs on the model the admin chose for the poll, which could be another provider than the person's.
- **The drafts are live calls, not the Batch API** ("Latency counts as quality"; Batch is for jobs nobody waits for). The person waits on the page for the report and the introduction.
- **The drafts are billed to the person's own account** (`request_type` `cohort_report` / `cohort_intro`, `request_ref` `cohort_invitation:<id>`), like a reply they ask for, so their spend cap counts them. Poll drafts bill a system account because the developer asks for them; here the person asks for their own document.
- **The report and the introduction are new private nodes of the person's own** ("The user's edit wins"). `node_type` "user", owned by them, top level. Asking again never overwrites: with a report there, the draft route answers that one; `regenerate` makes a new node and leaves the old one among their entries. The intro reads the report's current text, their edit.
- **Drafts are Chat at most** (training licence rule). A node a model wrote from the person's words is `ai_usage` "chat" and carries the strongest `unlicensed_input` mark of what it was written from.
- **The report reads only the person's own interview thread, nothing marked No AI** ("No AI means no AI", "Nobody but the author reads"). The transcript is the path to the thread's newest node of theirs; only their own nodes (their messages, the AI replies they asked for), alive, finished, AI-readable. An interview root that is not theirs or not an interview gives nothing to draft. The task checks the account's AI setting and its AI replies again when it runs.
- **An account set to No AI can still take part.** The interview and the drafts are refused with a sentence; `POST /report/draft {"blank": true}` gives the report's headings without a model call, and Send works as for any report ("No AI means no AI", "Show the choices").
- **Send is the existing Circles share step** ("Nothing leaves without your say"). `PUT /api/nodes/<id>/audience`'s body moved into `routes/nodes.share_entry`; the route and `POST /api/cohort/report/send` both call it, so the quote rule, the note and the audience rows are the same. The report's audience is replaced by exactly the founder. The audience route's gate (a node the person may edit, Circles on for them, not an AI reply from a thread now closed to them) is checked inside `share_entry`, so Send and the introduction's share cannot skip it.
- **Take-back is the normal edit to Only me.** The founder's line keeps the state `report_sent` (it records the act; he can no longer open the entry). The person's own view says live whether he can read it (`report.shared_with_founder`).
- **The founder's routes give states, timestamps and his own letters only.** No node id of the interview, the report or the introduction, no node text. They are owner-only: another admin gets 404 for a cohort that is not theirs ("Nobody but the author reads").
- **The interview prompt is visible to the person, and holds nothing hidden** (candidate rule of 2026-11-11: what a model reads for a user, that user may read). It is the person's own Voice or Text prompt (with their edits, if any) followed by the interview section with the cohort's about, the founder's name and the letter filled in, saved as a prompt version of their own (`generated_by` "cohort") that the thread's root links; the thread page shows it. The founder's text goes in with `{`/`}` turned into `(`/`)`, so a brace in a letter never acts as a placeholder in the person's prompt.
- **The interview thread is a normal agentic session.** `cohort_interview` joins `AGENTIC_PROMPT_KEYS`, so tools, proposal parsing and the Voice/Text mode notes work, and Voice or Text turns under it attach no second prompt.
- **The cohort prompts are read, never edited.** Listed on the Prompts page for a founder and invited people only, `"editable": false`, and every change route answers 403 "This prompt can't be changed." The drafts always run on the files.
- **Approval goes through the whitelist's own function.** `routes/admin.whitelist_x_handle` (the whitelist route's body, moved out unchanged) makes the account with the handle's X id; an account that already holds that id is used as it is (a new `existing_ok` argument; the route's own behaviour and order are unchanged). With payments off the whitelist leaves new accounts unapproved for the pre-fill workflow, so the invitation approves the account by hand, as Activate does. A blocked or deleting account is refused with a sentence.
- **Claim matches the X id when it is known.** A handle can change hands; the id it resolved to when the invitation was made is compared with the signed-in account's X id. Without a known id, the X handle seen at the account's last X sign-in is compared, case ignored (the spec's rule).
- **A claim binds once, and an invitation is accepted once.** Two claims of one code at the same moment: a conditional update binds it to one account, the other is told it was accepted from another account. An invitation with `claimed_at` set counts as accepted whatever its account, so its letter is never served at the link again and nobody else can take the first person's place (review M1).
- **Decline after the claim needs the bound account** ("Nothing leaves without your say"): only the person declines for themselves once they accepted. Declining as a member takes them out of the group's circles.
- **An invitee who already pays keeps their own subscription** (review W5): the free period applies only to an account with no subscription that gives access (trialing or active) and no alpha free month. Such an account's level ceiling, paid period and Stripe billing stay as they were; `subscription.cohort_free_until` is `null` for it.
- **A draft whose worker died can be asked for again** ("Problems show"; review M2). The time a draft was asked for is kept beside its status; a status still pending or processing after `DRAFT_STALE_AFTER` reads as `failed` everywhere (the pages then say it did not work and offer to ask again, or to write it by hand). The task is queued with that time and acts only while the invitation's draft is still that one: a task that comes late calls nothing, and one whose draft was given up on while the model wrote it saves no node (the call is billed and logged).
- **A double click makes one paid call** (review W2): the status moves to pending in one conditional update, and only the request that moved it queues the task.
- **"Not for me" stays one click; the founder can undo it** (review W3, frontend Needs Peter 5). `POST .../invitations/<iid>/reopen` (Admin → Cohorts → Reopen) clears `declined_at` on an invitation declined before anyone accepted it; the link shows the letter again. One accepted once, or closed, is not reopened.
- **An invitation needs an X handle** (review W6): without one, whoever opens the link first would accept it and get the free period. Handle-less invitations are made only where `COHORT_ALLOW_OPEN_INVITATIONS` is on (default off), for trying it locally.
- **The free period** (spec): an account bound to a live (not declined, not closed) invitation whose cohort's `free_until` is today or later counts as a plan set by hand: web, iPhone and the 402 gate follow `User.plan_set_by_hand`. A cohort without `free_until` lets nobody in for free. The same case is in the SQL versions of "paid features" (`User.paid_features_clause`, `User.profile_eligible_query`), so the background jobs (profile updates, recent context, embeddings) treat such an account as one with access ("full access, AI included").
- **Circles for cohort people** (spec): on for every founder and every account bound to a live invitation, in `privacy.circles_enabled_for`, only while `COHORT_V1` is on. One query per database session, forgotten after every flush, commit and rollback, as the Circles sets are.
- **The circles Loore keeps can't be changed by hand, and a member can't "leave" one.** Rename, delete, add, remove and the add preview answer 409 with a sentence. Leaving an entry read through a group circle answers 409 with the way out: hide the person (the per-reader mute still works), or tell the founder. Peter's "leave" (2026-11-23) is for an audience a person was put in without being asked; joining the group was the consent (walkthrough: "accepting the invitation is the consent"), and a leave would be undone by the next sync.
- **Closing a person who was not admitted does not take back a report they sent.** Sending was their act; they take it back themselves (taking back works with Circles off). Their Circles access ends with the invitation, so they share nothing new.
- **A deleted account leaves the invitation behind without it, closed** ("deleting a user's content deletes what they made, lists about other people included; what other people made about them stays"): the invitation is the founder's record, so `delete_identity` closes it (so it is never accepted again, review M1), takes a member out of the group's circles, and clears its account and node ids. A founder's deleted account leaves the cohort without an owner.
- **The page at the link** records only `first_opened_at`, and answers with `X-Robots-Tag: noindex, nofollow` and `Cache-Control: private, no-store` (it carries a letter about a person).
- **Loore sends nothing to anyone.** No email, no X message; the founder sends the link himself.

Frontend:

- **"Start the interview" goes straight to Sign in with X** (`/auth/login?next=/cohort`), not to the sign-in page with its email choice: the claim checks the X account. Signed in already, it goes to `/cohort` at once. The code is kept in this browser under its own key, the way the referral code is kept (`utils/invite.codeStore`, now shared by both); unlike the referral code, a sign-in does not drop it. The claim drops it, and so do every refusal of the claim, "Not for me" and signing out (every sign-out link), so a code never follows another account that signs in on the same browser (frontend review 5, 6). Where the browser keeps nothing (some private windows), it rides in the address (`/cohort?code=`).
- **"Not for me" declines at once, with no dialog** (walkthrough: "asks nothing more"); the founder's Reopen undoes a mistaken one (above).
- **A claim refused for the wrong X account says to sign out, open the link again and sign in with the X account it names**; opening the link keeps the code anew.
- **What is sent is what the editor shows** ("Nothing leaves without your say", "The user's edit wins"). Send saves the editor's text first, on the text it loaded (`base_content`), so a newer text saved elsewhere is never overwritten; Send nothing also saves the person's edits first, so they stay in their own entry.
- **Taking the report back is on the page** ("You can take it back later" is promised under the button): the call step says whether the founder can read it, with "Take it back" (a dialog, then the normal edit to Only me).
- **The report is sent only from its editor, with the text on screen** (hard rule "Nothing leaves without your explicit, previewed act"; frontend review 1). Where the founder can't read the report (after Send nothing, or after taking it back), the call step offers "Read and send the report", which opens the report's editor with Send, Send nothing and Back; it never sends by itself.
- **"Loore wrote this draft from your interview" only over a draft a model wrote** (frontend review 4). A new column, `CohortInvitation.report_written_by_model` (True when the task saves a draft, False for "Write it myself"), answered as `report.written_by_loore`; a report written by hand reads "This is your report. Write what you want Peter to know; leave any part empty."
- **The way back from the interview is named everywhere the same** (frontend review 2): the start step says "When you are done, come back to this page with the "First Eight" link at the top of the page, and press "I'm done: write the report""; the interview prompt gets a `{cohort_name}` placeholder and tells the person the same at the end, instead of "press I'm done" on a page that has no such button; and the Text and Voice pages show "Back to First Eight" above the thread while they hold the interview that `/cohort` opened (a mark kept for the tab, dropped on any other page).
- **The report step offers to continue the interview** (by voice or text), then a new draft; also when the interview gave nothing to draft, where "Ask again" now shows too.
- **Why the interview could not start stays on screen** after the page moves from the start step to the interview step, with "You can try again with the buttons above."
- **An account that accepted its invitation but is not let in** (payments on; the cohort's free period not set, or over) reads one sentence above the subscribe page saying which; "Opening your invitation…" stays until the user is read again after the claim, so the subscribe page does not flash. **An account waiting for approval** (payments off) with a kept code reads one sentence on the waiting page: the invitation is for the X account the founder sent it to; sign out, open the link again and sign in with that one (from the public invitation route; the approval gate keeps it from `/api/cohort/*`).
- **"Write it myself" for the introduction makes one entry**: a retry after a refused share updates the entry made on the first try.
- **A new draft is asked for behind a dialog** that says the earlier draft stays among the person's entries (the server never overwrites).
- **The introduction can be written without a draft** ("Write it myself"): a private entry of theirs, then shared with the group in the same press. Needed for an account set to No AI and for an empty report.
- **One sentence where a member shares with the group**: "Everyone in First Eight can read it, including people who join the group later." On the introduction's share step, on the group page's line about sharing, and in the Circles audience panel of any entry when a group circle is chosen.
- **The founder's `/cohort` is the group page**, without the introduction step (the API has none for founders), with a line pointing to Admin → Cohorts.
- **A visit that starts on `/cohort` opens neither the updates modal nor the both-providers question**, as for `/welcome`: an invited person arrives there from X and goes into the interview ("She does not see the home page first").
- **The once-per-session send to `/cohort`** happens only when the page load arrived on Home or `/dashboard` (where a sign-in lands), for an invited person or member whose report step is open, or with a kept code. Once per tab session; nothing sends them back.
- **"Before each call, the page asks her what she wants to bring"**: one sentence under the next call's time, no form.
- **The Admin tab shows only while `COHORT_V1` is on** (the current user then carries `cohort`), and the founder's `datetime-local` input is sent as UTC with a `Z`; pages show the call in the viewer's own time zone with the zone named.
- **Close and Admit ask in a dialog.** Close says what it ends (the free period, the group's circles). Admit says that the person can at once read everything the group has shared so far, introductions included, and gets a note about the newest entries, and that only Close (which also ends the free period) undoes it (frontend review 3). Call done acts at once.

## Decided by the coordinator, for Peter to object to

- **Members admitted later read what the group shared before, introductions included.** Scenario: Mara is admitted on Monday and shares her introduction on Tuesday; Ben is admitted on Thursday and reads it, and the group page shows it beside her name. The group is the audience the author chose when sharing, and the letter says the group sees what members share with it. `utils/cohort.NEW_MEMBERS_READ_EARLIER_ENTRIES` is now `True`; only the circles Loore keeps for a cohort use it, so a person added to an ordinary circle by its owner still reads new entries only unless the owner chooses otherwise. Wherever a member shares with the group, the page says that people who join later will also be able to read it. Side effect of the Circles step this uses: on admission the new member gets one note per member whose group circle already holds entries (for its newest entry), as when an owner adds someone with "Also show earlier entries". Test: `test_a_member_admitted_later_reads_what_the_group_shared_before`.

## Heuristics introduced

| Number | What | Where |
|---|---|---|
| 50 | entries on the group page (the spec's number) | `utils/cohort.PAGE_ENTRIES` |
| `True` | a member admitted later also reads what the group shared before (coordinator's decision, above; the spec said `False`) | `utils/cohort.NEW_MEMBERS_READ_EARLIER_ENTRIES` |
| 2,000 tokens | output cap of the report draft: about 700 words of text plus room for a model's reasoning, which counts against the same cap (#366). The prompt itself asks for at most 500 words | `utils/cohort.REPORT_MAX_TOKENS` |
| 1,000 tokens | output cap of the introduction draft (the prompt asks for at most 150 words) | `utils/cohort.INTRO_MAX_TOKENS` |
| 2,000 | nodes of the interview thread walked to find where it ends | `utils/cohort.MAX_THREAD_NODES` |
| 24 bytes | invitation code, `secrets.token_urlsafe(24)` (32 characters; the spec's number) | `utils/cohort.new_code` |
| 20,000 / 20,000 / 5,000 characters | letter / cohort about / question | `routes/cohort.MAX_LETTER_CHARS`, `MAX_ABOUT_CHARS`, `MAX_QUESTION_CHARS` |
| 60 characters | cohort name (a circle's name holds at most 60) | `routes/cohort.MAX_NAME_CHARS` |
| 64 / 512 characters | founder name / booking and call links | `routes/cohort.MAX_FOUNDER_NAME_CHARS`, `MAX_URL_CHARS` |
| 48 characters, `-2`, `-3`… | a slug made from the name, made unique | `utils/cohort.slugify` |
| " (group)" | added to a member's group circle name when they have a circle of their own by that name | `utils/cohort._circle_name` |
| 10 minutes | a report or introduction draft still pending or processing this long after it was asked for reads as failed (the model call takes up to about a minute; the rest covers a queue behind other replies) | `utils/cohort.DRAFT_STALE_AFTER` |
| 3 s | how often a page asks whether a report or introduction draft is ready | `components/cohort/CohortSteps.DRAFT_POLL_MS` |
| 30 days | how long the invitation code waits in the browser for a sign-in (the referral code's number) | `utils/cohort.COHORT_CODE_MAX_AGE_DAYS` |
| `/`, `/dashboard` | the page loads that send an invitee with an open report step to `/cohort` (once per tab session) | `utils/cohort.SIGN_IN_LANDINGS` |
| 11rem | widest NavBar link for the cohort's name (longer names end in "…", the full name in its tooltip) | `components/NavBar.js` |
| "about twenty minutes" | how long the start step says the interview takes (the walkthrough's number) | `components/cohort/CohortSteps.StartStep` |

## Needs Peter

1. **The trial's model-cost ceiling is not applied to cohort accounts.** Scenario: Mara talks to her model every day for the six weeks. As built she counts as "plan set by hand", so her ceiling is the global monthly default (`PER_USER_MONTHLY_LIMIT_USD`; none when it is 0), not the trial's $30 in all. The trial's ceiling was not a small change: its block tells the person it "lifts with your first payment", which is wrong for someone in a free experiment, and its start date comes from Stripe. Recommendation: set a per-user limit for each member in the Users tab (the existing admin control), for example the Standard level's monthly ceiling. If you say nothing: the global default applies.
2. *(Moved to "Decided by the coordinator, for Peter to object to": members admitted later read what the group shared before.)*
3. **Close ends the free period at once.** Scenario: after the call you decide not to admit Ben and press Close. With payments on, Ben is then not let in and has no AI, although the walkthrough says "a person you do not admit keeps their account for the free period". As built (the spec's rule: free while the invitation is not declined or closed), leave a person you do not admit as they are; Close is for someone who must leave. Recommendation: keep it so, and use Close only for that. If you say nothing: as built.
4. **What happens on the day after `free_until`.** Nothing is built for it (the walkthrough leaves the price to you). With payments on, an account that never had a subscription is then not let in (it sees only the subscribe page); with payments off nothing changes. Recommendation: decide the price and the message before the six weeks end. If you say nothing: as above.
5. *(Answered by the coordinator: "Not for me" stays one click, and the founder can reopen it from Admin → Cohorts. See Decided.)*

## Deviations from the spec

- **"Plan set by hand" is decided in `User.plan_set_by_hand` (models.py)**, not in `utils/subscriptions.py`; the cohort case is there, plus `User.cohort_free_access` and the SQL clauses above. The spec said to follow the code where they differ.
- **The Circles switch is `privacy.circles_enabled_for`**, which `utils/circles.circles_enabled` calls; the hook is there. `privacy.circles_on_anywhere` is also true while `COHORT_V1` is on (so the access rule reads thread pointers).
- **The trial's cost ceiling is not applied** (Needs Peter 1).
- **Model choice, billing and Batch** differ from `poll_draft.py` (see Decided).
- **Report cap 2,000 tokens** instead of about 1,000 (700 words): reasoning tokens count against the cap.
- **Node ids on the invitation have no foreign key.** "Delete all my content" deletes those nodes; an id that no longer resolves reads as none (`Node.audience_root_id` is done the same way).
- **Added columns:** `CohortInvitation.x_id` (the X id the handle resolved to, compared at claim) and `Cohort.founder_name` (the name letters and prompts use; accounts have no display name, so without it the prompts would say "hrosspet").
- **An invitation may be made without `x_handle` where `COHORT_ALLOW_OPEN_INVITATIONS` is on** (default off): then any signed-in account holding the link can accept it. Needed to walk the steps locally, where accounts sign in by email.
- **More routes and columns from the review:** `POST /api/admin/cohorts/<id>/invitations/<iid>/reopen`; `CohortInvitation.report_task_started_at` / `intro_task_started_at`.
- **The interview prompt file holds the interview section only.** The thread's prompt is composed when the interview starts (the person's own agentic prompt + the section, filled), as a prompt version of theirs with `generated_by` "cohort". The spec's "prompt key `cohort_interview`" is the root's key.
- **Extra routes:** `GET /api/admin/cohorts/<id>`, `GET /api/cohort/intro`; `{"blank": true}` on the report draft; `GET /api/cohort/me` always answers both `invitation` and `owner_of`.
- **New fields on existing payloads** (COHORT_V1 only): `user.cohort`, `user.subscription.cohort_free_until`, `cohort` on a group circle in `GET /api/circles`; `editable` on every prompt (always, an added field).
- **No rate limit by IP** on the page at the link: there is no IP limiter in the codebase (the spec's fallback).
- **The design roadmaps** (`FOUR-FEATURE-ECOSYSTEM.md`, `TECHNICAL-ROADMAP.md`, `ACCELERATED-DEVELOPMENT-ROADMAP.md`) are not updated: this is an experiment branch, not for main without your review.
- **`NEW_MEMBERS_READ_EARLIER_ENTRIES` is `True`** (the spec said only entries shared from then on): decided by the coordinator, above.

Frontend:

- **`/invitation/<code>` is rendered outside the app's frame** (an early return in `App.js`): no NavBar, banners, terms or updates modal, the same for a signed-out visitor, a member, and an account not let in yet.
- **`/cohort` exists also for an account not let in yet** (payments on, before the claim): there it only claims a kept code, which can let the account in for the free period; anything else at that address is the subscribe page.
- **The NavBar link shows for the founder too** (the spec: invitees and members); he reads the group as a member.
- **Things the API doc asked of the web app beyond the spec's list:** the circles Loore keeps for a group hide Rename, Delete, Remove and Add on the Circles page and say why; a short list of the four steps at the top of `/cohort` (not in the spec; it shows what comes next).
- **The Admin tab is loaded when opened** (`React.lazy`), so the admin page's other tests and its first load do not pull in the markdown renderer.
- **`FIRST-COHORT-API.md` corrected/completed:** `next_call_at` without a zone is stored as UTC (a `datetime-local` value must be converted first); `user.cohort` is present for every account while the flag is on; how a member writes an introduction without a draft.
- **From the frontend review:** the column `CohortInvitation.report_written_by_model` and the field `report.written_by_loore`; the `{cohort_name}` placeholder in the interview prompt (backend file and its copy in `first-cohort-prompts/`).


## Worth an issue later

- **Two admits at the same moment can give one person two kept circles** (review W1). There is no lock in admit, and no unique constraint on `Circle(user_id, cohort_id)`. Two first admits (or a double click on the very first one) under PostgreSQL READ COMMITTED can each create a circle for the founder; later syncs keep one circle per person, and the other keeps its members, including someone closed later, and cannot be deleted by hand (409). Fix: lock the cohort row (`with_for_update`) in admit, close, decline and rename, and add a partial unique index on `(user_id, cohort_id)`. Not done here: it needs a migration on prod, and the founder admits one person at a time.
- **Two `/interview/start` calls at once before a root exists make two roots**; the invitation keeps the second, and the first stays a private thread of the person's own. Harmless.

Frontend:

- **The iPhone app does not say who can read a group share later** (frontend review 13). The sentence "people who join the group later will also be able to read it" is on the web only (the introduction's share step, the group page, the Circles audience panel). A member who shares with the group circle from the iPhone app is not told. For an iOS issue, or for Peter to tell the group on the first call.
- **Send uses the stored text when the editor holds no change** (frontend review 12). If the report was edited in another tab after this editor loaded, Send shares that newer text, which this tab does not show. Fix: let `POST /report/send` take the text the editor shows as `base_content` and refuse when it differs. A two-tab case; not done here.

## Not verified

- No real model was called. The opening is written with only the interview prompt and the mode note as input (no message of the person's yet); whether every model opens the interview rather than acknowledging the prompt has not been tried. If one does not, a line in the section ("When the conversation below is empty, write your opening now.") is the fix.
- The routes were exercised through Flask's test client only.

Frontend (no browser was opened; jest renders the pages with the API mocked, and `npm run build` compiles without warnings):

- How the pages look, in light and dark, on a phone and on a wide screen; the letter's typography.
- The round trip through X: `/auth/login?next=/cohort` and back to `/cohort` with the code claimed (needs X keys locally; locally, sign in first, then open the link).
- That the Text page opens the interview at `/node/<id>?awaitLlm=<id>` and Voice at `/voice?resume=<id>` / `?parent=<id>` as the API doc says, and how the "Back to First Eight" line sits above those pages (on a phone, in Voice); whether a model ends the interview with the link's name as the prompt now asks.
- Draft polling against a running Celery worker; the take-back edit against a real shared node.
- That no updates modal or both-providers question opens over `/cohort` in a real session; the `noindex` meta tag in the page's head; copy-link in the Admin table (the clipboard needs a secure context; localhost is one).

## How to try it locally

Local LLM calls bill a real key: use the safe test account, local user 5 `seowriter`, as the invited person, and keep the interview short.

1. In the uncommitted root `.env`: `COHORT_V1=true` and `COHORT_ALLOW_OPEN_INVITATIONS=true` (local accounts sign in by email, so the invitations below have no X handle; never set it on prod). Circles turns on for the founder and the invited person by itself. (`PAYMENTS_ENABLED=true` too, if you want to see the free period at work.) Restart the stack (`make dev`).
2. Schema, in a backend container Python shell (`docker compose exec backend flask shell`): new tables come with `db.create_all()`; the new column on the existing `circle` table does not:

   ```python
   from sqlalchemy import text
   db.create_all()
   db.session.execute(text("ALTER TABLE circle ADD COLUMN IF NOT EXISTS cohort_id INTEGER REFERENCES cohort(id)"))
   # Only if the cohort tables were created before the review's fixes:
   db.session.execute(text("ALTER TABLE cohort_invitation ADD COLUMN IF NOT EXISTS report_task_started_at TIMESTAMP"))
   db.session.execute(text("ALTER TABLE cohort_invitation ADD COLUMN IF NOT EXISTS intro_task_started_at TIMESTAMP"))
   db.session.execute(text("ALTER TABLE cohort_invitation ADD COLUMN IF NOT EXISTS report_written_by_model BOOLEAN"))
   db.session.commit()
   ```

3. Sign in as your admin account (the founder) and create the cohort, from the browser console on `http://localhost:3001` (same-origin API):

   ```js
   await fetch('/api/admin/cohorts', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: 'First Eight', founder_name: 'Peter', about: 'Eight people who differ…', booking_url: 'https://example.com/cal', call_url: 'https://example.com/call', free_until: '2027-01-10'})}).then(r => r.json())
   ```

   Then an invitation without a handle (local accounts sign in by email), with the id from the answer:

   ```js
   await fetch('/api/admin/cohorts/1/invitations', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({letter: 'Dear seowriter, …'})}).then(r => r.json())
   ```

   The answer's `link` is `/invitation/<code>`. (With a real X handle, `x_handle` approves that X account through the whitelist; it needs `COMMUNITY_ARCHIVE_API_KEY`.)

   The same from a flask shell, if you prefer:

   ```python
   from datetime import date
   from backend.models import Cohort, CohortInvitation, User
   from backend.utils.cohort import new_code
   peter = User.query.filter_by(username="hrosspet").one()
   c = Cohort(slug="first-eight", name="First Eight", founder_name="Peter", about="Eight people who differ…", owner_user_id=peter.id, booking_url="https://example.com/cal", free_until=date(2027, 1, 10))
   db.session.add(c); db.session.commit()
   inv = CohortInvitation(cohort_id=c.id, code=new_code()); inv.set_letter("Dear seowriter, …")
   db.session.add(inv); db.session.commit()
   print(f"/invitation/{inv.code}")
   ```

4. The invited person: open a private window, get a sign-in link for `seowriter` (`from backend.utils.sign_in_links import make_link_token; make_link_token("<seowriter's email>")`, then `http://localhost:5010/auth/magic-link/verify?token=…`). Until the frontend exists, walk the steps from that window's console: `GET /api/invitation/<code>`, `POST /api/cohort/claim {"code"}`, `POST /api/cohort/interview/start {"mode": "text"}` (open the `open.text` path it answers and answer two or three questions), `POST /api/cohort/report/draft {}`, poll `GET /api/cohort/report`, read and edit the node (`/node/<report.node_id>`), `POST /api/cohort/report/send`.
5. The founder's window: Updates shows the note, Shared with you holds the report; `GET /api/admin/cohorts/1/invitations` shows the states. `POST /api/admin/cohorts/1/invitations/<iid>/call_done`, then `/admit`: both accounts now have a circle "First Eight" in `/circles`, and `GET /api/cohort/page` works for both.
6. A second invited account (another local email user, or `{"blank": true}` for its report so no model is called) shows the circles in both directions after its admit, and `/close` removing it again.

### With the web pages

Steps 1 and 2 as above, then (`http://localhost:3001`):

1. **Founder, Admin → Cohorts** (the tab is there while `COHORT_V1` is on): fill in the name, your name as people read it, what the group is, a calendar link, a call link, the next call (your local time), a question and Free until; Create cohort. Under "Add an invitation", leave the X handle empty (local accounts sign in by email), write a letter (try Preview), Add invitation, copy the link.
2. **Invitee** (a private window, signed in as `seowriter` with a sign-in link as in step 4 above): open the link. The letter page has no navigation bar. Press **Start the interview**: `/cohort` claims it and shows the interview step (the NavBar now has a link named after the cohort). **Start in text** opens the thread page with Loore's first message, under a "Back to First Eight" line; answer two or three questions.
3. Back to `/cohort` by that line (or the NavBar link): **Continue in text** / **I'm done: write the report**. The page says the report is being written, then shows the editor: change a line, Preview, **Save**, then **Send to Peter** (or **Send nothing**). The call step shows **Choose a time** and the report's line with **Take it back**; after taking it back, **Read and send the report** opens the editor again (it never sends by itself).
4. **Founder**: Shared with you holds the report. Admin → Cohorts: the row shows Report sent with its times; press **Call done**, then **Admit** (a dialog first). `/cohort` (the NavBar link) is the group page.
5. **Invitee**: reload `/cohort`: the group page. **Draft my introduction from my report** (or **Write it myself**), edit, **Share with the group**. The members list shows the introduction.
6. **A second invitee admitted later** (another invitation and account, its report written by hand so no model is called): after its admit, it reads the first invitee's introduction on the group page (the coordinator's decision above).
7. **Sharing an entry with the group**: write an entry, choose Circles under "Who can read this", then the cohort's circle: the panel says that people who join later will also read it. It appears under "Shared in the group".
8. **The other doors**: on a third link press **Not for me** (one sentence, nothing asked); open it again (one sentence instead of the letter); in Admin → Cohorts press **Reopen** on that row, and the link shows the letter again. In Admin, **Close** an invitation (a dialog first). On `/circles` the group's circle has no Rename, Remove or Add.
