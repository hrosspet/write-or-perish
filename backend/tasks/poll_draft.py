"""LLM-drafted poll answers (#207 polling extension).

Runs only after the user's explicit opt-in 1 (draft_requested_at is their
consent timestamp). What the model may read is chosen by the admin PER
POLL and shown to the user before they opt in:

  * data_source='derived'       — profile + recent summary + intentions
  * data_source='recent_window' — the most recent raw writing that fits
                                  the model's context window (AI-readable
                                  nodes only)

Drafts ride the provider Batch API (~50% cheaper; they're async anyway):
`submit_poll_draft` fires on opt-in, `collect_poll_draft_batches` (beat,
every minute) saves finished drafts. Costs are attributed to the polls
SYSTEM account with request_ref="poll:<id>" — never to the answering
user. Drafts stay PRIVATE until the user separately opts in to send.
"""
import logging
from datetime import datetime

from backend.celery_app import celery, flask_app
from backend.extensions import db
from backend.models import (
    PollResponse, PollDraftBatchJob, UserProfile, UserRecentContext,
    UserArtifact, APICostLog,
)
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.cost import calculate_llm_cost_microdollars
from backend.utils.llm_batch import (
    batch_submit, batch_check_and_collect, apply_batch_key_override,
)
from backend.utils.tokens import approximate_token_count

logger = logging.getLogger(__name__)

MAX_DRAFT_TOKENS = 1000
# Headroom for the system prompt, question and formatting when filling
# the context window with user data.
WINDOW_OVERHEAD_TOKENS = 2000

DRAFT_SYSTEM_PROMPT = """\
You are drafting an answer ON BEHALF OF a Loore user to a question the \
developer asked the community. You have access to the user's context \
below.

Rules:
- Write in first person, in the user's voice, grounded ONLY in what the \
context shows about their actual experience with Loore.
- Be honest and specific; concrete observations beat pleasantries.
- Keep it short: a few sentences to two short paragraphs.
- If the context doesn't contain enough to answer meaningfully, say so \
plainly (e.g. "I haven't used that part much yet") rather than inventing.
- The user will review and edit this draft before deciding whether to \
send it. Output ONLY the draft answer — no preamble, no headings.
"""


def _derived_context(user):
    parts = []
    profile = UserProfile.query.filter_by(user_id=user.id).order_by(
        UserProfile.created_at.desc()).first()
    if profile:
        parts.append("## User profile\n\n" + profile.get_content())
    recent = UserRecentContext.query.filter_by(user_id=user.id).order_by(
        UserRecentContext.created_at.desc()).first()
    if recent:
        parts.append("## Recent context\n\n" + recent.get_content())
    intentions = UserArtifact.latest_for(user.id, "intentions")
    if intentions:
        parts.append("## Intentions\n\n" + intentions.get_content())
    return "\n\n".join(parts)


def _recent_window_context(user, model_cfg):
    """Most recent AI-readable writing that fits the model's context
    window (minus draft output + prompt overhead)."""
    from backend.tasks.exports import build_user_export_content
    budget = (model_cfg.get("context_window", 200000)
              - MAX_DRAFT_TOKENS - WINDOW_OVERHEAD_TOKENS)
    export = build_user_export_content(
        user, max_tokens=budget, filter_ai_usage=True,
        include_strategy="engaged_threads")
    if not export:
        return ""
    return "## The user's recent writing\n\n" + export


def _build_request(resp):
    """Build the batch request dict for one PollResponse, or None when
    there's no context to draft from."""
    poll = resp.poll
    user = resp.user
    model_id = poll.model_id or flask_app.config.get("DEFAULT_LLM_MODEL")
    model_cfg = flask_app.config["SUPPORTED_MODELS"].get(model_id)
    if model_cfg is None:
        raise ValueError(f"Unsupported model: {model_id}")

    if poll.data_source == "recent_window":
        context = _recent_window_context(user, model_cfg)
    else:
        context = _derived_context(user)
    if not context.strip():
        return None

    messages = [
        {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"{context}\n\n---\n\n"
            f"The developer asks:\n\n{poll.question}\n\n"
            f"Draft the user's answer."
        )},
    ]
    logger.info(
        "Poll draft request for response %s: poll %s, model %s, "
        "source %s, ~%s ctx tokens", resp.id, poll.id, model_id,
        poll.data_source, approximate_token_count(context))
    return {
        "custom_id": f"poll-draft-{resp.id}",
        "model_id": model_id,
        "api_model": model_cfg["api_model"],
        "messages": messages,
        "max_tokens": MAX_DRAFT_TOKENS,
        "provider": model_cfg["provider"],
    }


def _fail_response(resp):
    if resp is not None and resp.status == "drafting":
        resp.status = "draft_failed"
        db.session.commit()


def _submit_poll_draft(response_id, task_id=None):
    """Core of submit (plain function so tests can call it without the
    Celery machinery)."""
    resp = PollResponse.query.get(response_id)
    if resp is None or resp.status != "drafting":
        logger.info("Poll draft %s skipped (gone or not drafting)",
                    response_id)
        return
    try:
        request = _build_request(resp)
        if request is None:
            # Nothing to draft from — fail soft so the UI offers manual.
            _fail_response(resp)
            return

        keys = apply_batch_key_override(
            get_api_keys_for_usage(flask_app.config, "chat"),
            flask_app.config)
        provider = request.pop("provider")
        batch_ids = batch_submit({provider: [request]}, keys, "poll")
        if not batch_ids:
            raise RuntimeError("batch_submit returned no batch ids")

        for provider_key, batch_id in batch_ids.items():
            db.session.add(PollDraftBatchJob(
                provider_key=provider_key, batch_id=batch_id,
                items=[{
                    "custom_id": request["custom_id"],
                    "response_id": resp.id,
                    "poll_id": resp.poll_id,
                    "model_id": request["model_id"],
                }],
            ))
        resp.draft_task_id = task_id
        db.session.commit()
    except Exception:
        db.session.rollback()
        _fail_response(PollResponse.query.get(response_id))
        logger.exception(
            "Poll draft submit failed for response %s", response_id)


@celery.task(bind=True)
def submit_poll_draft(self, response_id: int):
    """Opt-in 1 fired: submit a single-item provider batch for this
    response. (Single-item batches still get the 50% batch discount.)"""
    with flask_app.app_context():
        _submit_poll_draft(response_id, task_id=self.request.id)


def _save_draft_result(item, result):
    """Save one collected batch result to its PollResponse and log the
    cost to the polls system account."""
    from backend.utils.system_accounts import get_poll_system_user

    resp = PollResponse.query.get(item["response_id"])
    if resp is None or resp.status != "drafting":
        logger.info("Draft result for response %s dropped (status %s)",
                    item["response_id"],
                    resp.status if resp else "gone")
        return

    system_user = get_poll_system_user()
    db.session.add(APICostLog(
        user_id=system_user.id, model_id=item["model_id"],
        request_type="poll_draft",
        request_ref=f"poll:{item['poll_id']}",
        input_tokens=result["input_tokens"],
        output_tokens=result["output_tokens"],
        cost_microdollars=calculate_llm_cost_microdollars(
            item["model_id"], result["input_tokens"],
            result["output_tokens"], batch=True),
    ))
    resp.set_content(result["content"].strip())
    resp.generated_by = item["model_id"]
    resp.status = "draft"
    logger.info("Saved poll draft for response %s (poll %s)",
                resp.id, item["poll_id"])


def _collect_poll_draft_batches():
    """Core of collect (plain function so tests can call it without the
    Celery machinery)."""
    jobs = PollDraftBatchJob.query.filter_by(status="pending").all()
    if not jobs:
        return

    keys = apply_batch_key_override(
        get_api_keys_for_usage(flask_app.config, "chat"),
        flask_app.config)

    for job in jobs:
        try:
            results, still_pending, _ = batch_check_and_collect(
                {job.provider_key: job.batch_id}, keys)
        except Exception:
            logger.exception("Collect failed for poll batch %s",
                             job.batch_id)
            continue
        if still_pending:
            continue

        # Batch ended: every item either has a result or failed.
        for item in job.items:
            result = results.get(item["custom_id"])
            if result:
                _save_draft_result(item, result)
            else:
                logger.warning(
                    "Poll draft item %s missing from batch %s",
                    item["custom_id"], job.batch_id)
                _fail_response(
                    PollResponse.query.get(item["response_id"]))
        job.status = "collected"
        job.collected_at = datetime.utcnow()
        db.session.commit()


@celery.task
def collect_poll_draft_batches():
    """Beat task: retrieve finished poll-draft batches and save drafts.
    No-op when nothing is pending."""
    with flask_app.app_context():
        _collect_poll_draft_batches()


# Referenced by Poll model docs; kept here so route code has one import
# point for both entry points.
__all__ = ["submit_poll_draft", "collect_poll_draft_batches"]
