"""Shared profile version rules.

The version a user sees ("v7") is the position of a profile among their
VISIBLE profiles — pipeline intermediates are not editions. The rule
lives here so every surface that names a version (the profile page, the
profile-updated notification) says the same number.

Also shared by the profile routes and the profile jobs (#183): when two
profile texts count as the same, and which source coverage a version the
user writes (an edit, a new profile, a revert) carries.
"""
from backend.extensions import db
from backend.models import UserProfile

# Generation steps hidden from the history view: 'iterative' rows are the
# chunk-by-chunk build steps of a full generation and 'update' rows are the
# pre-merge increments — both are pipeline intermediates, not editions of
# the profile. Everything else stays visible: 'initial' (complete
# single-pass build), 'integration' (merged update), 'revert' (a user
# action), and legacy NULL-typed rows (profiles predating the column).
#
# The rows of a large build's window set (#545, tasks/profile_windows.py)
# are intermediates too: a "window" is one window's version, built from
# that window alone; a "window_skip" marks a window given up after its
# outputs failed; a "group" merges consecutive windows. They are never the
# user's profile: only the set's final "integration" is.
WINDOW_TYPES = ("window", "window_skip")
SET_INTERNAL_TYPES = WINDOW_TYPES + ("group",)
PROFILE_HISTORY_HIDDEN_TYPES = ("iterative", "update") + SET_INTERNAL_TYPES


def not_set_internal(column=None):
    """SQL criterion: a profile row that is not a window set's
    intermediate (legacy NULL-typed rows included)."""
    column = column if column is not None else UserProfile.generation_type
    return db.or_(column.is_(None), column.notin_(SET_INTERNAL_TYPES))


def current_profile_query(user_id):
    """The user's profile rows that can be their current profile, newest
    first: everything but a window set's intermediates. Every reader of
    "the user's profile" (replies, recent context, the profile page, session
    pins) goes through this, so a large build in progress never serves one
    window's version: the profile in use stays the previous one until the
    build's final merge is saved. Callers keep their own ai_usage filter."""
    return UserProfile.query.filter(
        UserProfile.user_id == user_id, not_set_internal(),
    ).order_by(UserProfile.created_at.desc())


def profile_lines(text):
    """A profile's lines as an edit comparison sees them: trailing spaces
    and blank lines left out, since they change nothing a model reads."""
    return [line.rstrip() for line in (text or "").splitlines()
            if line.strip()]


def same_profile_text(before, after):
    """Whether two profile texts differ only in whitespace: blank lines,
    trailing spaces or a trailing newline."""
    return profile_lines(before) == profile_lines(after)


def continue_boundary(user, profile):
    """The boundary the continue rule (tasks/exports.should_continue_chain)
    uses for ``profile``: when the window it covers was rendered. A version
    saved before that column existed has no render time; on a pinned
    (pre-filled) account the rule then uses its save time, and on any
    other account it has no boundary (None)."""
    boundary = getattr(profile, "source_rendered_at", None)
    if boundary is None and getattr(user, "profile_force_batch", False):
        boundary = getattr(profile, "created_at", None)
    return boundary


def coverage_of(user, version):
    """The source coverage of a version the user makes on top of
    ``version``: an edit of it, a revert to it, or a new profile written
    while it is the newest version (POST /profile). The new version covers
    the same writing, so it carries the same cutoff, units and origin
    stats, and the same continue-rule boundary, and the update gates decide
    as they did before the user's version existed.

    An integration has no render time of its own (its source_rendered_at
    is None). It merges the chain that ends at its parent, so its boundary
    is the parent's. Copying the integration's None instead made a pinned
    account's continue rule measure from the time of the edit, which
    seeded an extra update after every edit (review of #414, finding 3)."""
    bounded = version
    if version.generation_type == "integration" and version.parent_profile:
        bounded = version.parent_profile
    return {
        "source_tokens_used": version.source_tokens_used,
        "source_data_cutoff": version.source_data_cutoff,
        "source_origin_stats": version.source_origin_stats,
        # No cutoff, nothing to continue: no boundary either.
        "source_rendered_at": (continue_boundary(user, bounded)
                               if version.source_data_cutoff else None),
    }


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


# ── A large build's window set (#545) ────────────────────────────────────
# Metadata lookups only (no content is loaded or decrypted), shared by the
# update-base rule (tasks/exports.profile_update_base) and the window
# pipeline (tasks/profile_windows).

def _ai_readable():
    from backend.utils.privacy import AI_ALLOWED
    return UserProfile.ai_usage.in_(AI_ALLOWED)


def current_window_set(user_id):
    """The window set of the user's newest AI-readable window (or skipped
    window), or None when they have none."""
    row = db.session.query(UserProfile.window_set).filter(
        UserProfile.user_id == user_id,
        UserProfile.generation_type.in_(WINDOW_TYPES),
        UserProfile.window_set.isnot(None),
        _ai_readable(),
    ).order_by(UserProfile.created_at.desc()).first()
    return row[0] if row else None


def window_set_tip(user_id, set_id):
    """The set's AI-readable window (or skipped window) with the latest
    cutoff: the end of the writing the set has read."""
    if not set_id:
        return None
    return UserProfile.query.filter(
        UserProfile.user_id == user_id,
        UserProfile.window_set == set_id,
        UserProfile.generation_type.in_(WINDOW_TYPES),
        _ai_readable(),
    ).order_by(UserProfile.source_data_cutoff.desc(),
               UserProfile.created_at.desc()).first()


def latest_set_final(user_id, set_id):
    """The set's newest AI-readable final merge (an "integration" row
    carrying the set's id), or None."""
    if not set_id:
        return None
    return UserProfile.query.filter(
        UserProfile.user_id == user_id,
        UserProfile.window_set == set_id,
        UserProfile.generation_type == "integration",
        _ai_readable(),
    ).order_by(UserProfile.created_at.desc()).first()
