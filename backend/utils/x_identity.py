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
    # Paid X user reads made (0 or 1). Already logged unless the caller
    # asked to defer the charge (``defer_cost``), in which case it bills.
    paid_reads: int = 0


class XIdUnresolved(Exception):
    """The handle did not resolve to exactly one X id.

    ``reason`` is one of: not-in-archive, archive-error, ambiguous,
    not-on-x, x-error. ``message`` is safe to show an admin."""

    def __init__(self, reason, message, paid_reads=0):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.paid_reads = paid_reads  # see ResolvedX.paid_reads


def log_user_read(cost_user_id, handle):
    """Put one paid X user read for ``handle`` on ``cost_user_id``'s ledger."""
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
                 creds=None, timeout=LOOKUP_TIMEOUT, defer_cost=False):
    """ResolvedX for ``handle`` or raise XIdUnresolved.

    Community Archive first (free, exact username match) unless ``archive``
    is false (the paid X pre-fill needs X's own record). A handle the
    archive lacks is looked up on the X API only when ``x_lookup`` is true;
    that read is logged to APICostLog against ``cost_user_id`` (required
    then) whenever the request was sent — an error or a timeout on the
    read included, since X may have counted it. With ``defer_cost`` the
    read is not logged here but reported as ``paid_reads`` on the result
    or the exception, for a caller whose account to bill does not exist
    yet (the whitelist bills the account it creates). ``timeout`` is one
    budget for all the calls together."""
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

    if cost_user_id is None and not defer_cost:
        raise ValueError("a paid X lookup must name the user it is billed to")
    from flask import current_app
    from backend.utils import x_api
    if creds is None:
        creds = (current_app.config.get("TWITTER_API_KEY"),
                 current_app.config.get("TWITTER_API_SECRET"))
    def settle(sent):
        """Log the read now unless deferred; either way say how many."""
        if sent and not defer_cost:
            log_user_read(cost_user_id, handle)
        return 1 if sent else 0

    step_timeout = budget("x-error")
    try:
        account = x_api.lookup_user(handle, creds, timeout=step_timeout)
    except x_api.XApiError as e:
        raise XIdUnresolved("x-error", str(e), paid_reads=settle(e.sent)) from e
    except Exception as e:
        logger.warning("X API lookup failed for @%s: %s", handle, e)
        raise XIdUnresolved("x-error", f"X API lookup failed: {e}",
                            paid_reads=settle(False)) from e
    paid = settle(True)
    if account is None:
        raise XIdUnresolved(
            "not-on-x", f"@{handle} is not on X (suspended, renamed, or never existed).",
            paid_reads=paid)
    return ResolvedX(str(account["id"]), "x-api", account["username"],
                     account.get("name"), account, paid_reads=paid)
