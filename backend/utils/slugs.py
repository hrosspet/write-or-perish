"""Human-readable permalinks for public nodes (#228).

A published share gets a slug derived from its content's first line:
lowercase, dashes, diacritics folded (Czech-friendly), capped at a word
boundary. Unique per author — the permalink shape is /u/<username>/<slug>.
"""
import re
import unicodedata

from backend.utils.privacy import PrivacyLevel

MAX_SLUG_CHARS = 60


def permalink_for(node):
    """The node's public address, or None while it is not public (#263).

    The slug is KEPT on the row when a node goes private, so republishing
    restores the same URL — but the permalink only resolves for public
    nodes (commons.resolve_permalink is public-only), so advertising it
    for a private node sends the owner to a 404 (NodeDetail rewrites the
    address bar; the Share page card navigates to it). Every serializer
    that emits a permalink goes through here: gate on current privacy,
    not on the slug.

    The username is the HUMAN owner's (human_owner_id, falling back to
    user_id), never the model account's: slug uniqueness and both
    resolvers (commons.resolve_permalink, public_pages._resolve_permalink)
    key on human_owner_id, so an LLM-authored public root addressed as
    /@<model>/<slug> would 404.
    """
    from backend.models import User

    if (node is None or not node.public_slug
            or node.privacy_level != PrivacyLevel.PUBLIC):
        return None
    if node.human_owner_id in (None, node.user_id):
        owner = node.user
    else:
        owner = User.query.get(node.human_owner_id)
    if owner is None:
        return None
    return f"/@{owner.username}/{node.public_slug}"


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
