"""System accounts for cost attribution (#207).

Poll drafts run over a user's context but serve the admin's feedback ask,
so their cost must not eat the answering user's spend budget. It lands on
a dedicated, non-loginable system account instead — visible in the admin
users table like any other account.

The username sits inside the protected brand namespace ("loore" is
blocked for registration by BRAND_SUBSTRING_RE), so no real user can ever
claim it; we create it directly, bypassing signup validation on purpose.
"""
import logging

from backend.extensions import db
from backend.models import User

logger = logging.getLogger(__name__)

POLL_SYSTEM_USERNAME = "loore-polls"


def get_poll_system_user():
    """Get or lazily create the poll-costs system account.

    approved=False (cannot log in, blocked by the before_request gate) and
    plan="free" (excluded from profile generation eligibility). Race-safe:
    a concurrent create loses on the unique username and re-reads.
    """
    user = User.query.filter_by(username=POLL_SYSTEM_USERNAME).first()
    if user:
        return user
    try:
        user = User(username=POLL_SYSTEM_USERNAME, approved=False,
                    plan="free", default_ai_usage="none")
        db.session.add(user)
        db.session.commit()
        logger.info("Created poll system account (user %s)", user.id)
        return user
    except Exception:
        db.session.rollback()
        return User.query.filter_by(username=POLL_SYSTEM_USERNAME).first()
