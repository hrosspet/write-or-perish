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

# One product per level, with our own ids (Stripe accepts them at
# creation), so the sync finds them again without a search. Separate
# products are what lets the customer portal switch Standard and
# Supporter: it cannot offer two monthly prices of one product.
PRODUCTS = {
    "standard": ("loore_standard", "Loore Standard"),
    "supporter": ("loore_supporter", "Loore Supporter"),
    "discounted": ("loore_reduced", "Loore (reduced price)"),
}
# The single product of this PR's first version: archived by the sync.
OLD_PRODUCT_ID = "loore_subscription"
# Levels a subscriber may switch between in the portal. The discounted
# level is never offered there: a discounted subscriber may move up, not
# back without a code.
PORTAL_SWITCH_LEVELS = ("standard", "supporter")
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

    def list_refunds(self, payment_intent):
        page = self.client.v1.refunds.list(
            params={"payment_intent": payment_intent, "limit": 100})
        return [plain(r) for r in page.data]

    def retrieve_charge(self, charge_id):
        return plain(self.client.v1.charges.retrieve(charge_id))

    def delete_customer(self, customer_id):
        return plain(self.client.v1.customers.delete(customer_id))

    def create_portal_session(self, params):
        return plain(self.client.v1.billing_portal.sessions.create(
            params=params))

    # Customer credit (#439: a referral's free week)
    def preview_invoice(self, customer_id, subscription_id):
        """What the subscription's next invoice will charge."""
        return plain(self.client.v1.invoices.create_preview(params={
            "customer": customer_id, "subscription": subscription_id}))

    def list_balance_transactions(self, customer_id):
        page = self.client.v1.customers.balance_transactions.list(
            customer_id, params={"limit": 100})
        return [plain(t) for t in page.data]

    def create_balance_transaction(self, customer_id, params,
                                   idempotency_key=None):
        options = {"idempotency_key": idempotency_key} if idempotency_key \
            else None
        return plain(self.client.v1.customers.balance_transactions.create(
            customer_id, params=params, options=options))

    # Subscriptions
    def retrieve_subscription(self, subscription_id):
        return plain(self.client.v1.subscriptions.retrieve(subscription_id))

    def update_subscription(self, subscription_id, params):
        return plain(self.client.v1.subscriptions.update(
            subscription_id, params=params))

    def cancel_subscription(self, subscription_id):
        return plain(self.client.v1.subscriptions.cancel(subscription_id))

    def release_subscription_schedule(self, schedule_id):
        return plain(self.client.v1.subscription_schedules.release(
            schedule_id))

    # One trial per card (#697): only the card's fingerprint is read.
    def retrieve_payment_method(self, payment_method_id):
        return plain(self.client.v1.payment_methods.retrieve(
            payment_method_id))


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
            and product == PRODUCTS[level][0]
            and price.get("tax_behavior")
            == cfg.get("SUBSCRIPTION_TAX_BEHAVIOR", "inclusive"))


def portal_configuration_params(cfg, prices=None):
    """The customer portal Loore links to: change the card, see invoices,
    cancel at the end of the period, and switch between Standard and
    Supporter (*prices*: level -> price id). An upgrade is charged at
    once: the prorated difference for the rest of the period is invoiced
    when the user confirms, so "Subscribe and pay" on Stripe's confirm
    page is what happens and the next invoice is the plain monthly price
    (decided under Peter's "depending on the difficulty ... pls decide",
    2026-11-12). A downgrade waits for the end of the period: the user
    keeps the higher level they paid for, nothing is refunded, and the
    lower price starts with the next period. A trialing subscription keeps
    its trial when it switches. No customer detail edits."""
    frontend = (cfg.get("FRONTEND_URL") or "").rstrip("/")
    switch = {"enabled": False}
    if prices:
        switch = {
            "enabled": True,
            "default_allowed_updates": ["price"],
            "products": [{"product": PRODUCTS[level][0],
                          "prices": [prices[level]]}
                         for level in PORTAL_SWITCH_LEVELS],
            "proration_behavior": "always_invoice",
            "schedule_at_period_end": {
                "conditions": [{"type": "decreasing_item_amount"}]},
            "trial_update_behavior": "continue_trial",
        }
    return {
        "name": "Loore",
        "default_return_url": f"{frontend}/account#subscription",
        "features": {
            "payment_method_update": {"enabled": True},
            "invoice_history": {"enabled": True},
            "subscription_cancel": {"enabled": True,
                                    "mode": "at_period_end"},
            "subscription_update": switch,
            "customer_update": {"enabled": False},
        },
        "metadata": {PORTAL_METADATA_KEY: "1"},
    }


def _sync_product(gateway, level, cfg, changes):
    product_id, name = PRODUCTS[level]
    params = {"name": name, "tax_code": cfg.get("STRIPE_PRODUCT_TAX_CODE")}
    product = gateway.retrieve_product(product_id)
    if product is None:
        gateway.create_product({"id": product_id, **params})
        changes.append(f"created product {product_id}")
        return
    tax_code = product.get("tax_code")
    if isinstance(tax_code, dict):
        tax_code = tax_code.get("id")
    if (product.get("name") != name or tax_code != params["tax_code"]
            or not product.get("active", True)):
        gateway.update_product(product_id, {**params, "active": True})
        changes.append(f"updated product {product_id}")


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
    for level in SUBSCRIPTION_LEVELS:
        _sync_product(gateway, level, cfg, changes)
        log(f"product {PRODUCTS[level][0]}")

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
            "product": PRODUCTS[level][0],
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
    old = gateway.retrieve_product(OLD_PRODUCT_ID)
    if old is not None and old.get("active", True):
        gateway.update_product(OLD_PRODUCT_ID, {"active": False})
        changes.append(f"archived product {OLD_PRODUCT_ID}")

    params = portal_configuration_params(cfg, prices)
    ours = [c for c in gateway.list_portal_configurations()
            if (c.get("metadata") or {}).get(PORTAL_METADATA_KEY) == "1"]
    if ours:
        portal = gateway.update_portal_configuration(ours[0]["id"], params)
        log(f"portal configuration {portal['id']} (updated)")
    else:
        portal = gateway.create_portal_configuration(params)
        changes.append(f"created portal configuration {portal['id']}")
        log(f"portal configuration {portal['id']} (new)")
    return {"products": {lv: PRODUCTS[lv][0] for lv in PRODUCTS},
            "prices": prices,
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
        idempotency_key=customer_idempotency_key(user))
    user.stripe_customer_id = customer["id"]
    db.session.commit()
    return user.stripe_customer_id


def customer_idempotency_key(user):
    """The key of the one "create customer" call for *user* (a checkout's,
    or a referral's credit, #439). The account's creation time is in it:
    databases that share one Stripe account (staging and local development
    on the sandbox) have the same account ids, and within Stripe's 24-hour
    window the id alone handed one database's customer to the other (seen
    in the sandbox, 2026-11-12: a customer deleted since, so the credit
    failed with "No such customer")."""
    created = getattr(user, "created_at", None)
    stamp = f"-{int(created.timestamp())}" if created else ""
    return f"loore-customer-{user.id}{stamp}"


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
    if user.is_alpha_tester:
        # The free month is an alpha tester's trial: no 14 days on top.
        # Subscribing during it, the first payment is due when it ends.
        from backend.utils.subscriptions import alpha_checkout_trial_end
        trial_end = alpha_checkout_trial_end(user, cfg)
        if trial_end is not None:
            subscription_data["trial_end"] = int(
                trial_end.replace(tzinfo=timezone.utc).timestamp())
    elif trial_days > 0 and not user.subscription_trial_used:
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
    completed = user.subscription_status in SUBSCRIPTION_GRANTING_STATUSES
    for row in CheckoutSession.query.filter_by(user_id=user.id,
                                               state="open").all():
        expire_session(gateway, row)
    # A completed checkout whose subscription the webhook has not brought
    # yet (the buyer closed the tab at Stripe): read it from Stripe now,
    # so it refuses this press and every later one.
    for row in CheckoutSession.query.filter_by(user_id=user.id,
                                               state="complete").all():
        completed = settle_checkout_row(gateway, user, row, config) or \
            completed
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


def settle_checkout_row(gateway, user, row, config=None):
    """Bring the subscription of a completed checkout onto the account
    and mark the row "settled". True when the account then has a live
    subscription."""
    session = gateway.retrieve_checkout_session(row.id) or {}
    sub_id = session.get("subscription")
    if isinstance(sub_id, dict):
        sub_id = sub_id.get("id")
    if sub_id:
        settle_subscription(gateway, gateway.retrieve_subscription(sub_id),
                            config, lock=False)
    row.state = "settled"
    return user.subscription_status in SUBSCRIPTION_GRANTING_STATUSES


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


def _apply_payment_state(user, sub, status):
    """The columns that belong to one subscription start again with a new
    one; why it ended, and whether its owner ended it (kept for good);
    the trial ceiling lasts until the first payment.

    "Paid once" (User.subscription_ever_paid) is not set here: Stripe
    makes a subscription active when its trial ends and charges the
    first invoice about an hour later, so active is not proof of a
    payment. Only a paid invoice above zero sets it (handle_event)."""
    # An alpha tester before their first paid invoice (#438) has one
    # ceiling from the free month's start; a subscription that arrives
    # meanwhile does not start it again.
    from backend.utils.spend import alpha_unpaid_since
    alpha_unpaid = alpha_unpaid_since(user) is not None
    if user.stripe_subscription_id != sub.get("id"):
        user.subscription_revoked = None
        if not alpha_unpaid:
            user.subscription_trial_capped = False
    first_payment_pending = (status == "active"
                             and not user.subscription_ever_paid
                             and bool(sub.get("trial_start")))
    if (status != "trialing" and not first_payment_pending
            and not alpha_unpaid):
        user.subscription_trial_capped = False
    details = sub.get("cancellation_details") or {}
    user.subscription_end_reason = (details.get("reason")
                                    if status == "ended" else None)
    meta = sub.get("metadata") or {}
    loore_cancelled = (meta.get(DUPLICATE_METADATA_KEY) == "1"
                       or bool(meta.get(LOORE_CANCELLED_METADATA_KEY)))
    if (user.subscription_end_reason == "cancellation_requested"
            and not loore_cancelled):
        # Its owner cancelled it (not Loore, which marks the duplicates
        # and second trials it cancels): the account had a subscription
        # and keeps its writing (lapsed), also once a later attempt to
        # subscribe again replaces these columns.
        user.subscription_cancelled_by_owner = True


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
    other = (user.stripe_subscription_id
             and user.stripe_subscription_id != sub.get("id"))
    if (other and not granting
            and user.subscription_status in SUBSCRIPTION_GRANTING_STATUSES):
        # A late event about an older subscription: the one that gives
        # access now stays.
        logger.info("Stripe subscription %s ignored for user %s: %s is "
                    "current", sub.get("id"), user.id,
                    user.stripe_subscription_id)
        return user
    if other and sub.get("status") in ("incomplete", "incomplete_expired"):
        # An attempt to subscribe again that never went through (its first
        # payment is pending or expired) never granted anything: it does
        # not replace the account's earlier subscription, so a lapsed
        # account stays lapsed. It takes over once it is live.
        logger.info("Stripe subscription %s (%s) not applied for user %s: "
                    "it never went through", sub.get("id"),
                    sub.get("status"), user.id)
        return user
    limit_before = get_user_spend_limit_usd(user, cfg)

    item = _first_item(sub)
    level = level_for_price(item.get("price"), sub.get("metadata"))
    period_end = _ts(item.get("current_period_end")
                     or sub.get("current_period_end"))
    cancel_at = _ts(sub.get("cancel_at"))
    cancelling = bool(sub.get("cancel_at_period_end")) or cancel_at is not None
    _apply_payment_state(user, sub, status)
    user.stripe_subscription_id = sub.get("id")
    user.subscription_level = level
    user.subscription_status = status
    user.subscription_period_end = cancel_at or period_end
    user.subscription_trial_end = _ts(sub.get("trial_end"))
    user.subscription_cancel_at_period_end = cancelling and status != "ended"
    user.subscription_trial_start = _ts(sub.get("trial_start"))
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


def _latest_payment_intent(gateway, sub):
    """(the latest invoice, its payment intent) of *sub*, or (invoice,
    None) when it charged nothing."""
    invoice_id = sub.get("latest_invoice")
    if isinstance(invoice_id, dict):
        invoice_id = invoice_id.get("id")
    if not invoice_id:
        return None, None
    invoice = gateway.retrieve_invoice(invoice_id)
    if not invoice.get("amount_paid"):
        return invoice, None
    return invoice, _payment_intent_of(invoice)


def refund_latest_payment(gateway, sub):
    """Refund what the subscription's latest invoice charged, once: a
    payment that has a refund already is left alone, and the refund's
    idempotency key is the subscription. Returns the refund, or None when
    nothing is to be refunded."""
    invoice, pi = _latest_payment_intent(gateway, sub)
    if invoice is None or not invoice.get("amount_paid"):
        return None
    if not pi:
        logger.error("Duplicate subscription %s: paid invoice without a "
                     "payment to refund", sub.get("id"))
        return None
    if gateway.list_refunds(pi):
        return None
    return gateway.create_refund(
        {"payment_intent": pi, "reason": "duplicate"},
        idempotency_key=f"loore-duplicate-{sub.get('id')}")


DUPLICATE_METADATA_KEY = "loore_duplicate"
# Set on a subscription Loore cancels for another reason (an account
# deletion while a payment failed): not the owner's cancellation.
LOORE_CANCELLED_METADATA_KEY = "loore_cancelled"


def cancel_duplicate(gateway, sub):
    """End a subscription the account must not keep: mark it as Loore's
    duplicate, refund what it charged, then cancel it, and give back the
    code use it took. In that order, so a failure part way leaves it
    live (the event is sent again and finishes the job) and never
    cancelled without its refund. Returns it cancelled."""
    from backend.utils import discount_codes
    if (sub.get("metadata") or {}).get(DUPLICATE_METADATA_KEY) != "1":
        gateway.update_subscription(
            sub["id"], {"metadata": {DUPLICATE_METADATA_KEY: "1"}})
    refund_latest_payment(gateway, sub)
    cancelled = gateway.cancel_subscription(sub["id"])
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


def settle_subscription(gateway, sub, config=None, lock=True):
    """Apply *sub* (just read from Stripe) to its account, keeping one
    subscription and one trial per account:

    * a second subscription that gives access while the account's current
      one still does: the one created later is cancelled and what it
      charged refunded, whichever arrives first;
    * a subscription still in a trial for an account whose trial another
      subscription used: cancelled (nothing was charged yet);
    * one still in a trial whose card started a trial on another account
      (#697, utils/trial_cards): cancelled the same way;
    * the code that opened a kept subscription takes its use now.

    The account row is locked first (*lock*), so two events of one
    account are settled one after the other, never side by side.

    Returns the account (None when the subscription belongs to none).
    Does not commit."""
    user = _user_for_subscription(sub)
    if user is None:
        logger.warning("Stripe subscription %s belongs to no account",
                       sub.get("id"))
        return None
    if lock:
        db.session.query(User).filter(
            User.id == user.id).with_for_update().one()
        db.session.refresh(user)
    sub_id = sub.get("id")
    if (not _granting(sub) and sub_id != user.stripe_subscription_id
            and (sub.get("metadata") or {}).get(DUPLICATE_METADATA_KEY)
            == "1"):
        # A duplicate Loore cancelled: make sure its payment went back.
        refund_latest_payment(gateway, sub)
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
        # One trial per card too (#697): a trial whose card already
        # started one on another account goes the same way.
        from backend.utils.trial_cards import trial_card_refused
        if trial_card_refused(gateway, user, sub, config):
            # The account's next checkout has no trial, whatever card.
            user.subscription_trial_used = True
            user.subscription_trial_card_refused_at = datetime.utcnow()
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
CHARGE_EVENTS = {"charge.dispute.created", "charge.refunded"}
HANDLED_EVENTS = (SUBSCRIPTION_EVENTS | INVOICE_EVENTS | CHECKOUT_EVENTS
                  | CHARGE_EVENTS)
# What Peter registers on the webhook endpoint.
REGISTERED_EVENTS = sorted(
    (SUBSCRIPTION_EVENTS - {"customer.subscription.pending_update_applied",
                            "customer.subscription.pending_update_expired"})
    | {"invoice.paid", "invoice.payment_failed",
       "invoice.payment_action_required"}
    | CHECKOUT_EVENTS | CHARGE_EVENTS)


def _set_checkout_state(session, state):
    row = db.session.get(CheckoutSession, session.get("id") or "")
    if row is not None and row.state in ("open", "complete"):
        row.state = state


def _withdraw(gateway, user, reason, config=None):
    """A dispute, or a full refund of the latest payment: the subscription
    gives nothing from now on, and it is cancelled so it charges nothing
    more. Shown in the admin list as the reason."""
    user.subscription_revoked = reason
    sub_id = user.stripe_subscription_id
    if sub_id and user.subscription_status in SUBSCRIPTION_GRANTING_STATUSES:
        cancelled = gateway.cancel_subscription(sub_id)
        apply_subscription(cancelled, config)
        user.subscription_revoked = reason
    logger.warning("Subscription of user %s withdrawn: %s", user.id, reason)


def _handle_charge_event(gateway, etype, obj, config=None):
    charge = obj
    if etype == "charge.dispute.created":
        charge_id = obj.get("charge")
        if isinstance(charge_id, dict):
            charge_id = charge_id.get("id")
        if not charge_id:
            return "ignored"
        charge = gateway.retrieve_charge(charge_id)
    customer = charge.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    user = User.query.filter_by(stripe_customer_id=customer).first() \
        if customer else None
    if user is None:
        return "no_account"
    db.session.query(User).filter(User.id == user.id).with_for_update().one()
    db.session.refresh(user)
    if etype == "charge.dispute.created":
        _withdraw(gateway, user, "dispute", config)
        return "applied"
    if not _refunds_current_payment(gateway, user, charge):
        return "ignored"
    _withdraw(gateway, user, "refund", config)
    return "applied"


def _refunds_current_payment(gateway, user, charge):
    """A full refund of the current subscription's latest payment (a
    duplicate's refund belongs to another subscription)."""
    if not charge.get("refunded") and \
            charge.get("amount_refunded", 0) < charge.get("amount", 0):
        return False
    if not user.stripe_subscription_id:
        return False
    current = gateway.retrieve_subscription(user.stripe_subscription_id)
    _invoice, pi = _latest_payment_intent(gateway, current)
    charge_pi = charge.get("payment_intent")
    if isinstance(charge_pi, dict):
        charge_pi = charge_pi.get("id")
    return bool(pi) and pi == charge_pi


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


def _handle_checkout_event(gateway, etype, obj, config=None):
    if obj.get("mode") != "subscription":
        return "ignored"
    if etype == "checkout.session.expired":
        _set_checkout_state(obj, "expired")
        return "applied"
    _set_checkout_state(obj, "settled")
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


def handle_event(gateway, event, config=None):
    """Apply one verified event. Returns what was done ("applied",
    "ignored", "no_account"). Raises on a failure worth a retry (Stripe
    unreachable); the caller then answers with an error so Stripe sends
    the event again. Does not commit."""
    etype = event.get("type")
    obj = ((event.get("data") or {}).get("object")) or {}
    if etype not in HANDLED_EVENTS:
        return "ignored"
    if etype in CHARGE_EVENTS:
        return _handle_charge_event(gateway, etype, obj, config)
    if etype in CHECKOUT_EVENTS:
        return _handle_checkout_event(gateway, etype, obj, config)
    if etype in INVOICE_EVENTS:
        sub_id = subscription_id_of_invoice(obj)
    else:
        sub_id = obj.get("id")
    if not sub_id:
        return "ignored"
    user = settle_subscription(
        gateway, gateway.retrieve_subscription(sub_id), config)
    if (user is not None and etype in ("invoice.paid",
                                       "invoice.payment_succeeded")
            and user.stripe_subscription_id == sub_id):
        # The only proof of a payment (see _apply_payment_state): an
        # invoice above zero, paid. Customer credit (a referral's free
        # week, #439) can pay all of one, so its total counts, not only
        # what the card paid. The trial's ceiling ends with it, and the
        # monthly ceiling may change with it (an alpha tester's is
        # Standard's until the first payment, #438): a block that the new
        # ceiling no longer warrants is lifted.
        if ((obj.get("amount_paid") or 0) > 0
                or (obj.get("total") or 0) > 0):
            from backend.utils.spend import (
                get_user_spend_limit_usd, reconcile_user_spend_block)
            cfg = _config(config)
            limit_before = get_user_spend_limit_usd(user, cfg)
            user.subscription_ever_paid = True
            user.subscription_trial_capped = False
            if get_user_spend_limit_usd(user, cfg) != limit_before:
                reconcile_user_spend_block(user, cfg)
        # The friend's first payment by card gives the referral's free
        # weeks, once (#439).
        from backend.utils.referrals import grant_on_first_payment
        grant_on_first_payment(gateway, user, obj, config)
    return "applied" if user is not None else "no_account"


# ── Account deletion ────────────────────────────────────────────────────

def stop_renewal_for_deletion(user, config=None):
    """An account deletion was scheduled: its subscription does not renew
    (it ends at the end of the period already paid, before the purge).
    One whose payment failed (past_due, live at Stripe, retried) is
    cancelled at once: it gives nothing any more, and a retry must not
    charge an account being deleted. Best effort: a failure is logged;
    the purge cancels it again."""
    live = user.subscription_status in SUBSCRIPTION_GRANTING_STATUSES
    if not (user.stripe_subscription_id and live):
        return False
    if (user.subscription_status != "past_due"
            and user.subscription_cancel_at_period_end):
        return False
    try:
        gateway = get_gateway(config)
        if user.subscription_status == "past_due":
            # Marked as Loore's cancellation, so it does not count as the
            # owner cancelling (which would make a never-paid account
            # lapsed if it is restored).
            gateway.update_subscription(
                user.stripe_subscription_id,
                {"metadata": {LOORE_CANCELLED_METADATA_KEY: "deletion"}})
            sub = gateway.cancel_subscription(user.stripe_subscription_id)
            apply_subscription(sub, config)
            db.session.commit()
            return True
        # A downgrade waiting for the period end is a subscription
        # schedule, and Stripe refuses cancel_at_period_end on a
        # subscription a schedule manages: release it first (the
        # subscription ends with this period, so the downgrade is moot).
        current = gateway.retrieve_subscription(user.stripe_subscription_id)
        schedule = current.get("schedule")
        if isinstance(schedule, dict):
            schedule = schedule.get("id")
        if schedule:
            gateway.release_subscription_schedule(schedule)
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


def _missing(exc):
    return (getattr(exc, "http_status", None) == 404
            or getattr(exc, "code", None) == "resource_missing")


def cancel_for_purge(user, config=None):
    """The account is about to be deleted: a subscription that still runs
    is cancelled now. One Stripe has already ended, or does not know, is
    taken as ended. Raises when Stripe cannot be reached, so the purge
    waits and tries again rather than leave a subscription running for an
    account that no longer exists."""
    if not user.stripe_subscription_id or user.subscription_status in (
            "none", "ended"):
        return False
    gateway = get_gateway(config)
    try:
        sub = gateway.cancel_subscription(user.stripe_subscription_id)
    except Exception as e:  # noqa: BLE001 - read what it is now
        try:
            sub = gateway.retrieve_subscription(user.stripe_subscription_id)
        except Exception as e2:  # noqa: BLE001
            if not _missing(e2):
                raise
            user.subscription_status = "ended"
            return True
        if sub.get("status") not in ("canceled", "incomplete_expired"):
            raise e
    apply_subscription(sub, config)
    return True


def delete_customer_for_purge(user, config=None):
    """The account is about to be deleted: its Stripe customer goes too,
    with the name, email and address Checkout asked for (Stripe keeps
    what it must as merchant of record). A customer Stripe does not know
    is taken as gone. Raises when Stripe cannot be reached."""
    if not user.stripe_customer_id:
        return False
    gateway = get_gateway(config)
    try:
        gateway.delete_customer(user.stripe_customer_id)
    except Exception as e:  # noqa: BLE001
        if not _missing(e):
            raise
    user.stripe_customer_id = None
    return True
