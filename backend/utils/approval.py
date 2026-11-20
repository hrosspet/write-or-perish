"""The admin's approval while payments are on (#697).

Peter, 2026-11-13: "waiting for my approval is a bad first UX". With
payments on, the trial with a card is the filter and nobody waits for the
admin. With payments off, nothing here changes anything for an account
that could exist before this change: it waits for the admin's approval
(``User.approved``) as it always did.

With payments on:

* An account that was never approved is let in at its first request (the
  app's before_request, ``let_in``). It is approved like any other account
  (``let_in_at`` records that the gate did it) and goes on to the step
  where it starts the trial: an account that never had a subscription sees
  only the subscribe page (utils/subscriptions).
* Whitelisting an X handle or starting a pre-fill approves the account
  ahead of its owner (``let_in`` from routes/admin, not automatic): it is
  ready at their first sign-in. No welcome email is sent by it.
* The admin's Block on the Users tab (``block`` / ``unblock``) works on any
  account. A blocked account is ``approved`` False with ``deactivated_at``
  set, which the Deactivate button has always written and which the
  profile jobs already treat as the admin's pause. It is never let in by
  the rules above, gets no background job, and reaches only
  BLOCKED_OPEN: who it is, its subscription and Stripe's page to cancel it,
  its data download, the terms, deleting itself. Nothing of its
  subscription is cancelled or refunded.
* An account the admin marked as spam (``User.spam``, the Users tab's
  toggle; #713) is never let in by the gate, and one the gate let in
  before the mark is held from that moment (``held_as_spam``). It is
  treated as a blocked account (``not_active``). The admin's own approval
  wins over the mark: Activate, Activate & Welcome, a whitelist or a
  pre-fill. Unmarking alone approves nothing.

With payments off again, an account the gate let in by itself, without the
paid set and never paid, waits for approval like a new one
(``waiting_again``): nobody gets the alpha's free replies by having signed
up while payments were on. Its data stays; the admin may approve it by
hand.

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


def held_as_spam(user, config=None):
    """Payments on (#713; Peter, 2026-11-20): the admin marked *user* as
    spam and did not approve it by hand. Such an account is never let in
    by the gate, and one the gate let in before the mark (``let_in_at``)
    is held from the moment of the mark. Approved with no ``let_in_at``
    is the admin's own approval (Activate, Activate & Welcome, a whitelist
    or pre-fill, an approval from before payments), which wins over the
    mark. False while payments are off: the mark only hides the row in
    the Users tab, as it always did."""
    if user is None or not getattr(user, "spam", False):
        return False
    if not approval_waived(config):
        return False
    return not (user.approved and user.let_in_at is None)


def not_active(user, config=None):
    """Payments on: the admin blocked *user*, or it is held as spam. Such
    an account gets NOT_ACTIVE_MESSAGE, reaches only BLOCKED_OPEN and gets
    no background job. False while payments are off."""
    return bool(approval_waived(config)
                and (is_blocked(user) or held_as_spam(user, config)))


def waiting_again(user, config=None):
    """Payments off: an account the gate let in by itself while they were
    on, not approved by the admin since, without the paid set (a plan set
    by hand, a live subscription) and never paid. It waits for approval
    like a never-approved account: turning payments off gives nobody the
    alpha's free replies for having signed up meanwhile. A subscription
    that exists always counts (utils/subscriptions)."""
    if user is None or approval_waived(config):
        return False
    return bool(user.approved and getattr(user, "let_in_at", None)
                and not user.on_paid_plan
                and not user.subscription_ever_paid)


def counts_as_approved(user, config=None):
    """``approved``, except an account waiting again (``waiting_again``,
    payments off) and one held as spam (``held_as_spam``, payments on).
    While payments are off, for every account that could exist before
    #697 this is exactly ``User.approved``."""
    return bool(user is not None and user.approved
                and not waiting_again(user, config)
                and not held_as_spam(user, config))


def let_in(user, now=None, config=None, automatic=True):
    """Payments on: approve *user* when it was never approved (not one the
    admin blocked, not a system account, not one being deleted). It goes
    on to the trial step: an "alpha" plan, the default of every account
    created before payments, becomes "free", so only a plan the admin sets
    by hand gives the paid features without a subscription. *automatic*:
    the gate did it on the account's own request (``let_in_at``), not the
    admin's whitelist or pre-fill; the gate never lets in an account the
    admin marked as spam (#713). Does not commit. Returns True when it
    approved the account."""
    if not approval_waived(config) or not never_approved(user):
        return False
    if automatic and getattr(user, "spam", False):
        return False
    if user.deleted_at is not None:
        return False
    from backend.utils.system_accounts import SYSTEM_USERNAMES
    if user.username in SYSTEM_USERNAMES:
        return False
    now = now or datetime.utcnow()
    user.approved = True
    user.approved_at = now
    if automatic:
        user.let_in_at = now
    if user.plan == "alpha" and not user.is_admin \
            and user.alpha_free_granted_at is None:
        user.plan = "free"
    return True


def approved_by_hand(user):
    """The admin approved *user* by hand (Activate, Activate & Welcome, a
    pre-fill while payments are on): no longer an account the gate let in
    by itself, nor one waiting to be unblocked. Does not commit."""
    user.let_in_at = None
    user.approved_before_block = None


def block(user, now=None):
    """The admin's Block (#697), on any account: never approved, approved
    by the admin or let in by the gate. It is turned off as Deactivate
    always did (``approved`` False, ``deactivated_at``), and whether it was
    approved is kept for ``unblock``. Returns False when it was blocked
    already. Does not commit."""
    if is_blocked(user):
        return False
    user.approved_before_block = bool(user.approved)
    user.approved = False
    user.deactivated_at = now or datetime.utcnow()
    return True


def unblock(user, now=None):
    """Undo ``block``: the account is exactly as it was (approved or not,
    ``approved_at`` and ``let_in_at`` as they were), and ``deactivated_at``
    is cleared, so its API and clipper tokens work again and it is not
    asked for the terms again. An account deactivated before payments
    (no ``approved_before_block``) is activated as Activate always did:
    approved now, with the alpha testers' free month where it applies;
    its ``deactivated_at`` stays, as after any Activate (the terms are
    asked again). Such an account marked as spam (#713) is not approved:
    the block is cleared and nothing else, so the mark holds it
    (``held_as_spam``) until the admin activates it by hand. Returns
    False when it was not blocked. Does not commit."""
    if not is_blocked(user):
        return False
    prior = user.approved_before_block
    if prior is None and getattr(user, "spam", False):
        user.deactivated_at = None
        return True
    if prior is None:
        user.approved = True
        user.approved_at = now or datetime.utcnow()
        from backend.utils.subscriptions import convert_if_alpha
        convert_if_alpha(user)
        return True
    user.approved = bool(prior)
    user.deactivated_at = None
    user.approved_before_block = None
    return True


# What a blocked account may still call while payments are on: who it is,
# its subscription, Stripe's page to cancel it or change the card (the web
# only: the iPhone never says where to pay), its own data as one file
# (Peter, 2026-11-13: "yes, Ben should be able to export"; HEAD is the
# web's check before the download), the terms, deleting itself. The same
# (method, path) form as utils/subscriptions._OPEN_EXACT for accounts not
# let in; a blocked account never reaches that gate.
BLOCKED_OPEN = {
    ("GET", "/api/dashboard"),
    ("GET", "/api/billing"),
    ("POST", "/api/billing/portal"),
    ("GET", "/api/export/threads"),
    ("HEAD", "/api/export/threads"),
    ("POST", "/api/account/delete"),
    ("POST", "/api/account/delete/confirm"),
    ("POST", "/api/account/restore"),
    ("POST", "/api/account/restore/decline"),
    ("GET", "/api/account/restore"),
}
_BLOCKED_OPEN_PREFIXES = ("/api/terms",)


def open_while_blocked(method, path):
    """Whether a blocked account may call *method* *path* (BLOCKED_OPEN)."""
    path = path.rstrip("/") or "/"
    if (method, path) in BLOCKED_OPEN:
        return True
    return any(path == p or path.startswith(p + "/")
               for p in _BLOCKED_OPEN_PREFIXES)


def background_work_allowed(user, config=None):
    """Whether a background job nobody asked for just now (the nightly
    digest of saved references, the bookmark sync, embeddings) may make a
    model call for *user*. Payments on: the paid set (#438), and not an
    account the admin blocked (#697) or one held as spam (#713), whatever
    its subscription. True while payments are off: the job's own checks
    stay as they were."""
    from backend.utils.subscriptions import ai_replies_allowed
    if not ai_replies_allowed(user, config):
        return False
    return not not_active(user, config)


def not_held_as_spam_clause(config=None):
    """In SQL, for a query joined to User: not ``held_as_spam`` (#713).
    Nothing while payments are off, so the query is the one it was."""
    if not approval_waived(config):
        return []
    from sqlalchemy import and_, or_
    from backend.models import User
    return [or_(User.spam.is_(False),
                and_(User.approved.is_(True), User.let_in_at.is_(None)))]


def background_work_clause(config=None):
    """background_work_allowed in SQL, for a query joined to User. Nothing
    while payments are off, so the query is the one it was."""
    if not approval_waived(config):
        return []
    from sqlalchemy import or_
    from backend.models import User
    return [User.paid_features_clause(),
            or_(User.approved.is_(True), User.deactivated_at.is_(None)),
            *not_held_as_spam_clause(config)]


def refusal_body(user, config=None):
    """The 403 body of the approval gate for an account that is not
    approved: while payments are on, the not-active text for an account
    the admin blocked or one held as spam; otherwise the waiting text it
    has always been."""
    if not_active(user, config):
        return {"error": NOT_ACTIVE_MESSAGE, "code": NOT_ACTIVE_CODE}
    return {"error": WAITING_MESSAGE}


def signup_notice_due(user, config=None):
    """Whether the admin gets the "New signup" email when *user* accepts
    the terms. Payments off: an account waiting for approval, as always.
    Payments on: never; nobody waits, and new accounts are at the top of
    the admin's Users list."""
    if approval_waived(config):
        return False
    return not counts_as_approved(user, config)


def user_payload_fields(user, config=None):
    """Payments on: whether the account is not active, blocked by the admin
    or held as spam (the clients show NOT_ACTIVE_MESSAGE instead of the
    waitlist). Nothing while they are off, so the payload is exactly what
    it was."""
    if not approval_waived(config):
        return {}
    return {"account_blocked": not_active(user, config)}
