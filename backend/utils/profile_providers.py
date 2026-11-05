"""Whether a user's profile build may use both providers' models
(Peter, 2026-11-05).

A user's writing reaches the other provider only when everything a build
can read for them is public, or when they agreed to both providers
(User.profile_both_providers, #599). "Public only" is decided from
metadata, never from the content, and any doubt counts as private.
Everything here only ever pushes a user towards private.
"""
from sqlalchemy import func, not_, or_
from sqlalchemy.orm import aliased

from backend.extensions import db
from backend.models import Node, NodeVersion, UserProfile, UserPrompt

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
