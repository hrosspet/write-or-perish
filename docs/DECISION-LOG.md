# Decision log

Peter's product decisions, in his own words, with the prediction I (Claude) made before he answered. It exists so that `LOORE-ESSENCE.md` can learn from his reviews, and so that the hit rate shows where my model of his choices is good enough to decide alone.

How it works:
- **Before a review**, I write down what I expect him to answer and how sure I am. The prediction is recorded before his answer, in Loore or in my notes, and copied here afterwards.
- **After the review**, each entry gets his answer verbatim, a score (hit, partial or miss), and for a miss, what made me guess wrong.
- **Rules** that come out of his answers are proposed for `LOORE-ESSENCE.md`. Nothing changes that document without his approval.
- **Where my predictions matched** his answers over the last ten decisions of a kind, I decide those myself and record the decision here; elsewhere I keep asking.
- **Backfilled entries** (decisions made before this log existed) have no prediction. They teach the log, but they can't measure it.
- **Left out:** security problems until they are fixed (then they come in, with the prediction and decision), other users' data, and Peter's personal matters.

## Hit rate

| Date | Session | Parts scored | Hits | Partial | Misses | Score | Expected from my confidence |
|---|---|---|---|---|---|---|---|
| 2026-10-02 | Paid Beta voice review | 10 | 4 | 2 | 4 | 5.0 | 6.45 |

Partial hits count as half. Calibration on 2026-10-02: at 75–80 % confidence I scored 2.5 of 4; at 55–65 %, 2.5 of 5; at 40 %, 0 of 1. Overconfident by about 15 points.

One more miss is not in the table: I excluded the question of when to post the situations, claiming his reply already showed "now". He answered "after the Beta scope is settled", as I had proposed.

## Patterns in my misses

- **Hard lines read as conditions.** Where Peter holds a rule without exceptions ("never change providers", "never decrypt users' content"), I predicted a yes with conditions.
- **A cost emergency generalised into a cost mandate.** The 2026-09-12 digest incident was a fix for runaway spend, not a standing wish to cut cost. In this phase he puts product quality first.
- **My own proposals used as evidence of his view.** "Consenting alpha users" came from my triage note, not from him.
- **Assumed constraints instead of checked ones.** I guessed a social reason for not contacting reporters; the real limit was that no channel exists.

## Entries

### 2026-10-02 · A provider fails for an account reason

- **Situation:** a provider fails for a reason waiting won't fix (spend limit, billing, revoked key), and a paying user's reply fails. Do we tell the user which provider failed, and switch them to the other provider at our cost?
- **Prediction:** name the provider, yes (75 %). Fall back to the other provider automatically, with the reply showing which model wrote it (55 %; second guess: a one-click "answer with GPT instead").
- **Peter:** "no, we should just prevent failures for account reasons. They will happen anyway in case of a rapid user growth. But then it'll be fine and we'll figure it out. Changing providers is however never acceptable."
- **Score:** two misses.
- **Why I missed:** I weighed friction and cost and treated the provider as a detail Loore can swap. For Peter, the model a user chose is not Loore's to change, and account failures are to be prevented, not designed around.
- **Applies to:** #369 (plain "temporarily unavailable" message and an alert to Peter; no fallback, provider not named), #360.
- **Rule:** "A user's model is theirs" in `LOORE-ESSENCE.md`.

### 2026-10-02 · Cheaper models and the Batch API for background jobs

- **Situation:** for background jobs such as the todo merge, recent context or intentions, may I choose the cheapest model or the Batch API whenever a blind comparison shows no loss and the result can be some hours old?
- **Prediction:** yes, I decide myself as long as I record the comparison and the choice; Peter judges user-facing text, I judge mechanical jobs (65 %).
- **Peter:** "batch api for background jobs is a natural choice, use it wherever it makes sense" · "on the other hand, cost vs. quality is something I'd like have a say in" · "what could remove some decision burden from me would be making comparisons between different models without my say, if it's cheap - so I can imagine you making an experiment where you run a comparison with the cheapest available model (like GPT-6 Luna) against my prod data and compare against the frontier models that I triggered in the past manually. Like todo merges" · "this is still on prod, so I'd like to start the experiment manually myself, but you can expect me to want to run such experiments" · "we're still in a really early phase, so we're not optimizing for cost. We want to build the best product we can, and if paying for frontier models is what it takes, then ok. Later, when we have too many todo merge requests and it costs us a ton, only then it will make sense to run such experiments, whose aim is cost optimization" · "smaller models also may have better latency, and that is something that influences UX a lot. So when todo merges have 100% accuracy on both frontier, and small models, and small models have significantly better latency, then that would be desired."
- **Score:** partial. The Batch API part was right; deciding the model myself was wrong.
- **Why I missed:** I generalised from the digest cost incident and missed latency as a criterion.
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

- **Situation:** with no reproduction, may I ask the reporter directly, ship a defensive fix with error logging, and close the issue if it doesn't recur?
- **Prediction:** logging yes; asking the reporter yes, but Peter asks them himself; no fix that only hides the symptom; close after a stated quiet period (55 %).
- **Peter:** "I don't think you have a way of contacting the reporter directly, unless they reported a bug via their own github account? Well, in that case yes, you can ask them directly. Btw this policy may change in the future - it's for current alpha and early Beta. If there is significant growth, we might want to rethink PR policy" · "or did you mean via the changelog feature in Loore? Users can submit issues via Loore, which uses my hrosspet account, but tags the issue with their username. We're letting the users know when an issue they submitted is resolved. You propose to use the same mechanism? In that case I give the same response as for the above case"
- **Score:** miss on the part he answered (who asks). The defensive fix and closing were not answered.
- **Why I missed:** I assumed a social reason (people he knows) where the real limit is technical: Loore can tell a reporter that their issue is closed, but can't ask them a question.
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
- **Why I missed:** I reasoned from a legal deadline I hadn't checked. No law sets a grace period: GDPR asks for erasure without undue delay and an answer within a month (extendable by two for complex cases), and a 30-day window the user agrees to is common practice.
- **Applies to:** #268, #269.
- **Rule:** "Deletion" in `LOORE-ESSENCE.md`.

### 2026-10-02 · Experiments on other users' archives

- **Situation:** may experiments run on other users' archives if the outputs stay in files and never reach their accounts?
- **Prediction:** public data (Community Archive tweets) yes; private Loore writing only Peter's own archive plus users who agreed to that experiment (75 %).
- **Peter:** "no, we never decrypt user's content" · "we can run scripts over their unencrypted metadata" · "if we need content, we can run it on my data (I have plenty), if the experiment is non-destructive - ie keeps the original data untouched, just used for comparison"
- **Score:** partial. Peter's own archive was right; the consent exception was wrong.
- **Why I missed:** the consent idea came from my own triage note on #359, which I took as his view.
- **Applies to:** #357, #359, the Read notes test.
- **Rule:** "Nobody decrypts users' content" in `LOORE-ESSENCE.md`.

### 2026-10-02 · Where the decision log lives

- **Situation:** a log in the public repository, or a private one in Loore or my notes?
- **Prediction:** the public repository, with anything security-related left out (60 %).
- **Peter:** "yeah, let's try putting it on the public repository in the spirit of transparency (this is your model of me / my vision of Loore, and this should be public)" · "what do you mean security-related? I'd keep out information that would reveal security issues publicly before they are fixed. But after they are fixed, I'd include publicly your predictions (made locally in your memory) and my decisions, or feedback from followup issues"
- **Score:** hit. "Security-related" narrows to unfixed security problems.
- **Rule:** "Transparency" in `LOORE-ESSENCE.md`.

### 2026-10-02 · When to post the decision situations

- **Situation:** post the situations for the Beta-relevant issues now, or after the Beta scope is settled?
- **Prediction:** not made; I wrongly claimed his reply already showed "now".
- **Peter:** "yes" (to "after we settle the Beta scope").
- **Score:** miss, not counted in the table.

### 2026-10-02 · Moving reversible engineering items to ready

- **Situation:** seven to nine "needs your input" issues already recommended a reversible fix; under the standing rule (ask only before irreversible actions) they move to ready to build.
- **Prediction:** not scored; he had answered before the predictions.
- **Peter:** "go ahead" · "yes"
- **Applies to:** #217, #218, #275, #224, #216, #374, #287; #379 and #331 except Peter's prod steps.
