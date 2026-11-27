"""The first cohort (experiment, dark behind COHORT_V1).

A founder invites a few people in person. Each gets a letter at a link
that needs no sign-in, signs in with X, is interviewed by Loore in a
private thread of their own, and may send the founder a report drafted
from it: their own node, shared with the founder alone through Circles.
After a call the founder admits them; Loore then keeps, for every member
and the founder, a circle holding all the other members, so "share with
the group" is a normal Circles share. The story is in
docs/experiments/FIRST-COHORT.md, the build in FIRST-COHORT-SPEC.md, the
routes in FIRST-COHORT-API.md.

What the founder gets is states and timestamps (invitation_state). No
code here gives anyone but the author a path to the interview or to a
report that was not sent: the report reaches the founder only through
the Circles audience the person sets by pressing Send.

This module holds what the routes and the draft task share: the flag,
the two access hooks (Circles for cohort people, the free period), the
states, preparing an invited account, the interview's prompt and its
transcript, the circles Loore keeps, and the group page's entries. Ids,
usernames and timestamps, except where a function says it reads text.
"""
import logging
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import event, func

from backend.extensions import db
from backend.models import (
    Circle, CircleMember, Cohort, CohortInvitation, Node, NodeCircle, User,
    UserPrompt,
)

logger = logging.getLogger(__name__)

INTERVIEW_KEY = "cohort_interview"
REPORT_KEY = "cohort_report"
INTRO_KEY = "cohort_intro"

# ── Heuristics (named in docs/experiments/FIRST-COHORT-PR.md) ───────────
# Entries the group page lists (the spec's number).
PAGE_ENTRIES = 50
# A member admitted later also reads what members shared with the group
# before, the earlier members' introductions included: the group is the
# audience its members chose when they shared (decided by the
# coordinator for Peter to object to; the spec and Peter's Circles
# default of 2026-11-11 said only entries shared from then on). Only the
# circles Loore keeps for a cohort use this; a person added to an
# ordinary circle by its owner still reads new entries only unless the
# owner chooses otherwise.
NEW_MEMBERS_READ_EARLIER_ENTRIES = True
# Output caps of the two drafts, in tokens. They include a reasoning
# model's thinking, which counts against the same cap (#366): the
# prompts themselves ask for at most 500 words (report) and 150 words
# (introduction); about 700 words of report text fit with room to think.
REPORT_MAX_TOKENS = 2000
INTRO_MAX_TOKENS = 1000
# Nodes of the interview thread walked to find where it ends (a thread of
# about twenty minutes has a few dozen).
MAX_THREAD_NODES = 2000

# A draft still "pending" or "processing" this long after it was asked
# for reads as failed: the worker died (a deploy restarts the workers, and
# a task is not run again), and the person can ask again. The model call
# takes up to about a minute; ten covers a queue behind other replies.
DRAFT_STALE_AFTER = timedelta(minutes=10)

# Task statuses of the two drafts (report_task_status, intro_task_status).
PENDING, PROCESSING, DONE, FAILED = "pending", "processing", "done", "failed"
NOTHING = "nothing_to_draft"
RUNNING = (PENDING, PROCESSING)
# Each status column and the column holding when its draft was asked for.
STARTED_AT = {"report_task_status": "report_task_started_at",
              "intro_task_status": "intro_task_started_at"}

# ── Plain sentences the routes answer with ──────────────────────────────
NOT_FOUND = "Not found"
NO_INVITATION = "You have no invitation to a group."
NOT_OPEN = "This invitation is no longer open."
TAKEN = "This invitation was accepted from another account."
OTHER_INVITATION = "You have already accepted another invitation."
OWNER_CLAIM = "You started this group. The invitation is for someone else."
INTERVIEW_FIRST = "Start the interview first."
REPORT_SENT = ("The report has been sent. Take it back first (set it to "
               "Only me) to draft a new one.")
REPORT_SENT_SKIP = ("The report has been sent. Take it back first (set it "
                    "to Only me) if you want to send nothing.")
REPORT_EMPTY = "The report is empty. To send nothing, choose Send nothing."
NO_REPORT = "There is no report yet."
MEMBERS_ONLY = "Only members of the group can do this."
NO_REPORT_FOR_INTRO = ("There is no report to draft an introduction from. "
                       "You can write your introduction yourself.")
INTRO_NOT_SHARED = "Share it with the group first."
NOT_YOURS = "That entry is not yours."
AI_OFF = ("Your account is set to No AI, so Loore drafts nothing. You can "
          "write it yourself.")
AI_OFF_INTERVIEW = ("Your account is set to No AI, so Loore can't hold the "
                    "interview. You can still send a report you write "
                    "yourself, or send nothing.")
REPORT_AI_OFF = ("The report is set to No AI, so Loore can't draft from it. "
                 "You can write your introduction yourself.")
DECLINE_NOT_YOURS = ("Only the person who accepted this invitation can "
                     "decline it.")


def cohort_circle_text(cohort):
    return (f"Loore keeps this circle for {cohort.name}: its name and its "
            "people follow the group.")


def cohort_leave_text(cohort):
    return (f"You read this as a member of {cohort.name}. To stop seeing "
            "someone's entries, hide them. To leave the group, tell "
            f"{founder_name(cohort)}.")


class CohortError(Exception):
    """A refused cohort request, with the plain sentence it answers."""

    def __init__(self, message, status=400, code=None, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.extra = extra

    def response(self):
        from flask import jsonify
        body = {"error": self.message}
        if self.code:
            body["code"] = self.code
        body.update(self.extra)
        return jsonify(body), self.status


# ── The flag and the per-session memory ─────────────────────────────────

def cohort_enabled():
    try:
        from flask import current_app
        return bool(current_app.config.get("COHORT_V1", False))
    except RuntimeError:
        return False


def _cache():
    return db.session.info.setdefault("cohort", {})


def forget_cohort_cache(session=None, *_):
    """Drop what this database session remembers about cohorts: after
    every flush, commit and rollback, as Circles does with its sets."""
    try:
        (session or db.session).info.pop("cohort", None)
    except Exception:  # noqa: BLE001 - no session, nothing remembered
        pass


def _install_cache_reset():
    from sqlalchemy.orm import Session
    for name in ("after_flush", "after_commit", "after_rollback"):
        event.listen(Session, name, forget_cohort_cache)


_install_cache_reset()


# ── States ──────────────────────────────────────────────────────────────

def is_live(inv):
    return inv is not None and inv.declined_at is None and inv.closed_at is None


def invitation_state(inv):
    """The one state the founder's line shows, from the timestamps:
    declined > closed > admitted > call_done > report_sent |
    report_skipped (the later of the two) > report_drafted >
    interview_started > claimed > opened > invited."""
    if inv.declined_at:
        return "declined"
    if inv.closed_at:
        return "closed"
    if inv.admitted_at:
        return "admitted"
    if inv.call_done_at:
        return "call_done"
    if inv.report_sent_at or inv.report_skipped_at:
        sent, skipped = inv.report_sent_at, inv.report_skipped_at
        if sent and (not skipped or sent >= skipped):
            return "report_sent"
        return "report_skipped"
    if inv.report_drafted_at:
        return "report_drafted"
    if inv.interview_started_at:
        return "interview_started"
    if inv.claimed_at or inv.user_id:
        return "claimed"
    if inv.first_opened_at:
        return "opened"
    return "invited"


def is_claimed(inv):
    """Accepted once, by an account that may no longer exist: an
    invitation is never accepted twice (its letter, admission and states
    were the first person's)."""
    return inv.user_id is not None or inv.claimed_at is not None


def public_state(inv):
    """What the page at the link may say: open, claimed, declined or
    closed."""
    if inv.declined_at:
        return "declined"
    if inv.closed_at:
        return "closed"
    if is_claimed(inv):
        return "claimed"
    return "open"


# ── The two drafts' statuses ────────────────────────────────────────────

def _stale(started_at, now=None):
    return (started_at is None
            or (now or datetime.utcnow()) - started_at > DRAFT_STALE_AFTER)


def task_status(inv, field):
    """The draft status *field* as the person should see it: a running
    status older than DRAFT_STALE_AFTER is "failed"."""
    status = getattr(inv, field)
    if status in RUNNING and _stale(getattr(inv, STARTED_AT[field])):
        return FAILED
    return status


def expire_stale_task(inv, field):
    """Write "failed" over a running status that is stale. Returns True
    when it changed (the caller commits)."""
    if getattr(inv, field) in RUNNING and task_status(inv, field) == FAILED:
        setattr(inv, field, FAILED)
        return True
    return False


def start_task(inv, field):
    """Mark the draft *field* pending, in one conditional update, unless
    one is running already: of two requests at once (a double click) one
    queues the draft. Returns the stamp the task is queued with (its
    started-at time), or None when another request was first. Commits."""
    from sqlalchemy import or_
    now = datetime.utcnow()
    status_col = getattr(CohortInvitation, field)
    started_col = getattr(CohortInvitation, STARTED_AT[field])
    claimed = CohortInvitation.query.filter(
        CohortInvitation.id == inv.id,
        or_(status_col.is_(None), status_col.notin_(RUNNING))).update(
        {status_col: PENDING, started_col: now}, synchronize_session=False)
    db.session.commit()
    db.session.refresh(inv)
    return now if claimed else None


def stamp_of(when):
    return when.isoformat() if when is not None else None


def report_step_done(inv):
    return bool(inv.report_sent_at or inv.report_skipped_at)


def founder_name(cohort):
    """The name the founder signs with, else the owner's username."""
    if cohort.founder_name:
        return cohort.founder_name
    owner = (db.session.get(User, cohort.owner_user_id)
             if cohort.owner_user_id else None)
    return owner.username if owner else "the founder"


def founder_username(cohort):
    owner = (db.session.get(User, cohort.owner_user_id)
             if cohort.owner_user_id else None)
    return owner.username if owner else None


# ── The access hooks ────────────────────────────────────────────────────

def cohort_circle_user_ids():
    """Ids of the accounts Circles is on for because of a cohort: every
    cohort's founder, and every account bound to a live invitation
    (privacy.circles_enabled_for). One query per database session."""
    cache = _cache()
    ids = cache.get("circle_ids")
    if ids is None:
        owners = {uid for (uid,) in db.session.query(
            Cohort.owner_user_id).filter(Cohort.owner_user_id.isnot(None))}
        bound = {uid for (uid,) in db.session.query(
            CohortInvitation.user_id).filter(
            CohortInvitation.user_id.isnot(None),
            CohortInvitation.declined_at.is_(None),
            CohortInvitation.closed_at.is_(None))}
        ids = cache["circle_ids"] = frozenset(owners | bound)
    return ids


def is_cohort_person(user_id):
    return cohort_enabled() and user_id in cohort_circle_user_ids()


def free_access(user):
    """*user* is bound to a live invitation whose cohort is free today
    (free_until is today or later, UTC): let in with full access, AI
    included, without a card, as if its plan were set by hand
    (User.plan_set_by_hand). False while COHORT_V1 is off, with no
    query."""
    if not cohort_enabled() or user is None or getattr(user, "id", None) is None:
        return False
    # An account with its own active subscription (or an alpha tester's
    # free month) keeps it unchanged: its ceiling and its paid period stay
    # its own. The free period is for accounts with none.
    if (getattr(user, "subscription_grants_access", False)
            or getattr(user, "alpha_free_month_active", False)):
        return False
    cache = _cache().setdefault("free", {})
    if user.id not in cache:
        today = datetime.utcnow().date()
        cache[user.id] = db.session.query(CohortInvitation.id).join(
            Cohort, Cohort.id == CohortInvitation.cohort_id).filter(
            CohortInvitation.user_id == user.id,
            CohortInvitation.declined_at.is_(None),
            CohortInvitation.closed_at.is_(None),
            Cohort.free_until.isnot(None),
            Cohort.free_until >= today).first() is not None
    return cache[user.id]


def free_access_user_ids():
    """The ids free_access is True for, as a list for a SQL ``IN`` (the
    paid-features clauses of the background jobs). Empty while COHORT_V1
    is off, with no query."""
    if not cohort_enabled():
        return []
    today = datetime.utcnow().date()
    return sorted({uid for (uid,) in db.session.query(
        CohortInvitation.user_id).join(
        Cohort, Cohort.id == CohortInvitation.cohort_id).filter(
        CohortInvitation.user_id.isnot(None),
        CohortInvitation.declined_at.is_(None),
        CohortInvitation.closed_at.is_(None),
        Cohort.free_until.isnot(None),
        Cohort.free_until >= today)})


def free_until_of(user):
    """The free period's last day (a date) for *user*, or None (also for
    an account whose own subscription applies, free_access)."""
    if (getattr(user, "subscription_grants_access", False)
            or getattr(user, "alpha_free_month_active", False)):
        return None
    inv = invitation_for_user(user.id)
    if not is_live(inv):
        return None
    cohort = db.session.get(Cohort, inv.cohort_id)
    return cohort.free_until if cohort else None


# The routes an account reaches before it has claimed its invitation,
# whatever its subscription or approval (the subscription gate and the
# approval gate ask these while COHORT_V1 is on).
_OPEN_EXACT = {("POST", "/api/cohort/claim"), ("GET", "/api/cohort/me")}


def open_without_subscription(method, path):
    if not cohort_enabled():
        return False
    path = path.rstrip("/") or "/"
    return path.startswith("/api/invitation/") or (method, path) in _OPEN_EXACT


def open_without_approval(path):
    """The invitation page's routes are public: also for a signed-in
    account still waiting for approval."""
    return cohort_enabled() and path.startswith("/api/invitation/")


# ── Lookups ─────────────────────────────────────────────────────────────

def invitation_by_code(code):
    if not code or not isinstance(code, str) or len(code) > 64:
        return None
    return CohortInvitation.query.filter_by(code=code).first()


def invitation_for_user(user_id):
    """The account's invitation: a live one first, then the newest."""
    rows = (CohortInvitation.query.filter_by(user_id=user_id)
            .order_by(CohortInvitation.claimed_at.desc(),
                      CohortInvitation.id.desc()).all())
    live = [r for r in rows if is_live(r)]
    return (live or rows or [None])[0]


def owned_cohorts(user_id):
    return (Cohort.query.filter_by(owner_user_id=user_id)
            .order_by(Cohort.created_at.asc(), Cohort.id.asc()).all())


def member_ids(cohort):
    """The group: every admitted, live, bound account that is not being
    deleted, and the founder."""
    ids = {uid for (uid,) in db.session.query(CohortInvitation.user_id).join(
        User, User.id == CohortInvitation.user_id).filter(
        CohortInvitation.cohort_id == cohort.id,
        CohortInvitation.admitted_at.isnot(None),
        CohortInvitation.declined_at.is_(None),
        CohortInvitation.closed_at.is_(None),
        User.deleted_at.is_(None))}
    if cohort.owner_user_id:
        ids.add(cohort.owner_user_id)
    return ids


def is_member(cohort, user_id):
    return user_id is not None and user_id in member_ids(cohort)


def cohort_circle_of(user_id, cohort_id):
    return Circle.query.filter_by(user_id=user_id, cohort_id=cohort_id).first()


def new_code():
    return secrets.token_urlsafe(24)


def slugify(name):
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:48]
    base = base or "cohort"
    slug, n = base, 1
    while Cohort.query.filter_by(slug=slug).first() is not None:
        n += 1
        slug = f"{base}-{n}"
    return slug


def clean_handle(raw):
    return (raw or "").strip().lstrip("@").strip().lower()


# ── Preparing the invited person's account ──────────────────────────────

def prepare_invited_account(handle, x_lookup=False, payer_id=None):
    """Approve the X handle so that the person's X sign-in finds or
    creates an approved account: the admin whitelist's own function
    (routes/admin.whitelist_x_handle) makes the account with the handle's
    X id; an account that already holds that id is used as it is. Then
    the account is approved by hand when it is not (payments off, the
    whitelist leaves it unapproved for the pre-fill workflow). Returns
    (user, x_id). Raises CohortError with the whitelist's answer, or for
    an account the admin blocked. Commits."""
    from backend.routes.admin import WhitelistRefused, whitelist_x_handle
    from backend.utils.approval import (
        approved_by_hand, counts_as_approved, is_blocked)
    try:
        user, resolved = whitelist_x_handle(handle, x_lookup=x_lookup,
                                            payer_id=payer_id,
                                            existing_ok=True)
    except WhitelistRefused as e:
        if e.holder is None:
            body = dict(e.body)
            raise CohortError(body.pop("error"), e.status,
                              code="cohort_handle", **body)
        user = e.holder
    if user.deleted_at is not None:
        raise CohortError(f"@{user.username}'s account is being deleted.",
                          409, code="cohort_account_deleted")
    if is_blocked(user):
        raise CohortError(f"@{user.username}'s account is blocked. Unblock "
                          "it in Users first.", 409,
                          code="cohort_account_blocked")
    if not counts_as_approved(user):
        user.approved = True
        user.approved_at = datetime.utcnow()
        approved_by_hand(user)
        from backend.utils.subscriptions import convert_if_alpha
        convert_if_alpha(user)
        db.session.commit()
    return user, user.twitter_id


def claim_refusal(inv, user):
    """Why *user* may not accept *inv*, as a CohortError, or None."""
    if not is_live(inv):
        return CohortError(NOT_OPEN, 409, code="cohort_not_open")
    if is_claimed(inv):
        if inv.user_id is not None and inv.user_id == user.id:
            return None
        return CohortError(TAKEN, 409, code="cohort_taken")
    cohort = db.session.get(Cohort, inv.cohort_id)
    if cohort is not None and cohort.owner_user_id == user.id:
        return CohortError(OWNER_CLAIM, 409, code="cohort_owner")
    if inv.x_handle:
        handle = inv.x_handle
        if not user.twitter_id:
            return CohortError(
                f"This invitation is for @{handle}. Sign in with X as "
                f"@{handle} to accept it.", 403, code="cohort_sign_in_with_x")
        if inv.x_id:
            matches = user.twitter_id == inv.x_id
        else:
            matches = (user.twitter_handle or "").lower() == handle
        if not matches:
            return CohortError(
                f"This invitation is for @{handle}. Sign in with that X "
                "account to accept it.", 403, code="cohort_wrong_account")
    other = invitation_for_user(user.id)
    if other is not None and other.id != inv.id and is_live(other):
        return CohortError(OTHER_INVITATION, 409, code="cohort_other")
    return None


# ── The interview ───────────────────────────────────────────────────────

def _plain(text):
    """Founder text put into a person's prompt never acts as a
    placeholder there: braces become parentheses."""
    return (text or "").replace("{", "(").replace("}", ")")


def compose_interview_prompt(user_id, cohort, inv, mode):
    """The interview thread's prompt text: the person's own agentic
    prompt for *mode* (their Voice or Text prompt, edited or Loore's),
    then the interview section with the cohort's name and about, the
    founder's name and the person's letter filled in. Reads the letter."""
    from backend.utils.prompts import get_user_prompt, load_default_prompt
    base = get_user_prompt(
        user_id, "voice" if mode == "voice" else "textmode") or ""
    section = load_default_prompt(INTERVIEW_KEY) or ""
    values = {
        "{founder_name}": _plain(founder_name(cohort)),
        # The name the web app's link back to /cohort carries (the NavBar).
        "{cohort_name}": _plain(cohort.name or ""),
        "{cohort_about}": _plain(cohort.about or ""),
        "{invitation_letter}": _plain(inv.get_letter()),
    }
    # One pass: text filled in for one placeholder is never searched for
    # another.
    pattern = re.compile("|".join(re.escape(k) for k in values))
    section = pattern.sub(lambda m: values[m.group(0)], section)
    return base.rstrip() + "\n\n" + section


def interview_prompt_version(user_id, text):
    """A prompt version of the person's own holding *text*, for the
    interview thread's prompt node to link (so the thread renders,
    caches and pins its documents like any agentic thread). Flushes."""
    from backend.utils.prompts import PROMPT_DEFAULTS, default_prompt_hash
    version = UserPrompt(
        user_id=user_id, prompt_key=INTERVIEW_KEY,
        title=PROMPT_DEFAULTS[INTERVIEW_KEY]["title"],
        generated_by="cohort",
        based_on_default_hash=default_prompt_hash(INTERVIEW_KEY))
    version.set_content(text)
    db.session.add(version)
    db.session.flush()
    return version


def own_node(node, user_id):
    return (node is not None and node.deleted_at is None
            and (node.human_owner_id or node.user_id) == user_id)


def interview_root(inv, user_id):
    """The invitation's interview thread root, when it is the person's
    own, alive, interview prompt node; else None."""
    if inv is None or not inv.interview_node_id:
        return None
    root = db.session.get(Node, inv.interview_node_id)
    if not own_node(root, user_id) or root.get_prompt_key() != INTERVIEW_KEY:
        return None
    return root


def thread_path(root_id, user_id):
    """(tip id, [ids from below the root down to the tip]) of the
    interview thread: the tip is its newest alive node of the person's
    own (their messages and the AI replies they asked for), the path the
    nodes that lead to it. (None, []) for a thread with nothing below
    its root. Ids only."""
    from backend.utils.thread_tree import subtree_walk
    walk = subtree_walk([root_id], name="cohort_interview_walk")
    rows = (db.session.query(walk.c.id, walk.c.parent_id, walk.c.user_id,
                             walk.c.human_owner_id, walk.c.deleted_at,
                             walk.c.created_at, walk.c.depth)
            .limit(MAX_THREAD_NODES).all())
    parent = {r.id: r.parent_id for r in rows}
    candidates = [r for r in rows if r.depth > 0 and r.deleted_at is None
                  and (r.human_owner_id or r.user_id) == user_id]
    if not candidates:
        return None, []
    tip = max(candidates, key=lambda r: (r.created_at or datetime.min, r.id))
    path, current = [], tip.id
    for _ in range(MAX_THREAD_NODES):
        if current is None or current == root_id:
            break
        path.append(current)
        current = parent.get(current)
    path.reverse()
    return tip.id, path


def _is_ai(node):
    return node.node_type == "llm" or node.llm_model is not None


def interview_transcript(inv, user):
    """The text the report is drafted from: the person's interview
    thread as it ends (thread_path), their messages and the
    interviewer's, each labelled. Only nodes of their own, alive,
    finished, and whose ai_usage lets a model read them (No AI means no
    AI). Returns (text, nodes), ("", []) when there is nothing. Reads
    the interview: called only by the draft task, for the person."""
    from backend.utils.encryption import prefetch_deks
    from backend.utils.privacy import AI_ALLOWED
    root = interview_root(inv, user.id)
    if root is None:
        return "", []
    _tip, path = thread_path(root.id, user.id)
    if not path:
        return "", []
    by_id = {n.id: n for n in Node.query.filter(Node.id.in_(path))}
    nodes = []
    for nid in path:
        n = by_id.get(nid)
        if not own_node(n, user.id) or n.ai_usage not in AI_ALLOWED:
            continue
        if _is_ai(n) and n.llm_task_status not in (None, "completed"):
            continue
        nodes.append(n)
    prefetch_deks(n.content for n in nodes)
    parts, used = [], []
    for n in nodes:
        text = (n.get_content() or "").strip()
        if not text:
            continue
        parts.append(("Interviewer" if _is_ai(n) else "Me") + ":\n" + text)
        used.append(n)
    return "\n\n".join(parts), used


def report_template(cohort):
    """The report's headings, for a person who writes it themselves:
    the form the report prompt asks the model for."""
    from backend.utils.prompts import load_default_prompt
    text = load_default_prompt(REPORT_KEY) or ""
    marker = "in this form:"
    form = text[text.index(marker) + len(marker):] if marker in text else ""
    lines = [ln for ln in form.splitlines() if ln.startswith("#")]
    name = founder_name(cohort)
    return "\n\n".join(ln.replace("{founder_name}", name) for ln in lines) + "\n"


def report_prompt(cohort):
    from backend.utils.prompts import load_default_prompt
    return (load_default_prompt(REPORT_KEY) or "").replace(
        "{founder_name}", founder_name(cohort))


def intro_prompt():
    from backend.utils.prompts import load_default_prompt
    return load_default_prompt(INTRO_KEY) or ""


# ── The circles Loore keeps ─────────────────────────────────────────────

def _circle_name(cohort, owner_id, circle_id=None):
    """The cohort's name for a member's circle, unless the member has a
    circle of their own by that name already."""
    from backend.utils.circles import MAX_CIRCLE_NAME_CHARS
    name = (cohort.name or "Group")[:MAX_CIRCLE_NAME_CHARS]
    clash = Circle.query.filter(Circle.user_id == owner_id,
                                func.lower(Circle.name) == name.lower())
    if circle_id is not None:
        clash = clash.filter(Circle.id != circle_id)
    if clash.first() is not None:
        name = (name[:MAX_CIRCLE_NAME_CHARS - 8] + " (group)")
    return name


def sync_cohort_circles(cohort, now=None):
    """For every member and the founder, a circle of their own marked with
    the cohort and named after it, whose people are exactly all the other
    members and the founder. A person added also reads what was shared
    with it before (NEW_MEMBERS_READ_EARLIER_ENTRIES). The circle
    of someone who left the group is deleted, and they leave everyone
    else's. Members are added and removed through the Circles functions
    (circles.add_circle_member / remove_circle_member), so their side
    effects stay what an owner's own add or remove does. The caller
    commits."""
    from backend.utils import circles as cz
    now = now or datetime.utcnow()
    members = member_ids(cohort)
    users = {u.id: u for u in User.query.filter(User.id.in_(members))} \
        if members else {}
    existing = {c.user_id: c for c in Circle.query.filter_by(
        cohort_id=cohort.id)}
    for uid in sorted(members):
        circle = existing.get(uid)
        if circle is None:
            circle = Circle(user_id=uid, cohort_id=cohort.id,
                            name=_circle_name(cohort, uid), created_at=now)
            db.session.add(circle)
            db.session.flush()
        else:
            want_name = _circle_name(cohort, uid, circle.id)
            if circle.name != want_name:
                circle.name = want_name
        want = members - {uid}
        have = {m.user_id: m for m in CircleMember.query.filter_by(
            circle_id=circle.id)}
        for gone in sorted(have.keys() - want):
            cz.remove_circle_member(have[gone])
        for new in sorted(want - have.keys()):
            cz.add_circle_member(circle, users[new],
                                 NEW_MEMBERS_READ_EARLIER_ENTRIES, now=now)
    for uid, circle in existing.items():
        if uid not in members:
            cz.delete_circle(circle)
    db.session.flush()


def close_for_deleted_account(user_id, now=None):
    """The account bound to these invitations is being deleted
    (account_deletion.delete_identity): each live one is closed, so it can
    never be accepted again, and a member leaves the group's circles.
    Returns how many were closed. The caller commits."""
    now = now or datetime.utcnow()
    closed, cohorts = 0, set()
    for inv in CohortInvitation.query.filter(
            CohortInvitation.user_id == user_id,
            CohortInvitation.declined_at.is_(None),
            CohortInvitation.closed_at.is_(None)):
        inv.closed_at = now
        closed += 1
        if inv.admitted_at:
            cohorts.add(inv.cohort_id)
    db.session.flush()
    for cohort_id in sorted(cohorts):
        cohort = db.session.get(Cohort, cohort_id)
        if cohort is not None:
            sync_cohort_circles(cohort, now=now)
    return closed


def cohort_of_circle(circle):
    if circle is None or not circle.cohort_id or not cohort_enabled():
        return None
    return db.session.get(Cohort, circle.cohort_id)


# ── The group page ──────────────────────────────────────────────────────

def page_entries(cohort, viewer_id, limit=PAGE_ENTRIES):
    """The newest *limit* entries shared through the cohort's circles by
    its members that *viewer_id* reads (the "Shared with you" set,
    privacy.readable_entry_ids), and the viewer's own entries shared with
    their cohort circle, newest share first: [(node, shared_at)]. Reads
    no text."""
    from backend.utils.circles import is_shared_entry
    from backend.utils.privacy import readable_entry_ids
    members = member_ids(cohort)
    readable = set(readable_entry_ids(viewer_id))
    rows = (db.session.query(NodeCircle.node_id, NodeCircle.shared_at,
                             Circle.user_id)
            .join(Circle, Circle.id == NodeCircle.circle_id)
            .filter(Circle.cohort_id == cohort.id,
                    Circle.user_id.in_(members)).all()) if members else []
    newest = {}
    for node_id, shared_at, circle_owner in rows:
        if circle_owner != viewer_id and node_id not in readable:
            continue
        if node_id not in newest or shared_at > newest[node_id][0]:
            newest[node_id] = (shared_at, circle_owner)
    if not newest:
        return []
    nodes = {n.id: n for n in Node.query.filter(Node.id.in_(list(newest)))}
    out = []
    for node_id, (shared_at, circle_owner) in newest.items():
        n = nodes.get(node_id)
        if n is None or n.deleted_at is not None or not is_shared_entry(n):
            continue
        owner = n.human_owner_id or n.user_id
        if owner != circle_owner:
            continue
        out.append((n, shared_at))
    out.sort(key=lambda t: (t[1], t[0].id), reverse=True)
    return out[:limit]


# ── The signed-in user's payload ────────────────────────────────────────

def user_fields(user):
    """The current-user payload's ``cohort`` (COHORT_V1 only; no key
    while it is off): for an account with a live invitation or a
    founder, what the NavBar link and the first-page redirect need."""
    if not cohort_enabled() or user is None:
        return {}
    inv = invitation_for_user(user.id)
    if is_live(inv):
        cohort = db.session.get(Cohort, inv.cohort_id)
        return {"cohort": {
            "name": cohort.name if cohort else None,
            "slug": cohort.slug if cohort else None,
            "role": "member" if inv.admitted_at else "invited",
            "state": invitation_state(inv),
            "report_step_done": report_step_done(inv),
        }}
    owned = owned_cohorts(user.id)
    if owned:
        return {"cohort": {"name": owned[0].name, "slug": owned[0].slug,
                           "role": "founder", "state": None,
                           "report_step_done": True}}
    return {"cohort": None}
