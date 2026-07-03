"""External-references digest (quote-as-response, #155/#208).

Rebuilds the ``external_digest`` UserArtifact — a compact topic map of the
user's imported external content (tweets, bookmarks). It appears in the
agentic artifacts index like any other artifact, so the model can pull it
with read_artifact to learn what MIGHT exist in the saved corpus before
deciding to search it — and it costs nothing in contexts that never look
(most Loore contexts don't need external content).

Regenerated after each import/fetch/sync that created new items. The
digest is a single direct LLM call over a capped, compact rendering of
the corpus; moving this to the Batch API (50% pricing) is a follow-up —
flagged in the PR, not silently skipped.
"""
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app
from backend.extensions import db
from backend.models import APICostLog, ExternalItem, User, UserArtifact
from backend.llm_providers import LLMProvider
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.cost import calculate_llm_cost_microdollars

logger = get_task_logger(__name__)

DIGEST_KIND = "external_digest"
DIGEST_TITLE = "Saved References Digest"
DIGEST_DESCRIPTION = (
    "Topic map of the tweets and bookmarks {name} saved elsewhere — read "
    "this before searching their saved references, to see what might be "
    "there."
)

# Corpus caps for the digest prompt. Most recent items first; each item
# rendered compactly. ~1500 items x ~300 chars ≈ 450k chars worst case,
# so the total char cap is the binding constraint in practice.
MAX_DIGEST_ITEMS = 1500
ITEM_SNIPPET_CHARS = 280
MAX_CORPUS_CHARS = 300_000

DIGEST_PROMPT = """\
Below is a collection of tweets and bookmarks a user saved on external \
platforms — content they marked as worth returning to. Write a compact \
digest of this corpus as a topic map:

- Cluster the items into topics (roughly 5-15, whatever the corpus \
actually supports). For each topic give a short heading, a one-sentence \
description of what's there, an approximate item count, and 2-4 \
representative items (author handle + a few words each).
- Note the overall shape at the top: total items, dominant themes, rough \
time range. Use the exact counts from the corpus header — you cannot \
reliably count the list yourself, so don't estimate how many items you \
were shown.
- This digest is read by an AI assistant deciding whether the user's \
saved references might contain something relevant to a live \
conversation — write for that reader: dense, factual, scannable. No \
preamble, no advice, markdown headings only.

The corpus:

{corpus}
"""


@celery.task(name='backend.tasks.external_digest.rebuild_external_digest',
             bind=True, max_retries=2, default_retry_delay=60)
def rebuild_external_digest(self, user_id):
    with flask_app.app_context():
        user = User.query.get(user_id)
        if user is None:
            return {"status": "no_user"}

        items = ExternalItem.query.filter_by(user_id=user_id).order_by(
            ExternalItem.posted_at.desc().nullslast(),
            ExternalItem.fetched_at.desc(),
        ).limit(MAX_DIGEST_ITEMS).all()
        if not items:
            return {"status": "no_items"}

        total = ExternalItem.query.filter_by(user_id=user_id).count()

        lines = []
        used = 0
        truncated = False
        for item in items:
            text = (item.get_content() or "").strip().replace("\n", " ")
            if not text:
                continue
            snippet = text[:ITEM_SNIPPET_CHARS]
            stamp = (item.posted_at.strftime("%Y-%m-%d")
                     if item.posted_at else "?")
            author = item.author_handle or item.source
            line = f"- @{author} ({stamp}): {snippet}"
            if used + len(line) > MAX_CORPUS_CHARS:
                truncated = True
                break
            lines.append(line)
            used += len(line)

        # State the count explicitly — the model can't count a long list
        # and will otherwise invent a "sample of ~N" caveat.
        if truncated or len(lines) < total:
            header = (f"The corpus holds {total} saved items; the "
                      f"{len(lines)} most recent are listed below "
                      f"({total - len(lines)} older items not shown).")
        else:
            header = (f"The corpus holds {total} saved items; ALL of them "
                      f"are listed below.")
        corpus = header + "\n" + "\n".join(lines)
        prompt_text = DIGEST_PROMPT.replace("{corpus}", corpus)

        default_model = flask_app.config.get("LLM_NAME")
        model_id = user.preferred_model or default_model
        if model_id not in flask_app.config.get("SUPPORTED_MODELS", {}):
            model_id = default_model

        api_keys = get_api_keys_for_usage(flask_app.config, 'chat')
        messages = [{
            "role": "user",
            "content": [{"type": "text", "text": prompt_text}],
        }]
        try:
            response = LLMProvider.get_completion(
                model_id, messages, api_keys)
        except Exception as exc:
            logger.warning(
                "External digest generation failed for user %s: %s",
                user_id, exc)
            raise self.retry(exc=exc)

        digest_text = (response.get("content") or "").strip()
        if not digest_text:
            return {"status": "empty_response"}

        input_tokens = response.get("input_tokens", 0)
        output_tokens = response.get("output_tokens", 0)
        cost = calculate_llm_cost_microdollars(
            model_id, input_tokens, output_tokens)
        db.session.add(APICostLog(
            user_id=user_id,
            model_id=model_id,
            request_type="external_digest",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microdollars=cost,
        ))

        previous = UserArtifact.latest_for(user_id, DIGEST_KIND)
        artifact = UserArtifact(
            user_id=user_id,
            kind=DIGEST_KIND,
            title=(previous.title if previous else DIGEST_TITLE),
            description=(
                previous.description if previous and previous.description
                else DIGEST_DESCRIPTION.replace(
                    "{name}", user.username or "the user")),
            generated_by=model_id,
            tokens_used=input_tokens + output_tokens,
            # Respect a manual opt-out on the previous version; otherwise
            # mirror the user's global default (recent_context precedent).
            ai_usage=(previous.ai_usage if previous
                      else user.default_ai_usage),
        )
        artifact.set_content(digest_text)
        db.session.add(artifact)
        db.session.commit()
        logger.info(
            "External digest rebuilt for user %s (%d items, model %s)",
            user_id, total, model_id)
        return {"status": "ok", "artifact_id": artifact.id,
                "items": total}
