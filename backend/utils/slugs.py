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



def slug_transcribed_thread(node, transcript):
    """The permalink of an uploaded recording that started a Text-mode
    thread, given once, when its transcript is stored (the transcription
    task calls this before it commits). Until then the entry has no slug:
    it is read at /node/<id> and is in no listing (utils/public_threads).

    Given only when *transcript* has text and the entry, as stored now,
    is public, alive, without a slug, and the first entry directly under
    a living, non-public Text-mode prompt root of the same person. The
    slug is made from the transcript's first line, as a typed entry's
    is, under the account's recordings lock, so two uploads that finish
    together do not take the same one. Returns the slug or None."""
    from sqlalchemy import func
    from backend.extensions import db
    from backend.models import Node
    from backend.utils.recording_limits import RecordingsBusy, lock_account

    if not (transcript or "").strip() or not node.parent_id:
        return None
    owner_id = node.human_owner_id or node.user_id
    try:
        lock_account(owner_id)
    except RecordingsBusy:
        # The entry stays without an address of its own.
        return None
    # As stored now: the owner may have changed the entry while the
    # recording was being transcribed.
    row = db.session.query(
        Node.privacy_level, Node.deleted_at, Node.public_slug,
        Node.parent_id).filter(Node.id == node.id).first()
    if (row is None or row.privacy_level != PrivacyLevel.PUBLIC.value
            or row.deleted_at is not None or row.public_slug
            or row.parent_id is None):
        return None
    root = db.session.query(
        Node.id, Node.parent_id, Node.prompt_key, Node.deleted_at,
        Node.privacy_level, Node.human_owner_id, Node.user_id,
    ).filter(Node.id == row.parent_id).first()
    if (root is None or root.parent_id is not None
            or root.prompt_key != "textmode" or root.deleted_at is not None
            or root.privacy_level == PrivacyLevel.PUBLIC.value
            or (root.human_owner_id or root.user_id) != owner_id):
        return None
    first_id = db.session.query(func.min(Node.id)).filter(
        Node.parent_id == root.id).scalar()
    if first_id != node.id:
        return None
    node.public_slug = generate_unique_public_slug(owner_id, transcript)
    return node.public_slug
