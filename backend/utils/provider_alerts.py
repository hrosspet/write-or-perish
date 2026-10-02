"""One admin alert per cause when model calls fail for an account reason
(#369, #360 item 3).

A spend limit, a billing problem, a bad API key or a retired model id
fails every call to that provider until someone fixes the account, so the
first failure is what matters: backend/llm_providers.py recognises it
(account_failure_kind) and calls report_account_failure, which

- logs a warning for every failed call;
- for the first failure of a cause in PROVIDER_ACCOUNT_ALERT_THROTTLE_SECONDS,
  emails SPEND_ALERT_EMAIL (the spend monitor's recipient, #85) and sends
  one Sentry event.

A cause is the provider and the kind (plus the model id for an unknown
model). The window is claimed in Redis (SET NX EX), so one email goes out
across the web and worker processes; without Redis (tests, a local run
without it) each process keeps its own claims.

Every Sentry event whose exception is, or was raised from, a
ProviderAccountError — or, for a log message without an exception, that
was logged while one was being handled — gets the cause's fingerprint
(``apply_fingerprint``, called from the before_send hook in
backend/__init__.py), so the task failures and error logs of all those
calls group into one issue per cause, next to the event sent here.
"""
import logging
import sys
import threading
import time

logger = logging.getLogger(__name__)

# Default window: one email per cause per 6 hours. A spend limit or a
# revoked key stays broken until someone acts, so the alert repeats while
# calls keep failing, at most four times a day.
DEFAULT_THROTTLE_SECONDS = 6 * 3600

KEY_PREFIX = "loore:provider-account-alert:"
FINGERPRINT_PREFIX = "provider-account-failure"

_local_claims = {}
_local_lock = threading.Lock()
_client = None


def cause_key(err):
    """The cause of a ProviderAccountError as a tuple of strings: provider,
    kind, and the model id when the model is the problem."""
    parts = [(err.provider or "unknown").lower(), err.kind or "unknown"]
    if err.kind == "model_not_found" and err.model:
        parts.append(str(err.model))
    return tuple(parts)


def fingerprint(err):
    """The Sentry fingerprint of a cause: the same for every failed call
    of that cause, whatever the call site or the request."""
    return [FINGERPRINT_PREFIX, *cause_key(err)]


def account_failure_in(exc):
    """The ProviderAccountError that *exc* is or was raised from (its
    __cause__ / __context__ chain), or None."""
    from backend.llm_providers import ProviderAccountError
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, ProviderAccountError):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def apply_fingerprint(event, exc=None):
    """Sentry before_send: group an event about an account failure under
    its cause's fingerprint. Returns the event.

    *exc* is the event's exception. A log message without one (e.g. the
    task's "LLM completion failed for node …" line) is about the
    exception being handled when it was logged: before_send runs in the
    logging thread, so that is sys.exc_info()."""
    if exc is None:
        exc = sys.exc_info()[1]
    err = account_failure_in(exc) if exc is not None else None
    if err is not None:
        event["fingerprint"] = fingerprint(err)
    return event


def throttle_seconds(config):
    try:
        value = int(config.get("PROVIDER_ACCOUNT_ALERT_THROTTLE_SECONDS")
                    or DEFAULT_THROTTLE_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_THROTTLE_SECONDS
    return max(value, 1)


def _redis(config):
    global _client
    if config.get("TESTING"):
        return None
    if _client is None:
        url = config.get("CELERY_BROKER_URL")
        if not url:
            return None
        import redis
        _client = redis.Redis.from_url(
            url, socket_timeout=0.5, socket_connect_timeout=0.5)
    return _client


def claim(key, window, config):
    """True for the first claim of *key* in *window* seconds — across
    processes when Redis answers, else within this process."""
    name = KEY_PREFIX + ":".join(key)
    try:
        client = _redis(config)
        if client is not None:
            return bool(client.set(name, str(int(time.time())),
                                   nx=True, ex=window))
    except Exception:
        logger.warning("Provider alert throttle: Redis unavailable; "
                       "throttling in this process only", exc_info=True)
    now = time.monotonic()
    with _local_lock:
        until = _local_claims.get(name)
        if until is not None and until > now:
            return False
        _local_claims[name] = now + window
        return True


def _capture(err, detail):
    """One Sentry event for the cause, under its fingerprint."""
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.fingerprint = fingerprint(err)
            scope.set_tag("provider", (err.provider or "").lower())
            scope.set_tag("account_failure", err.kind)
            if err.model:
                scope.set_tag("model", str(err.model))
            scope.set_context("provider_error", detail)
            sentry_sdk.capture_message(
                f"Model provider account failure: {err.provider} "
                f"({err.kind})", level="error")
    except Exception:  # pragma: no cover — reporting must not mask it
        logger.exception("Sentry report of the account failure failed")


def report_account_failure(err, exc, config=None, send_email=None):
    """Report a ProviderAccountError *err* raised from the provider's
    error *exc*. Returns True when this call sent the alert (the first of
    its cause in the window), False when the window had already been
    claimed. ``config`` and ``send_email`` are injectable for tests;
    they default to the app's config and the real sender."""
    from backend.llm_providers import provider_error_detail
    detail = provider_error_detail(exc)
    key = cause_key(err)
    logger.warning(
        "Model provider account failure %s (status=%s type=%s code=%s "
        "request_id=%s): %s", ":".join(key), detail.get("status"),
        detail.get("type"), detail.get("code"), detail.get("request_id"),
        detail.get("message"))
    if config is None:
        try:
            from flask import current_app
            config = current_app.config
        except RuntimeError:  # outside an app context (a script)
            config = {}
    window = throttle_seconds(config)
    if not claim(key, window, config):
        return False
    _capture(err, detail)
    if send_email is None:
        from backend.utils.email import send_provider_account_alert_email
        send_email = send_provider_account_alert_email
    try:
        send_email(
            to_email=config.get("SPEND_ALERT_EMAIL") or "signup@loore.org",
            provider=err.provider, kind=err.kind, model=err.model,
            detail=detail, window_seconds=window)
    except Exception:
        # The window stays claimed: retrying the mail on every failed
        # call would slow each of them by the SMTP timeout. Sentry has
        # the event.
        logger.exception("Failed to send the provider account alert "
                         "email (%s)", ":".join(key))
    return True
