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


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_unique_username(email):
    from backend.models import User

    prefix = email.split("@")[0]
    # Clean up prefix: keep only alphanumeric and underscores
    clean = "".join(c for c in prefix if c.isalnum() or c == "_")
    if not clean:
        clean = "user"

    candidate = clean
    if not User.query.filter_by(username=candidate).first():
        return candidate

    suffix = 2
    while True:
        candidate = f"{clean}{suffix}"
        if not User.query.filter_by(username=candidate).first():
            return candidate
        suffix += 1
