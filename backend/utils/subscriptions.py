"""What a subscription gives an account (#438), without calling Stripe.

The switch is PAYMENTS_ENABLED. Off, nothing here changes what an account
can do. On:

* an account gets AI replies when it is on a paid plan
  (User.on_paid_plan: a plan set by hand, alpha or pro, a trialing or
  active subscription, or an alpha tester's free month) or is an admin.
  Every other account
  ("free", or a subscription that ended) keeps writing, reading and
  export; create_llm_placeholder refuses its replies with
  SubscriptionRequired, which the routes answer as a 402 (or, where the
  writing was saved, as the ``subscription_required`` fields of a 2xx).
  Such an account starts no recording, dictation or upload (402 before
  anything is stored). One accepted while it had AI replies, whose
  access ends before it is finalized, transcribed or replied to (Peter,
  2026-11-13), is stored and transcribed to the end; only its reply is
  skipped, with the "recording" text, said aloud in Voice
  (utils/voice_refusals).
* Voice mode and profile updates follow User.on_paid_plan whatever the
  switch: a subscription that exists always counts, so turning payments
  off does not take what people paid for.
* the per-user spend cap of an account whose access comes from its
  subscription is the level's ceiling (subscription_spend_limit_usd).

A Stripe event never writes ``User.plan``: an account set by hand stays
as it is (utils/stripe_billing.apply_subscription).
"""
import re
from datetime import datetime, timedelta

from flask import current_app, has_app_context

from backend.models import SUBSCRIPTION_LEVELS
from backend.utils.timefmt import iso_utc

# The levels a user can choose; the discounted one needs a code.
OFFERED_LEVELS = ("standard", "supporter")


class SubscriptionRequired(Exception):
    """A reply asked for by an account that has no AI replies while
    payments are on. Raised by create_llm_placeholder before any write;
    the app answers it with subscription_required_response (402)."""

    def __init__(self, user=None):
        super().__init__("subscription_required")
        self.message = subscription_required_message(user)
        # Why, for a recording kept without its reply (access ended while
        # it was recorded): the text it carries, said aloud in Voice.
        self.reason = paused_reason(user)
        self.recording_message = paused_message(self.reason, "recording")


def _config(config=None):
    if config is not None:
        return config
    if has_app_context():
        return current_app.config
    return {}


def payments_enabled(config=None):
    return bool(_config(config).get("PAYMENTS_ENABLED"))


def is_production(config=None):
    """Anything but development, testing and staging (FLASK_ENV)."""
    env = (_config(config).get("DEPLOY_ENV") or "production").lower()
    return env not in ("development", "testing", "staging")


def key_mode(key):
    """"live", "test" or None for a Stripe secret or restricted key."""
    if not key:
        return None
    if key.startswith(("sk_live_", "rk_live_")):
        return "live"
    if key.startswith(("sk_test_", "rk_test_")):
        return "test"
    return None


def check_payments_config(config):
    """At boot: in production, payments stay off unless the key is a live
    one (a sandbox key there would sell nothing real and take test events
    as real). Returns why they were turned off, or None."""
    if not config.get("PAYMENTS_ENABLED") or not is_production(config):
        return None
    if key_mode(config.get("STRIPE_SECRET_KEY")) == "live":
        return None
    config["PAYMENTS_ENABLED"] = False
    return ("PAYMENTS_ENABLED is set, but the Stripe key is not a live "
            "key; payments stay off in production.")


def refuse_without_paid_features(user, config=None):
    """For a route that starts an optional model call (new speech for a
    profile or a reference): the 402 to answer while payments are on and
    the account has no AI replies, else None."""
    if ai_replies_allowed(user, config):
        return None
    return subscription_required_response(user)


def access_state(user, config=None):
    """Where the account stands: "full" while payments are off, for an
    admin and for a plan set by hand; otherwise the subscription's
    ACCESS_STATES ("active", "lapsed", "none")."""
    if not payments_enabled(config) or user is None:
        return "full"
    if getattr(user, "is_admin", False) or user.plan_set_by_hand:
        return "full"
    return user.subscription_access_state


# State "none" (#438; Peter, 2026-11-12: "people shouldn't be let in until
# they have a subscription"): the only routes such an account reaches.
# Everything else under /api and /media answers no_subscription_response.
_OPEN_EXACT = {
    ("GET", "/api/dashboard"),            # who am I (no profile in it)
    # Its own settings, as a waitlisted account always could: the
    # subscribe page asks "Start from your tweets?" (#697). Starts nothing.
    ("PUT", "/api/dashboard/user"),
    ("PATCH", "/api/dashboard/timezone"),
    ("POST", "/api/account/delete"),
    ("POST", "/api/account/delete/confirm"),
    ("POST", "/api/account/restore"),
    ("POST", "/api/account/restore/decline"),
    ("GET", "/api/account/restore"),
    # The account's own data, as one file (Peter, 2026-11-13, of an account
    # whose trial ended and whose first payment failed: "yes, Ben should
    # be able to export"). Built from what is stored, no model call; HEAD
    # is the web's check before the download, which builds nothing.
    ("GET", "/api/export/threads"),
    ("HEAD", "/api/export/threads"),
}
_OPEN_PREFIXES = ("/api/billing", "/api/terms", "/api/webhooks",
                  "/api/health", "/api/ready", "/api/live")

NO_SUBSCRIPTION_ERROR = "no_subscription"
NO_SUBSCRIPTION_MESSAGE = "This account has no active subscription yet."


def open_without_subscription(method, path):
    """Whether an account in state "none" may call *method* *path*."""
    path = path.rstrip("/") or "/"
    if not (path.startswith("/api") or path.startswith("/media")):
        return True   # sign-in, sign-out, the pages themselves
    if (method, path) in _OPEN_EXACT:
        return True
    return any(path == p or path.startswith(p + "/") for p in _OPEN_PREFIXES)


def no_subscription_response():
    from flask import jsonify
    return jsonify({"error": NO_SUBSCRIPTION_ERROR,
                    "message": NO_SUBSCRIPTION_MESSAGE}), 402


def block_accounts_without_subscription():
    """The app's before_request (#438). Payments on: an approved account
    that never had a subscription (and whose plan was not set by hand) is
    not let in. It reaches only who-am-I, its subscription, the terms,
    deleting itself, the download of its data, and the calls that finish
    a recording it had under way (finishes_recording_in_progress); every
    other /api and /media route answers 402 {"error": "no_subscription"}
    before the view runs. No-op while payments are off."""
    from flask_login import current_user
    if not current_user.is_authenticated:
        return None
    return refuse_not_let_in(current_user)


def refuse_not_let_in(user):
    """402 no_subscription when *user* (the session's, or the account an
    API token belongs to: utils/api_tokens) is not let in and this
    request is not one it may make; None otherwise. The same rule for
    every way a request is authenticated. The calls that finish a
    recording it had under way when it stopped being let in pass
    (finishes_recording_in_progress)."""
    if not payments_enabled():
        return None
    from flask import request
    if user is None or not getattr(user, "approved", False):
        return None
    if open_without_subscription(request.method, request.path):
        return None
    if access_state(user) != "none":
        return None
    if finishes_recording_in_progress(user, request):
        return None
    return no_subscription_response()


# ── A recording in progress when the account stops being let in ─────────
# Peter, 2026-11-13, on a subscription that ends during a recording: "User's
# recorded message should be stored on Loore's servers, possibly even
# transcribed (transcription is cheap). Just further AI use should be
# blocked." An account that falls into state "none" while it records (a
# trial whose first payment fails, say) keeps the calls of that recording
# only: its remaining chunks, its finalize, and the status and stream that
# report its transcript; for an upload, its chunks, its finalize and its
# transcription status. Nothing else opens: no new session or upload (every
# init is refused in this state, so each session or upload these calls
# name was started while the account was let in, or before payments were
# on), no claim of a session from another device, no reply (the account
# has no AI replies), nothing else to read. A session still recording takes
# its chunks whatever the gap since the last one (a pause, the app killed
# and opened again, a network gap: the audio recorded before and after it
# is kept); and none of it older than RECORDING_IN_PROGRESS_MAX_AGE
# (Draft.created_at, the upload's Node.created_at). The saved entry then
# waits in the account; a dictation's text waits as a draft, which the
# data download includes (routes/export_data.iter_drafts_part).

# INTRODUCED HEURISTIC: how long after it started a recording or upload may
# still be finished under this exception, and so what an account that is
# not let in can still have transcribed (one recording's audio). Three
# hours is far longer than recordings run in practice, but neither app
# limits a recording's length (the web's ~60-minute cliff is the open bug
# #127; the iPhone has none) and Draft.created_at counts pauses: a
# recording still running three hours after it started loses what is
# recorded after that (its chunks are refused; what came before is kept).
RECORDING_IN_PROGRESS_MAX_AGE = timedelta(hours=3)

_DRAFT_SESSION_CALL = re.compile(
    r"^/api/drafts/streaming/([^/]+)/(audio-chunk|finalize|release|status)$")
_DRAFT_SESSION_STREAM = re.compile(
    r"^/api/sse/drafts/([^/]+)/transcription-stream$")
_UPLOAD_CALLS = ("/api/nodes/upload/chunk", "/api/nodes/upload/finalize")
_UPLOAD_STATUS = re.compile(r"^/api/nodes/(\d+)/transcription-status$")


def finishes_recording_in_progress(user, req, now=None):
    """Whether *req* (a Flask request) only finishes a recording or upload
    of *user* that was under way when it stopped being let in (the
    comment above). Asked by the gate for an account in state "none".
    Every id the request names is checked against the others and against
    the account: the session in the path is the account's; the thread a
    finalize names is the one the session started in; an upload's folder
    (upload_id) is the account's and was made by the init of the entry it
    names (node_id), and a chunk is one of those its init announced."""
    from backend.extensions import db
    from backend.models import Draft, Node
    now = now or datetime.utcnow()
    since = now - RECORDING_IN_PROGRESS_MAX_AGE
    path = req.path.rstrip("/")
    method = req.method

    call = _DRAFT_SESSION_CALL.match(path)
    stream = _DRAFT_SESSION_STREAM.match(path)
    if call or stream:
        session_id = (call or stream).group(1)
        action = call.group(2) if call else "stream"
        if method != ("GET" if action in ("status", "stream") else "POST"):
            return False
        draft = Draft.query.filter_by(session_id=session_id,
                                      user_id=user.id).first()
        if draft is None or draft.created_at is None \
                or draft.created_at < since:
            return False
        status = draft.streaming_status
        if action in ("audio-chunk", "release"):
            # Whatever the gap since its last sign of life: a long pause or
            # a relaunch must not lose the rest (the route still refuses a
            # device the session was taken over from).
            return status == "recording"
        if action == "finalize":
            # A Voice turn joins the thread its session started in; a
            # dictation's finalize names none (its entry is saved later,
            # under the session's own parent).
            named = _body(req).get("parent_id")
            return (status == "recording"
                    and (named in (None, "")
                         or _same_id(named, draft.parent_id)))
        return status in ("recording", "finalizing", "completed")

    if method == "POST" and path in _UPLOAD_CALLS:
        is_chunk = path.endswith("/chunk")
        values = req.form if is_chunk else _body(req)
        metadata = _upload_metadata(user, values.get("upload_id"))
        if metadata is None or not _same_id(values.get("node_id"),
                                            metadata.get("node_id")):
            return False
        node = _own_recent_upload_node(db, Node, user, metadata.get("node_id"),
                                       since)
        # Chunks and finalize: an upload whose init was accepted and whose
        # file is not assembled yet.
        if (node is None or node.audio_original_url is not None
                or node.transcription_status != "pending"):
            return False
        if is_chunk:
            try:
                index = int(values.get("chunk_index"))
                total = int(metadata.get("total_chunks"))
            except (TypeError, ValueError):
                return False
            return 0 <= index < total
        return True

    status_call = _UPLOAD_STATUS.match(path)
    if method == "GET" and status_call:
        node = _own_recent_upload_node(db, Node, user, status_call.group(1),
                                       since)
        return node is not None
    return False


def _body(req):
    body = req.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _same_id(raw, stored):
    """*raw* (from a request: an int, a digit string, or nothing) names the
    same id as *stored* (an int or None)."""
    if raw in (None, ""):
        return stored is None
    try:
        return stored is not None and int(raw) == int(stored)
    except (TypeError, ValueError):
        return False


def _upload_metadata(user, upload_id):
    """The metadata its init wrote for *user*'s chunked upload *upload_id*
    (routes/nodes.init_chunked_upload), or None: a malformed id, a folder
    outside the account's, or no such upload."""
    import json
    from backend.routes import nodes as nodes_routes
    if not isinstance(upload_id, str) \
            or not nodes_routes._UPLOAD_ID_RE.fullmatch(upload_id):
        return None
    own = (nodes_routes.AUDIO_STORAGE_ROOT / "chunks" / str(user.id)).resolve()
    folder = (own / upload_id).resolve()
    if folder.parent != own:
        return None
    try:
        metadata = json.loads((folder / "metadata.json").read_text())
    except (OSError, ValueError):
        return None
    return metadata if isinstance(metadata, dict) else None


def _own_recent_upload_node(db, node_model, user, node_id, since):
    """*user*'s audio entry *node_id*, made since *since*, or None."""
    try:
        node = db.session.get(node_model, int(node_id))
    except (TypeError, ValueError):
        return None
    if (node is None or node.user_id != user.id
            or node.created_at is None or node.created_at < since
            or node.transcription_status is None):
        return None
    return node


def ai_replies_allowed(user, config=None):
    """Whether *user* may get an AI reply. Always True while payments
    are off, for admins, and on a paid plan (set by hand or subscribed)."""
    if not payments_enabled(config):
        return True
    if user is None:
        return True
    if getattr(user, "is_admin", False):
        return True
    return bool(user.on_paid_plan)


# What stops, per kind of refused call: (the clause after "so", the
# sentence without a subscription, what still works). Without the paid
# set an account makes no billed call at all (decided under Peter's "no
# AI replies and background jobs", 2026-11-12): replies, transcription,
# search by meaning, todo merges and poll drafts alike. The one exception
# is a recording accepted while it had the paid set: it is transcribed
# to the end (Peter, 2026-11-13: "User's recorded message should be
# stored on Loore's servers, possibly even transcribed").
_PAUSED = {
    "reply": ("Loore doesn't reply",
              "Loore doesn't reply on an account without a subscription.",
              "Your writing is saved."),
    "transcribe": ("Loore doesn't transcribe recordings",
                   "Loore doesn't transcribe recordings on an account "
                   "without a subscription.",
                   "Typing works as before."),
    "search": ("search by meaning is paused",
               "Search by meaning is paused on an account without a "
               "subscription.",
               "Search by words works as before."),
    "ai": ("Loore doesn't use AI for this",
           "Loore doesn't use AI on an account without a subscription.",
           "Your writing is saved."),
    "sync": ("Loore doesn't sync bookmarks from X",
             "Loore doesn't sync bookmarks from X on an account without a "
             "subscription.",
             "Your saved references stay."),
    # A recording accepted while the account had AI replies, whose access
    # ended before its reply (Peter, 2026-11-13): it is kept and
    # transcribed, only the reply is skipped. Said aloud in Voice
    # (utils/voice_refusals).
    "recording": ("Loore doesn't reply",
                  "Loore doesn't reply on an account without a "
                  "subscription.",
                  "Your recording is saved."),
}

# Why an account has no AI replies, as the texts name it: its
# subscription ended, its last payment failed, its free month (an alpha
# tester's) ended, or none of these.
PAUSED_REASONS = ("ended", "past_due", "free_month", "none")
_REASON_CLAUSES = {
    "ended": "Your subscription has ended",
    "past_due": "Your last payment didn't go through",
    "free_month": "Your free month has ended",
}


def paused_reason(user):
    """PAUSED_REASONS for *user* (None: "none")."""
    status = getattr(user, "subscription_status", None) if user else None
    if status in ("ended", "past_due"):
        return status
    if user is not None and getattr(user, "is_alpha_tester", False):
        return "free_month"
    return "none"


def paused_message(reason, kind="reply"):
    """The text for *reason* (PAUSED_REASONS) and *kind* (_PAUSED)."""
    so, alone, works = _PAUSED.get(kind, _PAUSED["reply"])
    clause = _REASON_CLAUSES.get(reason)
    if clause is None:
        return f"{alone} {works}"
    return f"{clause}, so {so}. {works}"


def subscription_required_message(user=None, kind="reply"):
    """Why the call is refused, for the web and the iPhone alike: it
    names the state, never where or how to pay (the iPhone app shows it
    as it is). The web adds its link to Settings."""
    return paused_message(paused_reason(user), kind)


def subscription_required_payload(user=None, kind="reply"):
    return {
        "error": "subscription_required",
        "message": subscription_required_message(user, kind),
    }


def subscription_required_response(user=None, kind="reply"):
    from flask import jsonify
    return jsonify(subscription_required_payload(user, kind)), 402


def refuse_billed_call(user, kind="ai", config=None):
    """402 subscription_required when *user* may make no billed call (no
    paid set while payments are on); None otherwise."""
    if ai_replies_allowed(user, config):
        return None
    return subscription_required_response(user, kind)


def subscription_required_fields(user, config=None):
    """For a 2xx that saved the writing: the fields that say the reply
    was skipped and why, or None when the reply may go ahead."""
    if ai_replies_allowed(user, config):
        return None
    return {"subscription_required": True,
            "subscription_message": subscription_required_message(user)}


def check_ai_replies(user, config=None):
    """Raise SubscriptionRequired when *user* may not get an AI reply."""
    if not ai_replies_allowed(user, config):
        raise SubscriptionRequired(user)


def level_spend_limit_usd(level, config=None):
    cfg = _config(config)
    if level not in SUBSCRIPTION_LEVELS:
        return None
    value = cfg.get(f"SUBSCRIPTION_SPEND_LIMIT_USD_{level.upper()}")
    return None if value is None else float(value)


# Until an alpha tester's first paid invoice, the usage ceiling is this
# level's, whatever level they chose (Peter, 2026-11-13: "the standard
# limit"): during the free month, and between its end and the first charge.
ALPHA_UNPAID_CEILING_LEVEL = "standard"


def subscription_spend_limit_usd(user, config=None):
    """The monthly ceiling an account's subscription gives it, or None:
    for an account set by hand (its cap stays what it was) and for one
    whose subscription gives nothing. An alpha tester has the Standard
    level's ceiling until a first invoice is paid: during the month
    without a subscription, and with one started during the month (at any
    level) whose first payment has not gone through. The chosen level's
    ceiling applies from then."""
    if user is None or getattr(user, "plan_set_by_hand", True):
        return None
    alpha_unpaid = (getattr(user, "alpha_free_granted_at", None) is not None
                    and not getattr(user, "subscription_ever_paid", False))
    if getattr(user, "subscription_grants_access", False):
        if alpha_unpaid:
            return level_spend_limit_usd(ALPHA_UNPAID_CEILING_LEVEL, config)
        return level_spend_limit_usd(user.subscription_level, config)
    if getattr(user, "alpha_free_month_active", False):
        return level_spend_limit_usd(ALPHA_UNPAID_CEILING_LEVEL, config)
    return None


# ── The alpha free month (#438) ─────────────────────────────────────────
# Peter, 2026-11-12: alpha testers are "free for one month after the
# opening, then the discounted price"; 2026-11-13, on when the month
# starts: "at the beta release date I'll decide later". So it starts on
# one date for every tester, which the owner gives to
# `flask alpha-free-month --apply --start YYYY-MM-DD` (00:00 UTC that
# day), and ends ALPHA_FREE_MONTH_DAYS later, whether or not they visit.
# The command moves every approved account on the hand-set "alpha" plan
# to "free" and stores the dates on it. Before the start nothing changes
# for a tester: they keep the alpha plan's access (User.plan_set_by_hand).
# During the month they have everything without a card and may subscribe
# at any time, at the discounted price without a code; the first payment
# is due when the month ends. After it, without a subscription, the
# account is lapsed: reading, writing and export stay.

# Stripe refuses a Checkout trial that ends less than 48 hours ahead (one
# hour of margin on top). A tester who subscribes in the month's last 49
# hours gets the rest of those hours free: the least Stripe allows for a
# trial; none at all after the end.
CHECKOUT_TRIAL_MIN = timedelta(hours=49)


def parse_start_date(raw):
    """The free month's start for "YYYY-MM-DD": 00:00 UTC that day, as a
    naive UTC datetime. Raises ValueError for anything else."""
    return datetime.strptime((raw or "").strip(), "%Y-%m-%d")


def free_month_dates(start, config=None):
    """(start, end) of the free month that starts at *start*."""
    cfg = _config(config)
    return start, start + timedelta(
        days=int(cfg.get("ALPHA_FREE_MONTH_DAYS") or 30))


def alpha_checkout_trial_end(user, config=None, now=None):
    """The trial a Checkout gives an alpha tester who subscribes during
    the free month: until the month ends (at least CHECKOUT_TRIAL_MIN
    ahead), so the first payment is due then. None once the month is
    over, or when the account used a trial already: a later subscription
    is charged at checkout. (Before the month starts a tester keeps the
    alpha plan's access, and Checkout refuses them as it refuses alpha.)"""
    if getattr(user, "subscription_trial_used", False):
        return None
    if not getattr(user, "alpha_free_month_active", False):
        return None
    now = now or datetime.utcnow()
    return max(user.alpha_free_until, now + CHECKOUT_TRIAL_MIN)


def alpha_free_month_candidates(usernames=None):
    """Accounts the free month goes to: approved, not admins, on the
    hand-set "alpha" plan, not being deleted, not given it already.
    *usernames* narrows the list."""
    from backend.models import User
    q = User.query.filter(User.approved.is_(True),
                          User.is_admin.isnot(True),
                          User.plan == "alpha",
                          User.deleted_at.is_(None),
                          User.alpha_free_granted_at.is_(None))
    if usernames:
        q = q.filter(User.username.in_(list(usernames)))
    return q.order_by(User.id).all()


def current_free_month():
    """(start, end) of the free month the command gave, or None before it
    ran (or after a full --undo). Every tester has the same dates: the
    command refuses a second date (--change-date moves them all)."""
    from backend.models import User
    row = (User.query
           .filter(User.alpha_free_granted_at.isnot(None),
                   User.alpha_free_start.isnot(None),
                   User.alpha_free_until.isnot(None))
           .order_by(User.alpha_free_granted_at.desc(), User.id.desc())
           .first())
    return (row.alpha_free_start, row.alpha_free_until) if row else None


def convert_if_alpha(user, config=None, now=None):
    """Once the free month has been given, an account that comes in on
    the "alpha" plan later (approved after a wait, or restored from a
    deletion it was waiting on when the command ran) gets the same month,
    with the same dates, as the command would have given it. Does not
    commit: the caller's change and this go together. Returns True when
    it converted the account."""
    if user is None or not payments_enabled(config):
        return False
    if (user.plan != "alpha" or not user.approved or user.is_admin
            or user.deleted_at is not None
            or user.alpha_free_granted_at is not None):
        return False
    month = current_free_month()
    if month is None:
        return False
    user.plan = "free"
    user.alpha_free_granted_at = now or datetime.utcnow()
    user.alpha_free_start, user.alpha_free_until = month
    return True


def alpha_testers(usernames=None):
    """Accounts given the free month already (*usernames* narrows)."""
    from backend.models import User
    q = User.query.filter(User.alpha_free_granted_at.isnot(None))
    if usernames:
        q = q.filter(User.username.in_(list(usernames)))
    return q.order_by(User.id).all()


def grant_alpha_free_month(users, start, config=None, now=None):
    """Move *users* from "alpha" to "free" with the free month from
    *start* (naive UTC). Commits. Returns how many changed."""
    from backend.extensions import db
    now = now or datetime.utcnow()
    begin, end = free_month_dates(start, config)
    for user in users:
        user.plan = "free"
        user.alpha_free_granted_at = now
        user.alpha_free_start = begin
        user.alpha_free_until = end
    db.session.commit()
    return len(users)


def move_alpha_free_month(users, start, config=None):
    """Give *users* (every alpha tester) the free month from *start*
    instead. Their plan and anything they subscribed stay as they are.
    Commits. Returns how many changed."""
    from backend.extensions import db
    begin, end = free_month_dates(start, config)
    for user in users:
        user.alpha_free_start = begin
        user.alpha_free_until = end
    db.session.commit()
    return len(users)


def undo_alpha_free_month(usernames=None):
    """Put alpha testers back on the hand-set "alpha" plan and forget
    their free month. Left as they are: an account whose plan was changed
    since (an admin's choice), and one with a live subscription (it would
    keep being charged for what "alpha" gives free; cancel it first).
    Commits. Returns (how many changed, [(username, why skipped)])."""
    from backend.extensions import db
    from backend.models import SUBSCRIPTION_GRANTING_STATUSES
    done, skipped = 0, []
    for user in alpha_testers(usernames):
        if user.plan != "free":
            skipped.append((user.username, f"its plan is {user.plan} now"))
            continue
        if user.subscription_status in SUBSCRIPTION_GRANTING_STATUSES:
            skipped.append((user.username, "it has a live subscription"))
            continue
        user.plan = "alpha"
        user.alpha_free_granted_at = None
        user.alpha_free_start = None
        user.alpha_free_until = None
        done += 1
    db.session.commit()
    return done, skipped


def checkout_levels(user):
    """The levels the account may choose without a code: an alpha tester
    has the discounted one too, first."""
    if getattr(user, "is_alpha_tester", False):
        return ("discounted",) + OFFERED_LEVELS
    return OFFERED_LEVELS


def level_price_cents(level, config=None):
    cfg = _config(config)
    return int(cfg.get(f"SUBSCRIPTION_PRICE_CENTS_{level.upper()}") or 0)


def subscription_state(user, config=None):
    """The account's subscription as the clients show it (web Settings,
    iPhone Settings). Ids and payment details stay on the server."""
    cfg = _config(config)
    state = access_state(user, cfg)
    alpha = bool(user.is_alpha_tester)
    return {
        # "set_by_hand" (also admins), "active", "lapsed", "none".
        "state": "set_by_hand" if state == "full" else state,
        "level": user.subscription_level,
        "status": user.subscription_status or "none",
        "current_period_end": iso_utc(user.subscription_period_end),
        "trial_end": iso_utc(user.subscription_trial_end),
        "cancel_at_period_end": bool(user.subscription_cancel_at_period_end),
        # An alpha tester's free month takes the trial's place.
        "trial_available": not user.subscription_trial_used and not alpha,
        "trial_days": int(cfg.get("SUBSCRIPTION_TRIAL_DAYS") or 0),
        # A trial was cancelled because its card had one on another
        # account (#697): the page says so, and that a subscription
        # without a trial is possible.
        "trial_card_refused": bool(user.subscription_trial_card_refused_at),
        # The alpha free month: whether this is an alpha tester, when the
        # month starts and ends (one date for every tester), whether it is
        # still to come (the alpha plan's access until then) or runs.
        "alpha_tester": alpha,
        "free_month_upcoming": bool(user.alpha_free_month_upcoming),
        "free_month_active": bool(user.alpha_free_month_active),
        "free_month_start": iso_utc(user.alpha_free_start) if alpha else None,
        "free_month_until": iso_utc(user.alpha_free_until) if alpha else None,
        "plan_set_by_hand": bool(user.plan_set_by_hand),
        "ai_replies": ai_replies_allowed(user, cfg),
        # Live at Stripe (it renews) but giving nothing: a failed payment
        # past its grace days, or withdrawn after a dispute or a refund.
        "access": bool(user.subscription_grants_access),
        "has_customer": bool(user.stripe_customer_id),
    }


def user_payload_fields(user, config=None):
    """Extra fields of the signed-in user while payments are on; none
    while they are off, so the payload is exactly what it was."""
    if not payments_enabled(config):
        return {}
    from backend.utils.referrals import referrals_enabled
    return {
        "payments_enabled": True,
        "subscription": subscription_state(user, config),
        # The invite link on the Account page (#439).
        "referrals_enabled": referrals_enabled(config),
    }
