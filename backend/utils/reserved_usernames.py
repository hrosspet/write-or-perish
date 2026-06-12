"""Reserved / protected username matching.

Centralizes the rules that prevent users from claiming usernames that
impersonate the brand, the founders, or system/route names. Used by the
dashboard username-update endpoint, Twitter OAuth login, the magic-link
unique-username generator, and the admin whitelist endpoint.

Matching strategy (after normalizing to lowercase alphanumerics):
  - RESERVED_EXACT: blocked only on an *exact* normalized match. This keeps
    short tokens like 'lore'/'lor' from collateral-blocking words such as
    'explore' or 'folklore'.
  - BRAND_SUBSTRING: the coined brand name 'loore' is blocked as a substring
    (so 'myloore', 'loore123' are blocked) -- it does not appear inside common
    English words, so 'explore' is safe.
  - FOUNDER_PREFIXES: blocked when the normalized name *starts with* one of
    these (e.g. 'hrosspetx', 'hrosspet_official').
"""

import re
from typing import Optional

# Exact-match reserved names (lowercased, normalized). 'lore'/'lor' are here
# (exact-only) so that 'explore'/'folklore' remain available.
RESERVED_EXACT = frozenset({
    "admin", "administrator", "system", "bot", "ai", "support", "help",
    "feedback", "info", "contact", "official", "team", "staff", "mod",
    "moderator", "profile", "settings", "login", "logout", "signup",
    "register", "node", "thread", "export", "archive", "feed", "log", "api",
    "webhook", "status", "health", "ping", "about", "terms", "privacy",
    "legal", "dashboard", "account", "welcome", "voice", "converse", "todo",
    "prompts", "import", "null", "undefined", "anonymous", "guest", "user",
    "test", "demo", "root", "superuser", "sysadmin", "god", "owner",
    "founder", "creator", "www", "mail", "smtp", "lore", "lor",
})

# Coined brand name -- substring match (does not collide with English words).
BRAND_SUBSTRING = ("loore",)

# Founder identifiers -- prefix match. Common first names ('peter', 'peta')
# are deliberately NOT listed: prefix-blocking them locks out real people.
FOUNDER_PREFIXES = ("hrosspet",)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")

# Mirrors the username format rule used elsewhere in the app.
_USERNAME_FORMAT_RE = re.compile(r"[a-zA-Z0-9_]+")
_MAX_USERNAME_LEN = 64


def _normalize(username: str) -> str:
    """Lowercase and strip all non-alphanumeric characters."""
    if not username:
        return ""
    return _NON_ALNUM_RE.sub("", username.lower())


def is_username_reserved(username: str) -> bool:
    """Return True if the (normalized) username is reserved/protected."""
    norm = _normalize(username)
    if not norm:
        return False
    if norm in RESERVED_EXACT:
        return True
    if any(brand in norm for brand in BRAND_SUBSTRING):
        return True
    if any(norm.startswith(prefix) for prefix in FOUNDER_PREFIXES):
        return True
    return False


def validate_username(username: str, exclude_user_id=None) -> Optional[str]:
    """Validate a desired username.

    Returns an error string describing the first problem found, or None if the
    username is acceptable. Checks (in order): non-empty, length, allowed
    characters, reserved, and case-insensitive uniqueness.

    ``exclude_user_id`` lets the caller exclude the user's own current row from
    the uniqueness check (so re-saving the same username is allowed).
    """
    if not username:
        return "Username cannot be empty."
    if len(username) > _MAX_USERNAME_LEN:
        return "Username must be 64 characters or fewer."
    if not _USERNAME_FORMAT_RE.fullmatch(username):
        return "Username may only contain letters, numbers, and underscores."
    if is_username_reserved(username):
        return "That username is reserved."

    # Deferred import to avoid a hard dependency on the app/db at import time
    # (keeps the pure helpers above unit-testable in isolation).
    from backend.models import User
    from backend.extensions import db

    query = User.query.filter(db.func.lower(User.username) == username.lower())
    if exclude_user_id is not None:
        query = query.filter(User.id != exclude_user_id)
    if query.first():
        return "That username is already taken."

    return None


def derive_available_username(base):
    """Derive a username from ``base`` that is neither reserved nor taken.

    Returns ``base`` itself when it passes both checks (case-insensitive
    uniqueness, matching validate_username); otherwise appends an
    incrementing numeric suffix starting at 2. Used by the magic-link signup
    (email prefix) and Twitter OAuth signup (screen_name) to pick a fallback
    instead of failing the auth callback.
    """
    from backend.models import User
    from backend.extensions import db

    if not base:
        base = "user"
    # Brand-substring / founder-prefix reserved matches can't be escaped by
    # appending digits (e.g. 'myloore2' still contains 'loore'), while
    # exact-match reservations can ('admin2'). Probe with a suffix appended to
    # tell them apart, and fall back to the neutral base instead of suffixing
    # forever.
    if is_username_reserved(base) and is_username_reserved(f"{base}2"):
        base = "user"

    def _available(candidate):
        if is_username_reserved(candidate):
            return False
        return not User.query.filter(
            db.func.lower(User.username) == candidate.lower()
        ).first()

    if _available(base):
        return base

    suffix = 2
    while True:
        candidate = f"{base}{suffix}"
        if _available(candidate):
            return candidate
        suffix += 1
