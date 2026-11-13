"""Where a sign-in returns to (#704): an ordinary page of Loore, or nothing.

A sign-in that starts on a page comes back to it: the X sign-in keeps the
page in the session through the round trip to X, the email sign-in carries
it inside the signed token of the link it mails (so the link works in any
browser and on any device). Every route that sends a browser to such a
destination checks it here first; the web has a twin of this check
(frontend/src/utils/destination.js) that follows the same rules, written
the same way, and both are tested against one table of values
(frontend/src/utils/destinationCases.json). No Unicode database is
consulted, so the two give the same answer on every runtime.

A destination is kept when all of this holds, and dropped without a word
otherwise (the person lands on the default page; the value is never
logged):

- It is 1 to MAX_DESTINATION_LENGTH characters of printable ASCII
  (a browser's location gives every page that way: it percent-encodes the
  rest), with no backslash. It is read as path, then ?query, then
  #fragment.
- Its path, as written and percent-decoded once and twice (an escape that
  is not two hex digits or not UTF-8, or a path that would still change on
  a third round, drops it), starts with "/", is printable ASCII without a
  backslash, has no "//", and no segment containing "." (so no "." or ".."
  segment, which a browser would resolve, and no file such as feed.xml).
- None of those forms of the path, compared without case, is one of
  EXCLUDED_PATHS or below one: server routes and the pages that act when
  opened or belong to a flow already under way. Destinations are
  normalised (decoded, no dot segments) before this is checked.
- Its query names none of EXCLUDED_QUERY_NAMES (a checkout's return).
- Its query and fragment, as written and decoded once and twice, hold no
  character of UNSEEN_RANGES (controls, bidi and other format characters,
  line and paragraph separators).
"""
import re
from urllib.parse import unquote

# Longest destination kept (heuristic, #704): far above any Loore page
# address (a permalink with its query and fragment is under 200), well
# below what browsers and proxies accept in a URL.
MAX_DESTINATION_LENGTH = 2048

# Percent-decoding rounds checked: a value encoded twice is still caught
# (/%252F%252Fevil.example). A path that changes on a further round is
# dropped: no page of Loore is encoded three times.
_DECODE_ROUNDS = 2

# Never a place to come back to, with everything below each (the same list
# in frontend/src/utils/destination.js):
EXCLUDED_PATHS = (
    "/auth",             # sign-in, its X round trip, the emailed link's
                         # verification, sign-out, Connect X
    "/api",              # the JSON API, the export download included
    "/media",            # files
    "/static",           # the app's own files
    "/health",           # server checks
    "/ready",
    "/login",            # the sign-in page itself
    "/invite",           # an invite link: keeps its code when opened
    "/invite-mismatch",  # the choice after a sign-in an invite was not for
    "/account-restore",  # acts on a deleted account's restore offer
    "/account-deleted",  # shown once, after a deletion
)

# Query parameters of a return that completes a flow when opened: Stripe
# Checkout's return to the Account page (?subscription=…&session_id=…).
EXCLUDED_QUERY_NAMES = ("session_id", "subscription")

# Code points never kept in a destination, decoded or not: C0 and C1
# controls, and Unicode's format characters and line and paragraph
# separators as of Unicode 16 (categories Cf, Zl, Zp), U+2060-U+206F
# whole. Listed here rather than read from a Unicode database, whose
# version differs between the server and the browsers.
UNSEEN_RANGES = (
    (0x0000, 0x001F), (0x007F, 0x009F), (0x00AD, 0x00AD),
    (0x0600, 0x0605), (0x061C, 0x061C), (0x06DD, 0x06DD),
    (0x070F, 0x070F), (0x0890, 0x0891), (0x08E2, 0x08E2),
    (0x180E, 0x180E), (0x200B, 0x200F), (0x2028, 0x202E),
    (0x2060, 0x206F), (0xFEFF, 0xFEFF), (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD), (0x110CD, 0x110CD), (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)

_PRINTABLE_ASCII = re.compile(r"[\x21-\x7e]+")
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _split(value):
    """(path, query, fragment); query and fragment are None when absent."""
    before, sep, fragment = value.partition("#")
    path, qsep, query = before.partition("?")
    return path, (query if qsep else None), (fragment if sep else None)


def _decode(text):
    """One round of percent-decoding, or None for an escape a browser
    would refuse (not two hex digits, or bytes that are not UTF-8)."""
    if _BAD_ESCAPE.search(text):
        return None
    try:
        return unquote(text, errors="strict")
    except UnicodeDecodeError:
        return None


def _path_forms(path):
    """The path as written and decoded, or None: a malformed escape, or a
    path that would still change on a third round."""
    forms = [path]
    for _ in range(_DECODE_ROUNDS):
        decoded = _decode(forms[-1])
        if decoded is None:
            return None
        if decoded == forms[-1]:
            return forms
        forms.append(decoded)
    return forms if _decode(forms[-1]) == forms[-1] else None


def _text_forms(text):
    """A query or fragment as written and decoded: a malformed escape as
    written drops it; one that appears only after a round is literal
    text (a search for "100%")."""
    forms = [text]
    for _ in range(_DECODE_ROUNDS):
        decoded = _decode(forms[-1])
        if decoded is None:
            return None if len(forms) == 1 else forms
        if decoded == forms[-1]:
            break
        forms.append(decoded)
    return forms


def _is_page_path(form):
    if (not form.startswith("/") or not _PRINTABLE_ASCII.fullmatch(form)
            or "\\" in form or "//" in form):
        return False
    if any("." in segment for segment in form.split("/")):
        return False
    lowered = form.lower()
    return not any(lowered == p or lowered.startswith(p + "/")
                   for p in EXCLUDED_PATHS)


def _has_unseen(text):
    return any(lo <= ord(ch) <= hi for ch in text for lo, hi in UNSEEN_RANGES)


def _names_excluded_parameter(query):
    for pair in query.split("&"):
        name = _decode(pair.partition("=")[0])
        if name is not None and name.lower() in EXCLUDED_QUERY_NAMES:
            return True
    return False


def safe_destination(value):
    """*value* when it is an ordinary page of Loore to return to, else
    None."""
    if (not isinstance(value, str) or not value
            or len(value) > MAX_DESTINATION_LENGTH
            or not _PRINTABLE_ASCII.fullmatch(value) or "\\" in value):
        return None
    path, query, fragment = _split(value)
    forms = _path_forms(path)
    if forms is None or not all(_is_page_path(f) for f in forms):
        return None
    if query is not None and _names_excluded_parameter(query):
        return None
    for text in (query, fragment):
        if text is None:
            continue
        decoded = _text_forms(text)
        if decoded is None or any(_has_unseen(f) for f in decoded):
            return None
    return value
