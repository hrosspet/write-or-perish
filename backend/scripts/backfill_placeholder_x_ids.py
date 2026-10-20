"""Give handle-only placeholder accounts their numeric X id.

Before 2026-09-17, admin whitelisting created an account with just the
handle and the owner's first X login claimed it by handle. X logins now
match by X id only, so a placeholder without one cannot be claimed until it
has its id. This finds every account with no login of its own (no X id, no
email; reserved names are listed apart and skipped) and resolves the handle
it was pre-filled from (``prefilled_handle``, else the username):

  1. the newest X API pre-fill dump under <data>/x-api/ whose header names
     this account (``for_user_id``) and this handle — the id was already
     paid for, reuse it (a dump for another handle is reported, not used);
  2. the Community Archive (free, exact username match);
  3. with --x-lookup AND --apply, one paid X API user read, billed to the
     placeholder itself — the read finds its own X id, like its pre-fills.

Dry run by default: no paid calls, nothing written; it prints who each
placeholder would be matched to (archive username and display name, X id,
source) and lists what it could not resolve and what conflicts. --apply
stamps the ids. Two placeholders resolving to one X id are reported as a
conflict in both modes, never stamped twice.

Prod runbook (after the deploy that switched X logins to id matching):

    cd /path/to/write-or-perish && source .env  # or however flask runs there
    python -m backend.scripts.backfill_placeholder_x_ids                # dry run
    # review: every line names the matched account; UNRESOLVED and
    # CONFLICTS list what needs a paid lookup or a hand fix
    python -m backend.scripts.backfill_placeholder_x_ids --apply
    python -m backend.scripts.backfill_placeholder_x_ids --apply --x-lookup
    python -m backend.scripts.backfill_placeholder_x_ids   # "0 placeholder(s) needing an id"

An "already signs in as user N" line means the owner logged in with X
before the backfill and got a fresh empty account; that account has the id
now. Fix by hand: delete the empty account and re-run (renaming it does
not help — the id stays with it). An "is already on user N, a placeholder"
line means two placeholders resolve to one X account, one of them stamped
in an earlier run or by a pre-fill: keep one, delete the other. A "no
sign-in on record but predates the last-seen record" line is an account
from before 2026-08-29 that may be a real early X signup: ask before
deleting anything.

Safe to run while pre-fills run: the write is conditional (only an account
that still has no id is stamped), so neither job overwrites the other.
"""
import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

# A script, not a request: the archive's own timeout, not the whitelist's
# request budget.
LOOKUP_TIMEOUT = 60


def _ids_from_x_dumps(dumps_dir):
    """{for_user_id: [(fetched_at, x_id, username, display_name, path), ...]}
    from the header line of every X API pre-fill dump, newest fetch first
    (by the header's fetched_at, not the file name)."""
    found = {}
    if not dumps_dir:
        return found
    for path in pathlib.Path(dumps_dir).glob("*.jsonl"):
        try:
            with open(path, encoding="utf-8") as f:
                head = json.loads(f.readline() or "{}")
        except (OSError, ValueError):
            continue
        account = head.get("account") or {}
        if head.get("for_user_id") is None or not account.get("id"):
            continue
        fetched = head.get("fetched_at") or datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc).isoformat()
        found.setdefault(int(head["for_user_id"]), []).append(
            (str(fetched), str(account["id"]), account.get("username"),
             account.get("name"), path))
    for dumps in found.values():
        dumps.sort(key=lambda d: d[0], reverse=True)
    return found


def _who(username, display_name):
    return f"@{username} “{display_name}”" if display_name else f"@{username}"


def _run(apply, x_lookup, dumps_dir, out=sys.stdout):
    from sqlalchemy import func, update
    from sqlalchemy.exc import IntegrityError
    from backend.extensions import db
    from backend.models import User, Node
    from backend.utils.activity import sign_in_status
    from backend.utils.reserved_usernames import is_username_reserved
    from backend.utils.x_identity import resolve_x_id, XIdUnresolved

    dumped = _ids_from_x_dumps(dumps_dir)
    rows = (User.query
            .filter(User.twitter_id.is_(None), User.email.is_(None))
            .order_by(User.id).all())
    reserved = [u for u in rows if is_username_reserved(u.username)]
    placeholders = [u for u in rows if not is_username_reserved(u.username)]
    stamped, unresolved, conflicts = 0, [], []
    assigned = {}  # x_id → placeholder matched earlier in this run
    for user in placeholders:
        label = f"user {user.id} @{user.username}"
        handle = user.prefilled_handle or user.username
        if user.prefilled_handle and user.prefilled_handle.lower() != user.username.lower():
            label += f" (pre-filled from @{user.prefilled_handle})"

        dumps = dumped.get(user.id) or []
        if dumps:
            _fetched, x_id, uname, dname, path = dumps[0]
            if (uname or "").lower() != handle.lower():
                conflicts.append(
                    f"{label}: X pre-fill dump {path.name} is for @{uname}, not @{handle} — "
                    "a mistaken pull? not stamped; resolve by hand")
                continue
            source, who = f"x-api dump {path.name}", _who(uname, dname)
        else:
            try:
                r = resolve_x_id(handle, x_lookup=False, timeout=LOOKUP_TIMEOUT)
            except XIdUnresolved as e:
                if e.reason == "not-in-archive" and x_lookup:
                    if not apply:
                        unresolved.append(f"{label}: not in the archive — would need a paid X lookup (--apply)")
                        continue
                    try:
                        # billed to the placeholder: the read finds its own id
                        r = resolve_x_id(handle, x_lookup=True, cost_user_id=user.id,
                                         timeout=LOOKUP_TIMEOUT)
                    except XIdUnresolved as e2:
                        unresolved.append(f"{label}: {e2.message}")
                        continue
                else:
                    unresolved.append(f"{label}: {e.message}"
                                      + ("" if x_lookup or e.reason != "not-in-archive"
                                         else " (--x-lookup to try X)"))
                    continue
            x_id, source, who = r.x_id, r.source, _who(r.username, r.display_name)

        if x_id in assigned:
            other = assigned[x_id]
            conflicts.append(
                f"{label}: X id {x_id} ({source}, {who}) also resolves to user {other.id} "
                f"@{other.username}, {'stamped' if apply else 'matched'} earlier in this run — "
                "two placeholders for one X account; keep one, delete or rename the other, re-run")
            continue
        holder = User.query.filter(User.twitter_id == x_id, User.id != user.id).first()
        if holder is not None:
            n_nodes = db.session.query(func.count(Node.id)).filter(Node.user_id == holder.id).scalar()
            status = sign_in_status(holder)
            if status == "signed-in":
                seen = holder.last_seen_at or holder.accepted_terms_at
                conflicts.append(
                    f"{label}: X id {x_id} ({source}, {who}) already signs in as user {holder.id} "
                    f"@{holder.username} (last seen {seen:%Y-%m-%d}, {n_nodes} nodes) — the owner "
                    "logged in before the backfill and got that account; if it is the empty "
                    "duplicate, delete it (renaming does not help: the id stays) and re-run")
            elif status == "never":
                conflicts.append(
                    f"{label}: X id {x_id} ({source}, {who}) is already on user {holder.id} "
                    f"@{holder.username}, a placeholder nobody has signed into ({n_nodes} nodes; "
                    "stamped in an earlier run or by a pre-fill) — two placeholders for one X "
                    "account; keep one, delete the other (renaming does not help: the id stays)")
            else:
                created = f"{holder.created_at:%Y-%m-%d}" if holder.created_at else "?"
                conflicts.append(
                    f"{label}: X id {x_id} ({source}, {who}) is already on user {holder.id} "
                    f"@{holder.username}, which has no sign-in on record but predates the "
                    f"last-seen record (created {created}, {n_nodes} nodes): an early X signup "
                    "who never accepted the terms looks the same as a stamped placeholder — "
                    "check with the person before deleting anything; not stamped")
            continue
        assigned[x_id] = user
        print(f"{label}: {who} → X id {x_id} ({source})" + ("" if apply else " [dry run]"),
              file=out)
        if apply:
            # Conditional: a pre-fill that stamped this account while the
            # lookup ran must not be overwritten.
            try:
                rows = db.session.execute(
                    update(User).where(User.id == user.id, User.twitter_id.is_(None))
                    .values(twitter_id=x_id)).rowcount
                db.session.commit()
            except IntegrityError:  # another account took the id meanwhile
                db.session.rollback()
                rows = 0
            if rows == 0:
                db.session.refresh(user)
                now = (f"now X id {user.twitter_id}" if user.twitter_id
                       else "the id now belongs to another account")
                conflicts.append(f"{label}: changed by another job while this ran ({now}); "
                                 "not stamped — re-run")
                continue
            stamped += 1

    print(file=out)
    print(f"{len(placeholders)} placeholder(s) needing an id: {stamped} stamped, "
          f"{len(unresolved)} unresolved, {len(conflicts)} conflicting"
          + ("" if apply else " — dry run, nothing written"), file=out)
    if reserved:
        print(f"{len(reserved)} reserved-name account(s) skipped (system accounts, never claimable): "
              + ", ".join("@" + u.username for u in reserved), file=out)
    if unresolved:
        print("\nUNRESOLVED (still not claimable):", file=out)
        for line in unresolved:
            print("  " + line, file=out)
    if conflicts:
        print("\nCONFLICTS (fix by hand):", file=out)
        for line in conflicts:
            print("  " + line, file=out)
    return {"stamped": stamped, "unresolved": unresolved, "conflicts": conflicts,
            "reserved": [u.username for u in reserved], "placeholders": len(placeholders)}


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--apply", action="store_true", help="write the ids (default: dry run)")
    p.add_argument("--x-lookup", action="store_true",
                   help="with --apply: also try the X API for handles the archive lacks "
                        "(one paid user read each, billed to the placeholder)")
    args = p.parse_args()
    from backend import create_app
    app = create_app()
    with app.app_context():
        from backend.utils.twitter_archive import STASH_ROOT
        _run(args.apply, args.x_lookup, STASH_ROOT.parent / "x-api")


if __name__ == "__main__":
    main()
