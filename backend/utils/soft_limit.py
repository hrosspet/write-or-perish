"""Celery's soft time limit, let through the provider SDKs.

When a task passes its soft time limit, Celery raises SoftTimeLimitExceeded
in it, wherever it is, so that the task's handler can stop it cleanly
before the hard limit kills it. Inside a provider SDK's HTTP call the
exception never reaches the task: the Anthropic and OpenAI SDKs catch any
exception from sending a request as a connection error, send the request
again while retries remain, and then raise APIConnectionError from it.
Callers that take a connection error for "estimate instead"
(LLMProvider.count_tokens) or "the submission failed, try later"
(llm_batch.batch_submit) then swallowed it, and a profile run past its
soft limit went on rendering until the hard limit killed it, with no
handler run (2026-11-07).

- ``client_kwargs(sdk)``: ``http_client=`` for an SDK client, the SDK's
  default HTTP client whose send raises the soft limit as ``_SoftLimit``,
  a BaseException, which the SDK's ``except Exception`` does not catch:
  the request is not sent again.
- ``passes()``: around the SDK calls, turns that back into the
  SoftTimeLimitExceeded.
- ``found_in(exc)`` / ``reraise(exc)``: in an ``except Exception``
  handler, the soft limit that ``exc`` is or was raised from (raised in
  our own code, or by a client built without ``client_kwargs``).
"""
from contextlib import contextmanager

try:
    # Celery's class (celery.exceptions re-exports it), imported from
    # billiard so that tests which replace the celery package still get
    # the real class.
    from billiard.exceptions import SoftTimeLimitExceeded
except ImportError:  # pragma: no cover — billiard ships with Celery
    class SoftTimeLimitExceeded(Exception):
        pass


class _SoftLimit(BaseException):
    """Carries a SoftTimeLimitExceeded out of an SDK call without the SDK's
    retry. Only ``passes()`` catches it."""

    def __init__(self, exc):
        super().__init__(exc)
        self.exc = exc


def found_in(exc):
    """The SoftTimeLimitExceeded that ``exc`` is, carries, or was raised
    from (its __cause__ / __context__ chain), else None."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, SoftTimeLimitExceeded):
            return exc
        if isinstance(exc, _SoftLimit):
            return exc.exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def reraise(exc):
    """Raise the soft limit ``exc`` is or was raised from, if any."""
    found = found_in(exc)
    if found is not None:
        raise found


@contextmanager
def passes():
    """Around SDK calls made with ``client_kwargs``: the soft limit comes
    out as SoftTimeLimitExceeded."""
    try:
        yield
    except _SoftLimit as carrier:
        raise carrier.exc


_client_classes = {}


def _client_class(base):
    cls = _client_classes.get(base)
    if cls is None:
        class SoftLimitHttpxClient(base):
            def send(self, *args, **kwargs):
                try:
                    return super().send(*args, **kwargs)
                except SoftTimeLimitExceeded as e:
                    raise _SoftLimit(e) from e

            def __del__(self):
                # As the SDKs' own client wrappers: the SDK closes only a
                # client it created.
                try:
                    self.close()
                except Exception:  # noqa: BLE001
                    pass

        cls = _client_classes[base] = SoftLimitHttpxClient
    return cls


def client_kwargs(sdk):
    """``{"http_client": ...}`` for a client of ``sdk`` (the anthropic or
    openai module): its default HTTP client (timeouts, connection limits)
    letting the soft limit through. Empty when the module has no
    DefaultHttpxClient (a test's stand-in): the SDK's own client then, and
    ``found_in`` still finds the soft limit in its APIConnectionError."""
    base = getattr(sdk, "DefaultHttpxClient", None)
    if not isinstance(base, type):
        return {}
    return {"http_client": _client_class(base)()}
