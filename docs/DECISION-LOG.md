# Decision log

Peter's product decisions, in his own words, with the prediction the builder made before he answered. It exists so that `LOORE-ESSENCE.md` can learn from his reviews, and so that the rates show where the builder's model of his choices is good enough to decide alone. The builder is the agent that builds Loore. Its model changes over time, so each entry names the model that made the call.

Started: 2026-10-02. Entries dated before then are backfilled: mostly Peter's corrections, plus "Accepted by merge" rows for PRs merged since 2026-09-01.

This file is raw data: entries and tables, no computed counts. Rates come from `python3 ~/.claude/skills/decision-log/stats.py docs/DECISION-LOG.md` (options `--kind`, `--model`, `--since`, `--last N`, `--json`; `--timeseries` counts per week; `--check` finds entries that don't parse). They are counted in three types: predictions; decisions, which include the rows accepted by merge; and escalations, whose two errors are counted apart. Patterns in the builder's misses are written up in `DECISION-PATTERNS.md`.

How it works:
- **Entry format.** A heading `### YYYY-MM-DD · Title`, then the lines Kind, Situation, Prediction or Decision, Peter (his words verbatim), Score, Why it missed (for a miss), Source (with the model that made the call), Applies to, Rule. Score is `hit`, `partial`, `miss`, `2 × miss` or parts joined by `+` (`miss + hit`), optionally followed by an explanation; an entry with neither a Prediction nor a Decision line is `not scored`. A partial counts as half a hit.
- **Before a review**, the builder writes down what it expects him to answer and how sure it is. The prediction is recorded before his answer, in Loore or in the builder's notes, and copied here afterwards.
- **After the review**, each entry gets his answer verbatim, a score (hit, partial or miss), and for a miss, what led to the wrong guess.
- **Decision lines** record what the builder did without asking (wrote text, built something, chose a default) before Peter reviewed it; a decision is scored like a prediction that he would approve, and backfilled entries (made before this log existed) have only these.
- **Escalation entries** (`**Kind:** escalation`) score whether to ask was the right call, as a decision of its own. One sits right after the entry it belongs to, with the same Situation, a Decision of "decided without asking Peter" or "asked Peter instead of deciding", Peter's words about the asking, and a hit or miss. The two errors are counted apart: a miss on "asked Peter instead of deciding" is an over-escalation (asked when it should have decided), and a miss on "decided without asking Peter" is an under-escalation (decided when it should have asked).
- **Entries arrive** in these ways:
  - a prediction, written before a brief or reply goes out, and scored when Peter answers;
  - Peter corrects a decision the builder made, in any channel (a voice review, a Claude Code session, a PR comment): a Decision entry, scored as a miss;
  - Peter agrees with a decision but wanted to be asked first: two entries, the decision scored as a hit and an escalation entry ("decided without asking Peter") scored as a miss;
  - Peter says a question should have been decided without him: an escalation entry ("asked Peter instead of deciding") scored as a miss, and any recommendation in the brief scored as a normal prediction entry;
  - an issue filed days or weeks after a PR traces back to a decision the builder made in that PR: a Decision entry with the issue as its source, scored as a miss, with what the builder could have seen at the time;
  - a PR merges, and the builder decisions it lists that Peter didn't correct count as decisions scored as hits, "accepted by merge": a row in "Accepted by merge" below, or the Result of a row in "Decided by the builder";
  - not logged: implementation details nobody reacted to, and bugs that reviewers find.
- **Rules** that come out of his answers are proposed for `LOORE-ESSENCE.md`. Nothing changes that document without his approval.
- **Whether to ask** is the builder's judgement, informed by the rates for the kind at hand. There are no fixed thresholds (Peter, 2026-10-08).
- **Left out:** security problems until they are fixed (then they come in, with the prediction and decision), other users' data, and Peter's personal matters.

Kinds (one per entry and per table row):
- **cost**: what Loore spends: model choice for cost or quality, the Batch API, billing, cost records.
- **provider**: the provider and model a user chose, provider failures, API keys and provider accounts.
- **privacy**: who may see or process users' content: encryption, AI-usage settings, the training key, consent, public and private content, deletion, account security.
- **user-facing text**: words that users or the public read: labels, tooltips, messages, descriptions of Loore.
- **data safety**: users' data stays intact: edits kept, nothing lost or duplicated by jobs, syncs, recordings or migrations.
- **product scope**: what a feature is for, who it is for, what it includes and what waits.
- **UX**: how a feature behaves for the user: flows, the choices shown, dialogs, notifications, waiting times.
- **reliability**: what happens when calls or jobs fail or come near a limit: retries, stops, margins, timeouts, alerts, fixing the cause.
- **testing**: tests, evaluations, and the measures that judge a feature.
- **release/ops**: deploys, feature flags, access to prod, infrastructure and performance settings.
- **process**: how the builder and Peter work: briefs, reviews, this log, pull requests, bug reports.
- **escalation**: whether the builder should have asked or decided (its own entry, see above).

## Entries

### 2026-10-02 · A provider fails for an account reason

- **Kind:** provider
- **Situation:** a provider fails for a reason waiting won't fix (spend limit, billing, revoked key), and a paying user's reply fails. Do we tell the user which provider failed, and switch them to the other provider at our cost?
- **Prediction:** name the provider, yes (75 %). Fall back to the other provider automatically, with the reply showing which model wrote it (55 %; second guess: a one-click "answer with GPT instead").
- **Peter:** "no, we should just prevent failures for account reasons. They will happen anyway in case of a rapid user growth. But then it'll be fine and we'll figure it out. Changing providers is however never acceptable."
- **Score:** 2 × miss.
- **Why it missed:** friction and cost were weighed, and the provider was treated as a detail Loore can swap. For Peter, the model a user chose is not Loore's to change, and account failures are to be prevented, not designed around.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** #369 (plain "temporarily unavailable" message and an alert to Peter; no fallback, provider not named), #360.
- **Rule:** "A user's model is theirs" in `LOORE-ESSENCE.md`.

### 2026-10-02 · Cheaper models and the Batch API for background jobs

- **Kind:** cost
- **Situation:** for background jobs such as the todo merge, recent context or intentions, may the cheapest model or the Batch API be chosen without asking whenever a blind comparison shows no loss and the result can be some hours old?
- **Prediction:** yes: the builder decides as long as it records the comparison and the choice; Peter judges user-facing text, the builder judges mechanical jobs (65 %).
- **Peter:** "batch api for background jobs is a natural choice, use it wherever it makes sense" · "on the other hand, cost vs. quality is something I'd like have a say in" · "what could remove some decision burden from me would be making comparisons between different models without my say, if it's cheap - so I can imagine you making an experiment where you run a comparison with the cheapest available model (like GPT-6 Luna) against my prod data and compare against the frontier models that I triggered in the past manually. Like todo merges" · "this is still on prod, so I'd like to start the experiment manually myself, but you can expect me to want to run such experiments" · "we're still in a really early phase, so we're not optimizing for cost. We want to build the best product we can, and if paying for frontier models is what it takes, then ok. Later, when we have too many todo merge requests and it costs us a ton, only then it will make sense to run such experiments, whose aim is cost optimization" · "smaller models also may have better latency, and that is something that influences UX a lot. So when todo merges have 100% accuracy on both frontier, and small models, and small models have significantly better latency, then that would be desired."
- **Score:** partial. The Batch API part was right; deciding the model without Peter was wrong.
- **Why it missed:** the digest cost incident was generalised into a cost rule, and latency was missed as a criterion.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** #380 (recent context through the Batch API, ready to build), #234 (a comparison script for the todo merge, accuracy and latency, for Peter to run), #357.
- **Rule:** "Quality first, for now" in `LOORE-ESSENCE.md`.

### 2026-10-02 · A job regenerates something the user edited

- **Kind:** data safety
- **Situation:** when a job regenerates something the user has edited (profile, todo list, intentions), does it keep the user's edits and refresh only its own part?
- **Prediction:** the user's edit wins; the job keeps it and updates around it (80 %).
- **Peter:** "yes?" · "I don't see what other options there are. Could you give me some examples? Why would a model regenerate something a user has editted?"
- **Score:** hit. The other options were overwriting the edit (the #183 bug) or freezing the document once edited.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** #183 / PR #210, #225, #216, #143.
- **Rule:** "The user's edit wins" in `LOORE-ESSENCE.md`.

### 2026-10-02 · A bug nobody can reproduce

- **Kind:** process
- **Situation:** with no reproduction, may the reporter be asked directly, a defensive fix with error logging be shipped, and the issue be closed if it doesn't recur?
- **Prediction:** logging yes; asking the reporter yes, but Peter asks them himself; no fix that only hides the symptom; close after a stated quiet period (55 %).
- **Peter:** "I don't think you have a way of contacting the reporter directly, unless they reported a bug via their own github account? Well, in that case yes, you can ask them directly. Btw this policy may change in the future - it's for current alpha and early Beta. If there is significant growth, we might want to rethink PR policy" · "or did you mean via the changelog feature in Loore? Users can submit issues via Loore, which uses my hrosspet account, but tags the issue with their username. We're letting the users know when an issue they submitted is resolved. You propose to use the same mechanism? In that case I give the same response as for the above case"
- **Score:** miss, on the part he answered (who asks). The defensive fix and closing were not answered.
- **Why it missed:** a social reason (people he knows) was assumed where the real limit is technical: Loore can tell a reporter that their issue is closed, but can't ask them a question.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** #244, #249, #287, #274.
- **Rule:** "Bug reports" in `LOORE-ESSENCE.md` (alpha and early Beta).

### 2026-10-02 · Loore can't know what the user wants next

- **Kind:** UX
- **Situation:** when Loore can't know what the user wants to do next, does it show the choices instead of guessing?
- **Prediction:** show the choices, two at most, nothing pre-selected (65 %).
- **Peter:** "yes"
- **Score:** hit.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** #387 (show both buttons; ready to build), #349.
- **Rule:** "Show the choices" in `LOORE-ESSENCE.md`.

### 2026-10-02 · Account deletion

- **Kind:** privacy
- **Situation:** how long a grace period, and do we keep the cost records in anonymous form?
- **Prediction:** 14 days (40 %; second guess 30). Cost records kept, detached from the person (75 %).
- **Peter:** "I don't know" · "is there some law informing these decision?" · "my intuition tells me 30 day grace period" · "and yes, we keep cost records in anonymous form"
- **Score:** miss + hit: the grace period a miss (second guess right), the cost records a hit.
- **Why it missed:** the guess rested on a legal deadline that had not been checked. No law sets a grace period: GDPR asks for erasure without undue delay and an answer within a month (extendable by two for complex cases), and a 30-day window the user agrees to is common practice.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** #268, #269.
- **Rule:** "Deletion" in `LOORE-ESSENCE.md`.

### 2026-10-02 · Experiments on other users' archives

- **Kind:** privacy
- **Situation:** may experiments run on other users' archives if the outputs stay in files and never reach their accounts?
- **Prediction:** public data (Community Archive tweets) yes; private Loore writing only Peter's own archive plus users who agreed to that experiment (75 %).
- **Peter:** "no, we never decrypt user's content" · "we can run scripts over their unencrypted metadata" · "if we need content, we can run it on my data (I have plenty), if the experiment is non-destructive - ie keeps the original data untouched, just used for comparison"
- **Score:** partial. Peter's own archive was right; the consent exception was wrong.
- **Why it missed:** the consent idea came from the builder's own triage note on #359, taken as his view.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** #357, #359, the Read notes test.
- **Rule:** "Nobody decrypts users' content" in `LOORE-ESSENCE.md`.

### 2026-10-02 · Where the decision log lives

- **Kind:** process
- **Situation:** a log in the public repository, or a private one in Loore or the builder's notes?
- **Prediction:** the public repository, with anything security-related left out (60 %).
- **Peter:** "yeah, let's try putting it on the public repository in the spirit of transparency (this is your model of me / my vision of Loore, and this should be public)" · "what do you mean security-related? I'd keep out information that would reveal security issues publicly before they are fixed. But after they are fixed, I'd include publicly your predictions (made locally in your memory) and my decisions, or feedback from followup issues"
- **Score:** hit. "Security-related" narrows to unfixed security problems.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Rule:** "Transparency" in `LOORE-ESSENCE.md`.

### 2026-10-02 · When to post the decision situations

- **Kind:** process
- **Situation:** writing the predictions for the Paid Beta voice review. The brief on the open issues had asked when to post the situations for this log as separate nodes, and proposed doing it after the Beta scope is settled.
- **Decision:** dropped the question from the predictions as already answered, reading Peter's reply to the brief as "now".
- **Peter:** "yes" (to "I'd do it after we settle the Beta scope, so they cover only Beta-relevant issues").
- **Score:** miss.
- **Why it missed:** his "we can start right away with the example questions you asked above" covered the few examples in the brief, and was read as an answer about posting all the situations.
- **Source:** voice review in local Loore, 2026-10-02 (Opus 5.5): the brief is node 201225, his reply to it node 201261, the predictions node 201262, his answer node 201264.

### 2026-10-02 · Moving reversible engineering items to ready

- **Kind:** process
- **Situation:** seven to nine "needs your input" issues already recommended a reversible fix; under the standing rule (ask only before irreversible actions) they move to ready to build.
- **Peter:** "go ahead" · "yes"
- **Score:** not scored: he had answered before the predictions.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** #217, #218, #275, #224, #216, #374, #287; #379 and #331 except Peter's prod steps.

### 2026-10-05 · Recording keeps the headset's mic

- **Kind:** data safety
- **Situation:** the native iPhone app (PR #384) records voice turns; its design document set the audio session for recording.
- **Decision:** recorded in `.playAndRecord` with mode `.default`, not `.voiceChat` (which lowers playback volume), with Bluetooth A2DP allowed.
- **Peter:** issue #423: "In the native iPhone app, voice-mode recording over connected Bluetooth headphones fails partway through. After a while, the app plays its warning sound for a headphone/audio problem. Either most of the audio is lost, or the recording silently switches to the iPhone's built-in mic."
- **Score:** miss.
- **Why it missed:** the design chose `.default` without checking how the web app records: Safari records in `.videoChat`, a mode that keeps a connected headset's mic as the input, and that could have been checked before choosing.
- **Source:** issue #423 (2026-10-05) and PR #384 (Opus 5.5). The cause is confirmed: in the walk test of 2026-10-07 (PR #424), with `.videoChat` and the headset preferred as input, the input stayed on the headset mic through four turns with the phone locked, about 18 minutes, while in the bad turns of 10-04 and 10-05 it had moved to the phone's mic.
- **Applies to:** PR #384, #423, PR #424.

### 2026-10-06 · Todo merge model: Luna first

- **Kind:** cost
- **Situation:** the new todo-merge prompt (#234, PR #417) was not yet tested. Which model runs it, and who is compared with whom?
- **Peter:** "let's first run only Luna on the new prompt. And if it improves, decide what to do next. Is it perfect? Use it. Is it less than perfect? I'll run also the frontier model."
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** voice review 2026-10-06 (Opus 5.5).
- **Applies to:** #234, PR #431.

### 2026-10-06 · Todo merge returns edits

- **Kind:** data safety
- **Situation:** the comparison showed many copying errors, even from frontier models, when the merge rewrites the whole todo list.
- **Peter:** "I'm surprised how many errors even frontier models did. Copying is probably just a poor fit of a task for LLMs" · "I meant Option 2" (keep the proposal card and the confirmation; the merge returns edits) · "let's build the PR… Fixing todo merges via instructing frontier model to make specific edits, instead of rewriting the whole todo. Let's reuse the functionality from other artifacts" · "let's evaluate it on Opus 5.5". On a check that refuses ticks the proposal didn't name: "wouldn't this be brittle? Tentatively against it." On moving Priority Order out of the todo list: "yes".
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** voice review 2026-10-06 (Opus 5.5).
- **Applies to:** #234.

### 2026-10-06 · Read for every user: after a reflection, by choice

- **Kind:** product scope
- **Situation:** writing the Paid Beta scope brief from Peter's answers. Only an admin could start a Read (#435).
- **Decision:** wrote in the brief that in the Beta, a Read has to start by itself after each reflection, for every user.
- **Peter:** "no!" (it doesn't start by itself after each reflection) · "I expect most of new users come for the Read feature, so they will want to Reflect -> Read workflow, but some won't and it will still be ok to just Reflect." Rollout: "two env vars: one turns on Read for every user, default off; another env var for whitelisting users whom Read feature is shown to."
- **Score:** miss.
- **Source:** voice review 2026-10-06 (Opus 5.5).
- **Applies to:** #435.
- **Rule:** refines "reflection comes first" (adopted above).

### 2026-10-06 · No digest; the Read is a search

- **Kind:** product scope
- **Situation:** writing the same Paid Beta scope brief.
- **Decision:** listed "the digest" among the features that wait until after the Beta opens.
- **Peter:** "I'm not planning any digest feature… two types of problem related to the CA daily tweets: intentionally lossy compression (=digest…) and needle in the haystack search (=personal feed…). Digests already exist and we're not planning to build one for now. We're building the personal feed."
- **Score:** miss.
- **Why it missed:** a general note of Peter's on two kinds of problem (a digest, and a search for the needle) was taken as a plan for a digest feature.
- **Source:** voice review 2026-10-06 (Opus 5.5).
- **Rule:** candidate: Read is a needle-in-the-haystack search for this user now, not a summary of the day.

### 2026-10-06 · Data purge: build it, revoke the X login, 30 days to undo

- **Kind:** privacy
- **Situation:** the user-data purge (#268) was designed in PR #415 but not built. Revoke the X login at purge (it needs one decryption of the token)? An undo window for "Delete all my writing"?
- **Peter:** "build it" · revoking the X login at purge: "yes pls" · an undo for "Delete all my writing": "definitely, 30 days grace".
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** voice review 2026-10-06 (Opus 5.5).
- **Applies to:** #268, #415.

### 2026-10-08 · The name of the role

- **Kind:** process
- **Situation:** the log needed a name for the agent across model versions.
- **Prediction:** "the builder" was recommended over "Loore Code" and "Claude", with "Loore" advised against because it is the product's name. Peter was asked to choose; no confidence was recorded.
- **Peter:** "I like the builder"
- **Score:** hit.
- **Source:** Claude Code session 2026-10-08 (Opus 5.5).

### 2026-10-08 · Fix a flaky test where it shows up

- **Kind:** testing
- **Situation:** the iOS CI job failed on PR #424 in a test from the iPhone app's first PR (#384), unrelated to #424's change.
- **Decision:** judged it a flake and reran the job, without fixing the test.
- **Peter:** "pls fix the flaky test in the same PR"
- **Score:** miss.
- **Why it missed:** "not this PR's code" was taken as a reason to leave a known flaky test alone.
- **Source:** Claude Code session 2026-10-08 (Opus 5.5).
- **Applies to:** flaky tests found while working on any PR.
- **Rule:** candidate: a flaky test found during a PR is fixed in that PR, at its cause.

### 2026-10-08 · Haiku 5.5 as a Read model at the >100k price

- **Kind:** cost
- **Situation:** Peter proposed adding Claude Haiku 5.5 to the Read models, expecting Luna's price. Measured: every Read is over Haiku's 100k-token tier, about 7–8 cents per Read in batch against about 1 cent for Luna.
- **Prediction:** recommended adding it anyway, because quality decides in this phase (no confidence recorded).
- **Peter:** "yes pls, add it to the list of models for Read. We will see how good it is - if it's actually better than Luna, we can figure out how to get the input below 100k toks."
- **Score:** hit.
- **Source:** Claude Code session 2026-10-08 (Opus 5.5).
- **Applies to:** #452, #453.

### 2026-10-08 · Decision log v2: a global skill, approvals by merge, kinds

- **Kind:** process
- **Situation:** proposals for a second version of this log.
- **Prediction:** Q1: a standalone global skill for the decision log, usable in any project, plus a pointer that loads in every session (85 %). Q2: a builder decision listed in a merged PR that Peter didn't correct counts as a hit, "accepted by merge" (70 %). Q3: a kind tag on every entry, so the record is per area (75 %).
- **Peter:** "agreed to 1-3"
- **Score:** 3 × hit.
- **Source:** Claude Code session 2026-10-08 (Opus 5.5).
- **Applies to:** the decision-log skill; this log's Kind lines and "Accepted by merge" table.

### 2026-10-08 · No fixed thresholds for when to ask

- **Kind:** process
- **Situation:** the same proposals. Q4: when does the builder decide alone instead of asking?
- **Prediction:** Peter accepts fixed thresholds as a starting point: decide alone where the kind's hit rate is at least 80 % and the builder's confidence at least 70 % (55 %).
- **Peter:** "I'm not sure about hard rules for when to escalate. I'd go with judgement informed by the log for now."
- **Score:** miss.
- **Why it missed:** a fixed rule was proposed while most kinds hold only a few scored entries; Peter wants the log to inform the judgement, not to replace it.
- **Source:** Claude Code session 2026-10-08 (Opus 5.5).
- **Applies to:** the decision-log skill (when to ask).

### 2026-10-08 · Reviewer findings stay out of the log

- **Kind:** process
- **Situation:** the same proposals. Q5: do bugs that a review finds in the builder's PRs go into this log?
- **Prediction:** Peter agrees that they stay out (65 %).
- **Peter:** "yes, they stay out"
- **Score:** hit.
- **Source:** Claude Code session 2026-10-08 (Opus 5.5).
- **Applies to:** the decision-log skill.

### 2026-10-08 · The rates are counted outside the log

- **Kind:** process
- **Situation:** the first version of this log.
- **Decision:** kept the computed rates inside the log: hit-rate tables and counts, updated by hand at the end of each session.
- **Peter:** "I'd keep the decision log as raw data, count the rates in a separate doc, or make a script for it so that an agent can easily call it to look up the rates."
- **Score:** miss.
- **Why it missed:** the log was treated as a report to read rather than as data to count; counts kept by hand go stale and can't be filtered by kind or model.
- **Source:** Claude Code session 2026-10-08 (Opus 5.5).
- **Applies to:** this log (rates now come from `stats.py`), `DECISION-PATTERNS.md`.

### 2026-10-08 · Continue audio: repair other users' recordings?

- **Kind:** privacy
- **Situation:** the server's batch files for iPhone recordings reported a wrong length, so the phone stopped playing a continued recording early (fixed for new recordings in #458). Repairing the recordings already stored would mean decrypting other users' audio; the alternative was to fix new recordings only and repair just Peter's test node.
- **Prediction:** fix forward and repair only his node, with no scan of other users' recordings (85 %).
- **Peter:** "fix it for new recordings only, you can leave mine unfixed as it was just a test. Also, count that as a hit - fixing mine would have been ok, it's just unnecessary"
- **Score:** hit. Peter asked for it to count as one: repairing his node would have been fine, only unnecessary.
- **Source:** Loore voice review 2026-10-08 (Opus 5.5).
- **Applies to:** #458.
- **Rule:** "Nobody decrypts users' content" in `LOORE-ESSENCE.md`.

### 2026-10-08 · Todo merge: a separate call or inside the thread

- **Kind:** cost
- **Situation:** when the user confirms a todo proposal, the merge runs as a separate call with the merge prompt, the proposal and the newest list. The alternative was to let the thread's own agent make the edits: cheaper by at most about 3 cents, and only while the prompt cache is warm; several times dearer when the cache is cold on a long thread.
- **Prediction:** keep the separate call (80 %).
- **Peter:** "thx for the real numbers, that helped. Keep the separate call with edits"
- **Score:** hit.
- **Source:** Loore voice review 2026-10-08 (Opus 5.5).
- **Applies to:** #443, #234.

### 2026-10-08 · Todo merge by edits: no switch

- **Kind:** release/ops
- **Situation:** the builder had told Peter that #443 would ship switched off. It was built without a switch, so merging it moves every user to merges by edits at once. Asked: run the evaluation on his past merges first and then merge without a switch, or add the promised switch?
- **Prediction:** the evaluation first, then a merge without a switch (60 %).
- **Peter:** "there should be no switch, just ship it to everyone. It was a good decision" · "I'm gonna run the test for #443 and if it's 100% which it should be, I want it shipped"
- **Score:** hit.
- **Source:** Loore voice review 2026-10-08 (Opus 5.5).
- **Applies to:** #443.

### 2026-10-08 · Todo merge by edits shipped without the promised switch

- **Kind:** release/ops
- **Situation:** #443 changes how every user's todo merge works. A switch would let it reach some users first; without one, reverting the PR is the rollback.
- **Decision:** built #443 with no feature switch, although the builder had said in a voice review that it would ship switched off.
- **Peter:** "It was a good decision"
- **Score:** hit. This entry replaces the "Decided by the builder" row of 2026-10-06 for #443 (feature flag), so the decision is counted once.
- **Source:** Loore voice review 2026-10-08 (Opus 5.5).
- **Applies to:** #443.
- **Rule:** "Autonomy" in `LOORE-ESSENCE.md` (reversible work goes ahead without asking).

### 2026-10-08 · Mic alert played twice

- **Kind:** UX
- **Situation:** when a recording loses the microphone (a phone call takes it, or the headphones go), Loore plays an alert of three falling notes, on the web and in the iPhone app.
- **Decision:** #209 (commit 0c46f40b) played the alert's three notes twice, at 0 and 0.9 s, as an "urgent doubled" chime on the web. The iPhone app (#384) copied the sound note for note.
- **Peter:** "the sound played when I turn off the headphones while recording is too long. It's three notes with decreasing pitch played twice in a row. Change it to just once in a row. Do that for both iOS app and the web app."
- **Score:** miss.
- **Why it missed:** #245 asked for an audible alert, not a repeated one. The alert was the existing error sound's three notes, louder (gain 0.25 against 0.15) and played twice. The louder single pass already set it apart from the error sound, and the page plays it again when it returns to the foreground, so the second pass added length without telling the user anything more.
- **Source:** Peter's request of 2026-10-08, quoted in PR #456; the decision is PR #209's (Fable 5).
- **Applies to:** #209, #384, #456.

### 2026-10-08 · Old Sentry reports: delete them or let them expire

- **Kind:** privacy
- **Situation:** error reports sent to Sentry before #422 (server) and #459 (browser) could carry search words and other text from requests. Settings that scrub data in Sentry apply only to new reports, so the old ones could be deleted by hand or left to expire after the plan's 30 days.
- **Prediction:** delete the server project's old issues now and the browser project's after #459 deploys (80 %).
- **Peter:** "let it expire in 30 days" · "yeah, would delete them if we were in Beta"
- **Score:** miss.
- **Why it missed:** weighed as if Beta users' text were at stake. Before the Beta the reports hold mostly Peter's own and test data, so expiry within 30 days is enough.
- **Source:** Loore voice review and Claude Code session 2026-10-08 (Opus 5.5)
- **Applies to:** #422, #459.
- **Rule:** candidate: how urgent a privacy cleanup is depends on the phase; before the Beta, letting data expire is acceptable.

### 2026-10-09 · Cache writes on new threads: move the thread's timestamp out of the prompt start

- **Kind:** cost
- **Situation:** every new voice thread pays a full prompt-cache write of its ~100k-token system prompt (about $0.50 on Opus 5.5), the largest part of the conversation spend in early October. The rendered system prompt starts with the thread's own timestamp.
- **Decision:** recommended moving the timestamp out of the first bytes of the system prompt, so that threads started close together could share a cached prefix.
- **Peter:** "this is not a bug, it's a feature - we're feeding last 10k of raw conversation into the sys prompt, so it's expected each thread will have a cold cache"
- **Score:** miss.
- **Why it missed:** looked only at the first bytes. The render pins each thread's own window of recent raw entries (`created_before` the system node), so the prompt differs between threads anyway; the code that showed this was already open.
- **Source:** Claude Code session 2026-10-09 (Opus 5.5)
- **Applies to:** #462.
- **Rule:** candidate: a cold prompt cache on every new thread is expected, because the system prompt carries that thread's own recent raw entries.

### 2026-10-09 · New threads: make the reply wait for the cache pre-warm

- **Kind:** cost
- **Situation:** when a voice recording ends, Loore pre-warms the prompt cache with the ~100k-token system prompt and starts the reply at about the same time. On 2 of 6 new threads in early October the reply's request reached the API before the pre-warm had finished, so the reply wrote the whole prompt again and the pre-warm was paid for nothing. Fix: the reply waits for the pre-warm's "finished" signal, at most 10 s; replies without a pre-warm never wait.
- **Prediction:** yes, build it (70 %).
- **Peter:** "what do you mean wait a few seconds? Is this just an estimate, or do you intend to put there a constant number of second wait?" · "yes, go ahead with this PR"
- **Score:** hit.
- **Source:** Claude Code session 2026-10-09 (Opus 5.5)
- **Applies to:** #463.
- **Rule:** none new; "a few seconds" needed the mechanism stated (a signal with a capped wait, not a fixed sleep) before he decided.

### 2026-10-08 · Todo merge by edits: prompt rules and a local re-run

- **Kind:** testing
- **Situation:** the evaluation of #443 passed 17 of Peter's 18 past merges. The failures were a parent task ticked while sub-items were open, and a new top-level task put inside a sub-list. Peter could re-run the evaluation on prod, or the builder could fix the prompt and re-run it.
- **Decision:** added two prompt rules (a parent stays unticked while sub-items are open; new top-level tasks never go inside a sub-list) and re-ran the same 18 merges locally, from the results file Peter had copied (same inputs, the branch's code, about $0.80 on his key) instead of asking him to re-run on prod. Shipped on 18 of 18.
- **Peter:** "my reaction: great"
- **Score:** hit
- **Source:** Claude Code session 2026-10-08 (Opus 5.5)
- **Applies to:** #443, #234
- **Rule:** "Experiments run on Peter's data"; autonomy for reversible work in `LOORE-ESSENCE.md`

### 2026-10-08 · Todo merge evaluation re-run: ask or decide

- **Kind:** escalation
- **Situation:** the evaluation of #443 passed 17 of Peter's 18 past merges; the fix and the re-run were possible without him.
- **Decision:** decided without asking Peter
- **Peter:** "my reaction: great"
- **Score:** hit
- **Source:** Claude Code session 2026-10-08 (Opus 5.5)
- **Applies to:** #443, #234

### 2026-10-09 · The prompt cache keeps its 5-minute lifetime

- **Kind:** cost
- **Situation:** a 1-hour cache lifetime costs more on every first write (1.6 times) and saves on re-writes after 5 to 60 minutes. The builder priced both on the first eight days of October.
- **Decision:** left the 5-minute lifetime as it is: on that data the 1-hour lifetime costs about $1.3 more than it saves (about $4.8 saved on re-writes, against the higher writes). Decided without a question to Peter.
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Claude Code session 2026-10-09 (Opus 5.5)
- **Applies to:** #462, #463

### 2026-10-09 · First TestFlight build: a trial archive first

- **Kind:** release/ops
- **Situation:** the app's identifiers had to be registered with the developer team before the app record existed, and the first real build needed #424 (iOS) and #458 (backend) merged.
- **Decision:** made a trial Release archive and a local App Store export (no upload) to register the identifiers, and decided to cut the first real build from main only after #460.
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Claude Code session 2026-10-08 (Opus 5.5)
- **Applies to:** #460, #424, #458

### 2026-10-09 · The second home card is called Glean

- **Kind:** user-facing text
- **Situation:** the home page gets a second card that finds today's tweets for the user. Candidate names: Glean, Finds, Elsewhere, Kindred, Signal.
- **Prediction:** Glean, recommended (35 %); his own name or another round (30 %).
- **Peter:** "omg, Glean is actually really good"
- **Score:** hit: the cards became Reflect, Glean, Share.
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #435, #436

### 2026-10-09 · The Glean card's one-line text

- **Kind:** user-facing text
- **Situation:** the Glean card needs a one-line description.
- **Prediction:** "Loore finds today's tweets for you." (40 %).
- **Peter:** "how about Reflect, and Loore gleans for you."
- **Score:** miss
- **Why it missed:** recommended a description of what the card does; Peter wrote a pair that names the two cards and turns the card name into the verb.
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #435, #436

### 2026-10-09 · Navigation word for home

- **Kind:** user-facing text
- **Situation:** the navigation needs one word for the page with the cards. The brief recommended "Today"; no prediction was written first.
- **Decision:** recommended "Today" as the navigation word.
- **Peter:** "home"
- **Score:** miss
- **Why it missed:** chose a word for what the page shows; the page is the user's home, and the usual word fits better.
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #436

### 2026-10-09 · Tweet cards: full text or the 500-character preview

- **Kind:** UX
- **Situation:** a gleaned tweet card shows the first 500 characters of the tweet.
- **Decision:** proposed showing the full tweet text instead of the preview, to be kept unless Peter objected.
- **Peter:** "no, keep the preview as it is. I almost always click through to twitter"
- **Score:** miss
- **Why it missed:** assumed that reading in place is the goal; Peter reads the tweet on X, so a short preview that leads there is enough.
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #435, #473

### 2026-10-09 · The Read card's name

- **Kind:** user-facing text
- **Situation:** the card that shows recommended tweets was called Read in the briefs.
- **Decision:** named the card "Read".
- **Peter:** "still not satisfied with Read as the name"
- **Score:** miss
- **Why it missed:** the name came from the feature's internal job; Peter wants a word that fits the card pair (Reflect, Glean).
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #435, #436

### 2026-10-09 · The Glean card's first description

- **Kind:** user-facing text
- **Situation:** the Glean card needs a description for its start page.
- **Decision:** wrote "say or type what's on your mind, and get today's tweets for it".
- **Peter:** "too transactional… should be a one-liner"
- **Score:** miss
- **Why it missed:** wrote a sentence that explains the mechanism (input in, tweets out); Peter wants one short line that sounds like Loore.
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #435, #436

### 2026-10-09 · A user without Glean sees the usual home page

- **Kind:** UX
- **Situation:** #436: users without access to Glean keep today's home page with the Voice and Text cards.
- **Prediction:** yes (75 %).
- **Peter:** "yes"
- **Score:** hit
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #436

### 2026-10-09 · The welcome question also on the Glean card

- **Kind:** UX
- **Situation:** #436: a newcomer's welcome question is also asked on the Glean card.
- **Prediction:** yes, same rule as on the other cards (70 %).
- **Peter:** "yes"
- **Score:** hit
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #436

### 2026-10-09 · An entry made through Glean: chat reply or only the gleaning

- **Kind:** UX
- **Situation:** #435: an entry written through the Glean card gets only the gleaning, no chat reply?
- **Prediction:** yes, only the gleaning (55 %).
- **Peter:** "the current logic should stay for text gleaning - they start with a reflection, whatever number of turns they need. The Glean button will be there alongside the LLM Response · for voice we'll need to add a glean button below the button for recording next user message. Also to the lockscreen. Voice mode has auto-generate on, so the user should get a standard voice mode response, and then be able to choose between recording more, or hitting glean. · glean will always be synchronous call · when glean recommendation is ready, the view should automatically switch to text mode to display the recommendations"
- **Score:** miss
- **Why it missed:** treated the Glean card as a mode with its own flow; Peter wants the existing thread flow, with Glean as an action inside it.
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #435

### 2026-10-09 · Voice through Glean: the gleaning isn't played aloud

- **Kind:** UX
- **Situation:** #435: a voice entry made through Glean lands on the gleaning, without reading it aloud.
- **Prediction:** yes (65 %).
- **Peter:** "recommendations shouldn't be played aloud"
- **Score:** hit
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #435

### 2026-10-09 · A tapped tweet card opens the reference page

- **Kind:** UX
- **Situation:** a tapped card in a gleaning could open the pick's reference page in Loore (full text, listen, plus and minus), with "Open on X" as a button, or keep opening X as on main.
- **Prediction:** the reference page (70 %); keep opening X (20 %).
- **Peter:** "yes" · "love the mockup as a whole!"
- **Score:** hit
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #473

### 2026-10-09 · Mockup labels: good and bad, or the plus and minus icons

- **Kind:** UX
- **Situation:** the mockup of the gleaning page labelled the feedback buttons "good" and "bad".
- **Decision:** labelled the feedback buttons good and bad.
- **Peter:** "keep the plus minus icons instead of good bad. Could you make them visually fit with your mockup?"
- **Score:** miss
- **Why it missed:** replaced the existing reference-feedback framing (quote, plus and minus) with plain words in the mockup.
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #473

### 2026-10-09 · Where the Glean button appears

- **Kind:** UX
- **Situation:** #435: the recommended text mode = a Glean button next to LLM Response in every thread of a Glean user; voice = a Glean button (and a lock-screen control) only in threads started from the Glean card.
- **Prediction:** accepts (50 %).
- **Peter:** "I don't know. Let's talk it through"
- **Score:** not scored: not answered; Peter and the builder discussed it in the terminal (the entry after next).
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #435

### 2026-10-09 · Lock screen: a labelled Glean button in the app

- **Kind:** UX
- **Situation:** the iPhone app's Live Activity gets a labelled Glean button; the web keeps Glean on the page, because Media Session only has fixed media buttons.
- **Prediction:** accepts (60 %).
- **Peter:** "yes"
- **Score:** hit
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #435, #493

### 2026-10-09 · A per-user Glean switch and where the button shows

- **Kind:** UX
- **Situation:** after the previous question stayed open, the builder proposed: a per-user Glean switch, on by default for users with Community Archive or X data and off otherwise, changeable in Account; threads started from the Glean card keep the visible button (voice under the record button, text next to LLM Response); threads started from the Reflect card get no button, only "Glean for this reflection" in the node's menu, available later and at any time.
- **Prediction:** accepts as it is (45 %); accepts with a change to the Reflect-thread entry (35 %).
- **Peter:** "I like your proposal. The cost is acceptable and I wouldn't start with the link you mention at the end" · not verbatim in the notes: on by default also when X is connected later through Account
- **Score:** hit
- **Source:** Claude Code session 2026-10-09 (Opus 5.5)
- **Applies to:** #435, #473

### 2026-10-09 · The default Glean model for Anthropic users

- **Kind:** cost
- **Situation:** which Anthropic model runs a Glean by default.
- **Decision:** set Sonnet 5.5 as the default in the brief to the #435 builder (quality first).
- **Peter:** "default to Haiku 5.5"
- **Score:** miss
- **Why it missed:** applied "best product first" to a feature that runs on every user's request; Peter evaluated Sonnet 5.5 and Haiku 5.5 and wants the cheaper one as the default.
- **Source:** Claude Code session 2026-10-09 (Opus 5.5)
- **Applies to:** #435
- **Rule:** candidate: a feature that runs on every request, where an evaluated cheaper model exists, defaults to the cheaper model.

### 2026-10-09 · Referral reward: a free week for both

- **Kind:** product scope
- **Situation:** #439: what a referral earns the inviter and the friend.
- **Decision:** proposed the default: a free week for both, on the friend's first payment.
- **Peter:** "the default sounds good"
- **Score:** hit
- **Source:** Claude Code session 2026-10-09 (Opus 5.5)
- **Applies to:** #439

### 2026-10-09 · A referred friend without Community Archive data

- **Kind:** product scope
- **Situation:** #439: during the Beta, can a referred friend with no Community Archive data get in?
- **Prediction:** signs up through the link, the referral is recorded, and the friend waits for approval like any signup without archive data, with no automatic entry (60 %); referred friends are let in during the Beta (25 %).
- **Peter:** "go with your recommendation"
- **Score:** hit
- **Source:** Claude Code session 2026-10-09 (Opus 5.5)
- **Applies to:** #439

### 2026-10-09 · Referrals: the smaller defaults

- **Kind:** product scope
- **Situation:** #439: a friend who withdraws or is refunded costs the inviter nothing (no clawback); no cap on rewards; a provider without credit extends the current period.
- **Decision:** stated these as defaults.
- **Peter:** "the smaller defaults are also ok"
- **Score:** hit
- **Source:** Claude Code session 2026-10-09 (Opus 5.5)
- **Applies to:** #439

### 2026-10-09 · One tweet card in every thread

- **Kind:** UX
- **Situation:** #473: the new tweet card (name, handle, date, tap to open the reference page, "Open on X" button) is also used for references quoted in ordinary chat replies, not only in gleanings.
- **Prediction:** keep one card everywhere (65 %); gleanings only (30 %).
- **Peter:** "perfect"
- **Score:** hit
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #473

### 2026-10-09 · Glean read model: admins only, or everyone whitelisted

- **Kind:** provider
- **Situation:** #473: admins may pick another provider's read model next to Glean for their own evaluations (defaults never cross providers; non-admins get a 400).
- **Prediction:** keep it for admins (70 %); forbid it for everyone (25 %).
- **Peter:** "everyone whitelisted for Glean needs to be able to select from the same models as admins, as it is now" · not verbatim in the notes: later, one model per provider or a shortlist
- **Score:** miss
- **Why it missed:** read the provider rule as a limit on what users may pick; it limits what Loore may change for them.
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #473
- **Rule:** candidate: the provider rule forbids changing a provider without the user's knowledge; it does not forbid the user choosing one.

### 2026-10-09 · A model picker for Glean users

- **Kind:** cost
- **Situation:** #473: same-provider read models differ about 5 times in cost per glean. Should a non-admin choose the Glean model?
- **Prediction:** no picker, the server chooses (75 %); a picker in craft mode (20 %).
- **Peter:** "we'll need a model picker for everyone, with default selected based on the user's default model's provider"
- **Score:** miss
- **Why it missed:** weighed the cost spread and kept the choice with the server; for Peter the user's model choice comes first and the default handles the cost.
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #473

### 2026-10-09 · The provider rule: not without the user's knowledge

- **Kind:** provider
- **Situation:** the builder and the coordinator read "never switch a user's provider" as "a user can't pick another provider" and made the Glean model picker admin-only.
- **Decision:** built the picker for admins only, on that reading of the rule.
- **Peter:** "the rule about providers in the essence doc is that we don't change providers without user's knowledge, not that the user can't change providers"
- **Score:** miss
- **Why it missed:** took the rule, which is about Loore changing a provider silently, as a limit on the user's choices.
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #473
- **Rule:** "A user's model is theirs" in `LOORE-ESSENCE.md`

### 2026-10-09 · Share keeps its old card

- **Kind:** UX
- **Situation:** #474 restyled Share as a purpose card with one "Open" button for Glean users.
- **Decision:** restyled Share as a purpose card.
- **Peter:** cited in the coordinator's review, from an earlier decision: "Share stays as today"
- **Score:** miss: corrected by a review that quoted Peter's earlier decision; main's card stayed unchanged.
- **Why it missed:** the earlier decision was in the notes and was not checked when the card was restyled.
- **Source:** Claude Code session 2026-10-09 (Opus 5.5)
- **Applies to:** #474

### 2026-10-09 · The Glean reply is private under a public entry

- **Kind:** privacy
- **Situation:** a gleaning is created under a public entry of the user.
- **Decision:** created the gleaning reply private, even under a public entry.
- **Peter:** "great"
- **Score:** hit
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #473

### 2026-10-09 · Glean picker default: the provider's configured model each time

- **Kind:** UX
- **Situation:** #473: which model the Glean picker preselects.
- **Decision:** the provider's configured Glean model each time, with no remembered last pick; the picker sits where the admin picker is today.
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #473

### 2026-10-09 · Glean picker: the default provider follows the thread

- **Kind:** provider
- **Situation:** #473: Peter said the default is "based on the user's default model's provider". In a thread where the user moved to another provider, the account provider would send that thread to a provider the user did not choose for it.
- **Decision:** the picker's default provider is that of the user's own closest AI reply in the thread, else the account model, else the server default; a cross-provider pick never becomes a default.
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #473
- **Rule:** "A user's model is theirs" in `LOORE-ESSENCE.md`

### 2026-10-09 · Deleting an account revokes the X token

- **Kind:** privacy
- **Situation:** #415 / #464: when an account is deleted, Loore revokes the user's X connection. Write the call now, or have Peter write it, or ship without.
- **Prediction:** go, write it now (85 %).
- **Peter:** "I explicitly asked for revoke, so pls ship the PRs with revoke"
- **Score:** hit
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #415, #464

### 2026-10-09 · Terms: no version bump for the deletion sentence

- **Kind:** user-facing text
- **Situation:** #464 replaces "no account deletion feature" in the terms with a sentence about deleting an account. Bump `CURRENT_TERMS_VERSION` or not?
- **Prediction:** no bump (75 %).
- **Peter:** "ok"
- **Score:** hit
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #464

### 2026-10-09 · Terms: the backups sentence

- **Kind:** user-facing text
- **Situation:** the proposed terms sentence also said when copies in backups are erased. Peter could give the retention or say to drop the sentence.
- **Prediction:** drop or reword it (55 %).
- **Peter:** "I'd drop it, unless it's necessary"
- **Score:** hit
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #464

### 2026-10-09 · Terms: the deletion text the builder wrote

- **Kind:** user-facing text
- **Situation:** the brief proposed the wording for the terms and the app: an account is "hidden" after the request, and a sentence about backups.
- **Decision:** wrote the deletion text with the word "hidden" and a sentence about backups.
- **Peter:** "I wouldn't use the term hidden… deleted, with 30 days grace period… then deleted forever" · on the backups sentence: "not at all clear"
- **Score:** miss
- **Why it missed:** used the implementation's word (hidden) and a technical sentence; a non-technical reader needs what happens to their account and when.
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #464, #482
- **Rule:** candidate: user-facing text says what happens to the user, in the user's words, not in the implementation's.

### 2026-10-09 · Link speech for bare addresses

- **Kind:** user-facing text
- **Situation:** #471: how a bare address is spoken ("PR 460", "issue 423", "a link to example.com").
- **Prediction:** accepts as built (75 %).
- **Peter:** "great, ship it as it is"
- **Score:** hit
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #471

### 2026-10-09 · Link speech for profiles and saved references: now or later

- **Kind:** product scope
- **Situation:** #471 gives replies the new link speech. Profiles and saved references could get it in the same PR or in a follow-up issue.
- **Prediction:** later, file an issue (55 %); yes, now (35 %).
- **Peter:** "filing would also be ok, but now that we're talking about it, add them pls"
- **Score:** miss
- **Why it missed:** recommended deferring a small addition to the same feature; once Peter looks at the feature, he prefers it complete.
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #471

### 2026-10-09 · An iPhone "Delete my account" row now

- **Kind:** privacy
- **Situation:** App Store rule 5.1.1(v) requires in-app account deletion, and #269 was approved. The app has no such row.
- **Decision:** built the iPhone "Delete my account" row and the restore screen now, as an iOS PR from main, to merge after #464. Predicted that Peter accepts (85 %).
- **Peter:** "great"
- **Score:** hit
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #269, #482
- **Rule:** Deletion for every account; App Store 5.1.1(v)

### 2026-10-09 · Waitlisted accounts can delete themselves

- **Kind:** privacy
- **Situation:** #482's open question: an account on the waitlist is blocked from most routes, so it could not delete itself.
- **Decision:** exempted `POST /api/account/delete`, its confirmation and the restore routes from the waitlist block in #464; #482 shows the section on the waitlist screen. Predicted that Peter accepts (85 %).
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #464, #482
- **Rule:** Deletion for every account; App Store 5.1.1(v)

### 2026-10-09 · X sign-in accounts revoke their sign-in token too

- **Kind:** privacy
- **Situation:** #464 Needs Peter 1: the first build revoked only the token of the bookmark connection. The brief had described the sign-in case as well.
- **Decision:** revoke the session's X sign-in token when deletion is requested (best effort), and show the X fallback note to X-sign-in accounts too, without asking. Predicted that Peter accepts (85 %).
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #464
- **Rule:** Peter: "I explicitly asked for revoke"

### 2026-10-09 · X sign-in token revoke: ask or decide

- **Kind:** escalation
- **Situation:** #464 Needs Peter 1: the first build revoked only the token of the bookmark connection; the brief had described the sign-in case as well.
- **Decision:** decided without asking Peter
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #464

### 2026-10-09 · Backups must be gone by day 30

- **Kind:** privacy
- **Situation:** #464 Needs Peter 2: deleted accounts must also leave the backups within the 30 days the terms promise. PR #278 (database dump to storage) makes the backups.
- **Decision:** #278 must delete backups by day 30: suggested a 28-day lock and a 28-day lifecycle age, a heuristic because the storage lifecycle can run up to a day late. Commented on #278.
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #464, #278

### 2026-10-09 · Terms: a plain backups sentence stays

- **Kind:** user-facing text
- **Situation:** Peter said of the backups sentence "I'd drop it, unless it's necessary". The builder found that the backups outlive the promise unless the sentence says so, and that "deleted for good" is otherwise untrue.
- **Decision:** kept a plain sentence, "Copies in our backups are erased within 30 days after that.", after checking the backup retention. Predicted that Peter accepts (60 %) and drops it anyway (30 %).
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #464

### 2026-10-09 · Terms: rewritten in Peter's wording

- **Kind:** user-facing text
- **Situation:** after Peter's correction of the deletion text.
- **Decision:** rewrote the terms text as deleted, 30 days to recover by signing in, then deleted for good. Predicted that Peter accepts (70 %).
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #464

### 2026-10-09 · An AI reply in someone else's public thread uses no profile

- **Kind:** privacy
- **Situation:** #483: user B asks for an AI reply in user A's public thread. Use B's own profile and notes, or none (as built)?
- **Decision:** none: B's private context would shape a public reply. Reversible and safe. Decided in the reply to Peter instead of asking; predicted that Peter accepts (75 %).
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #483

### 2026-10-09 · Profile in someone else's public thread: ask or decide

- **Kind:** escalation
- **Situation:** #483: user B asks for an AI reply in user A's public thread. Use B's own profile and notes, or none?
- **Decision:** decided without asking Peter
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #483

### 2026-10-09 · Cost rows for refused requests stay as logged

- **Kind:** cost
- **Situation:** #488 (refusals): a request the provider refuses before any output has cost rows that are not $0 as logged. The billed categories are not clear yet.
- **Decision:** left the rows as logged, not $0, until the billed categories are clear; reversible, accounting only. Not asked; predicted that Peter accepts (80 %).
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #488, #470

### 2026-10-09 · Cost rows for refused requests: ask or decide

- **Kind:** escalation
- **Situation:** #488 (refusals): a request the provider refuses before any output has cost rows that are not $0 as logged.
- **Decision:** decided without asking Peter
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #488, #470

### 2026-10-09 · No Glean model picker on the iPhone Voice screen

- **Kind:** UX
- **Situation:** #493: the builder had added a Glean model picker to the iPhone Voice screen; on the web, #473 decided that voice and the menu entry use the default.
- **Decision:** removed the picker from the Voice screen to match the web. Not asked; predicted that Peter accepts (65 %) and that he wants a picker in voice on both (30 %).
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #493, #473
- **Rule:** one behaviour across clients (Peter's "card everywhere")

### 2026-10-09 · Glean picker on the iPhone Voice screen: ask or decide

- **Kind:** escalation
- **Situation:** #493: the builder had added a Glean model picker to the iPhone Voice screen; on the web, voice uses the default.
- **Decision:** decided without asking Peter
- **Peter:** no reaction yet.
- **Score:** not scored: no reaction yet
- **Source:** Voice review in local Loore 2026-10-09 (Opus 5.5)
- **Applies to:** #493, #473

## Decided by the builder

Choices that agents flagged in PRs and that the builder decided because a rule above already covers them (Peter, 2026-10-02: decide what the log supports, raise only real judgement calls). Each is also recorded on its PR, or in the session where Peter asked. The Decided column holds only the builder's part; a rule or decision of Peter's goes in the Rule column. Questions raised to Peter instead are not listed; his answers become entries. Model is the model that wrote the PR (its Co-Authored-By line). Result is "accepted by merge (PR #N, date)" once the PR has merged and Peter didn't correct the choice, "pending" until it merges, and "corrected → entry <title>" when he overrules it; the entry is then scored as a miss.

| Date | PR | Kind | Choice | Decided | Rule | Model | Result |
|---|---|---|---|---|---|---|---|
| 2026-10-02 | #406 | reliability | How often to email about one failing provider account | At most every 6 h per cause, as a setting | Every added heuristic is named | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-02 | #406 | reliability | A paid batch is refused for an account reason while polling | Keep polling until the cap | Prevent the failure where possible | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-02 | #409 | UX | How often finished recent-context batches are collected | Every 60 s | Latency counts as quality | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-02 | #401 | UX | A newcomer's first session and the updates window | `/welcome` skips the whole session | A newcomer reflects first | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-02 | #401 | product scope | What counts as the user's own entry | Session prompts, link nodes and deleted entries don't count either | Only entries written in Loore count; imports don't (Peter, 2026-10-01) | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-02 | #403 | product scope | iPhone only or iPhone and iPad | iPhone only | The design document | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-02 | #404 | provider | A chat reply is sent with a read-only model | Refused with a plain message. The first decision (run it on the chat default) was wrong: the review showed it can switch the provider | Never change providers, not even as a fallback | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-02 | #405 | testing | What counts as a dropped pick | Both kinds of picks Loore can't show | An empty Read is a good result | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-02 | #408 | UX | A failed auto-generate hides LLM Response outside Read | Follow-up issue #416 | Show the choices | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-02 | #413 | privacy | Admin-only guard, dry run, missing-items count in the todo-merge comparison | Kept | Experiments run on Peter's data; cost is his call | Opus 5.5 | accepted by merge (PR #413, 2026-10-06) |
| 2026-10-02 | #414 | data safety | The profile note says "keep what they wrote" | Kept | The user's edit wins | Opus 5.5 | pending |
| 2026-10-02 | #417 | data safety | The todo merge rewords items or moves them out of the section the user named | Keep wording; new items copied word for word; the named section is created if missing | The user's edit wins | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-02 | #415 | data safety | Purging an AI or system account | Refused | None needed: purging one would delete every AI reply in Loore | Opus 5.5 | pending |
| 2026-10-02 | #415 | privacy | Cost rows after a purge | Response ids, request refs and prompt-prefix hashes are cleared as well | Cost records are kept in anonymous form (Peter, 2026-10-02) | Opus 5.5 | pending |
| 2026-10-06 | #443 | data safety | Todo merge as edits: which sections change the list | Only New Tasks and Completed; Note, Issue and Priority Order don't | Extends Peter's "yes" on Priority Order | Opus 5.5 | pending |
| 2026-10-06 | #443 | data safety | Todo merge as edits: when a full rewrite is allowed | Only when the list has no tasks (nothing to anchor on); otherwise edits only | The user's edit wins | Opus 5.5 | pending |
| 2026-10-06 | #443 | data safety | Todo merge as edits: a check after the edits are applied | Every previous line is kept with its text, only [ ] to [x] may change; one retry, then fail. This differs from the proposal-wording check Peter leaned against. It sits in one removable function and the evaluation measures how often it fires | Every added heuristic is named | Opus 5.5 | pending |
| 2026-10-06 | #433 | reliability | A cut-off merge output | Fails with nothing saved; the message says to ask for the update again, since the card has no retry (#434) | Problems show | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-06 | #431 | release/ops | Running the comparison script on prod | From a separate worktree with `.env.production` symlinked, never by checking out a branch in the live app folder (the merge prompt file is read on every call) | Experiments run on Peter's data; he starts any run on prod | Sonnet 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #458 | reliability | A remuxed iPhone batch file still reports a wrong length | Rebuild it from the decoded audio (0.5 s tolerance, AAC 64 kbps), only for files that are already wrong | Fix the root cause, keep a fallback where the cause is outside our control | Opus 5.5 | pending |
| 2026-10-08 | #459 | privacy | Addresses in the browser's error reports (page, Referer, API calls, navigation, stack-frame files) | Query and fragment removed everywhere the SDK puts an address; paths kept | No user content reaches Sentry (#422) | Opus 5.5 | pending |
| 2026-10-08 | #459 | privacy | Console breadcrumbs from the browser | Dropped at every level, `console.error` included | No user content reaches Sentry (#422) | Opus 5.5 | pending |
| 2026-10-08 | #459 | privacy | How click and key-press breadcrumbs describe an element | Tag, id and classes only, no attribute values | No user content reaches Sentry (#422) | Opus 5.5 | pending |
| 2026-10-08 | #459 | privacy | `extra` on browser events | Dropped from every event | No user content reaches Sentry (#422) | Opus 5.5 | pending |
| 2026-10-08 | #459 | privacy | A request body or query string on a browser event | Dropped, although the SDK attaches neither today, so a later integration can't add them | No user content reaches Sentry (#422) | Opus 5.5 | pending |
| 2026-10-09 | #462 | cost | Where to strip trailing whitespace from the system block | In both the pre-warm's render and the reply's inline render | The pre-warm and the reply send byte-identical prefixes (#187, #192) | Opus 5.5 | accepted by merge (PR #462, 2026-10-09) |
| 2026-10-09 | #462 | cost | System-prompt renders cached before the fix, which may end in a newline | Render cache key v2 to v3: one extra system-prompt write for each thread active across the deploy, once | Fix the root cause | Opus 5.5 | accepted by merge (PR #462, 2026-10-09) |
| 2026-10-09 | #463 | reliability | What the pre-warm's "finished" signal carries | Its outcome (done, failed, or skipped with the reason), written to the worker log with each wait | Problems show | Opus 5.5 | accepted by merge (PR #463, 2026-10-09) |
| 2026-10-09 | #463 | reliability | Redis can't be read when the reply checks for the signal | No wait, a warning in the log, the reply goes ahead | Latency counts as quality; problems show | Opus 5.5 | accepted by merge (PR #463, 2026-10-09) |
| 2026-10-09 | #463 | UX | A reply on a different model from the one pre-warmed | Gets no wait (cannot happen today) | Latency counts as quality | Opus 5.5 | accepted by merge (PR #463, 2026-10-09) |
| 2026-10-09 | #463 | reliability | How often the reply checks, and how long the signal is kept | Every 0.2 s; signal kept 1 h, longer than finalize's wait for the last chunks (up to 10 min); the 10 s maximum is a setting and 0 turns the wait off | Every added heuristic is named | Opus 5.5 | accepted by merge (PR #463, 2026-10-09) |
| 2026-10-09 | #473 | cost | Which Glean turns run live | Every read turn, except `/read/start` and the batch rerun | Every added heuristic is named | Opus 5.5 | pending |
| 2026-10-09 | #473 | UX | What a click on Glean does | Always creates the reply | Latency counts as quality | Opus 5.5 | pending |
| 2026-10-09 | #473 | user-facing text | The label of the action | Stays "Glean" | Peter named the card (2026-10-09) | Opus 5.5 | pending |
| 2026-10-09 | #473 | UX | Where the menu entry for Glean shows | On every own entry, not on system prompts, pending entries or AI None | candidate: actions show where they can run | Opus 5.5 | pending |
| 2026-10-09 | #473 | UX | Voice Glean while the reply is still coming | Disabled until the reply lands; opens the thread on finish or failure | Problems show | Opus 5.5 | pending |
| 2026-10-09 | #473 | release/ops | The Glean route with the switch off | 403 | Dark features stay dark | Opus 5.5 | pending |
| 2026-10-09 | #473 | product scope | The per-user Glean switch's default | Computed live from six kinds of data (Community Archive, X and others) | Every added heuristic is named | Opus 5.5 | pending |
| 2026-10-09 | #473 | testing | Opening a reference page | Does not count as a read | Only a shown Read counts as opened (#405) | Opus 5.5 | pending |
| 2026-10-09 | #473 | data safety | The name of a quoted author | Filled onto the user's own tweet row | The user's edit wins | Opus 5.5 | pending |
| 2026-10-09 | #473 | release/ops | The backfill for those names | A script, run by hand | No heavy scripts on the prod VM | Opus 5.5 | pending |
| 2026-10-09 | #473 | user-facing text | Prompt titles | "Glean" | Peter named the card (2026-10-09) | Opus 5.5 | pending |
| 2026-10-09 | #473 | release/ops | Environment variable names | `GLEAN_*` | candidate: names follow the product name | Opus 5.5 | pending |
| 2026-10-09 | #473 | user-facing text | Why a pick appears | "chosen for what you said" | candidate: say what happened in the user's words | Opus 5.5 | pending |
| 2026-10-09 | #473 | user-facing text | A changelog entry while the feature is dark | None | Dark features stay dark | Opus 5.5 | pending |
| 2026-10-09 | #473 | user-facing text | A way out of a gleaning | "Back to Home" under it | candidate: every screen has a way home | Opus 5.5 | pending |
| 2026-10-09 | #473 | privacy | Reads for non-admins | Only through Glean (typed `{ca_tweets}`, read-prompt text, `/read/start` admin-only) | Experiments run on Peter's data; admin tools stay admin tools | Opus 5.5 | pending |
| 2026-10-09 | #473 | cost | Who picks a non-admin's Glean model | The server always picks; no reuse of an earlier read's model | Cost is Peter's call | Opus 5.5 | corrected → entry A model picker for Glean users |
| 2026-10-09 | #473 | reliability | A glean when the provider or the setting can't be resolved | Refused | Problems show | Opus 5.5 | pending |
| 2026-10-09 | #473 | privacy | Admin model choice | Only under the admin's own node | Admins never read content via the API | Opus 5.5 | pending |
| 2026-10-09 | #473 | provider | The Glean picker's default provider | The user's own closest AI reply in the thread, else the account model, else the server default | A user's model is theirs | Opus 5.5 | pending |
| 2026-10-09 | #473 | provider | An earlier same-provider read of the user's own | Kept as the default; a cross-provider pick never becomes a default | A user's model is theirs | Opus 5.5 | pending |
| 2026-10-09 | #473 | UX | Voice and the menu entry | No picker; they use the default | One behaviour across clients | Opus 5.5 | pending |
| 2026-10-09 | #415 | data safety | The abandon race in account deletion (review finding) | Fixed in the PR | Fix the root cause | Opus 5.5 | pending |
| 2026-10-09 | #464 | privacy | Privacy and text findings from the review | Fixed in the PR | Fix the root cause | Opus 5.5 | pending |
| 2026-10-09 | #464 | privacy | A 300 s public cache for other thread replies, and "taken" / "reserved" usernames | Left as they are | candidate: findings that break no promise are left | Opus 5.5 | pending |
| 2026-10-09 | #466 | testing | An extra test the review asked for | Left out | candidate: findings that break no promise are left | Opus 5.5 | pending |
| 2026-10-09 | #467 | UX | An icon finding | Fixed | Fix the root cause | Opus 5.5 | pending |
| 2026-10-09 | #468 | process | A content hunk in the PR, and a follow-up | Hunk reverted; #470 filed | One PR, one purpose | Opus 5.5 | pending |
| 2026-10-09 | #469 | reliability | A one-request race | Left; follow-up #477 | candidate: findings that break no promise are left | Opus 5.5 | pending |
| 2026-10-09 | #472 | reliability | `status_reported` and two lower findings | `status_reported` fixed; the others go to #477 | Fix the root cause | Opus 5.5 | pending |
| 2026-10-09 | #415 | privacy | Which X tokens are revoked at deletion | Access and refresh token; an expired access token is skipped | Deletion for every account | Opus 5.5 | pending |
| 2026-10-09 | #415 | reliability | How the revoke call is sent | `client_id` in the body and Basic auth | Fix the root cause | Opus 5.5 | pending |
| 2026-10-09 | #415 | reliability | Errors from the revoke call | Logged as an error, as a warning when X already refused; the outcome is not stored | Problems show | Opus 5.5 | pending |
| 2026-10-09 | #415 | reliability | The revoke call's timeout | `X_REVOKE_TIMEOUT_SECONDS` = 10 (a heuristic) | Every added heuristic is named | Opus 5.5 | pending |
| 2026-10-09 | #464 | user-facing text | "restore" or "recover" | "restore" in the app's sentences (it is the button), "recover" in the terms; admin texts keep "hidden" | candidate: the app's words follow its buttons | Opus 5.5 | pending |
| 2026-10-09 | #464 | user-facing text | The terms sentence "contact us… manually" | Dropped | candidate: user-facing text says what happens | Opus 5.5 | pending |
| 2026-10-09 | #482 | user-facing text | The emails about a deleted account | "Your Loore account is deleted" and "Your Loore account is deleted forever" | candidate: user-facing text says what happens | Opus 5.5 | pending |
| 2026-10-09 | #464 | privacy | Revoking the X sign-in token (OAuth 1.0a, `oauth/invalidate_token`) | A sibling helper with the same 10 s timeout; revoked when the user's own deletion is scheduled (for email accounts at the link confirmation); the X id must match; an expired token is skipped; a 401 is a warning | Deletion for every account | Opus 5.5 | pending |
| 2026-10-09 | #464 | privacy | The X sign-in token after the revoke | Dropped from the session whatever the outcome | Deletion for every account | Opus 5.5 | pending |
| 2026-10-09 | #464 | user-facing text | The X note in the deletion dialog | Two texts (revocable, not revocable); the bookmark text wins when both apply; the different-browser case is covered by the fallback sentence | candidate: user-facing text says what happens | Opus 5.5 | pending |
| 2026-10-09 | #488 | reliability | What a provider refusal does | The same backoff as a cut-off output (#368); no new retry | Problems show | Sonnet 5.5 | pending |
| 2026-10-09 | #488 | reliability | A partial text before a refusal | Dropped | Problems show | Sonnet 5.5 | pending |
| 2026-10-09 | #488 | reliability | A refusal in a digest or a poll draft | Follows the existing empty-result path | Fix the root cause | Sonnet 5.5 | pending |
| 2026-10-09 | #489 | release/ops | The dashboard card fields | Removed entirely, with no `?cards` flag, because no client reads them | Fix the root cause | Opus 5.5 | pending |
| 2026-10-09 | #489 | release/ops | The pagination keys of those fields | Removed with them | Fix the root cause | Opus 5.5 | pending |
| 2026-10-09 | #489 | release/ops | Decrypting the profile on every app load | Left as a follow-up ("Not in this PR") | One PR, one purpose | Opus 5.5 | pending |
| 2026-10-09 | #491 | data safety | Editing a deleted thread's start node (#480) | The same 410, check and lock as `POST /nodes/` | The user's edit wins | Opus 5.5 | pending |
| 2026-10-09 | #491 | UX | The order of the 410 and the AI-usage check | 410 first | Problems show | Opus 5.5 | pending |
| 2026-10-09 | #491 | data safety | Which nodes are checked | Only the start node | candidate: check where the user's words enter | Opus 5.5 | pending |
| 2026-10-09 | #491 | product scope | Text mode, recording save, streaming draft and save-as-node routes | Left as they are (the user's own words; #480's reasoning for text mode) | One PR, one purpose | Opus 5.5 | pending |
| 2026-10-09 | #491 | UX | The notice when the node is gone | A toast like LLM Response, the existing 8 s | Reuse form patterns | Opus 5.5 | pending |
| 2026-10-09 | #492 | reliability | Two todo merges for one user at once | A per-user database advisory lock; waits `TODO_LOCK_WAIT_SECONDS` = 10, then answers 503 `todo_busy` | Every added heuristic is named | Opus 5.5 | pending |
| 2026-10-09 | #492 | data safety | A merge's edits when the list changed in between | Re-applied to the newest list: exact once, ignoring checkboxes once, else fail with "Apply again" | The user's edit wins | Opus 5.5 | pending |
| 2026-10-09 | #492 | data safety | A tick the user made during a merge | The user's tick wins over the merge's checkbox | The user's edit wins | Opus 5.5 | pending |
| 2026-10-09 | #492 | data safety | A whole-list merge when the list changed | Fails | The user's edit wins | Opus 5.5 | pending |
| 2026-10-09 | #492 | UX | The editor on a version conflict (409) | "Save mine anyway" or "Show the newest list"; the text is kept (Copy, Discard) | The user's edit wins | Opus 5.5 | pending |
| 2026-10-09 | #492 | data safety | Reverting a todo version | Takes the lock, with no version check | candidate: a revert is the user's explicit choice | Opus 5.5 | pending |
| 2026-10-09 | #492 | data safety | A save without `base_revision` | Saves as before | Do not break older clients | Opus 5.5 | pending |
| 2026-10-09 | #492 | reliability | The iPhone on a 409 | Retries once | Every added heuristic is named | Opus 5.5 | pending |
| 2026-10-09 | #492 | reliability | The running-check of #472 | Uses `human_owner_id` | Fix the root cause | Opus 5.5 | pending |
| 2026-10-09 | #492 | data safety | Which lines a merge may change (review fix) | Only the same lines as in the list the model saw (look-alike copies counted in order, only if none changed); otherwise nothing is saved and "Apply again" shows | The user's edit wins | Opus 5.5 | pending |
| 2026-10-09 | #492 | UX | Editor texts kept after a conflict (review fix) | They accumulate until discarded, numbered | The user's edit wins | Opus 5.5 | pending |
| 2026-10-09 | #493 | UX | Gleaning from the Voice screen | Pushed over it; Back returns and the Live Activity stays | One behaviour across clients | Opus 5.5 | pending |
| 2026-10-09 | #493 | reliability | A lock-screen glean | Waits for the server's accept, then may suspend; opens at the next launch; a notification on failure | Problems show | Opus 5.5 | pending |
| 2026-10-09 | #493 | product scope | The admin Read card in the app's home | Removed | Admin tools stay admin tools | Opus 5.5 | pending |
| 2026-10-09 | #493 | user-facing text | The Glean card's question in the app | "What's on your mind?" (no newcomer question in the app yet) | One behaviour across clients | Opus 5.5 | pending |
| 2026-10-09 | #493 | UX | The Text Mode button | Shown without the Glean flag | Dark features stay dark | Opus 5.5 | pending |
| 2026-10-09 | #493 | UX | Sizes and timings in the app | 2 s poll, 150 × 44 pt button, 520 / 560 pt cards, title shrinks to 80 % | Every added heuristic is named | Opus 5.5 | pending |
| 2026-10-09 | #493 | reliability | How long a glean may wait (review fix) | Counts only in the foreground; one more status check before giving up | Problems show | Opus 5.5 | pending |
| 2026-10-09 | #493 | UX | The conversation when a gleaning opens (review fix) | Always moves under the gleaning | One behaviour across clients | Opus 5.5 | pending |
| 2026-10-09 | #493 | user-facing text | The lock-screen card after accept (review fix) | "Gleaning · Open Loore to read it when it's ready", no buttons | candidate: user-facing text says what happens | Opus 5.5 | pending |
| 2026-10-09 | #415 | data safety | The recent-context batch table that main added after the purge was written | The purge takes the user's items out of it under the collector's lock, and the collector reads a job's items after its claim | The purge leaves nothing of the user's (#268) | Opus 5.5 | pending |
| 2026-10-09 | #415 | reliability | A purge run that still finds the user's rows, files or batch items at the end | The run fails and is retried, then the job is marked failed; it is never reported done | Problems show | Opus 5.5 | pending |
| 2026-10-09 | #464 | privacy | What "hidden" covers in an account's grace period | Signed out everywhere, sign-in links and API tokens stop working, public pages, sitemap and Commons answer 404, other members see its public entries as deleted, background jobs skip it | Deletion: hidden at once (Peter, 2026-10-02) | Opus 5.5 | pending |
| 2026-10-09 | #464 | privacy | How an email account confirms its deletion | A mailed link, accepted only inside the account's own session from a page with both choices; an account without email confirms with the typed username | The #260 email-change pattern; the issue's "email / re-login confirmation" | Opus 5.5 | pending |
| 2026-10-09 | #464 | UX | A waiting "Delete all my writing" when the account is deleted | Becomes the account deletion, due on the account's date; restoring cancels both; the dialog says so | Show the choices; 30 days of grace for an account | Opus 5.5 | pending |
| 2026-10-09 | #464 | data safety | Restoring after the deletion has started | Refused; the restore page says it can no longer be undone | Problems show | Opus 5.5 | pending |
| 2026-10-09 | #464 | privacy | A deleted account's usernames | Current and former handles reserved for 365 days, then forgotten | #253's impersonation reasoning, bounded because a handle can identify a person; every added heuristic is named | Opus 5.5 | pending |
| 2026-10-09 | #464 | privacy | The account's tombstones in other users' threads after the deletion | Moved to `loore-erased`, the account of the anonymised cost rows | Other users' replies are kept (#415) | Opus 5.5 | pending |
| 2026-10-09 | #464 | privacy | Emails around an account deletion | The confirmation link, "will be deleted" with how to restore, and "has been deleted" to the address the account had | The issue's confirmation email; an erasure request gets an answer | Opus 5.5 | pending |
| 2026-10-09 | #464 | data safety | Admin "Purge data" on an account scheduled for deletion | Refused (409) instead of bringing the account deletion forward | Show the choices | Opus 5.5 | pending |
| 2026-10-09 | #464 | reliability | References to the account left after the identity layer | Counted from the schema; anything left fails the run and the user row stays | Problems show; verify by counting (#268) | Opus 5.5 | pending |
| 2026-10-09 | #464 | privacy | AI replies stored before replies had a human owner, when their owner deletes the account | The request sets their owner by the purge's rule (the nearest entry above them is the user's), so they are hidden with the account; a restore keeps the owner | Fix the cause, not the symptom; no global backfill | Opus 5.5 | pending |
| 2026-10-09 | #464 | data safety | Signing in to an account an admin deleted, before the deletion has started | No restore: an admin's deletion has no grace period; only the user's own request can be restored | Deletion: the grace period is for the user's own request; the admin action runs at once (#415) | Opus 5.5 | pending |

## Backfilled decisions

Decisions made before this log existed, oldest first. None of them has a prediction. Where the builder acted first and Peter then reviewed it, the entry has a Decision line (`stats.py` reports decisions apart from predictions, by type). Where the builder asked without writing down a prediction, or Peter raised the matter himself, the entry has neither line. Quotes are his own words, from earlier voice reviews in Loore, his comments on GitHub, and his messages in earlier Claude Code sessions. Fragments are joined with " · ".

### 2026-04-06 · Describe Loore's privacy as it is

- **Kind:** user-facing text
- **Situation:** asked for a positioning document on Loore, with pitches for promotion, comparing Loore with a knowledge base kept as files on a laptop.
- **Decision:** described Loore as offering "cryptographic privacy" and "stronger-than-local security" compared with keeping files on a laptop.
- **Peter:** "note that Loore is not end-to-end encrypted. There is encryption, but we're still decrypting things on the backend. It's still a trust-based model. But it's the right tradeoff between ease of use and total privacy"
- **Score:** miss.
- **Why it missed:** encryption at rest was counted as stronger privacy, though the server decrypts the content.
- **Source:** Claude Code session 2026-04-06 (Opus 4.6).
- **Applies to:** any text that describes Loore's encryption.
- **Rule:** candidate: Loore describes its privacy as it is: encrypted at rest, decrypted on the server, a model that rests on trust.

### 2026-05-28 · A finished job tells the user when they come back

- **Kind:** UX
- **Situation:** asked to file GitHub issues from Peter's todo list, including "after user profile is generated, notify the user about it".
- **Decision:** filed #131: a toast, banner or in-app indicator when the profile is ready, "whether or not the user is on the profile page".
- **Peter:** "note that it should work async - if the task finishes when the user is not using Loore, they should be notified once they return to Loore next time"
- **Score:** partial. The notice stayed; a user away from Loore was not covered.
- **Source:** issue #131 comment; the issue was filed in Claude Code session 2026-05-28 (Opus 4.7).
- **Applies to:** #131 (PR #201).
- **Rule:** candidate: work a user started finishes without them, and Loore tells them when they come back.

### 2026-06-17 · Four choices for the last slice of PR #196

- **Kind:** data safety
- **Situation:** an overnight run on PR #196, a refactor on staging, reached a slice that needed four choices. Peter was away.
- **Decision:** recommended four choices for the slice: the version-history mapping, keeping the old table for now, its place in the navigation, and the backfill as a standalone script.
- **Peter:** "1 - yes" · "2 - yes, make an issue to drop the table once confident the new table is correct and working" · "3 - yes, curated" · "4 - standalone"
- **Score:** hit.
- **Source:** Claude Code session 2026-06-17 (Opus 4.8).
- **Applies to:** PR #196.

### 2026-06-17 · Decide reversible work without asking

- **Kind:** escalation
- **Situation:** an overnight run on PR #196, a refactor on staging, reached a slice that needed four choices. Peter was away.
- **Decision:** asked Peter instead of deciding.
- **Peter:** "Were any of the changes destructive / irreversible? We're on staging -> no. Better to ship code that is tested and working, but potentially needs to be updated based on my different intent (or worst case dropped completely) for work that is running overnight." · "if I come back in the morning and no implementation and no UI tests were done, that's the same from my point of view as the worst case scenario of having to walk back some work that has been done." · "It's good to flag them explicitly, so I can review (I explicitly gave you this instruction before going afk), but that's for after the fact." · "what is risky is destructive / irreparable changes, not big changes in terms of volume"
- **Score:** miss.
- **Why it missed:** the size of the change was taken for risk; the measure is whether it can be undone.
- **Source:** Claude Code session 2026-06-17 (Opus 4.8).
- **Applies to:** all agent work; PR #196; the reversible items moved to ready on 2026-10-02.
- **Rule:** adopted 2026-10-06: reversible work goes ahead without asking and is flagged afterwards; every added heuristic is named; merging to main stays Peter's.

### 2026-06-25 · Fix the cause, not what is shown

- **Kind:** reliability
- **Situation:** Peter reported that the admin filter for users with costs above zero still listed rows showing $0.00, and asked whether a later commit broke it or sub-cent costs were rounded to $0.00.
- **Decision:** changed the filter to match the rounded display and pushed it to main.
- **Peter:** "Your display fix only covers up a deeper issue. I don't want that."
- **Score:** miss.
- **Why it missed:** the fix covered the rounding that made the rows visible, not the reason they had costs: embedding costs recorded on the model's account instead of the person's.
- **Source:** Claude Code session 2026-06-25 (Opus 4.8). The display fix was reverted.
- **Applies to:** cost attribution to the human owner (commits on main, 2026-06-25).
- **Rule:** adopted 2026-10-06: problems show, they are not hidden. Fail loudly, stop after repeated failures, and fix the cause rather than what is displayed.

### 2026-06-26 · Every added heuristic is flagged

- **Kind:** process
- **Situation:** asked to run the intentions backfill through the Batch API, one batch request per user, for half the price.
- **Decision:** multiplied the batch path's calibrated budget by 0.92 "for safety", so it read about 8 % less of each archive than the direct path. The report left it out; it came up when Peter asked why the batch read less.
- **Peter:** "I don't like that you put extra multiplier and didn't flag it. Pls remove it."
- **Score:** miss.
- **Why it missed:** a batch that never fails was put ahead of full coverage, and the trade was not reported.
- **Source:** Claude Code session 2026-06-26 (Opus 4.8).
- **Applies to:** the intentions batch backfill (PR #202, commit 77222b2).
- **Rule:** adopted 2026-10-06: reversible work goes ahead without asking and is flagged afterwards; every added heuristic is named; merging to main stays Peter's.

### 2026-07-02 · Replies under a public post are public

- **Kind:** privacy
- **Situation:** designing the public side of Loore (#228). Who may reply under a public post, and may those replies be private? A visitor's reply took the visitor's default privacy, often private.
- **Peter:** "other users (non-authors) can't call LLM response right away - they must first respond (with a question / prompt / commentary) and only then they'll be able to call an LLM" · first "private replies are ok imo", then, later the same day: "I'd actually push back - I want users to be able to respond directly under a public node. But we should allow only public responses. Maybe a pop up confirmation dialog upon hitting send that replies under public posts are public (and an option to not show this dialog again). There is a separate mechanism how people can interact with public nodes privately - they can quote it in their private threads (even agentic ones)"
- **Score:** not scored: no prediction or decision by the builder.
- **Note:** his second answer replaced the first within the same discussion.
- **Source:** Claude Code session 2026-07-02 (Fable 5); recorded on issue #228.
- **Applies to:** #228, PR #229.

### 2026-07-02 · The public side ships silent and opt-in

- **Kind:** product scope
- **Situation:** the public side of Loore (#228, PR #229) had no notifications, so an author would not learn that someone replied.
- **Peter:** "It should ship silent in v1, but I'll have to think more about notifications. They go against the no-dopamine-rush intention of Loore" · "I would like the whole public side of Loore we just built to - ship dark - let the users enable it or disable it in their Account"
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** Claude Code session 2026-07-02 (Fable 5); recorded on issue #228.
- **Applies to:** #228, PR #229.
- **Rule:** candidate: no notifications designed to bring people back into Loore; until a design avoids that, features ship without them.

### 2026-07-24 · A failed deploy step fails loudly

- **Kind:** release/ops
- **Situation:** a deploy left the site down: a failed check on the background scheduler stopped the deploy before the web server was restarted. Restart the web server first, and turn the scheduler check into a warning?
- **Peter:** "how would I know there was a problem? That would be a silent fail. I'd like the site to be up without it, but the deploy to fail loudly so that I can fix it in case something similar happens next time."
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** Claude Code session 2026-07-24 (Opus 5).
- **Applies to:** `deploy.sh` (commits b457c57, 6e26217).
- **Rule:** adopted 2026-10-06: problems show, they are not hidden. Fail loudly, stop after repeated failures, and fix the cause rather than what is displayed.

### 2026-08-22 · Turning sharing off takes content down at once

- **Kind:** privacy
- **Situation:** server-rendered public pages (PR #252). Content stayed public after its author switched public sharing off. Should the new pages keep that rule, as the API did, or should the switch hide existing public posts everywhere?
- **Peter:** "we should respect author taking a public post down immediately"
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** Claude Code session 2026-08-22 (Fable 5); PR #252 ("Peter's call").
- **Applies to:** PR #252: the switch takes down every public post and reply of the author at once, on every public surface.
- **Rule:** candidate: withdrawing content takes effect at once, everywhere it is shown.

### 2026-08-27 · A consent question can be dismissed

- **Kind:** UX
- **Situation:** asked how to add an opt-in to X signups for seeding the account from the user's public tweets (PR #267): a hard signal, but still opt-in, and not a checkbox.
- **Decision:** recommended a screen right after X login with two equal buttons, Yes and No, and no skip link, close button or timeout.
- **Peter:** "btw I disagree with having only a Yes and a No options. Escape (not necessarily a button, but having an ability to dismiss it) is important, because a hesitation could turn off the user from actually finishing the signup. Better to not know what the user wants than not having them at all." · "if the user doesn't click anything, that means we don't know"
- **Score:** partial. The question with Yes and No and the recorded answer stayed; the forced choice did not.
- **Source:** Claude Code session 2026-08-27 (Fable 5).
- **Applies to:** PR #267.
- **Rule:** adopted 2026-10-06: ask when the choice matters, in a dialog that can be dismissed; no answer means "we don't know", not "no".

### 2026-09-13 · Recommendations get a quiet hit-or-miss signal

- **Kind:** UX
- **Situation:** Loore quotes saved references in its replies, and nothing recorded whether a quote was read or useful.
- **Peter:** "We need to start keeping track of at least the number of quotes shown to the user, number of read, and preferentially also whether it was a hit, or miss recommendation." · "A heart icon? Sounds good if it's subtle / not colorful and just a contour." · "Thumbs down was the first that came to my mind, but that's too emotive distractive. Maybe a plus and a minus icon in a circle? That could feel neutral"
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** Claude Code session 2026-09-13.
- **Applies to:** the Good quote / Bad quote buttons (commit 805a197, which replaced a "more / fewer like this" framing); #351, #352, #363.

### 2026-09-13 · Recommend fewer, better

- **Kind:** product scope
- **Situation:** scoping a personal feed (the Read) over the Community Archive's daily tweets, with cost estimates.
- **Decision:** compared designs by how many relevant tweets or connections each would yield ("yield per call is flat in window size").
- **Peter:** "it repeatedly sounds like you're maximizing yield" · "the fewer tweets / connections we can recommend, the better. If those that we do, are really relevant." · "the point is to increase our ability to find the needle, not to produce 10x more potential needles."
- **Score:** miss.
- **Why it missed:** the personal feed (find the needle) was mixed with intention matching, as Peter said in the same reply.
- **Source:** Claude Code session 2026-09-13 (Fable 5.1).
- **Applies to:** the Read (#297, #307) and its evaluations.
- **Rule:** adopted 2026-10-06: recommend fewer, better. An empty Read is a good result, an untouched pick is neutral, and Read uses no patterns from extractive feeds.

### 2026-09-17 · A paid fallback is asked in a dialog and billed to the account it serves

- **Kind:** cost
- **Situation:** the admin whitelist needed a way to look a handle up on X, a paid call, when the Community Archive doesn't have it.
- **Decision:** added a checkbox under the whitelist form that allows the paid lookup. The dialog that replaced it billed the lookup to the admin.
- **Peter:** "why is there a checkbox? I wanted a confirmation dialog when the handle is not found in CA. This is a bad UX" · "it should be billed to that newly whitelisted account"
- **Score:** miss, on both parts: the checkbox and who is billed.
- **Source:** Claude Code session 2026-09-17 (Fable 5.1).
- **Applies to:** the admin whitelist (commits 9dc39b0, ac6a25a); later the Build profile dialog in PR #375.
- **Rule:** adopted 2026-10-06: ask when the choice matters, in a dialog that can be dismissed; no answer means "we don't know", not "no". · candidate (awaits Peter's confirmation, asked 2026-10-06 in node 201350; the first wording, "a retry pays only for the missing work", was unclear to him because a retried single call costs the full price; it came from the X bookmark sync, PR #334): every billed call records its cost on the account it serves. A job that stops partway resumes where it stopped, so the next run doesn't pay again for work already done.

### 2026-09-21 · The Read never brings back what was read

- **Kind:** user-facing text
- **Situation:** Peter found the button "Read the day again" and its tooltip unclear, and asked for a label clearer than "Generate more", less addictive and more intentional (PR #322).
- **Decision:** renamed it "Read again with my marks", with a tooltip saying what the second read gets and which picks it leaves out.
- **Peter:** "Read again with my marks should be Read further." · "It should be doing the obvious thing - which is to not ever recommend again a thing marked read (talking about the personal feed here only; agentic should be able to recommend read stuff, but it should know it was already read - which it does)"
- **Score:** miss.
- **Source:** Claude Code session 2026-09-21 (Fable 5.1).
- **Applies to:** PR #322; #352 (PR #358).

### 2026-09-21 · One Chat item takes the whole call off the training key

- **Kind:** privacy
- **Situation:** issue #326 asked: should one entry marked Chat inside a 100k-token export pull the whole thread off the training key? (The strict reading: the promise is on the row.)
- **Peter:** "yes"
- **Score:** not scored: no prediction or decision by the builder.
- **Note:** two days later he also decided that artifacts take the user's default setting instead of a hardcoded Chat, so each row means what it says (recorded on #326).
- **Source:** issue #326 comment; the issue was filed in a Claude Code session of 2026-09-21 (Fable 5.1).
- **Applies to:** #325, #326, PR #327, PR #339.
- **Rule:** adopted 2026-10-06: a call goes to the training key only if everything in its prompt allows it. External content quoted word for word never reaches training.

### 2026-09-24 · A bookmark isn't seen until it's marked read

- **Kind:** product scope
- **Situation:** PR #322 made the Read leave out tweets the user has already seen. Peter's note, quoted in the PR, asked to "Pre-filter seen recommendations (and bookmarks!)".
- **Decision:** counted as seen every tweet saved from X (bookmarks and clipped tweets), read or not, besides the picks marked read.
- **Peter:** "really? I only wanted to filter out external references already marked as read. Bookmarked or clipped tweets are not marked as read"
- **Score:** miss.
- **Why it missed:** a bookmark shows that the user saved a tweet, not that they read it, so the builder could have asked whether "(and bookmarks!)" meant every bookmark or only those marked read.
- **Source:** Claude Code session 2026-09-24, which rewrote issue #352; PR #322 (Fable 5.1).
- **Applies to:** PR #322, #352, PR #358.

### 2026-09-25 · An account set to None runs no AI jobs

- **Kind:** privacy
- **Situation:** a user who switches their account's AI usage to None still had background jobs sending their writing to models: profile updates, summaries, embeddings and others (#346).
- **Peter:** "it's possible a user has Loore account full of data with ai_usage enabled. Then they decide to no longer want to provide it to LLMs and they change their account setting ai_usage to None. In that case no background/automatic jobs should run. No profile updates, integrations, embeddings, poll drafts, recent context summaries, etc." · "All of such jobs should be gated on user's account ai_usage allowed." · "there is one usecase for this, where it's valid that ai_usage none items are included: when user requests an export of their Loore data"
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** Claude Code session 2026-09-25 (review of PR #344 and issue #346).
- **Applies to:** #346, PR #344, PR #361.
- **Rule:** adopted 2026-10-06: AI usage None means no AI. Nothing marked None reaches a model by any route, and no job runs for an account set to None. The user's own data export is the exception.

### 2026-09-25 · A "no" to the prefill blocks every prefill job

- **Kind:** privacy
- **Situation:** should the prefill from public tweets require a "yes" to the prefill question, checked in the backend? The admin panel only displayed the answer.
- **Peter:** "no, not every signup needs to fill this in" · "but prefill_consent == "no" should be checked and prevent the prefill from being done (with admin dashboard popup warning). Intentions prefill should also be gated this way" · on the Build profile button: "it should also not proceed when consent is "no"."
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** Claude Code session 2026-09-25 (Opus 5.5).
- **Applies to:** #346, PR #361.
- **Rule:** adopted 2026-10-06: ask when the choice matters, in a dialog that can be dismissed; no answer means "we don't know", not "no".

### 2026-09-25 · A reply under a Read keeps the user's own setting

- **Kind:** privacy
- **Situation:** a Read's nodes are Chat because they quote other people's tweets. A user's reply under a Read copied that Chat, although the account was set to Train.
- **Peter:** "The node shouldn't copy Read's ai_usage which is chat, because it's quoting public tweets we don't have a license to train on. The resolver knows if these get into the context, the whole request needs to be done as "chat", but my nodes should still have the setting of "train"."
- **Score:** not scored: no prediction or decision by the builder.
- **Note:** his answers on #362, as recorded there: an AI reply whose context held the tweets is stored as Chat ("the stored value should describe what the node was built from"), and existing replies are not backfilled. The first part changed on 2026-09-29; see "AI replies under a Read: Chat for trust, but no lock and no inheritance".
- **Source:** Claude Code session 2026-09-25; issue #362 comment.
- **Applies to:** #362, PR #365.
- **Rule:** adopted 2026-10-06: a call goes to the training key only if everything in its prompt allows it. External content quoted word for word never reaches training.

### 2026-09-29 · The deploy drain: 90 s, not 240 s

- **Kind:** release/ops
- **Situation:** PR #333 lets running Celery tasks finish before a deploy restarts the worker. While the worker drains, new tasks wait.
- **Decision:** a 240 s drain before running tasks are stopped, flagged in the PR as a guess.
- **Peter:** "Decided 2026-09-29 (voice review): 90 s drain, no re-queue in this PR" (as recorded in the PR; commit 37026b2: "drain grace 90 s instead of 240 s (maintainer decision)").
- **Score:** miss.
- **Why it missed:** the drain was judged by the tasks it saves, not by the replies that wait behind it during every deploy.
- **Source:** PR #333 and the voice review of 2026-09-29 (Opus 5.5); found on 2026-10-08 while backfilling approvals by merge.
- **Applies to:** PR #333, #379.

### 2026-09-29 · Recent-context summaries through the Batch API

- **Kind:** release/ops
- **Situation:** PR #333 lets running jobs finish for 90 seconds at a deploy, then cuts them off. Peter had asked for cut-off jobs to be re-queued.
- **Decision:** kept the PR without re-queueing, and recommended handling re-queueing in #379 (a separate worker for background jobs).
- **Peter:** "requeueing not necessary for this PR" · "profile and intentions are already computed via batch processing, so the probability we kill them during the job submission is pretty low and in the case of profiles it recovers." · "But I'm thinking about recent context summary generation - do we use batch processing? If not, file an issue for it"
- **Score:** hit. The Batch API question for recent context was Peter's own addition.
- **Source:** voice review 2026-09-29 (Opus 5.5).
- **Applies to:** PR #333, #379, #380.
- **Rule:** "Quality first, for now" in `LOORE-ESSENCE.md` (background jobs use the Batch API).

### 2026-09-29 · Derived content may be Train; external content never

- **Kind:** privacy
- **Situation:** PR #339 had to decide which of the user's own context rows may go out on the training key (#326).
- **Decision:** kept the references digest off Train in all cases, and counted every artifact listed in the prompt's artifact index by its own setting.
- **Peter:** "derived artifacts should use user's ai_usage setting, including 'train'. By derived is those, that are unlikely to quote other people's content verbatim. Digest, profile, context summary - they should all possibly use train. External references should never use train." · "LLM nodes that quote external references should be safe to set as "train" because the external content is not quoted verbatim, but via the keywords." · "Feeds should theoretically behave the same, but it's safer to always set them as chat" · on the index: "Artifact index is again a derived artifact, unlikely to quote "chat" content verbatim, so it should follow user's preference, including possibly "train""
- **Score:** miss.
- **Source:** voice review 2026-09-29 (Opus 5.5).
- **Applies to:** PR #339, PR #365, #381.
- **Rule:** adopted 2026-10-06: a call goes to the training key only if everything in its prompt allows it. External content quoted word for word never reaches training.

### 2026-09-29 · AI replies under a Read: Chat for trust, but no lock and no inheritance

- **Kind:** privacy
- **Situation:** PR #365 (#362): a user's reply under a Read had copied the Read's Chat.
- **Decision:** the first version stored every AI reply under a Read as Chat and kept it there, even after the Read was deleted, as a default Peter could veto.
- **Peter:** "Read could theoretically also be "train" - the problem is not in the Node containing the recommendations, it's the external references themselves. We're setting Read to "chat" just to be safe." · "In my experience, chatting below the recommendation never produced a quote verbatim" · to dropping the lock: "yes pls" · "I think marking them as chat makes sense for user trust: so they know this won't be trained on because we don't have the license. But subsequent replies, neither user nor AI, shouldn't inherit that chat"
- **Score:** partial. Chat stayed for the reply that presents the picks; the lock and the inheritance by later replies went.
- **Note:** this changes his 2026-09-25 answer on #362. Only the reply that presents the picks stays Chat; every reply after it takes the thread's setting. The rare verbatim copy is to be caught when a training set is built (#381).
- **Source:** voice review 2026-09-29 (Opus 5.5).
- **Applies to:** PR #365, #381.
- **Rule:** adopted 2026-10-06: a call goes to the training key only if everything in its prompt allows it. External content quoted word for word never reaches training.

### 2026-09-29 · The default AI usage of a new reply

- **Kind:** privacy
- **Situation:** PR #365 had to set the default AI usage of a user's new reply in a thread.
- **Decision:** if any node passed on the way up the thread was set to None, the new reply defaulted to None, as a default Peter could veto.
- **Peter:** "The helper selector for user nodes should take previous node, if there is none, the user account setting, if there is none, take the default. Why should any None in the context make new user reply also None? I think it shouldn't"
- **Score:** miss.
- **Source:** voice review 2026-09-29 (Opus 5.5).
- **Applies to:** PR #365.

### 2026-09-29 · Switching the account to Train changes no existing artifact

- **Kind:** privacy
- **Situation:** in PR #339, a user switches their account from Chat to Train. What happens to artifacts written before?
- **Decision:** built the PR so that the switch changes no artifact already written, and recommended leaving it so, over asking at the switch or a setting per artifact.
- **Peter:** "changing account setting to train should do nothing for artifacts. only when they're being edited (or new versions are created, such as profile updates, integrations, recent-context summaries), that should change the artifact's setting to the current account setting"
- **Score:** hit.
- **Source:** voice review 2026-09-29 (Opus 5.5).
- **Applies to:** PR #339.
- **Rule:** adopted 2026-10-06: a call goes to the training key only if everything in its prompt allows it. External content quoted word for word never reaches training.

### 2026-10-01 · The server finishes an upload's reply; warnings stay private

- **Kind:** UX
- **Situation:** an audio file uploaded in Text mode now gets a reply (PR #347).
- **Decision:** built the PR so that the server starts the reply, which arrives even if the tab is closed, and only the owner gets the reply link and warnings such as "you hit your spending limit"; recommended keeping both.
- **Peter:** "yes" (the server starts the reply) · "yes" (owner only)
- **Score:** hit.
- **Source:** voice review 2026-10-01 (Opus 5.5).
- **Applies to:** PR #347.
- **Rule:** candidate: work a user started finishes without them, and Loore tells them when they come back.

### 2026-10-01 · Auto-generate can't know what the user wants

- **Kind:** UX
- **Situation:** in a Read thread with auto-generate on, an uploaded reply showed only the Read further button. LLM Response was hidden, though nothing was generated. Should a follow-up make such an upload get an answer?
- **Peter:** "with autogenerate off you have two options: either chat about the recommendations, or Read further. Autogenerate has no way to know which one the user wants, so it just doesn't autogenerate" · "what's not ok is that with autogenerate on the LLM response button is hidden, so the user sees only Read button. That is confusing, we should either figure out what the autogenerate should do in this case, and then show neither of the buttons, or accept the autogenerate doesn't know, but then show both."
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** voice review 2026-10-01 (Opus 5.5).
- **Applies to:** #387 (show both, decided 2026-10-02).
- **Rule:** "Show the choices" in `LOORE-ESSENCE.md`.

### 2026-10-01 · No button deletes an unfinished recording; Text mode will ask

- **Kind:** data safety
- **Situation:** PR #338 stops a second tab from ending a live recording. Text mode still restored an abandoned recording into the box without asking.
- **Decision:** built the fix so that Discard and Send in the text box never delete an unfinished recording.
- **Peter:** "yes" (Discard and Send never delete an unfinished recording) · on asking first: "I think this is already the case for voice mode, isn't it? We should do it for text mode as well. But in a followup issue"
- **Score:** hit.
- **Source:** voice review 2026-10-01 (Opus 5.5).
- **Applies to:** PR #338, #388, #389.
- **Rule:** "Show the choices" in `LOORE-ESSENCE.md`.

### 2026-10-01 · A failed sync must not make the next one expensive

- **Kind:** cost
- **Situation:** X charges per bookmark read. A sync cut off partway left the older bookmarks missing for good (PR #334).
- **Decision:** after a sync stopped partway, the next sync read all bookmarks again, up to 800 (at most $4, expected once).
- **Peter:** "for accounts that never had the sync the $4 is ok" · "but it seems to me this sync all the way up to the 800 bookmarks could be triggered on a failed later sync. That's a big problem. $4 a night instead of a couple cents" · "what? The sync button should only sync new bookmarks, not all the 800"
- **Score:** miss.
- **Why it missed:** the full reread was expected once; a failure that repeats, or each press of the sync button, would repeat it.
- **Source:** voice review 2026-10-01 (Opus 5.5).
- **Applies to:** PR #334 (a resumed sync now reads only back to the last finished sync).
- **Rule:** candidate (awaits Peter's confirmation, asked 2026-10-06 in node 201350; the first wording, "a retry pays only for the missing work", was unclear to him because a retried single call costs the full price; it came from the X bookmark sync, PR #334): every billed call records its cost on the account it serves. A job that stops partway resumes where it stopped, so the next run doesn't pay again for work already done.

### 2026-10-01 · Stop after two failures, loudly

- **Kind:** reliability
- **Situation:** background jobs whose output is cut off now save nothing (PR #375, #368). Without a limit, a refused job would repeat the same billed call on every scheduler run: recent context every ten minutes, the profile every hour.
- **Decision:** a retry backoff with a growing wait: one hour, then four hours, then one attempt a week, with an error to Sentry from the third failure and no final stop; recommended as built.
- **Peter:** "the error is logged after a week? That's too long. If a profile generation got cutoff with 32k tokens output limit, that's way too much! Two errors should be enough to stop completely and log a very loud error on Sentry" · on an import that triggers a rebuild: "Then I'd skip the wait and go right ahead" · on the Build profile button for a stopped user: "it should pop up a dialog informing me the profile generation failed twice, and whether I really want to start another build." · "Cut-off intentions count as success, empty artifact no. But admin dashboard shows it as success. This should be fixed."
- **Score:** miss.
- **Why it missed:** the schedule avoided stopping for good, because nothing would restart a stopped job.
- **Source:** voice review in local Loore, 2026-10-01 (Opus 5.5): the brief is node 201165, his replies nodes 201213 and 201215.
- **Applies to:** PR #375, #368, and PR #409, which applies the same stop to recent context through the Batch API.
- **Rule:** adopted 2026-10-06: problems show, they are not hidden. Fail loudly, stop after repeated failures, and fix the cause rather than what is displayed.

### 2026-10-01 · Every billed call records its cost

- **Kind:** cost
- **Situation:** in the review of PR #375, the voice todo merge and the references digest skipped an empty output without recording what the call cost.
- **Decision:** left this out of the PR as a listed follow-up, among the choices that ship unless Peter objects.
- **Peter:** "they should log the cost. Pls fix in this PR"
- **Score:** miss.
- **Source:** voice review 2026-10-01 (Opus 5.5).
- **Applies to:** PR #375.
- **Rule:** candidate (awaits Peter's confirmation, asked 2026-10-06 in node 201350; the first wording, "a retry pays only for the missing work", was unclear to him because a retried single call costs the full price; it came from the X bookmark sync, PR #334): every billed call records its cost on the account it serves. A job that stops partway resumes where it stopped, so the next run doesn't pay again for work already done.

### 2026-10-01 · A cut-off todo merge is saved as the user's list

- **Kind:** reliability
- **Situation:** PR #375 (merged through #393) makes background jobs refuse a model output cut off at the output limit. Only the profile refuses any cut-off output; every other job refuses only an output cut off before any text. The voice todo merge is one of those jobs, so a merge cut off partway through replaces the user's whole todo list, and the next merge builds on it.
- **Decision:** kept the rule that jobs other than the profile refuse only an empty cut-off output, the todo merge included (PR #375, "Earlier decisions that still hold").
- **Score:** miss.
- **Why it missed:** the rule was written for outputs cut off before any text (#366, #368), and the todo merge was not checked against it, although a partial list harms the user the same way a partial profile does, which the same PR refuses.
- **Source:** issue #432 (2026-10-06), which the builder filed during the voice review after Peter's comparison run showed one merge looping until the 32,000-token cap and losing 105 items; issue #432 expects "A truncated result fails the merge." Peter's own words on it are not on record. PR #375 (Opus 5.5).
- **Applies to:** PR #375, #432, PR #433.

### 2026-10-01 · The line under Reflect goes

- **Kind:** user-facing text
- **Situation:** PR #337 changes `/welcome`: the button on the first-entry card becomes "Reflect" and leads to the homepage. The card asks its own question, while the homepage and the Voice and Text modes ask "What's on your mind?".
- **Decision:** added a helper line under the Reflect button: "Take the question with you, or start with whatever is on your mind. Type or record a voice note, whichever feels natural."
- **Peter:** "I'd drop the "Take the question with you, or start with whatever is on your mind. Type or record a voice note, whichever feels natural." - there is a better text below"
- **Score:** miss.
- **Why it missed:** the line was written to carry the card's question over to screens that ask "What's on your mind?", but the page already had better text below it.
- **Source:** Claude Code session 2026-10-01, during the voice review, looking at `/welcome` on the PR's build; PR #337 (Opus 5.5). Found on 2026-10-08 while backfilling approvals by merge.
- **Applies to:** PR #337, #391.

### 2026-10-01 · What the Read is for

- **Kind:** product scope
- **Situation:** Peter's notes on the personal feed (the Read), written over the previous two weeks.
- **Peter:** "the intention for the feed is to stay synced with the society without being addicted to the feed and with as little effort as possible" · "imagine coming to a good knowledgeable friend of yours and asking them to give you an update in their area of expertise." · "we also want to incentivize intentionality (and instrumentally also reflection) so we should gate on reflection"
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** Peter's notes, shared in the voice review of 2026-10-01.
- **Applies to:** the Read.
- **Rule:** adopted 2026-10-06: reflection comes first. A Read answers a reflection, also in a newcomer's first session, and reading more means reflecting again. A Read does not start by itself after each reflection: the user opens it from its card (Reflect, then Read), and reflecting alone is fine.

### 2026-10-01 · The Read stays on Chat

- **Kind:** privacy
- **Situation:** the Read quotes other people's tweets. Can a user set it to Train?
- **Peter:** "it should always use chat, user shouldn't be able to use train (we don't have license to train on public tweets)"
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** Peter's notes, shared in the voice review of 2026-10-01 (marked done, by PR #307).
- **Applies to:** PR #307, PR #365.
- **Rule:** adopted 2026-10-06: a call goes to the training key only if everything in its prompt allows it. External content quoted word for word never reaches training.

### 2026-10-01 · The note says why a pick matters to this user

- **Kind:** user-facing text
- **Situation:** the Read presented picks with the reason added in italics after each tweet.
- **Peter:** "make the feed as Loore QT the threads. For reading the threads I'll probably open twitter, but the QT text should motivate me to do so (make it relevant to my situation). Not to add it in italics as a post-script. Once I read the QTed tweet, it should either be obvious why it's relevant, or not and in that case it's either a bad recommendation, or it's even more important to connect it to my actual situation"
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** Peter's notes, shared in the voice review of 2026-10-01 (marked done, by PR #307).
- **Applies to:** PR #307.

### 2026-10-01 · A Read the user waits for runs live

- **Kind:** UX
- **Situation:** every Read went through the Batch API. Most finished within 15 minutes; one in five took from 40 minutes to 7 hours.
- **Peter:** "batch processing is unusable for product - too unpredictable response time, too long in expectation"
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** Peter's notes, shared in the voice review of 2026-10-01.
- **Applies to:** the Read (user-facing Reads move from the Batch API to live calls).
- **Rule:** "Quality first, for now" in `LOORE-ESSENCE.md` (the Batch API is for jobs nobody waits for).

### 2026-10-01 · A new user must not wait for their profile

- **Kind:** UX
- **Situation:** prefilling a new user's profile from their public tweets takes a long time after signup.
- **Peter:** "prefill of new signups needs to be streamlined." · "This is necessary, otherwise prefill takes really long and that would be too big of a friction for activation"
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** voice review 2026-10-01 (Paid Beta planning).
- **Applies to:** Beta onboarding; #225.

### 2026-10-01 · A newcomer reflects first

- **Kind:** product scope
- **Situation:** a newcomer has a profile but hasn't reflected yet. Do they get a first Read right away, or does the first session lead into a reflection?
- **Peter:** "yes, we need to show them how easy the reflection is while they are still curious about Loore. The next day they will possibly have already forgotten about Loore"
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** voice review 2026-10-01 (Paid Beta planning; Opus 5.5).
- **Applies to:** Beta onboarding; #391.
- **Rule:** adopted 2026-10-06: reflection comes first. A Read answers a reflection, also in a newcomer's first session, and reading more means reflecting again. A Read does not start by itself after each reflection: the user opens it from its card (Reflect, then Read), and reflecting alone is fine.

### 2026-10-01 · An untouched pick is neutral

- **Kind:** testing
- **Situation:** comparing Read models on Peter's past Reads.
- **Decision:** counted a pick he didn't mark as "not good".
- **Peter:** "no, it should count as neither good nor bad. A recommendation I'm not offended by, but also not excited about. If there is enough good ones, these neither good nor bad ones don't matter that much."
- **Score:** miss.
- **Source:** voice review 2026-10-01 (Paid Beta planning; Opus 5.5).
- **Applies to:** Read model evaluations; #352.
- **Rule:** adopted 2026-10-06: recommend fewer, better. An empty Read is a good result, an untouched pick is neutral, and Read uses no patterns from extractive feeds.

### 2026-10-01 · Reading more takes reflecting again

- **Kind:** product scope
- **Situation:** the original design gave extra Reads from a cheaper, blander model, so that reading on would be less rewarding.
- **Decision:** proposed one Read per reflection instead: reflecting again gives a new Read.
- **Peter:** earlier, in his notes: "if the user wants to scroll further, we can offer them Luna, which is dirt cheap, produces some good recommendations, but is more bland on avg -> disincentivizes further scrolling" · now: "makes sense to me"
- **Score:** hit.
- **Note:** this replaces the earlier design.
- **Source:** Peter's notes and the voice review of 2026-10-01 (Paid Beta planning; Opus 5.5).
- **Applies to:** the Read.
- **Rule:** adopted 2026-10-06: reflection comes first. A Read answers a reflection, also in a newcomer's first session, and reading more means reflecting again. A Read does not start by itself after each reflection: the user opens it from its card (Reflect, then Read), and reflecting alone is fine.

### 2026-10-01 · Admins see no more user content than anyone else

- **Kind:** privacy
- **Situation:** some status routes let an admin read any user's replies and transcripts. Should admins read user content through these routes at all?
- **Peter:** "Admins shouldn't be able to read any node's text directly. Via code on prod yes, but we never do it"
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** Claude Code session 2026-10-01 (Opus 5.5).
- **Applies to:** PR #395 (merged).
- **Rule:** "Nobody decrypts users' content" in `LOORE-ESSENCE.md`.

### 2026-10-01 · No AI in Voice mode when AI usage is None

- **Kind:** privacy
- **Situation:** Voice mode sent every recording to the model and spoke the reply, even in threads set to None. PR #395 made content marked None refuse new text-to-speech.
- **Decision:** kept text-to-speech for model replies, so Voice mode still spoke its replies in None threads.
- **Peter:** "Voice threads shouldn't produce a textual llm response when ai_usage is none, so why do we allow tts?"
- **Score:** miss.
- **Source:** Claude Code session 2026-10-01 (Opus 5.5).
- **Applies to:** PR #396 (merged): no reply anywhere under content marked None, and Voice mode explains instead of recording.
- **Rule:** adopted 2026-10-06: AI usage None means no AI. Nothing marked None reaches a model by any route, and no job runs for an account set to None. The user's own data export is the exception.

### 2026-10-01 · Agents open pull requests; Peter merges

- **Kind:** process
- **Situation:** which agents may open pull requests, and who merges? Merging to main deploys to production.
- **Peter:** "Every agent should be able to open PRs" · "Yeah, merges should be done by me"
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** Claude Code session 2026-10-01.
- **Applies to:** all pull requests.
- **Rule:** adopted 2026-10-06: reversible work goes ahead without asking and is flagged afterwards; every added heuristic is named; merging to main stays Peter's.

### 2026-10-02 · The Read doesn't look like an extractive feed

- **Kind:** UX
- **Situation:** planning the Paid Beta: the Read's name, and the tweet card under each pick, which showed no avatar or display name.
- **Decision:** recommended a display name, handle and avatar on each card, because "on Twitter that's half of how people judge a tweet".
- **Peter:** "My default is Feed. Or Personal Feed. But ofc this associates with the doomscrolling, or at least the mainstream extractive addictive feeds. Twitter has For You. That's a good direction, but ofc twitter is exactly the doomscrolling and extraction, so they already spoiled the name." · "I'm against avatar. What you name is a true pattern for people scrolling twitter. But it's a pattern optimized for extractive feeds. We wanna break it, we don't want Loore's users to just scroll and skim the recommendations. We want them to be intentional and read through them. Slow. It's usually 1 - 6 of tweets. Not the dozens one needs to scroll through on twitter"
- **Score:** partial. The display name stayed; the avatar did not.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** the Read's name and tweet card.
- **Rule:** adopted 2026-10-06: recommend fewer, better. An empty Read is a good result, an untouched pick is neutral, and Read uses no patterns from extractive feeds.

### 2026-10-02 · The Beta cohort comes with profiles

- **Kind:** product scope
- **Situation:** planning the Paid Beta: when to show the Read to the first testers from outside.
- **Decision:** made it a condition that the Read works for someone with no profile and no intentions.
- **Peter:** "no, this is an assumption you're repeatedly making that I disagree with" · "for Beta we'll be targeting people already aligned with Loore's vision and those already present in the CA" · "for this first public cohort we'll be targeting those that will have a profile and intentions ready from their public tweets" · "later on we can extend this to users with no public data or with just tweets and no CA. But not for Beta"
- **Score:** miss.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** Beta features and evaluations (first Read, onboarding).

### 2026-10-02 · An empty Read is a good result

- **Kind:** testing
- **Situation:** Reads that found nothing worth reading were not counted anywhere.
- **Peter:** "we should at least count them separately. No recommendation is a good recommendation (at least for me, who already has trust built that if there are good tweets, Loore finds them. So if Loore finds nothing, I'm glad I saved my time.)"
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** voice review 2026-10-02 (Paid Beta planning).
- **Applies to:** Read tracking.
- **Rule:** adopted 2026-10-06: recommend fewer, better. An empty Read is a good result, an untouched pick is neutral, and Read uses no patterns from extractive feeds.

### 2026-10-02 · The iPhone app goes to alpha testers first

- **Kind:** release/ops
- **Situation:** planning the Paid Beta. The native iPhone app worked on Peter's phone. The App Store needs Apple billing, Sign in with Apple and a review.
- **Decision:** proposed that the Paid Beta open on the web, with the iPhone app in TestFlight for free testers (Peter, the alpha users, the first testers) and the App Store later.
- **Peter:** "I would like to do this ASAP because we are currently in alpha, everyone is free. So that's consistent with the test flight conditions." · "before finalizing the beta, we would already know whether the iPhone app is really such a big improvement that it's worth it to go through the additional steps to put it into App Store" · "Maybe the feedback will show that the app needs much more work and we will postpone it."
- **Score:** hit.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** PR #384.

### 2026-10-02 · Prod gets its own API key

- **Kind:** provider
- **Situation:** Loore's spend monitor counts only prod's costs, but the provider's spend limit counts everything on the account, local and lab experiments included. So the warning could come after the cut-off. Separate keys for prod, or a monitor set below the limit?
- **Peter:** "however, on top of this, yes we should be detecting the specific errors" · on a separate key and workspace for prod: "yes, we need this. Pls walk me through it when it makes sense"
- **Score:** not scored: no prediction or decision by the builder.
- **Source:** voice review 2026-10-02 (Paid Beta planning; Opus 5.5).
- **Applies to:** #360, #369.
- **Rule:** "A user's model is theirs" in `LOORE-ESSENCE.md` (account failures are prevented).

## Accepted by merge

Builder decisions listed in PRs that merged without Peter correcting them, backfilled on 2026-10-08. Each row is a decision scored as a hit. Sources: the sections of the PR body that list the builder's own choices ("Decisions to review", "Decided under Peter's rules", "Heuristics introduced", "Choices to check", "Things I decided that you may want to change", introduced constants). A decision counts as corrected if this log, the PR's comments, its later commits or a later issue show Peter changing it; the corrections found that way are entries above ("The deploy drain: 90 s, not 240 s", "The line under Reflect goes", "A bookmark isn't seen until it's marked read", "Recording keeps the headset's mic", "A cut-off todo merge is saved as the user's list"). Left out: decisions already in this log as entries, Peter's own decisions written into a PR body, implementation details he couldn't have decided differently, known limitations, reviewer findings, and decisions he turned into a follow-up issue. The cutoff, PRs merged since 2026-09-01, is a heuristic: older PRs are not backfilled. Date is the merge date; Model is the PR's Co-Authored-By line. Choices explicitly accepted in a voice review before the merge are counted here too. Rows dated 2026-10-08 and later are added when their PR merges, not backfilled; a PR shipped in a merge train names the train in Result.

| Date | PR | Kind | Decision | Model | Result |
|---|---|---|---|---|---|
| 2026-09-02 | #279 | release/ops | The thread walk queries at most 900 ids per statement (`_WALK_CHUNK`) | Fable 5.1 | accepted by merge |
| 2026-09-02 | #281 | release/ops | Encryption keys are prefetched in windows of about 1,000 nodes (`DEK_PREFETCH_WINDOW`) | Fable 5.1 | accepted by merge |
| 2026-09-04 | #285 | reliability | Profile chunks keep 5 % of the input cap in reserve (`CAP_MARGIN`) | Fable 5.1 | accepted by merge |
| 2026-09-04 | #285 | reliability | The final profile chunk asks for 10,000 units more than remain (`FINAL_CHUNK_OVERASK_UNITS`) | Fable 5.1 | accepted by merge |
| 2026-09-04 | #285 | reliability | Tokens-per-unit priors at the top of the measured ranges, with sanity bounds of 0.25 and 8.0 | Fable 5.1 | accepted by merge |
| 2026-09-04 | #285 | reliability | The profile repair script branches only where chunks plan within 25 % of the target (`CHUNK_BAND`) | Fable 5.1 | accepted by merge |
| 2026-09-12 | #288 | privacy | At most 10 active clipper tokens per user | Fable 5.1 | accepted by merge |
| 2026-09-12 | #288 | release/ops | A clipper token's last use is written at most every 5 min | Fable 5.1 | accepted by merge |
| 2026-09-12 | #288 | UX | The clipper's badge clears after 1.8 s | Fable 5.1 | accepted by merge |
| 2026-09-12 | #291 | data safety | A re-clip replaces a reference's text only when the new capture is longer | Fable 5.1 | accepted by merge |
| 2026-09-16 | #309 | cost | Nightly bookmark pages start at 10 posts and double to 100 while every post is new | Fable 5.1 | accepted by merge |
| 2026-09-16 | #309 | provider | The references digest and poll drafts keep following the chosen model, priced for either provider, instead of being pinned to Anthropic | Fable 5.1 | accepted by merge |
| 2026-09-18 | #305 | privacy | A waitlisted signup's email is bound only after a confirmation click | Fable 5.1 | accepted by merge |
| 2026-09-18 | #305 | privacy | The email confirmation link works only where the account is signed in | Fable 5.1 | accepted by merge |
| 2026-09-18 | #305 | privacy | Email-change links last 24 h (sign-in links stay at 15 min) | Fable 5.1 | accepted by merge |
| 2026-09-18 | #305 | reliability | Every mail the app sends times out after 10 s | Fable 5.1 | accepted by merge |
| 2026-09-18 | #305 | privacy | The audit trail of an email change is a log line without the addresses, not a table | Fable 5.1 | accepted by merge |
| 2026-09-18 | #307 | UX | A pick's relevance % and recommend flag are no longer shown | Fable 5.1 | accepted by merge |
| 2026-09-18 | #307 | cost | A live rerun of a full day costs full price, on the admin's click only | Fable 5.1 | accepted by merge |
| 2026-09-18 | #307 | UX | Old Read replies keep the old list rendering; no backfill | Fable 5.1 | accepted by merge |
| 2026-09-18 | #307 | reliability | Errors while a batch is submitted are retried up to the 800-poll cap (about 27 h) | Fable 5.1 | accepted by merge |
| 2026-09-18 | #318 | user-facing text | The import summary shows a "No text" count with a sentence saying why those posts were skipped | Opus 5 | accepted by merge |
| 2026-09-21 | #324 | release/ops | The public-source check makes at most 300 lookups a night, 0.5 s apart | Fable 5.1 | accepted by merge |
| 2026-09-21 | #324 | release/ops | The public-source check runs at 05:00 UTC, at least 20 h apart | Fable 5.1 | accepted by merge |
| 2026-09-21 | #324 | privacy | A 403 or 404 answer marks a tweet as not public for good (kept after review, per Peter) | Fable 5.1 | accepted by merge |
| 2026-09-23 | #335 | privacy | The Connect X intent has no time limit; it lasts while the account that started it stays signed in | Opus 5.5 | accepted by merge |
| 2026-09-23 | #335 | privacy | An X callback from a browser session that didn't start the sign-in is refused, at the cost of some legitimate sign-ins | Opus 5.5 | accepted by merge |
| 2026-09-23 | #335 | privacy | Public pages link the X account only for X sign-ups; no setting | Opus 5.5 | accepted by merge |
| 2026-09-23 | #335 | user-facing text | Messages for an X account that is already taken, pointing to the support address | Opus 5.5 | accepted by merge |
| 2026-09-23 | #335 | user-facing text | The login-page line on Sign in with X and Connect X | Opus 5.5 | accepted by merge |
| 2026-09-23 | #335 | product scope | Disconnect X built in the same PR | Opus 5.5 | accepted by merge |
| 2026-09-23 | #335 | product scope | Releasing an X id from an empty duplicate account left out of the PR | Opus 5.5 | accepted by merge |
| 2026-09-24 | #336 | reliability | The fix changes the shared markdown component (18 call sites), not only the checkbox | Opus 5.5 | accepted by merge |
| 2026-09-24 | #345 | data safety | A capped chunked upload is refused at its start, so no empty entry is left | Opus 5.5 | accepted by merge |
| 2026-09-24 | #345 | UX | A capped Voice recording is saved as an entry with no reply and a toast | Opus 5.5 | accepted by merge |
| 2026-09-24 | #345 | user-facing text | The spend-cap toast's wording, with the reset date taken in UTC | Opus 5.5 | accepted by merge |
| 2026-09-24 | #345 | UX | The spend-cap toast stacks above the limit banner | Opus 5.5 | accepted by merge |
| 2026-09-24 | #345 | cost | No prompt-cache prewarm for a capped user | Opus 5.5 | accepted by merge |
| 2026-09-24 | #353 | UX | The ⊕/⊖ tap targets are 34 px wide, not 40 | Opus 5.5 | accepted by merge |
| 2026-09-24 | #353 | UX | Footer rows grow to 40 px rather than overlap on phones | Opus 5.5 | accepted by merge |
| 2026-09-24 | #354 | cost | The system-prompt hash is recorded on every conversation call, Anthropic included | Opus 5.5 | accepted by merge |
| 2026-09-24 | #354 | cost | A cache baseline from a different model is still sent; the miss reason is only recorded | Opus 5.5 | accepted by merge |
| 2026-09-24 | #358 | data safety | Migration: an archive row counts as made by a pick when fetched within 60 s of its first pick | Opus 5.5 | accepted by merge |
| 2026-09-24 | #358 | testing | A quote's decision time is its placeholder's creation time | Opus 5.5 | accepted by merge |
| 2026-09-24 | #358 | testing | Verdicts given before the change are recorded as given outside any reply | Opus 5.5 | accepted by merge |
| 2026-09-24 | #358 | data safety | Deleting a saved reference also deletes its quote records | Opus 5.5 | accepted by merge |
| 2026-09-24 | #358 | testing | Every open of a pick is logged, reopenings included | Opus 5.5 | accepted by merge |
| 2026-09-25 | #344 | privacy | A profile row marked None keeps its heading with a placeholder, instead of being dropped | Opus 5.5 | accepted by merge |
| 2026-09-25 | #344 | privacy | The unbudgeted full export fixed too, beyond what the issue named | Opus 5.5 | accepted by merge |
| 2026-09-25 | #344 | privacy | The prompt render cache is not cleared at deploy; a cached row marked None can stay up to 24 h | Opus 5.5 | accepted by merge |
| 2026-09-25 | #361 | privacy | An account set to None keeps its existing embeddings; only new embedding stops | Opus 5.5 | accepted by merge |
| 2026-09-25 | #361 | privacy | The Read is checked only when the user asks for one, not gated as a background job | Opus 5.5 | accepted by merge |
| 2026-09-28 | #370 | UX | Share blocks are never spoken, in batch speech too | Opus 5.5 | accepted by merge |
| 2026-09-28 | #370 | UX | Proposal blocks are left out of streamed speech per heading; batch speech keeps its rule | Opus 5.5 | accepted by merge |
| 2026-09-28 | #370 | UX | Blank lines are not extra cut points for speech | Opus 5.5 | accepted by merge |
| 2026-09-28 | #370 | release/ops | Streamed speech runs in a thread inside the reply task, not as a separate task | Opus 5.5 | accepted by merge |
| 2026-09-29 | #365 | privacy | A "Read further" reply and an LLM reply asked for under a Read count as recommendation replies and stay Chat | Opus 5.5 | accepted by merge |
| 2026-09-29 | #365 | privacy | The nearest node that isn't the Read's decides a reply's default AI usage, whoever wrote it | Opus 5.5 | accepted by merge |
| 2026-09-29 | #365 | privacy | The completion task's Chat backstop stays, narrowed to read turns | Opus 5.5 | accepted by merge |
| 2026-09-29 | #365 | privacy | A read reply with no picks can still be raised to Train | Opus 5.5 | accepted by merge |
| 2026-09-29 | #372 | testing | Voice latency measured on local Docker, not on staging | Opus 5.5 | accepted by merge |
| 2026-09-29 | #372 | release/ops | Timing data kept in Redis behind an endpoint, not only in logs | Opus 5.5 | accepted by merge |
| 2026-09-29 | #372 | UX | The early first chunk applies whenever the speech queue runs empty | Opus 5.5 | accepted by merge |
| 2026-09-29 | #372 | UX | The first speech chunk keeps its 80-character minimum | Opus 5.5 | accepted by merge |
| 2026-09-29 | #372 | UX | A finishing recording is polled every 0.25 s (was 2 s) | Opus 5.5 | accepted by merge |
| 2026-09-29 | #372 | UX | Speech is refilled 2.5 s ahead of the playing chunk | Opus 5.5 | accepted by merge |
| 2026-09-29 | #372 | reliability | Shorter first chunks: a provider failure after the first cut ends as a cut-off reply | Opus 5.5 | accepted by merge |
| 2026-09-29 | #376 | reliability | A reply's audio stream gets 3 s of catch-up before it counts as stalled (`TTS_SSE_CATCH_UP_MS`) | Opus 5.5 | accepted by merge |
| 2026-09-29 | #378 | data safety | A new Completed section goes above New Tasks, the order the prompts ask for | Opus 5.5 | accepted by merge |
| 2026-09-29 | #378 | data safety | A todo section emptied of items keeps its heading | Opus 5.5 | accepted by merge |
| 2026-09-30 | #383 | reliability | Text-to-speech audio from a call that ran 290 s or more is treated as cut and not stored | Opus 5.5 | accepted by merge |
| 2026-09-30 | #383 | reliability | A text-to-speech call slower than realtime after 30 s is cancelled | Opus 5.5 | accepted by merge |
| 2026-09-30 | #383 | reliability | A cancelled text-to-speech call is resent up to twice | Opus 5.5 | accepted by merge |
| 2026-09-30 | #383 | reliability | The last attempt keeps the speed check, so it fails sooner instead of waiting up to about 5 min | Opus 5.5 | accepted by merge |
| 2026-10-01 | #337 (via #393) | UX | Reflect stays the main card, with import as a smaller line above it | Opus 5.5 | accepted by merge |
| 2026-10-01 | #337 (via #393) | user-facing text | The import line: "Already have journals, notes, AI chats or tweets?" / "Import them →" | Opus 5.5 | accepted by merge |
| 2026-10-01 | #338 (via #393) | reliability | A recording counts as live for 45 s after its last stamp | Opus 5.5 | accepted by merge |
| 2026-10-01 | #338 (via #393) | data safety | A laptop asleep mid-recording keeps its session for up to about 15 min before another device may recover it | Opus 5.5 | accepted by merge |
| 2026-10-01 | #338 (via #393) | data safety | The smaller defaults: a server-side liveness stamp, an "ended elsewhere" message that keeps the audio, a second tab's typing saved as its own draft | Opus 5.5 | accepted by merge |
| 2026-10-01 | #347 (via #393) | UX | An agentic audio upload gets no file-name title | Opus 5.5 | accepted by merge |
| 2026-10-01 | #347 (via #393) | reliability | The ~3 s deploy window in which an upload's reply can fail is accepted | Opus 5.5 | accepted by merge |
| 2026-10-01 | #347 (via #393) | product scope | Uploads in a Read thread's reply box still get no reply in this PR | Opus 5.5 | accepted by merge |
| 2026-10-01 | #347 (via #393) | UX | A spend cap reached during transcription shows a toast, not the banner | Opus 5.5 | accepted by merge |
| 2026-10-01 | #364 (via #393) | UX | The ⊕/⊖ verdict is off unless a surface turns it on | Opus 5.5 | accepted by merge |
| 2026-10-01 | #364 (via #393) | UX | The "You rated this" line is hidden with the verdict on the user's own nodes | Opus 5.5 | accepted by merge |
| 2026-10-01 | #364 (via #393) | data safety | No server-side check on which node a verdict is given in | Opus 5.5 | accepted by merge |
| 2026-10-01 | #375 (via #393) | reliability | Jobs other than the profile refuse only an empty cut-off output, the todo merge included | Opus 5.5 | corrected → entry A cut-off todo merge is saved as the user's list |
| 2026-10-01 | #375 (via #393) | reliability | A new profile version restarts a stopped recent-context summary | Opus 5.5 | accepted by merge |
| 2026-10-01 | #395 | privacy | Profile versions marked None get no new speech; their speaker icon isn't greyed out yet | Opus 5.5 | accepted by merge |
| 2026-10-02 | #396 | privacy | Link nodes keep the column default None, since no client creates them | Opus 5.5 | accepted by merge |
| 2026-10-02 | #396 | UX | Voice users set to None see the explanation instead of a reply, with no migration of old threads | Opus 5.5 | accepted by merge |
| 2026-10-05 | #384 | product scope | The iPhone app: SwiftUI, iOS 17 and later, iPhone only | Opus 5.5 | accepted by merge |
| 2026-10-05 | #384 | UX | The web navigation as a bottom tab bar, with a More tab | Opus 5.5 | accepted by merge |
| 2026-10-05 | #384 | product scope | Admin pages, public pages, Connect X and bookmarks as web views in the app | Opus 5.5 | accepted by merge |
| 2026-10-05 | #384 | privacy | Sign-in by pasting the emailed link; the app keeps the cookie's 30-day expiry | Opus 5.5 | accepted by merge |
| 2026-10-05 | #384 | UX | An audible thinking cue between Stop and the reply, with a setting | Opus 5.5 | accepted by merge |
| 2026-10-05 | #384 | privacy | Memory-only HTTP cache; no analytics, no third-party SDKs, no content in logs | Opus 5.5 | accepted by merge |
| 2026-10-06 | #444 | privacy | Only Loore's own media loads by itself; every other image waits for a tap | Opus 5.5 | accepted by merge |
| 2026-10-06 | #444 | UX | An outside image shows a quiet placeholder; a tap loads that one image | Opus 5.5 | accepted by merge |
| 2026-10-06 | #444 | product scope | No global "always load images" setting | Opus 5.5 | accepted by merge |
| 2026-10-06 | #444 | privacy | A web content-security policy that allows only Loore's own images | Opus 5.5 | accepted by merge |
| 2026-10-06 | #444 | privacy | Image markdown in model output is not stripped on the server; the fix is in rendering | Opus 5.5 | accepted by merge |
| 2026-10-06 | #444 | UX | On the web, a tap opens the image in a new tab rather than in place | Opus 5.5 | accepted by merge |
| 2026-10-06 | #444 | privacy | Only `/media/...` paths count as Loore's own, with no percent-escapes | Opus 5.5 | accepted by merge |
| 2026-10-06 | #444 | UX | An outside image inside a link shows as plain text in that link | Opus 5.5 | accepted by merge |
| 2026-10-06 | #444 | UX | iPhone links outside the allowlist show as plain text | Opus 5.5 | accepted by merge |
| 2026-10-06 | #444 | user-facing text | The placeholder reads "Image from <host>: <alt>" | Opus 5.5 | accepted by merge |
| 2026-10-06 | #446 | privacy | Search snippets are escaped HTML whose only tag is `<mark>`; titles stay plain text | Opus 5.5 | accepted by merge |
| 2026-10-07 | #447 | UX | Moving to another node waits at most 2 s for it | Opus 5.5 | accepted by merge |
| 2026-10-07 | #447 | UX | Prefetched node data is used for at most 10 s | Opus 5.5 | accepted by merge |
| 2026-10-08 | #408 | product scope | LLM Response on an upload in a Read thread still runs without the Text-mode prompt; left to the upload work (#342, PR #347) | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #401 | product scope | An import is recognised by `origin` or by `source_key`, so imports from before the `origin` column don't count as the user's own entries | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #401 | UX | A continued Voice thread keeps "What's on your mind?"; only a fresh Voice session asks the welcome question | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #401 | UX | The client marks the first entry itself after a save, without refetching; an entry written elsewhere changes the question at the next page load | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #401 | product scope | Only polls wait for the first entry; changelog and notifications don't | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #401 | UX | Only an explicit `has_own_entries: false` marks a newcomer; a user object without the flag gets "What's on your mind?" | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #405 | testing | A Read counts as opened from a page load or from a poll sent by a visible tab, so the web page sends a visibility flag, a client change beyond the server-only plan | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #405 | testing | A finished Read that reached a hidden tab gets one more poll when the tab is shown, so it counts as opened | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #409 | reliability | A stop made only of failures Loore wasn't billed for lifts after 24 h (`UNBILLED_STOP_EXPIRY`); two billed failures stop recent context until a new summary or profile version | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #409 | provider | OpenAI recent-context batches go through chat/completions like the other batch jobs, on the same model | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #409 | reliability | A failed batch submit is not a strike; the next check, 10 minutes later, tries again | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #409 | reliability | The recent-context batch lock expires after 30 min if its holder dies (`RC_BATCH_LOCK_TTL`) | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #406 | user-facing text | The account-failure message, without the provider's name: "AI replies are temporarily unavailable. This is a problem on Loore's side, not yours, and it has been reported." | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #406 | user-facing text | An unknown model gets "This model isn't available any more. Choose another model." | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #406 | cost | With the OpenAI batch key revoked, profile users wait for the fix instead of moving to the full-price path on the chat key | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #404 | product scope | GPT-6.1 Sol and Sonnet 5.5 are not featured, and the Read default stays GPT-6 Luna | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #404 | provider | Admin polls refuse read-only and deprecated models | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #404 | product scope | A Read makes no todo proposal, even under an agentic read prompt | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #404 | user-facing text | A chat reply on a read-only model is refused with a message naming the model, e.g. "GPT-6.1 Sol is only for Read. Choose another model for replies." | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #403 | privacy | The iPhone app's privacy manifest leaves out the collected-data list until the App Store release, because an empty list would say the app collects nothing | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #417 | data safety | A blank list counts as empty, and a list item with text counts as a task with or without a checkbox, so the merge ticks an existing plain item instead of adding a duplicate | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #417 | product scope | No model-free path for an empty list: the model still writes it, since parsing proposals by hand would be brittle | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #431 | privacy | A past merge made with a custom prompt is run on the file prompt in `--current-prompt` mode; the custom prompt is not read or decrypted | Sonnet 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #431 | testing | `--current-prompt` selects the same merges as a normal run, so the results stay comparable | Sonnet 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #433 | reliability | A cut-off todo merge gets no fallback model and no automatic retry; the user asks again | Sonnet 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #433 | cost | Every cut-off merge result marks its cost row as refused, not only an empty one | Sonnet 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #452 | product scope | Haiku 5.5 is read-only, not featured and not the Read default | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #452 | provider | No fallbacks and no move to another model when Haiku 5.5 refuses a Read | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #452 | cost | A Haiku 5.5 Read runs through the Batch API like other Reads | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #452 | cost | A Haiku 5.5 Read sends no effort or thinking setting, so it runs at Haiku's default | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
| 2026-10-08 | #452 | cost | Sonnet 5.5's recorded cache-hit price corrected to 0.05× in the same PR | Opus 5.5 | accepted by merge (PR #451, 2026-10-08) |
