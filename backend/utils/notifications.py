"""Producers for targeted in-app notifications (#207).

One helper, called from wherever background work finishes. Notifications
are the persistent sibling of the transient completion toasts — they
survive until the user opens Loore and marks them read. Bodies are
system-generated template text only; never put user content in them.
"""
import logging

from backend.extensions import db
from backend.models import UserNotification

logger = logging.getLogger(__name__)


def notify_user(user_id, type, title, body=None, link=None,
                replace_unread=True):
    """Create a targeted notification. With replace_unread (default), an
    existing UNREAD notification of the same type for this user is updated
    in place instead of stacking ("your profile was updated" twice is one
    fact, not two).

    Commits as part of the caller's session — call it right before/with
    the caller's own commit. Never raises: a failed notification must not
    break the work it announces.
    """
    try:
        notification = None
        if replace_unread:
            notification = UserNotification.query.filter_by(
                user_id=user_id, type=type, status="unread").first()
        if notification is None:
            notification = UserNotification(user_id=user_id, type=type)
            db.session.add(notification)
        notification.title = title
        notification.body = body
        notification.link = link
        db.session.commit()
        return notification
    except Exception:
        db.session.rollback()
        logger.exception(
            "Failed to create %s notification for user %s", type, user_id)
        return None


def notify_profile_ready(user_id):
    return notify_user(
        user_id,
        type="profile_ready",
        title="Your profile has been updated",
        body="Loore re-read your recent writing and refreshed your "
             "profile. Take a look — and correct anything it got wrong.",
        link="/profile",
    )
