"""Filing the GitHub issues an AI reply proposes.

One reply can propose several issues (one ``### Issue Title`` block each).
Each is filed on its own, addressed by its position in the reply
(``parse_github_issues`` order, 0-based). The filed ones accumulate in the
reply's propose_github_issue entry (``filed``: index, issue_url,
issue_number); the pending draft is deleted only once all are filed, so
the remaining ones can still be filed (the card's buttons,
apply_github_issue). Shared by the card's route and the apply_github_issue
tool.
"""
from flask import current_app

from backend.extensions import db
from backend.utils.error_privacy import describe_exception
from backend.utils.tool_meta import (
    get_tool_meta_entry, parse_github_issues, update_tool_meta)

NO_ISSUE_MESSAGE = "Could not parse issue from proposal"
FAILED_MESSAGE = "Couldn't create the GitHub issue. Please try again later."


def file_proposed_issues(origin_node, draft, username, platform, index=None):
    """File the issue at *index* of *origin_node*'s proposal, or every
    one not filed yet when *index* is None. The caller has checked that
    the node is the user's own live proposal.

    Returns ``(filed_now, total, error, status)``: *filed_now* is a list
    of ``{'index', 'issue_url', 'issue_number'}`` for this call, *error* a
    user-safe message (or None) with the HTTP *status* it maps to. Issues
    filed before a failure stay recorded; the caller commits either way.
    Nothing is committed here."""
    from backend.utils.github import create_github_issue
    issues = parse_github_issues(origin_node.get_content() or "")
    if not issues:
        return [], 0, NO_ISSUE_MESSAGE, 400

    entry = get_tool_meta_entry(origin_node, "propose_github_issue") or {}
    filed = [f for f in (entry.get("filed") or []) if isinstance(f, dict)]
    done = {f.get("index") for f in filed}

    if index is not None:
        if not 0 <= index < len(issues):
            return [], len(issues), "Invalid issue_index", 400
        if index in done:
            return [], len(issues), "Already filed", 400
        targets = [index]
    else:
        targets = [i for i in range(len(issues)) if i not in done]
        if not targets:
            return [], len(issues), "Already filed", 400

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
        db.session.flush()
    return filed_now, len(issues), error, status
