"""Rows from a ``dump_user.py`` file (#698): one place that turns a dumped
entry, profile version, recent context, todo or artifact into a new row
of this database. ``load_user.py`` (a whole file at once, by hand) and
the pre-fill bundle load (backend/utils/prefill_bundle_load.py, in
resumable chunks) both build their rows here, so a bundle loads exactly
as a dump does.

Content is written with ``set_content``: public rows stay plaintext and
everything else is encrypted with THIS environment's key (#257). A bulk
writer passes one data key (``encryption.new_dek``) for many entries, as
imports do, so a chunk costs one key-service call instead of one per
entry. Nothing here decrypts or calls a model.
"""
import base64
from datetime import datetime

from backend.extensions import db

# An entry's links to other entries of the dump, remapped once every
# entry has its new id.
LINK_COLUMNS = ("parent_id", "continuation_node_id", "linked_node_id")


def dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def new_profile(pf, user_id):
    """A UserProfile for a dumped version (``parent_profile_id`` and
    ``merged_profile_id`` point at dumped ids: the caller remaps them)."""
    from backend.models import UserProfile
    row = UserProfile(
        user_id=user_id, generated_by=pf.get("generated_by") or "import",
        tokens_used=pf.get("tokens_used") or 0,
        privacy_level=pf.get("privacy_level") or "private",
        ai_usage=pf.get("ai_usage") or "chat",
        source_tokens_used=pf.get("source_tokens_used") or 0,
        source_data_cutoff=dt(pf.get("source_data_cutoff")),
        source_origin_stats=pf.get("source_origin_stats"),
        generation_type=pf.get("generation_type") or "initial",
        # A large build's window set (#545): the set id, where a window
        # starts and when its writing was rendered. The profile pipeline
        # reads a set by them: a later update adds windows to the
        # account's current set and merges it again (#698). Older dumps
        # lack them: None.
        window_set=pf.get("window_set"),
        covers_start=dt(pf.get("covers_start")),
        source_rendered_at=dt(pf.get("source_rendered_at")),
    )
    row.set_content(pf.get("content") or "")
    if dt(pf.get("created_at")):
        row.created_at = dt(pf["created_at"])
    return row


def new_recent_context(rc, user_id, profile_id):
    from backend.models import UserRecentContext
    row = UserRecentContext(
        user_id=user_id, generated_by=rc.get("generated_by") or "import",
        tokens_used=rc.get("tokens_used") or 0,
        source_data_cutoff=dt(rc.get("source_data_cutoff")),
        source_tokens_covered=rc.get("source_tokens_covered") or 0,
        profile_id=profile_id,
        ai_usage=rc.get("ai_usage") or "chat",
    )
    row.set_content(rc.get("content") or "")
    if dt(rc.get("created_at")):
        row.created_at = dt(rc["created_at"])
    return row


def new_todo(td, user_id):
    from backend.models import UserTodo
    row = UserTodo(
        user_id=user_id, generated_by=td.get("generated_by") or "import",
        tokens_used=td.get("tokens_used") or 0,
        privacy_level=td.get("privacy_level") or "private",
        ai_usage=td.get("ai_usage") or "chat",
    )
    row.set_content(td.get("content") or "")
    if dt(td.get("created_at")):
        row.created_at = dt(td["created_at"])
    return row


def new_artifact(a, user_id):
    from backend.models import UserArtifact
    row = UserArtifact(
        user_id=user_id, kind=a["kind"], title=a.get("title") or a["kind"],
        description=a.get("description"),
        generated_by=a.get("generated_by") or "import",
        tokens_used=a.get("tokens_used") or 0,
        privacy_level=a.get("privacy_level") or "private",
        ai_usage=a.get("ai_usage") or "chat",
    )
    row.set_content(a.get("content") or "")
    if dt(a.get("created_at")):
        row.created_at = dt(a["created_at"])
    return row


def new_node(nd, user_id, author_id, dek=None):
    """A Node for a dumped entry, owned by ``user_id`` when the dump says
    the user owned it. Links (parent, continuation, linked) are set by
    the caller once every entry has its new id."""
    from backend.models import Node
    from backend.routes.import_data import PROVENANCES
    new = Node(
        user_id=author_id,
        human_owner_id=user_id if nd.get("owner_is_user", True) else None,
        node_type=nd.get("node_type", "user"),
        llm_model=nd.get("llm_model"),
        token_count=nd.get("token_count") or 0,
        distributed_tokens=nd.get("distributed_tokens") or 0,
        privacy_level=nd.get("privacy_level", "private"),
        ai_usage=nd.get("ai_usage", "none"),
        source_key=nd.get("source_key"),
        origin=nd.get("origin"),
        provenance=(nd.get("provenance")
                    if nd.get("provenance") in PROVENANCES else None),
        tool_calls_meta=nd.get("tool_calls_meta"),
        prompt_key=nd.get("prompt_key"),
    )
    new.set_content(nd.get("content") or "", dek=dek)
    if dt(nd.get("created_at")):
        new.created_at = dt(nd["created_at"])
    if dt(nd.get("updated_at")):
        new.updated_at = dt(nd["updated_at"])
    return new


def new_embedding(e, node_id, user_id):
    from backend.models import NodeEmbedding
    return NodeEmbedding(
        node_id=node_id, user_id=user_id, model=e["model"],
        content_hash=e.get("content_hash") or "",
        vector=base64.b64decode(e["vector"]),
        node_updated_at=dt(e.get("node_updated_at")),
    )


def prompt_row_id(ref, user_id, cache):
    """The id of the user's prompt row holding the dumped prompt ``ref``
    (by value): an existing row with the same key and text, else a new
    one. ``cache`` maps (key, text) to ids for the caller's run."""
    from backend.models import UserPrompt
    key = (ref["prompt_key"], ref.get("content") or "")
    if key in cache:
        return cache[key]
    row = None
    for cand in UserPrompt.query.filter_by(user_id=user_id, prompt_key=ref["prompt_key"]) \
                                .order_by(UserPrompt.created_at.desc()).all():
        if (cand.get_content() or "") == key[1]:
            row = cand
            break
    if row is None:
        row = UserPrompt(user_id=user_id, prompt_key=ref["prompt_key"],
                         title=ref.get("title") or ref["prompt_key"],
                         generated_by=ref.get("generated_by") or "import")
        row.set_content(key[1])
        if dt(ref.get("created_at")):
            row.created_at = dt(ref["created_at"])
        db.session.add(row)
        db.session.flush()
    cache[key] = row.id
    return row.id


def pin_prompt(node, ref, user_id, cache):
    """Pin the dumped prompt ``ref`` on a new system node, as the dump's
    source had it (so the node resolves to the prompt, not a copy)."""
    from backend.models import NodeContextArtifact
    from backend.utils.context_artifacts import sync_context_artifacts
    db.session.add(NodeContextArtifact(
        node_id=node.id, artifact_type="prompt",
        artifact_id=prompt_row_id(ref, user_id, cache)))
    if not node.prompt_key:
        node.prompt_key = ref.get("prompt_key")
    sync_context_artifacts(node.id, user_id, ref.get("content") or "")


def remap_profile_links(profiles, profile_map):
    """Point loaded versions' parent_profile_id and merged_profile_id at
    the loaded rows (``profile_map``: dumped id -> new id), for the
    dumped versions in ``profiles`` that were loaded. A link to a version
    the dump does not carry is left unset."""
    from backend.models import UserProfile
    for pf in profiles:
        if pf["id"] not in profile_map:
            continue
        values = {}
        for col in ("parent_profile_id", "merged_profile_id"):
            old = pf.get(col)
            if old in profile_map:
                values[col] = profile_map[old]
        if values:
            db.session.query(UserProfile).filter(
                UserProfile.id == profile_map[pf["id"]]).update(
                values, synchronize_session=False)
