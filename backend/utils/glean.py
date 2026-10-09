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
except the admin's /read/start experiments). By default it runs on the
glean model of the provider the user's chat model is from
(GLEAN_MODEL_ANTHROPIC / GLEAN_MODEL_OPENAI): Loore never moves a user to
another provider on its own. The user may pick another read model in the
picker beside Glean, another provider's too (may_choose_glean_model).
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


def may_choose_glean_model(user):
    """Whether *user* may name the model a glean runs on: an admin, or
    anyone who gleans (Peter, 2026-10-09: while Glean is tested,
    everyone on it picks from the same models as admins, other
    providers' included). The choice is the user's; Loore's default
    never crosses providers (resolve_read_model). Only under the user's
    own node (llm_nodes._read_turn_model)."""
    if user is None:
        return False
    if getattr(user, "is_admin", False) is True:
        return True
    return glean_enabled(user)


def is_glean_prompt_node(node, user):
    """A read prompt node as a glean attaches it FOR *user*: the user's
    own node, keyed as a read prompt, still linked to the user's own read
    prompt version that Loore wrote (from the file default). A per-thread
    edit removes the link and makes the text the user's own; a saved user
    version is the user's text too; another user's prompt node is theirs.
    None of those is this user's Glean prompt. Fails closed: no node, no
    user, no link or an unknown version is not one."""
    from backend.utils.ca_feed import READ_PROMPT_KEYS
    user_id = getattr(user, "id", None)
    if node is None or user_id is None:
        return False
    if (node.human_owner_id or node.user_id) != user_id:
        return False
    if node.get_prompt_key() not in READ_PROMPT_KEYS:
        return False
    prompt = node.get_artifact("prompt")
    return (prompt is not None
            and prompt.user_id == user_id
            and prompt.prompt_key in READ_PROMPT_KEYS
            and prompt.generated_by == "default")


def read_turn_allowed(user, ca_node):
    """Whether the completion task may run a read for *user* whose
    {ca_tweets} sits on *ca_node*: an admin always (the PoC's
    experiments); anyone else only from their own read prompt as a glean
    attaches it (is_glean_prompt_node), and only while they glean.
    Checked on the EFFECTIVE placeholder, which can sit higher up the
    thread than the parent the reply's pre-flight sees (#435 review)."""
    if user is None:
        return False
    if getattr(user, "is_admin", False):
        return True
    return is_glean_prompt_node(ca_node, user) and glean_enabled(user)


def own_read_prompt_above(node, user, include_poc=False):
    """Whether *node* or an alive ancestor is a read prompt of *user*'s
    own: only then may a glean continue that thread's read ("glean
    again"); a read prompt someone else attached higher up a public
    thread is never reused. *include_poc* (admins) also counts the
    2026-09-13 PoC shape, {ca_tweets} typed into the user's own node.
    Walks the chain (one lookup per level; the PoC check decrypts)."""
    from backend.utils.ca_feed import READ_PROMPT_KEYS
    from backend.utils.placeholders import CA_TWEETS_PATTERN
    user_id = getattr(user, "id", None)
    seen = set()
    current = node
    while (user_id is not None and current is not None
           and current.id not in seen):
        seen.add(current.id)
        if (current.deleted_at is None
                and (current.human_owner_id or current.user_id) == user_id):
            if current.get_prompt_key() in READ_PROMPT_KEYS:
                return True
            if include_poc and CA_TWEETS_PATTERN.search(
                    current.get_content() or ""):
                return True
        current = current.parent
    return False


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
    """The configured glean model of *provider* (GLEAN_MODEL_ANTHROPIC /
    GLEAN_MODEL_OPENAI), when it is a read model of that provider. Raises
    GleanModelUnavailable otherwise: no other provider's model and no
    other default is ever used in its place."""
    key = GLEAN_MODEL_KEYS.get(provider)
    model_id = current_app.config.get(key) if key else None
    cfg = current_app.config.get("SUPPORTED_MODELS", {}).get(model_id) or {}
    if (not model_id or not cfg.get("read") or cfg.get("deprecated")
            or cfg.get("provider") != provider):
        raise GleanModelUnavailable(provider)
    return model_id
