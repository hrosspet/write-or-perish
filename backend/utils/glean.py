"""Glean (#435): who gleans, which threads offer the Glean button, and
which model a glean runs on.

Glean is the Community Archive read (utils/ca_feed.py) under its user-facing
name: Loore reads the latest day of the archive against what the user
said in a thread, their profile and their intentions, and shows the few
tweets worth their time. Internally it keeps the read's names (routes
under /api/read, the 'read' / 'read_thread' prompts, FeedPick kind
'read').

Who sees it (Peter, 2026-10-09):
  - the rollout gate (placeholders.ca_tweets_allowed: admins,
    GLEAN_FOR_ALL, GLEAN_USER_IDS), and on top of it
  - the user's own switch in Account settings (User.glean_enabled). An
    explicit choice wins; without one it is on for an account with
    Community Archive or X data — it arrived with it, or connected X
    later through Account — and off for everyone else.
  A user with Glean off sees no Glean card and no Glean button anywhere.

Where the button is: a thread started from the Glean card carries
started_from = "glean" on its root node; every turn of it offers the
Glean button (text: next to LLM Response; voice: under the record
button once a message is recorded). Any other thread offers "Glean for
this reflection" in the entry's menu instead.

A glean is always a live call, never the Batch API (READ_LIVE_MARKER on
its placeholder, set by create_llm_placeholder for every read turn
except the admin's /read/start experiments). It runs on a read model of
the provider the user's chat model is from, never another provider's
(GLEAN_MODEL_ANTHROPIC / GLEAN_MODEL_OPENAI).
"""
from flask import current_app

GLEAN_ENTRY = "glean"

# The node meta entry that makes a read turn a live call (llm_completion
# reads it; routes/read.py's batch rerun removes it).
READ_LIVE_MARKER = "_live"

# ExternalItem sources that are the user's own X / Community Archive data
# (saved tweets: an archive import, X bookmarks or likes). A Glean pick's
# row (read_pick) is not: Glean made it.
X_DATA_SOURCES = ("community_archive", "twitter_bookmark", "twitter_like")

# The config key of each provider's glean model.
GLEAN_MODEL_KEYS = {
    "anthropic": "GLEAN_MODEL_ANTHROPIC",
    "openai": "GLEAN_MODEL_OPENAI",
}
PROVIDER_NAMES = {"anthropic": "Anthropic", "openai": "OpenAI"}


def provider_name(provider):
    return PROVIDER_NAMES.get(provider, provider or "unknown")


class GleanModelUnavailable(ValueError):
    """No glean model is configured for the user's provider. Never
    answered with another provider's model (a user's provider is never
    switched)."""

    def __init__(self, provider):
        self.provider = provider
        super().__init__(
            f"Glean is not available for {provider_name(provider)} "
            "models yet.")


def glean_allowed(user):
    """The rollout gate (see placeholders.ca_tweets_allowed)."""
    from backend.utils.placeholders import ca_tweets_allowed
    return ca_tweets_allowed(user)


def has_x_or_archive_data(user):
    """Whether the account holds Community Archive or X data: it signed up
    with X or connected X in Account (twitter_id), connected X for the
    bookmark sync (ExternalAccount), was pre-filled from the Community
    Archive or the X API (prefilled_handle; the prefilled tweets are
    nodes with origin 'twitter'), uploaded its own X archive (origin
    'twitter'), or saved tweets as references. Cheapest checks first;
    the queries are EXISTS on indexed user columns."""
    if user is None:
        return False
    if getattr(user, "twitter_id", None) or getattr(user, "prefilled_handle", None):
        return True
    from backend.extensions import db
    from backend.models import ExternalAccount, ExternalItem, Node
    uid = user.id
    if db.session.query(
            ExternalAccount.query.filter_by(user_id=uid, provider="twitter")
            .exists()).scalar():
        return True
    if db.session.query(
            Node.query.filter(Node.user_id == uid, Node.origin == "twitter")
            .exists()).scalar():
        return True
    return bool(db.session.query(
        ExternalItem.query.filter(ExternalItem.user_id == uid,
                                  ExternalItem.source.in_(X_DATA_SOURCES))
        .exists()).scalar())


def glean_default_on(user):
    """The switch's value while the user has not chosen (Peter,
    2026-10-09): on with Community Archive or X data, off without. Read
    live, so connecting X later turns it on."""
    return has_x_or_archive_data(user)


def glean_enabled(user):
    """Whether *user* gleans: inside the gate, and their switch is on —
    their explicit choice, else the default."""
    if not glean_allowed(user):
        return False
    choice = getattr(user, "glean_enabled", None)
    if choice is not None:
        return bool(choice)
    return glean_default_on(user)


def glean_user_fields(user):
    """The current-user payload's Glean fields. available = inside the
    gate (Account shows the switch); enabled = the switch's effective
    value (every Glean card and button keys off it)."""
    allowed = glean_allowed(user)
    return {
        "glean_available": allowed,
        "glean_enabled": bool(allowed and glean_enabled(user)),
    }


def stamp_glean_entry(root, user, entry):
    """Mark *root*, a new thread's root, as started from the Glean card
    when the request said so (*entry* == "glean") and the user gleans.
    Returns whether it did. The caller commits."""
    if root is None or entry != GLEAN_ENTRY or not glean_enabled(user):
        return False
    root.started_from = GLEAN_ENTRY
    return True


def thread_root(node):
    """The root of *node*'s thread (walks up; one lookup per level)."""
    current = node
    seen = set()
    while current is not None and current.parent_id is not None \
            and current.id not in seen:
        seen.add(current.id)
        current = current.parent
    return current


def is_glean_root(root):
    return (root is not None and root.parent_id is None
            and root.started_from == GLEAN_ENTRY)


def is_glean_thread(node):
    """Whether *node*'s thread was started from the Glean card."""
    return is_glean_root(thread_root(node))


def is_gleaning(node):
    """An LLM node that is (or is becoming) a glean's reply: a read reply
    (ca_feed.is_read_reply: a render, picks or a batch), one marked live
    or glean-again, or one directly under a read prompt — so a pending or
    failed glean counts too."""
    import json
    from backend.utils.ca_feed import (
        READ_FURTHER_MARKER, READ_PROMPT_KEYS, is_read_reply,
    )
    if node is None or not (node.node_type == "llm" or node.llm_model):
        return False
    if is_read_reply(node):
        return True
    try:
        meta = json.loads(node.tool_calls_meta or "[]") or []
    except (TypeError, ValueError):
        meta = []
    if any(isinstance(m, dict) and m.get("name") in (
            READ_LIVE_MARKER, READ_FURTHER_MARKER) for m in meta):
        return True
    parent = node.parent
    return parent is not None and parent.get_prompt_key() in READ_PROMPT_KEYS


def model_provider(model_id):
    cfg = current_app.config.get("SUPPORTED_MODELS", {}).get(model_id) or {}
    return cfg.get("provider")


def glean_model_for_provider(provider):
    """The configured glean model of *provider*; READ_DEFAULT_MODEL when no
    glean model is configured and that one is of the same provider.
    Raises GleanModelUnavailable otherwise: never another provider's."""
    key = GLEAN_MODEL_KEYS.get(provider)
    model_id = current_app.config.get(key) if key else None
    if model_id:
        return model_id
    fallback = current_app.config.get("READ_DEFAULT_MODEL")
    if fallback and provider and model_provider(fallback) == provider:
        return fallback
    raise GleanModelUnavailable(provider)


def read_model_fits(user, model_id, provider):
    """Whether a read on *model_id* keeps the user on *provider*. An
    admin may name any read model (their own evaluations); defaults never
    cross providers for anyone."""
    if getattr(user, "is_admin", False):
        return True
    return model_provider(model_id) == provider
