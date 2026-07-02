"""Human-readable permalinks for public nodes (#228).

A published share gets a slug derived from its content's first line:
lowercase, dashes, diacritics folded (Czech-friendly), capped at a word
boundary. Unique per author — the permalink shape is /u/<username>/<slug>.
"""
import re
import unicodedata

MAX_SLUG_CHARS = 60


def slugify(text):
    """First line of *text* → url-safe slug. Returns '' when nothing
    usable survives (caller falls back)."""
    if not text:
        return ""
    first_line = text.strip().split("\n")[0]
    # Strip markdown heading/list/emphasis markers.
    first_line = re.sub(r"[#>*_`\[\]()]+", " ", first_line)
    # Fold diacritics: příliš → prilis.
    folded = unicodedata.normalize("NFKD", first_line)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    words = re.findall(r"[a-zA-Z0-9]+", folded)
    slug = "-".join(w.lower() for w in words)
    if len(slug) > MAX_SLUG_CHARS:
        slug = slug[:MAX_SLUG_CHARS].rsplit("-", 1)[0]
    return slug


def generate_unique_public_slug(owner_id, content):
    """Slug for a new public node, deduped per owner (-2, -3, …)."""
    from backend.models import Node

    base = slugify(content) or "shared"
    candidate = base
    suffix = 2
    while Node.query.filter_by(
            human_owner_id=owner_id, public_slug=candidate).first():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate
