"""Artifact versions as the model and the user see them (#604).

An artifact (memory, scratchpad, a custom kind) is a run of UserArtifact
rows per (user, kind); the UI numbers them v1, v2, ... oldest first
(routes/artifacts.py). The model sees the same numbers: on the inline
copies, in read_artifact results, in the answer to each write, and it
names the version a write is based on (update_artifact's base_version).

Ids of versions are kept in plaintext tool_calls_meta (an update's
artifact_id and previous_artifact_id); their content never is. Every
reader of such an id fetches the row through ``stored_version``, so a
rule on which versions may still be read (a deleted one, #605) is added
in one place.
"""
import difflib
import re

from sqlalchemy import and_, or_

from backend.models import UserArtifact


def _versions_query(user_id, kind):
    """The versions of *kind* the numbering counts. #605 (deleting an
    artifact) restricts this to the versions that are not deleted."""
    return UserArtifact.query.filter(
        UserArtifact.user_id == user_id, UserArtifact.kind == kind)


def stored_version(artifact_id, user_id):
    """The version *artifact_id* points at, when it is *user_id*'s;
    otherwise (no such row, someone else's) None. For ids kept elsewhere:
    tool_calls_meta entries and the Actions-taken diff request. Callers
    still check the row's ai_usage before its text reaches a model."""
    if artifact_id is None:
        return None
    row = UserArtifact.query.get(artifact_id)
    if row is None or row.user_id != user_id:
        return None
    return row


def version_number(row, cache=None):
    """*row*'s number among its kind's versions, oldest first: the vN the
    artifacts page shows. *cache* ({row id: number}) saves the count when
    one prompt numbers the same row twice."""
    if cache is not None and row.id in cache:
        return cache[row.id]
    number = _versions_query(row.user_id, row.kind).filter(or_(
        UserArtifact.created_at < row.created_at,
        and_(UserArtifact.created_at == row.created_at,
             UserArtifact.id <= row.id),
    )).count()
    if cache is not None:
        cache[row.id] = number
    return number


def previous_version(row):
    """The version of *row*'s kind just before it, or None (v1)."""
    return _versions_query(row.user_id, row.kind).filter(or_(
        UserArtifact.created_at < row.created_at,
        and_(UserArtifact.created_at == row.created_at,
             UserArtifact.id < row.id),
    )).order_by(UserArtifact.created_at.desc(),
                UserArtifact.id.desc()).first()


_VERSION_RE = re.compile(r"^\s*v?(\d+)\s*$", re.IGNORECASE)


def parse_version(value):
    """A base_version as the model sends it (13, "13", "v13") → 13;
    None when absent or not a version number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        match = _VERSION_RE.match(value)
        if match:
            return int(match.group(1))
    return None


def artifact_diff(old_text, new_text):
    """The unified diff from *old_text* to *new_text*, hunks only (no
    ---/+++ file headers), one line of context. Empty when nothing
    changed. This is what the model sees of a write."""
    lines = list(difflib.unified_diff(
        (old_text or "").splitlines(), (new_text or "").splitlines(),
        lineterm="", n=1))[2:]
    return "\n".join(lines)


def line_diff(old_text, new_text):
    """The line diff from *old_text* (None: nothing before, a creation)
    to *new_text*, for the user to read: every line, in order, as
    {"type": "same" | "del" | "add", "text"}. The web client folds long
    unchanged runs and marks changed words (utils/diff.js)."""
    new_lines = (new_text or "").split("\n")
    if old_text is None:
        return [{"type": "add", "text": line} for line in new_lines]
    old_lines = old_text.split("\n")
    ops = []
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            ops.extend({"type": "same", "text": line}
                       for line in old_lines[i1:i2])
            continue
        ops.extend({"type": "del", "text": line}
                   for line in old_lines[i1:i2])
        ops.extend({"type": "add", "text": line}
                   for line in new_lines[j1:j2])
    return ops
