"""The admin's approval while payments are on (#697).

Peter, 2026-11-13: "waiting for my approval is a bad first UX". With
payments on, the trial with a card is the filter and nobody waits for the
admin. With payments off, nothing here changes anything: an account waits
for the admin's approval (``User.approved``) as it always did.

With payments on:

* An account that was never approved is let in at its first request (the
  app's before_request, ``let_in``). It is approved like any other account
  and goes on to the step where it starts the trial: an account that never
  had a subscription sees only the subscribe page (utils/subscriptions).
* Whitelisting an X handle or starting a pre-fill approves the account
  ahead of its owner (``let_in`` from routes/admin): it is ready at their
  first sign-in. No welcome email is sent by it.
* The admin's switch on the Users tab blocks and unblocks an account
  (routes/admin.toggle_user_status). A blocked account is one the admin
  turned off: ``approved`` False with ``deactivated_at`` set, which the
  Deactivate button has always written and which the profile jobs already
  treat as the admin's pause (tasks/profile_batch._held_users). It is never
  let in by the rules above, cannot start a trial, and sees only
  NOT_ACTIVE_MESSAGE. Nothing of its subscription is cancelled or refunded.

Letting in writes ``User.approved``, so everything that reads it (the
profile seeder's cohort, API tokens, the Activity tab, the alpha free
month) treats such an account as one the admin approved.
"""
from datetime import datetime

from backend.utils.subscriptions import payments_enabled

NOT_ACTIVE_CODE = "account_not_active"
NOT_ACTIVE_MESSAGE = "This account is not active. Write to info@loore.org."
# The 403 every unapproved account got before payments, and still gets
# while they are off (the iPhone app keys on its first words).
WAITING_MESSAGE = "Your account is not approved. Please wait for approval."


def approval_waived(config=None):
    """Whether nobody waits for the admin's approval: payments are on."""
    return payments_enabled(config)


def is_blocked(user):
    """The admin turned this account off: not approved, and deactivated."""
    return bool(user is not None and not user.approved
                and user.deactivated_at is not None)


def never_approved(user):
    """Not approved and never turned off: an account waiting for the
    admin's approval while payments are off."""
    return bool(user is not None and not user.approved
                and user.deactivated_at is None)


def let_in(user, now=None, config=None):
    """Payments on: approve *user* when it was never approved (not one the
    admin blocked, not a system account, not one being deleted). It goes
    on to the trial step: an "alpha" plan, the default of every account
    created before payments, becomes "free", so only a plan the admin sets
    by hand gives the paid features without a subscription. Does not
    commit. Returns True when it approved the account."""
    if not approval_waived(config) or not never_approved(user):
        return False
    if user.deleted_at is not None:
        return False
    from backend.utils.system_accounts import SYSTEM_USERNAMES
    if user.username in SYSTEM_USERNAMES:
        return False
    user.approved = True
    user.approved_at = now or datetime.utcnow()
    if user.plan == "alpha" and not user.is_admin \
            and user.alpha_free_granted_at is None:
        user.plan = "free"
    return True


def refusal_body(user, config=None):
    """The 403 body of the approval gate for an account that is not
    approved: the blocked account's own text while payments are on,
    otherwise the waiting text it has always been."""
    if approval_waived(config) and is_blocked(user):
        return {"error": NOT_ACTIVE_MESSAGE, "code": NOT_ACTIVE_CODE}
    return {"error": WAITING_MESSAGE}


def signup_notice(user, first_acceptance, config=None):
    """Which "New signup" email the admin gets when *user* accepts the
    terms: "waiting" (payments off: an account to approve, as always),
    "let_in" (payments on: its first acceptance, for information only),
    or None."""
    if not approval_waived(config):
        return None if user.approved else "waiting"
    if first_acceptance and not is_blocked(user):
        return "let_in"
    return None


def user_payload_fields(user, config=None):
    """Payments on: whether the admin blocked the account (the clients show
    NOT_ACTIVE_MESSAGE instead of the waitlist). Nothing while they are
    off, so the payload is exactly what it was."""
    if not approval_waived(config):
        return {}
    return {"account_blocked": is_blocked(user)}
