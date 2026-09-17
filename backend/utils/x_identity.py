"""One place that turns an X handle into the numeric X id Loore keys X
logins on (see ``_create_x_user`` in routes/auth.py: the handle never finds
an account, only the id does).

Used by the admin whitelist, the placeholder backfill script and both
pre-fills. The archive step is free; the X API step is one paid user read,
taken only when the caller asks for it and always billed to the user id
the caller names (the admin doing it, or the pre-filled account — never a
placeholder that is not the subject of the action).
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# One deadline for the whole resolution — archive read, then X token and
# user read — well under the 60 s nginx and the frontend give a request: a
# lookup that outlives the request would create the placeholder after the
# admin already saw an error, and a retry would then collide with it.
LOOKUP_TIMEOUT = 20


@dataclass
class ResolvedX:
    x_id: str
    source: str                   # "community-archive" | "x-api"
    username: str                 # as the source spells it
    display_name: Optional[str]
    # The source's own record, for callers that need more than the id
    # (archive: num_tweets; X: tweet_count, protected). Never re-resolve
    # the handle after this — a second lookup can name another account.
    account: dict = field(default_factory=dict)


class XIdUnresolved(Exception):
    """The handle did not resolve to exactly one X id.

    ``reason`` is one of: not-in-archive, archive-error, ambiguous,
    not-on-x, x-error. ``message`` is safe to show an admin."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason
        self.message = message


def _log_user_read(cost_user_id, handle):
    from backend.extensions import db
    from backend.models import APICostLog
    from backend.utils import x_api
    db.session.add(APICostLog(
        user_id=cost_user_id, model_id="x-api/user-lookup",
        request_type="x_id_lookup", request_ref=f"@{handle}"[:64],
        input_tokens=0, output_tokens=0,
        cost_microdollars=x_api.cost_microdollars(0, user_reads=1)))
    db.session.commit()


def resolve_x_id(handle, *, x_lookup=False, archive=True, cost_user_id=None,
                 creds=None, timeout=LOOKUP_TIMEOUT):
    """ResolvedX for ``handle`` or raise XIdUnresolved.

    Community Archive first (free, exact username match) unless ``archive``
    is false (the paid X pre-fill needs X's own record). A handle the
    archive lacks is looked up on the X API only when ``x_lookup`` is true;
    that read is logged to APICostLog against ``cost_user_id`` (required
    then) whenever the request was sent — an error or a timeout on the
    read included, since X may have counted it. ``timeout`` is one budget
    for all the calls together."""
    from backend.utils import community_archive as ca

    handle = (handle or "").strip().lstrip("@")
    if not handle:
        raise XIdUnresolved("not-in-archive", "Handle is required.")
    deadline = time.monotonic() + timeout

    def budget(reason):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise XIdUnresolved(reason, f"Looking up @{handle} took longer than {timeout} s.")
        return remaining

    archive_error = None
    if archive:
        step_timeout = budget("archive-error")
        try:
            account = ca.fetch_account(handle, timeout=step_timeout)
        except ca.CommunityArchiveError as e:  # several exact matches
            raise XIdUnresolved("ambiguous", str(e)) from e
        except Exception as e:  # network / provider errors
            logger.warning("Community Archive lookup failed for @%s: %s", handle, e)
            account, archive_error = None, e
        if account:
            return ResolvedX(str(account["account_id"]), "community-archive",
                             account.get("username") or handle,
                             account.get("account_display_name"), account)

    if not x_lookup:
        if archive_error is not None:
            raise XIdUnresolved(
                "archive-error", f"Community Archive lookup failed: {archive_error}")
        raise XIdUnresolved(
            "not-in-archive", f"@{handle} is not in the Community Archive.")

    if cost_user_id is None:
        raise ValueError("a paid X lookup must name the user it is billed to")
    from flask import current_app
    from backend.utils import x_api
    if creds is None:
        creds = (current_app.config.get("TWITTER_API_KEY"),
                 current_app.config.get("TWITTER_API_SECRET"))
    step_timeout = budget("x-error")
    sent = False
    try:
        account = x_api.lookup_user(handle, creds, timeout=step_timeout)
        sent = True
    except x_api.XApiError as e:
        sent = e.sent
        raise XIdUnresolved("x-error", str(e)) from e
    except Exception as e:
        logger.warning("X API lookup failed for @%s: %s", handle, e)
        raise XIdUnresolved("x-error", f"X API lookup failed: {e}") from e
    finally:
        if sent:
            _log_user_read(cost_user_id, handle)
    if account is None:
        raise XIdUnresolved(
            "not-on-x", f"@{handle} is not on X (suspended, renamed, or never existed).")
    return ResolvedX(str(account["id"]), "x-api", account["username"],
                     account.get("name"), account)
