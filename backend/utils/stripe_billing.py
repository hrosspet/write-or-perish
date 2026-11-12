"""Stripe: the catalog, Checkout, the customer portal and the subscription
state on the account (#438).

Stripe Managed Payments (Stripe, through Link, is the merchant of record):
Checkout is Stripe-hosted (``managed_payments.enabled``), the product has
an eligible tax code, and a customer manages or cancels the subscription
on Stripe's pages (the Billing customer portal, or link.com). No card
data reaches Loore.

Every Stripe call goes through StripeGateway, which hands back plain
dicts; tests replace get_gateway with a stub and never reach Stripe.

The subscription state on the account is written only by
apply_subscription, from a subscription read from Stripe at that moment
(sync_subscription): an event says which subscription changed, never
what its state is, so events may arrive in any order. It never writes
``User.plan``, the admin's spend limit, or anything else set by hand.
"""
import logging
import time
from datetime import datetime, timezone

from flask import current_app, has_app_context

from backend.extensions import db
from backend.models import (
    SUBSCRIPTION_GRANTING_STATUSES, SUBSCRIPTION_LEVELS, CheckoutSession, User)

logger = logging.getLogger(__name__)

# The product's id in every Stripe account Loore uses (Stripe accepts our
# own id at creation), so the sync finds it again without a search.
PRODUCT_ID = "loore_subscription"
PRODUCT_NAME = "Loore subscription"
# One monthly price per level, found by its lookup key. A key moves to a
# new price when the amount, currency or tax behaviour changes.
PRICE_LOOKUP_KEYS = {
    "standard": "loore_standard_monthly",
    "supporter": "loore_supporter_monthly",
    "discounted": "loore_discounted_monthly",
}
LEVEL_BY_LOOKUP_KEY = {v: k for k, v in PRICE_LOOKUP_KEYS.items()}
# Marks the customer-portal configuration the sync keeps.
PORTAL_METADATA_KEY = "loore_portal"

# Stripe subscription status -> the account's subscription_status.
# "incomplete": the first payment has not gone through yet; nothing is
# given until it does. "paused" (a trial that ended without a card, which
# Checkout does not let happen here) and "unpaid" give nothing either.
STATUS_MAP = {
    "trialing": "trialing",
    "active": "active",
    "past_due": "past_due",
    "incomplete": "none",
    "incomplete_expired": "ended",
    "canceled": "ended",
    "unpaid": "ended",
    "paused": "ended",
}

# How long a looked-up price id is reused in this process.
PRICE_CACHE_SECONDS = 600
_price_cache = {}


class BillingNotConfigured(Exception):
    """No Stripe secret key, or the catalog is not in the Stripe account
    (run `flask stripe-sync-catalog`)."""


def _config(config=None):
    if config is not None:
        return config
    return current_app.config if has_app_context() else {}


def plain(obj):
    """A Stripe object (or a dict, or None) as plain dicts and lists."""
    if obj is None:
        return None
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj


class StripeGateway:
    """The Stripe calls Loore makes, each returning plain data."""

    def __init__(self, client):
        self.client = client

    # Catalog
    def retrieve_product(self, product_id):
        import stripe
        try:
            return plain(self.client.v1.products.retrieve(product_id))
        except stripe.InvalidRequestError as e:
            if getattr(e, "http_status", None) == 404:
                return None
            raise

    def create_product(self, params):
        return plain(self.client.v1.products.create(params=params))

    def update_product(self, product_id, params):
        return plain(self.client.v1.products.update(product_id,
                                                    params=params))

    def list_prices(self, lookup_keys):
        page = self.client.v1.prices.list(params={
            "lookup_keys": list(lookup_keys), "active": True, "limit": 100})
        return [plain(p) for p in page.data]

    def create_price(self, params):
        return plain(self.client.v1.prices.create(params=params))

    def update_price(self, price_id, params):
        return plain(self.client.v1.prices.update(price_id, params=params))

    def list_portal_configurations(self):
        page = self.client.v1.billing_portal.configurations.list(
            params={"active": True, "limit": 100})
        return [plain(c) for c in page.data]

    def create_portal_configuration(self, params):
        return plain(self.client.v1.billing_portal.configurations.create(
            params=params))

    def update_portal_configuration(self, config_id, params):
        return plain(self.client.v1.billing_portal.configurations.update(
            config_id, params=params))

    # Customers and sessions
    def create_customer(self, params, idempotency_key=None):
        options = {"idempotency_key": idempotency_key} if idempotency_key \
            else None
        return plain(self.client.v1.customers.create(params=params,
                                                     options=options))

    def create_checkout_session(self, params):
        return plain(self.client.v1.checkout.sessions.create(params=params))

    def retrieve_checkout_session(self, session_id):
        return plain(self.client.v1.checkout.sessions.retrieve(session_id))

    def expire_checkout_session(self, session_id):
        return plain(self.client.v1.checkout.sessions.expire(session_id))

    def retrieve_invoice(self, invoice_id):
        return plain(self.client.v1.invoices.retrieve(
            invoice_id, params={"expand": ["payments"]}))

    def create_refund(self, params, idempotency_key=None):
        options = {"idempotency_key": idempotency_key} if idempotency_key \
            else None
        return plain(self.client.v1.refunds.create(params=params,
                                                   options=options))

    def create_portal_session(self, params):
        return plain(self.client.v1.billing_portal.sessions.create(
            params=params))

    # Subscriptions
    def retrieve_subscription(self, subscription_id):
        return plain(self.client.v1.subscriptions.retrieve(subscription_id))

    def update_subscription(self, subscription_id, params):
        return plain(self.client.v1.subscriptions.update(
            subscription_id, params=params))

    def cancel_subscription(self, subscription_id):
        return plain(self.client.v1.subscriptions.cancel(subscription_id))


def get_gateway(config=None):
    """The gateway for the configured secret key. Raises
    BillingNotConfigured without one."""
    cfg = _config(config)
    key = cfg.get("STRIPE_SECRET_KEY")
    if not key:
        raise BillingNotConfigured("No Stripe secret key is configured.")
    import stripe
    return StripeGateway(stripe.StripeClient(key, max_network_retries=2))


def _ts(value):
    """A Stripe Unix timestamp as a naive UTC datetime (None stays None)."""
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc).replace(
        tzinfo=None)


# ── The catalog ─────────────────────────────────────────────────────────

def _price_matches(price, level, cfg):
    from backend.utils.subscriptions import level_price_cents
    recurring = price.get("recurring") or {}
    product = price.get("product")
    if isinstance(product, dict):
        product = product.get("id")
    return (price.get("unit_amount") == level_price_cents(level, cfg)
            and (price.get("currency") or "").lower()
            == cfg.get("SUBSCRIPTION_CURRENCY", "usd")
            and recurring.get("interval") == "month"
            and (recurring.get("interval_count") or 1) == 1
            and product == PRODUCT_ID
            and price.get("tax_behavior")
            == cfg.get("SUBSCRIPTION_TAX_BEHAVIOR", "inclusive"))


def portal_configuration_params(cfg):
    """The customer portal Loore links to: change the card, see invoices,
    cancel at the end of the period. No plan switching (the discounted
    price is opened only by a code) and no customer-detail edits."""
    frontend = (cfg.get("FRONTEND_URL") or "").rstrip("/")
    return {
        "name": "Loore",
        "default_return_url": f"{frontend}/account#subscription",
        "features": {
            "payment_method_update": {"enabled": True},
            "invoice_history": {"enabled": True},
            "subscription_cancel": {"enabled": True,
                                    "mode": "at_period_end"},
            "subscription_update": {"enabled": False},
            "customer_update": {"enabled": False},
        },
        "metadata": {PORTAL_METADATA_KEY: "1"},
    }


def sync_catalog(gateway, config=None, log=None):
    """Create or update the product, the three monthly prices and the
    customer-portal configuration in the Stripe account of the gateway's
    key. Idempotent: what already matches is left alone. Returns
    {"product": id, "prices": {level: id}, "portal_configuration": id,
    "changes": [...]}"""
    from backend.utils.subscriptions import level_price_cents
    cfg = _config(config)
    log = log or (lambda _msg: None)
    changes = []
    product_params = {
        "name": PRODUCT_NAME,
        "tax_code": cfg.get("STRIPE_PRODUCT_TAX_CODE"),
    }
    product = gateway.retrieve_product(PRODUCT_ID)
    if product is None:
        product = gateway.create_product({"id": PRODUCT_ID, **product_params})
        changes.append(f"created product {PRODUCT_ID}")
    else:
        tax_code = product.get("tax_code")
        if isinstance(tax_code, dict):
            tax_code = tax_code.get("id")
        if (product.get("name") != PRODUCT_NAME
                or tax_code != product_params["tax_code"]
                or not product.get("active", True)):
            product = gateway.update_product(
                PRODUCT_ID, {**product_params, "active": True})
            changes.append(f"updated product {PRODUCT_ID}")
    log(f"product {PRODUCT_ID}")

    existing = {p.get("lookup_key"): p
                for p in gateway.list_prices(PRICE_LOOKUP_KEYS.values())}
    prices = {}
    for level in SUBSCRIPTION_LEVELS:
        key = PRICE_LOOKUP_KEYS[level]
        current = existing.get(key)
        if current is not None and _price_matches(current, level, cfg):
            prices[level] = current["id"]
            log(f"price {level}: {current['id']} (unchanged)")
            continue
        created = gateway.create_price({
            "product": PRODUCT_ID,
            "unit_amount": level_price_cents(level, cfg),
            "currency": cfg.get("SUBSCRIPTION_CURRENCY", "usd"),
            "recurring": {"interval": "month"},
            "tax_behavior": cfg.get("SUBSCRIPTION_TAX_BEHAVIOR", "inclusive"),
            "lookup_key": key,
            # Moves the key from the old price, if any.
            "transfer_lookup_key": True,
            "nickname": f"Loore {level} monthly",
            "metadata": {"loore_level": level},
        })
        prices[level] = created["id"]
        changes.append(f"created price {level} {created['id']}")
        log(f"price {level}: {created['id']} (new)")
        if current is not None:
            # Running subscriptions keep the old price; no new checkout
            # can pick it up.
            gateway.update_price(current["id"], {"active": False})
            changes.append(f"archived price {current['id']}")
    _price_cache.clear()

    params = portal_configuration_params(cfg)
    ours = [c for c in gateway.list_portal_configurations()
            if (c.get("metadata") or {}).get(PORTAL_METADATA_KEY) == "1"]
    if ours:
        portal = gateway.update_portal_configuration(ours[0]["id"], params)
        log(f"portal configuration {portal['id']} (updated)")
    else:
        portal = gateway.create_portal_configuration(params)
        changes.append(f"created portal configuration {portal['id']}")
        log(f"portal configuration {portal['id']} (new)")
    return {"product": PRODUCT_ID, "prices": prices,
            "portal_configuration": portal["id"], "changes": changes}


def price_id_for_level(gateway, level):
    """The active price with the level's lookup key (cached for
    PRICE_CACHE_SECONDS). Raises BillingNotConfigured when the catalog
    has not been synced to this Stripe account."""
    key = PRICE_LOOKUP_KEYS[level]
    hit = _price_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[1] < PRICE_CACHE_SECONDS:
        return hit[0]
    found = [p for p in gateway.list_prices([key])
             if p.get("lookup_key") == key]
    if not found:
        raise BillingNotConfigured(
            f"No price {key}: run `flask stripe-sync-catalog`.")
    _price_cache[key] = (found[0]["id"], now)
    return found[0]["id"]


def portal_configuration_id(gateway):
    for c in gateway.list_portal_configurations():
        if (c.get("metadata") or {}).get(PORTAL_METADATA_KEY) == "1":
            return c["id"]
    return None


def level_for_price(price, metadata=None):
    """The level a subscription's price stands for: its lookup key, else
    the level the checkout wrote into the subscription's metadata."""
    price = price or {}
    level = LEVEL_BY_LOOKUP_KEY.get(price.get("lookup_key"))
    if level is None:
        level = ((price.get("metadata") or {}).get("loore_level")
                 or (metadata or {}).get("loore_level"))
    return level if level in SUBSCRIPTION_LEVELS else None


# ── Customers, Checkout and the portal ─────────────────────────────────

def ensure_customer(gateway, user):
    """The account's Stripe customer, created on first use with only the
    account id in its metadata (the buyer's name, email and address are
    asked for by Checkout). Commits the id."""
    if user.stripe_customer_id:
        return user.stripe_customer_id
    # One customer per account, also for two clicks at once.
    db.session.query(User).filter(User.id == user.id).with_for_update().one()
    db.session.refresh(user)
    if user.stripe_customer_id:
        db.session.commit()
        return user.stripe_customer_id
    customer = gateway.create_customer(
        {"metadata": {"loore_user_id": str(user.id)}},
        idempotency_key=f"loore-customer-{user.id}")
    user.stripe_customer_id = customer["id"]
    db.session.commit()
    return user.stripe_customer_id


class CheckoutAlreadyCompleted(Exception):
    """The account's open checkout was completed: its subscription is on
    its way (the webhook has not arrived yet), so no other opens."""


def checkout_session_params(user, level, price_id, config=None,
                            code_id=None):
    cfg = _config(config)
    frontend = (cfg.get("FRONTEND_URL") or "").rstrip("/")
    meta = {"loore_user_id": str(user.id), "loore_level": level}
    if code_id is not None:
        meta["loore_code_id"] = str(code_id)
    subscription_data = {"metadata": dict(meta)}
    trial_days = int(cfg.get("SUBSCRIPTION_TRIAL_DAYS") or 0)
    if trial_days > 0 and not user.subscription_trial_used:
        subscription_data["trial_period_days"] = trial_days
    return {
        "mode": "subscription",
        "managed_payments": {"enabled": True},
        "customer": user.stripe_customer_id,
        "client_reference_id": str(user.id),
        "line_items": [{"price": price_id, "quantity": 1}],
        "subscription_data": subscription_data,
        "metadata": meta,
        "success_url": (f"{frontend}/account?subscription=success"
                        "&session_id={CHECKOUT_SESSION_ID}#subscription"),
        "cancel_url": f"{frontend}/account?subscription=cancelled#subscription",
    }


def expire_session(gateway, row):
    """Expire one of Loore's open sessions at Stripe and record what it
    is now: "expired", or "complete" when the buyer finished it first
    (Stripe refuses to expire a session that is no longer open). Raises
    when Stripe can't be reached, or keeps an open session open."""
    try:
        gateway.expire_checkout_session(row.id)
        row.state = "expired"
        return row.state
    except Exception:  # noqa: BLE001 - read what the session is now
        status = (gateway.retrieve_checkout_session(row.id) or {}).get(
            "status")
        if status == "open":
            raise
    row.state = "complete" if status == "complete" else "expired"
    return row.state


def create_checkout(gateway, user, level, config=None, code=None):
    """A Stripe-hosted Checkout Session for *user* at *level* (*code*: the
    discount code that opened it), with the trial when the account has not
    had one. The account's earlier open session is expired first, so only
    one can ever be completed; raises CheckoutAlreadyCompleted when it was
    completed already. Returns the session (its ``url`` is where the
    browser goes). Commits."""
    price_id = price_id_for_level(gateway, level)
    ensure_customer(gateway, user)
    # One open checkout per account, also for two presses at once. Read
    # the account again under the lock: a webhook may have just started
    # its subscription or used its trial.
    db.session.query(User).filter(User.id == user.id).with_for_update().one()
    db.session.refresh(user)
    completed = user.subscription_grants_access
    for row in CheckoutSession.query.filter_by(user_id=user.id,
                                               state="open").all():
        completed = expire_session(gateway, row) == "complete" or completed
    if completed:
        db.session.commit()
        raise CheckoutAlreadyCompleted()
    session = gateway.create_checkout_session(checkout_session_params(
        user, level, price_id, config,
        code_id=code.id if code is not None else None))
    db.session.add(CheckoutSession(
        id=session["id"], user_id=user.id, level=level,
        code_id=code.id if code is not None else None, state="open"))
    db.session.commit()
    return session


def create_portal(gateway, user, config=None):
    """A customer-portal session (change the card, invoices, cancel).
    Raises BillingNotConfigured when the account has no customer."""
    cfg = _config(config)
    if not user.stripe_customer_id:
        raise BillingNotConfigured("This account has no Stripe customer.")
    frontend = (cfg.get("FRONTEND_URL") or "").rstrip("/")
    params = {"customer": user.stripe_customer_id,
              "return_url": f"{frontend}/account#subscription"}
    config_id = portal_configuration_id(gateway)
    if config_id:
        params["configuration"] = config_id
    return gateway.create_portal_session(params)


# ── The subscription on the account ─────────────────────────────────────

def _user_for_subscription(sub):
    customer = sub.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    if customer:
        user = User.query.filter_by(stripe_customer_id=customer).first()
        if user is not None:
            return user
    raw = (sub.get("metadata") or {}).get("loore_user_id")
    try:
        user = db.session.get(User, int(raw)) if raw else None
    except (TypeError, ValueError):
        user = None
    if user is not None and user.stripe_customer_id in (None, customer):
        return user
    return None


def _first_item(sub):
    items = (sub.get("items") or {}).get("data") or []
    return items[0] if items else {}


def apply_subscription(sub, config=None):
    """Write the state of *sub* (a subscription just read from Stripe) on
    its account. Returns the account, or None when the subscription
    belongs to no account. Touches only the subscription columns (and,
    when the ceiling changed, the spend block): never ``plan`` or an
    admin's limit. Does not commit."""
    from backend.utils.spend import (
        get_user_spend_limit_usd, reconcile_user_spend_block)
    cfg = _config(config)
    user = _user_for_subscription(sub)
    if user is None:
        logger.warning("Stripe subscription %s belongs to no account",
                       sub.get("id"))
        return None
    status = STATUS_MAP.get(sub.get("status"), "none")
    granting = status in SUBSCRIPTION_GRANTING_STATUSES
    if (user.stripe_subscription_id
            and user.stripe_subscription_id != sub.get("id")
            and not granting and user.subscription_grants_access):
        # A late event about an older subscription: the one that gives
        # access now stays.
        logger.info("Stripe subscription %s ignored for user %s: %s is "
                    "current", sub.get("id"), user.id,
                    user.stripe_subscription_id)
        return user
    limit_before = get_user_spend_limit_usd(user, cfg)

    item = _first_item(sub)
    level = level_for_price(item.get("price"), sub.get("metadata"))
    period_end = _ts(item.get("current_period_end")
                     or sub.get("current_period_end"))
    cancel_at = _ts(sub.get("cancel_at"))
    cancelling = bool(sub.get("cancel_at_period_end")) or cancel_at is not None
    user.stripe_subscription_id = sub.get("id")
    user.subscription_level = level
    user.subscription_status = status
    user.subscription_period_end = cancel_at or period_end
    user.subscription_trial_end = _ts(sub.get("trial_end"))
    user.subscription_cancel_at_period_end = cancelling and status != "ended"
    if sub.get("trial_end") or sub.get("trial_start"):
        user.subscription_trial_used = True
        if not user.subscription_trial_sub_id:
            user.subscription_trial_sub_id = sub.get("id")
    if not user.stripe_customer_id:
        customer = sub.get("customer")
        user.stripe_customer_id = (customer.get("id")
                                   if isinstance(customer, dict) else customer)
    if get_user_spend_limit_usd(user, cfg) != limit_before:
        reconcile_user_spend_block(user, cfg)
    return user


def _granting(sub):
    return STATUS_MAP.get(sub.get("status"), "none") in \
        SUBSCRIPTION_GRANTING_STATUSES


def _order_key(sub):
    """Which of two subscriptions of one account is kept: the one created
    first, then the smaller id, whatever order their events arrive in."""
    return (int(sub.get("created") or 0), sub.get("id") or "")


def _payment_intent_of(invoice):
    pi = invoice.get("payment_intent")
    for entry in ((invoice.get("payments") or {}).get("data") or []):
        payment = entry.get("payment") or {}
        pi = payment.get("payment_intent") or pi
    if isinstance(pi, dict):
        pi = pi.get("id")
    return pi


def refund_latest_payment(gateway, sub):
    """Refund what the subscription's latest invoice charged, once (the
    refund's idempotency key is the subscription). Returns the refund, or
    None when nothing was charged."""
    invoice_id = sub.get("latest_invoice")
    if isinstance(invoice_id, dict):
        invoice_id = invoice_id.get("id")
    if not invoice_id:
        return None
    invoice = gateway.retrieve_invoice(invoice_id)
    if not invoice.get("amount_paid"):
        return None
    pi = _payment_intent_of(invoice)
    if not pi:
        logger.error("Duplicate subscription %s: paid invoice without a "
                     "payment to refund", sub.get("id"))
        return None
    return gateway.create_refund(
        {"payment_intent": pi, "reason": "duplicate"},
        idempotency_key=f"loore-duplicate-{sub.get('id')}")


def cancel_duplicate(gateway, sub):
    """Cancel a subscription the account must not keep, refund what it
    charged, and give back the code use it took. Returns it cancelled."""
    from backend.utils import discount_codes
    cancelled = gateway.cancel_subscription(sub["id"])
    refund_latest_payment(gateway, sub)
    discount_codes.void_use(sub["id"])
    return cancelled


def _record_code_use(gateway, user, sub):
    """The code that opened a kept subscription takes one use; when that
    was its last, other accounts' open checkouts with it are expired."""
    from backend.utils import discount_codes
    raw = (sub.get("metadata") or {}).get("loore_code_id")
    try:
        code_id = int(raw) if raw else None
    except (TypeError, ValueError):
        code_id = None
    if code_id is None:
        return
    code, used_up = discount_codes.record_use(code_id, user.id, sub["id"])
    if not used_up:
        return
    for row in CheckoutSession.query.filter(
            CheckoutSession.code_id == code.id,
            CheckoutSession.state == "open",
            CheckoutSession.user_id != user.id).all():
        expire_session(gateway, row)


def settle_subscription(gateway, sub, config=None):
    """Apply *sub* (just read from Stripe) to its account, keeping one
    subscription and one trial per account:

    * a second subscription that gives access while the account's current
      one still does: the one created later is cancelled and what it
      charged refunded, whichever arrives first;
    * a subscription still in a trial for an account whose trial another
      subscription used: cancelled (nothing was charged yet);
    * the code that opened a kept subscription takes its use now.

    Returns the account (None when the subscription belongs to none).
    Does not commit."""
    user = _user_for_subscription(sub)
    if user is None:
        logger.warning("Stripe subscription %s belongs to no account",
                       sub.get("id"))
        return None
    sub_id = sub.get("id")
    if _granting(sub):
        current_id = user.stripe_subscription_id
        if current_id and current_id != sub_id:
            current = gateway.retrieve_subscription(current_id)
            if _granting(current):
                keep, drop = sorted([current, sub], key=_order_key)
                logger.warning("User %s has two subscriptions: %s kept, "
                               "%s cancelled", user.id, keep.get("id"),
                               drop.get("id"))
                cancel_duplicate(gateway, drop)
                if user.subscription_trial_sub_id == drop.get("id"):
                    user.subscription_trial_sub_id = None
                apply_subscription(keep, config)
                _record_code_use(gateway, user, keep)
                return user
        if (sub.get("status") == "trialing"
                and user.subscription_trial_used
                and user.subscription_trial_sub_id not in (None, sub_id)):
            logger.warning("User %s: a second trial (%s) cancelled; %s had "
                           "the trial", user.id, sub_id,
                           user.subscription_trial_sub_id)
            cancelled = cancel_duplicate(gateway, sub)
            apply_subscription(cancelled, config)
            return user
    apply_subscription(sub, config)
    if _granting(sub):
        _record_code_use(gateway, user, sub)
    return user


def sync_subscription(gateway, subscription_id, config=None):
    """Read the subscription from Stripe and settle it. Commits."""
    sub = gateway.retrieve_subscription(subscription_id)
    user = settle_subscription(gateway, sub, config)
    db.session.commit()
    return user


def subscription_id_of_invoice(invoice):
    """The subscription an invoice belongs to (on the invoice before API
    2025-03-31, under parent.subscription_details since)."""
    sub = invoice.get("subscription")
    if not sub:
        details = ((invoice.get("parent") or {})
                   .get("subscription_details") or {})
        sub = details.get("subscription")
    if isinstance(sub, dict):
        sub = sub.get("id")
    return sub


# ── Webhook events ──────────────────────────────────────────────────────

SUBSCRIPTION_EVENTS = {
    "customer.subscription.created", "customer.subscription.updated",
    "customer.subscription.deleted", "customer.subscription.paused",
    "customer.subscription.resumed",
    "customer.subscription.trial_will_end",
    "customer.subscription.pending_update_applied",
    "customer.subscription.pending_update_expired",
}
INVOICE_EVENTS = {
    "invoice.paid", "invoice.payment_succeeded", "invoice.payment_failed",
    "invoice.payment_action_required",
}
CHECKOUT_EVENTS = {"checkout.session.completed", "checkout.session.expired"}
HANDLED_EVENTS = SUBSCRIPTION_EVENTS | INVOICE_EVENTS | CHECKOUT_EVENTS


def _set_checkout_state(session, state):
    row = db.session.get(CheckoutSession, session.get("id") or "")
    if row is not None and row.state == "open":
        row.state = state


def _checkout_user(session):
    raw = session.get("client_reference_id") or (
        session.get("metadata") or {}).get("loore_user_id")
    try:
        user = db.session.get(User, int(raw)) if raw else None
    except (TypeError, ValueError):
        return None
    if user is None:
        return None
    customer = session.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    if user.stripe_customer_id and customer and \
            user.stripe_customer_id != customer:
        logger.warning("Checkout %s: customer does not match user %s",
                       session.get("id"), user.id)
        return None
    if customer and not user.stripe_customer_id:
        user.stripe_customer_id = customer
    return user


def handle_event(gateway, event, config=None):
    """Apply one verified event. Returns what was done ("applied",
    "ignored", "no_account"). Raises on a failure worth a retry (Stripe
    unreachable); the caller then answers with an error so Stripe sends
    the event again. Does not commit."""
    etype = event.get("type")
    obj = ((event.get("data") or {}).get("object")) or {}
    if etype not in HANDLED_EVENTS:
        return "ignored"
    if etype in CHECKOUT_EVENTS:
        if obj.get("mode") != "subscription":
            return "ignored"
        if etype == "checkout.session.expired":
            _set_checkout_state(obj, "expired")
            return "applied"
        _set_checkout_state(obj, "complete")
        user = _checkout_user(obj)
        if user is None:
            return "no_account"
        sub_id = obj.get("subscription")
        if isinstance(sub_id, dict):
            sub_id = sub_id.get("id")
        if sub_id:
            settle_subscription(
                gateway, gateway.retrieve_subscription(sub_id), config)
        return "applied"
    if etype in INVOICE_EVENTS:
        sub_id = subscription_id_of_invoice(obj)
    else:
        sub_id = obj.get("id")
    if not sub_id:
        return "ignored"
    user = settle_subscription(
        gateway, gateway.retrieve_subscription(sub_id), config)
    return "applied" if user is not None else "no_account"


# ── Account deletion ────────────────────────────────────────────────────

def stop_renewal_for_deletion(user, config=None):
    """An account deletion was scheduled: its subscription does not renew
    (it ends at the end of the period already paid, before the purge).
    Best effort: a failure is logged; the purge cancels it again."""
    if not (user.stripe_subscription_id and user.subscription_grants_access
            and not user.subscription_cancel_at_period_end):
        return False
    try:
        gateway = get_gateway(config)
        sub = gateway.update_subscription(
            user.stripe_subscription_id, {"cancel_at_period_end": True})
        apply_subscription(sub, config)
        db.session.commit()
        return True
    except Exception as e:  # noqa: BLE001 - the purge tries again
        db.session.rollback()
        logger.error("Could not stop the subscription of user %s at its "
                     "deletion: %s", user.id, type(e).__name__)
        return False


def cancel_for_purge(user, config=None):
    """The account is about to be deleted: a subscription that still runs
    is cancelled now. Raises when Stripe cannot be reached, so the purge
    waits and tries again rather than leave a subscription running for an
    account that no longer exists."""
    if not user.stripe_subscription_id or user.subscription_status in (
            "none", "ended"):
        return False
    gateway = get_gateway(config)
    sub = gateway.cancel_subscription(user.stripe_subscription_id)
    apply_subscription(sub, config)
    return True
