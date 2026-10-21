"""Exception text stays out of responses, logs and Sentry (Peter,
2026-10-20).

A database error's text quotes the row it refused: the failing SQL's
parameters, and Postgres's DETAIL line ("Failing row contains (…)",
"Key (…)=(…)"). A refused save can therefore carry what the user typed,
and other exceptions can quote their input too. What is kept everywhere
is what failed and where: the exception's type and its stack frames.

- Responses carry a message written for the user, never ``str(e)``.
- ``SQLALCHEMY_ENGINE_OPTIONS["hide_parameters"]`` (config.py) keeps the
  SQL parameters out of every database error's text.
- ``install_traceback_scrubbing`` (run when the backend package is
  imported): every traceback a ``backend.*`` logger writes keeps each
  exception's type and frames, without its message, whatever handlers
  are configured. That covers Flask's log line for an unhandled exception
  and every ``logger.exception``.
- Log lines name the exception with ``describe_exception``: its type and
  the value-free codes of ``safe_exception_summary``.
- ``scrub_sentry_event`` and ``scrub_sentry_breadcrumb`` do the same for
  what reaches Sentry: exception messages are replaced, log messages lose
  the text of the exceptions they quote and any database values.
"""

import logging
import re
import sys
import traceback

# Shown in place of an exception's message, in Sentry and in the log.
MESSAGE_REMOVED = "[message removed]"

# Exception text shorter than this is not looked for in log messages: a
# one- to three-character message ("0", "id") would match unrelated text.
MIN_MATCH_LENGTH = 4

# Codes and schema names are kept only when they look like identifiers.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# Text scrubbed for Sentry is cut after this many characters and the rest
# replaced by a note, so the patterns below run over a bounded length.
MAX_SCRUB_CHARS = 16384


def exception_chain(exc):
    """*exc* and the exceptions it was raised from or while handling,
    outermost first."""
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        chain.append(exc)
        if exc.__cause__ is not None:
            exc = exc.__cause__
        elif not exc.__suppress_context__:
            exc = exc.__context__
        else:
            exc = None
    return chain


def _identifier(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and _IDENTIFIER.match(value):
        return value
    return None


def _database_codes(exc):
    """SQLSTATE and the constraint, table and column a database error
    names (SQLAlchemy keeps the driver's error in .orig)."""
    db_error = getattr(exc, "orig", None) or exc
    sqlstate = _identifier(getattr(db_error, "pgcode", None)
                           or getattr(db_error, "sqlstate", None))
    parts = [f"sqlstate {sqlstate}"] if sqlstate else []
    diag = getattr(db_error, "diag", None)
    for label, attr in (("constraint", "constraint_name"),
                        ("table", "table_name"), ("column", "column_name")):
        name = (_identifier(getattr(diag, attr, None))
                if diag is not None else None)
        if name:
            parts.append(f"{label} {name}")
    return parts


def _other_codes(exc):
    """An HTTP status, an error code and an OS error's errno."""
    parts = []
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    status = _identifier(status) if isinstance(status, int) else None
    if status:
        parts.append(f"status {status}")
    # SQLAlchemy's .code names a page of its docs, not the failure
    code = (None if type(exc).__module__.startswith("sqlalchemy")
            else getattr(exc, "code", None))
    code = _identifier(code) if isinstance(code, (int, str)) else None
    if code and code != status:
        parts.append(f"code {code}")
    errno = _identifier(getattr(exc, "errno", None))
    if errno and isinstance(getattr(exc, "errno", None), int):
        parts.append(f"errno {errno}")
    return parts


def safe_exception_summary(exc):
    """Codes and schema names in *exc* that hold no values: a database
    error's SQLSTATE and the constraint, table and column it names, an
    HTTP status and error code, an OS error's errno. '' when none."""
    try:
        return ", ".join(_database_codes(exc) + _other_codes(exc))
    except Exception:  # never fail while describing a failure
        return ""


def describe_exception(exc):
    """For a log line: the type, with safe_exception_summary's codes when
    there are any, e.g. "IntegrityError (sqlstate 23505, constraint
    user_username_key, table user)". Never the message."""
    name = type(exc).__name__
    summary = safe_exception_summary(exc)
    return f"{name} ({summary})" if summary else name


def _type_line(exc):
    cls = type(exc)
    module = getattr(cls, "__module__", None)
    name = getattr(cls, "__qualname__", None) or cls.__name__
    if module not in (None, "builtins", "__main__"):
        name = f"{module}.{name}"
    summary = safe_exception_summary(exc)
    tail = f" ({summary})" if summary else ""
    return f"{name}: {MESSAGE_REMOVED}{tail}\n"


def format_exception_without_messages(exc):
    """The traceback Python would print for *exc*, its causes included,
    with each exception's message replaced by MESSAGE_REMOVED (and the
    codes from safe_exception_summary). No trailing newline, like
    logging.Formatter.formatException."""
    innermost_first = list(reversed(exception_chain(exc)))
    lines = []
    for i, current in enumerate(innermost_first):
        if i:
            if current.__cause__ is innermost_first[i - 1]:
                lines.append("\nThe above exception was the direct cause "
                             "of the following exception:\n\n")
            else:
                lines.append("\nDuring handling of the above exception, "
                             "another exception occurred:\n\n")
        if current.__traceback__ is not None:
            lines.append("Traceback (most recent call last):\n")
            lines.extend(traceback.format_tb(current.__traceback__))
        lines.append(_type_line(current))
    return "".join(lines).rstrip("\n")


def install_traceback_scrubbing(prefix="backend"):
    """Make every log record from the *prefix* logger or one below it
    carry its traceback without exception messages
    (format_exception_without_messages), set as ``record.exc_text`` when
    the record is made. Formatters reuse ``exc_text`` when it is set, so
    every handler writes the traceback this way whatever logging setup is
    in place (gunicorn's, Celery's, ``basicConfig``, pytest's). Sentry
    reads ``exc_info``, not the text, and keeps its structured stack
    trace. Other loggers' records are left alone. Once per process."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "scrubs_tracebacks", False):
        return
    dotted = prefix + "."

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            exc_info = record.exc_info
            if (isinstance(exc_info, tuple) and len(exc_info) > 1
                    and exc_info[1] is not None
                    and (record.name == prefix
                         or record.name.startswith(dotted))):
                record.exc_text = format_exception_without_messages(
                    exc_info[1])
        except Exception:  # logging must go on (the full traceback then)
            pass
        return record

    factory.scrubs_tracebacks = True
    logging.setLogRecordFactory(factory)


# --- Text scrubbing --------------------------------------------------------

# What may follow a Postgres DETAIL / CONTEXT / LINE block in an error's
# text. A value inside the block can hold newlines, so the block runs until
# one of these, not until the end of its line.
_BLOCK_END = (r"(?=\n(?:HINT|CONTEXT|WHERE|QUERY|STATEMENT|LINE \d+):"
              r"|\n\[SQL: |\n\[parameters: |\n\[SQL parameters hidden"
              r"|\n\(Background on this error|\Z)")
# Every pattern either ends at _BLOCK_END (which matches at the end of the
# text at the latest) or scans a bounded or quote-free span, so a run over
# the text is linear in its length. The LINE block is replaced last: the
# other blocks end where it starts.
_DATABASE_VALUES = [
    # A value Postgres could not convert, quoted in the error itself; the
    # value can hold quotes and newlines, so it runs to the next block
    (re.compile(r'((?:invalid input (?:syntax|value) for|field value out '
                r'of range)[^:\n]{0,128}: )".*?' + _BLOCK_END, re.S),
     r"\1[removed]"),
    # A number too large for its type: digits, no quotes or newlines
    (re.compile(r'value "[^"\n]*" is out of range'),
     "value [removed] is out of range"),
    (re.compile(r"DETAIL:.*?" + _BLOCK_END, re.S), "DETAIL: [removed]"),
    (re.compile(r"CONTEXT:.*?" + _BLOCK_END, re.S), "CONTEXT: [removed]"),
    (re.compile(r"Failing row contains .*?" + _BLOCK_END, re.S),
     "Failing row contains [removed]"),
    (re.compile(r"Key \(.*?" + _BLOCK_END, re.S), "Key [removed]"),
    # The statement as Postgres received it: psycopg2 sends the values
    # inline, so the excerpt quotes them
    (re.compile(r"LINE \d+: .*?" + _BLOCK_END, re.S), "LINE: [removed]"),
    (re.compile(r"\[parameters: .*?(?=\n\(Background on this error|\Z)",
                re.S), "[parameters: removed]"),
]


def strip_database_values(text):
    """*text* without the values a Postgres or SQLAlchemy error quotes:
    DETAIL, CONTEXT and LINE blocks, "Failing row contains (…)",
    "Key (…)=(…)", an unconvertible value ('invalid input syntax for type
    integer: "…"') and SQLAlchemy's "[parameters: …]". Text longer than
    MAX_SCRUB_CHARS is cut there first."""
    if len(text) > MAX_SCRUB_CHARS:
        text = (text[:MAX_SCRUB_CHARS]
                + f" [{len(text) - MAX_SCRUB_CHARS} more characters removed]")
    for pattern, replacement in _DATABASE_VALUES:
        text = pattern.sub(replacement, text)
    return text


def _texts_of(exc):
    """The ways *exc*'s text can appear in a message, longest first."""
    texts = set()
    for render in (str, repr):
        try:
            texts.add(render(exc))
        except Exception:
            pass
    for arg in getattr(exc, "args", ()) or ():
        if isinstance(arg, str):
            texts.add(arg)
    message = getattr(exc, "message", None)
    if isinstance(message, str):
        texts.add(message)
    return sorted((t for t in texts if len(t) >= MIN_MATCH_LENGTH),
                  key=len, reverse=True)


def scrub_text(text, exceptions=()):
    """*text* with each of *exceptions*' text replaced by its type name,
    then without database values (strip_database_values)."""
    if not isinstance(text, str) or not text:
        return text
    for exc in exceptions:
        name = type(exc).__name__
        for exc_text in _texts_of(exc):
            if exc_text in text:
                text = text.replace(exc_text, name)
    return strip_database_values(text)


def _exceptions_for(hint):
    """The exceptions whose text an event or breadcrumb may quote: the
    hint's, the log record's (its exc_info and exceptions passed as
    arguments), and the one being handled when the event was made: a log
    line written inside an ``except`` block with the exception in an
    f-string. Sentry runs these hooks in the thread that logged."""
    found = []
    hint = hint or {}
    exc_info = hint.get("exc_info")
    if isinstance(exc_info, tuple) and len(exc_info) > 1:
        found.append(exc_info[1])
    record = hint.get("log_record")
    if record is not None:
        if isinstance(record.exc_info, tuple):
            found.append(record.exc_info[1])
        args = record.args
        if isinstance(args, dict):
            args = tuple(args.values())
        elif not isinstance(args, tuple):
            args = (args,)
        found.extend(a for a in args if isinstance(a, BaseException))
    found.append(sys.exc_info()[1])
    chain = []
    for exc in found:
        if isinstance(exc, BaseException):
            for link in exception_chain(exc):
                if all(link is not known for known in chain):
                    chain.append(link)
    return chain


def _scrub_params(params, exceptions):
    def one(value):
        if isinstance(value, BaseException):
            return type(value).__name__
        if isinstance(value, str):
            return scrub_text(value, exceptions)
        return value
    if isinstance(params, dict):
        return {k: one(v) for k, v in params.items()}
    if isinstance(params, (list, tuple)):
        return [one(v) for v in params]
    return one(params)


def _scrub_exception_values(event, exceptions):
    by_type = {}
    for exc in exceptions:
        cls = type(exc)
        by_type.setdefault(
            getattr(cls, "__qualname__", None) or cls.__name__, exc)
    for value in (event.get("exception") or {}).get("values") or ():
        exc = by_type.get(value.get("type"))
        summary = safe_exception_summary(exc) if exc is not None else ""
        if value.get("value") or summary:
            value["value"] = (f"{MESSAGE_REMOVED} ({summary})" if summary
                              else MESSAGE_REMOVED)
        for frame in (value.get("stacktrace") or {}).get("frames") or ():
            frame.pop("vars", None)
    for thread in (event.get("threads") or {}).get("values") or ():
        for frame in (thread.get("stacktrace") or {}).get("frames") or ():
            frame.pop("vars", None)


def _scrub_messages(event, exceptions):
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        for key in ("message", "formatted"):
            if key in logentry:
                logentry[key] = scrub_text(logentry[key], exceptions)
        if logentry.get("params"):
            logentry["params"] = _scrub_params(logentry["params"], exceptions)
    message = event.get("message")
    if isinstance(message, str):
        event["message"] = scrub_text(message, exceptions)
    elif isinstance(message, dict):
        for key in ("message", "formatted"):
            if key in message:
                message[key] = scrub_text(message[key], exceptions)


def _scrub_breadcrumbs(event):
    crumbs = event.get("breadcrumbs")
    if isinstance(crumbs, dict):
        crumbs = crumbs.get("values")
    for crumb in crumbs or ():
        if isinstance(crumb, dict) and isinstance(crumb.get("message"), str):
            crumb["message"] = strip_database_values(crumb["message"])


def scrub_sentry_event(event, hint):
    """Sentry before_send: the event keeps each exception's type and stack
    frames; messages are replaced (safe_exception_summary is kept), log
    messages lose the text of the exceptions they quote, and database
    values are stripped from every message and breadcrumb. A part that
    cannot be scrubbed is dropped rather than sent as it is."""
    try:
        exceptions = _exceptions_for(hint)
    except Exception:
        exceptions = []
    for scrub, drop in ((_scrub_exception_values, _drop_exception_values),
                        (_scrub_messages, _drop_messages)):
        try:
            scrub(event, exceptions)
        except Exception:
            drop(event)
    try:
        _scrub_breadcrumbs(event)
    except Exception:
        event.pop("breadcrumbs", None)
    return event


def _drop_exception_values(event):
    for value in (event.get("exception") or {}).get("values") or ():
        if isinstance(value, dict):
            value["value"] = MESSAGE_REMOVED


def _drop_messages(event):
    event.pop("logentry", None)
    if "message" in event:
        event["message"] = MESSAGE_REMOVED


def scrub_sentry_breadcrumb(crumb, hint):
    """Sentry before_breadcrumb: a log line's breadcrumb without the text
    of the exceptions it quotes and without database values."""
    try:
        if isinstance(crumb.get("message"), str):
            crumb["message"] = scrub_text(crumb["message"],
                                          _exceptions_for(hint))
    except Exception:
        crumb["message"] = MESSAGE_REMOVED
    return crumb


# ── Texts stored where the owner reads them ──────────────────────────────
# A failed reply, transcription or merge stores a plain message, never
# str(exc) (Peter, 2026-10-20: no raw exception text anywhere). The
# exception itself is logged (describe_exception) and reaches Sentry, whose
# events are scrubbed above.
REPLY_FAILED_TEXT = (
    "Loore couldn't write this reply. The problem has been reported. "
    "Please try again.")
REPLY_NOT_STARTED_TEXT = "Loore couldn't start a reply."
TRANSCRIPTION_FAILED_TEXT = (
    "Loore couldn't transcribe this recording. The problem has been "
    "reported. Please try again.")
TODO_MERGE_FAILED_TEXT = (
    "Loore couldn't apply these changes. The problem has been reported. "
    "Please try again.")


PROMPT_TOO_LONG_TEXT = (
    "This thread is longer than the model can read. Start a new thread, "
    "or choose a model that reads more.")
TOOL_FAILED_TEXT = "This action didn't work. The problem has been reported."


def user_written_text(exc):
    """The text of *exc* when it is written for the user, else None. One
    list for every task that stores a failure where the owner reads it:
    classes with a message written for the user, anything carrying a
    ``user_text`` of its own, and a prompt over the context window (its
    own text, which says what to do instead of the numbers)."""
    own = getattr(exc, "user_text", None)
    if isinstance(own, str) and own:
        return own
    # llm_providers' classes are matched by name: tests stub or reload
    # the module, and a class from another copy must still count.
    names = {c.__name__ for c in type(exc).__mro__}
    if "PromptTooLongError" in names:
        return PROMPT_TOO_LONG_TEXT
    from backend.utils.ca_feed import ArchiveNotReady
    from backend.utils.llm_nodes import AIUsageRefused, ReadOnlyModelRefused
    from backend.utils.placeholders import UserExportValidationError
    written = isinstance(exc, (
        UserMessageError, ArchiveNotReady, AIUsageRefused,
        ReadOnlyModelRefused, UserExportValidationError))
    written = written or bool(
        names & {"ProviderUnavailableError", "ProviderAccountError"})
    if written and str(exc):
        return str(exc)
    return None


class UserMessageError(ValueError):
    """A ValueError whose message is written for the user, so a reply
    task may store it as the reply's error (reply_error_text)."""
