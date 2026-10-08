# Decision log

Peter's product decisions, in his own words, with the prediction the builder made before he answered. It exists so that `LOORE-ESSENCE.md` can learn from his reviews, and so that the hit rate shows where the builder's model of his choices is good enough to decide alone. The builder is the agent that builds Loore. Its model changes over time, so each entry names the model that made the call.

How it works:
- **Before a review**, the builder writes down what it expects him to answer and how sure it is. The prediction is recorded before his answer, in Loore or in the builder's notes, and copied here afterwards.
- **After the review**, each entry gets his answer verbatim, a score (hit, partial or miss), and for a miss, what led to the wrong guess.
- **Decision lines** record what the builder did without asking (wrote text, built something, chose a default) before Peter reviewed it; a decision is scored like a prediction that he would approve, and backfilled entries (made before this log existed) have only these.
- **Escalation lines** appear only when the choice to ask or to act was wrong: "asked; should have decided" or "decided; should have asked". The escalation is scored apart from the content.
- **Entries arrive** in these ways:
  - a prediction, written before a brief or reply goes out, and scored when Peter answers;
  - Peter corrects a decision the builder made in a PR (Decision, miss);
  - Peter agrees with a decision but wanted to be asked (Escalation: decided; should have asked);
  - Peter says a question should have been decided without him (Escalation: asked; should have decided);
  - an issue filed days or weeks after a PR traces back to a decision the builder made in that PR: a Decision entry with the issue as its source, scored as a miss, with what the builder could have seen at the time;
  - not logged: the many small implementation choices nobody reacted to.
- **Rules** that come out of his answers are proposed for `LOORE-ESSENCE.md`. Nothing changes that document without his approval.
- **Where the builder's predictions matched** his answers over the last ten decisions of a kind, the builder decides those itself and records the decision here; elsewhere it keeps asking.
- **Left out:** security problems until they are fixed (then they come in, with the prediction and decision), other users' data, and Peter's personal matters.

## Hit rate

| Date | Session | Model | Parts scored | Hits | Partial | Misses | Score | Expected from the stated confidence |
|---|---|---|---|---|---|---|---|---|
| 2026-10-02 | Paid Beta voice review | Opus 5.5 | 10 | 4 | 2 | 4 | 5.0 | 6.45 |
| 2026-10-08 | Role name; Haiku 5.5 for the Read | Opus 5.5 | 2 | 2 | 0 | 0 | 2.0 | not recorded |

Backfilled decisions (27 entries, 2026-04-06 to 2026-10-06): 7 hits / 4 partial / 16 misses. These are **not** comparable with the live predictions above: the backfill mostly picked up Peter's corrections, while approvals passed without comment and were not recorded.

Live decisions (from 2026-10-08): 0 hits / 0 partial / 1 miss. They arrive the same way, mostly through corrections, so they are not comparable with the predictions either.

Escalation misses: asked; should have decided: 1 (backfilled, 2026-06-17). Decided; should have asked: 0.

Partial hits count as half. Calibration on 2026-10-02: at 75–80 % confidence the builder scored 2.5 of 4; at 55–65 %, 2.5 of 5; at 40 %, 0 of 1. Overconfident by about 15 points.

One more miss is not in the table: the builder excluded the question of when to post the situations, claiming his reply already showed "now". He answered "after the Beta scope is settled", as the builder had proposed.

## Patterns in the builder's misses

- **Hard lines read as conditions.** Where Peter holds a rule without exceptions ("never change providers", "never decrypt users' content"), the prediction was a yes with conditions.
- **A cost emergency generalised into a cost mandate.** The 2026-09-12 digest incident was a fix for runaway spend, not a standing wish to cut cost. In this phase he puts product quality first.
- **The builder's own proposals used as evidence of his view.** "Consenting alpha users" came from the builder's triage note, not from him.
- **Assumed constraints instead of checked ones.** A social reason was guessed for not contacting reporters; the real limit was that no channel exists.

## Entries

### 2026-10-02 · A provider fails for an account reason

- **Situation:** a provider fails for a reason waiting won't fix (spend limit, billing, revoked key), and a paying user's reply fails. Do we tell the user which provider failed, and switch them to the other provider at our cost?
- **Prediction:** name the provider, yes (75 %). Fall back to the other provider automatically, with the reply showing which model wrote it (55 %; second guess: a one-click "answer with GPT instead").
- **Peter:** "no, we should just prevent failures for account reasons. They will happen anyway in case of a rapid user growth. But then it'll be fine and we'll figure it out. Changing providers is however never acceptable."
- **Score:** two misses.
- **Why it missed:** friction and cost were weighed, and the provider was treated as a detail Loore can swap. For Peter, the model a user chose is not Loore's to change, and account failures are to be prevented, not designed around.
- **Applies to:** #369 (plain "temporarily unavailable" message and an alert to Peter; no fallback, provider not named), #360.
- **Rule:** "A user's model is theirs" in `LOORE-ESSENCE.md`.

### 2026-10-02 · Cheaper models and the Batch API for background jobs

- **Situation:** for background jobs such as the todo merge, recent context or intentions, may the cheapest model or the Batch API be chosen without asking whenever a blind comparison shows no loss and the result can be some hours old?
- **Prediction:** yes, I decide myself as long as I record the comparison and the choice; Peter judges user-facing text, I judge mechanical jobs (65 %).
- **Peter:** "batch api for background jobs is a natural choice, use it wherever it makes sense" · "on the other hand, cost vs. quality is something I'd like have a say in" · "what could remove some decision burden from me would be making comparisons between different models without my say, if it's cheap - so I can imagine you making an experiment where you run a comparison with the cheapest available model (like GPT-6 Luna) against my prod data and compare against the frontier models that I triggered in the past manually. Like todo merges" · "this is still on prod, so I'd like to start the experiment manually myself, but you can expect me to want to run such experiments" · "we're still in a really early phase, so we're not optimizing for cost. We want to build the best product we can, and if paying for frontier models is what it takes, then ok. Later, when we have too many todo merge requests and it costs us a ton, only then it will make sense to run such experiments, whose aim is cost optimization" · "smaller models also may have better latency, and that is something that influences UX a lot. So when todo merges have 100% accuracy on both frontier, and small models, and small models have significantly better latency, then that would be desired."
- **Score:** partial. The Batch API part was right; deciding the model without Peter was wrong.
- **Why it missed:** the digest cost incident was generalised into a cost rule, and latency was missed as a criterion.
- **Applies to:** #380 (recent context through the Batch API, ready to build), #234 (a comparison script for the todo merge, accuracy and latency, for Peter to run), #357.
- **Rule:** "Quality first, for now" in `LOORE-ESSENCE.md`.

### 2026-10-02 · A job regenerates something the user edited

- **Situation:** when a job regenerates something the user has edited (profile, todo list, intentions), does it keep the user's edits and refresh only its own part?
- **Prediction:** the user's edit wins; the job keeps it and updates around it (80 %).
- **Peter:** "yes?" · "I don't see what other options there are. Could you give me some examples? Why would a model regenerate something a user has editted?"
- **Score:** hit. The other options were overwriting the edit (the #183 bug) or freezing the document once edited.
- **Applies to:** #183 / PR #210, #225, #216, #143.
- **Rule:** "The user's edit wins" in `LOORE-ESSENCE.md`.

### 2026-10-02 · A bug nobody can reproduce

- **Situation:** with no reproduction, may the reporter be asked directly, a defensive fix with error logging be shipped, and the issue be closed if it doesn't recur?
- **Prediction:** logging yes; asking the reporter yes, but Peter asks them himself; no fix that only hides the symptom; close after a stated quiet period (55 %).
- **Peter:** "I don't think you have a way of contacting the reporter directly, unless they reported a bug via their own github account? Well, in that case yes, you can ask them directly. Btw this policy may change in the future - it's for current alpha and early Beta. If there is significant growth, we might want to rethink PR policy" · "or did you mean via the changelog feature in Loore? Users can submit issues via Loore, which uses my hrosspet account, but tags the issue with their username. We're letting the users know when an issue they submitted is resolved. You propose to use the same mechanism? In that case I give the same response as for the above case"
- **Score:** miss on the part he answered (who asks). The defensive fix and closing were not answered.
- **Why it missed:** a social reason (people he knows) was assumed where the real limit is technical: Loore can tell a reporter that their issue is closed, but can't ask them a question.
- **Applies to:** #244, #249, #287, #274.
- **Rule:** "Bug reports" in `LOORE-ESSENCE.md` (alpha and early Beta).

### 2026-10-02 · Loore can't know what the user wants next

- **Situation:** when Loore can't know what the user wants to do next, does it show the choices instead of guessing?
- **Prediction:** show the choices, two at most, nothing pre-selected (65 %).
- **Peter:** "yes"
- **Score:** hit.
- **Applies to:** #387 (show both buttons; ready to build), #349.
- **Rule:** "Show the choices" in `LOORE-ESSENCE.md`.

### 2026-10-02 · Account deletion

- **Situation:** how long a grace period, and do we keep the cost records in anonymous form?
- **Prediction:** 14 days (40 %; second guess 30). Cost records kept, detached from the person (75 %).
- **Peter:** "I don't know" · "is there some law informing these decision?" · "my intuition tells me 30 day grace period" · "and yes, we keep cost records in anonymous form"
- **Score:** grace period a miss (second guess right); cost records a hit.
- **Why it missed:** the guess rested on a legal deadline that had not been checked. No law sets a grace period: GDPR asks for erasure without undue delay and an answer within a month (extendable by two for complex cases), and a 30-day window the user agrees to is common practice.
- **Applies to:** #268, #269.
- **Rule:** "Deletion" in `LOORE-ESSENCE.md`.

### 2026-10-02 · Experiments on other users' archives

- **Situation:** may experiments run on other users' archives if the outputs stay in files and never reach their accounts?
- **Prediction:** public data (Community Archive tweets) yes; private Loore writing only Peter's own archive plus users who agreed to that experiment (75 %).
- **Peter:** "no, we never decrypt user's content" · "we can run scripts over their unencrypted metadata" · "if we need content, we can run it on my data (I have plenty), if the experiment is non-destructive - ie keeps the original data untouched, just used for comparison"
- **Score:** partial. Peter's own archive was right; the consent exception was wrong.
- **Why it missed:** the consent idea came from the builder's own triage note on #359, taken as his view.
- **Applies to:** #357, #359, the Read notes test.
- **Rule:** "Nobody decrypts users' content" in `LOORE-ESSENCE.md`.

### 2026-10-02 · Where the decision log lives

- **Situation:** a log in the public repository, or a private one in Loore or the builder's notes?
- **Prediction:** the public repository, with anything security-related left out (60 %).
- **Peter:** "yeah, let's try putting it on the public repository in the spirit of transparency (this is your model of me / my vision of Loore, and this should be public)" · "what do you mean security-related? I'd keep out information that would reveal security issues publicly before they are fixed. But after they are fixed, I'd include publicly your predictions (made locally in your memory) and my decisions, or feedback from followup issues"
- **Score:** hit. "Security-related" narrows to unfixed security problems.
- **Rule:** "Transparency" in `LOORE-ESSENCE.md`.

### 2026-10-02 · When to post the decision situations

- **Situation:** post the situations for the Beta-relevant issues now, or after the Beta scope is settled?
- **Prediction:** not made; his reply was wrongly taken as already showing "now".
- **Peter:** "yes" (to "after we settle the Beta scope").
- **Score:** miss, not counted in the table.

### 2026-10-02 · Moving reversible engineering items to ready

- **Situation:** seven to nine "needs your input" issues already recommended a reversible fix; under the standing rule (ask only before irreversible actions) they move to ready to build.
- **Prediction:** not scored; he had answered before the predictions.
- **Peter:** "go ahead" · "yes"
- **Applies to:** #217, #218, #275, #224, #216, #374, #287; #379 and #331 except Peter's prod steps.

### 2026-10-06 · Todo merge model: Luna first

- **Situation:** the new todo-merge prompt (#234, PR #417) was not yet tested. Which model runs it, and who is compared with whom?
- **Peter:** "let's first run only Luna on the new prompt. And if it improves, decide what to do next. Is it perfect? Use it. Is it less than perfect? I'll run also the frontier model."
- **Source:** voice review 2026-10-06 (Opus 5.5).
- **Applies to:** #234, PR #431.

### 2026-10-06 · Todo merge returns edits

- **Situation:** the comparison showed many copying errors, even from frontier models, when the merge rewrites the whole todo list.
- **Peter:** "I'm surprised how many errors even frontier models did. Copying is probably just a poor fit of a task for LLMs" · "I meant Option 2" (keep the proposal card and the confirmation; the merge returns edits) · "let's build the PR… Fixing todo merges via instructing frontier model to make specific edits, instead of rewriting the whole todo. Let's reuse the functionality from other artifacts" · "let's evaluate it on Opus 5.5". On a check that refuses ticks the proposal didn't name: "wouldn't this be brittle? Tentatively against it." On moving Priority Order out of the todo list: "yes".
- **Source:** voice review 2026-10-06 (Opus 5.5).
- **Applies to:** #234.

### 2026-10-06 · Read for every user: after a reflection, by choice

- **Situation:** writing the Paid Beta scope brief from Peter's answers. Only an admin could start a Read (#435).
- **Decision:** wrote in the brief that in the Beta, a Read has to start by itself after each reflection, for every user.
- **Peter:** "no!" (it doesn't start by itself after each reflection) · "I expect most of new users come for the Read feature, so they will want to Reflect -> Read workflow, but some won't and it will still be ok to just Reflect." Rollout: "two env vars: one turns on Read for every user, default off; another env var for whitelisting users whom Read feature is shown to."
- **Score:** miss.
- **Source:** voice review 2026-10-06 (Opus 5.5).
- **Applies to:** #435.
- **Rule:** refines "reflection comes first" (adopted above).

### 2026-10-06 · No digest; the Read is a search

- **Situation:** writing the same Paid Beta scope brief.
- **Decision:** listed "the digest" among the features that wait until after the Beta opens.
- **Peter:** "I'm not planning any digest feature… two types of problem related to the CA daily tweets: intentionally lossy compression (=digest…) and needle in the haystack search (=personal feed…). Digests already exist and we're not planning to build one for now. We're building the personal feed."
- **Score:** miss.
- **Why it missed:** a general note of Peter's on two kinds of problem (a digest, and a search for the needle) was taken as a plan for a digest feature.
- **Source:** voice review 2026-10-06 (Opus 5.5).
- **Rule:** candidate: Read is a needle-in-the-haystack search for this user now, not a summary of the day.

### 2026-10-06 · Data purge: build it, revoke the X login, 30 days to undo

- **Situation:** the user-data purge (#268) was designed in PR #415 but not built. Revoke the X login at purge (it needs one decryption of the token)? An undo window for "Delete all my writing"?
- **Peter:** "build it" · revoking the X login at purge: "yes pls" · an undo for "Delete all my writing": "definitely, 30 days grace".
- **Source:** voice review 2026-10-06 (Opus 5.5).
- **Applies to:** #268, #415.

### 2026-10-08 · The name of the role

- **Situation:** the log needed a name for the agent across model versions.
- **Prediction:** "the builder" was recommended over "Loore Code" and "Claude", with "Loore" advised against because it is the product's name. Peter was asked to choose; no confidence was recorded.
- **Peter:** "I like the builder"
- **Score:** hit.
- **Source:** Claude Code session 2026-10-08 (Opus 5.5).

### 2026-10-08 · Fix a flaky test where it shows up

- **Situation:** the iOS CI job failed on PR #424 in a test from the iPhone app's first PR (#384), unrelated to #424's change.
- **Decision:** judged it a flake and reran the job, without fixing the test.
- **Peter:** "pls fix the flaky test in the same PR"
- **Score:** miss.
- **Why it missed:** "not this PR's code" was taken as a reason to leave a known flaky test alone.
- **Source:** Claude Code session 2026-10-08 (Opus 5.5).
- **Applies to:** flaky tests found while working on any PR.
- **Rule:** candidate: a flaky test found during a PR is fixed in that PR, at its cause.

### 2026-10-08 · Haiku 5.5 as a Read model at the >100k price

- **Situation:** Peter proposed adding Claude Haiku 5.5 to the Read models, expecting Luna's price. Measured: every Read is over Haiku's 100k-token tier, about 7–8 cents per Read in batch against about 1 cent for Luna.
- **Prediction:** recommended adding it anyway, because quality decides in this phase (no confidence recorded).
- **Peter:** "yes pls, add it to the list of models for Read. We will see how good it is - if it's actually better than Luna, we can figure out how to get the input below 100k toks."
- **Score:** hit.
- **Source:** Claude Code session 2026-10-08 (Opus 5.5).
- **Applies to:** #452, #453.

## Decided by the builder

Choices that agents flagged in PRs and that the builder decided because a rule above already covers them (Peter, 2026-10-02: decide what the log supports, raise only real judgement calls). Each is also recorded on its PR. Peter can overrule any of them; an overruled one becomes a miss in the hit rate.

| Date | PR | Choice | Decided | Rule |
|---|---|---|---|---|
| 2026-10-02 | #406 | How often to email about one failing provider account | At most every 6 h per cause, as a setting | Every added heuristic is named |
| 2026-10-02 | #406 | A paid batch is refused for an account reason while polling | Keep polling until the cap | Prevent the failure where possible |
| 2026-10-02 | #409 | Recent context keeps failing for a user | Stop after two failures in a row, report it | Stop after two failures, loudly (#368) |
| 2026-10-02 | #409 | How often finished recent-context batches are collected | Every 60 s | Latency counts as quality |
| 2026-10-02 | #401 | A newcomer's first session and the updates window | `/welcome` skips the whole session | A newcomer reflects first |
| 2026-10-02 | #401 | What counts as the user's own entry | Only writing done in Loore; not imports, session prompts, links or deleted entries | Imports don't count (Peter, 2026-10-01) |
| 2026-10-02 | #403 | iPhone only or iPhone and iPad | iPhone only | The design document |
| 2026-10-02 | #404 | A chat reply is sent with a read-only model | Refused with a plain message. The first decision (run it on the chat default) was wrong: the review showed it can switch the provider | Never change providers, not even as a fallback |
| 2026-10-02 | #405 | What counts as a dropped pick | Both kinds of picks Loore can't show | An empty Read is a good result |
| 2026-10-02 | #408 | A failed auto-generate hides LLM Response outside Read | Follow-up issue #416 | Show the choices |
| 2026-10-02 | #413 | Admin-only guard, dry run, missing-items count in the todo-merge comparison | Kept | Experiments run on Peter's data; cost is his call |
| 2026-10-02 | #414 | The profile note says "keep what they wrote" | Kept | The user's edit wins |
| 2026-10-02 | #417 | The todo merge rewords items or moves them out of the section the user named | Keep wording; new items copied word for word; the named section is created if missing | The user's edit wins |
| 2026-10-02 | #415 | Purging an AI or system account | Refused | None needed: purging one would delete every AI reply in Loore |
| 2026-10-02 | #415 | Cost rows after a purge | Anonymised, with response ids cleared | Cost records are kept in anonymous form |
| 2026-10-06 | #234 | Todo merge as edits: which sections change the list | Only New Tasks and Completed; Note, Issue and Priority Order don't | Extends Peter's "yes" on Priority Order |
| 2026-10-06 | #234 | Todo merge as edits: when a full rewrite is allowed | Only when the list has no tasks (nothing to anchor on); otherwise edits only | The user's edit wins |
| 2026-10-06 | #234 | Todo merge as edits: a check after the edits are applied | Every previous line is kept with its text, only [ ] to [x] may change; one retry, then fail. This differs from the proposal-wording check Peter leaned against. It sits in one removable function and the evaluation measures how often it fires | Every added heuristic is named |
| 2026-10-06 | #234 | Todo merge as edits: feature flag | None, because Peter evaluates from the branch before merging | Reversible work goes ahead |
| 2026-10-06 | #433 | A cut-off merge output | Fails with nothing saved; the message says to ask for the update again, since the card has no retry (#434) | Problems show |
| 2026-10-06 | #413 | Running the comparison script on prod | From a separate worktree with `.env.production` symlinked, never by checking out a branch in the live app folder (the merge prompt file is read on every call) | Experiments run on Peter's data; he starts any run on prod |

Raised to Peter instead: whether the profile states what the entries show next to the user's own description of themselves (#414), revoking the X login at purge (it needs one decryption of the token), an undo window for "Delete all my writing" (#415), and real-model checks of #414 and #417.

## Backfilled decisions

Decisions made before this log existed, oldest first. None of them has a prediction. Where the builder acted first and Peter then reviewed it, the entry has a Decision line, counted apart from the live predictions (see Hit rate). Where the builder asked without writing down a prediction, or Peter raised the matter himself, the entry has neither line. Quotes are his own words, from earlier voice reviews in Loore, his comments on GitHub, and his messages in earlier Claude Code sessions. Fragments are joined with " · ".

### 2026-04-06 · Describe Loore's privacy as it is

- **Situation:** asked for a positioning document on Loore, with pitches for promotion, comparing Loore with a knowledge base kept as files on a laptop.
- **Decision:** described Loore as offering "cryptographic privacy" and "stronger-than-local security" compared with keeping files on a laptop.
- **Peter:** "note that Loore is not end-to-end encrypted. There is encryption, but we're still decrypting things on the backend. It's still a trust-based model. But it's the right tradeoff between ease of use and total privacy"
- **Score:** miss.
- **Why it missed:** encryption at rest was counted as stronger privacy, though the server decrypts the content.
- **Source:** Claude Code session 2026-04-06 (Opus 4.6).
- **Applies to:** any text that describes Loore's encryption.
- **Rule:** candidate: Loore describes its privacy as it is: encrypted at rest, decrypted on the server, a model that rests on trust.

### 2026-05-28 · A finished job tells the user when they come back

- **Situation:** asked to file GitHub issues from Peter's todo list, including "after user profile is generated, notify the user about it".
- **Decision:** filed #131: a toast, banner or in-app indicator when the profile is ready, "whether or not the user is on the profile page".
- **Peter:** "note that it should work async - if the task finishes when the user is not using Loore, they should be notified once they return to Loore next time"
- **Score:** partial. The notice stayed; a user away from Loore was not covered.
- **Source:** issue #131 comment; the issue was filed in Claude Code session 2026-05-28 (Opus 4.7).
- **Applies to:** #131 (PR #201).
- **Rule:** candidate: work a user started finishes without them, and Loore tells them when they come back.

### 2026-06-17 · Reversibility is the bar for working without asking

- **Situation:** an overnight run on PR #196, a refactor on staging, reached a slice that needed four choices. Peter was away.
- **Decision:** recommended four choices for the slice (the version-history mapping, keeping the old table for now, its place in the navigation, the backfill as a standalone script), and stopped to ask Peter to confirm them before building.
- **Peter:** "Were any of the changes destructive / irreversible? We're on staging -> no. Better to ship code that is tested and working, but potentially needs to be updated based on my different intent (or worst case dropped completely) for work that is running overnight." · "if I come back in the morning and no implementation and no UI tests were done, that's the same from my point of view as the worst case scenario of having to walk back some work that has been done." · "It's good to flag them explicitly, so I can review (I explicitly gave you this instruction before going afk), but that's for after the fact." · "what is risky is destructive / irreparable changes, not big changes in terms of volume"
- **Score:** hit. He accepted all four.
- **Escalation:** asked; should have decided.
- **Why the escalation missed:** the size of the change was taken for risk; the measure is whether it can be undone.
- **Source:** Claude Code session 2026-06-17 (Opus 4.8).
- **Applies to:** all agent work; PR #196; the reversible items moved to ready on 2026-10-02.
- **Rule:** adopted 2026-10-06: reversible work goes ahead without asking and is flagged afterwards; every added heuristic is named; merging to main stays Peter's.

### 2026-06-25 · Fix the cause, not what is shown

- **Situation:** Peter reported that the admin filter for users with costs above zero still listed rows showing $0.00, and asked whether a later commit broke it or sub-cent costs were rounded to $0.00.
- **Decision:** changed the filter to match the rounded display and pushed it to main.
- **Peter:** "Your display fix only covers up a deeper issue. I don't want that."
- **Score:** miss.
- **Why it missed:** the fix covered the rounding that made the rows visible, not the reason they had costs: embedding costs recorded on the model's account instead of the person's.
- **Source:** Claude Code session 2026-06-25 (Opus 4.8). The display fix was reverted.
- **Applies to:** cost attribution to the human owner (commits on main, 2026-06-25).
- **Rule:** adopted 2026-10-06: problems show, they are not hidden. Fail loudly, stop after repeated failures, and fix the cause rather than what is displayed.

### 2026-06-26 · Every added heuristic is flagged

- **Situation:** asked to run the intentions backfill through the Batch API, one batch request per user, for half the price.
- **Decision:** multiplied the batch path's calibrated budget by 0.92 "for safety", so it read about 8 % less of each archive than the direct path. The report left it out; it came up when Peter asked why the batch read less.
- **Peter:** "I don't like that you put extra multiplier and didn't flag it. Pls remove it."
- **Score:** miss.
- **Why it missed:** a batch that never fails was put ahead of full coverage, and the trade was not reported.
- **Source:** Claude Code session 2026-06-26 (Opus 4.8).
- **Applies to:** the intentions batch backfill (PR #202, commit 77222b2).
- **Rule:** adopted 2026-10-06: reversible work goes ahead without asking and is flagged afterwards; every added heuristic is named; merging to main stays Peter's.

### 2026-07-02 · Replies under a public post are public

- **Situation:** designing the public side of Loore (#228). Who may reply under a public post, and may those replies be private? A visitor's reply took the visitor's default privacy, often private.
- **Peter:** "other users (non-authors) can't call LLM response right away - they must first respond (with a question / prompt / commentary) and only then they'll be able to call an LLM" · first "private replies are ok imo", then, later the same day: "I'd actually push back - I want users to be able to respond directly under a public node. But we should allow only public responses. Maybe a pop up confirmation dialog upon hitting send that replies under public posts are public (and an option to not show this dialog again). There is a separate mechanism how people can interact with public nodes privately - they can quote it in their private threads (even agentic ones)"
- **Note:** his second answer replaced the first within the same discussion.
- **Source:** Claude Code session 2026-07-02 (Fable 5); recorded on issue #228.
- **Applies to:** #228, PR #229.

### 2026-07-02 · The public side ships silent and opt-in

- **Situation:** the public side of Loore (#228, PR #229) had no notifications, so an author would not learn that someone replied.
- **Peter:** "It should ship silent in v1, but I'll have to think more about notifications. They go against the no-dopamine-rush intention of Loore" · "I would like the whole public side of Loore we just built to - ship dark - let the users enable it or disable it in their Account"
- **Source:** Claude Code session 2026-07-02 (Fable 5); recorded on issue #228.
- **Applies to:** #228, PR #229.
- **Rule:** candidate: no notifications designed to bring people back into Loore; until a design avoids that, features ship without them.

### 2026-07-24 · A failed deploy step fails loudly

- **Situation:** a deploy left the site down: a failed check on the background scheduler stopped the deploy before the web server was restarted. Restart the web server first, and turn the scheduler check into a warning?
- **Peter:** "how would I know there was a problem? That would be a silent fail. I'd like the site to be up without it, but the deploy to fail loudly so that I can fix it in case something similar happens next time."
- **Source:** Claude Code session 2026-07-24 (Opus 5).
- **Applies to:** `deploy.sh` (commits b457c57, 6e26217).
- **Rule:** adopted 2026-10-06: problems show, they are not hidden. Fail loudly, stop after repeated failures, and fix the cause rather than what is displayed.

### 2026-08-22 · Turning sharing off takes content down at once

- **Situation:** server-rendered public pages (PR #252). Content stayed public after its author switched public sharing off. Should the new pages keep that rule, as the API did, or should the switch hide existing public posts everywhere?
- **Peter:** "we should respect author taking a public post down immediately"
- **Source:** Claude Code session 2026-08-22 (Fable 5); PR #252 ("Peter's call").
- **Applies to:** PR #252: the switch takes down every public post and reply of the author at once, on every public surface.
- **Rule:** candidate: withdrawing content takes effect at once, everywhere it is shown.

### 2026-08-27 · A consent question can be dismissed

- **Situation:** asked how to add an opt-in to X signups for seeding the account from the user's public tweets (PR #267): a hard signal, but still opt-in, and not a checkbox.
- **Decision:** recommended a screen right after X login with two equal buttons, Yes and No, and no skip link, close button or timeout.
- **Peter:** "btw I disagree with having only a Yes and a No options. Escape (not necessarily a button, but having an ability to dismiss it) is important, because a hesitation could turn off the user from actually finishing the signup. Better to not know what the user wants than not having them at all." · "if the user doesn't click anything, that means we don't know"
- **Score:** partial. The question with Yes and No and the recorded answer stayed; the forced choice did not.
- **Source:** Claude Code session 2026-08-27 (Fable 5).
- **Applies to:** PR #267.
- **Rule:** adopted 2026-10-06: ask when the choice matters, in a dialog that can be dismissed; no answer means "we don't know", not "no".

### 2026-09-13 · Recommendations get a quiet hit-or-miss signal

- **Situation:** Loore quotes saved references in its replies, and nothing recorded whether a quote was read or useful.
- **Peter:** "We need to start keeping track of at least the number of quotes shown to the user, number of read, and preferentially also whether it was a hit, or miss recommendation." · "A heart icon? Sounds good if it's subtle / not colorful and just a contour." · "Thumbs down was the first that came to my mind, but that's too emotive distractive. Maybe a plus and a minus icon in a circle? That could feel neutral"
- **Source:** Claude Code session 2026-09-13.
- **Applies to:** the Good quote / Bad quote buttons (commit 805a197, which replaced a "more / fewer like this" framing); #351, #352, #363.

### 2026-09-13 · Recommend fewer, better

- **Situation:** scoping a personal feed (the Read) over the Community Archive's daily tweets, with cost estimates.
- **Decision:** compared designs by how many relevant tweets or connections each would yield ("yield per call is flat in window size").
- **Peter:** "it repeatedly sounds like you're maximizing yield" · "the fewer tweets / connections we can recommend, the better. If those that we do, are really relevant." · "the point is to increase our ability to find the needle, not to produce 10x more potential needles."
- **Score:** miss.
- **Why it missed:** the personal feed (find the needle) was mixed with intention matching, as Peter said in the same reply.
- **Source:** Claude Code session 2026-09-13 (Fable 5.1).
- **Applies to:** the Read (#297, #307) and its evaluations.
- **Rule:** adopted 2026-10-06: recommend fewer, better. An empty Read is a good result, an untouched pick is neutral, and Read uses no patterns from extractive feeds.

### 2026-09-17 · A paid fallback is asked in a dialog and billed to the account it serves

- **Situation:** the admin whitelist needed a way to look a handle up on X, a paid call, when the Community Archive doesn't have it.
- **Decision:** added a checkbox under the whitelist form that allows the paid lookup. The dialog that replaced it billed the lookup to the admin.
- **Peter:** "why is there a checkbox? I wanted a confirmation dialog when the handle is not found in CA. This is a bad UX" · "it should be billed to that newly whitelisted account"
- **Score:** miss, on both parts: the checkbox and who is billed.
- **Source:** Claude Code session 2026-09-17 (Fable 5.1).
- **Applies to:** the admin whitelist (commits 9dc39b0, ac6a25a); later the Build profile dialog in PR #375.
- **Rule:** adopted 2026-10-06: ask when the choice matters, in a dialog that can be dismissed; no answer means "we don't know", not "no". · candidate (awaits Peter's confirmation, asked 2026-10-06 in node 201350; the first wording, "a retry pays only for the missing work", was unclear to him because a retried single call costs the full price; it came from the X bookmark sync, PR #334): every billed call records its cost on the account it serves. A job that stops partway resumes where it stopped, so the next run doesn't pay again for work already done.

### 2026-09-21 · The Read never brings back what was read

- **Situation:** Peter found the button "Read the day again" and its tooltip unclear, and asked for a label clearer than "Generate more", less addictive and more intentional (PR #322).
- **Decision:** renamed it "Read again with my marks", with a tooltip saying what the second read gets and which picks it leaves out.
- **Peter:** "Read again with my marks should be Read further." · "It should be doing the obvious thing - which is to not ever recommend again a thing marked read (talking about the personal feed here only; agentic should be able to recommend read stuff, but it should know it was already read - which it does)"
- **Score:** miss.
- **Source:** Claude Code session 2026-09-21 (Fable 5.1).
- **Applies to:** PR #322; #352 (PR #358).

### 2026-09-21 · One Chat item takes the whole call off the training key

- **Situation:** issue #326 asked: should one entry marked Chat inside a 100k-token export pull the whole thread off the training key? (The strict reading: the promise is on the row.)
- **Peter:** "yes"
- **Note:** two days later he also decided that artifacts take the user's default setting instead of a hardcoded Chat, so each row means what it says (recorded on #326).
- **Source:** issue #326 comment; the issue was filed in a Claude Code session of 2026-09-21 (Fable 5.1).
- **Applies to:** #325, #326, PR #327, PR #339.
- **Rule:** adopted 2026-10-06: a call goes to the training key only if everything in its prompt allows it. External content quoted word for word never reaches training.

### 2026-09-25 · An account set to None runs no AI jobs

- **Situation:** a user who switches their account's AI usage to None still had background jobs sending their writing to models: profile updates, summaries, embeddings and others (#346).
- **Peter:** "it's possible a user has Loore account full of data with ai_usage enabled. Then they decide to no longer want to provide it to LLMs and they change their account setting ai_usage to None. In that case no background/automatic jobs should run. No profile updates, integrations, embeddings, poll drafts, recent context summaries, etc." · "All of such jobs should be gated on user's account ai_usage allowed." · "there is one usecase for this, where it's valid that ai_usage none items are included: when user requests an export of their Loore data"
- **Source:** Claude Code session 2026-09-25 (review of PR #344 and issue #346).
- **Applies to:** #346, PR #344, PR #361.
- **Rule:** adopted 2026-10-06: AI usage None means no AI. Nothing marked None reaches a model by any route, and no job runs for an account set to None. The user's own data export is the exception.

### 2026-09-25 · A "no" to the prefill blocks every prefill job

- **Situation:** should the prefill from public tweets require a "yes" to the prefill question, checked in the backend? The admin panel only displayed the answer.
- **Peter:** "no, not every signup needs to fill this in" · "but prefill_consent == "no" should be checked and prevent the prefill from being done (with admin dashboard popup warning). Intentions prefill should also be gated this way" · on the Build profile button: "it should also not proceed when consent is "no"."
- **Source:** Claude Code session 2026-09-25 (Opus 5.5).
- **Applies to:** #346, PR #361.
- **Rule:** adopted 2026-10-06: ask when the choice matters, in a dialog that can be dismissed; no answer means "we don't know", not "no".

### 2026-09-25 · A reply under a Read keeps the user's own setting

- **Situation:** a Read's nodes are Chat because they quote other people's tweets. A user's reply under a Read copied that Chat, although the account was set to Train.
- **Peter:** "The node shouldn't copy Read's ai_usage which is chat, because it's quoting public tweets we don't have a license to train on. The resolver knows if these get into the context, the whole request needs to be done as "chat", but my nodes should still have the setting of "train"."
- **Note:** his answers on #362, as recorded there: an AI reply whose context held the tweets is stored as Chat ("the stored value should describe what the node was built from"), and existing replies are not backfilled. The first part changed on 2026-09-29; see "AI replies under a Read: Chat for trust, but no lock and no inheritance".
- **Source:** Claude Code session 2026-09-25; issue #362 comment.
- **Applies to:** #362, PR #365.
- **Rule:** adopted 2026-10-06: a call goes to the training key only if everything in its prompt allows it. External content quoted word for word never reaches training.

### 2026-09-29 · Recent-context summaries through the Batch API

- **Situation:** PR #333 lets running jobs finish for 90 seconds at a deploy, then cuts them off. Peter had asked for cut-off jobs to be re-queued.
- **Decision:** kept the PR without re-queueing, and recommended handling re-queueing in #379 (a separate worker for background jobs).
- **Peter:** "requeueing not necessary for this PR" · "profile and intentions are already computed via batch processing, so the probability we kill them during the job submission is pretty low and in the case of profiles it recovers." · "But I'm thinking about recent context summary generation - do we use batch processing? If not, file an issue for it"
- **Score:** hit. The Batch API question for recent context was Peter's own addition.
- **Source:** voice review 2026-09-29 (Opus 5.5).
- **Applies to:** PR #333, #379, #380.
- **Rule:** "Quality first, for now" in `LOORE-ESSENCE.md` (background jobs use the Batch API).

### 2026-09-29 · Derived content may be Train; external content never

- **Situation:** PR #339 had to decide which of the user's own context rows may go out on the training key (#326).
- **Decision:** kept the references digest off Train in all cases, and counted every artifact listed in the prompt's artifact index by its own setting.
- **Peter:** "derived artifacts should use user's ai_usage setting, including 'train'. By derived is those, that are unlikely to quote other people's content verbatim. Digest, profile, context summary - they should all possibly use train. External references should never use train." · "LLM nodes that quote external references should be safe to set as "train" because the external content is not quoted verbatim, but via the keywords." · "Feeds should theoretically behave the same, but it's safer to always set them as chat" · on the index: "Artifact index is again a derived artifact, unlikely to quote "chat" content verbatim, so it should follow user's preference, including possibly "train""
- **Score:** miss.
- **Source:** voice review 2026-09-29 (Opus 5.5).
- **Applies to:** PR #339, PR #365, #381.
- **Rule:** adopted 2026-10-06: a call goes to the training key only if everything in its prompt allows it. External content quoted word for word never reaches training.

### 2026-09-29 · AI replies under a Read: Chat for trust, but no lock and no inheritance

- **Situation:** PR #365 (#362): a user's reply under a Read had copied the Read's Chat.
- **Decision:** the first version stored every AI reply under a Read as Chat and kept it there, even after the Read was deleted, as a default Peter could veto.
- **Peter:** "Read could theoretically also be "train" - the problem is not in the Node containing the recommendations, it's the external references themselves. We're setting Read to "chat" just to be safe." · "In my experience, chatting below the recommendation never produced a quote verbatim" · to dropping the lock: "yes pls" · "I think marking them as chat makes sense for user trust: so they know this won't be trained on because we don't have the license. But subsequent replies, neither user nor AI, shouldn't inherit that chat"
- **Score:** partial. Chat stayed for the reply that presents the picks; the lock and the inheritance by later replies went.
- **Note:** this changes his 2026-09-25 answer on #362. Only the reply that presents the picks stays Chat; every reply after it takes the thread's setting. The rare verbatim copy is to be caught when a training set is built (#381).
- **Source:** voice review 2026-09-29 (Opus 5.5).
- **Applies to:** PR #365, #381.
- **Rule:** adopted 2026-10-06: a call goes to the training key only if everything in its prompt allows it. External content quoted word for word never reaches training.

### 2026-09-29 · The default AI usage of a new reply

- **Situation:** PR #365 had to set the default AI usage of a user's new reply in a thread.
- **Decision:** if any node passed on the way up the thread was set to None, the new reply defaulted to None, as a default Peter could veto.
- **Peter:** "The helper selector for user nodes should take previous node, if there is none, the user account setting, if there is none, take the default. Why should any None in the context make new user reply also None? I think it shouldn't"
- **Score:** miss.
- **Source:** voice review 2026-09-29 (Opus 5.5).
- **Applies to:** PR #365.

### 2026-09-29 · Switching the account to Train changes no existing artifact

- **Situation:** in PR #339, a user switches their account from Chat to Train. What happens to artifacts written before?
- **Decision:** built the PR so that the switch changes no artifact already written, and recommended leaving it so, over asking at the switch or a setting per artifact.
- **Peter:** "changing account setting to train should do nothing for artifacts. only when they're being edited (or new versions are created, such as profile updates, integrations, recent-context summaries), that should change the artifact's setting to the current account setting"
- **Score:** hit.
- **Source:** voice review 2026-09-29 (Opus 5.5).
- **Applies to:** PR #339.
- **Rule:** adopted 2026-10-06: a call goes to the training key only if everything in its prompt allows it. External content quoted word for word never reaches training.

### 2026-10-01 · The server finishes an upload's reply; warnings stay private

- **Situation:** an audio file uploaded in Text mode now gets a reply (PR #347).
- **Decision:** built the PR so that the server starts the reply, which arrives even if the tab is closed, and only the owner gets the reply link and warnings such as "you hit your spending limit"; recommended keeping both.
- **Peter:** "yes" (the server starts the reply) · "yes" (owner only)
- **Score:** hit.
- **Source:** voice review 2026-10-01 (Opus 5.5).
- **Applies to:** PR #347.
- **Rule:** candidate: work a user started finishes without them, and Loore tells them when they come back.

### 2026-10-01 · Auto-generate can't know what the user wants

- **Situation:** in a Read thread with auto-generate on, an uploaded reply showed only the Read further button. LLM Response was hidden, though nothing was generated. Should a follow-up make such an upload get an answer?
- **Peter:** "with autogenerate off you have two options: either chat about the recommendations, or Read further. Autogenerate has no way to know which one the user wants, so it just doesn't autogenerate" · "what's not ok is that with autogenerate on the LLM response button is hidden, so the user sees only Read button. That is confusing, we should either figure out what the autogenerate should do in this case, and then show neither of the buttons, or accept the autogenerate doesn't know, but then show both."
- **Source:** voice review 2026-10-01 (Opus 5.5).
- **Applies to:** #387 (show both, decided 2026-10-02).
- **Rule:** "Show the choices" in `LOORE-ESSENCE.md`.

### 2026-10-01 · No button deletes an unfinished recording; Text mode will ask

- **Situation:** PR #338 stops a second tab from ending a live recording. Text mode still restored an abandoned recording into the box without asking.
- **Decision:** built the fix so that Discard and Send in the text box never delete an unfinished recording.
- **Peter:** "yes" (Discard and Send never delete an unfinished recording) · on asking first: "I think this is already the case for voice mode, isn't it? We should do it for text mode as well. But in a followup issue"
- **Score:** hit.
- **Source:** voice review 2026-10-01 (Opus 5.5).
- **Applies to:** PR #338, #388, #389.
- **Rule:** "Show the choices" in `LOORE-ESSENCE.md`.

### 2026-10-01 · A failed sync must not make the next one expensive

- **Situation:** X charges per bookmark read. A sync cut off partway left the older bookmarks missing for good (PR #334).
- **Decision:** after a sync stopped partway, the next sync read all bookmarks again, up to 800 (at most $4, expected once).
- **Peter:** "for accounts that never had the sync the $4 is ok" · "but it seems to me this sync all the way up to the 800 bookmarks could be triggered on a failed later sync. That's a big problem. $4 a night instead of a couple cents" · "what? The sync button should only sync new bookmarks, not all the 800"
- **Score:** miss.
- **Why it missed:** the full reread was expected once; a failure that repeats, or each press of the sync button, would repeat it.
- **Source:** voice review 2026-10-01 (Opus 5.5).
- **Applies to:** PR #334 (a resumed sync now reads only back to the last finished sync).
- **Rule:** candidate (awaits Peter's confirmation, asked 2026-10-06 in node 201350; the first wording, "a retry pays only for the missing work", was unclear to him because a retried single call costs the full price; it came from the X bookmark sync, PR #334): every billed call records its cost on the account it serves. A job that stops partway resumes where it stopped, so the next run doesn't pay again for work already done.

### 2026-10-01 · Stop after two failures, loudly

- **Situation:** background jobs whose output is cut off now save nothing (PR #375). Without a limit, a refused job would repeat the same billed call on every scheduler run.
- **Decision:** a retry backoff: one hour, then four hours, then weekly, with an error to Sentry from the third failure; recommended as built.
- **Peter:** "the error is logged after a week? That's too long. If a profile generation got cutoff with 32k tokens output limit, that's way too much! Two errors should be enough to stop completely and log a very loud error on Sentry" · on an import that triggers a rebuild: "Then I'd skip the wait and go right ahead" · on the Build profile button for a stopped user: "it should pop up a dialog informing me the profile generation failed twice, and whether I really want to start another build." · "Cut-off intentions count as success, empty artifact no. But admin dashboard shows it as success. This should be fixed."
- **Score:** miss.
- **Why it missed:** the schedule avoided stopping for good, because nothing would restart a stopped job.
- **Source:** voice review 2026-10-01 (Opus 5.5).
- **Applies to:** PR #375, #368.
- **Rule:** adopted 2026-10-06: problems show, they are not hidden. Fail loudly, stop after repeated failures, and fix the cause rather than what is displayed.

### 2026-10-01 · Every billed call records its cost

- **Situation:** in the review of PR #375, the voice todo merge and the references digest skipped an empty output without recording what the call cost.
- **Decision:** left this out of the PR as a listed follow-up, among the choices that ship unless Peter objects.
- **Peter:** "they should log the cost. Pls fix in this PR"
- **Score:** miss.
- **Source:** voice review 2026-10-01 (Opus 5.5).
- **Applies to:** PR #375.
- **Rule:** candidate (awaits Peter's confirmation, asked 2026-10-06 in node 201350; the first wording, "a retry pays only for the missing work", was unclear to him because a retried single call costs the full price; it came from the X bookmark sync, PR #334): every billed call records its cost on the account it serves. A job that stops partway resumes where it stopped, so the next run doesn't pay again for work already done.

### 2026-10-01 · What the Read is for

- **Situation:** Peter's notes on the personal feed (the Read), written over the previous two weeks.
- **Peter:** "the intention for the feed is to stay synced with the society without being addicted to the feed and with as little effort as possible" · "imagine coming to a good knowledgeable friend of yours and asking them to give you an update in their area of expertise." · "we also want to incentivize intentionality (and instrumentally also reflection) so we should gate on reflection"
- **Source:** Peter's notes, shared in the voice review of 2026-10-01.
- **Applies to:** the Read.
- **Rule:** adopted 2026-10-06: reflection comes first. A Read answers a reflection, also in a newcomer's first session, and reading more means reflecting again. A Read does not start by itself after each reflection: the user opens it from its card (Reflect, then Read), and reflecting alone is fine.

### 2026-10-01 · The Read stays on Chat

- **Situation:** the Read quotes other people's tweets. Can a user set it to Train?
- **Peter:** "it should always use chat, user shouldn't be able to use train (we don't have license to train on public tweets)"
- **Source:** Peter's notes, shared in the voice review of 2026-10-01 (marked done, by PR #307).
- **Applies to:** PR #307, PR #365.
- **Rule:** adopted 2026-10-06: a call goes to the training key only if everything in its prompt allows it. External content quoted word for word never reaches training.

### 2026-10-01 · The note says why a pick matters to this user

- **Situation:** the Read presented picks with the reason added in italics after each tweet.
- **Peter:** "make the feed as Loore QT the threads. For reading the threads I'll probably open twitter, but the QT text should motivate me to do so (make it relevant to my situation). Not to add it in italics as a post-script. Once I read the QTed tweet, it should either be obvious why it's relevant, or not and in that case it's either a bad recommendation, or it's even more important to connect it to my actual situation"
- **Source:** Peter's notes, shared in the voice review of 2026-10-01 (marked done, by PR #307).
- **Applies to:** PR #307.

### 2026-10-01 · A Read the user waits for runs live

- **Situation:** every Read went through the Batch API. Most finished within 15 minutes; one in five took from 40 minutes to 7 hours.
- **Peter:** "batch processing is unusable for product - too unpredictable response time, too long in expectation"
- **Source:** Peter's notes, shared in the voice review of 2026-10-01.
- **Applies to:** the Read (user-facing Reads move from the Batch API to live calls).
- **Rule:** "Quality first, for now" in `LOORE-ESSENCE.md` (the Batch API is for jobs nobody waits for).

### 2026-10-01 · A new user must not wait for their profile

- **Situation:** prefilling a new user's profile from their public tweets takes a long time after signup.
- **Peter:** "prefill of new signups needs to be streamlined." · "This is necessary, otherwise prefill takes really long and that would be too big of a friction for activation"
- **Source:** voice review 2026-10-01 (Paid Beta planning).
- **Applies to:** Beta onboarding; #225.

### 2026-10-01 · A newcomer reflects first

- **Situation:** a newcomer has a profile but hasn't reflected yet. Do they get a first Read right away, or does the first session lead into a reflection?
- **Peter:** "yes, we need to show them how easy the reflection is while they are still curious about Loore. The next day they will possibly have already forgotten about Loore"
- **Source:** voice review 2026-10-01 (Paid Beta planning; Opus 5.5).
- **Applies to:** Beta onboarding; #391.
- **Rule:** adopted 2026-10-06: reflection comes first. A Read answers a reflection, also in a newcomer's first session, and reading more means reflecting again. A Read does not start by itself after each reflection: the user opens it from its card (Reflect, then Read), and reflecting alone is fine.

### 2026-10-01 · An untouched pick is neutral

- **Situation:** comparing Read models on Peter's past Reads.
- **Decision:** counted a pick he didn't mark as "not good".
- **Peter:** "no, it should count as neither good nor bad. A recommendation I'm not offended by, but also not excited about. If there is enough good ones, these neither good nor bad ones don't matter that much."
- **Score:** miss.
- **Source:** voice review 2026-10-01 (Paid Beta planning; Opus 5.5).
- **Applies to:** Read model evaluations; #352.
- **Rule:** adopted 2026-10-06: recommend fewer, better. An empty Read is a good result, an untouched pick is neutral, and Read uses no patterns from extractive feeds.

### 2026-10-01 · Reading more takes reflecting again

- **Situation:** the original design gave extra Reads from a cheaper, blander model, so that reading on would be less rewarding.
- **Decision:** proposed one Read per reflection instead: reflecting again gives a new Read.
- **Peter:** earlier, in his notes: "if the user wants to scroll further, we can offer them Luna, which is dirt cheap, produces some good recommendations, but is more bland on avg -> disincentivizes further scrolling" · now: "makes sense to me"
- **Score:** hit.
- **Note:** this replaces the earlier design.
- **Source:** Peter's notes and the voice review of 2026-10-01 (Paid Beta planning; Opus 5.5).
- **Applies to:** the Read.
- **Rule:** adopted 2026-10-06: reflection comes first. A Read answers a reflection, also in a newcomer's first session, and reading more means reflecting again. A Read does not start by itself after each reflection: the user opens it from its card (Reflect, then Read), and reflecting alone is fine.

### 2026-10-01 · Admins see no more user content than anyone else

- **Situation:** some status routes let an admin read any user's replies and transcripts. Should admins read user content through these routes at all?
- **Peter:** "Admins shouldn't be able to read any node's text directly. Via code on prod yes, but we never do it"
- **Source:** Claude Code session 2026-10-01 (Opus 5.5).
- **Applies to:** PR #395 (merged).
- **Rule:** "Nobody decrypts users' content" in `LOORE-ESSENCE.md`.

### 2026-10-01 · No AI in Voice mode when AI usage is None

- **Situation:** Voice mode sent every recording to the model and spoke the reply, even in threads set to None. PR #395 made content marked None refuse new text-to-speech.
- **Decision:** kept text-to-speech for model replies, so Voice mode still spoke its replies in None threads.
- **Peter:** "Voice threads shouldn't produce a textual llm response when ai_usage is none, so why do we allow tts?"
- **Score:** miss.
- **Source:** Claude Code session 2026-10-01 (Opus 5.5).
- **Applies to:** PR #396 (merged): no reply anywhere under content marked None, and Voice mode explains instead of recording.
- **Rule:** adopted 2026-10-06: AI usage None means no AI. Nothing marked None reaches a model by any route, and no job runs for an account set to None. The user's own data export is the exception.

### 2026-10-01 · Agents open pull requests; Peter merges

- **Situation:** which agents may open pull requests, and who merges? Merging to main deploys to production.
- **Peter:** "Every agent should be able to open PRs" · "Yeah, merges should be done by me"
- **Source:** Claude Code session 2026-10-01.
- **Applies to:** all pull requests.
- **Rule:** adopted 2026-10-06: reversible work goes ahead without asking and is flagged afterwards; every added heuristic is named; merging to main stays Peter's.

### 2026-10-02 · The Read doesn't look like an extractive feed

- **Situation:** planning the Paid Beta: the Read's name, and the tweet card under each pick, which showed no avatar or display name.
- **Decision:** recommended a display name, handle and avatar on each card, because "on Twitter that's half of how people judge a tweet".
- **Peter:** "My default is Feed. Or Personal Feed. But ofc this associates with the doomscrolling, or at least the mainstream extractive addictive feeds. Twitter has For You. That's a good direction, but ofc twitter is exactly the doomscrolling and extraction, so they already spoiled the name." · "I'm against avatar. What you name is a true pattern for people scrolling twitter. But it's a pattern optimized for extractive feeds. We wanna break it, we don't want Loore's users to just scroll and skim the recommendations. We want them to be intentional and read through them. Slow. It's usually 1 - 6 of tweets. Not the dozens one needs to scroll through on twitter"
- **Score:** partial. The display name stayed; the avatar did not.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** the Read's name and tweet card.
- **Rule:** adopted 2026-10-06: recommend fewer, better. An empty Read is a good result, an untouched pick is neutral, and Read uses no patterns from extractive feeds.

### 2026-10-02 · The Beta cohort comes with profiles

- **Situation:** planning the Paid Beta: when to show the Read to the first testers from outside.
- **Decision:** made it a condition that the Read works for someone with no profile and no intentions.
- **Peter:** "no, this is an assumption you're repeatedly making that I disagree with" · "for Beta we'll be targeting people already aligned with Loore's vision and those already present in the CA" · "for this first public cohort we'll be targeting those that will have a profile and intentions ready from their public tweets" · "later on we can extend this to users with no public data or with just tweets and no CA. But not for Beta"
- **Score:** miss.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** Beta features and evaluations (first Read, onboarding).

### 2026-10-02 · An empty Read is a good result

- **Situation:** Reads that found nothing worth reading were not counted anywhere.
- **Peter:** "we should at least count them separately. No recommendation is a good recommendation (at least for me, who already has trust built that if there are good tweets, Loore finds them. So if Loore finds nothing, I'm glad I saved my time.)"
- **Source:** voice review 2026-10-02 (Paid Beta planning).
- **Applies to:** Read tracking.
- **Rule:** adopted 2026-10-06: recommend fewer, better. An empty Read is a good result, an untouched pick is neutral, and Read uses no patterns from extractive feeds.

### 2026-10-02 · The iPhone app goes to alpha testers first

- **Situation:** planning the Paid Beta. The native iPhone app worked on Peter's phone. The App Store needs Apple billing, Sign in with Apple and a review.
- **Decision:** proposed that the Paid Beta open on the web, with the iPhone app in TestFlight for free testers (Peter, the alpha users, the first testers) and the App Store later.
- **Peter:** "I would like to do this ASAP because we are currently in alpha, everyone is free. So that's consistent with the test flight conditions." · "before finalizing the beta, we would already know whether the iPhone app is really such a big improvement that it's worth it to go through the additional steps to put it into App Store" · "Maybe the feedback will show that the app needs much more work and we will postpone it."
- **Score:** hit.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** PR #384.

### 2026-10-02 · Prod gets its own API key

- **Situation:** Loore's spend monitor counts only prod's costs, but the provider's spend limit counts everything on the account, local and lab experiments included. So the warning could come after the cut-off. Separate keys for prod, or a monitor set below the limit?
- **Peter:** "however, on top of this, yes we should be detecting the specific errors" · on a separate key and workspace for prod: "yes, we need this. Pls walk me through it when it makes sense"
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** #360, #369.
- **Rule:** "A user's model is theirs" in `LOORE-ESSENCE.md` (account failures are prevented).
