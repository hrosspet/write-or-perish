"""Personal API tokens (#232): bearer auth for first-party clients that
can't use the browser session, e.g. the Chrome clipper.

Design:
- Plaintext ``loore_<43 url-safe chars>``; only sha256 stored (same
  helper the magic-link flow uses).
- Scoped. A route declares the scope it needs; a token with another
  scope is refused. The clipper's scope, ``external:write``, can add
  saved references and nothing else — a leaked token cannot read lore.
- ``token_or_login_required`` accepts a valid bearer token OR a normal
  session, so the same route serves the extension and the web app.
"""
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import g, jsonify, request
from flask_login import current_user

from backend.extensions import db
from backend.utils.magic_link import hash_token

TOKEN_MARKER = "loore_"
SCOPE_EXTERNAL_WRITE = "external:write"
KNOWN_SCOPES = (SCOPE_EXTERNAL_WRITE,)
# last_used_at is a coarse "is this token still in use" signal for the
# token list; a write per request would be pure churn during a clipping
# session.
LAST_USED_WRITE_INTERVAL = timedelta(minutes=5)


def generate_api_token():
    """Return (plaintext, prefix). Prefix identifies the token in lists."""
    plaintext = TOKEN_MARKER + secrets.token_urlsafe(32)
    return plaintext, plaintext[len(TOKEN_MARKER):len(TOKEN_MARKER) + 8]


def _bearer_from_request():
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    return token or None


def resolve_api_token(plaintext, scope):
    """Return the active ApiToken row for ``plaintext`` with ``scope``,
    or None. Touches last_used_at at most every few minutes."""
    from backend.models import ApiToken
    if not plaintext or not plaintext.startswith(TOKEN_MARKER):
        return None
    row = ApiToken.query.filter_by(token_hash=hash_token(plaintext)).first()
    if row is None or row.revoked_at is not None or row.scope != scope:
        return None
    # The session path enforces approval in a before_request hook that
    # only sees flask-login users; the bearer path must enforce it here.
    user = row.user
    if user is None or not user.approved or user.deactivated_at is not None:
        return None
    now = datetime.utcnow()
    if (row.last_used_at is None
            or now - row.last_used_at > LAST_USED_WRITE_INTERVAL):
        row.last_used_at = now
        db.session.commit()
    return row


def token_or_login_required(scope):
    """Decorator: authenticate via bearer token (with ``scope``) or the
    session. Sets ``g.api_user`` to the resolved user either way and
    ``g.api_token`` to the token row (None for session auth).

    A present-but-invalid bearer is a 401 even if a session exists: a
    client that sends a token means to be authenticated by it, and a
    silent fallback would hide a revoked token behind a logged-in tab.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            bearer = _bearer_from_request()
            if bearer is not None:
                row = resolve_api_token(bearer, scope)
                if row is None:
                    return jsonify({"error": "invalid_token"}), 401
                g.api_user = row.user
                g.api_token = row
                return fn(*args, **kwargs)
            if current_user.is_authenticated:
                g.api_user = current_user
                g.api_token = None
                return fn(*args, **kwargs)
            return jsonify({"error": "Unauthorized"}), 401
        return wrapper
    return decorator
