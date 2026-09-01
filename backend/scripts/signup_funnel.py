"""Signup funnel — where did each account stall, and when was it last seen?

Read-only, METADATA ONLY: pure SQL aggregates over timestamps, counts and
status columns. Never touches node/profile content (no decryption, no KMS
calls), so it is safe to run on the 4GB prod VM.

Usage (from the project root, conda env `write-or-perish` active):

    python backend/scripts/signup_funnel.py               # full report
    python backend/scripts/signup_funnel.py --anon        # user ids only
    python backend/scripts/signup_funnel.py --churn-days 45
    python backend/scripts/signup_funnel.py --csv /tmp/funnel.csv

Buckets (lifetime, humans only):
    A  never accepted terms        — never got past the terms modal (and no
                                     nodes; pre-modal accounts with nodes fall through)
    B  terms, zero own nodes       — saw the blank page and left
    C  1-3 own nodes               — first session(s) didn't stick
    D  4+ nodes, gone              — used it, then no sign of life > churn-days
    E  active                      — seen within churn-days

"own nodes" = node_type='user', not deleted, not imported (origin IS NULL
AND source_key IS NULL; origin catches split-import segments source_key
used to miss).
"last seen" = max(own node, user-initiated API call (conversation /
transcription / tts / embedding_query — automation cost rows don't count),
notification read, changelog read,
last user-requested magic link, user.last_seen_at, artifact view). Still a
floor for history: last_seen_at only exists since the 2026-08-29 deploy
(#272) and artifact_view since 2026-09-01, so older visits that left no
other trace are invisible.
The magic-link columns keep the latest minted link (verify never clears
them) and welcome emails mint 30-day links at approval time — an admin
action, not a user one — so that term is dropped when its mint time
matches approved_at or would land in the future.
"returned" = any of those signals on a UTC day after the signup day.
Spam-flagged accounts are excluded along with system accounts.

All dates are UTC (created_at is naive UTC).
"""
import argparse
import csv
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import text  # noqa: E402

from backend import create_app  # noqa: E402
from backend.extensions import db  # noqa: E402

FIRST_REPLY_WINDOW = timedelta(minutes=15)
# activate_and_welcome mints its magic link with this expiry (see
# backend/routes/admin.py) — used to recognize welcome links.
WELCOME_LINK_TTL = timedelta(days=30)


def table(headers, rows, aligns=None):
    cols = len(headers)
    aligns = aligns or (["<"] + [">"] * (cols - 1))
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join("{:{a}{w}}".format(h, a="<", w=widths[i])
                     for i, h in enumerate(headers))
    out = [line, "-" * len(line)]
    for r in rows:
        out.append("  ".join(
            "{:{a}{w}}".format(str(c), a=aligns[i], w=widths[i])
            for i, c in enumerate(r)))
    return "\n".join(out)


def section(title):
    print("\n\n" + "=" * 78)
    print(title)
    print("=" * 78)


def d(dt):
    return "-" if dt is None else dt.strftime("%Y-%m-%d")


def days(a, b):
    """Whole days from a to b, '-' if either is missing."""
    if a is None or b is None:
        return "-"
    return (b - a).days


def q(sql, **params):
    return db.session.execute(text(sql), params).mappings().all()


def main():
    ap = argparse.ArgumentParser(description="Signup funnel report")
    ap.add_argument("--anon", action="store_true",
                    help="show user ids only, no usernames")
    ap.add_argument("--churn-days", type=int, default=30,
                    help="no sign of life for this many days = churned "
                         "(default 30)")
    ap.add_argument("--csv", default=None,
                    help="also write the per-user table to this CSV path")
    args = ap.parse_args()

    app = create_app()
    with app.app_context():
        now = datetime.utcnow()
        magic_ttl = timedelta(seconds=int(
            app.config.get("MAGIC_LINK_EXPIRY_SECONDS") or 900))
        churn_cutoff = now - timedelta(days=args.churn_days)

        sys_ids = set(r[0] for r in db.session.execute(text("""
            SELECT DISTINCT user_id FROM node
            WHERE node_type = 'llm' AND user_id IS NOT NULL
        """)).all())
        sys_ids |= set(r[0] for r in db.session.execute(text("""
            SELECT id FROM "user" WHERE username = 'loore-polls'
        """)).all())

        users = q("""
            SELECT id, username, twitter_id IS NOT NULL AS via_twitter,
                   email IS NOT NULL AS has_email,
                   created_at, accepted_terms_at, approved, plan,
                   deactivated_at, magic_link_expires_at, is_admin,
                   approved_at, last_seen_at, last_seen_path, spam,
                   prefill_consent, prefilled_handle
            FROM "user" ORDER BY created_at
        """)
        spam_ids = set(u["id"] for u in users if u["spam"])

        # ---- per-user aggregates (one query each, GROUP BY user) --------
        own = {r["user_id"]: r for r in q("""
            SELECT user_id,
                   COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE audio_original_url IS NOT NULL) AS n_voice,
                   COUNT(*) FILTER (WHERE transcription_status = 'failed') AS n_tx_failed,
                   COUNT(*) FILTER (WHERE llm_task_status = 'failed') AS n_llm_failed,
                   COUNT(DISTINCT created_at::date) AS active_days,
                   MIN(created_at) AS first_at, MAX(created_at) AS last_at,
                   SUM(token_count) AS tokens
            FROM node
            WHERE node_type = 'user' AND deleted_at IS NULL
              AND source_key IS NULL AND origin IS NULL
            GROUP BY user_id
        """)}
        imported = {r["user_id"]: r for r in q("""
            SELECT user_id, COUNT(*) AS n, MIN(created_at) AS first_at
            FROM node
            WHERE node_type = 'user' AND deleted_at IS NULL
              AND (source_key IS NOT NULL OR origin IS NOT NULL)
            GROUP BY user_id
        """)}
        first_node_voice = {r["user_id"]: r["is_voice"] for r in q("""
            SELECT DISTINCT ON (user_id) user_id,
                   audio_original_url IS NOT NULL AS is_voice
            FROM node
            WHERE node_type = 'user' AND deleted_at IS NULL
              AND source_key IS NULL AND origin IS NULL
            ORDER BY user_id, created_at
        """)}
        # last_at / active_days count only user-initiated request types —
        # everything else in api_cost_log is automation (pre-fill, batch
        # profiles, embeddings, summaries) and says nothing about the
        # person being there (mirrors utils/activity.py). spend stays total.
        calls = {r["user_id"]: r for r in q("""
            SELECT user_id,
                   COUNT(*) FILTER (WHERE request_type = 'conversation') AS n_conv,
                   MIN(created_at) FILTER (WHERE request_type = 'conversation') AS first_conv,
                   MAX(created_at) FILTER (WHERE request_type IN
                       ('conversation', 'transcription', 'tts',
                        'embedding_query')) AS last_at,
                   COUNT(DISTINCT created_at::date) FILTER (WHERE request_type IN
                       ('conversation', 'transcription', 'tts',
                        'embedding_query')) AS active_days,
                   SUM(cost_microdollars) AS cost
            FROM api_cost_log GROUP BY user_id
        """)}
        profiles = {r["user_id"]: r for r in q("""
            SELECT user_id, COUNT(*) AS n, MIN(created_at) AS first_at
            FROM user_profile GROUP BY user_id
        """)}
        notif = {r["user_id"]: r["last_read"] for r in q("""
            SELECT user_id, MAX(read_at) AS last_read
            FROM user_notification WHERE read_at IS NOT NULL GROUP BY user_id
        """)}
        changelog = {r["user_id"]: r["last_read"] for r in q("""
            SELECT user_id, MAX(updated_at) AS last_read
            FROM changelog_read_state GROUP BY user_id
        """)}
        x_seeded = set(r["user_id"] for r in q("""
            SELECT DISTINCT user_id FROM api_cost_log
            WHERE request_type = 'x_prefill'
        """))
        # artifact_view exists since 2026-09-01 — older visits left no rows
        # (and the table itself may be missing before that deploy).
        art_views = defaultdict(dict)   # uid -> {kind: (n, last_at)}
        art_last = {}                   # uid -> most recent view
        try:
            view_rows = q("""
                SELECT user_id, kind, COUNT(*) AS n, MAX(viewed_at) AS last_at
                FROM artifact_view GROUP BY user_id, kind
            """)
        except Exception:
            db.session.rollback()
            view_rows = []
            print("note: artifact_view table not found — view columns "
                  "will be empty", file=sys.stderr)
        for r in view_rows:
            art_views[r["user_id"]][r["kind"]] = (r["n"], r["last_at"])
            prev = art_last.get(r["user_id"])
            if prev is None or r["last_at"] > prev:
                art_last[r["user_id"]] = r["last_at"]
        active_days = defaultdict(set)
        for r in q("""
            SELECT user_id, created_at::date AS day FROM node
            WHERE node_type = 'user' AND deleted_at IS NULL
              AND source_key IS NULL AND origin IS NULL
            UNION
            SELECT user_id, created_at::date FROM api_cost_log
            WHERE request_type IN ('conversation', 'transcription', 'tts',
                                   'embedding_query')
        """):
            active_days[r["user_id"]].add(r["day"])

        # ---- classify --------------------------------------------------
        rows = []
        for u in users:
            uid = u["id"]
            if uid in sys_ids or uid in spam_ids:
                continue
            o = own.get(uid)
            c = calls.get(uid)
            n_own = o["n"] if o else 0
            magic_seen = None
            if u["magic_link_expires_at"]:
                t_login = u["magic_link_expires_at"] - magic_ttl
                t_welcome = u["magic_link_expires_at"] - WELCOME_LINK_TTL
                is_welcome = u["approved_at"] is not None and abs(
                    (t_welcome - u["approved_at"]).total_seconds()) < 3600
                if not is_welcome and t_login <= now:
                    magic_seen = t_login
            last_seen = max(filter(None, [
                o["last_at"] if o else None,
                c["last_at"] if c else None,
                notif.get(uid), changelog.get(uid),
                magic_seen,
                u["last_seen_at"], art_last.get(uid),
                u["created_at"],
            ]))
            if u["accepted_terms_at"] is None and n_own == 0:
                bucket = "A"
            elif n_own == 0:
                bucket = "B"
            elif n_own <= 3:
                bucket = "C"
            elif last_seen < churn_cutoff:
                bucket = "D"
            else:
                bucket = "E"

            first_reply = None
            if o and c and c["first_conv"]:
                first_reply = c["first_conv"] - o["first_at"]
            views = art_views.get(uid, {})
            rows.append({
                "bucket": bucket,
                "id": uid,
                "user": str(uid) if args.anon else "{} ({})".format(
                    u["username"], uid),
                "signup": u["created_at"],
                "auth": "twitter" if u["via_twitter"] else "email",
                "has_email": u["has_email"],
                "plan": u["plan"],
                "approved": u["approved"],
                "approved_at": u["approved_at"],
                "seed": ("x" if uid in x_seeded
                         else "ca" if u["prefilled_handle"] else "-"),
                "consent": {"yes": "y", "no": "n"}.get(
                    u["prefill_consent"], "-"),
                "returned": last_seen.date() > u["created_at"].date(),
                "artifact_views": sum(n for n, _ in views.values()),
                "views_by_kind": " ".join(
                    "{}x{}".format(k, n) for k, (n, _) in sorted(
                        views.items(), key=lambda kv: kv[1][1], reverse=True)),
                "last_seen_path": u["last_seen_path"] or "",
                # toggle re-approval never clears deactivated_at, so the
                # tombstone alone is stale history — require both.
                "deactivated": (u["deactivated_at"] is not None
                                and not u["approved"]),
                "terms_after_d": days(u["created_at"], u["accepted_terms_at"]),
                "first_node_after_d": days(u["created_at"],
                                           o["first_at"] if o else None),
                "own_nodes": n_own,
                "voice_nodes": o["n_voice"] if o else 0,
                "first_node_voice": first_node_voice.get(uid),
                "imported_nodes": imported[uid]["n"] if uid in imported else 0,
                "tokens": o["tokens"] if o else 0,
                "llm_replies": c["n_conv"] if c else 0,
                "first_reply_min": ("-" if first_reply is None
                                    else round(first_reply.total_seconds() / 60)),
                "tx_failed": o["n_tx_failed"] if o else 0,
                "llm_failed": o["n_llm_failed"] if o else 0,
                "profiles": profiles[uid]["n"] if uid in profiles else 0,
                "profile_after_d": days(u["created_at"],
                                        profiles[uid]["first_at"]
                                        if uid in profiles else None),
                "active_days": len(active_days.get(uid, ())),
                "span_d": days(o["first_at"], o["last_at"]) if o else "-",
                "last_seen": last_seen,
                "silent_d": (now - last_seen).days,
                "spend": (c["cost"] or 0) / 1e6 if c else 0.0,
            })

        # ---- 1. funnel ---------------------------------------------------
        section("1. FUNNEL (humans only; {} system + {} spam accounts "
                "excluded)".format(len(sys_ids), len(spam_ids)))
        n = len(rows)
        steps = [
            ("signed up", n),
            ("accepted terms", sum(1 for r in rows if r["bucket"] != "A")),
            ("returned a later day", sum(1 for r in rows if r["returned"])),
            ("viewed an artifact*", sum(1 for r in rows
                                        if r["artifact_views"] >= 1)),
            ("wrote >= 1 own node", sum(1 for r in rows if r["own_nodes"] >= 1)),
            ("wrote >= 4 own nodes", sum(1 for r in rows if r["own_nodes"] >= 4)),
            ("got >= 1 LLM reply", sum(1 for r in rows if r["llm_replies"] >= 1)),
            ("has a profile", sum(1 for r in rows if r["profiles"] >= 1)),
            ("active in last {}d".format(args.churn_days),
             sum(1 for r in rows if r["bucket"] == "E")),
        ]
        print(table(["step", "users", "of signups"],
                    [[s, c, "{:.0f}%".format(100 * c / n if n else 0)]
                     for s, c in steps]))
        print("* artifact views tracked only since 2026-09-01; 'returned' "
              "is a floor\n  before the 2026-08-29 deploy (no last_seen "
              "tracking).")

        print()
        labels = {
            "A": "A never accepted terms",
            "B": "B terms, zero own nodes",
            "C": "C 1-3 own nodes",
            "D": "D 4+ nodes, silent > {}d".format(args.churn_days),
            "E": "E active",
        }
        bc = Counter(r["bucket"] for r in rows)
        print(table(["bucket", "users", "twitter", "email", "returned",
                     "deactivated"], [
            [labels[b], bc[b],
             sum(1 for r in rows if r["bucket"] == b and r["auth"] == "twitter"),
             sum(1 for r in rows if r["bucket"] == b and r["auth"] == "email"),
             sum(1 for r in rows if r["bucket"] == b and r["returned"]),
             sum(1 for r in rows if r["bucket"] == b and r["deactivated"])]
            for b in "ABCDE"]))

        # ---- 2. signup cohorts ------------------------------------------
        section("2. SIGNUP MONTH x BUCKET")
        months = sorted(set(r["signup"].strftime("%Y-%m") for r in rows))
        print(table(["month", "signups"] + list("ABCDE"), [
            [m, sum(1 for r in rows if r["signup"].strftime("%Y-%m") == m)]
            + [sum(1 for r in rows
                   if r["signup"].strftime("%Y-%m") == m and r["bucket"] == b)
               for b in "ABCDE"]
            for m in months]))

        # ---- 3. per-user -------------------------------------------------
        section("3. PER USER (sorted by bucket, then signup)")
        print("terms/1st node/profile = days after signup. 1st reply = minutes\n"
              "from first own node to first LLM call. silent = days since\n"
              "last sign of life. voice1 = first node was voice.\n"
              "seed = pre-filled from x/ca. cons = pre-fill consent. ret =\n"
              "returned a later day. views = artifact views (since 2026-09-01).")
        hdr = ["b", "user", "signup", "auth", "appr", "seed", "cons", "ret",
               "views", "terms", "1st node",
               "own", "voice", "voice1", "imp", "replies", "1st reply",
               "txF", "llmF", "prof", "prof d", "days", "span", "last seen",
               "silent", "spend"]
        out = []
        for r in sorted(rows, key=lambda r: (r["bucket"], r["signup"])):
            out.append([
                r["bucket"], r["user"], d(r["signup"]), r["auth"],
                "y" if r["approved"] else "n", r["seed"], r["consent"],
                "y" if r["returned"] else "-",
                r["artifact_views"] or "-", r["terms_after_d"],
                r["first_node_after_d"], r["own_nodes"], r["voice_nodes"],
                {True: "y", False: "n"}.get(r["first_node_voice"], "-"),
                r["imported_nodes"], r["llm_replies"], r["first_reply_min"],
                r["tx_failed"], r["llm_failed"], r["profiles"],
                r["profile_after_d"], r["active_days"], r["span_d"],
                d(r["last_seen"]), r["silent_d"],
                "${:.2f}".format(r["spend"]),
            ])
        print(table(hdr, out))

        # ---- 4. first-session diagnostics --------------------------------
        section("4. FIRST SESSION — did the first node get a reply? (buckets C+D+E)")
        tried = [r for r in rows if r["own_nodes"] >= 1]

        def share(pred, pool):
            k = sum(1 for r in pool if pred(r))
            return "{}/{} ({:.0f}%)".format(k, len(pool),
                                            100 * k / len(pool) if pool else 0)
        for b in "CDE":
            pool = [r for r in tried if r["bucket"] == b]
            if not pool:
                continue
            print("\n{}:".format(labels[b]))
            print("  first node was voice        : " + share(
                lambda r: r["first_node_voice"] is True, pool))
            print("  any LLM reply ever          : " + share(
                lambda r: r["llm_replies"] >= 1, pool))
            print("  first reply within {:>2} min   : ".format(
                int(FIRST_REPLY_WINDOW.total_seconds() // 60)) + share(
                lambda r: r["first_reply_min"] != "-"
                and r["first_reply_min"] <= FIRST_REPLY_WINDOW.total_seconds() / 60,
                pool))
            print("  any failed transcription    : " + share(
                lambda r: r["tx_failed"] > 0, pool))
            print("  any failed LLM task         : " + share(
                lambda r: r["llm_failed"] > 0, pool))
            print("  ever got a profile          : " + share(
                lambda r: r["profiles"] >= 1, pool))
            print("  first node same day as signup: " + share(
                lambda r: r["first_node_after_d"] == 0, pool))

        # ---- 5. pre-fill cohort ------------------------------------------
        section("5. PRE-FILL COHORT (seeded from X/CA, or asked for consent)")
        cohort = [r for r in rows if r["seed"] != "-" or r["consent"] != "-"]
        if cohort:
            print("appr d = days signup -> approval. ret w = returned after\n"
                  "the approval (welcome) day. email = welcome email possible.\n"
                  "views/path tracked since 2026-09-01 / 2026-08-29 only.")
            crows = []
            for r in sorted(cohort, key=lambda r: r["signup"], reverse=True):
                ret_w = "-"
                if r["approved_at"]:
                    ret_w = ("y" if r["last_seen"].date()
                             > r["approved_at"].date() else "n")
                crows.append([
                    r["bucket"], r["user"], r["seed"], r["consent"],
                    "y" if r["has_email"] else "-", d(r["signup"]),
                    days(r["signup"], r["approved_at"]), ret_w,
                    r["views_by_kind"] or "-", r["own_nodes"],
                    r["llm_replies"], r["voice_nodes"],
                    r["last_seen_path"][:28] or "-", r["silent_d"],
                ])
            print(table(["b", "user", "seed", "cons", "email", "signup",
                         "appr d", "ret w", "artifact views", "own",
                         "replies", "voice", "last path", "silent d"], crows,
                        aligns=["<", "<", "<", "<", "<", "<", ">", "<", "<",
                                ">", ">", ">", "<", ">"]))
            print("\n{} in cohort; {} returned after welcome; {} viewed an "
                  "artifact; {} wrote.".format(
                      len(cohort),
                      sum(1 for c in crows if c[7] == "y"),
                      sum(1 for r in cohort if r["artifact_views"] >= 1),
                      sum(1 for r in cohort if r["own_nodes"] >= 1)))
        else:
            print("(none)")

        # ---- 6. re-engagement candidates ---------------------------------
        section("6. RE-ENGAGEMENT CANDIDATES (B, C, D; not deactivated)")
        cands = [r for r in rows if r["bucket"] in "BCD" and not r["deactivated"]]
        print(table(["b", "user", "auth", "signup", "own", "silent d", "twitter"], [
            [r["bucket"], r["user"], r["auth"], d(r["signup"]), r["own_nodes"],
             r["silent_d"], "y" if r["auth"] == "twitter" else "-"]
            for r in sorted(cands, key=lambda r: (r["bucket"], r["silent_d"]))]))
        print("\n{} candidates; {} signed up via Twitter (handle on file for "
              "pre-loading).".format(
                  len(cands), sum(1 for r in cands if r["auth"] == "twitter")))

        if args.csv:
            with open(args.csv, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                for r in rows:
                    w.writerow(r)
            print("\nCSV written to {}".format(args.csv))


if __name__ == "__main__":
    main()
