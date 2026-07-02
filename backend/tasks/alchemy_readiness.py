"""Celery task: Alchemical Mode readiness/safety check.

The OUR-side half of the double gate (docs/FOUR-FEATURE-ECOSYSTEM.md,
"Alchemical Mode"): a separate LLM process reads roughly the last two
months of a user's writing and judges it against the safety/readiness
checklist in prompts/alchemy_readiness.txt. Only users judged ready are
ever OFFERED the mode; opting in stays a separate, explicit user action.

Dispatched manually (admin endpoint / flask shell) — deliberately not on
a beat schedule until the checklist has been reviewed and the rollout
policy decided.
"""
import json
import os
from datetime import datetime, timedelta

from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app
from backend.models import User, AlchemyState, APICostLog
from backend.extensions import db
from backend.llm_providers import LLMProvider, PromptTooLongError
from backend.utils.tokens import reduce_export_tokens
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.cost import calculate_llm_cost_microdollars

logger = get_task_logger(__name__)

READINESS_WINDOW_DAYS = 60


def _load_readiness_prompt():
    # Deliberately NOT user-overridable — a user editing their own safety
    # gate would defeat it.
    path = os.path.join(flask_app.root_path, "prompts",
                        "alchemy_readiness.txt")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _parse_verdict(text):
    """Parse the model's JSON verdict, tolerating code fences. Any parse
    failure resolves to not-ready (the gate fails closed)."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        verdict = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return {"ready": False,
                "rationale": "Verdict unparseable — gate fails closed.",
                "flags": ["unparseable-verdict"]}
    return {
        "ready": bool(verdict.get("ready", False)),
        "rationale": str(verdict.get("rationale", "")),
        "flags": list(verdict.get("flags", []) or []),
    }


@celery.task(name="backend.tasks.alchemy_readiness.check_user_readiness")
def check_user_readiness(user_id):
    """Run the readiness check for one user and persist the verdict."""
    with flask_app.app_context():
        if not flask_app.config.get("ALCHEMY_V1", False):
            logger.info("ALCHEMY_V1 off — skipping readiness check")
            return

        user = User.query.get(user_id)
        if user is None:
            logger.warning(f"Alchemy readiness: no user {user_id}")
            return

        state = AlchemyState.query.filter_by(user_id=user_id).first()
        if state is None:
            state = AlchemyState(user_id=user_id)
            db.session.add(state)
            db.session.flush()

        default_model = flask_app.config.get(
            "DEFAULT_LLM_MODEL", "claude-opus-4.6")
        model_id = default_model

        cutoff = datetime.utcnow() - timedelta(days=READINESS_WINDOW_DAYS)
        from backend.routes.export_data import (
            build_user_export_content as _build_export,
        )
        export_result = _build_export(
            user,
            max_tokens=None,
            filter_ai_usage=True,
            created_after=cutoff,
            chronological_order=True,
            return_metadata=True,
            collapse_artifacts=True,
        )
        if not export_result or not (export_result.get("content") or "").strip():
            state.readiness_status = "not_ready"
            state.set_rationale(
                "Not enough recent writing to assess readiness.")
            state.checked_at = datetime.utcnow()
            state.checked_by = model_id
            db.session.commit()
            logger.info(f"Alchemy readiness user {user_id}: insufficient data")
            return

        prompt_template = _load_readiness_prompt()
        api_keys = get_api_keys_for_usage(flask_app.config, "chat")

        recent_data = export_result["content"]
        MAX_RETRIES = 2
        max_data_tokens = None
        for attempt in range(MAX_RETRIES + 1):
            if max_data_tokens is not None:
                export_result = _build_export(
                    user,
                    max_tokens=max_data_tokens,
                    filter_ai_usage=True,
                    created_after=cutoff,
                    chronological_order=True,
                    return_metadata=True,
                    collapse_artifacts=True,
                )
                if not export_result:
                    logger.warning(
                        f"Alchemy readiness user {user_id}: "
                        "no data after truncation")
                    return
                recent_data = export_result["content"]
            prompt_text = (
                f"{prompt_template}\n\n"
                f"--- USER'S RECENT WRITING (last "
                f"{READINESS_WINDOW_DAYS} days) ---\n\n{recent_data}"
            )
            messages = [{
                "role": "user",
                "content": [{"type": "text", "text": prompt_text}],
            }]
            try:
                response = LLMProvider.get_completion(
                    model_id, messages, api_keys)
                break
            except PromptTooLongError as e:
                if attempt == MAX_RETRIES:
                    raise
                max_data_tokens = reduce_export_tokens(
                    max_data_tokens, e.actual_tokens, e.max_tokens,
                    export_content=recent_data,
                )
                logger.warning(
                    f"Alchemy readiness prompt too long "
                    f"({e.actual_tokens} > {e.max_tokens}); retrying "
                    f"with max_data_tokens={max_data_tokens}")

        input_tokens = response.get("input_tokens", 0)
        output_tokens = response.get("output_tokens", 0)
        cost = calculate_llm_cost_microdollars(
            model_id, input_tokens, output_tokens)
        db.session.add(APICostLog(
            user_id=user_id,
            model_id=model_id,
            request_type="alchemy_readiness",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microdollars=cost,
        ))

        verdict = _parse_verdict(response["content"])
        state.readiness_status = "ready" if verdict["ready"] else "not_ready"
        rationale = verdict["rationale"]
        if verdict["flags"]:
            rationale += f" [flags: {', '.join(verdict['flags'])}]"
        state.set_rationale(rationale)
        state.checked_at = datetime.utcnow()
        state.checked_by = model_id
        db.session.commit()
        logger.info(
            f"Alchemy readiness user {user_id}: {state.readiness_status}")
