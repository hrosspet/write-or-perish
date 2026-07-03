"""LLM-drafted poll answers (#207 polling extension).

Runs only after the user's explicit opt-in 1 (draft_requested_at is their
consent timestamp). The draft is written from the user's AI-readable
derived context — profile, recent context, intentions — never raw nodes,
and stays PRIVATE until the user separately opts in to send it.
"""
import logging

from backend.celery_app import celery, flask_app
from backend.extensions import db
from backend.models import (
    PollResponse, UserProfile, UserRecentContext, UserArtifact, APICostLog,
)
from backend.llm_providers import LLMProvider
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.cost import calculate_llm_cost_microdollars

logger = logging.getLogger(__name__)

DRAFT_SYSTEM_PROMPT = """\
You are drafting an answer ON BEHALF OF a Loore user to a question the \
developer asked the community. You have access to the user's profile and \
recent context below.

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


def _build_user_context(user):
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


@celery.task(bind=True)
def draft_poll_response(self, response_id: int):
    with flask_app.app_context():
        resp = PollResponse.query.get(response_id)
        if resp is None:
            logger.warning("PollResponse %s vanished", response_id)
            return
        if resp.status != "drafting":
            logger.info(
                "PollResponse %s no longer drafting (%s) — skipping",
                response_id, resp.status)
            return

        user = resp.user
        try:
            model_id = (
                user.preferred_model
                or flask_app.config.get("DEFAULT_LLM_MODEL")
            )
            if model_id not in flask_app.config["SUPPORTED_MODELS"]:
                raise ValueError(f"Unsupported model: {model_id}")

            context = _build_user_context(user)
            if not context:
                # Nothing to draft from — fail soft so the UI offers manual.
                resp.status = "draft_failed"
                db.session.commit()
                return

            messages = [
                {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"{context}\n\n---\n\n"
                    f"The developer asks:\n\n{resp.poll.question}\n\n"
                    f"Draft the user's answer."
                )},
            ]
            api_keys = get_api_keys_for_usage(flask_app.config, "chat")
            response = LLMProvider.get_completion(
                model_id, messages, api_keys, max_tokens=1000)

            input_tokens = response.get("input_tokens", 0)
            output_tokens = response.get("output_tokens", 0)
            db.session.add(APICostLog(
                user_id=user.id, model_id=model_id,
                request_type="poll_draft",
                input_tokens=input_tokens, output_tokens=output_tokens,
                cost_microdollars=calculate_llm_cost_microdollars(
                    model_id, input_tokens, output_tokens),
            ))

            resp.set_content(response["content"].strip())
            resp.generated_by = model_id
            resp.status = "draft"
            db.session.commit()
            logger.info(
                "Drafted poll response %s for user %s (%s)",
                response_id, user.id, model_id)
        except Exception:
            db.session.rollback()
            resp = PollResponse.query.get(response_id)
            if resp is not None and resp.status == "drafting":
                resp.status = "draft_failed"
                db.session.commit()
            logger.exception(
                "Poll draft failed for response %s", response_id)
