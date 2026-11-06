#!/usr/bin/env python3
"""Record content-policy blocks the old profile poller counted as provider
failures (2026-11-04).

Before #597, the profile poller never read an OpenAI batch's error file. A
window, group or final that the provider's content-policy filter blocked
(HTTP 400, e.g. ``bio_policy``) came back without a result, so the window
pipeline saved it as the outcome "missing", resubmitted it up to
PROFILE_VERSION_PROVIDER_ATTEMPTS times, and then let it wait out a 24 h
provider stop. Under the new code that stop would expire and the window
would be sent again. No block row exists for these items.

This script finds them and records each block the way the new collect path
does (``profile_windows.collect``):

- a blocked window's range is remembered for good
  (``profile_windows.record_blocked_range``: every later window plan cuts
  around it), a window_skip row takes its place (only for its newest
  attempt, so a skip row never lands in an older set) and a $0 block
  record stops nothing (``profile_windows.skip_blocked_window``); the
  account's set is then merged without it on the next pass;
- a blocked group or final stops the account's build
  (``profile_windows.stop_for_blocked_merge``);
- every such item's outcome becomes "blocked" (with ``policy_code``), so the
  window is never sent again and no provider stop counts it.

Accounts whose AI usage no longer allows AI get no skip row (the range,
the item's outcome and the block record only). Nor does a window whose
other attempt in the same set was saved (the old poller resent it and it
went through): that version and the set stay as they are, and only the
range is recorded, so no later rebuild sends that writing again. It scans the ProfileBatchJob rows
submitted in the last --days whose window-set items have a provider-failure
outcome ("missing" or "error") on an OpenAI batch, retrieves each batch and
reads its error file. An item whose error code is not a policy code (an
ordinary 500, say) is left as it is.

Idempotent: an item already "blocked" is not looked at again, and a skip
row is never written twice for the same range. No Sentry events: the
blocks are old, and the script prints them. Prints ids, dates and codes
only: no prompt, output or provider message.

Dry run by default: it reads the batches and their error files from the
provider and writes nothing. --apply writes. The next seed pass (the
hourly seeder, where beat runs) then picks the accounts up, as their work
is held for a later pass, and a set whose windows are all in sends its
merges.

    python backend/scripts/backfill_policy_blocks.py                # dry run, last 14 days
    python backend/scripts/backfill_policy_blocks.py --apply
    python backend/scripts/backfill_policy_blocks.py --user-id 42 --days 30
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# How far back the scan reaches by default: twice the window pipeline's
# attempt history (ATTEMPT_LOOKBACK, 7 days), so every attempt the new code
# could still count is covered.
DEFAULT_DAYS = 14


def openai_client():
    """An OpenAI client on the profile batches' key (the batch override,
    as the poller uses)."""
    from flask import current_app
    from openai import OpenAI
    from backend.utils.api_keys import get_api_keys_for_usage
    from backend.utils.llm_batch import apply_batch_key_override
    cfg = current_app.config
    keys = apply_batch_key_override(get_api_keys_for_usage(cfg, "chat"), cfg)
    return OpenAI(api_key=keys["openai"])


def find_blocks(days=DEFAULT_DAYS, user_id=None, client_for=openai_client,
                now=None, out=print):
    """[(job, item, code)]: the window-set items with a provider-failure
    outcome whose OpenAI batch's error file names a policy code, oldest
    job first."""
    from backend.models import ProfileBatchJob
    from backend.tasks import profile_windows as pw
    from backend.utils.llm_batch import is_policy_block, openai_batch_errors
    since = (now or datetime.utcnow()) - timedelta(days=days)
    jobs = (ProfileBatchJob.query
            .filter(ProfileBatchJob.submitted_at >= since,
                    ProfileBatchJob.status != "pending")
            .order_by(ProfileBatchJob.submitted_at, ProfileBatchJob.id).all())
    found, client = [], None
    for job in jobs:
        if not (job.provider_key or "").startswith("openai:"):
            continue
        if not job.batch_id or job.batch_id == "unsubmitted":
            continue   # refused at submission: nothing reached the model
        items = [i for i in job.items or []
                 if i.get("kind") in pw.ITEM_KINDS
                 and i.get("outcome") in pw.PROVIDER_FAILURES
                 and (user_id is None or i.get("user_id") == user_id)]
        if not items:
            continue
        client = client or client_for()
        try:
            batch = client.batches.retrieve(job.batch_id)
            errors = openai_batch_errors(client, job.batch_id, batch)
        except Exception as e:  # noqa: BLE001 — reported, the scan goes on
            out(f"job {job.id} batch {job.batch_id}: not readable "
                f"({type(e).__name__}); left as is")
            continue
        for item in items:
            err = errors.get(item.get("custom_id"))
            if err is not None and is_policy_block(err[1]):
                found.append((job, item, err[1]))
    return found


def _saved_windows(user_ids):
    """{(user_id, set, start)} of the windows an attempt saved: a job
    item with the outcome "saved", or a window row of the set at that
    start. The old poller resent a blocked window, and a later attempt
    could go through."""
    from backend.models import ProfileBatchJob, UserProfile
    saved = set()
    for job in ProfileBatchJob.query.filter(
            ProfileBatchJob.status != "pending").all():
        for i in job.items or []:
            if (i.get("kind") == "window" and i.get("outcome") == "saved"
                    and i.get("user_id") in user_ids):
                saved.add((i["user_id"], i.get("set"), i.get("start")))
    rows = (UserProfile.query
            .with_entities(UserProfile.user_id, UserProfile.window_set,
                           UserProfile.covers_start)
            .filter(UserProfile.user_id.in_(user_ids),
                    UserProfile.generation_type == "window").all())
    for uid, set_id, start in rows:
        saved.add((uid, set_id, start.isoformat() if start else None))
    return saved


def record_blocks(found, apply=False, out=print):
    """Print each block and, with ``apply``, record it as the new collect
    path does. Returns the number of items recorded (or that would be).

    A window whose other attempt in the same set was saved (the old
    poller resent it and it went through, before or after the blocked
    attempt) keeps that version: no skip row, the set untouched. Its
    blocked item still gets the outcome "blocked" and its range is
    recorded, so no later rebuild sends that writing automatically."""
    from backend.extensions import db
    from backend.models import User
    from backend.tasks import profile_windows as pw
    from backend.utils import refusal_backoff
    from backend.utils.privacy import account_allows_ai

    def key(item):
        return (item["user_id"], item.get("set"), item.get("start"))

    saved = _saved_windows({item["user_id"] for _j, item, _c in found})
    # The newest blocked attempt of each window gets the skip row.
    newest = {}
    for job, item, _code in found:
        if item.get("kind") == "window":
            newest[key(item)] = item.get("custom_id")
    by_job, users = {}, {}
    for job, item, code in found:
        user = users.get(item["user_id"]) or db.session.get(
            User, item["user_id"])
        users[item["user_id"]] = user
        kind = item.get("kind")
        allowed = user is not None and account_allows_ai(user)
        was_saved = kind == "window" and key(item) in saved
        skip = (kind == "window" and allowed and not was_saved
                and newest.get(key(item)) == item.get("custom_id"))
        out(f"job {job.id} batch {job.batch_id} user {item['user_id']} "
            f"{kind} {item.get('start') or 'start'}..{item.get('cutoff')} "
            f"{code} -> blocked"
            + (" + range" if kind == "window" else "")
            + (" + skip row" if skip else "")
            + (" (another attempt was saved: no skip row)"
               if was_saved else "")
            + ("" if allowed else " (AI usage does not allow AI: no skip row)")
            + ("" if kind == "window" else " + account stopped"))
        by_job.setdefault(job.id, (job, {}))[1][item["custom_id"]] = code
        if not apply or user is None:
            continue
        if kind == "window":
            if skip:
                # The range record, the skip row (unless the set was
                # superseded) and the $0 record.
                pw.skip_blocked_window(user, item, code, report=False)
            else:
                pw.record_blocked_range(
                    user.id, *pw.blocked_range_of(item), code,
                    item.get("model_id"))
                refusal_backoff.record_policy_block(
                    user.id, pw.WINDOW_BLOCK_REQUEST_TYPE, item.get("model_id"),
                    code, "profile window", report=False)
            if skip and not (user.profile_seed_error or "").startswith(
                    pw.POLICY_STOP_NOTE):
                user.profile_seed_error = pw._skip_note(item, code)
        else:
            pw.stop_for_blocked_merge(user, item, code, report=False)
            user.profile_seed_error = pw._policy_note(item, code)
        db.session.commit()
    if apply:
        for job, codes in by_job.values():
            job.items = [dict(i, outcome=pw.POLICY_BLOCKED,
                              policy_code=codes[i["custom_id"]])
                         if i.get("custom_id") in codes else i
                         for i in job.items or []]
        db.session.commit()
    return len(found)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"jobs submitted this many days back (default "
                         f"{DEFAULT_DAYS})")
    ap.add_argument("--user-id", type=int, default=None,
                    help="only this user's items")
    ap.add_argument("--apply", action="store_true",
                    help="record the blocks (default: dry run)")
    args = ap.parse_args()

    from backend import create_app
    app = create_app()
    with app.app_context():
        found = find_blocks(days=args.days, user_id=args.user_id)
        n = record_blocks(found, apply=args.apply)
        accounts = len({item["user_id"] for _j, item, _c in found})
        print(f"{n} blocked item(s) on {accounts} account(s) "
              + ("recorded" if args.apply else "found (dry run; --apply "
                                               "records them)"))


if __name__ == "__main__":
    main()
