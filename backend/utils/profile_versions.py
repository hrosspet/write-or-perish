"""Shared profile version numbering.

The version a user sees ("v7") is the position of a profile among their
VISIBLE profiles — pipeline intermediates are not editions. The rule
lives here so every surface that names a version (the profile page, the
profile-updated notification) says the same number.
"""
from backend.extensions import db
from backend.models import UserProfile

# Generation steps hidden from the history view: 'iterative' rows are the
# chunk-by-chunk build steps of a full generation and 'update' rows are the
# pre-merge increments — both are pipeline intermediates, not editions of
# the profile. Everything else stays visible: 'initial' (complete
# single-pass build), 'integration' (merged update), 'revert' (a user
# action), and legacy NULL-typed rows (profiles predating the column).
PROFILE_HISTORY_HIDDEN_TYPES = ("iterative", "update")


def _visible_criteria(user_id):
    return (
        UserProfile.user_id == user_id,
        db.or_(
            UserProfile.generation_type.is_(None),
            UserProfile.generation_type.notin_(PROFILE_HISTORY_HIDDEN_TYPES),
        ),
    )


def visible_profiles_query(user_id):
    """The user's profile editions, newest first."""
    return UserProfile.query.filter(*_visible_criteria(user_id)).order_by(
        UserProfile.created_at.desc()
    )


def current_profile_version(user_id):
    """(version_number, created_at) of the user's newest edition, or
    (None, None) when they have no profile yet.

    Column-only query on purpose: profile content is KMS-encrypted, and a
    metadata lookup must never pull the blob, let alone decrypt it.
    """
    base = db.session.query(
        UserProfile.id, UserProfile.created_at
    ).filter(*_visible_criteria(user_id))
    latest = base.order_by(UserProfile.created_at.desc()).first()
    if latest is None:
        return None, None
    return base.count(), latest.created_at
