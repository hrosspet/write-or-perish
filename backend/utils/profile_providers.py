"""Whether a user's profile build may use both providers' models
(Peter, 2026-11-05).

A user's writing reaches the other provider only when everything a build
can read for them is public, or when they agreed to both providers
(User.profile_both_providers, #599). "Public only" is decided from
metadata, never from the content, and any doubt counts as private.
Everything here only ever pushes a user towards private.

The question that asks the user (#599) and the rules for when it is
asked are at the end of this module (``both_providers_ask``).
"""
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import func, not_, or_
from sqlalchemy.orm import aliased

from backend.extensions import db
from backend.models import Node, NodeVersion, User, UserProfile, UserPrompt

# Node.provenance values that are public by construction (models.Node):
# Loore fetched the tweets from the Community Archive or the X API.
PUBLIC_PROVENANCES = ("prefill_ca", "prefill_x")
# The origin (Node.origin, "loore" when NULL) of pre-filled tweets, as
# profiles record it in source_origin_stats.
PREFILL_ORIGIN = "twitter"


def entries_public_only(user, public_privacy=True):
    """Whether every entry a build can read for this user is public:

    - each entry of the user (their own and the AI replies they own,
      deleted ones included, since an earlier version may have read them)
      was fetched from a public source (PUBLIC_PROVENANCES) and its text
      is unchanged since (no NodeVersion row, which every edit writes, and
      not a deleted entry whose text and edit history are already wiped),
      or it is public while the user shares publicly;
    - no other user's entry is a parent or a reply of theirs (the export
      reads the threads the user took part in);
    - no profile prompt is their own.

    A user with no entries is not public-only. ``public_privacy=False``
    counts only fetched entries as public (the backfill's reading of the
    writing before this rule existed)."""
    from backend.utils.privacy import PrivacyLevel
    mine = or_(Node.user_id == user.id, Node.human_owner_id == user.id)
    fetched = func.coalesce(Node.provenance, "").in_(PUBLIC_PROVENANCES)
    public = fetched
    if public_privacy and user.public_sharing_enabled:
        public = or_(public, func.coalesce(Node.privacy_level, "")
                     == PrivacyLevel.PUBLIC.value)
    if db.session.query(Node.id).filter(mine).first() is None:
        return False
    if db.session.query(Node.id).filter(mine, not_(public)).first():
        return False
    # A fetched entry whose text the user changed (an edit through the
    # node route writes the previous text as a NodeVersion row).
    if db.session.query(Node.id).join(
            NodeVersion, NodeVersion.node_id == Node.id).filter(
            mine, fetched).first():
        return False
    # A deleted fetched entry whose text the cleanup already wiped: its
    # edit history went with it, so nobody can tell it was unchanged.
    if db.session.query(Node.id).filter(
            mine, fetched, Node.deleted_at.isnot(None),
            Node.content.is_(None)).first():
        return False
    other = aliased(Node)
    owner = func.coalesce(other.human_owner_id, other.user_id)
    if db.session.query(Node.id).join(
            other, Node.parent_id == other.id).filter(
            mine, owner != user.id).first():
        return False
    if db.session.query(Node.id).join(
            other, other.parent_id == Node.id).filter(
            mine, owner != user.id).first():
        return False
    return UserPrompt.query.filter_by(user_id=user.id).first() is None


def profiles_public_only(user):
    """Whether every profile row the user has came from pre-filled public
    tweets only, so a build that merges them sends nothing private: no row
    is the user's own text, and every row records its sources
    (source_origin_stats) as pre-filled tweets alone ("twitter") on an
    account Loore pre-filled. A row without that record counts as
    private. Entries that were read and then purged leave their origin in
    the rows that read them."""
    rows = (UserProfile.query.with_entities(
        UserProfile.generated_by, UserProfile.source_origin_stats)
        .filter(UserProfile.user_id == user.id).all())
    if rows and not user.prefilled_handle:
        return False
    for generated_by, stats in rows:
        if generated_by == "user" or not stats:
            return False
        if set(stats) - {PREFILL_ORIGIN}:
            return False
    return True


def never_signed_in(user):
    """Whether nobody has ever signed into this account, as far as the
    record can tell (utils/activity.sign_in_status "never"): no sign-in
    and no terms acceptance on record, on an account newer than that
    record. An own archive upload needs a signed-in session, so such an
    account never uploaded one: a pre-fill waiting for its owner, or a
    lab account built from the Community Archive. Any other account
    counts as one that may have uploaded its own archive."""
    from backend.utils.activity import sign_in_status
    return sign_in_status(user) == "never"


def writing_is_public_only(user):
    """Whether everything a build of this user can read is public, so its
    windows and merges may go to either provider: an account nobody has
    signed into (``never_signed_in``; an account that ever uploaded its
    own archive is private), the user's entries (``entries_public_only``)
    and every profile row (``profiles_public_only``), and no profile pass
    has ever read private writing for them (User.profile_private_read,
    sticky; backfilled for the accounts that existed before it,
    scripts/backfill_profile_private_read.py). Every part only pushes
    towards private, and none depends on the backfill having run."""
    if user.profile_private_read or not never_signed_in(user):
        return False
    return entries_public_only(user) and profiles_public_only(user)


def needs_private_mark(user):
    """The backfill's test for an account from before the mark: someone
    signed in (it may have uploaded its own archive), an entry that is
    not an unchanged pre-filled tweet (deleted ones included), or a
    profile row not proven to have read pre-filled tweets alone. An
    account with no entries and no profile rows that nobody signed into
    is left unmarked (a pre-fill still to come)."""
    if not never_signed_in(user):
        return True
    has_entries = db.session.query(Node.id).filter(or_(
        Node.user_id == user.id, Node.human_owner_id == user.id)).first()
    has_profiles = UserProfile.query.filter_by(user_id=user.id).first()
    if has_entries is None and has_profiles is None:
        return False
    if has_entries is not None and not entries_public_only(
            user, public_privacy=False):
        return True
    return not profiles_public_only(user)


def note_private_read(user):
    """Called where a profile pass reads the user's writing (a batch
    submission, a saved version, a window pass): when their entries are
    not public-only, mark the user for good (User.profile_private_read).
    Nothing clears the mark automatically. The caller commits."""
    if user is None or user.profile_private_read:
        return
    if not entries_public_only(user):
        user.profile_private_read = True


# ── The question (#599, Peter 2026-11-05) ────────────────────────────────
# "Is it fine for you if we use both Anthropic and OpenAI for the
# background tasks, or do you want to stick to just one of them?"
# Until a user says yes, their writing goes to their own provider only.

# After a no, an import asks again only once the no is at least this old
# (Peter, 2026-11-05: "at least a month old"; a month read as 30 days).
REASK_AFTER_NO = timedelta(days=30)

# Where the client asks: "model" when the user changes their preferred
# model on the Account page, "visit" when they open Loore, "import" on an
# import's result.
ASK_CONTEXTS = ("model", "visit", "import")


def agreed_to_both(user):
    """The user's yes as the database holds it now, not as this session
    loaded it: a no given while a profile pass runs counts from the next
    check (every slice of requests is checked again before it is sent)."""
    if user is None or user.id is None:
        return False
    value = (db.session.query(User.profile_both_providers)
             .filter(User.id == user.id).scalar())
    return bool(value)


def both_providers_answer(user):
    """True (both), False (one provider) or None (never answered: "we
    don't know", not "no")."""
    if user is None or user.profile_both_providers_answered_at is None:
        return None
    return bool(user.profile_both_providers)


def set_both_providers(user, value, now=None):
    """Record the user's own answer and when they gave it. The caller
    commits."""
    user.profile_both_providers = bool(value)
    user.profile_both_providers_answered_at = now or datetime.utcnow()


def has_entries(user_id):
    """Whether the user has made at least one entry, written in Loore or
    imported (Peter: "after the 1st reflection", and an import counts):
    a user node that is not deleted and not the prompt node a Voice, Text
    or Glean session starts with. Metadata only; stops at the first row."""
    return db.session.query(Node.id).filter(
        Node.user_id == user_id, Node.node_type == "user",
        Node.deleted_at.is_(None), Node.prompt_key.is_(None),
    ).first() is not None


def profile_provider(user):
    """The provider of the user's preferred model (else the default
    model): "anthropic" or "openai", the one a no keeps them to."""
    from backend.utils.llm_nodes import default_model_for
    models = current_app.config.get("SUPPORTED_MODELS") or {}
    return (models.get(default_model_for(user)) or {}).get("provider")


def both_providers_ask(user, now=None):
    """Where the client should ask the question now, by context
    (ASK_CONTEXTS). The rules (Peter, 2026-11-05):

    - never a user who said yes;
    - never an account whose AI usage is None (no job runs for it);
    - "model": the user has never answered;
    - "visit": the user has never answered and has at least one entry,
      written or imported;
    - "import": the user has never answered, or said no at least
      REASK_AFTER_NO ago.

    Closing the question without an answer records nothing, so the next
    occasion asks again. The client decides which page may show it (never
    during a recording or a Voice or Text session)."""
    from backend.utils.privacy import account_allows_ai
    ask = dict.fromkeys(ASK_CONTEXTS, False)
    if user is None or not account_allows_ai(user):
        return ask
    answered = user.profile_both_providers_answered_at
    if answered is None:
        ask["model"] = True
        ask["import"] = True
        ask["visit"] = has_entries(user.id)
        return ask
    if user.profile_both_providers:
        return ask
    now = now or datetime.utcnow()
    ask["import"] = now - answered >= REASK_AFTER_NO
    return ask


def ask_after_import(user, ai_usage, added):
    """Whether an import's result asks the question: the import added
    entries that AI may read (its AI usage allows it), and the "import"
    rule of ``both_providers_ask`` holds. Best-effort: False on any
    error, so an import's result never fails over it."""
    from backend.utils.privacy import AI_ALLOWED
    if not added or ai_usage not in AI_ALLOWED:
        return False
    try:
        return bool(both_providers_ask(user)["import"])
    except Exception:
        db.session.rollback()
        current_app.logger.warning(
            "Both-providers check after import failed", exc_info=False)
        return False
