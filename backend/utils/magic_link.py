import hashlib
from flask import current_app
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired


def _get_serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"])


def generate_magic_link_token(email, next_url=None, max_age=None):
    s = _get_serializer()
    payload = {"email": email}
    if next_url:
        payload["next_url"] = next_url
    if max_age:
        payload["max_age"] = max_age
    return s.dumps(payload, salt="magic-link")


def verify_magic_link_token(token):
    s = _get_serializer()
    default_max_age = current_app.config.get("MAGIC_LINK_EXPIRY_SECONDS", 900)
    try:
        # First load without max_age check to read the payload
        payload = s.loads(token, salt="magic-link", max_age=None)
        # Use token-specific max_age if embedded, otherwise default
        max_age = payload.get("max_age", default_max_age)
        # Re-validate with the correct max_age
        payload = s.loads(token, salt="magic-link", max_age=max_age)
        return payload
    except SignatureExpired:
        return None
    except BadSignature:
        return None


# Email-change tokens (#260) are signed under their own salt, so they are
# not sign-in tokens: /auth/magic-link/verify rejects one as a bad
# signature instead of signing in (or creating) an account for the address
# it carries. They are only accepted by POST /api/dashboard/email/confirm,
# inside a session of the account named in the token.
_EMAIL_CHANGE_SALT = "email-change"


def email_change_expiry_seconds():
    return current_app.config.get("EMAIL_CHANGE_EXPIRY_SECONDS", 86400)


def generate_email_change_token(user_id, email):
    return _get_serializer().dumps(
        {"user_id": user_id, "email": email}, salt=_EMAIL_CHANGE_SALT)


def verify_email_change_token(token):
    """The token's payload, or None when it is malformed, not an
    email-change token, or older than EMAIL_CHANGE_EXPIRY_SECONDS."""
    try:
        payload = _get_serializer().loads(
            token, salt=_EMAIL_CHANGE_SALT,
            max_age=email_change_expiry_seconds())
    except (SignatureExpired, BadSignature):
        return None
    if (not isinstance(payload, dict)
            or not isinstance(payload.get("user_id"), int)
            or not isinstance(payload.get("email"), str)):
        return None
    return payload


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_unique_username(email):
    from backend.utils.reserved_usernames import derive_available_username

    prefix = email.split("@")[0]
    # Clean up prefix: keep only alphanumeric and underscores
    clean = "".join(c for c in prefix if c.isalnum() or c == "_")
    return derive_available_username(clean)
