"""Write tool calls that wait for the user's approval (Peter, 2026-11-12).

"direct writing to artifacts should be in the case when other user's text
is in the context, intercepted and via a dialog raised to the user. It
should [show] what tool call is proposed, and show the proposed diff if
it's about changing an artifact. The user would have to approve every
single tool call in such a thread. If more the one such possibly
destructive tool call is made in a single node, all of them should be
raised inside one dialog"

When a reply's history holds text the requester did not write
(utils/foreign_text.history_by_others), a call to a tool that writes the
requester's data directly is not run. It is kept as a Draft row of the
reply node it was made in (label TOOL_PENDING_LABEL, its full input
encrypted in the row's content), and the call's tool_calls_meta entry
says it waits ("pending_approval"), so the model answers knowing nothing
was done. The pages show every waiting call of one reply in one dialog;
the user approves or rejects each. Approving runs the call then, through
the same code the reply would have run (llm_completion._execute_tool_calls,
with its version check), and rejecting changes nothing. Either way the
Draft row goes, and the entry records the outcome, which the reply's
later turns are told once.

A todo proposal ("### New Tasks") is not a tool call: the user applies it
with its own button, as before. apply_todo_changes and the other apply
tools are the voice path that applies a proposal without that button, so
they wait here like update_artifact.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import and_, or_

from backend.extensions import db
from backend.models import Draft, Node, UserArtifact

TOOL_PENDING_LABEL = "tool_pending"
# A call one request has claimed to decide (claim): no other request
# decides it meanwhile, so an approval runs once.
TOOL_DECIDING_LABEL = "tool_deciding"
# INTRODUCED HEURISTIC: a claim older than this (its request died between
# the claim and its commit) reads as waiting again. Far above any
# decision's run (one write or one queued job), short enough that the
# call shows again the next time the user opens the thread.
DECIDING_STALE_AFTER = timedelta(minutes=10)

# Every tool that changes the user's data, or acts for them, when it runs.
DIRECT_WRITE_TOOLS = (
    "update_artifact",
    "apply_todo_changes",
    "apply_github_issue",
    "apply_feedback",
    "apply_share",
)

PENDING_STATUS = "pending_approval"
REJECTED_STATUS = "rejected"

# What each call does, as the dialog names it.
_ACTION_LABELS = {
    "apply_todo_changes": "Apply the proposed changes to your todo list",
    "apply_github_issue": "File the proposed GitHub issue",
    "apply_feedback": "Send the proposed feedback to the Loore team",
    "apply_share": "Save the proposed share as a private draft",
}

# The proposal draft each apply tool acts on (its label).
_PROPOSAL_LABELS = {
    "apply_todo_changes": "todo_pending",
    "apply_github_issue": "github_issue_pending",
    "apply_feedback": "feedback_pending",
    "apply_share": "share_pending",
}


class ApprovalHold:
    """The calls of one reply that wait for approval: present (truthy)
    when the reply's history holds text the requester did not write.
    *kinds* is that text's kinds (foreign_text), *turn_node_id* the
    reply's first node, which groups its calls into one dialog."""

    def __init__(self, kinds, turn_node_id):
        self.kinds = sorted(kinds or ())
        self.turn_node_id = turn_node_id

    def __bool__(self):
        return bool(self.kinds)

    def hold(self, name, inp, node, user_id, previous_artifact_id=None,
             proposal_draft_id=None, kind=None):
        """Keep the call *name*(*inp*) of *node* for approval. Returns the
        fields its tool_calls_meta entry takes."""
        draft = Draft(user_id=user_id, parent_id=node.id,
                      label=TOOL_PENDING_LABEL)
        draft.set_content(json.dumps({
            "tool": name,
            "input": inp,
            "turn_node_id": self.turn_node_id,
            "kinds": self.kinds,
            "previous_artifact_id": previous_artifact_id,
            "proposal_draft_id": proposal_draft_id,
            "kind": kind,
        }))
        db.session.add(draft)
        db.session.flush()
        fields = {"status": PENDING_STATUS, "pending_id": draft.id}
        if kind is not None:
            fields["kind"] = kind
        return fields


def _payload(draft):
    try:
        data = json.loads(draft.get_content() or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _waiting(now=None):
    """A call that waits for a decision: not claimed, or claimed by a
    request that died (DECIDING_STALE_AFTER)."""
    now = now or datetime.utcnow()
    return or_(Draft.label == TOOL_PENDING_LABEL,
               and_(Draft.label == TOOL_DECIDING_LABEL,
                    Draft.updated_at < now - DECIDING_STALE_AFTER))


def pending_drafts(user_id, node_ids):
    """The user's waiting calls made in the nodes *node_ids*, oldest
    first. One indexed query."""
    ids = [i for i in set(node_ids or ()) if i is not None]
    if not ids:
        return []
    rows = []
    for start in range(0, len(ids), 900):
        rows.extend(Draft.query.filter(
            Draft.user_id == user_id, _waiting(),
            Draft.parent_id.in_(ids[start:start + 900])).all())
    return sorted(rows, key=lambda d: d.id)


def claim(draft_id, user_id):
    """Claim the waiting call *draft_id* of *user_id* for one decision:
    one conditional UPDATE, committed. True when this request got it;
    False when it is not the user's, not waiting, or another request
    claimed it first (a double tap, two devices, a retry)."""
    now = datetime.utcnow()
    claimed = Draft.query.filter(
        Draft.id == draft_id, Draft.user_id == user_id, _waiting(now),
    ).update({Draft.label: TOOL_DECIDING_LABEL, Draft.updated_at: now},
             synchronize_session=False)
    db.session.commit()
    return claimed == 1


def release(draft_id):
    """Put a claimed call back to waiting (its run failed): the user can
    approve it again or reject it."""
    Draft.query.filter(
        Draft.id == draft_id, Draft.label == TOOL_DECIDING_LABEL,
    ).update({Draft.label: TOOL_PENDING_LABEL,
              Draft.updated_at: datetime.utcnow()},
             synchronize_session=False)
    db.session.commit()


def _artifact_preview(payload, user_id):
    """The dialog's view of an update_artifact call: the artifact, the
    version the call was based on and the latest one, and the line diff
    from the version it was based on to the text it would save."""
    from backend.utils.artifact_versions import (
        line_diff, stored_version, version_number,
    )
    from backend.tasks.llm_completion import _resolve_artifact_write
    inp = payload.get("input") or {}
    kind = payload.get("kind") or (inp.get("kind") or "").strip().lower()
    previous = None
    if payload.get("previous_artifact_id") is not None:
        previous = stored_version(payload["previous_artifact_id"], user_id)
    latest = UserArtifact.latest_for(user_id, kind)
    title = ((latest.title if latest else None)
             or (previous.title if previous else None)
             or (inp.get("title") or "").strip()
             or UserArtifact.DEFAULT_KINDS.get(kind)
             or kind.replace("-", " ").title())
    out = {
        "kind": kind,
        "artifact_title": title,
        "created": payload.get("previous_artifact_id") is None,
        "base_version": version_number(previous) if previous else None,
        "current_version": version_number(latest) if latest else None,
        # The artifact changed since the call was made: approving it
        # saves nothing (the write's version check, which also keeps the
        # title and description below as shown).
        "stale": ((latest.id if latest else None)
                  != payload.get("previous_artifact_id")),
    }
    # The write also sets the title and description the call names
    # (llm_completion._execute_tool_calls): shown when they change.
    new_title = (inp.get("title") or "").strip()[:128]
    old_title = previous.title if previous is not None else None
    if new_title and new_title != (old_title or ""):
        out["title_change"] = {"from": old_title, "to": new_title}
    new_description = (inp.get("description") or "").strip()
    old_description = previous.description if previous is not None else None
    if new_description and new_description != (old_description or ""):
        out["description_change"] = {"from": old_description,
                                      "to": new_description}
    if payload.get("previous_artifact_id") is not None and previous is None:
        out["lines"] = []
        return out
    new_text, err = _resolve_artifact_write(inp, previous)
    if err:
        out["lines"] = []
        out["error"] = err
    else:
        out["lines"] = line_diff(
            (previous.get_content() or "") if previous is not None else None,
            new_text or "")
    return out


def _issues_to_file(origin, inp):
    """The issues apply_github_issue(*inp*) would file from *origin*'s
    proposal, as [(1-based number, issue)]: the numbers it names, else
    every issue not filed yet with ``all``, else the last one; never one
    filed already (issue_proposals.file_issues_for_tool)."""
    from backend.utils.tool_meta import (
        get_tool_meta_entry, parse_github_issues,
    )
    issues = parse_github_issues(origin.get_content() or "")
    if not issues:
        return []
    entry = get_tool_meta_entry(origin, "propose_github_issue") or {}
    filed = [f for f in (entry.get("filed") or []) if isinstance(f, dict)]
    if entry.get("apply_status") == "completed" and not filed:
        filed = [{"index": len(issues) - 1}]
    done = {f.get("index") for f in filed}
    numbers = inp.get("issue_numbers")
    if numbers:
        try:
            targets = sorted({int(n) - 1 for n in numbers})
        except (TypeError, ValueError):
            return []
        targets = [i for i in targets if 0 <= i < len(issues)]
    elif inp.get("all"):
        targets = list(range(len(issues)))
    else:
        targets = [len(issues) - 1]
    return [(i + 1, issues[i]) for i in targets if i not in done]


def _proposal_preview(payload, user_id):
    """The dialog's view of an apply call: exactly what approving it
    applies, from the reply that proposed it. The todo changes: the
    proposal the merge reads. Issues: the ones the call's input selects.
    Feedback: the feedback text. Shares: the blocks not saved yet."""
    from backend.utils.tool_meta import (
        get_tool_meta_entry, parse_feedback, parse_share,
    )
    name = payload.get("tool")
    inp = payload.get("input") or {}
    draft_id = payload.get("proposal_draft_id")
    proposal = db.session.get(Draft, draft_id) if draft_id else None
    origin = (db.session.get(Node, proposal.parent_id)
              if proposal is not None else None)
    if (proposal is None or origin is None or proposal.user_id != user_id
            or proposal.label != _PROPOSAL_LABELS.get(name)):
        # Applied or replaced since: approving finds nothing to apply.
        return {"stale": True}
    out = {"stale": False, "proposal_node_id": proposal.parent_id}
    text = origin.get_content() or ""
    if name == "apply_github_issue":
        picked = _issues_to_file(origin, inp)
        out["issues"] = [{"number": n, "title": i.get("title", "")}
                         for n, i in picked]
        out["proposal_text"] = "\n\n".join(
            f"{i.get('title', '')}\n{i.get('description', '')}".strip()
            for _, i in picked)
        if not picked:
            out["stale"] = True
    elif name == "apply_feedback":
        out["proposal_text"] = (parse_feedback(text).get("content")
                                or "").strip()
    elif name == "apply_share":
        entry = get_tool_meta_entry(origin, "propose_share") or {}
        saved = set(entry.get("saved_indexes") or [])
        out["proposal_text"] = "\n\n".join(
            s["content"] for i, s in enumerate(
                parse_share(text).get("shares") or [])
            if i not in saved)
    else:
        out["proposal_text"] = text
    if not out.get("proposal_text"):
        out["stale"] = True
    return out


def _issue_title(item):
    issues = item.get("issues") or []
    if len(issues) == 1:
        return f"File the proposed GitHub issue: {issues[0]['title']}"
    if issues:
        return f"File {len(issues)} proposed GitHub issues"
    return _ACTION_LABELS["apply_github_issue"]


def serialize_pending(draft, user_id):
    """One waiting call as the dialog shows it."""
    payload = _payload(draft)
    name = payload.get("tool")
    item = {
        "id": draft.id,
        "node_id": draft.parent_id,
        "turn_node_id": payload.get("turn_node_id") or draft.parent_id,
        "tool": name,
        "kinds": payload.get("kinds") or [],
        "created_at": (draft.created_at.isoformat() + "Z"
                       if draft.created_at else None),
    }
    if name == "update_artifact":
        item.update(_artifact_preview(payload, user_id))
        verb = "Create" if item["created"] else "Change"
        item["title"] = f"{verb} your {item['artifact_title']}"
    else:
        item.update(_proposal_preview(payload, user_id))
        item["title"] = (_issue_title(item) if name == "apply_github_issue"
                         else _ACTION_LABELS.get(name, name))
    return item


def reply_alive(node):
    """The reply a call was made in still stands: not deleted, nor hidden
    with its owner's deleted account or content. A call of a reply that
    is gone is void: not listed, never run."""
    from backend.utils.privacy import owner_hidden
    return (node is not None and node.deleted_at is None
            and not owner_hidden(node))


def pending_for_user(user_id, node_ids):
    """Every waiting call of the user in *node_ids*, serialized, whose
    reply node still stands (reply_alive)."""
    out = []
    for draft in pending_drafts(user_id, node_ids):
        if not reply_alive(db.session.get(Node, draft.parent_id)):
            continue
        out.append(serialize_pending(draft, user_id))
    return out


def _chain_above(node):
    """*node*'s ancestors, root first: the history the reply had."""
    chain = []
    current = node.parent
    seen = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        chain.insert(0, current)
        current = current.parent
    return chain


def _record_outcome(node, pending_id, fields):
    """Replace the waiting call's tool_calls_meta entry with *fields* (the
    outcome). Marked unreported, so the reply's next turn is told once."""
    try:
        meta = json.loads(node.tool_calls_meta or "[]")
    except (json.JSONDecodeError, TypeError):
        meta = []
    for i, entry in enumerate(meta):
        if isinstance(entry, dict) and entry.get("pending_id") == pending_id:
            meta[i] = {**fields, "pending_id": pending_id,
                       "status_reported": False}
            break
    else:
        meta.append({**fields, "pending_id": pending_id,
                     "status_reported": False})
    node.tool_calls_meta = json.dumps(meta)


def decide(draft, user_id, approve):
    """Approve (run) or reject the waiting call *draft* (claimed by the
    caller, claim). Returns (outcome, result): "approved" or "rejected",
    recorded on the reply and the draft deleted; "gone": the reply was
    deleted, the call is void and deleted, nothing ran; or "failed": the call
    ran and did not succeed (e.g. the artifact changed since), nothing
    was saved or recorded, and the caller puts it back to waiting
    (release). *result* is the call's tool_calls_meta entry. The caller
    commits."""
    from backend.tasks.llm_completion import (
        _execute_tool_calls, _redact_tool_input,
    )
    from backend.utils.client_platform import CLIENT_MARKER
    from backend.utils.tool_meta import get_tool_meta_entry
    payload = _payload(draft)
    name = payload.get("tool")
    inp = payload.get("input") or {}
    node = db.session.get(Node, draft.parent_id)
    if not reply_alive(node):
        # The reply was deleted: its call is void, never run.
        db.session.delete(draft)
        return "gone", None
    if not approve:
        result = {"name": name, "input": _redact_tool_input(inp),
                  "status": REJECTED_STATUS, "approval": "rejected"}
        if payload.get("kind"):
            result["kind"] = payload["kind"]
        outcome = "rejected"
    else:
        client = (get_tool_meta_entry(node, CLIENT_MARKER) or {}).get(
            "client")
        result = _execute_tool_calls(
            [{"name": name, "input": inp}], node, _chain_above(node),
            user_id, client=client)[0]
        if result.get("status") != "success":
            return "failed", result
        outcome = "approved"
        result["approval"] = outcome
    result["decided_at"] = datetime.utcnow().isoformat() + "Z"
    if node is not None:
        _record_outcome(node, draft.id, result)
    db.session.delete(draft)
    return outcome, result
