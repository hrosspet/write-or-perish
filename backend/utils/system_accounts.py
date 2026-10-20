"""System accounts for cost attribution (#207, #268).

Poll drafts run over a user's context but serve the admin's feedback ask,
so their cost must not eat the answering user's spend budget. It lands on
a dedicated, non-loginable system account instead — visible in the admin
users table like any other account.

A data purge (#268) keeps the purged user's cost rows but moves them to a
second system account, so spend totals stay true while no row names the
person any more.

The usernames sit inside the protected brand namespace ("loore" is
blocked for registration by BRAND_SUBSTRING_RE), so no real user can ever
claim them; we create them directly, bypassing signup validation on
purpose.
"""
import logging

from backend.extensions import db
from backend.models import User

logger = logging.getLogger(__name__)

POLL_SYSTEM_USERNAME = "loore-polls"
# Cost rows of purged users (#268), detached from the person.
ERASED_SYSTEM_USERNAME = "loore-erased"
SYSTEM_USERNAMES = (POLL_SYSTEM_USERNAME, ERASED_SYSTEM_USERNAME)


def _get_or_create_system_user(username, label):
    """Get or lazily create a system account.

    approved=False (cannot log in, blocked by the before_request gate) and
    plan="free" (excluded from profile generation eligibility). Race-safe:
    a concurrent create loses on the unique username and re-reads.
    """
    user = User.query.filter_by(username=username).first()
    if user:
        return user
    try:
        user = User(username=username, approved=False,
                    plan="free", default_ai_usage="none")
        db.session.add(user)
        db.session.commit()
        logger.info("Created %s system account (user %s)", label, user.id)
        return user
    except Exception:
        db.session.rollback()
        return User.query.filter_by(username=username).first()


def get_poll_system_user():
    """Get or lazily create the poll-costs system account."""
    return _get_or_create_system_user(POLL_SYSTEM_USERNAME, "poll")


def get_erased_system_user():
    """Get or lazily create the account that holds purged users' cost
    rows (#268)."""
    return _get_or_create_system_user(ERASED_SYSTEM_USERNAME, "erased-costs")
