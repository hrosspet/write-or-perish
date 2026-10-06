#!/usr/bin/env python3
r"""Compare todo-merge models on one account's past merges (#234).

A todo merge is the background job that applies an accepted todo proposal
to the full todo list (backend/tasks/voice_todo_merge.py, prompt
backend/prompts/orient_apply_todo.txt). It runs on the model of the
conversation that made the proposal. This script re-runs past merges of ONE
account on candidate models and compares each output with the stored one,
so the choice of a merge model can be made on data: accuracy, latency,
tokens, cost.

Usage (on the prod VM, from the app dir, for your own account only):

    cd ~/write-or-perish
    source ~/miniconda3/etc/profile.d/conda.sh && conda activate write-or-perish
    # 1. Free: which merges can be rebuilt, and what a run would cost.
    python backend/scripts/compare_todo_merge_models.py --user hrosspet --dry-run
    # 2. The comparison: the 20 newest rebuildable merges on GPT-6 Luna.
    python backend/scripts/compare_todo_merge_models.py --user hrosspet
    # Optional: more models, a date window, and the stored model re-run
    # on the same inputs (that is what gives it a latency to compare with).
    python backend/scripts/compare_todo_merge_models.py --user hrosspet \
        --models gpt-6-luna gpt-6-sol --since 2026-08-01 --limit 20 --rerun-original

After the prompt file changed (#417), the past merges can't be rebuilt with
the prompt they used, and the run above refuses. To test today's merge on
the same past inputs, run (Luna is the default model):

    python backend/scripts/compare_todo_merge_models.py --user hrosspet --current-prompt

The evaluation of the merge by edits (#234) on Opus 5.5:

    python backend/scripts/compare_todo_merge_models.py --user hrosspet --current-prompt --models claude-opus-5.5

--current-prompt runs every merge (candidates and --rerun-original) the way
the task runs it today: the CURRENT backend/prompts/orient_apply_todo.txt
through the task's message builder (which appends the reply format,
REPLY_FORMAT, after any merge prompt), the model replying with edits
({old_text, new_text}), and the task's own function
(backend/utils/todo_merge_edits.run_todo_merge) applying them to the
previous todo list, with its one retry after a refused reply and its
checks. The resulting list is compared with the stored output as in a
normal run. Each run also records, over all calls of the merge: the edits
applied, retries, anchor errors (old_text not found or not unique), how
often the kept-lines check (an existing line changed or missing) refused a
reply, refused full rewrites, unparseable replies, the final failure if
the merge saved nothing, tokens, latency and cost; the JSONL also has each
reply. The inputs (proposal, previous todo list) and the stored output the
results are compared with stay those of the past merge. The
PROMPT_FILE_SHA256 check does not apply in this mode; the run record, each
merge record and the terminal summary say that the current prompt was used,
with its sha256. A merge whose stored prompt was a custom UserPrompt row is
run on the file prompt too, like the others (the custom row is not read).

create_app loads .env.production (prod DB, KMS key, API keys) on its own.
Before anything is decrypted, a run (not --dry-run) prints the account's
username and asks you to type it to continue.
Results go to ~/todo-merge-compare-u<id>-<UTC time>.jsonl (mode 0600, the
todo texts are in it); the terminal shows counts and metrics only.

Estimated cost per merge, without --current-prompt (one model call that
rewrites the list). Assumes a ~4k-token todo list: ~5k input and ~4k
output tokens, no cache hits. Reasoning tokens bill as output and come on
top. `--dry-run` prints the estimate from the token counts of your own
past merges (api_cost_log) instead.

    gpt-6-luna         ~$0.0025   (20 merges: ~$0.05)    <- the default
    gpt-6-sol          ~$0.05
    claude-opus-5.5    ~$0.10
    claude-opus-4.6    ~$0.13
    claude-fable-5.1   ~$0.25     (gpt-6-astra the same)

With --current-prompt (edits): the same input plus ~600 tokens (the longer
prompt and the reply schema), and ~1.5k output tokens (the edits and the
model's reasoning) instead of the whole list. A refused reply adds one
retry call of about the same size.

    gpt-6-luna         ~$0.0013
    gpt-6-sol          ~$0.03
    claude-opus-5.5    ~$0.05     (20 merges: ~$1; ~$2 if every merge retried)
    claude-opus-4.6    ~$0.07
    claude-fable-5.1   ~$0.13

`--dry-run --current-prompt` prints this estimate per model from your own
past merges' input tokens.

`--rerun-original` adds one merge on the stored merge's own model per
merge (a frontier model: ~$0.10-0.25 each, ~$2-5 for 20 merges; with
--current-prompt about half that).

Safety
------
* One account: --user is required, every query is scoped to that account,
  every row is checked to belong to it before it is decrypted, and the
  account must be an admin's (the script is for Peter's own data). Before
  anything is decrypted, the script prints the account's username and runs
  only if you type that username (--dry-run decrypts nothing and doesn't
  ask). There is no "all users" mode.
* No error reports: main() removes SENTRY_DSN from the environment before
  create_app(), so Sentry is never initialised. An unhandled exception or
  Ctrl-C would otherwise send the stack frames' local variables, which
  hold the decrypted texts, to Sentry.
* Never writes to the database: no todo versions, nodes, drafts or
  api_cost_log rows. On PostgreSQL every transaction runs READ ONLY, and
  the ORM session refuses any flush. Costs are computed with the app's
  calculator (backend.utils.cost) and only written to the JSONL.
* Content whose AI usage is 'none' (the proposal or the previous todo
  list), or an account set to 'none', is never sent to a model.
* Light on the VM: metadata (ids, timestamps, token counts) is loaded as
  plain tuples; content is decrypted (one KMS call per row) only for the
  merges that run, one merge at a time; results are written as they come.

Which merges can be rebuilt exactly
-----------------------------------
A merge output is a UserTodo version with generated_by='voice_session'
(the only writer is voice_todo_merge._run_merge). Its inputs are the
proposal node's text, the todo version that was newest when the merge ran,
and the account's orient_apply_todo prompt. Nothing stored links a merge
output to its proposal: the task passes ``todo_id=new_todo.id`` before the
row is flushed, so ``todo_id`` never reaches the node's tool_calls_meta.
The link is derived instead:

* A merge commits its todo version, its api_cost_log row (request_type
  'todo_merge') and the proposal's apply_status='completed' together, so
  merge outputs and applied proposals correspond one to one. (A merge by
  edits that needed a retry, #234, writes a row per call; its version's
  tokens_used is the applied call's output, so it pairs with that row.)
* Applying a proposal deletes every pending todo draft of the account, so
  proposals are applied in the order they were written, and merges run in
  that order (one at a time, under a per-user lock).

So the k-th newest merge output pairs with the k-th newest applied
proposal. A pair is used only when it passes every check below; otherwise
the merge is skipped and counted under its reason:

* made on or after 2026-04-03 (the prompt file and proposal tracking
  changed in #95 on 2026-04-02);
* the proposal was written after the previous merge output and before
  this one (a pairing shifted by a lost proposal always fails this, since
  a proposal is always older than its own merge output);
* its api_cost_log row exists (same commit: within 2 s, same output
  tokens) and names the proposal's model; that row is the stored model;
* no other todo version was saved between the proposal and the merge
  output (otherwise it is unknown which one the merge read);
* the proposal was not deleted, and not edited at or after the time of
  the merge output (ticking or adding items in the proposal card before
  the apply is allowed: the stored text is then what the merge read);
* AI usage of the proposal and of the previous todo list allows AI;
* the merge prompt did not change between the proposal and the merge.

Not detectable from stored data, so not checked: a checkbox toggle, quick
add or task insert (PATCH /api/todo edits the newest version in place)
made during the few seconds of the model call would be in the stored
previous version but not in what the merge read. And edits made later on
the merge output itself are in the stored output: compare checkbox-state
differences with that in mind, or use --rerun-original, which compares
against a fresh run of the stored model.

Accuracy
--------
Each todo list is parsed into items: (section heading, item text,
checkbox state), where an item is a list line with or without a checkbox.
Against a reference (the stored output, and with --rerun-original also
the original model's fresh output) the script reports: exact match (same
sections, items, order and checkbox states), identical text, same items
in any order, and counts of items added, removed, reworded (similarity
>= 0.8 to a removed item), moved to another section, and with a changed
checkbox state. It also counts items of the previous todo list missing
from each output (the prompt says never remove anything).
"""
import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from difflib import SequenceMatcher

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sqlalchemy import event, func, or_  # noqa: E402

from backend.extensions import db  # noqa: E402
from backend.models import (  # noqa: E402
    APICostLog, Node, NodeVersion, User, UserPrompt, UserTodo)
from backend.utils.privacy import AI_ALLOWED, account_allows_ai  # noqa: E402

DEFAULT_MODELS = ["gpt-6-luna"]
DEFAULT_LIMIT = 20

# The merge task's marker on the todo versions it writes.
MERGE_GENERATED_BY = "voice_session"
# Tool-meta entry of the proposal; renamed from update_todo in #95.
PROPOSAL_ENTRY_NAMES = ("propose_todo", "update_todo")
PROMPT_KEY = "orient_apply_todo"
# Merges before this ran another default prompt (#95, 2026-04-02 added the
# "## Group Name" rule) and older proposal tracking.
FLOOR = datetime(2026, 4, 3)
# sha256 of backend/prompts/orient_apply_todo.txt, unchanged since #95. The
# rebuild assumes every merge since FLOOR used this file as its default; if
# the file changes, set a new FLOOR and hash (or teach the script both).
PROMPT_FILE_SHA256 = (
    "96745bff99f4e05abf5deb50f0a6d9059c790bb6141e31bdc00c5442d98538c8")
# A merge's api_cost_log row and its todo version are flushed in one
# commit, milliseconds apart.
COST_ROW_TOLERANCE = timedelta(seconds=2)
# Heuristic: a removed and an added item at least this similar
# (difflib ratio) count as one reworded item.
REWORD_SIMILARITY = 0.8
# Heuristics for the --dry-run estimate of the merge by edits
# (--current-prompt), which no past merge has token counts for: the edits
# prompt and the reply schema add ~600 input tokens to what the past merge
# sent; the reply (the edits and the model's reasoning) is ~1.5k output
# tokens instead of the whole list.
EDITS_EXTRA_INPUT_TOKENS = 600
EDITS_OUTPUT_TOKENS = 1500

SKIP_REASONS = {
    "before_floor": "made before 2026-04-03 (older prompt and proposal "
                    "tracking)",
    "unpaired": "no applied proposal left to pair with (older history)",
    "not_between": "proposal not written between the previous merge "
                   "output and this one",
    "no_cost_row": "no matching api_cost_log row (stored model unknown)",
    "model_mismatch": "cost row model differs from the proposal's model",
    "version_between": "another todo version was saved between the "
                       "proposal and the merge",
    "proposal_deleted": "proposal deleted",
    "proposal_edited": "proposal edited after the merge",
    "proposal_ai_none": "proposal AI usage is none",
    "previous_ai_none": "previous todo list AI usage is none",
    "prompt_changed": "merge prompt changed between proposal and merge",
    "proposal_empty": "proposal text is empty",
}


class ReadOnlyViolation(RuntimeError):
    """Something tried to write to the database during the comparison."""


# ── read-only guards ─────────────────────────────────────────────────────

def _set_transaction_read_only(conn):
    # Runs as the first statement of every transaction (the "begin" event
    # fires before the driver's implicit BEGIN completes): PostgreSQL then
    # rejects any INSERT/UPDATE/DELETE in it.
    conn.exec_driver_sql("SET TRANSACTION READ ONLY")


def make_postgres_read_only(engine):
    """Every transaction of this process on *engine* runs READ ONLY (a
    no-op on other databases). Transaction-scoped, so it never leaks to
    other clients of the database."""
    if engine.dialect.name != "postgresql":
        return False
    if not event.contains(engine, "begin", _set_transaction_read_only):
        event.listen(engine, "begin", _set_transaction_read_only)
    return True


@contextmanager
def refuse_writes():
    """The ORM session refuses to flush anything while this is active."""
    session = db.session()

    def _refuse(sess, flush_context, instances):
        raise ReadOnlyViolation(
            "compare_todo_merge_models is read-only; refusing to flush "
            f"{len(sess.new)} new, {len(sess.dirty)} changed, "
            f"{len(sess.deleted)} deleted objects")

    event.listen(session, "before_flush", _refuse)
    try:
        yield
    finally:
        db.session.rollback()
        event.remove(session, "before_flush", _refuse)


# ── the account ──────────────────────────────────────────────────────────

def resolve_user(ident):
    """The one account to compare, by id or username. Raises SystemExit
    (nothing has been read) when it is missing or may not be used."""
    if ident is None or not str(ident).strip():
        raise SystemExit("Refusing to run: pass --user <id or username> "
                         "(your own account). There is no all-users mode.")
    ident = str(ident).strip()
    user = (db.session.get(User, int(ident)) if ident.isdigit()
            else User.query.filter_by(username=ident).first())
    if user is None:
        raise SystemExit(f"Refusing to run: no user {ident!r}.")
    if not user.is_admin:
        raise SystemExit(
            f"Refusing to run for user {user.id}: this comparison reads "
            "and decrypts the account's todo lists and proposals, so it "
            "only runs on an admin's own account.")
    if not account_allows_ai(user):
        raise SystemExit(
            f"Refusing to run for user {user.id}: the account's AI usage "
            "is none, so its content is never sent to a model.")
    return user


def confirm_account(user, out):
    """Ask the operator to type the account's username before anything is
    decrypted (the admin check alone allows any admin's account). Raises
    SystemExit when the answer differs or there is no terminal."""
    print(f"\nThis run decrypts the todo lists and proposals of "
          f"{user.username!r} (user {user.id}) and sends them to the "
          "models above.", file=out)
    try:
        answer = input("Type the username to continue: ")
    except EOFError:
        answer = None
    if answer is None or answer.strip() != user.username:
        raise SystemExit("Not confirmed: nothing was decrypted, no model "
                         "was called, no file was written.")


def _owned(row, user_id):
    """*row* if it belongs to *user_id*; raises before anything is
    decrypted otherwise (defence in depth: every query is scoped)."""
    owner = (row.human_owner_id if isinstance(row, Node)
             else row.user_id)
    if owner != user_id:
        raise RuntimeError(
            f"{type(row).__name__} {row.id} does not belong to user "
            f"{user_id}; refusing to decrypt it")
    return row


# ── stored metadata (no content) ─────────────────────────────────────────

def load_versions(user_id):
    """Every todo version of the account, oldest first, without content."""
    return db.session.query(
        UserTodo.id, UserTodo.created_at, UserTodo.generated_by,
        UserTodo.tokens_used, UserTodo.ai_usage,
    ).filter(UserTodo.user_id == user_id).order_by(
        UserTodo.created_at, UserTodo.id).all()


def _proposal_state(tool_calls_meta):
    """(applied, truncated) of a node's proposal entry."""
    try:
        meta = json.loads(tool_calls_meta or "[]")
    except (json.JSONDecodeError, TypeError):
        return False, False
    for entry in meta if isinstance(meta, list) else []:
        if (isinstance(entry, dict)
                and entry.get("name") in PROPOSAL_ENTRY_NAMES):
            return (entry.get("apply_status") == "completed",
                    bool(entry.get("apply_truncated")))
    return False, False


def load_applied_proposals(user_id):
    """The account's applied todo proposals, oldest first, without
    content: dicts with id, created_at, llm_model, ai_usage, deleted_at,
    truncated."""
    rows = db.session.query(
        Node.id, Node.created_at, Node.llm_model, Node.ai_usage,
        Node.deleted_at, Node.tool_calls_meta,
    ).filter(
        Node.human_owner_id == user_id,
        Node.node_type == "llm",
        or_(*[Node.tool_calls_meta.like(f'%"{name}"%')
              for name in PROPOSAL_ENTRY_NAMES]),
    ).order_by(Node.created_at, Node.id).all()
    applied = []
    for row in rows:
        done, truncated = _proposal_state(row.tool_calls_meta)
        if done:
            applied.append({
                "id": row.id, "created_at": row.created_at,
                "llm_model": row.llm_model, "ai_usage": row.ai_usage,
                "deleted_at": row.deleted_at, "truncated": truncated,
            })
    return applied


def load_cost_rows(user_id):
    return db.session.query(
        APICostLog.id, APICostLog.created_at, APICostLog.model_id,
        APICostLog.input_tokens, APICostLog.output_tokens,
        APICostLog.cost_microdollars,
    ).filter(
        APICostLog.user_id == user_id,
        APICostLog.request_type == "todo_merge",
    ).order_by(APICostLog.created_at).all()


def load_last_edit_times(node_ids):
    """{node id: time of its newest NodeVersion} for the edited nodes
    among *node_ids*. A NodeVersion keeps the text from before an edit and
    is stamped when the edit is saved."""
    last_edit = {}
    node_ids = list(node_ids)
    for start in range(0, len(node_ids), 500):
        chunk = node_ids[start:start + 500]
        last_edit.update(db.session.query(
            NodeVersion.node_id, func.max(NodeVersion.timestamp)).filter(
                NodeVersion.node_id.in_(chunk)).group_by(
                    NodeVersion.node_id).all())
    return last_edit


def load_prompt_rows(user_id):
    return db.session.query(
        UserPrompt.id, UserPrompt.created_at, UserPrompt.generated_by,
    ).filter(
        UserPrompt.user_id == user_id, UserPrompt.prompt_key == PROMPT_KEY,
    ).order_by(UserPrompt.created_at, UserPrompt.id).all()


def prompt_source_at(prompt_rows, when):
    """Which prompt get_user_prompt returned at *when*: None for the file
    default, else the UserPrompt id. Since FLOOR the file default has not
    changed, so a 'default' row resolves to the file either way (a stale
    one is replaced by the file, a current one holds the file's text)."""
    row = None
    for candidate in prompt_rows:
        if candidate.created_at <= when:
            row = candidate
        else:
            break
    if row is None or row.generated_by == "default":
        return None
    return row.id


def _prompt_changed(prompt_rows, start, end):
    """Whether the prompt in effect may differ anywhere in (start, end)."""
    sources = {prompt_source_at(prompt_rows, start)}
    for row in prompt_rows:
        if start < row.created_at < end:
            sources.add(prompt_source_at(prompt_rows, row.created_at))
    return len(sources) > 1


def _cost_row_for(version, cost_rows):
    near = [c for c in cost_rows
            if abs(c.created_at - version.created_at) <= COST_ROW_TOLERANCE
            and (c.output_tokens or 0) == (version.tokens_used or 0)]
    if not near:
        return None
    return min(near, key=lambda c: abs(c.created_at - version.created_at))


def _link_problem(todo, proposal, previous_merge, cost):
    """Why the rank pairing of *todo* with *proposal* can't be trusted,
    or None."""
    if not (proposal["created_at"] < todo.created_at
            and (previous_merge is None
                 or previous_merge.created_at < proposal["created_at"])):
        return "not_between"
    if cost is None:
        return "no_cost_row"
    if proposal["llm_model"] and cost.model_id != proposal["llm_model"]:
        return "model_mismatch"
    return None


def _input_problem(todo, proposal, previous, last_edit, prompt_rows):
    """Why the merge's inputs can't be rebuilt exactly or may not be sent
    to a model, or None."""
    if previous is not None and previous.created_at > proposal["created_at"]:
        return "version_between"
    if proposal["deleted_at"] is not None:
        return "proposal_deleted"
    # The proposal card allows edits (ticking, moving, adding items) only
    # before the apply, so an edit saved before the merge output is in the
    # text the merge read. Only a later edit changed it since.
    edited_at = last_edit.get(proposal["id"])
    if edited_at is not None and edited_at >= todo.created_at:
        return "proposal_edited"
    if proposal["ai_usage"] not in AI_ALLOWED:
        return "proposal_ai_none"
    if previous is not None and previous.ai_usage not in AI_ALLOWED:
        return "previous_ai_none"
    if _prompt_changed(prompt_rows, proposal["created_at"], todo.created_at):
        return "prompt_changed"
    return None


def link_merges(versions, proposals, cost_rows, last_edit,
                prompt_rows, floor=FLOOR):
    """Pair every merge output with its proposal and inputs (metadata
    only). *last_edit* maps a proposal id to the time of its newest edit.
    Returns one dict per merge output, newest first, with ``skip`` set to
    a SKIP_REASONS key when it can't be rebuilt exactly.
    """
    merges = [v for v in versions if v.generated_by == MERGE_GENERATED_BY]
    # Rank pairing from the newest end: the k-th newest merge output with
    # the k-th newest applied proposal.
    offset = len(proposals) - len(merges)
    position = {v.id: i for i, v in enumerate(versions)}
    linked = []
    for i, todo in enumerate(merges):
        k = position[todo.id]
        rec = {"todo": todo, "proposal": None, "cost": None,
               "previous": versions[k - 1] if k > 0 else None,
               "prompt_id": None, "skip": None}
        linked.append(rec)
        if todo.created_at < floor:
            rec["skip"] = "before_floor"
            continue
        if i + offset < 0:
            rec["skip"] = "unpaired"
            continue
        rec["proposal"] = proposals[i + offset]
        rec["cost"] = _cost_row_for(todo, cost_rows)
        rec["skip"] = (
            _link_problem(todo, rec["proposal"],
                          merges[i - 1] if i > 0 else None, rec["cost"])
            or _input_problem(todo, rec["proposal"], rec["previous"],
                              last_edit, prompt_rows))
        if rec["skip"] is None:
            rec["prompt_id"] = prompt_source_at(prompt_rows, todo.created_at)
    linked.reverse()
    return linked


# ── the merge call, as the app builds it ─────────────────────────────────

def build_merge_messages(merge_prompt, update_summary, current_todo):
    """The messages voice_todo_merge._run_merge sends, from the task's own
    builder, so the message and the prompt file come from the same deploy.

    Imported here, not at the top: importing the task module imports
    backend.celery_app, which runs create_app() once more. main() has
    removed SENTRY_DSN before this runs."""
    from backend.tasks.voice_todo_merge import (
        build_merge_messages as task_build_merge_messages)
    return task_build_merge_messages(merge_prompt, update_summary,
                                     current_todo)


def file_default_prompt(check_hash=True):
    """The prompt file's text. By default it must still be the file past
    merges used (PROMPT_FILE_SHA256); --current-prompt skips that check."""
    from backend.utils.prompts import load_default_prompt
    text = load_default_prompt(PROMPT_KEY)
    digest = hashlib.sha256(text.encode()).hexdigest()
    if check_hash and digest != PROMPT_FILE_SHA256:
        raise SystemExit(
            "Refusing to run: backend/prompts/orient_apply_todo.txt changed "
            "since this script was written, so past merges' default prompt "
            "can't be rebuilt from it. Update FLOOR and PROMPT_FILE_SHA256.")
    return text


def run_model(provider, model_id, messages, api_keys):
    """One live call, timed. Cost from the app's calculator; no
    api_cost_log row is written."""
    from backend.utils.cost import llm_cost_log_fields
    started = time.monotonic()
    try:
        response = provider.get_completion(model_id, messages, api_keys)
    except ReadOnlyViolation:
        raise
    except Exception as e:  # recorded, the run goes on
        return {"model": model_id, "ok": False,
                "latency_s": round(time.monotonic() - started, 3),
                "error_type": type(e).__name__, "error": str(e)}
    latency = time.monotonic() - started
    fields = llm_cost_log_fields(model_id, response)
    text = response.get("content") or ""
    return {
        "model": model_id, "ok": bool(text.strip()),
        "error_type": None if text.strip() else "EmptyOutput",
        "error": None if text.strip() else "empty model output",
        "latency_s": round(latency, 3),
        "input_tokens": fields["input_tokens"],
        "output_tokens": fields["output_tokens"],
        "cache_read_tokens": fields.get("cache_read_tokens", 0),
        "cache_write_tokens": fields.get("cache_write_tokens", 0),
        "cost_usd": fields["cost_microdollars"] / 1e6,
        "truncated": bool(response.get("truncated")),
        "text": text,
    }


def run_model_edits(provider, model_id, messages, api_keys, previous_text):
    """One merge as the task runs it now (--current-prompt): the model's
    edits applied to *previous_text* by the task's own function, with its
    retry and checks; timed over all its calls. Cost from the app's
    calculator, summed over the calls; no api_cost_log row is written."""
    from backend.utils.cost import llm_cost_log_fields
    from backend.utils import todo_merge_edits
    merge = todo_merge_edits.MergeRun()
    started = time.monotonic()
    raised = None
    try:
        todo_merge_edits.run_todo_merge(provider, model_id, messages,
                                        api_keys, previous_text, merge)
    except ReadOnlyViolation:
        raise
    except Exception as e:  # recorded with the calls made before it
        raised = e
    latency = time.monotonic() - started
    fields = [llm_cost_log_fields(model_id, r) for r in merge.responses]

    def total(key):
        return sum(f.get(key, 0) or 0 for f in fields)

    result = {
        "model": model_id,
        "mode": "edits",
        "latency_s": round(latency, 3),
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "cache_read_tokens": total("cache_read_tokens"),
        "cache_write_tokens": total("cache_write_tokens"),
        "cost_usd": total("cost_microdollars") / 1e6,
        "truncated": any(r.get("truncated") for r in merge.responses),
        "edits": merge.stats(),
        # The JSONL only: replies and refusal reasons quote the list.
        "replies": merge.replies,
        "refusals": merge.refusals,
        "text": merge.merged or "",
    }
    if raised is not None:
        result.update(ok=False, error_type=type(raised).__name__,
                      error=str(raised))
    elif merge.failure is not None:
        result.update(ok=False, error_type=f"MergeFailed:{merge.failure}",
                      error=f"the merge saved nothing ({merge.failure})")
    else:
        result.update(ok=True, error_type=None, error=None)
    return result


# ── accuracy ─────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$")
_ITEM_RE = re.compile(
    r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(?:\[([ xX])\][ \t]*)?(.*?)[ \t]*$")


def _norm(text):
    return " ".join(text.split())


def parse_todo(text):
    """Items of a markdown todo list: (section, text, checked) tuples in
    order; checked is None for a list line without a checkbox."""
    items, section = [], ""
    for line in (text or "").splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            section = _norm(heading.group(1))
            continue
        item = _ITEM_RE.match(line)
        if item:
            box = item.group(1)
            checked = None if box is None else box.lower() == "x"
            items.append((section, _norm(item.group(2)), checked))
    return items


def _norm_text(text):
    return "\n".join(line.rstrip()
                     for line in (text or "").strip().splitlines())


def _match_exact(ref_items, cand, free, same):
    """Pair each reference item with the first free candidate item that
    *same* accepts. Returns (pairs, unmatched reference items)."""
    pairs, left = [], []
    for r in ref_items:
        ci = next((ci for ci in free if same(r, cand[ci])), None)
        if ci is None:
            left.append(r)
        else:
            free.remove(ci)
            pairs.append((r, cand[ci]))
    return pairs, left


def _most_similar(text, cand, free):
    """The free candidate item most similar to *text*, if any reaches
    REWORD_SIMILARITY."""
    best, best_ratio = None, REWORD_SIMILARITY
    for ci in free:
        matcher = SequenceMatcher(None, text, cand[ci][1])
        if matcher.real_quick_ratio() < best_ratio:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio or (best is None and ratio >= best_ratio):
            best, best_ratio = ci, ratio
    return best


def compare_todos(reference, candidate):
    """Item-level comparison of *candidate* against *reference*."""
    ref, cand = parse_todo(reference), parse_todo(candidate)
    free = list(range(len(cand)))
    # 1. same section and text; 2. same text in another section.
    pairs, left = _match_exact(ref, cand, free,
                               lambda r, c: r[:2] == c[:2])
    moved_pairs, left = _match_exact(left, cand, free,
                                     lambda r, c: r[1] == c[1])
    moved = [[r[1], r[0], c[0]] for r, c in moved_pairs]
    # 3. similar text: reworded.
    reworded, removed = [], []
    for r in left:
        ci = _most_similar(r[1], cand, free)
        if ci is None:
            removed.append(r)
            continue
        free.remove(ci)
        moved_pairs.append((r, cand[ci]))
        reworded.append([r[1], cand[ci][1]])
    checkbox = [[c[1], r[2], c[2]] for r, c in pairs + moved_pairs
                if r[2] != c[2]]
    added = [cand[ci] for ci in free]
    return {
        "exact_match": ref == cand,
        "identical_text": _norm_text(reference) == _norm_text(candidate),
        "same_items_any_order": Counter(ref) == Counter(cand),
        "items_reference": len(ref),
        "items_candidate": len(cand),
        "added": len(added),
        "removed": len(removed),
        "reworded": len(reworded),
        "moved": len(moved),
        "checkbox_changed": len(checkbox),
        "details": {
            "added": [[s, t, c] for s, t, c in added],
            "removed": [[s, t, c] for s, t, c in removed],
            "reworded": reworded,
            "moved": moved,
            "checkbox_changed": checkbox,
        },
    }


def missing_input_items(previous_todo, output):
    """Item texts of the previous todo list that the output lacks (any
    section): the prompt says to keep every existing item."""
    have = Counter(t for _, t, _ in parse_todo(output) if t)
    want = Counter(t for _, t, _ in parse_todo(previous_todo) if t)
    return sorted((want - have).elements())


# ── output ───────────────────────────────────────────────────────────────

def default_out_path(user_id):
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    return os.path.expanduser(
        f"~/todo-merge-compare-u{user_id}-{stamp}.jsonl")


def open_private(path):
    """A new file only the owner can read (0600); never overwrites."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


def _iso(value):
    return value.isoformat() + "Z" if value else None


def _usd(microdollars):
    return (microdollars or 0) / 1e6


def _fmt_edits(run):
    """Counts of a merge by edits (no list text)."""
    e = run.get("edits")
    if not e:
        return ""
    return (f" edits={e['edits_applied']}"
            + (" full-write" if e["full_write"] else "")
            + f" retries={e['retries']} anchor-err={e['anchor_errors']}"
            f" kept-lines-refused={e['kept_lines_failures']}"
            f" rewrite-refused={e['rewrite_refusals']}"
            f" format-err={e['format_errors']}")


def _fmt_run(run):
    if not run["ok"]:
        line = f"{run['model']:<22} ERROR {run['error_type']}"
        if run.get("mode") == "edits":
            line += (f"{_fmt_edits(run)} {run['latency_s']:.1f}s"
                     f" ${run['cost_usd']:.4f}")
        return line
    vs = run["vs_stored"]
    return (f"{run['model']:<22} exact={'yes' if vs['exact_match'] else 'no '}"
            f" +{vs['added']} -{vs['removed']} ~{vs['reworded']}"
            f" moved={vs['moved']} checkbox={vs['checkbox_changed']}"
            f" missing-input={run['missing_input_items']}"
            f"{_fmt_edits(run)}"
            f" {run['latency_s']:.1f}s ${run['cost_usd']:.4f}"
            + (" TRUNCATED" if run["truncated"] else ""))


def _pct(part, whole):
    return f"{part}/{whole} ({100.0 * part / whole:.0f}%)" if whole else "-"


def summarize(runs_by_label):
    summary = {}
    for label, runs in runs_by_label.items():
        ok = [r for r in runs if r["ok"]]
        lat = [r["latency_s"] for r in ok]
        entry = {
            "calls": len(runs),
            "compared": len(ok),
            "errors": len(runs) - len(ok),
            "exact_match": sum(r["vs_stored"]["exact_match"] for r in ok),
            "identical_text": sum(r["vs_stored"]["identical_text"]
                                  for r in ok),
            "same_items_any_order": sum(
                r["vs_stored"]["same_items_any_order"] for r in ok),
            "with_added": sum(r["vs_stored"]["added"] > 0 for r in ok),
            "with_removed": sum(r["vs_stored"]["removed"] > 0 for r in ok),
            "with_reworded": sum(r["vs_stored"]["reworded"] > 0 for r in ok),
            "with_moved": sum(r["vs_stored"]["moved"] > 0 for r in ok),
            "with_checkbox_changed": sum(
                r["vs_stored"]["checkbox_changed"] > 0 for r in ok),
            "with_missing_input": sum(r["missing_input_items"] > 0
                                      for r in ok),
            "truncated": sum(r["truncated"] for r in ok),
            "latency_mean_s": round(statistics.mean(lat), 2) if lat else None,
            "latency_median_s": (round(statistics.median(lat), 2)
                                 if lat else None),
            "cost_usd_total": round(sum(r.get("cost_usd") or 0
                                        for r in runs), 6),
            "input_tokens_mean": (round(statistics.mean(
                r["input_tokens"] for r in ok)) if ok else None),
            "output_tokens_mean": (round(statistics.mean(
                r["output_tokens"] for r in ok)) if ok else None),
        }
        vs_rerun = [r for r in ok if r.get("vs_original_rerun")]
        if vs_rerun:
            entry["exact_match_vs_original_rerun"] = sum(
                r["vs_original_rerun"]["exact_match"] for r in vs_rerun)
            entry["compared_vs_original_rerun"] = len(vs_rerun)
        edits = [r["edits"] for r in runs if r.get("edits")]
        if edits:
            entry["edits"] = _summarize_edits(edits)
        summary[label] = entry
    return summary


def _summarize_edits(stats):
    """Totals over the merges by edits of one model (MergeRun.stats())."""
    def merges_with(key):
        return sum(bool(s[key]) for s in stats)

    def times(key):
        return sum(s[key] for s in stats)

    return {
        "merges": len(stats),
        "failed": sum(s["failure"] is not None for s in stats),
        "failed_by_reason": dict(Counter(
            s["failure"] for s in stats if s["failure"] is not None)),
        "with_retry": merges_with("retries"),
        "calls": times("calls"),
        "edits_applied": times("edits_applied"),
        "full_writes": merges_with("full_write"),
        "anchor_errors": times("anchor_errors"),
        "with_anchor_error": merges_with("anchor_errors"),
        "kept_lines_refusals": times("kept_lines_failures"),
        "with_kept_lines_refusal": merges_with("kept_lines_failures"),
        "rewrite_refusals": times("rewrite_refusals"),
        "format_errors": times("format_errors"),
    }


def print_summary(summary, stored_stats, out):
    print("\nSummary (accuracy against the stored merge output)", file=out)
    print(f"  stored outputs: {stored_stats['merges']} merges, models "
          + ", ".join(f"{m} x{n}" for m, n in
                      sorted(stored_stats["models"].items()))
          + f"; with missing input items: "
          f"{_pct(stored_stats['with_missing_input'], stored_stats['merges'])}"
          f"; mean stored cost ${stored_stats['cost_usd_mean']:.4f}",
          file=out)
    for label, s in summary.items():
        n = s["compared"]
        print(f"\n  {label}", file=out)
        print(f"    compared {n} of {s['calls']} calls"
              f" (errors {s['errors']}, truncated {s['truncated']})",
              file=out)
        print(f"    exact match {_pct(s['exact_match'], n)}"
              f" | identical text {_pct(s['identical_text'], n)}"
              f" | same items any order "
              f"{_pct(s['same_items_any_order'], n)}", file=out)
        if "compared_vs_original_rerun" in s:
            print(f"    exact match vs the original model's re-run "
                  f"{_pct(s['exact_match_vs_original_rerun'], s['compared_vs_original_rerun'])}",
                  file=out)
        print(f"    merges with items added {s['with_added']}, removed "
              f"{s['with_removed']}, reworded {s['with_reworded']}, moved "
              f"{s['with_moved']}, checkbox changed "
              f"{s['with_checkbox_changed']}, previous items missing "
              f"{s['with_missing_input']}", file=out)
        if n:
            print(f"    latency mean {s['latency_mean_s']}s, median "
                  f"{s['latency_median_s']}s | tokens mean in "
                  f"{s['input_tokens_mean']}, out {s['output_tokens_mean']}",
                  file=out)
        e = s.get("edits")
        if e:
            m = e["merges"]
            reasons = ", ".join(f"{k} {v}" for k, v in
                                sorted(e["failed_by_reason"].items()))
            print(f"    edits: failed {_pct(e['failed'], m)}"
                  + (f" ({reasons})" if reasons else "")
                  + f" | retried {_pct(e['with_retry'], m)}"
                  f" | {e['calls']} calls, {e['edits_applied']} edits "
                  f"applied, {e['full_writes']} full writes", file=out)
            print(f"    kept-lines check refused {e['kept_lines_refusals']}"
                  f" replies in {e['with_kept_lines_refusal']} merges"
                  f" | anchor errors {e['anchor_errors']} in "
                  f"{e['with_anchor_error']} merges | rewrites refused "
                  f"{e['rewrite_refusals']} | unparseable replies "
                  f"{e['format_errors']}", file=out)
        print(f"    total cost ${s['cost_usd_total']:.4f}", file=out)


# ── the run ──────────────────────────────────────────────────────────────

def _decrypt(row, user_id):
    return _owned(row, user_id).get_content() or ""


def _skipped_line(todo, reason):
    return {"type": "skipped", "todo_id": todo.id,
            "created_at": _iso(todo.created_at), "reason": reason,
            "reason_text": SKIP_REASONS[reason]}


def classify(uid, username, since, limit, out):
    """Link the account's merges (metadata only) and print the counts.
    Returns (window, chosen): every merge output in the window, newest
    first, and the rebuildable ones that will run."""
    versions = load_versions(uid)
    proposals = load_applied_proposals(uid)
    linked = link_merges(
        versions, proposals, load_cost_rows(uid),
        load_last_edit_times(p["id"] for p in proposals),
        load_prompt_rows(uid))
    window = [r for r in linked
              if since is None or r["todo"].created_at >= since]
    usable = [r for r in window if r["skip"] is None]
    print(f"User {username!r} (id {uid}): {len(versions)} todo versions, "
          f"{len(linked)} merge outputs, {len(proposals)} applied "
          "proposals", file=out)
    if since is not None and since < FLOOR:
        print(f"--since is before {FLOOR.date()}: older merges can't be "
              "rebuilt and are counted as skipped", file=out)
    print(f"Merge outputs in the window: {len(window)}; rebuildable: "
          f"{len(usable)}", file=out)
    for reason, n in Counter(
            r["skip"] for r in window if r["skip"]).most_common():
        print(f"  skipped {n:>4}: {SKIP_REASONS[reason]}", file=out)
    return window, usable[:limit]


def rebuild_inputs(rec, uid, default_prompt, prompt_cache,
                   current_prompt=False):
    """Decrypt what one merge needs (one KMS call per row): the proposal,
    the previous todo version, the stored output, a custom prompt."""
    proposal_text = _decrypt(db.session.get(Node, rec["proposal"]["id"]),
                             uid)
    previous = rec["previous"]
    previous_text = (_decrypt(db.session.get(UserTodo, previous.id), uid)
                     if previous is not None else "")
    stored_text = _decrypt(db.session.get(UserTodo, rec["todo"].id), uid)
    prompt_id = rec["prompt_id"]
    if prompt_id is None or current_prompt:
        # --current-prompt: the file prompt for every merge, so a custom
        # prompt row is not decrypted at all.
        merge_prompt = default_prompt
    else:
        if prompt_id not in prompt_cache:
            prompt_cache[prompt_id] = _decrypt(
                db.session.get(UserPrompt, prompt_id), uid)
        merge_prompt = prompt_cache[prompt_id]
    return {"proposal_text": proposal_text, "previous_text": previous_text,
            "stored_text": stored_text, "merge_prompt": merge_prompt}


def run_models(provider, models, original_model, messages, api_keys,
               supported, previous_text=None):
    """Candidates in order, then the stored model when *original_model*
    is given (--rerun-original). With *previous_text* (--current-prompt)
    each is a merge by edits applied to it, else one call that rewrites
    the list."""
    def one(model_id):
        if previous_text is not None:
            return run_model_edits(provider, model_id, messages, api_keys,
                                   previous_text)
        return run_model(provider, model_id, messages, api_keys)

    runs = []
    for model_id in models:
        runs.append(dict(one(model_id), role="candidate"))
    if original_model is not None:
        if original_model in supported:
            original = one(original_model)
        else:
            original = {"model": original_model, "ok": False,
                        "latency_s": 0, "cost_usd": 0,
                        "error_type": "UnknownModel",
                        "error": "model no longer configured"}
        runs.append(dict(original, role="original_rerun"))
    return runs


def score_runs(runs, stored_text, previous_text):
    original = next((r for r in runs if r["role"] == "original_rerun"
                     and r["ok"]), None)
    for result in runs:
        if not result["ok"]:
            continue
        result["vs_stored"] = compare_todos(stored_text, result["text"])
        missing = missing_input_items(previous_text, result["text"])
        result["missing_input_items"] = len(missing)
        result["missing_input_detail"] = missing
        if result["role"] == "candidate" and original is not None:
            result["vs_original_rerun"] = compare_todos(
                original["text"], result["text"])


def _merge_line(rec, inputs, stored_missing, runs, current_prompt=False):
    todo, proposal, cost = rec["todo"], rec["proposal"], rec["cost"]
    return {
        "type": "merge",
        "todo_id": todo.id,
        "created_at": _iso(todo.created_at),
        "proposal_node_id": proposal["id"],
        "proposal_created_at": _iso(proposal["created_at"]),
        "previous_todo_id": rec["previous"].id if rec["previous"] else None,
        "prompt": ("current_file" if current_prompt
                   else "file_default" if rec["prompt_id"] is None
                   else f"user_prompt:{rec['prompt_id']}"),
        "current_prompt": current_prompt,
        # What the stored output was made with (the reference).
        "stored_prompt": ("file_default" if rec["prompt_id"] is None
                          else f"user_prompt:{rec['prompt_id']}"),
        "prompt_sha256": hashlib.sha256(
            inputs["merge_prompt"].encode()).hexdigest(),
        "stored": {
            "model": cost.model_id,
            "cost_row_id": cost.id,
            "input_tokens": cost.input_tokens,
            "output_tokens": cost.output_tokens,
            "cost_usd": _usd(cost.cost_microdollars),
            "truncated": proposal["truncated"],
            "missing_input_items": len(stored_missing),
            "missing_input_detail": stored_missing,
            "text": inputs["stored_text"],
        },
        "inputs": {
            "proposal_text": inputs["proposal_text"],
            "previous_todo_text": inputs["previous_text"],
        },
        "runs": runs,
    }


def compare_chosen(chosen, window, uid, models, rerun_original, provider,
                   fh, out, current_prompt=False):
    """Run and score each chosen merge, one at a time, writing a JSONL
    line per merge as it finishes. Returns (runs_by_label, stored)."""
    from flask import current_app
    from backend.utils.api_keys import get_api_keys_for_usage
    supported = current_app.config.get("SUPPORTED_MODELS", {})
    api_keys = get_api_keys_for_usage(current_app.config, "chat")
    default_prompt = file_default_prompt(check_hash=not current_prompt)
    for rec in window:
        if rec["skip"]:
            fh.write(json.dumps(_skipped_line(rec["todo"], rec["skip"]))
                     + "\n")
    fh.flush()
    runs_by_label, prompt_cache = {}, {}
    stored = {"merges": 0, "models": Counter(), "with_missing_input": 0,
              "costs_usd": []}
    for n, rec in enumerate(chosen, 1):
        todo = rec["todo"]
        inputs = rebuild_inputs(rec, uid, default_prompt, prompt_cache,
                                current_prompt)
        db.session.rollback()   # no open transaction during model calls
        if not inputs["proposal_text"].strip():
            fh.write(json.dumps(_skipped_line(todo, "proposal_empty"))
                     + "\n")
            print(f"[{n}/{len(chosen)}] todo version {todo.id}: skipped, "
                  "empty proposal", file=out)
            continue
        stored_model = rec["cost"].model_id
        stored_missing = missing_input_items(inputs["previous_text"],
                                             inputs["stored_text"])
        stored["merges"] += 1
        stored["models"][stored_model] += 1
        stored["with_missing_input"] += bool(stored_missing)
        stored["costs_usd"].append(_usd(rec["cost"].cost_microdollars))
        print(f"[{n}/{len(chosen)}] todo version {todo.id} "
              f"({todo.created_at:%Y-%m-%d %H:%M}) stored {stored_model}, "
              f"{len(parse_todo(inputs['stored_text']))} items", file=out)

        messages = build_merge_messages(
            inputs["merge_prompt"], inputs["proposal_text"],
            inputs["previous_text"])
        runs = run_models(provider, models,
                          stored_model if rerun_original else None,
                          messages, api_keys, supported,
                          previous_text=(inputs["previous_text"]
                                         if current_prompt else None))
        score_runs(runs, inputs["stored_text"], inputs["previous_text"])
        for result in runs:
            label = (result["model"] if result["role"] == "candidate"
                     else f"{result['model']} (original re-run)")
            runs_by_label.setdefault(label, []).append(result)
            print("    " + _fmt_run(result), file=out)
        line = _merge_line(rec, inputs, stored_missing, runs,
                           current_prompt)
        fh.write(json.dumps(line) + "\n")
        fh.flush()
    costs = stored.pop("costs_usd")
    stored["cost_usd_mean"] = statistics.mean(costs) if costs else 0.0
    stored["models"] = dict(stored["models"])
    return runs_by_label, stored


def edits_cost_microdollars(model_id, stored_input_tokens):
    """Estimated cost of a merge by edits whose past merge sent
    *stored_input_tokens*: (one call, one call and a retry). The retry
    resends the first call's input with the reply and the reason it was
    refused."""
    from backend.utils.cost import calculate_llm_cost_microdollars
    first_input = (stored_input_tokens or 0) + EDITS_EXTRA_INPUT_TOKENS
    once = calculate_llm_cost_microdollars(
        model_id, first_input, EDITS_OUTPUT_TOKENS)
    retry = calculate_llm_cost_microdollars(
        model_id, first_input + EDITS_OUTPUT_TOKENS, EDITS_OUTPUT_TOKENS)
    return once, once + retry


def _estimate_edits(models, chosen, rerun_original, out):
    print("\nEstimated cost of this run, merges by edits (the input tokens "
          f"of these merges + {EDITS_EXTRA_INPUT_TOKENS}, and "
          f"~{EDITS_OUTPUT_TOKENS} output tokens per call for the edits and "
          "the model's reasoning; a refused reply adds one retry call):",
          file=out)

    def line(label, model_of):
        once = retried = 0
        for rec in chosen:
            one, two = edits_cost_microdollars(model_of(rec),
                                               rec["cost"].input_tokens)
            once, retried = once + one, retried + two
        per = once / len(chosen) if chosen else 0
        print(f"  {label:<22} ${_usd(once):.4f} total, "
              f"${_usd(per):.4f} per merge; up to ${_usd(retried):.4f} "
              "if every merge retried", file=out)

    for model_id in models:
        line(model_id, lambda rec, m=model_id: m)
    if rerun_original:
        line("original re-run", lambda rec: rec["cost"].model_id)


def _estimate(models, chosen, rerun_original, out, current_prompt=False):
    from backend.utils.cost import calculate_llm_cost_microdollars
    if current_prompt:
        _estimate_edits(models, chosen, rerun_original, out)
        return
    print("\nEstimated cost of this run (stored token counts of these "
          "merges; another model's tokenizer and reasoning tokens shift "
          "it):", file=out)
    for model_id in models:
        total = sum(calculate_llm_cost_microdollars(
            model_id, rec["cost"].input_tokens or 0,
            rec["cost"].output_tokens or 0) for rec in chosen)
        per = total / len(chosen) if chosen else 0
        print(f"  {model_id:<22} ${_usd(total):.4f} total, "
              f"${_usd(per):.4f} per merge", file=out)
    if rerun_original:
        total = sum(rec["cost"].cost_microdollars or 0 for rec in chosen)
        print(f"  {'original re-run':<22} ${_usd(total):.4f} total "
              "(what the stored merges cost)", file=out)


def run(user_ident, models=None, limit=DEFAULT_LIMIT, since=None,
        rerun_original=False, out_path=None, dry_run=False,
        provider=None, out=None, current_prompt=False):
    """The comparison, inside an app context. Returns the summary dict
    (per model label), or the counts on a dry run."""
    from flask import current_app
    out = out or sys.stdout
    models = list(models or DEFAULT_MODELS)
    if limit < 0:
        raise SystemExit("--limit must be 0 or more")
    unknown = [m for m in models
               if m not in current_app.config.get("SUPPORTED_MODELS", {})]
    if unknown:
        raise SystemExit(f"Unknown model(s): {', '.join(unknown)}")
    if provider is None:
        from backend.llm_providers import LLMProvider as provider

    with refuse_writes():
        user = resolve_user(user_ident)
        # Refuse early if the prompt file moved on (not with
        # --current-prompt: then the current file is what runs).
        prompt_text = file_default_prompt(check_hash=not current_prompt)
        prompt_sha = hashlib.sha256(prompt_text.encode()).hexdigest()
        if current_prompt:
            print(f"prompt: current file {prompt_sha[:8]}, not the one "
                  "these merges used", file=out)
        window, chosen = classify(user.id, user.username, since, limit,
                                  out)
        print(f"Running {len(chosen)} (limit {limit}), newest first, on "
              f"{', '.join(models)}"
              + (" + the stored model" if rerun_original else ""), file=out)
        if dry_run:
            _estimate(models, chosen, rerun_original, out, current_prompt)
            print("\nDry run: nothing decrypted, no model called, no file "
                  "written.", file=out)
            return {"dry_run": True, "chosen": len(chosen),
                    "rebuildable": sum(r["skip"] is None for r in window)}

        confirm_account(user, out)
        out_path = out_path or default_out_path(user.id)
        print(f"Writing {out_path}", file=out)
        with open_private(out_path) as fh:
            fh.write(json.dumps({
                "type": "run", "user_id": user.id, "models": models,
                "rerun_original": rerun_original, "limit": limit,
                "since": _iso(since), "floor": _iso(FLOOR),
                "started_at": _iso(datetime.utcnow()),
                "prompt_file_sha256": (prompt_sha if current_prompt
                                       else PROMPT_FILE_SHA256),
                "current_prompt": current_prompt,
            }) + "\n")
            runs_by_label, stored = compare_chosen(
                chosen, window, user.id, models, rerun_original, provider,
                fh, out, current_prompt)
            summary = summarize(runs_by_label)
            fh.write(json.dumps({"type": "summary", "stored": stored,
                                 "models": summary}) + "\n")
        print_summary(summary, stored, out)
        if current_prompt:
            print(f"\nprompt: current file {prompt_sha[:8]}, not the one "
                  "these merges used", file=out)
        print(f"\nPer-merge results (with the todo texts): {out_path}",
              file=out)
        return summary


def _utc_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected YYYY-MM-DD, got {value!r}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Re-run one account's past todo merges on candidate "
                    "models and compare with the stored outputs (#234). "
                    "Read-only.")
    parser.add_argument(
        "--user", required=True,
        help="your own account: user id or username (required; no "
             "all-users mode)")
    parser.add_argument(
        "--models", nargs="+", default=DEFAULT_MODELS,
        help="candidate model ids from SUPPORTED_MODELS "
             f"(default: {' '.join(DEFAULT_MODELS)})")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help="re-run at most this many merges, newest first "
             f"(default: {DEFAULT_LIMIT})")
    parser.add_argument(
        "--since", type=_utc_date,
        help="only merges on or after this UTC date (YYYY-MM-DD; merges "
             f"before {FLOOR.date()} can't be rebuilt)")
    parser.add_argument(
        "--rerun-original", action="store_true",
        help="also re-run each merge's stored model on the same inputs "
             "(adds one frontier-model call per merge)")
    parser.add_argument(
        "--current-prompt", action="store_true",
        help="run every merge (candidates and --rerun-original) as the "
             "task runs it today: the CURRENT orient_apply_todo.txt, the "
             "model replying with edits, applied and checked by the task's "
             "own function (#234); inputs and the stored reference stay as "
             "they were. Also records edits, retries, anchor errors and "
             "kept-lines check refusals per merge")
    parser.add_argument(
        "--out", help="JSONL path (default: ~/todo-merge-compare-u<id>-"
                      "<UTC time>.jsonl; never overwritten)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="count rebuildable merges and estimate the cost; decrypts "
             "nothing, calls no model, writes no file")
    return parser.parse_args(argv)


def main(argv=None):
    # Before create_app(): with SENTRY_DSN set it initialises Sentry, which
    # reports an unhandled exception (or Ctrl-C) with the local variables
    # of every stack frame, and those hold the decrypted texts.
    # backend/__init__.py loaded .env.production when this module was
    # imported, so removing the variable here is final.
    os.environ.pop("SENTRY_DSN", None)
    args = parse_args(argv)
    from backend import create_app
    app = create_app()
    with app.app_context():
        make_postgres_read_only(db.engine)
        db.session.rollback()   # the next transaction starts read-only
        run(args.user, models=args.models, limit=args.limit,
            since=args.since, rerun_original=args.rerun_original,
            out_path=args.out, dry_run=args.dry_run,
            current_prompt=args.current_prompt)


if __name__ == "__main__":
    main()
