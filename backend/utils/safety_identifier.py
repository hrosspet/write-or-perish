"""OpenAI safety identifiers (Peter, 2026-11-04).

Every OpenAI Responses and Chat Completions request Loore sends, live or
in a batch, carries ``safety_identifier``: a stable id for the Loore user
the request is made for. With it, OpenAI can act on one user's requests
when its content-policy filter flags them, instead of on the whole
organisation.

The id never contains a username, an email or the database id itself: it
is an HMAC-SHA256 of the user id keyed with the app's SECRET_KEY (the
keyed-digest pattern of routes/import_data._keyed_digest), so nobody
without the key can tell which user it stands for, and it stays the same
for a user across requests. Rotating SECRET_KEY gives every user a new id.

A call made for no user (none exists today: every OpenAI call site passes
the user it works for) carries SYSTEM_SAFETY_IDENTIFIER.

Audio endpoints (transcription, TTS) and embeddings take no such field
and are left out.
"""
import hashlib
import hmac
import os

PREFIX = "loore-"
# Heuristic: 32 hex characters (128 bits of the HMAC) keep ids distinct
# for any number of users Loore will have, and "loore-" + 32 is 38
# characters, under OpenAI's 64-character limit for the field.
DIGEST_HEX = 32
SYSTEM_SAFETY_IDENTIFIER = "loore-system"
_PURPOSE = "openai-safety-identifier"


def _secret():
    """The app's SECRET_KEY: from the Flask config inside an app context,
    else the environment, else the config's default."""
    secret = None
    try:
        from flask import current_app, has_app_context
        if has_app_context():
            secret = current_app.config.get("SECRET_KEY")
    except Exception:  # pragma: no cover — Flask missing or not set up
        secret = None
    if not secret:
        secret = os.environ.get("SECRET_KEY")
    if not secret:
        from backend.config import Config
        secret = Config.SECRET_KEY
    return secret.encode("utf-8") if isinstance(secret, str) else secret


def openai_safety_identifier(user_id):
    """The safety_identifier for requests made for ``user_id`` (a Loore
    user id), or SYSTEM_SAFETY_IDENTIFIER when there is none."""
    if user_id is None:
        return SYSTEM_SAFETY_IDENTIFIER
    msg = f"{_PURPOSE}\x00{int(user_id)}".encode("utf-8")
    digest = hmac.new(_secret(), msg, hashlib.sha256).hexdigest()
    return PREFIX + digest[:DIGEST_HEX]
