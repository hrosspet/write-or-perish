"""Shared helper for submitting user feedback from a proposal node (#158).

Feedback the AI proposes to send lives in the visible node content under a
`### Feedback` heading (see ``parse_feedback``); it is only persisted to the
``UserFeedback`` table when the user explicitly confirms — via the Send button
(``/api/feedback/submit``) or the ``apply_feedback`` tool. Keeping the parse +
persist logic here means both confirmation paths behave identically.
"""
from backend.models import UserFeedback
from backend.extensions import db
from backend.utils.tool_meta import parse_feedback

FEEDBACK_CATEGORIES = ("praise", "frustration", "idea", "other")


def submit_feedback_from_node(origin_node, user_id):
    """Parse the feedback proposal out of *origin_node*'s content and persist a
    UserFeedback row (flushed, not committed — the caller commits).

    Returns ``(feedback, None)`` on success or ``(None, error_message)`` when
    the proposal has no parseable feedback content.
    """
    parsed = parse_feedback(origin_node.get_content() or "")
    content = (parsed.get("content") or "").strip()
    if not content:
        return None, "Could not parse feedback from proposal"
    category = parsed.get("category") or "other"
    if category not in FEEDBACK_CATEGORIES:
        category = "other"
    feedback = UserFeedback(user_id=user_id, category=category, source="llm")
    feedback.set_content(content)
    db.session.add(feedback)
    db.session.flush()
    return feedback, None
