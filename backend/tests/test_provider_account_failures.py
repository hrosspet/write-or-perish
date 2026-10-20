"""Model calls that fail for an account reason — a spend limit, billing,
the API key, a model id (#369, #360 item 3): each recognised error shape
of both providers becomes ProviderAccountError, whose text is the user's
message instead of the SDK's raw error; it is never retried and no other
provider is tried; the admin gets one email and one Sentry event per cause
per throttle window, and every Sentry event about it carries the cause's
stable fingerprint.

No real model call and no real email: the SDK clients are stand-ins and
the email sender is replaced."""
import sys
import types

import anthropic
import httpx
import openai
import pytest


@pytest.fixture
def providers(monkeypatch):
    # The real provider module, imported fresh (siblings stub it); see
    # test_provider_streaming.py.
    import backend
    monkeypatch.setattr(backend, "llm_providers",
                        getattr(backend, "llm_providers", None),
                        raising=False)
    monkeypatch.delitem(sys.modules, "backend.llm_providers", raising=False)
    import backend.llm_providers as mod
    monkeypatch.setattr(mod, "STREAM_RETRY_DELAYS", (0, 0))
    return mod


@pytest.fixture
def alerts(monkeypatch, providers):
    import backend.utils.provider_alerts as mod
    monkeypatch.setattr(mod, "_local_claims", {})
    monkeypatch.setattr(mod, "_redis", lambda config: None)
    return mod


@pytest.fixture
def mails(monkeypatch, alerts):
    """The alert emails, recorded instead of sent."""
    import backend.utils.email as email
    sent = []
    monkeypatch.setattr(email, "send_provider_account_alert_email",
                        lambda **kw: sent.append(kw))
    return sent


@pytest.fixture
def sentry(monkeypatch):
    """A stand-in sentry_sdk recording each captured message with the
    fingerprint and tags its scope had."""
    events = []

    class _Scope:
        def __init__(self):
            self.fingerprint = None
            self.tags = {}

        def set_tag(self, key, value):
            self.tags[key] = value

        def set_context(self, key, value):
            pass

    current = []

    class _NewScope:
        def __enter__(self):
            current.append(_Scope())
            return current[-1]

        def __exit__(self, *exc):
            current.pop()
            return False

    def capture_message(message, level=None):
        scope = current[-1]
        events.append({"message": message, "level": level,
                       "fingerprint": scope.fingerprint,
                       "tags": dict(scope.tags)})

    fake = types.ModuleType("sentry_sdk")
    fake.new_scope = _NewScope
    fake.capture_message = capture_message
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    return events


# ── The error shapes ─────────────────────────────────────────────────────

def _anthropic_error(status, etype, message, details=None):
    """What the Anthropic SDK raises for an HTTP error (its own status →
    class mapping); the body is the API's error shape (docs: Errors →
    Error shapes)."""
    error = {"type": etype, "message": message}
    if details is not None:
        error["details"] = details
    body = {"type": "error", "error": error, "request_id": "req_011"}
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request,
                              headers={"request-id": "req_011"})
    return anthropic.Anthropic(api_key="k")._make_status_error(
        str(body), body=body, response=response)


def _anthropic_stream_error(etype, message, details=None):
    """An error event after the 200 (streaming → Error events): the SDK
    raises APIStatusError with the stream's status."""
    error = {"type": etype, "message": message}
    if details is not None:
        error["details"] = details
    body = {"type": "error", "error": error}
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        str(body), response=httpx.Response(200, request=request), body=body)


def _openai_error(status, etype, code, message="boom"):
    body = {"error": {"message": message, "type": etype, "param": None,
                      "code": code}}
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(status, request=request)
    return openai.OpenAI(api_key="k")._make_status_error(
        f"Error code: {status} - {body}", body=body, response=response)


SPEND_LIMIT_ORG = (
    "You have reached your specified API usage limits. You will regain "
    "access on 2026-10-01 at 00:00 UTC.")
SPEND_LIMIT_WORKSPACE = (
    "You have reached your specified workspace API usage limits. You will "
    "regain access on 2026-10-01 at 00:00 UTC.")
TIER_CAP = (
    "You have reached your API usage limits: your organization has crossed "
    "its monthly API usage threshold, set based on your organization's API "
    "tier. You will regain access on 2026-09-01 at 00:00 UTC.")
CREDIT_LOW = (
    "Your credit balance is too low to access the Anthropic API. Please go "
    "to Plans & Billing to upgrade or purchase credits.")
CAP_DETAILS = {"error_code": "enforced_spend_limit_reached"}

ACCOUNT_ERRORS = [
    # Anthropic (docs: API → Errors; Rate limits → Spend limits)
    ("anthropic console spend limit (organization)",
     lambda: _anthropic_error(400, "invalid_request_error", SPEND_LIMIT_ORG),
     "spend_limit"),
    ("anthropic console spend limit (workspace)",
     lambda: _anthropic_error(400, "invalid_request_error",
                              SPEND_LIMIT_WORKSPACE),
     "spend_limit"),
    ("anthropic tier spend cap",
     lambda: _anthropic_error(429, "rate_limit_error", TIER_CAP,
                              CAP_DETAILS),
     "usage_cap"),
    ("anthropic tier spend cap, mid-stream",
     lambda: _anthropic_stream_error("rate_limit_error", TIER_CAP,
                                     CAP_DETAILS),
     "usage_cap"),
    ("anthropic credit balance too low",
     lambda: _anthropic_error(400, "invalid_request_error", CREDIT_LOW),
     "billing"),
    ("anthropic billing_error",
     lambda: _anthropic_error(402, "billing_error", "Payment required"),
     "billing"),
    ("anthropic authentication_error",
     lambda: _anthropic_error(401, "authentication_error",
                              "invalid x-api-key"),
     "auth"),
    ("anthropic permission_error",
     lambda: _anthropic_error(403, "permission_error",
                              "Your API key does not have permission"),
     "permission"),
    ("anthropic unknown model",
     lambda: _anthropic_error(404, "not_found_error",
                              "model: claude-retired-1"),
     "model_not_found"),
    # OpenAI (docs: Guides → Error codes)
    ("openai insufficient_quota",
     lambda: _openai_error(429, "insufficient_quota", "insufficient_quota"),
     "billing"),
    ("openai credit_balance_exhausted",
     lambda: _openai_error(429, "insufficient_quota",
                           "credit_balance_exhausted"),
     "billing"),
    ("openai organization spend limit",
     lambda: _openai_error(429, "insufficient_quota",
                           "organization_spend_limit_exceeded"),
     "spend_limit"),
    ("openai project spend limit",
     lambda: _openai_error(429, "insufficient_quota",
                           "project_spend_limit_exceeded"),
     "spend_limit"),
    ("openai organization usage limit",
     lambda: _openai_error(429, "insufficient_quota",
                           "organization_usage_limit_exceeded"),
     "usage_cap"),
    ("openai invalid key",
     lambda: _openai_error(401, "invalid_request_error", "invalid_api_key",
                           "Incorrect API key provided: sk-…"),
     "auth"),
    ("openai unsupported region / no access",
     lambda: _openai_error(403, "invalid_request_error",
                           "unsupported_country_region_territory"),
     "permission"),
    ("openai unknown model",
     lambda: _openai_error(404, "invalid_request_error", "model_not_found",
                           "The model `gpt-retired` does not exist or you "
                           "do not have access to it."),
     "model_not_found"),
]

OTHER_ERRORS = [
    ("anthropic bad request (our bug)",
     lambda: _anthropic_error(400, "invalid_request_error",
                              "messages: field required")),
    ("anthropic prompt too long",
     lambda: _anthropic_error(400, "invalid_request_error",
                              "prompt is too long: 300000 tokens > 200000 "
                              "maximum")),
    ("anthropic rate limit",
     lambda: _anthropic_error(429, "rate_limit_error", "Rate limited")),
    ("anthropic overloaded",
     lambda: _anthropic_error(529, "overloaded_error", "Overloaded")),
    ("anthropic overloaded, mid-stream",
     lambda: _anthropic_stream_error("overloaded_error", "Overloaded")),
    ("openai rate limit",
     lambda: _openai_error(429, "requests", "rate_limit_exceeded")),
    ("openai slow down",
     lambda: _openai_error(429, "rate_limit_error", "slow_down")),
    ("openai context overflow",
     lambda: _openai_error(400, "invalid_request_error",
                           "context_length_exceeded")),
    ("connection error",
     lambda: anthropic.APIConnectionError(request=httpx.Request(
         "POST", "https://api.anthropic.com/v1/messages"))),
]


@pytest.mark.parametrize("make,kind", [(m, k) for _, m, k in ACCOUNT_ERRORS],
                         ids=[name for name, _, _ in ACCOUNT_ERRORS])
def test_account_errors_are_recognised(providers, make, kind):
    assert providers.account_failure_kind(make()) == kind


@pytest.mark.parametrize("make", [m for _, m in OTHER_ERRORS],
                         ids=[name for name, _ in OTHER_ERRORS])
def test_other_errors_are_not_account_failures(providers, make):
    assert providers.account_failure_kind(make()) is None


def test_openai_stream_error_codes_are_recognised(providers):
    # The same codes, arriving after the 200 (a response.failed event).
    assert providers.account_failure_kind(providers.OpenAIStreamError(
        "quota", "insufficient_quota")) == "billing"
    assert providers.account_failure_kind(providers.OpenAIStreamError(
        "boom", "server_error")) is None


def test_anthropic_404_is_the_model_only_on_a_model_call(providers):
    # A batch poll's 404 is about the batch id, not the model.
    error = _anthropic_error(404, "not_found_error", "batch not found")
    assert providers.account_failure_kind(error, model_call=False) is None


# ── The user's message ───────────────────────────────────────────────────

MSGS = [{"role": "user", "content": "hello"}]


class _Usage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


def _message():
    class _Text:
        type = "text"
        text = "hi"

    class _Msg:
        content = [_Text()]
        usage = _Usage()
        stop_reason = "end_turn"
    return _Msg()


def _anthropic_client(outcomes, calls):
    """Anthropic stand-in: each stream() raises or returns the next
    outcome from get_final_message."""
    class _Stream:
        def __init__(self, outcome):
            self.outcome = outcome

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    class _Messages:
        def stream(self, **kwargs):
            calls.append(kwargs)
            return _Stream(outcomes.pop(0))

    class _Client:
        def __init__(self, api_key=None, **kwargs):
            self.messages = _Messages()

    return _Client


def _openai_client(outcomes, calls):
    """OpenAI stand-in: each responses.create(stream=True) raises the
    next outcome when iterated."""
    class _Stream:
        def __init__(self, outcome):
            self.outcome = outcome

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            raise self.outcome
            yield  # pragma: no cover

    class _Responses:
        def create(self, **kwargs):
            calls.append(kwargs)
            return _Stream(outcomes.pop(0))

    class _Client:
        def __init__(self, api_key=None, **kwargs):
            self.responses = _Responses()

    return _Client


def _refuse(*args, **kwargs):
    raise AssertionError("the other provider must never be called")


GENERIC_MESSAGE = ("AI replies are temporarily unavailable. This is a "
                   "problem on Loore's side, not yours, and it has been "
                   "reported.")
MODEL_MESSAGE = "This model isn't available any more. Choose another model."


def _users_message(kind):
    # Only a missing model has its own message: the other models still
    # work, so the user is told to choose one (#406 review).
    return MODEL_MESSAGE if kind == "model_not_found" else GENERIC_MESSAGE


@pytest.mark.parametrize("make,kind", [(m, k) for name, m, k in ACCOUNT_ERRORS
                                       if name.startswith("anthropic")])
def test_anthropic_account_error_reads_as_the_users_message(
        providers, mails, sentry, monkeypatch, make, kind):
    error = make()
    calls = []
    monkeypatch.setattr(providers, "Anthropic", _anthropic_client(
        [error, _message(), _message()], calls))
    monkeypatch.setattr(providers, "OpenAI", _refuse)
    with pytest.raises(providers.ProviderAccountError) as exc_info:
        providers.LLMProvider._call_anthropic("claude-x", MSGS, "k")
    text = str(exc_info.value)
    assert text == _users_message(kind)
    assert exc_info.value.kind == kind
    # None of the raw error reaches the user, nor the provider's name.
    for raw in ("Error code", "Anthropic", "request_id", "req_011",
                "invalid_request_error"):
        assert raw not in text
    # The provider's error stays on the chain, for the logs; one call
    # only: neither we nor the wrapper retry it.
    assert exc_info.value.__cause__ is error
    assert len(calls) == 1
    assert exc_info.value.provider == "Anthropic"
    assert exc_info.value.model == "claude-x"


@pytest.mark.parametrize("make,kind", [(m, k) for name, m, k in ACCOUNT_ERRORS
                                       if name.startswith("openai")])
def test_openai_account_error_reads_as_the_users_message(
        providers, mails, sentry, monkeypatch, make, kind):
    error = make()
    calls = []
    monkeypatch.setattr(providers, "OpenAI", _openai_client(
        [error, error, error], calls))
    monkeypatch.setattr(providers, "Anthropic", _refuse)
    with pytest.raises(providers.ProviderAccountError) as exc_info:
        providers.LLMProvider._call_openai("gpt-x", MSGS, "k")
    assert str(exc_info.value) == _users_message(kind)
    assert exc_info.value.kind == kind
    assert "Error code" not in str(exc_info.value)
    assert exc_info.value.__cause__ is error
    assert len(calls) == 1


def test_the_message_survives_celerys_round_trip(providers):
    # Celery stores a failed task's exception by its args and rebuilds it
    # with cls(*args); the profile progress endpoint shows str(task.info).
    err = providers.ProviderAccountError("Anthropic", "spend_limit", "m")
    rebuilt = providers.ProviderAccountError(*err.args)
    assert str(rebuilt) == str(err) == GENERIC_MESSAGE
    # The missing model's own message is its class's, so it survives too.
    err = providers.ModelUnavailableError("OpenAI", "model_not_found", "g")
    rebuilt = type(err)(*err.args)
    assert str(rebuilt) == str(err) == MODEL_MESSAGE
    assert isinstance(rebuilt, providers.ProviderAccountError)


def test_only_a_missing_model_has_its_own_message(providers):
    for kind in providers.ACCOUNT_FAILURE_KINDS:
        assert providers.account_error_class(kind).USER_MESSAGE == \
            _users_message(kind), kind


def test_no_fallback_to_the_other_provider(providers, mails, sentry,
                                           monkeypatch):
    # get_completion on an Anthropic model whose account is paused raises;
    # the OpenAI client is never built, whatever the user's settings.
    from flask import Flask
    app = Flask(__name__)
    app.config["SUPPORTED_MODELS"] = {
        "claude-x": {"provider": "anthropic", "api_model": "claude-x-1"},
        "gpt-x": {"provider": "openai", "api_model": "gpt-x"},
    }
    calls = []
    monkeypatch.setattr(providers, "Anthropic", _anthropic_client(
        [_anthropic_error(400, "invalid_request_error", SPEND_LIMIT_ORG)],
        calls))
    monkeypatch.setattr(providers, "OpenAI", _refuse)
    with app.app_context():
        with pytest.raises(providers.ProviderAccountError):
            providers.LLMProvider.get_completion(
                "claude-x", MSGS, {"anthropic": "k", "openai": "k2"})
    assert len(calls) == 1


# ── The alert: one per cause per window ──────────────────────────────────

def test_one_email_per_cause_within_the_window(providers, alerts, mails,
                                               sentry, monkeypatch):
    config = {"PROVIDER_ACCOUNT_ALERT_THROTTLE_SECONDS": 3600,
              "SPEND_ALERT_EMAIL": "admin@example.com"}
    clock = [1000.0]
    monkeypatch.setattr(alerts.time, "monotonic", lambda: clock[0])
    paused = _anthropic_error(400, "invalid_request_error", SPEND_LIMIT_ORG)

    def report(error, kind, provider="Anthropic", model="claude-x"):
        return alerts.report_account_failure(
            providers.ProviderAccountError(provider, kind, model), error,
            config=config)

    # Many failing calls, one cause: one email, one Sentry event.
    assert report(paused, "spend_limit") is True
    for _ in range(5):
        assert report(paused, "spend_limit") is False
    assert len(mails) == 1 and len(sentry) == 1
    mail = mails[0]
    assert mail["to_email"] == "admin@example.com"
    assert mail["provider"] == "Anthropic"
    assert mail["kind"] == "spend_limit"
    assert mail["model"] == "claude-x"
    assert mail["window_seconds"] == 3600
    assert mail["detail"]["status"] == 400
    assert mail["detail"]["message"].startswith(
        "You have reached your specified")
    assert mail["detail"]["request_id"] == "req_011"

    # Another cause gets its own alert.
    key = _anthropic_error(401, "authentication_error", "invalid x-api-key")
    assert report(key, "auth") is True
    assert len(mails) == 2

    # Once the window has passed, a cause that still fails alerts again.
    clock[0] += 3601
    assert report(paused, "spend_limit") is True
    assert len(mails) == 3


def test_the_window_is_shared_across_processes_through_redis(
        providers, alerts, mails, sentry, monkeypatch):
    class _FakeRedis:
        def __init__(self):
            self.store = {}

        def set(self, name, value, nx=False, ex=None):
            assert nx and ex == 600
            if name in self.store:
                return None
            self.store[name] = value
            return True

    shared = _FakeRedis()
    monkeypatch.setattr(alerts, "_redis", lambda config: shared)
    config = {"PROVIDER_ACCOUNT_ALERT_THROTTLE_SECONDS": 600}
    error = _openai_error(429, "insufficient_quota", "insufficient_quota")
    err = providers.ProviderAccountError("OpenAI", "billing", "gpt-x",
                                         "batch")
    assert alerts.report_account_failure(err, error, config=config)
    # A second process: its own in-memory claims are empty, Redis isn't.
    monkeypatch.setattr(alerts, "_local_claims", {})
    assert not alerts.report_account_failure(err, error, config=config)
    assert len(mails) == 1
    assert list(shared.store) == [
        "loore:provider-account-alert:openai:billing:batch"]


def test_a_failed_email_does_not_fail_the_call(providers, alerts, sentry,
                                               monkeypatch):
    def broken(**kw):
        raise OSError("SMTP down")
    err = providers.ProviderAccountError("OpenAI", "auth", "gpt-x")
    error = _openai_error(401, "invalid_request_error", "invalid_api_key")
    assert alerts.report_account_failure(
        err, error, config={}, send_email=broken) is True
    # Sentry still has it.
    assert len(sentry) == 1


def test_the_default_window_and_an_empty_setting(alerts):
    assert alerts.throttle_seconds({}) == 6 * 3600
    # An unset compose variable arrives as an empty string.
    assert alerts.throttle_seconds(
        {"PROVIDER_ACCOUNT_ALERT_THROTTLE_SECONDS": ""}) == 6 * 3600


def test_a_live_call_reports_once(providers, mails, sentry, monkeypatch):
    # Through the provider call itself: many users hitting the same
    # paused account send one email.
    def paused():
        return _anthropic_error(429, "rate_limit_error", TIER_CAP,
                                CAP_DETAILS)
    monkeypatch.setattr(providers, "Anthropic", _anthropic_client(
        [paused() for _ in range(4)], []))
    for _ in range(4):
        with pytest.raises(providers.ProviderAccountError):
            providers.LLMProvider._call_anthropic("claude-x", MSGS, "k")
    assert len(mails) == 1 and mails[0]["kind"] == "usage_cap"
    assert len(sentry) == 1


# ── Sentry: one issue per cause ──────────────────────────────────────────

def test_the_fingerprint_is_stable_per_cause(providers, alerts):
    Err = providers.ProviderAccountError
    a = Err("Anthropic", "spend_limit", "claude-x", "chat")
    b = Err("Anthropic", "spend_limit", "claude-y", "chat")
    assert alerts.fingerprint(a) == alerts.fingerprint(b) == [
        "provider-account-failure", "anthropic", "spend_limit", "chat"]
    assert alerts.fingerprint(Err("OpenAI", "spend_limit", None, "chat")) \
        != alerts.fingerprint(a)
    assert alerts.fingerprint(Err("Anthropic", "auth", None, "chat")) != \
        alerts.fingerprint(a)
    # Another key is another cause: it is another thing to fix.
    assert alerts.fingerprint(Err("Anthropic", "spend_limit", "claude-x",
                                  "train")) != alerts.fingerprint(a)
    # A call site that didn't name the key.
    assert alerts.fingerprint(Err("Anthropic", "auth")) == [
        "provider-account-failure", "anthropic", "auth", "unknown"]
    # An unknown model is its own cause per model id.
    assert alerts.fingerprint(providers.ModelUnavailableError(
        "OpenAI", "model_not_found", "gpt-old", "chat")) == [
        "provider-account-failure", "openai", "model_not_found", "chat",
        "gpt-old"]


def test_the_sentry_event_carries_the_fingerprint(providers, alerts, mails,
                                                  sentry):
    err = providers.ProviderAccountError("Anthropic", "billing", "claude-x",
                                         "train")
    alerts.report_account_failure(
        err, _anthropic_error(402, "billing_error", "Payment required"),
        config={})
    assert sentry == [{
        "message": "Model provider account failure: Anthropic (billing)",
        "level": "error",
        "fingerprint": ["provider-account-failure", "anthropic", "billing",
                        "train"],
        "tags": {"provider": "anthropic", "account_failure": "billing",
                 "key_role": "train", "model": "claude-x"},
    }]


def test_before_send_groups_every_event_about_the_failure(providers,
                                                          alerts):
    err = providers.ProviderAccountError("OpenAI", "billing", "gpt-x", "chat")
    expected = ["provider-account-failure", "openai", "billing", "chat"]
    # The task failure itself.
    event = alerts.apply_fingerprint({}, err)
    assert event["fingerprint"] == expected
    # An error raised from it.
    try:
        try:
            raise err
        except providers.ProviderAccountError as inner:
            raise RuntimeError("gave up") from inner
    except RuntimeError as outer:
        event = alerts.apply_fingerprint({}, outer)
    assert event["fingerprint"] == expected
    # A log line without an exception that asks for it (log_extra), such
    # as ProfileGenerationTask.on_failure's.
    import logging
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed",
                               None, None)
    for key, value in alerts.log_extra(err).items():
        setattr(record, key, value)
    event = alerts.apply_fingerprint({"message": "failed"}, None, record)
    assert event["fingerprint"] == expected
    # Anything else keeps Sentry's own grouping.
    assert "fingerprint" not in alerts.apply_fingerprint(
        {}, ValueError("x"))
    assert "fingerprint" not in alerts.apply_fingerprint({}, None)
    assert alerts.log_extra(ValueError("x")) == {}


def test_an_unrelated_error_in_the_handler_keeps_its_own_grouping(
        providers, alerts):
    """An exception raised while an account failure is being handled (a
    DB commit in the task's error handler) only has it as __context__.
    It is not about the account, so it is not grouped under it; neither
    is a log line written while it is handled without log_extra (#406
    review, minor 1)."""
    err = providers.ProviderAccountError("Anthropic", "spend_limit",
                                         "claude-x", "chat")
    try:
        try:
            raise err
        except providers.ProviderAccountError:
            raise ConnectionError("database connection lost")
    except ConnectionError as db_error:
        assert db_error.__context__ is err  # what Python links
        assert "fingerprint" not in alerts.apply_fingerprint({}, db_error)
        # Nor a message-only event logged while it is handled.
        assert "fingerprint" not in alerts.apply_fingerprint(
            {"message": "commit failed"}, None)
    try:
        raise err
    except providers.ProviderAccountError:
        assert "fingerprint" not in alerts.apply_fingerprint(
            {"message": "something else failed"}, None)


def test_the_poll_cap_failure_is_grouped_under_the_account_failure(
        providers, alerts):
    # The task raises MaxRetriesExceededError at the poll cap with the
    # account failure set as its __cause__ (llm_completion), so that task
    # failure still groups with it.
    err = providers.ProviderAccountError("OpenAI", "auth", None, "chat")
    cap = RuntimeError("max retries exceeded")
    cap.__cause__ = err
    assert alerts.apply_fingerprint({}, cap)["fingerprint"] == [
        "provider-account-failure", "openai", "auth", "chat"]


# ── The email ────────────────────────────────────────────────────────────

def test_the_alert_email(providers, monkeypatch):
    import backend.utils.email as email
    delivered = []
    monkeypatch.setattr(email, "_deliver",
                        lambda *args: delivered.append(args))
    email.send_provider_account_alert_email(
        to_email="admin@example.com", provider="Anthropic",
        kind="spend_limit", model="claude-x",
        detail={"status": 400, "type": "invalid_request_error",
                "code": None, "message": SPEND_LIMIT_WORKSPACE,
                "request_id": "req_011"},
        window_seconds=21600, key_role="train", environment="staging",
        user_message=GENERIC_MESSAGE)
    (to, subject, text, html), = delivered
    assert to == "admin@example.com"
    # Staging mails the same inbox as prod: the subject says which.
    assert subject == \
        "[staging] Loore: Anthropic calls are failing (spend_limit)"
    assert "Deployment: staging" in text
    # Which key to fix: its role and setting name.
    assert "API key: train (ANTHROPIC_API_KEY_TRAIN)" in text
    assert "ANTHROPIC_API_KEY_TRAIN" in html
    assert "a spend limit set in the provider's console" in text
    assert SPEND_LIMIT_WORKSPACE in text and "req_011" in text
    assert f'Users see "{GENERIC_MESSAGE}"' in text
    assert "not emailed for 6 hours" in text
    assert "Error code: None" not in text  # empty fields are left out


def test_the_alert_email_for_a_missing_model(providers, monkeypatch):
    import backend.utils.email as email
    delivered = []
    monkeypatch.setattr(email, "_deliver",
                        lambda *args: delivered.append(args))
    email.send_provider_account_alert_email(
        to_email="admin@example.com", provider="OpenAI",
        kind="model_not_found", model="gpt-old",
        detail={"status": 404, "code": "model_not_found"},
        window_seconds=3600, key_role="chat/legacy", environment="prod")
    (_, subject, text, _), = delivered
    assert subject == \
        "[prod] Loore: OpenAI calls are failing (model_not_found)"
    assert f'Users see "{MODEL_MESSAGE}"' in text
    assert "API key: chat/legacy (OPENAI_API_KEY_CHAT, OPENAI_API_KEY)" \
        in text


@pytest.mark.parametrize("url,label", [
    ("https://loore.org", "prod"),
    ("https://www.loore.org/", "prod"),
    ("https://staging.loore.org", "staging"),
    ("http://localhost:3000", "local"),
    ("http://127.0.0.1:3001", "local"),
    ("", "local"),
    (None, "local"),
    ("https://loore.example.com", "loore.example.com"),
])
def test_the_deployment_label(alerts, url, label):
    assert alerts.environment_label({"FRONTEND_URL": url}) == label


def test_the_key_role(providers):
    from backend.utils.api_keys import api_key_role
    config = {"ANTHROPIC_API_KEY_CHAT": "sk-ant-chat",
              "ANTHROPIC_API_KEY_TRAIN": "sk-ant-train",
              "ANTHROPIC_API_KEY": "sk-ant-legacy",
              "OPENAI_API_KEY_CHAT": "",          # unset: an empty string
              "OPENAI_API_KEY_BATCH": "sk-oai-batch",
              "OPENAI_API_KEY": "sk-oai-legacy"}
    assert api_key_role(config, "Anthropic", "sk-ant-chat") == "chat"
    assert api_key_role(config, "anthropic", "sk-ant-train") == "train"
    assert api_key_role(config, "Anthropic", "sk-ant-legacy") == "legacy"
    assert api_key_role(config, "OpenAI", "sk-oai-batch") == "batch"
    assert api_key_role(config, "OpenAI", "sk-oai-legacy") == "legacy"
    # One key set in two settings: both roles.
    assert api_key_role({**config, "OPENAI_API_KEY_CHAT": "sk-oai-legacy"},
                        "OpenAI", "sk-oai-legacy") == "chat/legacy"
    # Not a configured key, no key: unknown. An empty setting never matches.
    assert api_key_role(config, "Anthropic", "sk-other") == "unknown"
    assert api_key_role(config, "OpenAI", None) == "unknown"
    assert api_key_role(config, "OpenAI", "") == "unknown"
    # Another provider's key is not this provider's.
    assert api_key_role(config, "OpenAI", "sk-ant-chat") == "unknown"


def test_two_keys_failing_send_two_emails(providers, alerts, mails, sentry):
    paused = _anthropic_error(400, "invalid_request_error", SPEND_LIMIT_ORG)
    for role in ("train", "chat", "train", "chat"):
        alerts.report_account_failure(
            providers.ProviderAccountError("Anthropic", "spend_limit",
                                           "claude-x", role),
            paused, config={})
    assert [m["key_role"] for m in mails] == ["train", "chat"]


def test_a_live_call_names_its_key_and_deployment_never_the_key(
        providers, alerts, sentry, monkeypatch):
    """End to end through the provider call and the real email (only the
    SMTP delivery is replaced): the alert names the deployment and the
    key's role and setting; no part of the key is anywhere in it."""
    from flask import Flask
    import backend.utils.email as email
    delivered = []
    monkeypatch.setattr(email, "_deliver",
                        lambda *args: delivered.append(args))
    app = Flask(__name__)
    secret = "sk-ant-api03-TRAINKEY0123456789abcdef"
    app.config.update(
        ANTHROPIC_API_KEY_CHAT="sk-ant-api03-CHATKEY0123456789abcdef",
        ANTHROPIC_API_KEY_TRAIN=secret,
        ANTHROPIC_API_KEY="",
        FRONTEND_URL="https://staging.loore.org",
        SPEND_ALERT_EMAIL="admin@example.com")
    revoked = _anthropic_error(401, "authentication_error",
                               "invalid x-api-key")
    monkeypatch.setattr(providers, "Anthropic",
                        _anthropic_client([revoked], []))
    with app.app_context():
        with pytest.raises(providers.ProviderAccountError) as exc_info:
            providers.LLMProvider._call_anthropic("claude-x", MSGS, secret)
    assert exc_info.value.key_role == "train"
    (to, subject, text, html), = delivered
    assert to == "admin@example.com"
    assert subject == "[staging] Loore: Anthropic calls are failing (auth)"
    assert "API key: train (ANTHROPIC_API_KEY_TRAIN)" in text
    for part in (subject, text, html):
        assert "TRAINKEY" not in part and "sk-ant" not in part
        assert secret[-4:] not in part
    assert sentry[0]["tags"]["key_role"] == "train"
