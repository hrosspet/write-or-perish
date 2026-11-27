"""The first cohort's routes (experiment, dark behind COHORT_V1: every
route here answers 404 while it is off). The API, field by field, is in
docs/experiments/FIRST-COHORT-API.md.

* invitation_bp (/api/invitation): the page at the link, no sign-in.
* cohort_bp (/api/cohort): the invited person's steps and the group page.
* cohort_admin_bp (/api/admin/cohorts): the founder's routes. They answer
  states, timestamps and the founder's own letters: never a node's text,
  never the id of an interview or of a report.

Nobody but the author gets a path to the interview or to a report that
was not sent. The report reaches the founder only through the Circles
audience the person sets with "Send" (routes/nodes.share_entry, the same
step as any share), and leaves him with the person's normal take-back.
"""
from datetime import date, datetime, timezone

from flask import Blueprint, jsonify, request
from flask_login import current_user

from backend.extensions import db
from backend.models import (
    Cohort, CohortInvitation, Node, NodeCircle, User,
)
from backend.utils import cohort as ch
from backend.utils.timefmt import iso_utc

invitation_bp = Blueprint("cohort_invitation_bp", __name__)
cohort_bp = Blueprint("cohort_bp", __name__)
cohort_admin_bp = Blueprint("cohort_admin_bp", __name__)

# ── Heuristics (named in docs/experiments/FIRST-COHORT-PR.md) ───────────
MAX_LETTER_CHARS = 20_000
MAX_ABOUT_CHARS = 20_000
MAX_QUESTION_CHARS = 5_000
MAX_NAME_CHARS = 60          # a circle's name holds at most 60
MAX_FOUNDER_NAME_CHARS = 64
MAX_URL_CHARS = 512


def _not_found():
    return jsonify({"error": ch.NOT_FOUND}), 404


@invitation_bp.before_request
def _public_gate():
    if not ch.cohort_enabled():
        return _not_found()
    return None


@cohort_bp.before_request
def _user_gate():
    if not ch.cohort_enabled():
        return _not_found()
    if not current_user.is_authenticated:
        return jsonify({"error": "Authentication required"}), 401
    return None


@cohort_admin_bp.before_request
def _admin_gate():
    if not ch.cohort_enabled():
        return _not_found()
    if not current_user.is_authenticated:
        return jsonify({"error": "Authentication required"}), 401
    if not getattr(current_user, "is_admin", False):
        return jsonify({"error": "Forbidden"}), 403
    return None


def _now():
    return datetime.utcnow()


def _no_store(response):
    """The invitation's answers carry a person's letter: never cached,
    never indexed."""
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "private, no-store"
    return response


# ── Shared serializers ──────────────────────────────────────────────────

def _own_alive(node_id, user_id):
    if not node_id:
        return None
    node = db.session.get(Node, node_id)
    return node if ch.own_node(node, user_id) else None


def _shared_with_founder(inv, node):
    """*node* is a shared entry the cohort's founder reads now."""
    from backend.utils.circles import is_shared_entry, reader_ids
    cohort = db.session.get(Cohort, inv.cohort_id)
    if node is None or cohort is None or not cohort.owner_user_id:
        return False
    return (is_shared_entry(node)
            and cohort.owner_user_id in reader_ids(node.id))


def _shared_with_group(inv, node):
    """*node* is shared with the member's own cohort circle."""
    from backend.utils.circles import is_shared_entry
    if node is None or not is_shared_entry(node):
        return False
    circle = ch.cohort_circle_of(inv.user_id, inv.cohort_id)
    return circle is not None and NodeCircle.query.filter_by(
        node_id=node.id, circle_id=circle.id).first() is not None


def _open_paths(tip):
    """Where the existing pages open the interview thread at *tip*: the
    thread page (Text) and Voice mode."""
    if tip is None:
        return None
    pending = tip.node_type == "llm" and tip.llm_task_status in (
        "pending", "processing")
    if pending:
        return {"text": f"/node/{tip.id}?awaitLlm={tip.id}",
                "voice": f"/voice?resume={tip.id}"}
    return {"text": f"/node/{tip.id}", "voice": f"/voice?parent={tip.id}"}


def _interview_json(inv, user_id):
    root = ch.interview_root(inv, user_id)
    if root is None:
        return {"node_id": None, "started_at": iso_utc(inv.interview_started_at),
                "tip_node_id": None, "tip_is_ai": None, "tip_status": None,
                "open": None}
    tip_id, _path = ch.thread_path(root.id, user_id)
    tip = db.session.get(Node, tip_id) if tip_id else None
    return {
        "node_id": root.id,
        "started_at": iso_utc(inv.interview_started_at),
        "tip_node_id": tip.id if tip else None,
        "tip_is_ai": (tip.node_type == "llm") if tip else None,
        "tip_status": tip.llm_task_status if tip else None,
        "open": _open_paths(tip),
    }


def _report_json(inv, user_id):
    node = _own_alive(inv.report_node_id, user_id)
    return {
        "node_id": node.id if node else None,
        "task_status": inv.report_task_status,
        "drafted_at": iso_utc(inv.report_drafted_at),
        "sent_at": iso_utc(inv.report_sent_at),
        "skipped_at": iso_utc(inv.report_skipped_at),
        "shared_with_founder": _shared_with_founder(inv, node),
    }


def _intro_json(inv, user_id):
    node = _own_alive(inv.intro_node_id, user_id)
    return {
        "node_id": node.id if node else None,
        "task_status": inv.intro_task_status,
        "shared_with_group": _shared_with_group(inv, node),
    }


def _cohort_public_json(cohort):
    return {
        "name": cohort.name,
        "slug": cohort.slug,
        "about": cohort.about,
        "founder": ch.founder_name(cohort),
        "founder_username": ch.founder_username(cohort),
        "free_until": cohort.free_until.isoformat() if cohort.free_until else None,
    }


def _me_json(inv):
    uid = current_user.id
    cohort = db.session.get(Cohort, inv.cohort_id)
    return {
        "id": inv.id,
        "state": ch.invitation_state(inv),
        "cohort": _cohort_public_json(cohort),
        "letter": inv.get_letter(),
        # The call is offered once the report step is done.
        "booking_url": (cohort.booking_url
                        if ch.report_step_done(inv) else None),
        "claimed_at": iso_utc(inv.claimed_at),
        "call_done_at": iso_utc(inv.call_done_at),
        "admitted_at": iso_utc(inv.admitted_at),
        "declined_at": iso_utc(inv.declined_at),
        "closed_at": iso_utc(inv.closed_at),
        "interview": _interview_json(inv, uid),
        "report": _report_json(inv, uid),
        "intro": _intro_json(inv, uid),
    }


def _my_live_invitation():
    """The caller's live invitation, or raises CohortError."""
    inv = ch.invitation_for_user(current_user.id)
    if inv is None:
        raise ch.CohortError(ch.NO_INVITATION, 404, code="cohort_none")
    if not ch.is_live(inv):
        raise ch.CohortError(ch.NOT_OPEN, 409, code="cohort_not_open")
    return inv


def _error_answers(view):
    """A view that raises CohortError answers with it."""
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except ch.CohortError as e:
            db.session.rollback()
            return e.response()
    return wrapped


def _sync_if_member(inv):
    if inv.admitted_at:
        cohort = db.session.get(Cohort, inv.cohort_id)
        if cohort is not None:
            ch.sync_cohort_circles(cohort)


# ── The page at the link (no sign-in) ───────────────────────────────────

@invitation_bp.route("/<code>", methods=["GET"])
def get_invitation(code):
    """The letter page. Opening it is recorded once, as a time; nothing
    else about the visitor. Once accepted the letter is no longer served
    here (the person reads it through /api/cohort/me)."""
    inv = ch.invitation_by_code(code)
    if inv is None:
        return _no_store(_not_found()[0]), 404
    if inv.first_opened_at is None:
        inv.first_opened_at = _now()
        db.session.commit()
    cohort = db.session.get(Cohort, inv.cohort_id)
    state = ch.public_state(inv)
    body = {
        "cohort": {"name": cohort.name, "about": cohort.about},
        "founder": ch.founder_name(cohort),
        "state": state,
    }
    if state == "open":
        body["letter"] = inv.get_letter()
    return _no_store(jsonify(body)), 200


@invitation_bp.route("/<code>/decline", methods=["POST"])
def decline_invitation(code):
    """"Not for me". Asks nothing. Before the invitation is accepted
    anyone holding the link may decline it; afterwards only the account
    that accepted it."""
    inv = ch.invitation_by_code(code)
    if inv is None:
        return _no_store(_not_found()[0]), 404
    if inv.declined_at is not None:
        return _no_store(jsonify({"state": "declined"})), 200
    if inv.closed_at is not None:
        return _no_store(jsonify({"error": ch.NOT_OPEN,
                                  "code": "cohort_not_open"})), 409
    if inv.user_id is not None and not (
            current_user.is_authenticated and current_user.id == inv.user_id):
        return _no_store(jsonify({"error": ch.DECLINE_NOT_YOURS,
                                  "code": "cohort_not_yours"})), 403
    inv.declined_at = _now()
    _sync_if_member(inv)
    db.session.commit()
    return _no_store(jsonify({"state": "declined"})), 200


# ── The invited person ──────────────────────────────────────────────────

@cohort_bp.route("/me", methods=["GET"])
def me():
    inv = ch.invitation_for_user(current_user.id)
    owned = ch.owned_cohorts(current_user.id)
    return jsonify({
        "invitation": _me_json(inv) if inv is not None else None,
        "owner_of": [{"id": c.id, "name": c.name, "slug": c.slug}
                     for c in owned],
    }), 200


@cohort_bp.route("/claim", methods=["POST"])
@_error_answers
def claim():
    """Bind the invitation to the signed-in account. Idempotent for the
    account it is bound to."""
    data = request.get_json(silent=True) or {}
    inv = ch.invitation_by_code(data.get("code"))
    if inv is None:
        return _not_found()
    refused = ch.claim_refusal(inv, current_user)
    if refused is not None:
        raise refused
    if inv.user_id is None:
        # A conditional update: of two claims at once, one binds it.
        now = _now()
        bound = CohortInvitation.query.filter(
            CohortInvitation.id == inv.id,
            CohortInvitation.user_id.is_(None)).update(
            {CohortInvitation.user_id: current_user.id,
             CohortInvitation.claimed_at: now,
             CohortInvitation.first_opened_at: (
                 inv.first_opened_at or now)},
            synchronize_session=False)
        db.session.commit()
        db.session.refresh(inv)
        if not bound and inv.user_id != current_user.id:
            raise ch.CohortError(ch.TAKEN, 409, code="cohort_taken")
    return jsonify({"invitation": _me_json(inv)}), 200


@cohort_bp.route("/interview/start", methods=["POST"])
@_error_answers
def start_interview():
    """Start the interview, or answer the one already started. The thread
    is the person's own private agentic thread under a prompt node with
    the key cohort_interview (utils/cohort.compose_interview_prompt), and
    the AI writes the first message, on the person's own model."""
    from backend.utils.context_artifacts import attach_context_artifacts
    from backend.utils.llm_nodes import (
        AIUsageRefused, ReadOnlyModelRefused, TooManyReplies,
        ai_usage_refused_response, create_llm_placeholder,
        pick_model_for_generation, read_only_model_response)
    from backend.utils.placeholders import UserExportValidationError
    from backend.utils.privacy import account_allows_ai
    from backend.utils.spend import SpendCapExceeded, spend_cap_response
    from backend.utils.subscriptions import (
        SubscriptionRequired, subscription_required_response)
    from backend.utils.thread_size import ThreadTooLong, reply_skipped_fields

    inv = _my_live_invitation()
    user = current_user
    data = request.get_json(silent=True) or {}
    mode = data.get("mode") or "text"
    if mode not in ("text", "voice"):
        return jsonify({"error": "mode must be text or voice"}), 400
    if not account_allows_ai(user):
        return jsonify({"error": ch.AI_OFF_INTERVIEW,
                        "code": "ai_usage_none"}), 403
    if mode == "voice" and not user.has_voice_mode:
        return jsonify({"error": "Voice mode is not available on this "
                                 "account. Choose text.",
                        "code": "voice_unavailable"}), 403

    root = ch.interview_root(inv, user.id)
    if root is not None:
        alive = Node.query.filter(Node.parent_id == root.id,
                                  Node.deleted_at.is_(None)).all()
        if any(not (n.node_type == "llm" and n.llm_task_status == "failed")
               for n in alive):
            tip_id, _ = ch.thread_path(root.id, user.id)
            tip = db.session.get(Node, tip_id) if tip_id else None
            return jsonify({
                "created": False,
                "interview_node_id": root.id,
                "opening_node_id": None,
                "task_id": None,
                "mode": mode,
                "open": _open_paths(tip),
            }), 200

    cohort = db.session.get(Cohort, inv.cohort_id)
    ai_usage = user.default_ai_usage
    created = root is None
    if created:
        text = ch.compose_interview_prompt(user.id, cohort, inv, mode)
        version = ch.interview_prompt_version(user.id, text)
        root = Node(user_id=user.id, human_owner_id=user.id, parent_id=None,
                    node_type="user", privacy_level="private",
                    ai_usage=ai_usage)
        db.session.add(root)
        db.session.flush()
        attach_context_artifacts(root.id, user.id, prompt_record=version)
        inv.interview_node_id = root.id
        if inv.interview_started_at is None:
            inv.interview_started_at = _now()
    model_id = pick_model_for_generation(None if created else root, user)
    body = {"created": created, "interview_node_id": root.id, "mode": mode}
    try:
        llm_node, task_id = create_llm_placeholder(
            root.id, model_id, user.id, ai_usage=ai_usage,
            source_mode="voice" if mode == "voice" else "textmode")
    except SubscriptionRequired:
        db.session.rollback()
        return subscription_required_response(user)
    except SpendCapExceeded:
        db.session.rollback()
        return spend_cap_response(user)
    except AIUsageRefused as e:
        db.session.rollback()
        return ai_usage_refused_response(e)
    except ReadOnlyModelRefused as e:
        db.session.rollback()
        return read_only_model_response(e)
    except UserExportValidationError as e:
        # The person's own prompt asks for an export it may not have: the
        # thread is kept, the opening skipped, as Text mode does.
        db.session.commit()
        body.update(opening_node_id=None, task_id=None, llm_error=str(e),
                    open=None)
        return jsonify(body), 202
    except (ThreadTooLong, TooManyReplies) as e:
        db.session.commit()
        body.update(opening_node_id=None, task_id=None, open=None,
                    **reply_skipped_fields(e))
        return jsonify(body), 202
    db.session.commit()
    body.update(opening_node_id=llm_node.id, task_id=task_id,
                open=_open_paths(llm_node))
    return jsonify(body), 202


def _billed_call_refusal(user):
    """The answer for a draft the account may not have now: No AI, no AI
    replies (payments on, no paid set), or over its spend cap."""
    from backend.utils.privacy import account_allows_ai
    from backend.utils.spend import spend_cap_response, user_is_capped
    from backend.utils.subscriptions import (
        ai_replies_allowed, subscription_required_response)
    if not account_allows_ai(user):
        return jsonify({"error": ch.AI_OFF, "code": "ai_usage_none"}), 403
    if not ai_replies_allowed(user):
        return subscription_required_response(user)
    if user_is_capped(user):
        return spend_cap_response(user)
    return None


def _dispatch(task, inv, field):
    """Queue *task* for *inv* after its status is committed; a queue that
    can't be reached fails the status loudly."""
    setattr(inv, field, ch.PENDING)
    db.session.commit()
    try:
        task.delay(inv.id)
    except Exception:  # noqa: BLE001 - reported to the person
        import logging
        logging.getLogger(__name__).exception(
            "cohort draft for invitation %s could not be queued", inv.id)
        setattr(inv, field, ch.FAILED)
        db.session.commit()
        return jsonify({"error": "Loore could not start the draft. Try "
                                 "again in a moment."}), 503
    return None


@cohort_bp.route("/report/draft", methods=["POST"])
@_error_answers
def draft_report():
    """Draft the report from the person's own interview thread, into a new
    private node of theirs. Re-running replaces nothing: with a report
    already there it answers that one, unless ``regenerate`` asks for a
    new draft (only while the current one is unsent; the old one stays
    among their entries). ``blank``: no model call, a node with the
    report's headings for the person to write themselves."""
    from backend.tasks.cohort_report import draft_cohort_report
    inv = _my_live_invitation()
    user = current_user
    data = request.get_json(silent=True) or {}
    regenerate = data.get("regenerate") is True
    blank = data.get("blank") is True
    report = _own_alive(inv.report_node_id, user.id)
    if report is not None and _shared_with_founder(inv, report):
        raise ch.CohortError(ch.REPORT_SENT, 409, code="cohort_report_sent")
    if inv.report_task_status in ch.RUNNING:
        return jsonify({"report": _report_json(inv, user.id)}), 202
    if report is not None and not regenerate:
        return jsonify({"report": _report_json(inv, user.id)}), 200
    cohort = db.session.get(Cohort, inv.cohort_id)
    if blank:
        node = Node(user_id=user.id, human_owner_id=user.id, parent_id=None,
                    node_type="user", privacy_level="private",
                    ai_usage=user.default_ai_usage or "chat")
        node.set_content(ch.report_template(cohort))
        db.session.add(node)
        db.session.flush()
        inv.report_node_id = node.id
        inv.report_drafted_at = _now()
        inv.report_task_status = ch.DONE
        db.session.commit()
        return jsonify({"report": _report_json(inv, user.id)}), 201
    if ch.interview_root(inv, user.id) is None:
        raise ch.CohortError(ch.INTERVIEW_FIRST, 409,
                             code="cohort_interview_first")
    refused = _billed_call_refusal(user)
    if refused is not None:
        return refused
    failed = _dispatch(draft_cohort_report, inv, "report_task_status")
    if failed is not None:
        return failed
    return jsonify({"report": _report_json(inv, user.id)}), 202


@cohort_bp.route("/report", methods=["GET"])
@_error_answers
def get_report():
    """The draft's status and node id; its text is read and saved through
    the normal node routes (GET / PUT /api/nodes/<id>)."""
    inv = ch.invitation_for_user(current_user.id)
    if inv is None:
        raise ch.CohortError(ch.NO_INVITATION, 404, code="cohort_none")
    cohort = db.session.get(Cohort, inv.cohort_id)
    return jsonify({"report": _report_json(inv, current_user.id),
                    "founder": ch.founder_name(cohort),
                    "founder_username": ch.founder_username(cohort)}), 200


@cohort_bp.route("/report/send", methods=["POST"])
@_error_answers
def send_report():
    """"Send to <founder>": the report node is shared with exactly one
    named reader, the cohort's founder, through the Circles audience step
    every share takes (routes/nodes.share_entry). He finds it under
    Shared with you, with the usual note. The person takes it back as
    any share: by setting the entry to Only me."""
    from backend.routes.nodes import share_entry
    inv = _my_live_invitation()
    user = current_user
    report = _own_alive(inv.report_node_id, user.id)
    if report is None:
        raise ch.CohortError(ch.NO_REPORT, 409, code="cohort_no_report")
    if not (report.get_content() or "").strip():
        raise ch.CohortError(ch.REPORT_EMPTY, 400, code="cohort_report_empty")
    cohort = db.session.get(Cohort, inv.cohort_id)
    founder = (db.session.get(User, cohort.owner_user_id)
               if cohort.owner_user_id else None)
    if founder is None or founder.deleted_at is not None:
        raise ch.CohortError("This group has no founder to send it to.",
                             409, code="cohort_no_founder")
    answer, status = share_entry(report, user, {
        "circle_ids": [], "circle_usernames": [founder.username]})
    if status != 200:
        return answer, status
    inv.report_sent_at = _now()
    db.session.commit()
    return jsonify({"report": _report_json(inv, user.id),
                    "booking_url": cohort.booking_url}), 200


@cohort_bp.route("/report/skip", methods=["POST"])
@_error_answers
def skip_report():
    """"Send nothing": nothing is shared, and nothing more is asked."""
    inv = _my_live_invitation()
    report = _own_alive(inv.report_node_id, current_user.id)
    if report is not None and _shared_with_founder(inv, report):
        raise ch.CohortError(ch.REPORT_SENT_SKIP, 409,
                             code="cohort_report_sent")
    inv.report_skipped_at = _now()
    db.session.commit()
    cohort = db.session.get(Cohort, inv.cohort_id)
    return jsonify({"report": _report_json(inv, current_user.id),
                    "booking_url": cohort.booking_url}), 200


def _admitted_invitation():
    inv = _my_live_invitation()
    if not inv.admitted_at:
        raise ch.CohortError(ch.MEMBERS_ONLY, 403, code="cohort_members_only")
    return inv


@cohort_bp.route("/intro/draft", methods=["POST"])
@_error_answers
def draft_intro():
    """Draft a short introduction for the group from the current text of
    the member's report (their edited version, never the interview), into
    a new private node of theirs."""
    from backend.tasks.cohort_report import draft_cohort_intro
    from backend.utils.privacy import AI_ALLOWED
    inv = _admitted_invitation()
    user = current_user
    intro = _own_alive(inv.intro_node_id, user.id)
    if intro is not None and _shared_with_group(inv, intro):
        raise ch.CohortError("Your introduction is already shared with the "
                             "group.", 409, code="cohort_intro_shared")
    if inv.intro_task_status in ch.RUNNING:
        return jsonify({"intro": _intro_json(inv, user.id)}), 202
    report = _own_alive(inv.report_node_id, user.id)
    if report is None or not (report.get_content() or "").strip():
        raise ch.CohortError(ch.NO_REPORT_FOR_INTRO, 409,
                             code="cohort_no_report")
    if report.ai_usage not in AI_ALLOWED:
        raise ch.CohortError(ch.REPORT_AI_OFF, 403, code="ai_usage_none")
    refused = _billed_call_refusal(user)
    if refused is not None:
        return refused
    failed = _dispatch(draft_cohort_intro, inv, "intro_task_status")
    if failed is not None:
        return failed
    return jsonify({"intro": _intro_json(inv, user.id)}), 202


@cohort_bp.route("/intro", methods=["GET"])
@_error_answers
def get_intro():
    inv = _my_live_invitation()
    return jsonify({"intro": _intro_json(inv, current_user.id)}), 200


@cohort_bp.route("/intro", methods=["POST"])
@_error_answers
def set_intro():
    """Mark one of the member's own entries as their introduction. It must
    be shared with their group circle; ``share: true`` shares it there in
    the same call (added to whatever it is shared with already)."""
    from backend.models import NodeReader
    from backend.routes.nodes import share_entry
    inv = _admitted_invitation()
    user = current_user
    data = request.get_json(silent=True) or {}
    try:
        node_id = int(data.get("node_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "node_id is required"}), 400
    node = db.session.get(Node, node_id)
    if node is None or node.deleted_at is not None:
        return _not_found()
    if not ch.own_node(node, user.id) or node.is_system_prompt:
        raise ch.CohortError(ch.NOT_YOURS, 403, code="cohort_not_yours")
    cohort = db.session.get(Cohort, inv.cohort_id)
    circle = ch.cohort_circle_of(user.id, cohort.id)
    if circle is None:
        ch.sync_cohort_circles(cohort)
        db.session.commit()
        circle = ch.cohort_circle_of(user.id, cohort.id)
    if data.get("share") is True and not _shared_with_group(inv, node):
        from backend.utils.circles import is_shared_entry
        circle_ids, names = [circle.id], []
        if is_shared_entry(node):
            circle_ids += [cid for (cid,) in db.session.query(
                NodeCircle.circle_id).filter_by(node_id=node.id)
                if cid != circle.id]
            names = [n for (n,) in db.session.query(User.username).join(
                NodeReader, NodeReader.user_id == User.id).filter(
                NodeReader.node_id == node.id)]
        answer, status = share_entry(node, user, {
            "circle_ids": circle_ids, "circle_usernames": names})
        if status != 200:
            return answer, status
    if not _shared_with_group(inv, node):
        raise ch.CohortError(ch.INTRO_NOT_SHARED, 409,
                             code="cohort_intro_not_shared")
    inv.intro_node_id = node.id
    db.session.commit()
    return jsonify({"intro": _intro_json(inv, user.id)}), 200


@cohort_bp.route("/page", methods=["GET"])
@_error_answers
def group_page():
    """The group page, for its members (admitted, or the founder):
    ``?cohort=<slug>`` picks one; without it the caller's own."""
    from backend.routes.circles import _last_repliers, _title_and_preview
    from backend.utils.encryption import prefetch_deks
    from backend.utils.privacy import can_user_access_node
    uid = current_user.id
    slug = request.args.get("cohort")
    cohort = None
    if slug:
        cohort = Cohort.query.filter_by(slug=slug).first()
    else:
        inv = ch.invitation_for_user(uid)
        if ch.is_live(inv) and inv.admitted_at:
            cohort = db.session.get(Cohort, inv.cohort_id)
        else:
            owned = ch.owned_cohorts(uid)
            cohort = owned[0] if owned else None
    if cohort is None or not ch.is_member(cohort, uid):
        raise ch.CohortError(ch.MEMBERS_ONLY, 403, code="cohort_members_only")

    members = ch.member_ids(cohort)
    users = {u.id: u for u in User.query.filter(User.id.in_(members))}
    intros = {}
    for inv in CohortInvitation.query.filter(
            CohortInvitation.cohort_id == cohort.id,
            CohortInvitation.user_id.in_(members),
            CohortInvitation.intro_node_id.isnot(None)):
        node = db.session.get(Node, inv.intro_node_id)
        if (ch.own_node(node, inv.user_id) and _shared_with_group(inv, node)
                and can_user_access_node(node, uid)):
            intros[inv.user_id] = node.id
    owner_id = cohort.owner_user_id
    member_rows = sorted(
        ({"username": u.username, "intro_node_id": intros.get(u.id),
          "is_founder": u.id == owner_id} for u in users.values()),
        key=lambda m: (not m["is_founder"], m["username"].lower()))

    entries = ch.page_entries(cohort, uid)
    nodes = [n for n, _ in entries]
    prefetch_deks(n.content for n in nodes)
    repliers = _last_repliers([n.id for n in nodes])
    owners = {n.human_owner_id or n.user_id for n in nodes}
    names = dict(db.session.query(User.id, User.username).filter(
        User.id.in_(owners))) if owners else {}
    entry_rows = []
    for n, shared_at in entries:
        title, preview = _title_and_preview(n.get_content())
        entry_rows.append({
            "node_id": n.id,
            "author_username": names.get(n.human_owner_id or n.user_id),
            "shared_at": iso_utc(shared_at),
            "created_at": iso_utc(n.created_at),
            "title": title,
            "preview": preview,
            "last_reply_by": repliers.get(n.id),
        })
    circle = ch.cohort_circle_of(uid, cohort.id)
    return jsonify({
        "cohort": {
            "id": cohort.id, "slug": cohort.slug, "name": cohort.name,
            "about": cohort.about,
            "founder": ch.founder_name(cohort),
            "founder_username": ch.founder_username(cohort),
            "question": cohort.question,
            "question_set_at": iso_utc(cohort.question_set_at),
            "next_call_at": iso_utc(cohort.next_call_at),
            "call_url": cohort.call_url,
            "free_until": (cohort.free_until.isoformat()
                           if cohort.free_until else None),
        },
        "is_founder": uid == owner_id,
        "circle_id": circle.id if circle else None,
        "members": member_rows,
        "entries": entry_rows,
    }), 200


# ── The founder (admin routes) ──────────────────────────────────────────

def _owned_cohort_or_404(cohort_id):
    cohort = db.session.get(Cohort, cohort_id)
    if cohort is None or cohort.owner_user_id != current_user.id:
        raise ch.CohortError(ch.NOT_FOUND, 404)
    return cohort


def _admin_cohort_json(cohort):
    counts = {}
    for inv in CohortInvitation.query.filter_by(cohort_id=cohort.id):
        state = ch.invitation_state(inv)
        counts[state] = counts.get(state, 0) + 1
    return {
        "id": cohort.id,
        "slug": cohort.slug,
        "name": cohort.name,
        "about": cohort.about,
        "founder_name": cohort.founder_name,
        "founder": ch.founder_name(cohort),
        "booking_url": cohort.booking_url,
        "call_url": cohort.call_url,
        "next_call_at": iso_utc(cohort.next_call_at),
        "question": cohort.question,
        "question_set_at": iso_utc(cohort.question_set_at),
        "free_until": (cohort.free_until.isoformat()
                       if cohort.free_until else None),
        "created_at": iso_utc(cohort.created_at),
        "state_counts": counts,
    }


_TIMESTAMPS = ("created_at", "first_opened_at", "claimed_at",
               "interview_started_at", "report_drafted_at", "report_sent_at",
               "report_skipped_at", "call_done_at", "admitted_at",
               "declined_at", "closed_at")


def _admin_invitation_json(inv):
    """States, timestamps and the founder's own letter. No node id, no
    node text."""
    user = db.session.get(User, inv.user_id) if inv.user_id else None
    out = {
        "id": inv.id,
        "x_handle": inv.x_handle,
        "username": user.username if user else None,
        "state": ch.invitation_state(inv),
        "link": f"/invitation/{inv.code}",
        "letter": inv.get_letter(),
    }
    for name in _TIMESTAMPS:
        out[name] = iso_utc(getattr(inv, name))
    return out


class _BadField(Exception):
    pass


def _text(data, key, limit, required=False):
    value = data.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise _BadField(f"{key} is required.")
        return None
    if not isinstance(value, str):
        raise _BadField(f"{key} must be text.")
    value = value.strip()
    if len(value) > limit:
        raise _BadField(f"{key} can have up to {limit:,} characters.")
    return value


def _url(data, key):
    value = _text(data, key, MAX_URL_CHARS)
    if value is not None and not value.startswith(("https://", "http://")):
        raise _BadField(f"{key} must start with https://")
    return value


def _when(data, key):
    value = data.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise _BadField(f"{key} must be an ISO date and time.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _BadField(f"{key} must be an ISO date and time.")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _day(data, key):
    value = data.get(key)
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise _BadField(f"{key} must be a date (YYYY-MM-DD).")


_COHORT_FIELDS = ("name", "about", "founder_name", "booking_url", "call_url",
                  "next_call_at", "question", "free_until")


def _apply_cohort_fields(cohort, data, creating=False):
    """Set the fields *data* carries (all of them when creating). Raises
    _BadField."""
    for key in _COHORT_FIELDS:
        if not creating and key not in data:
            continue
        if key == "name":
            name = " ".join((_text(data, "name", MAX_NAME_CHARS,
                                   required=True) or "").split())
            cohort.name = name
        elif key == "about":
            cohort.about = _text(data, key, MAX_ABOUT_CHARS)
        elif key == "founder_name":
            cohort.founder_name = _text(data, key, MAX_FOUNDER_NAME_CHARS)
        elif key in ("booking_url", "call_url"):
            setattr(cohort, key, _url(data, key))
        elif key == "next_call_at":
            cohort.next_call_at = _when(data, key)
        elif key == "question":
            question = _text(data, key, MAX_QUESTION_CHARS)
            if question != cohort.question:
                cohort.question = question
                cohort.question_set_at = _now() if question else None
        elif key == "free_until":
            cohort.free_until = _day(data, key)


@cohort_admin_bp.route("", methods=["GET"])
def admin_list_cohorts():
    return jsonify({"cohorts": [_admin_cohort_json(c) for c in
                                ch.owned_cohorts(current_user.id)]}), 200


@cohort_admin_bp.route("", methods=["POST"])
def admin_create_cohort():
    data = request.get_json(silent=True) or {}
    cohort = Cohort(owner_user_id=current_user.id)
    try:
        _apply_cohort_fields(cohort, data, creating=True)
        slug = _text(data, "slug", 64)
    except _BadField as e:
        return jsonify({"error": str(e)}), 400
    if slug is not None:
        import re
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
            return jsonify({"error": "slug can hold a-z, 0-9 and -."}), 400
        if Cohort.query.filter_by(slug=slug).first() is not None:
            return jsonify({"error": "That slug is taken."}), 409
    cohort.slug = slug or ch.slugify(cohort.name)
    db.session.add(cohort)
    db.session.commit()
    return jsonify({"cohort": _admin_cohort_json(cohort)}), 201


@cohort_admin_bp.route("/<int:cohort_id>", methods=["GET"])
@_error_answers
def admin_get_cohort(cohort_id):
    cohort = _owned_cohort_or_404(cohort_id)
    return jsonify({"cohort": _admin_cohort_json(cohort)}), 200


@cohort_admin_bp.route("/<int:cohort_id>", methods=["PUT"])
@_error_answers
def admin_update_cohort(cohort_id):
    cohort = _owned_cohort_or_404(cohort_id)
    data = request.get_json(silent=True) or {}
    old_name = cohort.name
    try:
        _apply_cohort_fields(cohort, data)
    except _BadField as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    if cohort.name != old_name:
        # The circles Loore keeps for the group carry its name.
        ch.sync_cohort_circles(cohort)
    db.session.commit()
    return jsonify({"cohort": _admin_cohort_json(cohort)}), 200


@cohort_admin_bp.route("/<int:cohort_id>/invitations", methods=["GET"])
@_error_answers
def admin_list_invitations(cohort_id):
    cohort = _owned_cohort_or_404(cohort_id)
    rows = (CohortInvitation.query.filter_by(cohort_id=cohort.id)
            .order_by(CohortInvitation.created_at.asc(),
                      CohortInvitation.id.asc()).all())
    return jsonify({"invitations": [_admin_invitation_json(i)
                                    for i in rows]}), 200


@cohort_admin_bp.route("/<int:cohort_id>/invitations", methods=["POST"])
@_error_answers
def admin_create_invitation(cohort_id):
    """An invitation for an X handle, with the founder's letter. The
    handle is approved through the admin whitelist's own function, so
    the person's X sign-in finds or creates an approved account. Loore
    sends nothing: the founder sends the link himself. Without
    ``x_handle`` any signed-in account holding the link may accept it
    (for trying it locally)."""
    cohort = _owned_cohort_or_404(cohort_id)
    data = request.get_json(silent=True) or {}
    try:
        letter = _text(data, "letter", MAX_LETTER_CHARS, required=True)
    except _BadField as e:
        return jsonify({"error": str(e)}), 400
    handle = ch.clean_handle(data.get("x_handle")
                             if isinstance(data.get("x_handle"), str) else "")
    x_id = None
    if handle:
        if CohortInvitation.query.filter_by(
                cohort_id=cohort.id, x_handle=handle).first() is not None:
            return jsonify({"error": f"There is already an invitation for "
                                     f"@{handle} in this group."}), 409
        _user, x_id = ch.prepare_invited_account(
            handle, x_lookup=data.get("x_lookup") is True,
            payer_id=current_user.id)
    inv = CohortInvitation(cohort_id=cohort.id, code=ch.new_code(),
                           x_handle=handle or None, x_id=x_id)
    inv.set_letter(letter)
    db.session.add(inv)
    db.session.commit()
    body = _admin_invitation_json(inv)
    return jsonify({"invitation": body, "link": body["link"]}), 201


def _owned_invitation_or_404(cohort_id, invitation_id):
    cohort = _owned_cohort_or_404(cohort_id)
    inv = db.session.get(CohortInvitation, invitation_id)
    if inv is None or inv.cohort_id != cohort.id:
        raise ch.CohortError(ch.NOT_FOUND, 404)
    return cohort, inv


@cohort_admin_bp.route("/<int:cohort_id>/invitations/<int:invitation_id>",
                       methods=["PUT"])
@_error_answers
def admin_update_invitation(cohort_id, invitation_id):
    _cohort, inv = _owned_invitation_or_404(cohort_id, invitation_id)
    if not ch.is_live(inv):
        return jsonify({"error": ch.NOT_OPEN, "code": "cohort_not_open"}), 409
    if inv.user_id is not None:
        return jsonify({"error": "The letter can't be changed after the "
                                 "invitation was accepted.",
                        "code": "cohort_claimed"}), 409
    data = request.get_json(silent=True) or {}
    try:
        letter = _text(data, "letter", MAX_LETTER_CHARS, required=True)
    except _BadField as e:
        return jsonify({"error": str(e)}), 400
    inv.set_letter(letter)
    db.session.commit()
    return jsonify({"invitation": _admin_invitation_json(inv)}), 200


def _needs_claimed_live(inv):
    if not ch.is_live(inv):
        return jsonify({"error": ch.NOT_OPEN, "code": "cohort_not_open"}), 409
    if inv.user_id is None:
        return jsonify({"error": "Nobody has accepted this invitation yet.",
                        "code": "cohort_not_claimed"}), 409
    return None


@cohort_admin_bp.route(
    "/<int:cohort_id>/invitations/<int:invitation_id>/call_done",
    methods=["POST"])
@_error_answers
def admin_call_done(cohort_id, invitation_id):
    _cohort, inv = _owned_invitation_or_404(cohort_id, invitation_id)
    refused = _needs_claimed_live(inv)
    if refused:
        return refused
    if inv.call_done_at is None:
        inv.call_done_at = _now()
        db.session.commit()
    return jsonify({"invitation": _admin_invitation_json(inv)}), 200


@cohort_admin_bp.route(
    "/<int:cohort_id>/invitations/<int:invitation_id>/admit",
    methods=["POST"])
@_error_answers
def admin_admit(cohort_id, invitation_id):
    """Admit: the person joins the group. Every member and the founder
    then have a circle with all the other members (sync_cohort_circles)."""
    cohort, inv = _owned_invitation_or_404(cohort_id, invitation_id)
    refused = _needs_claimed_live(inv)
    if refused:
        return refused
    if inv.admitted_at is None:
        inv.admitted_at = _now()
        db.session.flush()
        ch.sync_cohort_circles(cohort)
        db.session.commit()
    return jsonify({"invitation": _admin_invitation_json(inv)}), 200


@cohort_admin_bp.route(
    "/<int:cohort_id>/invitations/<int:invitation_id>/close",
    methods=["POST"])
@_error_answers
def admin_close(cohort_id, invitation_id):
    """Close: the invitation ends (a person not admitted, or a member who
    leaves). A member is taken out of every circle of the group and their
    own group circle goes. Loore tells nobody."""
    cohort, inv = _owned_invitation_or_404(cohort_id, invitation_id)
    if inv.closed_at is None and inv.declined_at is None:
        inv.closed_at = _now()
        db.session.flush()
        if inv.admitted_at:
            ch.sync_cohort_circles(cohort)
        db.session.commit()
    return jsonify({"invitation": _admin_invitation_json(inv)}), 200
