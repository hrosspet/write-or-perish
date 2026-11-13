from flask import (
    Blueprint, jsonify, Response, request, current_app, stream_with_context,
)
from flask_login import login_required, current_user
from backend.models import (
    Node, NodeContextArtifact, UserProfile, UserPrompt, UserTodo,
    UserArtifact,
)
from backend.extensions import db
from backend.utils.tokens import approximate_token_count
from backend.utils.privacy import (
    AI_ALLOWED, AIUsage, accessible_nodes_filter, can_user_access_node,
    withheld_prefill_clauses,
)
from backend.utils.quotes import (
    resolve_quotes, has_quotes, ExportQuoteResolver,
    resolve_quotes_for_export, resolve_ext_quotes, has_ext_quotes,
    ai_blocked_artifact, AI_BLOCKED_ARTIFACT_TEXT,
    quotes_inside, hold_quotes, release_quotes, reference_title_lines,
)
from backend.utils.timefmt import iso_utc
from backend.utils.encryption import dek_cached, prefetch_deks
from backend.utils.task_owner import failure_text, task_owned_by
from backend.utils.thread_tree import ancestor_chains
from sqlalchemy import and_, case, exists, func, or_
from sqlalchemy.orm import selectinload, subqueryload
from bisect import bisect_left, bisect_right
from collections import namedtuple
from datetime import datetime
from operator import attrgetter, itemgetter
import re
import secrets
import time

export_bp = Blueprint("export_bp", __name__)


def _node_author_label(node):
    """Return a compact author label like 'User (alice)' or 'AI (claude-opus-4.6)'.

    No name when the author deleted the account (in its grace period or
    deleted, #269): their placeholders name nobody, as in threads."""
    from backend.utils.privacy import author_gone
    if node.node_type == "llm":
        return f"AI ({node.llm_model})" if node.llm_model else "AI (unknown)"
    if author_gone(node):
        return "User"
    author = node.user.username if node.user else "Unknown"
    return f"User ({author})"


def _origin_suffix(node):
    """' via twitter' for imported nodes; '' for Loore-native ones.

    Loore is the default and main source, so it is never rendered — only
    the exceptions are marked, keeping the token cost to the imports.
    """
    return f" via {node.origin}" if node.origin else ""


def _origin_stats(rows):
    """Per-origin node and token counts for the export metadata.

    rows: objects with .origin and .token_count (CTE rows or Nodes).
    Returns {origin: {"nodes": n, "tokens": t}}, NULL origin keyed
    "loore". Profile generation renders this as a source-mix preamble
    so the model knows e.g. that 95% of the corpus is public tweets.
    """
    stats = {}
    for r in rows:
        key = r.origin or "loore"
        entry = stats.setdefault(key, {"nodes": 0, "tokens": 0})
        entry["nodes"] += 1
        entry["tokens"] += r.token_count or 0
    return stats


def _export_visible_filter(model, user_id):
    """SQL filter for nodes visible to *user_id* in their export.

    Same as ``accessible_nodes_filter`` for alive nodes (excludes
    soft-deleted at the SQL level), plus soft-deleted nodes the user
    *owned* — those render as tombstone placeholders so mixed threads
    keep their structure (§5a). Soft-deleted nodes by other users are
    NOT included even if public — including them would leak the
    deleter's username/timestamp into someone else's export.
    """
    return or_(
        accessible_nodes_filter(model, user_id),
        and_(
            model.deleted_at.isnot(None),
            or_(
                model.user_id == user_id,
                model.human_owner_id == user_id,
            ),
        ),
    )


# What a deleted node renders in place of its text (§5a).
TOMBSTONE_TEXT = "[Node deleted by author]"
# What a deleted node costs the budgeted raw window (#525) when it holds
# the place of an entry below it: its placeholder line, by the same
# chars/4 measure as an entry's stored token_count (6 units). A deleted
# node with no entry below it is not shown and costs nothing. Its former
# size counted until the purge, and could empty the window.
TOMBSTONE_UNITS = approximate_token_count(TOMBSTONE_TEXT)
# The most deleted rows the raw window's pre-selection passes (#525).
# They cost its budget nothing, but each one is a row loaded; while a
# "Delete all my content" waits out its 30 days, every row of the
# account is deleted. INTRODUCED CONSTANT.
MAX_WINDOW_DELETED_ROWS = 2000


def _alive_units(model):
    """SQL: what *model*'s row costs the raw window's pre-selection: its
    stored token_count, or 0 once it is deleted (#525)."""
    return case(
        (model.deleted_at.is_(None), func.coalesce(model.token_count, 0)),
        else_=0,
    )


def _header_stamp(node):
    """A node's time as its header shows it."""
    return node.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")


def _node_header_line(node, label):
    """Markdown header line shared by all node renderings."""
    return (f"{_index_heading(label)} {_node_author_label(node)}"
            f"{_origin_suffix(node)} - {_header_stamp(node)}\n")


# What a reply the user cannot access renders as, under a heading that
# holds only its place in the thread: no author, no time.
INACCESSIBLE_TEXT = "[Reply not accessible]"


# Position labels (#592). An entry's label is its place in its thread:
# the thread's number, then one number per entry along a linear stretch
# (thread 4: 4, 4.1, 4.2 ... 4.199), and a letter per branch where an
# entry has more than one reply shown (the replies to 2.2 are 2.2a.1 and
# 2.2b.1, the next entry on branch b is 2.2b.2). A label grows with the
# number of branch points above the entry, not with its depth: the old
# path added ".1" per level, so the 200th message of an imported chat
# carried a 400-character label.

def _branch_letters(i):
    """The letters of the branch with index *i* (from 0) at a branch
    point: a ... z, then aa, ab, ... (bijective base 26)."""
    letters = ""
    i += 1
    while i:
        i, rest = divmod(i - 1, 26)
        letters = chr(ord("a") + rest) + letters
    return letters


def _reply_labels(label, count):
    """The labels of the *count* replies shown under the entry labelled
    *label*. One reply continues the linear stretch with the next number
    (4 -> 4.1, 2.2a.1 -> 2.2a.2); two or more each start a branch,
    lettered in order (2.2 -> 2.2a.1, 2.2b.1)."""
    if count == 1:
        head, dot, tail = label.rpartition(".")
        if dot and tail.isdigit():
            return [f"{head}.{int(tail) + 1}"]
        return [f"{label}.1"]
    return [f"{label}{_branch_letters(i)}.1" for i in range(count)]


def _index_heading(label):
    """The Markdown heading of the entry labelled *label*, e.g.
    '## [4.12]': level 2 along the thread's first stretch, one level
    deeper per branch point above the entry (a segment with a letter),
    at most 6. A long linear thread stays at level 2."""
    branches = sum(1 for seg in label.split(".") if not seg.isdigit())
    return f"{'#' * min(branches + 2, 6)} [{label}]"


def _branch_separator(label):
    """The line before every branch after the first, naming the entry it
    replies to, so a reader need not work it out from the labels."""
    return f"---\n**BRANCH: another reply to {label}**\n---\n\n"


class _QuoteReferences:
    """The quote references of one export (#592). A quote the export
    resolves by reference (the quoted entry is in it elsewhere) names
    that entry by its position label, "[See entry 2.2b.1]". An entry can
    quote one rendered further down, so the render leaves a marker and
    ``fill`` replaces every marker once the document is complete. A
    quoted entry the document does not show as an entry (only inside
    another entry's quote, in a compact tweet run, or not at all) is
    named by its author and time, when the user may see them.

    A marker carries a tag drawn per export, so no stored text can forge
    one, and none survives ``fill``: the finished text is the same for
    the same tree and window.
    """

    def __init__(self):
        self.labels = {}    # node id -> the label of its rendered entry
        self._tag = secrets.token_hex(8)
        self._used = False

    def marker(self, node_id):
        self._used = True
        return f"\ue000{self._tag}:{node_id}\ue001"

    def fill(self, text, user_id, filter_ai_usage):
        """*text* with every marker replaced by its reference."""
        if not self._used:
            return text
        pattern = re.compile(
            "\ue000" + re.escape(self._tag) + r":(\d+)" + "\ue001")
        ids = {int(i) for i in pattern.findall(text)}
        refs = {i: f"[See entry {self.labels[i]}]"
                for i in ids if i in self.labels}
        unshown = ids - refs.keys()
        if unshown:
            for node in Node.query.filter(Node.id.in_(unshown)):
                refs[node.id] = _unshown_quote_reference(
                    node, user_id, filter_ai_usage)
        return pattern.sub(
            lambda m: refs.get(int(m.group(1)))
            or f"[Quoted node inaccessible: node {m.group(1)}]",
            text)


def _unshown_quote_reference(node, user_id, filter_ai_usage):
    """The reference to a quoted entry the export does not show as an
    entry (#592): its author and time, as its header would show them.
    A deleted or inaccessible node, or one kept from models, gets the
    text the export's inline quotes give it (resolve_quotes), naming no
    author."""
    from backend.utils.privacy import can_user_view_tombstone, shown_as_deleted
    if shown_as_deleted(node, user_id):
        if can_user_view_tombstone(node, user_id):
            return f"[Quoted node deleted: node {node.id}]"
        return f"[Quoted node inaccessible: node {node.id}]"
    if not can_user_access_node(node, user_id):
        return f"[Quoted node inaccessible: node {node.id}]"
    if filter_ai_usage and node.ai_usage not in AI_ALLOWED:
        return f"[Quote #{node.id}: AI usage not permitted by author]"
    return (f"[Quoted entry by {_node_author_label(node)}"
            f"{_origin_suffix(node)} - {_header_stamp(node)}]")


def prefetch_children(nodes, batch=500):
    """Load the whole subtree under ``nodes`` level by level in batched
    ``parent_id IN (...)`` queries and hand each node its ``children``
    list, so rendering never lazy-loads per node. Semantics are identical
    to the relationship (all children, unordered — callers sort); the
    cost drops from one full-row query per rendered node (an N+1 that
    seq-scanned the node table 1,700 times per profile chunk before
    parent_id was indexed) to ~one query per tree level."""
    from sqlalchemy.orm.attributes import set_committed_value
    level = list(nodes)
    seen = {n.id for n in level}
    while level:
        by_parent = {n.id: [] for n in level}
        ids = list(by_parent)
        for start in range(0, len(ids), batch):
            for child in Node.query.filter(
                    Node.parent_id.in_(ids[start:start + batch])).all():
                by_parent[child.parent_id].append(child)
        next_level = []
        for n in level:
            kids = by_parent[n.id]
            set_committed_value(n, "children", kids)
            next_level.extend(k for k in kids if k.id not in seen)
            seen.update(k.id for k in kids)
        level = next_level


# Nodes per DEK-prefetch window. Must stay well under the DEK cache size
# (encryption._DEK_CACHE_MAX, 4096): every DEK unwrapped for a window has
# to survive in the LRU until that window is rendered, and rendering also
# decrypts quotes and artifacts on the side. INTRODUCED CONSTANT.
DEK_PREFETCH_WINDOW = 1000


def iter_with_dek_prefetch(roots, window=DEK_PREFETCH_WINDOW):
    """Yield *roots* in order, unwrapping the DEKs of each upcoming window
    of subtrees (roots plus their already-loaded ``children``) in one
    concurrent batch first.

    Rendering decrypts one node at a time, and a cold KMS unwrap is
    ~80 ms from the prod VM (2026-09-02), so a 5k-node profile chunk
    spent ~7 minutes waiting on KMS in sequence. Windows keep the
    prefetched DEKs inside the LRU cache; a root's subtree is never
    split across windows. Call after prefetch_children(roots) — walking
    ``children`` here must not lazy-load. A deleted node's DEK is not
    unwrapped: its text is never rendered (#525).
    """
    batch, texts = [], []
    for root in roots:
        batch.append(root)
        stack = [root]
        while stack:
            n = stack.pop()
            if n.deleted_at is None:
                texts.append(n.content)
            stack.extend(n.children)
        if len(texts) >= window:
            prefetch_deks(texts)
            yield from batch
            batch, texts = [], []
    if batch:
        prefetch_deks(texts)
        yield from batch


def _filtered_children(node, filter_ai_usage, created_before, included_ids,
                       keep_tombstones, user_id=None):
    """Children of *node* that should render, sorted chronologically.

    keep_tombstones: when a budget pre-selection (included_ids) is
    active, tombstones pass through even if not pre-selected — they cost
    a window no more than their placeholder line (TOMBSTONE_UNITS) and
    dropping them creates discontinuities (§5a). A node hidden with its
    owner's deleted account (#269) counts as a tombstone for *user_id*,
    as it will after the purge. The inaccessible-placeholder branch does
    not extend this courtesy, matching the historical behavior.
    """
    from backend.utils.privacy import shown_as_deleted
    children = node.children
    if filter_ai_usage:
        children = [c for c in children if c.ai_usage in AI_ALLOWED]
    if created_before:
        children = [c for c in children if c.created_at < created_before]
    if included_ids is not None:
        if keep_tombstones:
            children = [
                c for c in children
                if c.id in included_ids or shown_as_deleted(c, user_id)
            ]
        else:
            children = [c for c in children if c.id in included_ids]
    # The id settles a tie, so replies with one timestamp keep their
    # order, and their labels (#592), from one render to the next.
    return sorted(children, key=lambda c: (c.created_at, c.id))


def _note_entry(licence, node):
    """Report an entry whose text the export renders to *licence* (a
    PayloadLicence, #326): an export going to a model carries every
    rendered entry, the user's own and other people's replies alike, and
    each is licensed for training by its own ai_usage. None = no report
    (the user's own data download). A licensed entry is only recorded, in
    case the licence tracks rows for a cached render."""
    if licence is None:
        return
    if node.ai_usage == AIUsage.TRAIN.value:
        licence.track_row(node.__tablename__, (node.id,))
    else:
        licence.note_usage(node.ai_usage, f"archive entry {node.id}",
                           row=node)


def _note_resolver_preamble(licence, resolver):
    """Report the rows a budgeted export's preamble renders in full: the
    system prompts (by the node their text came from) and the pinned
    profiles and todo lists (#326)."""
    if licence is None:
        return
    licence.note_nodes(
        [resolver.prompt_node_ids[pid]
         for pid in resolver.referenced_prompt_ids
         if pid in resolver.prompt_node_ids],
        what="archive system prompt")
    for kind, model in (("profile", UserProfile), ("todo", UserTodo)):
        ids = resolver.referenced_artifacts.get(kind)
        if not ids:
            continue
        for row_id, ai_usage in model.query.with_entities(
                model.id, model.ai_usage).filter(model.id.in_(ids)):
            licence.track_row(model.__tablename__, (row_id,))
            licence.note_usage(ai_usage, f"archive {kind} {row_id}")


class _CompactTweetRun:
    """Buffers consecutive flat imported-tweet entries and renders each run
    as one compact section (#276): a `# Tweets by <user> ...` header, then
    one `[YYYY-MM-DD HH:MM] text` entry per tweet. Shared by the
    incremental and legacy render loops. Tweets never carry pinned
    artifacts or {quote:ID} placeholders, so the compact line skips those
    lookups. Each rendered tweet reports to *licence* (#326)."""

    def __init__(self, export_lines, username, licence=None):
        self.lines = export_lines
        self.username = username
        self.licence = licence
        self.run = []

    def add(self, node):
        self.run.append(node)

    def flush(self):
        if not self.run:
            return
        self.lines.append(_tweet_run_heading(self.username, len(self.run)))
        self.lines.append("")
        for tweet in self.run:
            _note_entry(self.licence, tweet)
            self.lines.append(_tweet_line(tweet))
            self.lines.append("")
        self.lines.append("---")
        self.lines.append("")
        self.run.clear()


def _tweet_run_heading(username, count):
    """The heading line of a run of *count* flat imported tweets (#276)."""
    return (f"# Tweets by {username} (imported from Twitter/X) — "
            f"{count} tweets")


def _tweet_line(tweet):
    """One flat imported tweet in its run's section (#276)."""
    ts = tweet.created_at.strftime("%Y-%m-%d %H:%M")
    return f"[{ts}] {tweet.get_content()}"


def _is_flat_tweet_root(node, filter_ai_usage, created_before):
    """Legacy-path compact eligibility (#276): an alive root tweet with no
    renderable children is a one-node "thread" whose scaffolding dominates
    its text. Children are checked against the render filters but NOT the
    budget selection, mirroring the incremental path (a tweet someone
    engaged with is threaded content even if the budget excluded the
    reply)."""
    return (node.origin == "twitter" and node.parent_id is None
            and node.deleted_at is None
            and not _filtered_children(node, filter_ai_usage, created_before,
                                       None, keep_tombstones=True))


def _preamble_content(row, filter_ai_usage):
    """A pinned artifact version's preamble body. With *filter_ai_usage*,
    a row marked outside AI_ALLOWED renders as a placeholder, never its
    content (#340)."""
    if filter_ai_usage and ai_blocked_artifact(row):
        return AI_BLOCKED_ARTIFACT_TEXT
    return row.get_content()


def _artifact_ref_lines(node, user_id):
    """Reference lines for whatever context artifacts are pinned on *node*.

    Returns [] when nothing is pinned. Emitted for EVERY node (not just
    system-prompt nodes) so any node carrying pins — including interim
    retrieval nodes that pinned the artifact they read — surfaces them.
    Only *user_id*'s own versions are listed: someone else's node in the
    user's threads pins theirs (the export's preamble leaves them out
    too). The system prompt is listed either way: its text is the node's
    own content.
    """
    lines = []
    prompt = node.get_artifact("prompt")
    if prompt is not None:
        version_num = UserPrompt.query.filter(
            UserPrompt.user_id == prompt.user_id,
            UserPrompt.prompt_key == prompt.prompt_key,
            UserPrompt.created_at <= prompt.created_at,
        ).count()
        lines.append(
            f"[System Prompt: {prompt.title} v{version_num} "
            f"(ref #{prompt.id})]")

    profile = node.get_artifact("profile")
    if profile is not None and profile.user_id == user_id:
        from backend.utils.profile_versions import not_set_internal
        profile_ver = UserProfile.query.filter(
            UserProfile.user_id == profile.user_id,
            UserProfile.created_at <= profile.created_at,
            not_set_internal(),
        ).count()
        lines.append(f"[User Profile v{profile_ver} (ref #{profile.id})]")

    todo = node.get_artifact("todo")
    if todo is not None and todo.user_id == user_id:
        todo_ver = UserTodo.query.filter(
            UserTodo.user_id == todo.user_id,
            UserTodo.created_at <= todo.created_at,
        ).count()
        lines.append(f"[User TODO v{todo_ver} (ref #{todo.id})]")

    # ai_preferences is a UserArtifact (#158 Slice 5) → it flows through the
    # user_artifact loop below as [Artifact 'ai_preferences' vN].
    # get_user_artifacts() and live() leave deleted versions out (#603).
    for kind, artifact in sorted(node.get_user_artifacts().items()):
        if artifact.user_id != user_id:
            continue
        artifact_ver = UserArtifact.live().filter(
            UserArtifact.user_id == artifact.user_id,
            UserArtifact.kind == kind,
            UserArtifact.created_at <= artifact.created_at,
        ).count()
        lines.append(
            f"[Artifact '{kind}' v{artifact_ver} (ref #{artifact.id})]")

    return lines


def _is_system_prompt_node(node):
    """An agentic session's system node: it links a prompt version, or
    carries the prompt_key stamp a per-thread prompt edit leaves behind
    (its content is then the edited prompt). Either way its text is a
    system prompt, not the user's writing. Unlike Node.is_system_prompt,
    a link whose prompt row is gone counts too (its content renders
    empty)."""
    return bool(node.prompt_key) or node.has_artifact("prompt")


def _add_artifact_free_entry(resolver, node):
    """Add *node* to the budget resolver of an export that omits artifacts
    (#550). Every node counts its stored units, as the window selection
    counted it. A system-prompt node renders nothing, so its prompt adds
    no quote to embed; it is not a prompt entry either, whose reference
    line (and, with a preamble, prompt text) the window never counted.
    Counting those made the resolver drop the window's newest entries
    from the rendering while the resume cursor moved past them."""
    is_system = _is_system_prompt_node(node)
    resolver.add_node(
        node_id=node.id,
        created_at=node.created_at,
        content="" if is_system else node.get_content(),
        token_count=node.token_count,
    )


def _format_node_text(node, label, user_id, embedded_quotes,
                      ai_blocked_ids, licence=None, filter_ai_usage=False,
                      artifact_refs=True, quote_reference=None,
                      hold_others_markers=False, reference_titles=None):
    """The node's own text block: header, any pinned-artifact refs, then
    content. System-prompt nodes emit refs only (their content IS the
    prompt, shown as a ref). The node, the entries its quotes embed and
    the saved references it quotes report to *licence* (#326).

    With ``artifact_refs=False`` (an export that omits artifacts, #550)
    no reference line is emitted, and a system-prompt node renders
    nothing at all ("").

    *quote_reference* (node id -> text) writes a quote resolved by
    reference (_QuoteReferences.marker, #592).

    *hold_others_markers*: the markers left as written in text the user
    did not write stay held (quotes.hold_quotes) for a caller that runs
    its own quote passes over the export (an AI reply's prompt) and
    releases them after; otherwise they are released here.

    *reference_titles* (a quotes.ReferenceLines): a quoted saved
    reference is one line naming it (title or author, source; with
    ``named`` the name read_full opens it by, and its id goes into
    ``ids``) instead of its text (quotes.reference_title_lines), for the
    recent raw window of a call with tools. None keeps the text."""
    if not artifact_refs and _is_system_prompt_node(node):
        return ""
    result = _node_header_line(node, label)
    _note_entry(licence, node)

    ref_lines = _artifact_ref_lines(node, user_id) if artifact_refs else []
    is_prompt_node = node.get_artifact("prompt") is not None

    if is_prompt_node:
        # System prompt nodes: references instead of full content.
        for line in ref_lines:
            result += line + "\n"
        result += "\n"
        return result

    # Other nodes: emit any pinned-artifact refs (e.g. interim retrieval
    # nodes that read an artifact), then the quote-resolved content.
    for line in ref_lines:
        result += line + "\n"

    # Resolve {quote:ID} placeholders in content. In text the user did
    # not write (someone else's reply in their thread, or quoted text of
    # someone else's at any depth), a quote resolves only when every
    # reader of that text can read the quoted node, and a saved
    # reference stays as written (quotes_inside).
    content = node.get_content()
    inside = only = None
    if user_id and (has_quotes(content) or has_ext_quotes(content)):
        inside = quotes_inside(user_id)
        only = inside(node.id)
    if has_quotes(content):
        if embedded_quotes is not None:
            # Use smart resolution from ExportQuoteResolver
            if licence is not None:
                licence.note_nodes(list(embedded_quotes.get(node.id) or ()),
                                   what="archive quoted entry")
            content = resolve_quotes_for_export(
                content, node.id, embedded_quotes, user_id,
                ai_blocked_ids=ai_blocked_ids,
                reference=quote_reference,
                only=only, inside=inside,
            )
        elif user_id:
            # Fallback to simple resolution (depth 1). The unbudgeted
            # AI-filtered export reaches models too (recent-context first
            # attempt, uncapped {user_export}), so it blocks quotes of
            # nodes marked 'none' like the resolver path does.
            content, quoted_ids = resolve_quotes(
                content, user_id, for_llm=False, max_depth=1,
                block_ai_none=filter_ai_usage, only=only, inside=inside)
            if licence is not None:
                licence.note_nodes(quoted_ids, what="archive quoted entry")
    # {quote_ext:ID} (saved references) resolve inline unconditionally —
    # they're small and never nest, so no dedup machinery is needed.
    # Only in the user's own text: they resolve against the user.
    if user_id and only is None and has_ext_quotes(content):
        if reference_titles:
            # A line without a title carries no words of the reference,
            # only its author and source, so only a title reports
            # (Training licence; as a quote by author and time, #626).
            content, named_ids, titled = reference_title_lines(
                content, user_id, named=reference_titles.named)
            if reference_titles.named:
                reference_titles.ids.update(named_ids)
            if licence is not None:
                licence.note_external(titled,
                                      what="archive reference title")
        else:
            content, ext_ids = resolve_ext_quotes(
                content, user_id, for_llm=False)
            if licence is not None:
                licence.note_external(ext_ids, what="archive reference")
    if not hold_others_markers:
        content = release_quotes(content)
    elif only is not None:
        # Someone else's text: every marker left in it stays as written.
        content = hold_quotes(content)

    result += content
    result += "\n\n"
    return result


def format_node_tree(
    node,
    index_path="1",
    processed_nodes=None,
    filter_ai_usage=False,
    user_id=None,
    created_before=None,
    embedded_quotes=None,
    included_ids=None,
    ai_blocked_ids=None,
    licence=None,
    artifact_refs=True,
    prune_tombstones=False,
    references=None,
    hold_others_markers=False,
    reference_titles=None,
):
    """
    Format a node and its descendants into a human-readable tree structure
    using Markdown headers. Depth-first traversal for maximum readability.
    The text is the pieces ``iter_node_tree`` yields, joined; see there.
    """
    return "".join(iter_node_tree(
        node, index_path=index_path, processed_nodes=processed_nodes,
        filter_ai_usage=filter_ai_usage, user_id=user_id,
        created_before=created_before, embedded_quotes=embedded_quotes,
        included_ids=included_ids, ai_blocked_ids=ai_blocked_ids,
        licence=licence, artifact_refs=artifact_refs,
        prune_tombstones=prune_tombstones, references=references,
        hold_others_markers=hold_others_markers,
        reference_titles=reference_titles))


def iter_node_tree(
    node,
    index_path="1",
    processed_nodes=None,
    filter_ai_usage=False,
    user_id=None,
    created_before=None,
    embedded_quotes=None,
    included_ids=None,
    ai_blocked_ids=None,
    licence=None,
    artifact_refs=True,
    prune_tombstones=False,
    references=None,
    before_entry=None,
    hold_others_markers=False,
    reference_titles=None,
):
    """
    Yield the text of a node and its descendants piece by piece, in
    order: each entry's block, each placeholder, each branch separator.
    ``format_node_tree`` joins them; the streamed data download (#591)
    sends them as they come, so a very large thread starts going out
    before it is all rendered.

    before_entry: Optional callable, called with each node whose text is
        about to be rendered (the download unwraps the data keys of the
        entries ahead there, #591). Changes nothing in the text.

    Iterative (explicit work stack) rather than recursive: the renderer
    used one Python frame per reply-depth (plus the frames SQLAlchemy
    burns lazy-loading children each level), so deep reply chains
    overflowed the call stack — the same failure
    `_collect_all_nodes_in_tree` had before c556e4d, one stage later in
    the pipeline. Stack frames are (kind, node, label), where kind
    mirrors the old mutual recursion: "node" (cycle-checked; applies the
    access rule: renders the node, routes it to the tombstone or
    inaccessible shell, or leaves it out), "tombstone", and
    "inaccessible". Branch separators are pushed as ("text", str)
    frames, in pre-order with the entries.

    Every entry and placeholder carries a position label (#592, see
    _reply_labels): a reply the access rule leaves out takes no label
    and no separator, and a pruned run of deleted nodes is one
    placeholder with one label.

    Args:
        node: The Node object to format
        index_path: The label of *node*, the thread's number (e.g., "3")
        processed_nodes: Set of node IDs already processed (to avoid infinite loops)
        filter_ai_usage: If True, only include child nodes where ai_usage is 'chat' or 'train'
        user_id: User ID for resolving {quote:ID} placeholders (for access checks)
        created_before: Optional datetime. If provided, only include child nodes created before
                       this timestamp.
        embedded_quotes: Optional dict from ExportQuoteResolver mapping
                        node_id -> {quoted_id -> content}. When provided, uses smart
                        quote resolution that embeds only when needed.
        included_ids: Optional set of node IDs included in the export. Used with
                     embedded_quotes for reference-based resolution.
        licence: Optional PayloadLicence every rendered entry, quote and
                 reference reports to (#326).
        artifact_refs: False leaves out every artifact reference line and
                 the text of system-prompt nodes (their replies still
                 render under the same labels), for an export that
                 omits artifacts (#550). A tree that is only a system
                 prompt then renders as "".
        prune_tombstones: True leaves out every deleted node with no
                 entry rendered below it, and renders a chain of deleted
                 nodes, each the only one shown under the one before, as
                 one line ("[N nodes deleted by author]"), for the
                 budgeted raw window (#525). A placeholder then only
                 holds the place of a reply below it (§5a). A tree with
                 no entry rendered in it is "".
        references: Optional _QuoteReferences of the whole export
                 (#592): each rendered entry's label is recorded in it,
                 and quotes resolved by reference leave its markers.
        hold_others_markers: see _format_node_text.
        reference_titles: see _format_node_text.

    Yields:
        str: pieces of the formatted text of the node tree, nothing for
        a tree that renders as "".
    """
    from backend.utils.privacy import can_user_view_tombstone, shown_as_deleted

    def _deleted(n):
        # Soft-deleted, or hidden with its owner's deleted account
        # (#269): the same tombstone either way, as after the purge.
        if user_id is None:
            return n.deleted_at is not None
        return shown_as_deleted(n, user_id)

    def _children(n, keep_tombstones):
        return _filtered_children(
            n, filter_ai_usage, created_before, included_ids,
            keep_tombstones=keep_tombstones, user_id=user_id)

    def _dead_tombstones():
        """The tombstones the walk below would render with no "node"
        frame (an entry) under them, for prune_tombstones."""
        dead, live, below, seen = set(), {}, {}, set(processed_nodes)
        stack = [("tombstone" if _deleted(node) else "node", node, False)]
        while stack:
            kind, current, done = stack.pop()
            if done:
                live[current.id] = kind == "node" or any(
                    live.get(c.id, False) for c in below[current.id])
                if kind == "tombstone" and not live[current.id]:
                    dead.add(current.id)
                continue
            if current.id in seen:
                continue
            seen.add(current.id)
            # The walk's access rule, for every node (see the "node"
            # frame below). A deleted child the user could not see is
            # left out with everything below it, so it counts as dead:
            # it takes no child slot and does not break a run.
            kids = []
            for child in _children(current, kind != "inaccessible"):
                if _deleted(child):
                    if user_id is None or can_user_view_tombstone(
                            child, user_id):
                        kids.append(("tombstone", child))
                    else:
                        dead.add(child.id)
                elif user_id and not can_user_access_node(child, user_id):
                    kids.append(("inaccessible", child))
                else:
                    kids.append(("node", child))
            below[current.id] = [c for _, c in kids]
            stack.append((kind, current, True))
            stack.extend((k, c, False) for k, c in kids)
        return dead

    def _reply_frames(label, children):
        """The frames of the replies shown under the entry labelled
        *label*, ready to push: each reply with its label, and a
        separator before every branch after the first. A reply the
        walk leaves out (a deleted node the user could not see, a dead
        tombstone, a node already rendered) takes no label."""
        shown = [
            c for c in children
            if c.id not in dead and c.id not in processed_nodes
            and not (user_id is not None and _deleted(c)
                     and not can_user_view_tombstone(c, user_id))
        ]
        frames = []
        for i, (child, child_label) in enumerate(
                zip(shown, _reply_labels(label, len(shown)))):
            if i:
                frames.append(("text", _branch_separator(label)))
            frames.append(("node", child, child_label))
        # LIFO: reversed, so replies pop left-to-right (pre-order).
        return reversed(frames)

    if processed_nodes is None:
        processed_nodes = set()
    dead = _dead_tombstones() if prune_tombstones else set()
    if node.id in dead:
        return
    quote_reference = references.marker if references is not None else None

    stack = [("node", node, index_path)]
    while stack:
        frame = stack.pop()
        if frame[0] == "text":
            yield frame[1]
            continue
        kind, current, path = frame

        if kind == "node":
            # Avoid infinite loops from circular references
            if current.id in processed_nodes:
                continue
            # The access rule, the same for every node at any depth,
            # under a live node or a placeholder shell. Soft-deleted:
            # the tombstone shell (no content), only for a user who
            # could see the node before; otherwise it is left out with
            # everything below it (no structural "something is here").
            # Not accessible to the user: the inaccessible shell.
            if _deleted(current):
                if user_id is not None and not can_user_view_tombstone(
                        current, user_id):
                    continue
                kind = "tombstone"
            elif user_id and not can_user_access_node(current, user_id):
                kind = "inaccessible"

        if kind == "node":
            processed_nodes.add(current.id)
            if before_entry is not None:
                before_entry(current)
            text = _format_node_text(
                current, path, user_id, embedded_quotes, ai_blocked_ids,
                licence=licence, filter_ai_usage=filter_ai_usage,
                artifact_refs=artifact_refs,
                quote_reference=quote_reference,
                hold_others_markers=hold_others_markers,
                reference_titles=reference_titles)
            if text and references is not None:
                references.labels[current.id] = path
            stack.extend(_reply_frames(path, _children(current, True)))
            yield text
            continue

        # Placeholder shells (tombstone / inaccessible). Children still
        # render — they may be alive, accessible replies. All children
        # route through the "node" kind (cycle check + the access rule),
        # labelled and separated as under an entry.
        last = current
        if kind == "tombstone":
            keep_tombstones = True
            run = 1
            # Pruned: a chain of deleted nodes, each the only one shown
            # under the one before, is one line (#525).
            while prune_tombstones:
                kids = [c for c in _children(last, True)
                        if c.id not in dead]
                if (len(kids) != 1 or not _deleted(kids[0])
                        or kids[0].id in processed_nodes
                        or (user_id is not None
                            and not can_user_view_tombstone(
                                kids[0], user_id))):
                    break
                processed_nodes.add(last.id)
                last = kids[0]
                run += 1
            placeholder = (f"{TOMBSTONE_TEXT}\n\n" if run == 1
                           else f"[{run} nodes deleted by author]\n\n")
        else:
            # Another user's reply the user cannot access leaves no
            # trace but its place: no author, no time.
            placeholder = f"{INACCESSIBLE_TEXT}\n\n"
            keep_tombstones = False
        header = (_node_header_line(current, path) if kind == "tombstone"
                  else _index_heading(path) + "\n")
        yield header + placeholder
        processed_nodes.add(current.id)
        processed_nodes.add(last.id)
        stack.extend(_reply_frames(path, _children(last, keep_tombstones)))


def _collect_all_nodes_in_tree(node, filter_ai_usage=False, created_before=None,
                               collected=None, user_id=None):
    """
    Collect all accessible nodes in a tree, in pre-order.

    Iterative (explicit stack) rather than recursive: a recursive walk
    overflowed Python's call-stack limit on users with very deep reply
    chains — each level also lazy-loads ``node.children``, multiplying the
    frames per level — which crashed full profile regeneration before any
    LLM call. An explicit stack bounds depth by the heap instead.

    Args:
        node: Root node of the tree
        filter_ai_usage: If True, only include nodes where ai_usage is 'chat' or 'train'
        created_before: Optional datetime filter
        collected: Set of already collected node IDs (dedupe + cycle guard)
        user_id: If provided, skip inaccessible nodes but still descend
                into their children (which may be accessible).

    Returns:
        List of Node objects in the tree, pre-order (parent before children,
        children left-to-right) — identical ordering to the previous
        recursive implementation.
    """
    if collected is None:
        collected = set()

    result = []
    # LIFO stack; push children reversed so they pop left-to-right, giving
    # the same pre-order the recursive version produced.
    stack = [node]
    while stack:
        current = stack.pop()

        if current.id in collected:
            continue
        collected.add(current.id)

        # Include the node only if it's accessible (or no user_id check);
        # descend into children regardless — they may be accessible.
        if not (user_id and not can_user_access_node(current, user_id)):
            result.append(current)

        children = current.children
        if filter_ai_usage:
            children = [c for c in children if c.ai_usage in AI_ALLOWED]
        if created_before:
            children = [c for c in children if c.created_at < created_before]

        for child in reversed(children):
            stack.append(child)

    return result


def _scope_filter(user_id, filter_ai_usage, created_before, created_after):
    """The filter every row of the incremental scope passes, anchor,
    parent or reply alike.

    _export_visible_filter includes soft-deleted nodes the user owns so
    the export tree can render them as tombstones (§5a). Fully-deleted
    threads end up with no rows and are skipped at the entry-point
    selection step downstream."""
    clauses = [_export_visible_filter(Node, user_id)]
    if filter_ai_usage:
        clauses.append(Node.ai_usage.in_(AI_ALLOWED))
        # After a no to the tweet seed, the tweets a pre-fill brought in
        # are not read (#662). Empty for every other account.
        clauses.extend(withheld_prefill_clauses(user_id))
    if created_before is not None:
        clauses.append(Node.created_at < created_before)
    if created_after is not None:
        clauses.append(Node.created_at > created_after)
    return clauses


def _select_incremental_rows(user_id, filter_ai_usage=False,
                             created_before=None, created_after=None):
    """Return rows for the incremental-export scope.

    Used when build_user_export_content is called with `created_after`.
    Returns a list of namedtuple-like rows with .id, .parent_id,
    .created_at, .token_count for nodes in the target user's
    "conversational scope":

      - Anchors: target's own or addressed nodes
        (`user_id == uid OR human_owner_id == uid`) that pass the
        usual filters (accessible, ai_usage, created_before/after).
      - Climb up from anchors: include parents that pass the same
        filters. Stops when an ancestor fails (typically pre-cutoff).
      - Climb down from anchors: include descendants that pass the
        same filters.

    Foreign post-cutoff ancestors that pass `accessible_nodes_filter`
    are included (the conversation the target is responding to).
    Foreign siblings of anchors are NOT included (they are neither
    ancestors nor descendants of any anchor).

    Implementation note: iterates BFS in Python over indexed SQL
    queries (one per depth layer in each direction). Typical
    user-tree depths are <10, so this is a small number of fast
    queries. Could be folded into a single recursive CTE if profiling
    ever shows it matters; kept iterative for portability between
    PostgreSQL and the SQLite test database.

    ``WindowScope.rows`` computes the same rows in memory for a profile
    build's windows (#612); a change here needs the same change there
    (tests/test_window_scope.py compares the two).
    """
    cols = (Node.id, Node.parent_id, Node.created_at, Node.token_count,
            Node.deleted_at, Node.origin)

    # Built once, so every layer of the walk applies the same filter (the
    # tweet-seed answer is read once per call, #662).
    general_filter = _scope_filter(user_id, filter_ai_usage, created_before,
                                   created_after)

    def _general_filter():
        return general_filter

    # 1. Anchors: target-owned/addressed AND pass general filters.
    anchor_rows = db.session.query(*cols).filter(
        or_(Node.user_id == user_id, Node.human_owner_id == user_id),
        *_general_filter(),
    ).all()

    rows_by_id = {r.id: r for r in anchor_rows}

    # 2. Climb UP from anchors. Each iteration: fetch parents matching
    #    filters that we haven't seen yet. Stop when no new parents.
    pending_parent_ids = {
        r.parent_id for r in anchor_rows if r.parent_id is not None
    } - rows_by_id.keys()
    while pending_parent_ids:
        parent_rows = db.session.query(*cols).filter(
            Node.id.in_(pending_parent_ids),
            *_general_filter(),
        ).all()
        next_pending = set()
        for pr in parent_rows:
            if pr.id in rows_by_id:
                continue
            rows_by_id[pr.id] = pr
            if pr.parent_id is not None:
                next_pending.add(pr.parent_id)
        pending_parent_ids = next_pending - rows_by_id.keys()

    # 3. Climb DOWN from anchors. Each iteration: fetch children whose
    #    parent is in the current frontier and that pass filters.
    frontier_ids = {r.id for r in anchor_rows}
    while frontier_ids:
        child_rows = db.session.query(*cols).filter(
            Node.parent_id.in_(frontier_ids),
            *_general_filter(),
        ).all()
        next_frontier = set()
        for cr in child_rows:
            if cr.id in rows_by_id:
                continue
            rows_by_id[cr.id] = cr
            next_frontier.add(cr.id)
        frontier_ids = next_frontier

    return list(rows_by_id.values())


def count_remaining_units(user_id, created_after=None, filter_ai_usage=True,
                          created_before=None):
    """Stored content units (Node.token_count) still ahead of the profile
    chunk loop after ``created_after``: the sum over exactly the rows the
    incremental export window draws from (anchor scope plus climb-up
    ancestors and climb-down descendants, tombstones included, the same
    filters), so the plan made from this figure and the window it
    produces agree. The chunk planner calls it before every chunk
    (docs/design/chunk-planner.md). No node is loaded or decrypted.
    ``created_before`` (exclusive) bounds a window set's gap (#545).
    """
    rows = _select_incremental_rows(
        user_id, filter_ai_usage=filter_ai_usage,
        created_after=created_after, created_before=created_before)
    return sum((r.token_count or 0) for r in rows)


# ── One metadata read per profile pass (#612) ─────────────────────────────

ScopeRow = namedtuple(
    "ScopeRow", "id parent_id created_at token_count deleted_at origin")
_SCOPE_COLS = (Node.id, Node.parent_id, Node.created_at, Node.token_count,
               Node.deleted_at, Node.origin)
_by_id = attrgetter("id")


class ScopeChanged(RuntimeError):
    """A row a window selected from a WindowScope is no longer as the
    scope read it (hidden, deleted, edited, moved or gone): the caller
    reads the scope again before it renders the window."""


def _date_test(created_after, created_before):
    """The created_at bounds of _scope_filter as a test of one row, with
    SQL's NULL semantics: a row without a date passes no bound."""
    if created_after is None and created_before is None:
        return lambda row: True

    def test(row):
        t = row.created_at
        if t is None:
            return False
        if created_after is not None and not t > created_after:
            return False
        return created_before is None or t < created_before
    return test


class WindowScope:
    """The metadata of the writing a profile build's windows read, from
    one read of the database (#612): every row ``_select_incremental_rows``
    can return for a window after ``floor``, as id, parent, date, units,
    deleted state and origin (no content). A profile pass reads it once
    and cuts each window from it. Before, every window read the metadata
    of all the writing after its start again, once to plan the window
    and once per render while it was fitted to the model's cap, so the
    work grew with windows x entries.

    ``rows(created_after, created_before)`` returns exactly what
    ``_select_incremental_rows(user_id, filter_ai_usage, created_before,
    created_after)`` returns for a ``created_after`` at or after the
    floor: the same rows, in the order SQLite returns them: the anchors
    (the user's own rows, then the rows addressed to them, each by id),
    then the parents the climb up adds and the replies the climb down
    adds, a layer at a time, by id within a layer. Only the dates of the
    filter are applied here; the rest of ``_scope_filter`` (access,
    tombstones, ai_usage) is the database's, read with the rows. The
    order matters only for rows that share a date; on Postgres the old
    walk gave them its query plan's order, so there a window can end
    elsewhere in a run of such rows (each run stays whole in one window).

    The read covers what any window's walk can reach: the anchors after
    the floor, every ancestor the climb up reaches through rows that pass
    the filter, and every reply the climb down reaches from an anchor,
    also through a parent the climb up found (a window whose dates leave
    out the anchor below such a parent climbs down through it).

    ``fresh()`` says whether the user's rows changed since the read (a
    row added, removed, updated, deleted or restored) or an account was
    deleted or restored; ``check`` whether the rows a window's text is
    decided from are still as read: its own rows, the rows that put them
    in scope, and the replies under them. A pass reads the scope again
    rather than render from a row someone hid or changed meanwhile."""

    def __init__(self, user_id, filter_ai_usage=True, floor=None):
        self.user_id = user_id
        self.filter_ai_usage = filter_ai_usage
        self.floor = floor
        self.reads = 0
        self.reload()

    def _fingerprint(self):
        from backend.models import User
        uid = self.user_id
        # "Delete all my content" and its restore set and clear deleted_at
        # without touching updated_at (user_purge.hide_writing): the
        # deleted count and newest deletion see them. Accounts deleted or
        # restored change what other people's rows show (#269).
        own = tuple(db.session.query(
            func.count(Node.id), func.max(Node.id),
            func.max(Node.updated_at), func.count(Node.deleted_at),
            func.max(Node.deleted_at),
        ).filter(or_(Node.user_id == uid, Node.human_owner_id == uid)).one())
        gone = tuple(db.session.query(
            func.count(User.id), func.max(User.deleted_at),
        ).filter(User.deleted_at.isnot(None)).one())
        # A changed answer to the tweet seed changes which rows the
        # filter passes (#662).
        consent = (db.session.query(User.prefill_consent).filter(
            User.id == uid).scalar(),)
        return own + gone + consent

    @staticmethod
    def _fetch(column, ids, clauses):
        ids = sorted(ids)
        for i in range(0, len(ids), _WALK_CHUNK):
            for r in db.session.query(*_SCOPE_COLS).filter(
                    column.in_(ids[i:i + _WALK_CHUNK]), *clauses):
                yield ScopeRow(*r)

    def reload(self):
        """Read the metadata (again)."""
        uid = self.user_id
        # Before the rows: a change during the read shows at the next
        # fresh().
        self._mark = self._fingerprint()
        clauses = _scope_filter(uid, self.filter_ai_usage, None, self.floor)
        rows, own, addressed = {}, [], []
        for r in db.session.query(*_SCOPE_COLS, Node.user_id).filter(
                or_(Node.user_id == uid, Node.human_owner_id == uid),
                *clauses):
            row = ScopeRow(*r[:6])
            rows[row.id] = row
            (own if r[6] == uid else addressed).append(row)
        anchors = sorted(own, key=_by_id) + sorted(addressed, key=_by_id)
        anchor_ids = set(rows)

        # Up: the ancestors any window's climb up can reach.
        pending = {r.parent_id for r in anchors
                   if r.parent_id is not None} - rows.keys()
        while pending:
            found = [row for row in self._fetch(Node.id, pending, clauses)
                     if row.id not in rows]
            for row in found:
                rows[row.id] = row
            pending = {r.parent_id for r in found
                       if r.parent_id is not None} - rows.keys()

        # Down: the replies any window's climb down can reach. A reply
        # that is an anchor was read above; the others are other people's.
        others = clauses + [and_(
            Node.user_id != uid,
            or_(Node.human_owner_id.is_(None), Node.human_owner_id != uid))]
        expanded = set(anchor_ids)
        frontier = anchor_ids
        while frontier:
            nxt = set()
            for row in self._fetch(Node.parent_id, frontier, others):
                rows.setdefault(row.id, row)
                if row.id not in expanded:
                    expanded.add(row.id)
                    nxt.add(row.id)
            frontier = nxt

        dated = [(row.created_at, i, row) for i, row in enumerate(anchors)
                 if row.created_at is not None]
        dated.sort(key=itemgetter(0, 1))
        self._dated = dated
        self._dates = [d[0] for d in dated]
        self._undated = [(None, i, row) for i, row in enumerate(anchors)
                         if row.created_at is None]
        children = {}   # parent id -> its replies that are not anchors
        for row in rows.values():
            if row.id not in anchor_ids and row.parent_id is not None:
                children.setdefault(row.parent_id, []).append(row)
        for replies in children.values():
            replies.sort(key=_by_id)
        self._rows = rows
        self._anchor_ids = anchor_ids
        self._children = children
        # Only these anchors can add rows: one whose parent is someone
        # else's row (the climb up), one with someone else's replies (the
        # climb down). An anchor's anchor parent or reply is in a window
        # by its own date or not at all.
        self._up = [a for a in anchors if a.parent_id in rows
                    and a.parent_id not in anchor_ids]
        self._down = [a for a in anchors if a.id in children]
        self._last = None
        self.reads += 1

    def rows(self, created_after=None, created_before=None):
        """``_select_incremental_rows`` for these dates, from memory. The
        caller does not change the list (the last one is kept for the
        next call with the same dates: a window is planned and rendered
        several times on them)."""
        key = (created_after, created_before)
        if self._last is not None and self._last[0] == key:
            return self._last[1]
        if self.floor is not None and (created_after is None
                                       or created_after < self.floor):
            raise ValueError("a window before the scope's floor")
        test = _date_test(created_after, created_before)
        lo = (0 if created_after is None
              else bisect_right(self._dates, created_after))
        hi = (len(self._dates) if created_before is None
              else bisect_left(self._dates, created_before))
        anchors = self._dated[lo:hi]
        if created_after is None and created_before is None:
            anchors += self._undated
        anchors.sort(key=itemgetter(1))
        out = {row.id: row for _date, _i, row in anchors}
        via = {}
        self._climb_up(out, via, test)
        self._climb_down(out, via, test)
        result = list(out.values())
        self._last = (key, result, via, {})
        return result

    def _climb_up(self, out, via, test):
        """Step 2 of _select_incremental_rows: the parents of the anchors
        in ``out`` that pass, then theirs, a layer at a time. ``via``
        gets, for each, a child it was reached from."""
        get = self._rows.get
        pending = {}
        for a in self._up:
            if a.id in out:
                pending.setdefault(a.parent_id, a.id)
        while pending:
            layer = [row for row in map(get, sorted(pending))
                     if row is not None and test(row)]
            nxt = {}
            for row in layer:
                out[row.id] = row
                via[row.id] = pending[row.id]
            for row in layer:
                if row.parent_id is not None and row.parent_id not in out:
                    nxt.setdefault(row.parent_id, row.id)
            pending = nxt

    def _climb_down(self, out, via, test):
        """Step 3: the replies that pass under the anchors, then under
        the replies added, a layer at a time; a row already in ``out`` is
        neither added nor climbed through (as the database walk skips
        it). ``via`` gets each one's parent."""
        children = self._children
        frontier = [a for a in self._down if a.id in out]
        while frontier:
            new = {}
            for parent in frontier:
                for row in children.get(parent.id, ()):
                    if row.id not in out and test(row):
                        new[row.id] = row
            frontier = sorted(new.values(), key=_by_id)
            for row in frontier:
                out[row.id] = row
                via[row.id] = row.parent_id

    def units(self, created_after=None, created_before=None):
        """``count_remaining_units`` for these dates, from memory."""
        return sum((r.token_count or 0)
                   for r in self.rows(created_after, created_before))

    def fresh(self):
        """Whether, since the read, none of the user's rows was added,
        removed, updated, deleted or restored (their count, newest id,
        newest update, deleted count and newest deletion) and no account
        was deleted or restored."""
        return self._fingerprint() == self._mark

    def check(self, ids, created_after=None, created_before=None,
              replies_of=()):
        """Raise ScopeChanged unless the rows a window's text is decided
        from are as the scope read them, for these dates:

        - every row ``ids`` names (the window's own rows, before they are
          loaded; the rows the per-thread skip rule walked), and the rows
          that put each one in scope (its path to an anchor), still pass
          the scope's filter with the parent, date, units, deleted state
          and origin read;
        - each anchor in ``replies_of`` has exactly the replies that pass
          the filter which the read gave it: none hidden, deleted, made
          private or gone with its author's account, and none new. An
          anchor's replies that pass are all in scope, so the read's
          replies are the database walk's.

        Someone else's row the window does not show can thus still decide
        nothing about it from the read: a thread whose only live reply
        was deleted after the read is left out, as the database walk
        leaves it out (§5a)."""
        self.rows(created_after, created_before)
        _key, result, via, children = self._last
        clauses = _scope_filter(self.user_id, self.filter_ai_usage,
                                created_before, created_after)
        self._check_rows(ids, via, clauses)
        parents = {i for i in replies_of if i in self._anchor_ids}
        if parents:
            self._check_replies(parents, result, children, clauses)

    def _check_rows(self, ids, via, clauses):
        want = set(ids)
        for node_id in list(want):
            while node_id in via:
                node_id = via[node_id]
                want.add(node_id)
        live = {row.id: row for row in self._fetch(Node.id, want, clauses)}
        for node_id in want:
            if live.get(node_id) != self._rows.get(node_id):
                raise ScopeChanged(
                    f"user {self.user_id}: row {node_id} changed since the "
                    f"window scope was read")

    def _check_replies(self, parents, result, children, clauses):
        if not children:
            for row in result:
                if row.parent_id is not None:
                    children.setdefault(row.parent_id, set()).add(row)
        replies = {}
        for row in self._fetch(Node.parent_id, parents, clauses):
            replies.setdefault(row.parent_id, set()).add(row)
        for parent_id in parents:
            read = children.get(parent_id, set())
            if replies.get(parent_id, set()) != read:
                raise ScopeChanged(
                    f"user {self.user_id}: the replies under row "
                    f"{parent_id} changed since the window scope was read")


def _thread_has_alive_node(root_id):
    """Per-thread skip rule (§5a): does the subtree rooted at *root_id*
    contain any node with deleted_at IS NULL?

    Returns True for a thread that should appear in the export (mixed or
    fully alive); False for a fully-deleted thread that should be
    omitted entirely so the AI doesn't see ghost-tombstone breadcrumbs
    of content the user wanted gone.

    One thread's answer of ``_threads_with_alive_node``.
    """
    return root_id in _threads_with_alive_node([root_id])


# Ids per IN list of the export's batched queries (#591): the threads
# per query of the deleted-thread check (a query per thread was 84 of
# the 120 s a 42k-entry export took), the parents per query of the
# level-by-level thread walks, and the entries per query the download
# loads by id. INTRODUCED CONSTANT.
IDS_PER_QUERY = 500


def _threads_with_alive_node(root_ids, batch=None):
    """The ids among *root_ids* whose subtree holds a node with
    deleted_at IS NULL: the per-thread skip rule (§5a) of
    ``_thread_has_alive_node``.

    A caller with many threads passes only those whose root is deleted:
    a thread with a live root is kept without a query.

    The threads are walked level by level, *batch* ids (default
    IDS_PER_QUERY) per ``IN (...)`` query, and a thread's walk stops at
    its first live node. This was one recursive query per batch of
    threads (#591), which Postgres planned as a full scan of ``node`` for
    every level of the deepest thread (it cannot estimate a recursive
    work table; see _foreign_thread_node_ids): on a 1M-row table, 400
    deleted threads up to 130 levels deep took 38 s, and 19 took 1.2 s
    (local measurement, 2026-11-11).
    """
    batch = batch or IDS_PER_QUERY
    ids = list(root_ids)
    alive = set()
    root_of = {}     # a deleted node still walked -> the thread it is in
    for start in range(0, len(ids), batch):
        for node_id, deleted_at in db.session.query(
                Node.id, Node.deleted_at).filter(
                Node.id.in_(ids[start:start + batch])):
            if deleted_at is None:
                alive.add(node_id)
            else:
                root_of[node_id] = node_id
    level = list(root_of)
    while level:
        next_level = []
        for start in range(0, len(level), batch):
            for node_id, parent_id, deleted_at in db.session.query(
                    Node.id, Node.parent_id, Node.deleted_at).filter(
                    Node.parent_id.in_(level[start:start + batch])):
                root = root_of[parent_id]
                if root in alive or node_id in root_of:
                    continue
                if deleted_at is None:
                    alive.add(root)
                else:
                    root_of[node_id] = root
                    next_level.append(node_id)
        level = [n for n in next_level if root_of[n] not in alive]
    return alive


def _window_places(rows, user_id, filter_ai_usage):
    """Where each selected row of a budgeted window renders (#525):
    {row id: (the window row it renders under, or None when it starts a
    block; the created_at of its thread's root)}.

    The window takes the newest rows, so a thread's oldest rows, its
    root first, are the first to fall outside it. A row renders under
    its parent when the parent is in the window. A row whose parent is
    not starts a block of its own: the thread's root, or, when the
    thread started before the window, its newest part. (Rendering from
    roots only, a session longer than the window emptied it.)

    A row is left out (absent) where it is when its thread renders
    whole: when the thread is not the user's own (this export's scope),
    or, with *filter_ai_usage*, when a node above it is outside
    AI_ALLOWED (the walk down stops there).
    """
    window = {n.id: n for n in rows}
    loose = [n.id for n in rows
             if n.parent_id is not None and n.parent_id not in window]
    chains = ancestor_chains(
        loose, Node.user_id, Node.ai_usage, Node.created_at)
    # Row id -> the window row it renders under, None for a block's
    # start. A row missing here is left out.
    under = {}
    root_at = {}   # block start -> its thread root's created_at
    for n in rows:
        if n.parent_id is None:
            if n.user_id == user_id:
                under[n.id] = None
                root_at[n.id] = n.created_at
            continue
        if n.parent_id in window:
            under[n.id] = n.parent_id
            continue
        for row in chains.get(n.id, ()):
            if filter_ai_usage and row.ai_usage not in AI_ALLOWED:
                break
            if row.id in window:
                under[n.id] = row.id
                break
            if row.parent_id is None:
                if row.user_id == user_id:
                    under[n.id] = None
                    root_at[n.id] = row.created_at
                break

    # Each row's block start; None when the row is left out.
    start_of = {}
    for n in rows:
        path, seen, cur = [], set(), n.id
        while (cur not in start_of and under.get(cur) is not None
               and cur not in seen):
            path.append(cur)
            seen.add(cur)
            cur = under[cur]
        if cur in start_of:
            start = start_of[cur]
        elif cur in under and cur not in seen:
            start = cur
        else:
            start = None   # left out, or a parent cycle
        for row_id in path + [cur]:
            start_of.setdefault(row_id, start)
    return {row_id: (under[row_id], root_at[start])
            for row_id, start in start_of.items() if start is not None}


def _connecting_tombstones(rows, places):
    """The deleted rows of a budgeted window with an alive row below
    them: their placeholder holds that row's place in its thread (§5a),
    so they cost TOMBSTONE_UNITS. A deleted row with nothing alive below
    is not shown (format_node_tree's prune_tombstones) and costs
    nothing (#525)."""
    alive = {n.id for n in rows if n.deleted_at is None and n.id in places}
    connecting = set()
    for row_id in alive:
        above = places[row_id][0]
        while (above is not None and above not in alive
               and above not in connecting):
            connecting.add(above)
            above = places[above][0]
    return connecting


def _window_blocks(kept, places):
    """The blocks a budgeted window renders from the rows the resolver
    kept (#525). A kept row starts a block when the row it renders under
    is not kept. A block with no alive row is left out (§5a).

    Returns (starts, continued_from, shown): the rows that start a
    block, oldest first; {start id: the thread root's created_at} for
    the blocks that continue an earlier thread; and the ids of the rows
    the blocks show.
    """
    kept_by_id = {n.id: n for n in kept if n.id in places}
    start_of = {}
    for row_id in kept_by_id:
        path, cur = [], row_id
        while cur not in start_of:
            above = places[cur][0]
            if above is None or above not in kept_by_id:
                break
            path.append(cur)
            cur = above
        start = start_of.get(cur, cur)
        for i in path + [cur]:
            start_of.setdefault(i, start)

    rows_of = {}
    for row_id, start in start_of.items():
        rows_of.setdefault(start, []).append(kept_by_id[row_id])
    starts = sorted(
        (kept_by_id[start] for start, rows in rows_of.items()
         if any(r.deleted_at is None for r in rows)),
        key=lambda n: (n.created_at, n.id),
    )
    continued_from = {s.id: places[s.id][1] for s in starts
                      if s.parent_id is not None}
    shown = {r.id for s in starts for r in rows_of[s.id]}
    return starts, continued_from, shown


# Per-query id-list size for the level-batched thread walk. Keeps each
# statement small on SQLite (old builds cap bound variables at 999) and
# cheap to plan on Postgres.
_WALK_CHUNK = 900


def _foreign_thread_node_ids(user_id):
    """IDs of nodes by OTHER authors inside the user's thread trees.

    A user's thread trees are every node they authored plus all
    descendants (#110: seeded from every authored node, not just their
    top-level threads, so replies inside other users' threads count).
    The authored half needs no id list — callers match it with
    ``Node.user_id == user_id`` — so this walks only the other authors'
    nodes: LLM replies, other people's comments, and the sub-threads
    under them. Soft-deleted nodes are walked through; visibility is the
    caller's filter.

    Level 1 is a plain join (children of any authored node), which the
    planner estimates from real column stats. Deeper levels are walked
    with one ``parent_id IN (...)`` seek per level, and a level stops at
    the next authored node because that node is its own seed. On a
    user/LLM alternating thread the walk ends after two levels.

    This replaced a recursive CTE on 2026-09-02: Postgres cannot
    estimate a recursive work table (it assumes 10x the seed), so on a
    260k-row table it chose a hash join fed by a full sequential scan of
    ``node`` for EVERY recursion level — a 120-level thread meant 31
    million rows scanned per call, 8.5 s on prod, on every thread open
    and every LLM reply.
    """
    parent = db.aliased(Node)
    frontier = [
        row[0] for row in
        db.session.query(Node.id)
        .join(parent, Node.parent_id == parent.id)
        .filter(parent.user_id == user_id, Node.user_id != user_id)
    ]
    seen = set(frontier)
    while frontier:
        next_level = []
        for i in range(0, len(frontier), _WALK_CHUNK):
            chunk = frontier[i:i + _WALK_CHUNK]
            next_level.extend(
                row[0] for row in
                db.session.query(Node.id).filter(
                    Node.parent_id.in_(chunk), Node.user_id != user_id,
                )
                if row[0] not in seen
            )
        seen.update(next_level)
        frontier = next_level
    return seen


def _preselect_node_ids(user_id, budget, filter_ai_usage=False,
                        created_before=None, created_after=None,
                        chronological_order=False):
    """Select node IDs that fit within a token budget using a SQL window function.

    Covers the user's thread trees — their own nodes plus other authors'
    nodes under them (see _foreign_thread_node_ids) — filtered to
    accessible nodes only.  Returns a list of node IDs ordered by
    created_at (direction controlled by chronological_order).  No nodes
    are loaded or decrypted. A deleted node costs the budget nothing
    (_alive_units): the window shows it only where it holds an entry's
    place, as one placeholder line (#525). The selection passes at most
    MAX_WINDOW_DELETED_ROWS deleted nodes.
    """
    foreign_ids = sorted(_foreign_thread_node_ids(user_id))
    sort_order = (
        Node.created_at.asc() if chronological_order
        else Node.created_at.desc()
    )
    cumul = func.sum(_alive_units(Node)).over(
        order_by=sort_order
    ).label("cumul")
    deleted_cumul = func.sum(
        case((Node.deleted_at.is_(None), 0), else_=1)
    ).over(order_by=sort_order).label("deleted_cumul")

    # The row's units and created_at ride along in the window subquery so
    # the budget cut and the final ordering need no second join back to
    # node (which the planner ran as another full scan).
    inner = db.session.query(
        Node.id, _alive_units(Node).label("units"), Node.created_at, cumul,
        deleted_cumul,
    ).filter(
        or_(Node.user_id == user_id, Node.id.in_(foreign_ids)),
        # Includes soft-deleted user-owned nodes so they can render as
        # tombstones in the export tree (§5a).
        _export_visible_filter(Node, user_id),
    )
    if filter_ai_usage:
        inner = inner.filter(Node.ai_usage.in_(AI_ALLOWED),
                             *withheld_prefill_clauses(user_id))
    if created_before:
        inner = inner.filter(Node.created_at < created_before)
    if created_after:
        inner = inner.filter(Node.created_at > created_after)

    inner = inner.subquery()

    selected = db.session.query(inner.c.id, inner.c.created_at).filter(
        inner.c.cumul - inner.c.units < budget,
        inner.c.deleted_cumul <= MAX_WINDOW_DELETED_ROWS,
    ).order_by(
        inner.c.created_at.asc() if chronological_order
        else inner.c.created_at.desc()
    ).all()
    if not selected:
        return []
    ids = [row[0] for row in selected]
    # A budget window ends between two nodes; when several nodes share
    # the boundary timestamp, take them all. The next window starts at
    # `created_at > cutoff`, so a node left behind on the same second as
    # the cutoff node would never be read by any chunk (tweets carry
    # second precision, so ties are reachable).
    edge_ts = selected[-1][1]
    seen = set(ids)
    ties = db.session.query(inner.c.id).filter(
        inner.c.created_at == edge_ts, inner.c.id.notin_(ids)).all()
    ids.extend(row[0] for row in ties if row[0] not in seen)
    return ids


def get_raw_data_date_range(user_id, max_tokens=10000, created_before=None):
    """Return (earliest, latest) created_at for the pre-selected raw data window.

    This is an approximation: the resolver may pull in older nodes as quote
    dependencies, but using those dates would be misleading (e.g. a quote
    from years back doesn't mean the raw data "covers" that period).  The
    pre-selection window reflects the actual recent writing period.

    Runs only the SQL window function — no nodes are loaded or decrypted.
    Returns (earliest, latest, total_tokens) or (None, None, 0).
    """
    budget = max_tokens - 100  # header/footer reserve
    selected_ids = _preselect_node_ids(
        user_id, budget, filter_ai_usage=True,
        created_before=created_before, chronological_order=False,
    )
    if not selected_ids:
        return None, None, 0

    row = db.session.query(
        func.min(Node.created_at), func.max(Node.created_at),
        func.coalesce(func.sum(_alive_units(Node)), 0),
    ).filter(Node.id.in_(selected_ids)).one()
    return row[0], row[1], row[2]


def _entry_point_top_level_started(node, user_id):
    """Walk node.parent until parent_id IS NULL; return that root's
    created_at IFF the root is accessible to user_id. Falls back to
    None if the walk hits a missing parent, a cycle, or an
    inaccessible root — the caller renders a generic preamble
    without the date in that case.

    Checking accessibility prevents leaking the start-date of a
    private foreign thread whose root was excluded by
    accessible_nodes_filter during the CTE walk.

    Used only by the incremental export. O(depth) ORM lookups per
    entry point — N+1 potential, fine for typical thread depths
    (≤ ~10) and small entry-point counts. If that ever proves too
    expensive, batch the ancestor lookup with a single recursive
    CTE keyed by entry id.
    """
    cur = node
    seen = set()
    while cur is not None and cur.parent_id is not None:
        if cur.id in seen:  # defensive: avoid cycles
            return None
        seen.add(cur.id)
        cur = cur.parent
    if cur is None:
        return None
    # Allow soft-deleted-with-pre-access roots through (the user's own
    # tombstoned thread roots): the entry-point preamble's start-date
    # is the user's own data and is fine to surface in their export.
    if not can_user_access_node(cur, user_id):
        from backend.utils.privacy import can_user_view_tombstone
        if not (cur.deleted_at is not None
                and can_user_view_tombstone(cur, user_id)):
            return None
    return cur.created_at


PREAMBLE_PREFIX = "> [Continuation of thread"


def _format_preamble(top_started_at):
    """Render the entry-point preamble. If we know when the thread
    started, include the date so the summarizer can distinguish
    multiple older-thread continuations."""
    if top_started_at is not None:
        date_str = top_started_at.strftime("%Y-%m-%d")
        return (
            f"{PREAMBLE_PREFIX} started {date_str} — earlier context "
            f"in this thread is not shown.]"
        )
    return (
        f"{PREAMBLE_PREFIX} — earlier context in this thread is not shown.]"
    )


def _build_user_export_incremental(
    user, max_tokens, filter_ai_usage, created_before, created_after,
    chronological_order, return_metadata, collapse_artifacts,
    licence=None, omit_artifacts=False, whole_window=False, scope=None,
    hold_others_markers=False, reference_titles=None,
):
    """Incremental export path (created_after is set). See
    `build_user_export_content` docstring for behavior. The CTE row
    set defines `included_ids`; entry points are CTE rows whose
    parent is not in scope (preamble emitted if parent exists).
    """
    if scope is not None:
        # The rows from the pass's one read (WindowScope, #612); the
        # window's own rows are checked against the database below.
        if (scope.user_id != user.id
                or scope.filter_ai_usage != bool(filter_ai_usage)
                or not max_tokens):
            raise ValueError("the window scope is another export's")
        cte_rows = scope.rows(created_after, created_before)
    else:
        cte_rows = _select_incremental_rows(
            user.id, filter_ai_usage=filter_ai_usage,
            created_before=created_before, created_after=created_after,
        )
    cte_row_ids = {r.id for r in cte_rows}

    if not cte_rows:
        return None

    # Variables for smart quote resolution (used when max_tokens is set).
    embedded_quotes = None
    ai_blocked_ids = None
    resolver = None
    selected_ids = None
    # `render_included_ids` is what `format_node_tree` filters children
    # by. In the no-budget case it equals the CTE set. In the budgeted
    # case it equals the resolver's set (which may include quoted
    # pre-cutoff nodes for embedding).
    render_included_ids = set(cte_row_ids)

    if max_tokens:
        header_footer_tokens = 100
        budget = max_tokens - header_footer_tokens

        # Apply budget windowing to CTE rows. Semantics differ slightly
        # from `_preselect_node_ids`'s SQL window: this loop stops
        # BEFORE a row that would overshoot the budget (strict fit),
        # while the SQL version includes the overshooting row (its
        # predicate is `cumul - token_count < budget`, which is true
        # for the row that straddles the boundary). The strict-fit
        # variant keeps budgeted recent-context calls closer to the
        # caller's stated ceiling and is simpler to reason about.
        # Intentional difference; if callers ever need the old
        # overshoot semantic, add a flag.
        ordered = sorted(
            cte_rows,
            key=lambda r: r.created_at,
            reverse=not chronological_order,
        )
        cumulative = 0
        selected_ids = []
        edge_ts = None
        for r in ordered:
            tk = r.token_count or 0
            if cumulative + tk > budget and cumulative > 0:
                # A window ends between two rows; when the boundary
                # falls inside a run of rows sharing one timestamp, take
                # the whole run. The next window resumes at
                # `created_at > cutoff`, so a row left behind on the
                # cutoff's own second would never be read by any chunk
                # (tweets carry second precision, so ties are reachable).
                if r.created_at != edge_ts:
                    break
            selected_ids.append(r.id)
            cumulative += tk
            edge_ts = r.created_at

        if not selected_ids:
            return None
        if scope is not None:
            # Rows hidden, deleted or edited since the pass read its
            # metadata, and replies under them that changed, are not
            # rendered or decided from that read (ScopeChanged).
            scope.check(selected_ids, created_after, created_before,
                        replies_of=selected_ids)

        selected_nodes = (
            Node.query
            .filter(Node.id.in_(selected_ids))
            .options(subqueryload(Node.context_artifacts))
            .all()
        )
        selected_nodes.sort(
            key=lambda n: n.created_at,
            reverse=not chronological_order,
        )
        # The resolver loop below decrypts every selected node, one KMS
        # unwrap each on a cold worker (~80 ms; a 5k-node profile chunk
        # waited ~7 minutes here, then again at render time once the
        # 4k-entry DEK cache had cycled). One concurrent batch up front;
        # the render loop's windowed prefetch then finds them cached.
        # Deleted rows are never decrypted (#525).
        prefetch_deks(n.content for n in selected_nodes
                      if n.deleted_at is None)

        resolver = ExportQuoteResolver(
            user.id,
            # A whole window's budget is the selection above: the
            # resolver only resolves its quotes (#552).
            None if whole_window else budget,
            filter_ai_usage=filter_ai_usage,
            chronological=chronological_order,
            # A collapsed export emits no preamble, so the budget counts
            # only the entries it does emit (#518).
            with_preamble=not collapse_artifacts,
            inside=quotes_inside(user.id),
        )
        for node in selected_nodes:
            if node.deleted_at is not None:
                # Shown as its placeholder line; its text is never read.
                resolver.add_tombstone(node.id, node.created_at,
                                       TOMBSTONE_UNITS)
                continue
            if omit_artifacts:
                _add_artifact_free_entry(resolver, node)
                continue
            content = node.get_content()
            node_artifacts = [
                (a.artifact_type, a.artifact_id)
                for a in node.context_artifacts
            ]
            prompt = node.get_artifact("prompt")
            if prompt is not None:
                version_num = UserPrompt.query.filter(
                    UserPrompt.user_id == prompt.user_id,
                    UserPrompt.prompt_key == prompt.prompt_key,
                    UserPrompt.created_at <= prompt.created_at,
                ).count()
                resolver.add_node(
                    node_id=node.id,
                    created_at=node.created_at,
                    content=content,
                    token_count=node.token_count,
                    user_prompt_id=prompt.id,
                    prompt_content=content,
                    prompt_label=f"{prompt.title} v{version_num}",
                    artifacts=node_artifacts,
                )
            else:
                resolver.add_node(
                    node_id=node.id,
                    created_at=node.created_at,
                    content=content,
                    token_count=node.token_count,
                    artifacts=node_artifacts,
                )

        resolver.resolve()
        included_ids, embedded_quotes, ai_blocked_ids = (
            resolver.get_resolution_result()
        )
        render_included_ids = included_ids
        if whole_window:
            # The window's own rows, each rendered once as an entry. A
            # node the resolver embedded is also in included_ids; here
            # it shows only inside its quote. Counted as part of the
            # window, an embedded older parent hid the reply under it
            # (the reply was not an entry point, and the parent is not
            # one either), and an embedded newer row was an entry here
            # and again in its own window.
            render_included_ids = set(selected_ids)

    # Per-thread skip rule (§5a): exclude entry points whose entire
    # subtree (within the user's accessible/owned set) is soft-deleted.
    # Without this a fully-deleted thread would surface as just a chain
    # of `[Node deleted by author]` placeholders — which leaks the
    # structural fact "this thread existed" into the AI's recent
    # context even though the user wanted it gone.
    children_by_parent = {}
    for r in cte_rows:
        if r.parent_id is not None:
            children_by_parent.setdefault(r.parent_id, []).append(r)

    walked, expanded = set(), set()   # what the skip rule read (#612)

    def _subtree_has_alive(row):
        # Iterative: recursion depth would equal thread depth, which
        # overflows on very deep reply chains (same class of bug as the
        # old recursive format_node_tree).
        pending = [row]
        while pending:
            r = pending.pop()
            walked.add(r.id)
            if r.deleted_at is None:
                return True
            expanded.add(r.id)
            pending.extend(children_by_parent.get(r.id, []))
        return False

    # Entry points. Iterate CTE rows so quoted-pre-cutoff embeds added
    # by the resolver can never be entry candidates. Parent membership
    # is checked against `render_included_ids` so a budget-ejected
    # parent correctly leaves its surviving child as an entry point.
    entry_rows = [
        r for r in cte_rows
        if r.id in render_included_ids
        and (r.parent_id is None
             or r.parent_id not in render_included_ids)
        and _subtree_has_alive(r)
    ]
    entry_rows.sort(
        key=lambda r: r.created_at,
        reverse=not chronological_order,
    )
    if scope is not None:
        # The skip rule and the compact form below read rows outside the
        # selection from the pass's read: the rows the skip rule walked
        # and the replies under the deleted ones and under the tweets
        # that may render compact must still be as read (#612).
        checked = set(selected_ids)
        tweets = {r.id for r in entry_rows
                  if r.origin == "twitter" and r.parent_id is None}
        scope.check(walked - checked, created_after, created_before,
                    replies_of=(expanded | tweets) - checked)

    if not entry_rows:
        return None

    entry_nodes_by_id = {
        n.id: n for n in Node.query.filter(
            Node.id.in_([r.id for r in entry_rows])
        ).all()
    }
    entry_nodes = [
        entry_nodes_by_id[r.id]
        for r in entry_rows
        if r.id in entry_nodes_by_id
    ]

    # Compact rendering for flat imported-tweet corpora (#276): the
    # Twitter importers create every tweet as its own root node, so each
    # one is a one-node "thread" whose per-thread scaffolding (Thread
    # header, Started line, node header, separators) costs several times
    # the tweet's own text in real model tokens (~4.8x the stored token
    # units on a 13k-tweet corpus; ~2.2x with this compact form). Runs of
    # consecutive such entries render as a `[YYYY-MM-DD HH:MM] text` list
    # under one section header carrying the shared metadata (author,
    # origin) instead of per-thread blocks. Threaded content keeps the
    # full rendering — a tweet that has in-scope children (someone
    # engaged with it in Loore) is threaded content, not a flat run.
    # Tweets never carry pinned artifacts or {quote:ID} placeholders, so
    # the compact line skips those lookups.
    compact_ids = {
        r.id for r in entry_rows
        if r.origin == "twitter" and r.parent_id is None
        and not children_by_parent.get(r.id)
    }

    # Build the export content using Markdown format
    export_lines = []
    export_lines.append("# Loore - Thread Export")
    export_lines.append("")
    export_lines.append(f"**User:** {user.username}")
    export_lines.append(
        f"**Export Date:** "
        f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    entry_points_line = len(export_lines)
    export_lines.append(f"**Entry Points:** {len(entry_nodes)}")
    if max_tokens:
        export_lines.append(f"*(Limited to ~{max_tokens:,} tokens)*")
    export_lines.append("")
    export_lines.append("---")
    export_lines.append("")

    # Artifact preambles. Mirrors the legacy path's behavior but driven
    # by entry_nodes instead of top_level_nodes.
    if collapse_artifacts:
        pass
    elif resolver is not None:
        preamble = resolver.get_artifacts_preamble()
        if preamble:
            _note_resolver_preamble(licence, resolver)
            export_lines.append(preamble)
            export_lines.append("---")
            export_lines.append("")

    prefetch_children(entry_nodes)

    compact_run = _CompactTweetRun(export_lines, user.username, licence)
    references = _QuoteReferences()
    thread_num = 0
    left_out = 0   # threads that are only a system prompt (omit_artifacts)
    for entry in iter_with_dek_prefetch(entry_nodes):
        if entry.id in compact_ids:
            compact_run.add(entry)
            continue
        compact_run.flush()

        thread_text = format_node_tree(
            entry,
            index_path=str(thread_num + 1),
            filter_ai_usage=filter_ai_usage,
            user_id=user.id,
            created_before=created_before,
            embedded_quotes=embedded_quotes,
            included_ids=render_included_ids,
            ai_blocked_ids=ai_blocked_ids,
            licence=licence,
            artifact_refs=not omit_artifacts,
            references=references,
            hold_others_markers=hold_others_markers,
            reference_titles=reference_titles,
        )
        if omit_artifacts and not thread_text:
            # Nothing of this thread in the window but its system
            # prompt, which this export leaves out (#550).
            left_out += 1
            continue
        thread_num += 1

        if entry.parent_id is not None:
            top_started = _entry_point_top_level_started(entry, user.id)
            export_lines.append(_format_preamble(top_started))
            export_lines.append("")

        export_lines.append(f"# Thread {thread_num}")
        export_lines.append(
            f"**Started:** "
            f"{entry.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        export_lines.append("")
        export_lines.append(thread_text)
        export_lines.append("---")
        export_lines.append("")

    compact_run.flush()

    if left_out:
        if left_out == len(entry_nodes):
            return None   # the window held nothing but system prompts
        export_lines[entry_points_line] = (
            f"**Entry Points:** {len(entry_nodes) - left_out}")

    export_lines.append("*End of Export*")
    # Quote references, once every entry has its label (#592); before
    # the text is measured.
    content = references.fill(
        "\n".join(export_lines), user.id, filter_ai_usage)

    if return_metadata:
        # Metadata must come from the budget-selected window, not the
        # full in-scope CTE set. Chunked profile generation uses
        # `latest_node_created_at` as the resume cursor for the next
        # chunk; reporting the newest node of the *unbudgeted* scope
        # made the cursor leap past everything the budget excluded, so
        # multi-chunk regens silently dropped all data after chunk 2's
        # window. Mirrors the legacy path, which measures selected_ids
        # ("not inflated by quote dependencies").
        if selected_ids is not None:
            selected_id_set = set(selected_ids)
            meta_rows = [r for r in cte_rows if r.id in selected_id_set]
        else:
            meta_rows = cte_rows
        timestamps = [r.created_at for r in meta_rows]
        latest_ts = max(timestamps) if timestamps else None
        earliest_ts = min(timestamps) if timestamps else None
        return {
            "content": content,
            "token_count": approximate_token_count(content),
            # Stored units (Node.token_count) of the window's own rows:
            # the figure the chunk planner balances on and the divisor
            # of the per-user tokens-per-unit ratio. `token_count` above
            # is the RENDERED chars/4 and includes scaffolding.
            "unit_count": sum((r.token_count or 0) for r in meta_rows),
            "latest_node_created_at": latest_ts,
            "earliest_node_created_at": earliest_ts,
            "node_count": len(meta_rows),
            "node_ids": {r.id for r in meta_rows},
            "origin_stats": _origin_stats(meta_rows),
        }

    return content


def _export_header_lines(user, total_threads, included_threads=None,
                         max_tokens=None):
    """The opening lines of a thread export (legacy path and the streamed
    download, #591)."""
    lines = [
        "# Loore - Thread Export",
        "",
        f"**User:** {user.username}",
        f"**Export Date:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"**Total Threads:** {total_threads}",
    ]
    if max_tokens and included_threads < total_threads:
        lines.append(f"**Included Threads (most recent):** {included_threads}")
        lines.append(f"*(Limited to ~{max_tokens:,} tokens)*")
    lines += ["", "---", ""]
    return lines


class _ArtifactVersions:
    """The context-artifact versions a full (unbudgeted) export prints in
    its preamble: every prompt, profile, todo list and artifact version
    pinned on an entry of the exported threads, the user's own only (see
    _artifact_ref_lines). Each reports to *licence* (#326) when it is
    added.

    add_node() takes the entries in any order, and an entry that pins
    nothing adds nothing: the streamed download (#591) passes only the
    entries that pin something, the legacy path every entry. A version's
    number and text are read when iter_lines() reaches it, so the
    download can send the preamble while it reads it."""

    def __init__(self, user, filter_ai_usage, licence=None):
        self.user = user
        self.filter_ai_usage = filter_ai_usage
        self.licence = licence
        self.prompts = {}
        self.profiles = {}
        self.todos = {}
        self.artifacts = {}

    def add_node(self, n):
        # Prompt artifacts. The entry's text is the prompt version's.
        prompt = n.get_artifact("prompt")
        if prompt is not None and prompt.id not in self.prompts:
            _note_entry(self.licence, n)
            self.prompts[prompt.id] = prompt
        # Pinned versions of the user's own only (see
        # _artifact_ref_lines): another author's node in these
        # threads pins theirs.
        # Profile artifacts
        profile = n.get_artifact("profile")
        if (profile is not None and profile.user_id == self.user.id
                and profile.id not in self.profiles):
            if self.licence is not None:
                self.licence.note_usage(profile.ai_usage,
                                        f"archive profile {profile.id}",
                                        row=profile)
            self.profiles[profile.id] = profile
        # Todo artifacts
        todo = n.get_artifact("todo")
        if (todo is not None and todo.user_id == self.user.id
                and todo.id not in self.todos):
            if self.licence is not None:
                self.licence.note_usage(todo.ai_usage,
                                        f"archive todo list {todo.id}",
                                        row=todo)
            self.todos[todo.id] = todo
        # User artifacts (memory, scratchpad, predictions,
        # ai_preferences, custom):
        # one entry per referenced version, full content.
        for kind, artifact in sorted(n.get_user_artifacts().items()):
            if (artifact.id in self.artifacts
                    or artifact.user_id != self.user.id):
                continue
            if self.licence is not None:
                self.licence.note_usage(
                    artifact.ai_usage,
                    f"archive {kind} artifact {artifact.id}",
                    row=artifact)
            self.artifacts[artifact.id] = artifact

    def rows(self):
        """Every version the preamble prints, in the order it prints
        them."""
        return [group[key] for group in (
            self.prompts, self.profiles, self.todos, self.artifacts)
            for key in sorted(group)]

    def iter_lines(self, before_row=None):
        """The preamble's lines, none when nothing is pinned.
        *before_row*, when given, is called with each version before its
        text is read (the download unwraps data keys there, #591)."""
        from backend.utils.profile_versions import not_set_internal

        def text(row, read):
            if before_row is not None:
                before_row(row)
            return read()

        if self.prompts:
            yield "## System Prompts Referenced\n"
            sorted_pids = sorted(self.prompts)
            for i, pid in enumerate(sorted_pids):
                prompt = self.prompts[pid]
                vnum = UserPrompt.query.filter(
                    UserPrompt.user_id == prompt.user_id,
                    UserPrompt.prompt_key == prompt.prompt_key,
                    UserPrompt.created_at <= prompt.created_at,
                ).count()
                yield f"### {prompt.title} v{vnum} (ref #{pid})\n"
                yield text(prompt, prompt.get_content)
                yield ""
                if i < len(sorted_pids) - 1:
                    yield "===\n"
        if self.profiles:
            if self.prompts:
                yield "===\n"
            yield "## User Profiles Referenced\n"
            sorted_pids = sorted(self.profiles)
            for i, pid in enumerate(sorted_pids):
                profile = self.profiles[pid]
                pver = UserProfile.query.filter(
                    UserProfile.user_id == profile.user_id,
                    UserProfile.created_at <= profile.created_at,
                    not_set_internal(),
                ).count()
                yield f"### User Profile v{pver} (ref #{pid})\n"
                yield text(profile, lambda: _preamble_content(
                    profile, self.filter_ai_usage))
                yield ""
                if i < len(sorted_pids) - 1:
                    yield "===\n"
        if self.todos:
            if self.prompts or self.profiles:
                yield "===\n"
            yield "## User TODOs Referenced\n"
            sorted_tids = sorted(self.todos)
            for i, tid in enumerate(sorted_tids):
                todo = self.todos[tid]
                tver = UserTodo.query.filter(
                    UserTodo.user_id == todo.user_id,
                    UserTodo.created_at <= todo.created_at,
                ).count()
                yield f"### User TODO v{tver} (ref #{tid})\n"
                yield text(todo, lambda: _preamble_content(
                    todo, self.filter_ai_usage))
                yield ""
                if i < len(sorted_tids) - 1:
                    yield "===\n"
        if self.artifacts:
            if self.prompts or self.profiles or self.todos:
                yield "===\n"
            yield "## Artifacts Referenced\n"
            sorted_artids = sorted(self.artifacts)
            for i, artid in enumerate(sorted_artids):
                artifact = self.artifacts[artid]
                # Live versions only (#603), as get_user_artifacts.
                art_ver = UserArtifact.live().filter(
                    UserArtifact.user_id == artifact.user_id,
                    UserArtifact.kind == artifact.kind,
                    UserArtifact.created_at <= artifact.created_at,
                ).count()
                yield (f"### {artifact.title} ('{artifact.kind}') "
                       f"v{art_ver} (ref #{artid})\n")
                yield text(artifact, lambda: _preamble_content(
                    artifact, self.filter_ai_usage))
                yield ""
                if i < len(sorted_artids) - 1:
                    yield "===\n"
        if self.prompts or self.profiles or self.todos or self.artifacts:
            yield "==="
            yield ""


def build_user_export_content(
    user, max_tokens=None, *, filter_ai_usage,
    created_before=None, created_after=None,
    chronological_order=False, return_metadata=False,
    collapse_artifacts=False,
    include_strategy="authored_threads",
    licence=None,
    omit_artifacts=False,
    whole_window=False,
    scope=None,
    hold_others_markers=False,
    reference_titles=None,
):
    """
    Core export logic: Build a human-readable text export of threads for a user.

    Two scope topologies, selected by `include_strategy`:
      - **`authored_threads`** (default): includes top-level threads owned by
        the user (`Node.user_id == user.id`, `parent_id IS NULL`) and walks
        their accessible descendants top-down via `format_node_tree`. The
        user does not need to author every node — replies from others on
        their threads are included if accessible. Does NOT include foreign
        threads the user replied to. Matches the user-facing `/export/threads`
        data dump. With `max_tokens`, a thread whose start is older than
        the window shows its newest part, under a continuation line, and
        a deleted node shows (and counts) as a placeholder line only
        where it holds the place of an entry below it (#525).
      - **`engaged_threads`**: anchored on every node where the user is author
        or `human_owner_id`. From each anchor, climb up through accessible
        parents to the root or first inaccessible node, and climb down
        through all accessible descendants. For user-rooted subtrees this is
        a superset of `authored_threads` (climb-down from the owned root
        picks up every accessible descendant). For foreign-rooted threads it
        includes only the climb-up + climb-down path through user replies —
        foreign sibling branches off the foreign root that the user did not
        engage with are excluded. Used by `{user_export}` placeholder and
        `recent_context` summaries.

    NOTE: `include_strategy` is only consulted when `created_after` is None.
    If `created_after` is set, the function always routes to the anchor-based
    incremental path regardless of `include_strategy` (the time-floor
    semantics imply the anchor topology). Preserves backwards compat with
    existing recent_context callers.

    When `max_tokens` is specified, uses `ExportQuoteResolver` for smart
    quote resolution. The resolver may pull in pre-cutoff quoted nodes for
    embedding; those never become entry points (entry-point membership is
    decided from the CTE rows, not from the resolver's mutated set).

    Args:
        user: User object to export threads for
        max_tokens: Optional maximum token count. If provided, only includes
                   most recent threads that fit within this limit, and uses
                   smart quote resolution.
        filter_ai_usage: Required, keyword-only. True leaves out every
                        node, quote and artifact row whose ai_usage is
                        not 'chat'/'train'; any export that reaches a
                        model must pass True. False is only for the
                        user's own download of their data
                        (/export/threads, export_user_threads), which
                        includes rows marked 'none'. No default, so a
                        new caller has to choose.
        created_before: Optional datetime. If provided, only includes
                       nodes created before this timestamp.
        created_after: Optional datetime. If provided, forces the anchor-based
                      incremental path with a time floor on anchors,
                      regardless of `include_strategy`.
        chronological_order: If True and max_tokens is set, select oldest
                           nodes first (for iterative profile building).
        return_metadata: If True, return a dict with `content`,
                        `token_count` (rendered chars/4), `unit_count`
                        (stored Node.token_count sum of the included
                        rows), `latest_node_created_at`,
                        `earliest_node_created_at`, `node_count`, and
                        `node_ids` (set of included node IDs; when
                        max_tokens is set this reflects the
                        budget-selected window, so chunked callers can
                        use `latest_node_created_at` as a resume
                        cursor).
        collapse_artifacts: If True, suppress full artifact preambles
                           (artifact ID refs are still inlined), and the
                           max_tokens budget counts only the entries, as
                           nothing else is emitted.
        include_strategy: Scope topology when `created_after` is None.
                         "authored_threads" (default) or "engaged_threads".
                         Any other value raises ValueError. See module
                         docstring above for details.
        licence: Optional PayloadLicence (#326) for an export going to a
                 model. Everything the export renders reports to it — each
                 entry (the user's own and other people's replies) and
                 each preamble row by its own ai_usage, quoted saved
                 references as other people's writing — so one 'chat'
                 entry keeps the payload off the training key.
        omit_artifacts: If True, the export carries no context artifact
                       at all: no preamble, no reference lines, and no
                       system-prompt node (its replies still render; a
                       thread that is only a system prompt is left out).
                       Implies collapse_artifacts. For the jobs that read
                       the user's writing: profile builds, intentions and
                       poll drafts (#550).
        whole_window: If True (with max_tokens), the text holds every row
                     the budget window selects, each once as an entry, in
                     order: the quote resolver embeds quotes but drops no
                     selected row, and a node it embeds renders only
                     inside its quote. The text can then run past the
                     budget by its quote embeds and by a run of rows
                     sharing the window's last timestamp, which the
                     selection takes whole. For a caller that resumes
                     after the window's `latest_node_created_at` (profile
                     builds), where a row left out of the text is never
                     read, or records it as covered (recent context's
                     source_data_cutoff) (#552). Needs the incremental
                     path (`created_after` or `engaged_threads`).
        scope: Optional WindowScope of this user and filter_ai_usage (a
               profile build's windows, #612): the rows in scope come
               from its one read of the metadata instead of a read per
               call, the same rows. Needs max_tokens and the
               incremental path; the selected rows are checked against
               the database first (ScopeChanged when one changed).
        hold_others_markers: If True, the {quote:ID} / {quote_ext:ID}
               markers left as written in text the user did not write
               come back held (quotes.hold_quotes), for an AI reply's
               prompt, which runs its own quote passes over the text
               and then releases them (quotes.release_quotes).
        reference_titles: a quotes.ReferenceLines: a saved reference
               quoted in the user's own text is one line naming it (its
               title, or its author; its source; with ``named`` the name
               read_full opens it by, collected in ``ids``), not its
               text (quotes.reference_title_lines). Only for the recent
               raw window of a call with tools (Peter, voice review
               2026-11-12); None, for the data download, {user_export}
               and the background jobs, keeps the text.

    Returns:
        str or dict: Formatted export content (or None if no threads found).
                    If return_metadata=True, returns the dict described above.
    """
    if include_strategy not in ("authored_threads", "engaged_threads"):
        raise ValueError(
            f"include_strategy must be 'authored_threads' or "
            f"'engaged_threads', got {include_strategy!r}"
        )
    if omit_artifacts:
        collapse_artifacts = True

    if created_after is not None or include_strategy == "engaged_threads":
        return _build_user_export_incremental(
            user, max_tokens=max_tokens, filter_ai_usage=filter_ai_usage,
            created_before=created_before, created_after=created_after,
            chronological_order=chronological_order,
            return_metadata=return_metadata,
            collapse_artifacts=collapse_artifacts,
            licence=licence,
            omit_artifacts=omit_artifacts,
            whole_window=whole_window,
            scope=scope,
            hold_others_markers=hold_others_markers,
            reference_titles=reference_titles,
        )
    if whole_window:
        raise ValueError(
            "whole_window needs the incremental export (created_after or "
            "include_strategy='engaged_threads')")
    if scope is not None:
        raise ValueError(
            "a window scope needs the incremental export (created_after "
            "or include_strategy='engaged_threads')")

    # Legacy path: top-level threads owned by the user.
    query = Node.query.filter_by(
        user_id=user.id,
        parent_id=None
    )

    # Only filter by AI usage if requested (for AI profile generation)
    if filter_ai_usage:
        query = query.filter(Node.ai_usage.in_(AI_ALLOWED),
                             *withheld_prefill_clauses(user.id))

    # Filter by creation timestamp if specified (for {user_export} context
    # limiting). `created_after` is intercepted by the dispatcher above and
    # is always None on the legacy path; we leave the parameter in the
    # signature for backward compatibility with callers that pass it.
    if created_before:
        query = query.filter(Node.created_at < created_before)

    # Thread count for the header — a COUNT, never a load.
    total_threads = query.count()
    if not total_threads:
        return None

    # Variables for smart quote resolution (used when max_tokens is specified)
    embedded_quotes = None
    included_ids = None
    ai_blocked_ids = None
    resolver = None
    selected_ids = None
    budget = None

    # The budgeted window's thread blocks (#525): which blocks continue
    # a thread that started before the window, the rows they show, and
    # the deleted rows whose placeholder holds an entry's place.
    continued_from = {}
    shown_ids = None
    connecting = set()

    if max_tokens:
        # Budgeted callers ({user_recent_raw} every reply, {user_export}
        # windows) must never materialise the whole corpus: pre-select the
        # window in SQL FIRST and load only those rows. Loading every
        # top-level node and running the per-thread alive check on each
        # (61k rows, 61k queries) OOM-killed the 512 MB worker on a large
        # imported archive before the 10k-token budget was even consulted.
        header_footer_tokens = 100
        budget = max_tokens - header_footer_tokens
        selected_ids = _preselect_node_ids(
            user.id, budget, filter_ai_usage=filter_ai_usage,
            created_before=created_before, created_after=created_after,
            chronological_order=chronological_order,
        )
        if not selected_ids:
            return None

        # Load selected Node objects with context_artifacts eager-loaded
        selected_nodes = (
            Node.query
            .filter(Node.id.in_(selected_ids))
            .options(subqueryload(Node.context_artifacts))
            .all()
        )
        # Sort in Python to match the desired order. The id settles a
        # tie, so two renders of one window are the same bytes.
        selected_nodes.sort(
            key=lambda n: (n.created_at, n.id),
            reverse=not chronological_order,
        )
        # The resolver loop below decrypts every selected node, one KMS
        # unwrap each on a cold worker (~80 ms; a 5k-node profile chunk
        # waited ~7 minutes here, then again at render time once the
        # 4k-entry DEK cache had cycled). One concurrent batch up front;
        # the render loop's windowed prefetch then finds them cached.
        # Deleted rows are never decrypted (#525).
        prefetch_deks(n.content for n in selected_nodes
                      if n.deleted_at is None)

        # Where each row renders, also in a thread whose start is older
        # than the window, and which deleted rows hold an entry's place
        # (#525).
        places = _window_places(selected_nodes, user.id, filter_ai_usage)
        connecting = _connecting_tombstones(selected_nodes, places)

        # Create resolver with adjusted token budget. A collapsed export
        # emits no preamble, so the budget counts only the entries it
        # does emit (#518).
        resolver = ExportQuoteResolver(
            user.id, budget,
            filter_ai_usage=filter_ai_usage,
            chronological=chronological_order,
            with_preamble=not collapse_artifacts,
            inside=quotes_inside(user.id),
        )

        # Add only the pre-selected nodes to the resolver (decrypts here)
        for node in selected_nodes:
            if node.deleted_at is not None:
                # Its text is never read. Shown as its placeholder line
                # only where it holds an entry's place.
                resolver.add_tombstone(
                    node.id, node.created_at,
                    TOMBSTONE_UNITS if node.id in connecting else 0)
                continue
            if omit_artifacts:
                _add_artifact_free_entry(resolver, node)
                continue
            content = node.get_content()
            # Collect artifact tuples from join table
            node_artifacts = [
                (a.artifact_type, a.artifact_id)
                for a in node.context_artifacts
            ]
            prompt = node.get_artifact("prompt")
            if prompt is not None:
                version_num = UserPrompt.query.filter(
                    UserPrompt.user_id == prompt.user_id,
                    UserPrompt.prompt_key == prompt.prompt_key,
                    UserPrompt.created_at <= prompt.created_at,
                ).count()
                resolver.add_node(
                    node_id=node.id,
                    created_at=node.created_at,
                    content=content,
                    token_count=node.token_count,
                    user_prompt_id=prompt.id,
                    prompt_content=content,
                    prompt_label=f"{prompt.title} v{version_num}",
                    artifacts=node_artifacts,
                )
            else:
                resolver.add_node(
                    node_id=node.id,
                    created_at=node.created_at,
                    content=content,
                    token_count=node.token_count,
                    artifacts=node_artifacts,
                )

        # Run the resolution algorithm
        resolver.resolve()

        # Get the results
        included_ids, embedded_quotes, ai_blocked_ids = resolver.get_resolution_result()

        # Every row in the window renders, also in a thread whose start
        # is older than the window (#525), oldest block first.
        top_level_nodes, continued_from, shown_ids = _window_blocks(
            [n for n in selected_nodes if n.id in included_ids], places)
    else:
        # §5a per-thread skip: drop fully-deleted threads so the export
        # doesn't show a chain of `[Node deleted by author]` placeholders
        # for content the user explicitly removed. Mixed threads (some
        # alive, some deleted) still show through with tombstones rendered
        # by format_node_tree's tombstone shell. All threads, in
        # chronological order. A thread with a live root is kept; the
        # others are checked in one query per batch (#591).
        roots = list(reversed(query.order_by(Node.created_at.desc()).all()))
        alive = _threads_with_alive_node(
            n.id for n in roots if n.deleted_at is not None)
        top_level_nodes = [n for n in roots
                           if n.deleted_at is None or n.id in alive]

    if not top_level_nodes:
        return None

    # Build the export content using Markdown format
    export_lines = _export_header_lines(
        user, total_threads, included_threads=len(top_level_nodes),
        max_tokens=max_tokens)

    # Emit artifact preambles (prompts, profiles, todos) if any are referenced
    if collapse_artifacts:
        pass  # Skip full artifact content; inline refs are still emitted
    elif resolver is not None:
        preamble = resolver.get_artifacts_preamble()
        if preamble:
            _note_resolver_preamble(licence, resolver)
            export_lines.append(preamble)
            export_lines.append("---")
            export_lines.append("")
    elif not max_tokens:
        # Full export: collect all unique artifact versions
        versions = _ArtifactVersions(user, filter_ai_usage, licence)
        for top_node in top_level_nodes:
            for n in _collect_all_nodes_in_tree(
                    top_node, filter_ai_usage, created_before,
                    user_id=user.id):
                versions.add_node(n)
        export_lines.extend(versions.iter_lines())

    # Process each thread. Flat imported tweets render as compact runs
    # (#276), same as the incremental path; threaded content keeps the
    # full per-thread rendering.
    prefetch_children(top_level_nodes)
    compact_run = _CompactTweetRun(export_lines, user.username, licence)
    references = _QuoteReferences()
    thread_num = 0
    rendered_any = False
    for node in iter_with_dek_prefetch(top_level_nodes):
        if _is_flat_tweet_root(node, filter_ai_usage, created_before):
            compact_run.add(node)
            rendered_any = True
            continue
        compact_run.flush()

        # Format the entire thread tree (depth-first traversal)
        thread_text = format_node_tree(
            node,
            index_path=str(thread_num + 1),
            filter_ai_usage=filter_ai_usage,
            user_id=user.id,
            created_before=created_before,
            embedded_quotes=embedded_quotes,
            included_ids=included_ids,
            ai_blocked_ids=ai_blocked_ids,
            licence=licence,
            artifact_refs=not omit_artifacts,
            # A budgeted window shows no placeholder without an entry
            # below it (#525).
            prune_tombstones=bool(max_tokens),
            references=references,
            hold_others_markers=hold_others_markers,
            reference_titles=reference_titles,
        )
        if not thread_text:
            # Only a system prompt (#550), or nothing alive (#525).
            continue
        rendered_any = True
        thread_num += 1
        if node.id in continued_from:
            # The thread started before the window (#525).
            export_lines.append(_format_preamble(continued_from[node.id]))
            export_lines.append("")
        export_lines.append(f"# Thread {thread_num}")
        export_lines.append(f"**Started:** {node.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        export_lines.append("")
        export_lines.append(thread_text)

        export_lines.append("---")
        export_lines.append("")

    compact_run.flush()
    if not rendered_any:
        return None   # nothing but system prompts (omit_artifacts)

    # Add footer
    export_lines.append("*End of Export*")

    # Quote references, once every entry has its label (#592); before
    # the text is measured.
    content = references.fill(
        "\n".join(export_lines), user.id, filter_ai_usage)

    if return_metadata:
        if shown_ids is not None:
            # The rows the window shows (#525): not the pre-selection,
            # whose oldest row the resolver can leave out, nor the quote
            # dependencies the resolver may have pulled in, nor deleted
            # rows it shows no placeholder for.
            meta_nodes = [n for n in selected_nodes if n.id in shown_ids
                          and (n.deleted_at is None or n.id in connecting)]
        else:
            # No max_tokens path — scan all included nodes
            meta_nodes = []
            for top_node in top_level_nodes:
                meta_nodes.extend(_collect_all_nodes_in_tree(
                    top_node, filter_ai_usage, created_before,
                    user_id=user.id,
                ))
        latest_ts = max(
            (n.created_at for n in meta_nodes), default=None
        )
        earliest_ts = min(
            (n.created_at for n in meta_nodes), default=None
        )
        node_count = len(meta_nodes)
        node_ids = {n.id for n in meta_nodes}
        meta_rows = meta_nodes
        return {
            "content": content,
            "token_count": approximate_token_count(content),
            "latest_node_created_at": latest_ts,
            "earliest_node_created_at": earliest_ts,
            "node_count": node_count,
            "node_ids": node_ids,
            "origin_stats": _origin_stats(meta_rows),
        }

    return content


# ── The user's data download, sent while it is built (#591) ──────────
#
# The download built the whole file in memory before its first byte: a
# 42k-entry account took two minutes and ~360 MB, a large ChatGPT import
# ~700 MB, and the proxy in front of the app (and the web client) gave
# up after 60 s without a byte. iter_user_export sends the same text a
# few threads at a time instead. Every wait between two pieces stays far
# below that limit: the first piece (the header) goes out after a few
# metadata queries; the preamble's artifact versions come from the pins
# found by a level-by-level walk of the threads instead of a render of
# every entry; the deleted-thread check walks only threads with a
# deleted root; data keys are unwrapped a window of entries at a time,
# also inside one very large thread; and a thread is sent piece by piece
# as it renders.
#
# The response's headers go out before any of that work
# (_send_user_export), and they say "nosniff": a browser sniffs a
# text/plain response's first 1 KiB before it starts a download (Chrome
# waits for those bytes, measured 2026-11-11), so without it the save
# dialog waited until the export had sent 1 KiB, ~30 s into a large
# account's export.

# What the download holds when the user has no thread to export.
NO_THREADS_TEXT = "No threads found to export."

# Threads the download loads at once, with every entry below them
# (#591): it holds one batch in memory, never the account. 50 threads of
# a large ChatGPT import peaked at ~110 MB, 100 at ~130 MB, 25 at ~80 MB
# (local measurement, 2026-11-10). INTRODUCED CONSTANT.
EXPORT_STREAM_BATCH = 50

# Entries whose data keys the download unwraps in one concurrent batch
# when it reaches an entry whose key is not cached (#591). Each entry
# written in Loore has its own key, so a cold export makes one KMS call
# per entry (~80 ms, 8 in flight): 400 entries are about 4 s of waiting,
# far below the 60 s the proxy allows between two reads, also inside one
# very large thread. Imports share a key per chunk, so they cost a few
# calls. INTRODUCED CONSTANT.
EXPORT_DEK_WINDOW = 400

# The download sends what it has gathered once it holds this many
# characters, or once this many seconds passed since it last sent
# something (#591). INTRODUCED CONSTANTS.
EXPORT_FLUSH_CHARS = 64 * 1024
EXPORT_FLUSH_SECONDS = 2.0

# Under the gevent worker the download shares its worker with other
# requests, and it gives them a turn: once this many seconds of its own
# work have passed, it sleeps EXPORT_PAUSE_SECONDS (#591). The sleep must
# be longer than 0: a 0 s sleep is a queued loop callback, and the loop
# polls sockets and timers only every 50 callbacks, so another request on
# the worker waited up to 1.7 s (4 s in a second measurement) during an
# export; with these values it waited 44 ms in the median, 56 ms at
# most, and the export took no longer (local measurement, 2026-11-10).
# While the export waits for the key service, the loop polls anyway.
# The database queries block the worker while they run (the driver is
# not made cooperative); each is one short statement.
# INTRODUCED CONSTANTS.
EXPORT_PAUSE_EVERY_SECONDS = 0.05
EXPORT_PAUSE_SECONDS = 0.001


class _Pieces:
    """Text of the download gathered until it is worth sending. Adding a
    piece also gives the worker's other requests a turn when one is due
    (EXPORT_PAUSE_EVERY_SECONDS)."""

    def __init__(self):
        self.parts = []
        self.size = 0
        self.sent_at = self.paused_at = time.monotonic()

    def add(self, text):
        self.parts.append(text)
        self.size += len(text)
        self.turn()

    def turn(self):
        """Sleep EXPORT_PAUSE_SECONDS if EXPORT_PAUSE_EVERY_SECONDS passed
        since the last pause: under the gevent worker, the worker's other
        requests run meanwhile."""
        if time.monotonic() - self.paused_at >= EXPORT_PAUSE_EVERY_SECONDS:
            time.sleep(EXPORT_PAUSE_SECONDS)
            self.paused_at = time.monotonic()

    def due(self):
        return bool(self.parts) and (
            self.size >= EXPORT_FLUSH_CHARS
            or time.monotonic() - self.sent_at >= EXPORT_FLUSH_SECONDS)

    def take(self):
        text = "".join(self.parts)
        self.parts, self.size = [], 0
        self.sent_at = time.monotonic()
        return text


def _export_roots(user_id):
    """The user's threads in the full export's order (the legacy path's
    query: oldest first, equal start times in the database's order), as
    rows of id, created_at, origin, deleted_at and has_reply. Metadata
    only: no entry is loaded or decrypted."""
    child = db.aliased(Node, flat=True)
    has_reply = exists().where(child.parent_id == Node.id)
    rows = (
        db.session.query(Node.id, Node.created_at, Node.origin,
                         Node.deleted_at, has_reply.label("has_reply"))
        .filter(Node.user_id == user_id, Node.parent_id.is_(None))
        .order_by(Node.created_at.desc())
        .all()
    )
    rows.reverse()
    return rows


def _thread_entry_ids(roots):
    """The ids of every entry of the threads *roots* (rows of
    _export_roots), the roots included. Walked level by level from the
    roots that have a reply, IDS_PER_QUERY parents per query: about one
    query per level of the deepest thread, each a seek on the parent_id
    index, so the cost follows the size of the threads, not of the
    table."""
    ids = [r.id for r in roots]
    level = [r.id for r in roots if r.has_reply]
    while level:
        next_level = []
        for start in range(0, len(level), IDS_PER_QUERY):
            next_level.extend(node_id for (node_id,) in db.session.query(
                Node.id).filter(
                Node.parent_id.in_(level[start:start + IDS_PER_QUERY])))
        ids.extend(next_level)
        level = next_level
    return ids


def _pinning_entries(user_id, roots):
    """The entries of the threads *roots* (rows of _export_roots: the
    threads the download shows) that pin a context artifact and that the
    user can access: the only ones that add to the full export's preamble
    (_ArtifactVersions, which the legacy path feeds every entry
    _collect_all_nodes_in_tree returns), their pins loaded with them.

    This was one recursive query over the user's threads (#591), which
    Postgres planned as a full scan and sort of ``node`` for every level
    of the deepest thread: 68 s on a 1M-row table with a 130-level
    thread (local measurement, 2026-11-11), the wait before the
    download's second piece. The threads are walked level by level
    instead (_thread_entry_ids)."""
    ids = _thread_entry_ids(roots)
    pinning = set()
    for start in range(0, len(ids), IDS_PER_QUERY):
        pinning.update(node_id for (node_id,) in db.session.query(
            NodeContextArtifact.node_id).filter(
            NodeContextArtifact.node_id.in_(
                ids[start:start + IDS_PER_QUERY])).distinct())
    pinning = sorted(pinning)
    entries = []
    for start in range(0, len(pinning), IDS_PER_QUERY):
        entries.extend(Node.query.options(
            selectinload(Node.context_artifacts)).filter(
            Node.id.in_(pinning[start:start + IDS_PER_QUERY])))
    return [n for n in entries if can_user_access_node(n, user_id)]


def _render_order(roots, user_id):
    """The entries below *roots* (their children loaded) whose text the
    export renders, in the order it renders them: depth first, replies
    oldest first. Leaves out deleted entries and those the user cannot
    access, whose text is never read."""
    order, seen = [], set()
    for root in roots:
        stack = [root]
        while stack:
            n = stack.pop()
            if n.id in seen:
                continue
            seen.add(n.id)
            if can_user_access_node(n, user_id):
                order.append(n)
            # Replies in the render's order: oldest first, the id
            # settling a tie (_filtered_children, #592).
            stack.extend(reversed(sorted(
                n.children, key=attrgetter("created_at", "id"))))
    return order


class _DekLookahead:
    """The download's hook before it reads a row's text (#591): at a row
    (an entry, or an artifact version of the preamble) whose data key is
    not cached, unwrap the keys of the next EXPORT_DEK_WINDOW rows of
    *order*, the order the download reads them in, in one concurrent
    batch. The rows are then decrypted from the cache, and no wait is
    longer than one window's, also inside a thread larger than the
    window (iter_with_dek_prefetch unwraps a whole thread at once)."""

    def __init__(self, order):
        self.order = order
        self.window = EXPORT_DEK_WINDOW
        self.position = {(type(r), r.id): i for i, r in enumerate(order)}

    def __call__(self, row):
        if dek_cached(row.content):
            return
        i = self.position.get((type(row), row.id))
        ahead = [row] if i is None else self.order[i:i + self.window]
        prefetch_deks(r.content for r in ahead)


def iter_user_export(user):
    """The user's own data download (/export/threads), piece by piece,
    built while it is sent (#591). The pieces join to the text of
    ``build_user_export_content(user, filter_ai_usage=False)``, or to
    NO_THREADS_TEXT where that returns None.

    A piece is yielded once it is due (_Pieces). Every
    EXPORT_PAUSE_EVERY_SECONDS of work the generator sleeps briefly
    (_Pieces.turn): under the gevent worker that lets the worker's other
    requests run during the export, which they could not while one
    request built a whole archive."""
    roots = _export_roots(user.id)
    alive = _threads_with_alive_node(
        r.id for r in roots if r.deleted_at is not None)
    kept = [r for r in roots if r.deleted_at is None or r.id in alive]
    if not kept:
        yield NO_THREADS_TEXT
        return

    out = _Pieces()
    for line in _export_header_lines(user, len(roots)):
        out.add(line + "\n")
    yield out.take()

    versions = _ArtifactVersions(user, filter_ai_usage=False)
    for n in _pinning_entries(user.id, kept):
        versions.add_node(n)
        out.turn()
    for line in versions.iter_lines(
            before_row=_DekLookahead(versions.rows())):
        out.add(line + "\n")
        if out.due():
            yield out.take()
    del versions

    # Flat imported tweets render as compact runs (#276); a run's heading
    # counts its tweets, so the runs are measured on the metadata first.
    flat = [r.origin == "twitter" and r.deleted_at is None
            and not r.has_reply for r in kept]
    thread_num = 0
    rendered_any = False
    for start in range(0, len(kept), EXPORT_STREAM_BATCH):
        batch = kept[start:start + EXPORT_STREAM_BATCH]
        loaded = {n.id: n for n in Node.query.filter(
            Node.id.in_([r.id for r in batch]))}
        nodes = [loaded[r.id] for r in batch if r.id in loaded]
        prefetch_children(nodes)
        out.turn()
        lookahead = _DekLookahead(_render_order(nodes, user.id))
        for i, row in enumerate(batch, start):
            node = loaded.get(row.id)
            if flat[i]:
                if i == 0 or not flat[i - 1]:
                    run = 1
                    while i + run < len(kept) and flat[i + run]:
                        run += 1
                    out.add(_tweet_run_heading(user.username, run) + "\n\n")
                if node is not None:
                    lookahead(node)
                    out.add(_tweet_line(node) + "\n\n")
                    rendered_any = True
                if i + 1 == len(kept) or not flat[i + 1]:
                    out.add("---\n\n")
            elif node is not None:
                pieces = iter_node_tree(
                    node, index_path=str(thread_num + 1),
                    filter_ai_usage=False, user_id=user.id,
                    before_entry=lookahead)
                first = next((p for p in pieces if p), None)
                if first is not None:
                    rendered_any = True
                    thread_num += 1
                    started = node.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')
                    out.add(f"# Thread {thread_num}\n"
                            f"**Started:** {started}\n\n")
                    out.add(first)
                    for piece in pieces:
                        out.add(piece)
                        if out.due():
                            yield out.take()
                    out.add("\n---\n\n")
            if out.due():
                yield out.take()
    if not rendered_any:
        # Every kept thread starts with an entry of the user's own, so
        # one always renders; the legacy path would return None here,
        # after the header went out. Fail loudly rather than send a
        # different file.
        raise RuntimeError(f"export of user {user.id}: no thread rendered")
    out.add("*End of Export*")
    yield out.take()


def _send_user_export(user):
    """iter_user_export after an empty piece, logging a failure before
    it ends the response.

    The empty piece comes before any query: the app server sends the
    response's headers on it (it writes no body chunk for it), so the
    browser starts the download and shows its save dialog at once,
    whatever the account's size. The first piece of the file follows
    when iter_user_export has it.

    So a failure cannot become an error status: the exception
    propagates, the app server closes the connection without the
    response's last chunk, and the browser (or the iPhone app) marks the
    download failed. The file never ends as if it were complete."""
    yield ""
    sent = 0
    try:
        for piece in iter_user_export(user):
            sent += len(piece)
            yield piece
    except Exception:
        current_app.logger.exception(
            "Data download of user %s failed after %d characters; "
            "the connection is cut so the download shows as failed",
            user.id, sent)
        raise


@export_bp.route("/export/threads", methods=["GET"])
@login_required
def export_threads():
    """
    Export all threads originated by the current user in a human-readable text format.

    This includes:
    - All top-level nodes created by the user
    - All descendants of those nodes (including AI replies)
    - Properly formatted with hierarchical structure showing branches

    The file is sent while it is built (#591), so the download starts at
    once whatever the account's size, and no copy is stored on the
    server. A HEAD request (the web app's check before it starts the
    download) builds nothing.
    """
    # The user's own download: rows marked 'none' are included.
    user = current_user._get_current_object()
    filename = f"loore-export-{datetime.utcnow().strftime('%Y-%m-%d')}.txt"
    return Response(
        stream_with_context(_send_user_export(user)),
        mimetype="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            # Pass each piece on as it comes (see sse.py).
            "X-Accel-Buffering": "no",
            # Start the download on the headers: without it a browser
            # waits for the first 1 KiB of a text/plain body to sniff its
            # type before it shows the save dialog.
            "X-Content-Type-Options": "nosniff",
        },
    )


def _latest_profile_snapshot(user):
    """id / created_at of the newest saved version. The client detects a
    chunk landing (and the end of a batch chain) by the id changing;
    the content is fetched from /dashboard when it needs it. A window
    set's intermediates are not versions (#545): the id changes when the
    set's final merge is saved."""
    from backend.utils.profile_versions import current_profile_query
    latest = current_profile_query(user.id).first()
    if not latest:
        return None
    return {
        "id": latest.id,
        "generation_type": latest.generation_type,
        "created_at": iso_utc(latest.created_at),
    }


_SYNC_STATUS_MAP = {
    'PENDING': 'pending',
    # Waiting for the heavy-job slot (#613, utils/heavy_slot): queued again.
    'RETRY': 'pending',
    'STARTED': 'processing',
    'PROGRESS': 'progress',
    'SUCCESS': 'completed',
    'FAILURE': 'failed',
    'REVOKED': 'failed',
}

# A failed profile build's error, unless the failure was written for the
# user (utils/task_owner.failure_text).
PROFILE_FAILED_TEXT = "The profile update failed."


def _pending_batch_item(user_id):
    """The user's in-flight profile step, from the item meta of the
    pending ProfileBatchJob rows (a handful at any time; items are keyed
    by custom_id, so scanned in Python). None when the flag is set but no
    job carries the user — a submit that failed after the flag was set,
    or a job cancelled by an admin."""
    from backend.models import ProfileBatchJob
    jobs = (ProfileBatchJob.query.filter_by(status="pending")
            .order_by(ProfileBatchJob.id.desc()).all())
    for job in jobs:
        for item in job.items or []:
            if (item.get("user_id") == user_id
                    and item.get("kind") in ("chunk", "integration",
                                             "merge", "window", "group",
                                             "final")):
                return item
    return None


def _batch_progress(user):
    """Progress of the user's in-flight Batch API step (#258).

    The batch pipeline (backend/tasks/profile_batch.py) sets no Celery
    task id; the in-flight step is read from the pending job's item meta,
    where the request builder recorded its kind and, for a chunk, its
    ordinal and the planned total. Nothing is re-planned here: the plan
    walks the user's whole in-scope corpus (about a second and tens of
    MB for a 60k-node account), and this runs on every poll.
    """
    item = _pending_batch_item(user.id)
    if item is None:
        message, progress = "Generating profile", 0
    elif item.get("kind") == "integration":
        message, progress = "Integrating profile versions", 95
    elif item.get("kind") == "merge":
        # A rebuild merging the user's own version in (#183).
        message, progress = "Merging your own version", 85
    elif item.get("kind") == "window":
        # A large build reading every window at once (#545); a small one
        # reads one window, then rewrites it as the profile.
        total = item.get("total")
        message = (f"Generating profile: reading all {total} chunks"
                   if total and total > 1 else "Generating profile")
        progress = 20
    elif item.get("kind") == "group":
        message, progress = "Integrating profile versions", 80
    elif item.get("kind") == "final":
        message = ("Generating profile"
                   if len(item.get("inputs") or ()) == 1
                   else "Integrating profile versions")
        progress = 95
    elif item.get("chunk_num") and item.get("chunk_total"):
        n, total = int(item["chunk_num"]), int(item["chunk_total"])
        message = f"Generating profile: Chunk {n} of ~{total}"
        progress = int(100 * (n - 1) / total) if total else 0
    else:
        # Item submitted before the ordinal was recorded.
        message, progress = "Generating profile", 0
    return {
        "running": True,
        "source": "batch",
        "status": "progress",
        "progress": progress,
        "message": message,
        "task_id": None,
        "error": None,
        "latest_profile": _latest_profile_snapshot(user),
    }


def _sync_progress(user):
    """Progress of the synchronous Celery task the guard points at, with
    the same staleness rule the dispatchers apply (_is_task_stale:
    finished, PENDING for 15+ min, or running for 1+ h — Celery kills
    every task at 1 h). A stale guard is cleared here too, exactly as
    maybe_trigger_profile_update clears it, so a reload does not report
    the same dead task again."""
    from backend.celery_app import celery
    from backend.tasks.exports import _is_task_stale

    task_id = user.profile_generation_task_id
    task = celery.AsyncResult(task_id)
    if _is_task_stale(user):
        state = task.state
        if state == 'SUCCESS':
            status = 'completed'
        elif state in ('FAILURE', 'REVOKED'):
            status = 'failed'
        else:
            status = 'stalled'
        current_app.logger.warning(
            f"Profile task {task_id} for user {user.id} is stale "
            f"(state {state}); clearing guard")
        user.profile_generation_task_id = None
        user.profile_generation_task_dispatched_at = None
        db.session.commit()
        return {
            "running": False,
            "source": "sync",
            "status": status,
            "progress": 100 if status == 'completed' else 0,
            "message": "",
            "task_id": task_id,
            "error": (failure_text(task.info, PROFILE_FAILED_TEXT)
                      if status == 'failed' else None),
            "latest_profile": _latest_profile_snapshot(user),
        }

    state = task.state
    info = task.info if isinstance(task.info, dict) else {}
    return {
        "running": True,
        "source": "sync",
        "status": _SYNC_STATUS_MAP.get(state, state.lower()),
        "progress": info.get('progress', 0),
        "message": info.get('status', ''),
        "task_id": task_id,
        "error": None,
        "latest_profile": _latest_profile_snapshot(user),
    }


@export_bp.route("/export/profile-progress", methods=["GET"])
@login_required
def get_profile_progress():
    """Is the current user's profile being built, and how far (#258).

    One source of truth for both pipelines: the Batch API chain
    (profile_batch_pending — no task id, invisible to the old per-task
    endpoint) and the synchronous Celery task (profile_generation_task_id).
    ProfileGenerationWatcher polls this while `running` is true.

    Query `task_id`: the sync task the client last saw running. The
    task's own `finally` clears the guard before the client can observe
    the terminal state, so with nothing in flight the outcome of that
    task is resolved from the Celery result (kept 24 h) — the client
    gets `completed` / `failed` instead of a bare `idle`. Only for the
    user's own task (the task records its owner, utils/task_owner);
    any other id is ignored. A failure says only text written for the
    user, else a fixed line.
    """
    if current_user.profile_batch_pending:
        return jsonify(_batch_progress(current_user)), 200
    if current_user.profile_generation_task_id:
        return jsonify(_sync_progress(current_user)), 200

    status, error = "idle", None
    last_task_id = request.args.get("task_id")
    if last_task_id and task_owned_by(last_task_id, current_user.id,
                                      current_app.config):
        from backend.celery_app import celery
        task = celery.AsyncResult(last_task_id)
        if task.state in ('FAILURE', 'REVOKED'):
            status = "failed"
            error = failure_text(task.info, PROFILE_FAILED_TEXT)
        else:
            # SUCCESS, or PENDING for an id Celery no longer knows: the
            # task finished (the guard is gone).
            status = "completed"
    return jsonify({
        "running": False,
        "source": None,
        "status": status,
        "progress": 100 if status == "completed" else 0,
        "message": "",
        "task_id": None,
        "error": error,
        "latest_profile": _latest_profile_snapshot(current_user),
        # The batch chain's last step failed and waits for the seeder's
        # retry (attempts reset to 0 whenever a step is applied), so a
        # chain that saved a chunk and then failed is not "updated".
        "batch_step_failed": bool(current_user.profile_batch_attempts),
    }), 200


# DELETE /api/delete_my_data was removed (#268): it deleted only the
# user's own node rows, missed imports, AI replies and every dependent
# table, and failed on Postgres's foreign keys. "Delete all my content"
# is DELETE /api/account/data (backend/routes/account_data.py).
