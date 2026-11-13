"""Where a sign-in returns to (#704): a page inside Loore, or nothing.

A sign-in that starts on a page comes back to it: the X sign-in keeps the
page in the session through the round trip to X, the email sign-in carries
it inside the signed token of the link it mails (so the link works in any
browser and on any device). Every route that sends a browser to such a
destination checks it here first; the web has a twin of this check
(frontend/src/utils/destination.js), and both are tested against the same
table of hostile values.

Accepted: a path that starts with a single "/", optionally followed by a
query and a fragment, made of printable ASCII (a browser's location gives
every page that way: it percent-encodes the rest), at most
MAX_DESTINATION_LENGTH characters. Its path part, also once and twice
percent-decoded, must not start or continue with "//", hold a backslash or
a control or format character, or name a server route (/auth, /api). So
nothing a browser, a proxy or the router could read as another origin, and
no server action (signing out, Connect X) gets started by a sign-in.

Anything else is dropped without a word: the person lands on the default
page. The value is never logged.
"""
import re
import unicodedata
from urllib.parse import unquote

# Longest destination kept (heuristic, #704): far above any Loore page
# address (a permalink with its query and fragment is under 200), well
# below what browsers and proxies accept in a URL.
MAX_DESTINATION_LENGTH = 2048

# Percent-decoding rounds checked: a path encoded twice is still caught
# (/%252F%252Fevil.example). One that changes on a further round is
# refused: no page of Loore is encoded three times.
_DECODE_ROUNDS = 2

# Server routes, never a page to come back to (GET /auth/logout signs out,
# /auth/x/connect starts Connect X, /api answers JSON).
_NOT_PAGES = ("/auth", "/api")

_PRINTABLE_ASCII = re.compile(r"^[\x21-\x7e]+$")
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_UNSEEN = ("Cc", "Cf", "Zl", "Zp")


def _path_part(value):
    return re.split(r"[?#]", value, maxsplit=1)[0]


def _safe_path(path):
    if not path.startswith("/") or "//" in path or "\\" in path:
        return False
    if any(unicodedata.category(ch) in _UNSEEN for ch in path):
        return False
    lowered = path.lower()
    return not any(lowered == p or lowered.startswith(p + "/")
                   for p in _NOT_PAGES)


def _decode(path):
    """One round of percent-decoding, or None for an escape a browser
    would refuse (not two hex digits, or bytes that are not UTF-8)."""
    if _BAD_ESCAPE.search(path):
        return None
    try:
        return unquote(path, errors="strict")
    except UnicodeDecodeError:
        return None


def safe_destination(value):
    """*value* when it is a page inside Loore to return to, else None."""
    if (not isinstance(value, str) or not value
            or len(value) > MAX_DESTINATION_LENGTH
            or not _PRINTABLE_ASCII.match(value)
            or "\\" in value):
        return None
    path = _path_part(value)
    if not _safe_path(path):
        return None
    for _ in range(_DECODE_ROUNDS):
        decoded = _decode(path)
        if decoded is None or not _safe_path(decoded):
            return None
        if decoded == path:
            return value
        path = decoded
    return value if _decode(path) == path else None
