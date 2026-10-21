"""Filing the GitHub issues an AI reply proposes.

One reply can propose several issues (one ``### Issue Title`` block each).
Each is filed on its own, addressed by its position in the reply
(``parse_github_issues`` order, 0-based). The filed ones accumulate in the
reply's propose_github_issue entry (``filed``: index, issue_url,
issue_number); the pending draft is deleted only once all are filed, so
the remaining ones can still be filed (the card's buttons,
apply_github_issue). Shared by the card's route and the apply_github_issue
tool.

Filing to GitHub can't be undone, so the filing of one reply is serialized
by a Redis lock (as the todo merge does for a user) and the reply's
tool_calls_meta is read again once the lock is held: two requests never
file the same position twice or overwrite each other's ``filed`` record.
A request that names no position files only the LAST issue, which is what
every client did before replies could hold several.
"""
import redis
from flask import current_app

from backend.extensions import db
from backend.utils.error_privacy import describe_exception
from backend.utils.tool_meta import (
    get_tool_meta_entry, parse_github_issues, update_tool_meta)

NO_ISSUE_MESSAGE = "Could not parse issue from proposal"
FAILED_MESSAGE = "Couldn't create the GitHub issue. Please try again later."
ALREADY_FILED_MESSAGE = "Already filed"
BUSY_MESSAGE = ("These issues are being created right now. "
                "Please try again in a moment.")

# Longest a filing holds the lock (one GitHub call per issue), and how long
# a second request waits for it.
_LOCK_TIMEOUT = 120
_LOCK_WAIT = 30


def _lock_client():
    """The Redis client for the filing lock, or None when there is none
    (tests)."""
    config = current_app.config
    if config.get("TESTING"):
        return None
    url = config.get("CELERY_BROKER_URL") or "redis://localhost:6379/0"
    return redis.Redis.from_url(url)


class _NoLock:
    def acquire(self, blocking=True):
        return True

    def release(self):
        pass


def _filing_lock(node_id):
    client = _lock_client()
    if client is None:
        return _NoLock()
    return client.lock(f"issue_filing_lock:{node_id}",
                       timeout=_LOCK_TIMEOUT, blocking_timeout=_LOCK_WAIT)


def file_proposed_issues(origin_node, draft, username, platform,
                         index=None, all_unfiled=False):
    """File the issue at *index* of *origin_node*'s proposal; with no
    *index*, every issue not filed yet when *all_unfiled*, else only the
    last one. The caller has checked that the node is the user's own live
    proposal.

    Returns ``(filed_now, total, error, status)``: *filed_now* is a list
    of ``{'index', 'issue_url', 'issue_number'}`` for this call, *error* a
    user-safe message (or None) with the HTTP *status* it maps to. Issues
    filed before a failure stay recorded. Commits when it filed anything,
    while still holding the lock."""
    lock = _filing_lock(origin_node.id)
    if not lock.acquire(blocking=True):
        return [], 0, BUSY_MESSAGE, 409
    try:
        # Another request may have filed in the meantime.
        db.session.refresh(origin_node)
        return _file_locked(origin_node, draft, username, platform,
                            index, all_unfiled)
    finally:
        try:
            lock.release()
        except redis.exceptions.LockError:
            current_app.logger.warning(
                "Issue filing lock expired before release for node %s",
                origin_node.id)


def _file_locked(origin_node, draft, username, platform, index,
                 all_unfiled):
    from backend.utils.github import create_github_issue
    issues = parse_github_issues(origin_node.get_content() or "")
    if not issues:
        return [], 0, NO_ISSUE_MESSAGE, 400

    entry = get_tool_meta_entry(origin_node, "propose_github_issue") or {}
    filed = [f for f in (entry.get("filed") or []) if isinstance(f, dict)]
    if entry.get("apply_status") == "completed" and not filed:
        # Filed before issues were tracked one by one: the last one.
        filed = [{"index": len(issues) - 1,
                  "issue_url": entry.get("issue_url"),
                  "issue_number": entry.get("issue_number")}]
    done = {f.get("index") for f in filed}

    if index is not None:
        if not 0 <= index < len(issues):
            return [], len(issues), "Invalid issue_index", 400
        targets = [index]
    elif all_unfiled:
        targets = [i for i in range(len(issues)) if i not in done]
    else:
        targets = [len(issues) - 1]
    targets = [i for i in targets if i not in done]
    if not targets:
        return [], len(issues), ALREADY_FILED_MESSAGE, 400

    filed_now = []
    error, status = None, 200
    for i in targets:
        issue = issues[i]
        category = issue.get("category", "enhancement")
        if category not in ("bug", "feature", "enhancement"):
            category = "enhancement"
        try:
            gh_result = create_github_issue(
                title=issue["title"],
                description=issue.get("description", ""),
                category=category,
                username=username,
                platform=platform,
            )
        except (ValueError, RuntimeError) as e:
            # Missing configuration or GitHub's answer: neither is for
            # the user. The log keeps the type.
            # describe_exception names GitHub's HTTP status (401 vs 422)
            current_app.logger.exception(
                "GitHub issue creation failed: %s", describe_exception(e))
            error, status = FAILED_MESSAGE, 500
            break
        filed_now.append({"index": i, "issue_url": gh_result["url"],
                          "issue_number": gh_result["number"]})

    if filed_now:
        filed = filed + filed_now
        all_filed = len({f.get("index") for f in filed}) >= len(issues)
        if draft is not None and all_filed:
            db.session.delete(draft)
        update_tool_meta(origin_node, "propose_github_issue", {
            "apply_status": "completed" if all_filed else "partial",
            # First filed issue, for clients that know only one.
            "issue_url": filed[0]["issue_url"],
            "issue_number": filed[0]["issue_number"],
            "filed": filed,
        })
        # Committed here, under the lock, so the next request sees it.
        db.session.commit()
    return filed_now, len(issues), error, status


def file_issues_for_tool(origin_node, draft, username, platform,
                         numbers=None, all_unfiled=False):
    """The apply_github_issue tool's filing: the 1-based *numbers* the
    model named, else every unfiled issue when *all_unfiled*, else only the
    last issue (what the tool always did). Returns the tool result's
    fields, with a ``summary`` that says truthfully what was created, what
    was filed already and what failed, for the model to tell the user."""
    created, already, failed, not_tried = [], [], [], []
    error = None
    if numbers:
        try:
            picks = sorted({int(n) - 1 for n in numbers})
        except (TypeError, ValueError):
            return {"status": "error", "error": "Invalid issue_numbers",
                    "summary": "Invalid issue_numbers"}
        for pos, i in enumerate(picks):
            filed, _total, err, _status = file_proposed_issues(
                origin_node, draft, username, platform, index=i)
            created += filed
            if err == ALREADY_FILED_MESSAGE:
                already.append(i)
            elif err:
                error = err
                failed.append(i)
                not_tried = picks[pos + 1:]
                break
    else:
        created, _total, error, _status = file_proposed_issues(
            origin_node, draft, username, platform, all_unfiled=all_unfiled)
        if error == ALREADY_FILED_MESSAGE:
            already.append(-1)
            error = None
        elif error and all_unfiled:
            failed.append(-1)

    def numbered(items):
        return ", ".join(str(i + 1) for i in items)

    parts = []
    if created:
        parts.append("Created " + ", ".join(
            f"#{f['issue_number']} (issue {f['index'] + 1}, "
            f"{f['issue_url']})" for f in created) + ".")
    if already:
        parts.append("Already created earlier: " + (
            numbered(already) if already != [-1]
            else "the issue asked for") + ".")
    if failed and error:
        what = ("the other issues" if failed == [-1]
                else "issue " + numbered(failed))
        parts.append(f"Not created: {what} ({error}) "
                     "They are still pending.")
    if not_tried:
        parts.append("Not tried: issue " + numbered(not_tried)
                     + ". Still pending.")
    if not created and error and not failed:
        parts.append(error)
    summary = " ".join(parts)
    result = {"summary": summary}
    if created:
        result.update(
            status="success", issues=created,
            issue_url=created[0]["issue_url"],
            issue_number=created[0]["issue_number"])
        if error:
            result["error"] = error
        elif len(parts) == 1 and len(created) == 1:
            # One issue created, nothing else to tell: the plain note.
            del result["summary"]
    else:
        result.update(status="error", error=summary or error)
    return result
