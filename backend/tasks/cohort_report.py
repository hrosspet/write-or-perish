"""The first cohort's two drafts (experiment, COHORT_V1): the report a
person may send the founder, and the introduction a member may share with
the group. Both are asked for by the person (routes/cohort.py), run live
(the person waits on the page for them; tasks/poll_draft.py is the
pattern for the rest), and are saved as a new private node of the
person's own: their document, which they edit like any entry, so their
edit wins.

* The report reads the person's own interview thread and nothing else
  (utils/cohort.interview_transcript: their nodes, AI-readable only).
* The introduction reads the current text of the person's own report
  node, their edited version, never the interview.

The model is the person's own, chosen the way their replies are
(llm_nodes.pick_model_for_generation): never another provider. The call
is billed to their account (request_type "cohort_report" /
"cohort_intro"), which their spend cap counts. A refusal, an empty
answer or an error marks the draft failed, and the page offers to ask
again; nothing is half saved.
"""
import logging
from datetime import datetime

from backend.celery_app import celery, flask_app
from backend.extensions import db
from backend.models import APICostLog, Cohort, CohortInvitation, Node, User
from backend.utils import cohort as ch

logger = logging.getLogger(__name__)


def _call(model_id, system, text, max_tokens, user_id):
    from backend.llm_providers import LLMProvider
    from backend.utils.api_keys import get_api_keys_for_usage
    keys = get_api_keys_for_usage(flask_app.config, "chat")
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": text}]
    # One-shot: no later request starts with this prefix (#803).
    return LLMProvider.get_completion(
        model_id, messages, keys, max_tokens=max_tokens, user_id=user_id,
        one_shot=True)


def _log_cost(user_id, model_id, response, kind, inv_id):
    from backend.utils.cost import llm_cost_log_fields
    db.session.add(APICostLog(
        user_id=user_id, model_id=model_id, request_type=kind,
        request_ref=f"cohort_invitation:{inv_id}",
        **llm_cost_log_fields(model_id, response)))


def _usable_text(response):
    from backend.llm_providers import is_empty_truncated, is_refused
    if is_refused(response) or is_empty_truncated(response):
        return None
    text = (response.get("content") or "").strip()
    return text or None


def _save_node(user, text, sources):
    """A new private node of the person's own holding *text*. Chat at
    most (never offered for training as written by a model from their
    words), carrying the strongest input mark of its *sources*."""
    from backend.utils.privacy import strongest_input
    from backend.utils.tokens import approximate_token_count
    node = Node(user_id=user.id, human_owner_id=user.id, parent_id=None,
                node_type="user", privacy_level="private", ai_usage="chat",
                unlicensed_input=strongest_input(
                    s.unlicensed_input for s in sources),
                token_count=approximate_token_count(text))
    node.set_content(text)
    db.session.add(node)
    db.session.flush()
    return node


def _ours(inv, field, stamp):
    """*inv*'s draft in *field* is still the one this task was queued for:
    running, asked for at *stamp*, and not stale. A task that comes late
    (its draft read as failed and was asked for again) does nothing."""
    started = getattr(inv, ch.STARTED_AT[field])
    return (getattr(inv, field) in ch.RUNNING
            and ch.task_status(inv, field) != ch.FAILED
            and (stamp is None or ch.stamp_of(started) == stamp))


def _start(invitation_id, field, stamp=None):
    """The invitation and its person when the draft in *field* is still
    the one asked for at *stamp*, with the status moved to processing;
    else (None, None). A stale one is marked failed."""
    inv = db.session.get(CohortInvitation, invitation_id)
    if inv is None:
        return None, None
    if not _ours(inv, field, stamp):
        if (stamp is None or ch.stamp_of(
                getattr(inv, ch.STARTED_AT[field])) == stamp) \
                and ch.expire_stale_task(inv, field):
            db.session.commit()
        return None, None
    user = db.session.get(User, inv.user_id) if inv.user_id else None
    if user is None or not ch.is_live(inv) or user.deleted_at is not None:
        setattr(inv, field, ch.FAILED)
        db.session.commit()
        return None, None
    setattr(inv, field, ch.PROCESSING)
    db.session.commit()
    return inv, user


def _may_call(user):
    """No AI means no AI, and no billed call without AI replies: checked
    when the task runs, since either can change after the click."""
    from backend.utils.privacy import account_allows_ai
    from backend.utils.subscriptions import ai_replies_allowed
    return account_allows_ai(user) and ai_replies_allowed(user)


def _fail(invitation_id, field, stamp=None):
    db.session.rollback()
    inv = db.session.get(CohortInvitation, invitation_id)
    if inv is not None and getattr(inv, field) in ch.RUNNING and (
            stamp is None
            or ch.stamp_of(getattr(inv, ch.STARTED_AT[field])) == stamp):
        setattr(inv, field, ch.FAILED)
        db.session.commit()


def _still_ours(inv, field, stamp):
    """Before saving: the draft was not given up on (stale) or asked for
    again meanwhile. The call was billed either way; only the node is not
    made."""
    db.session.refresh(inv)
    if _ours(inv, field, stamp):
        return True
    logger.warning("Cohort draft %s for invitation %s not saved: asked "
                   "for again or given up on while it was written", field,
                   inv.id)
    return False


def run_report_draft(invitation_id, stamp=None):
    """Core of draft_cohort_report (a plain function the tests call)."""
    from backend.utils.llm_nodes import pick_model_for_generation
    inv, user = _start(invitation_id, "report_task_status", stamp)
    if inv is None:
        return None
    try:
        if not _may_call(user):
            inv.report_task_status = ch.FAILED
            db.session.commit()
            return None
        text, sources = ch.interview_transcript(inv, user)
        if not text:
            inv.report_task_status = ch.NOTHING
            db.session.commit()
            return None
        cohort = db.session.get(Cohort, inv.cohort_id)
        model_id = pick_model_for_generation(sources[-1], user)
        response = _call(model_id, ch.report_prompt(cohort), text,
                         ch.REPORT_MAX_TOKENS, user.id)
        _log_cost(user.id, model_id, response, "cohort_report", inv.id)
        db.session.commit()
        if not _still_ours(inv, "report_task_status", stamp):
            return None
        draft = _usable_text(response)
        if draft is None:
            logger.warning("Cohort report for invitation %s not saved: "
                           "refused or empty (model %s)", inv.id, model_id)
            inv.report_task_status = ch.FAILED
            db.session.commit()
            return None
        node = _save_node(user, draft, sources)
        inv.report_node_id = node.id
        inv.report_written_by_model = True
        inv.report_drafted_at = datetime.utcnow()
        inv.report_task_status = ch.DONE
        db.session.commit()
        return node.id
    except Exception:
        logger.exception("Cohort report draft failed for invitation %s",
                         invitation_id)
        _fail(invitation_id, "report_task_status", stamp)
        return None


def run_intro_draft(invitation_id, stamp=None):
    """Core of draft_cohort_intro (a plain function the tests call)."""
    from backend.utils.llm_nodes import pick_model_for_generation
    from backend.utils.privacy import AI_ALLOWED
    inv, user = _start(invitation_id, "intro_task_status", stamp)
    if inv is None:
        return None
    try:
        report = (db.session.get(Node, inv.report_node_id)
                  if inv.report_node_id else None)
        if (not _may_call(user) or not inv.admitted_at
                or not ch.own_node(report, user.id)
                or report.ai_usage not in AI_ALLOWED):
            inv.intro_task_status = ch.FAILED
            db.session.commit()
            return None
        # The report as the person left it: their edit, not the draft.
        text = (report.get_content() or "").strip()
        if not text:
            inv.intro_task_status = ch.NOTHING
            db.session.commit()
            return None
        model_id = pick_model_for_generation(None, user)
        response = _call(model_id, ch.intro_prompt(), text,
                         ch.INTRO_MAX_TOKENS, user.id)
        _log_cost(user.id, model_id, response, "cohort_intro", inv.id)
        db.session.commit()
        if not _still_ours(inv, "intro_task_status", stamp):
            return None
        draft = _usable_text(response)
        if draft is None:
            logger.warning("Cohort introduction for invitation %s not "
                           "saved: refused or empty (model %s)", inv.id,
                           model_id)
            inv.intro_task_status = ch.FAILED
            db.session.commit()
            return None
        node = _save_node(user, draft, [report])
        inv.intro_node_id = node.id
        inv.intro_task_status = ch.DONE
        db.session.commit()
        return node.id
    except Exception:
        logger.exception("Cohort introduction draft failed for invitation "
                         "%s", invitation_id)
        _fail(invitation_id, "intro_task_status", stamp)
        return None


@celery.task
def draft_cohort_report(invitation_id, stamp=None):
    """*stamp*: when the draft was asked for (ch.start_task); the task acts
    only while the invitation's draft is still that one."""
    with flask_app.app_context():
        return run_report_draft(invitation_id, stamp)


@celery.task
def draft_cohort_intro(invitation_id, stamp=None):
    with flask_app.app_context():
        return run_intro_draft(invitation_id, stamp)
