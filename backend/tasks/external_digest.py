"""External-references digest (quote-as-response, #155/#208).

Rebuilds the ``external_digest`` UserArtifact — a compact topic map of the
user's imported external content (tweets, bookmarks). It appears in the
agentic artifacts index like any other artifact, so the model can pull it
with read_artifact to learn what MIGHT exist in the saved corpus before
deciding to search it — and it costs nothing in contexts that never look
(most Loore contexts don't need external content).

Rebuilt ONCE A NIGHT, in the user's own night, and only when their
corpus changed since the last rebuild. The digest is a single direct LLM
call over a capped, compact rendering of the WHOLE corpus, so its cost
scales with the corpus, not with what just arrived: riding individual
saves cost $26 in one day of clipping (30 rebuilds x ~77k input tokens
of a 1223-item corpus on a flagship model). Moving this to the Batch API
(50% pricing) is still a follow-up.
"""
from datetime import datetime

from celery.utils.log import get_task_logger
from sqlalchemy import func

from backend.celery_app import celery, flask_app
from backend.extensions import db
from backend.models import APICostLog, ExternalItem, User, UserArtifact
from backend.llm_providers import LLMProvider
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.cost import calculate_llm_cost_microdollars
from backend.utils.timefmt import user_local_hour

logger = get_task_logger(__name__)

DIGEST_KIND = "external_digest"
DIGEST_TITLE = "Saved References Digest"
DIGEST_DESCRIPTION = (
    "Topic map of the tweets, bookmarks and web pages {name} saved "
    "elsewhere — read this before searching their saved references, to "
    "see what might be there."
)

# The nightly rebuild fires at this hour on the USER's own clock, one
# hour after the X bookmark sync (NIGHTLY_SYNC_LOCAL_HOUR = 3), so a
# night's new bookmarks are stored before the digest reads the corpus.
NIGHTLY_DIGEST_LOCAL_HOUR = 4
# Seconds between dispatches, so N users don't hit the LLM at once.
DIGEST_DISPATCH_STAGGER_SECONDS = 60

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
time range. Use the exact total from the corpus header — you cannot \
reliably count the list yourself, so don't estimate how many items you \
were shown.
- Your clusters need not cover every item — a fat head of clear topics \
beats exhaustive coverage. But SAY what you did: roughly how many items \
the clusters account for, and one line characterizing the uncovered \
remainder (e.g. "long tail of one-off links and jokes").
- This digest is read by an AI assistant deciding whether the user's \
saved references might contain something relevant to a live \
conversation — write for that reader: dense, factual, scannable. No \
preamble, no advice, markdown headings only.

The corpus:

{corpus}
"""


def _newest_item_at(user_id):
    return db.session.query(func.max(ExternalItem.fetched_at)).filter(
        ExternalItem.user_id == user_id).scalar()


def _digest_built_at(user_id):
    return db.session.query(func.max(UserArtifact.created_at)).filter(
        UserArtifact.user_id == user_id,
        UserArtifact.kind == DIGEST_KIND,
    ).scalar()


def digest_is_stale(user_id):
    """True when the corpus changed since the last digest was built.

    ``fetched_at`` moves both when an item is inserted and when a re-clip
    replaces a reference's stored text, so both count as a change. A
    DELETED item moves no timestamp, so the digest keeps describing it
    until the next save — unchanged from the behaviour before the nightly
    gate, where deletion triggered no rebuild either.
    """
    newest = _newest_item_at(user_id)
    if newest is None:
        return False
    built = _digest_built_at(user_id)
    return built is None or built < newest


@celery.task(name='backend.tasks.external_digest.sweep_external_digests')
def sweep_external_digests():
    """Hourly beat gate: rebuild each user's digest at most once a night,
    in their own night, and only if their references changed since the
    last rebuild.

    This is the ONLY scheduled dispatcher. Saving a reference (clipper,
    import, bookmark sync) deliberately does not rebuild — a burst of
    clips would otherwise pay for one whole-corpus LLM call each.
    """
    with flask_app.app_context():
        newest_by_user = dict(
            db.session.query(
                ExternalItem.user_id, func.max(ExternalItem.fetched_at)
            ).group_by(ExternalItem.user_id).all()
        )
        built_by_user = dict(
            db.session.query(
                UserArtifact.user_id, func.max(UserArtifact.created_at)
            ).filter(UserArtifact.kind == DIGEST_KIND)
            .group_by(UserArtifact.user_id).all()
        )
        stale_ids = [
            user_id for user_id, newest in newest_by_user.items()
            if newest is not None
            and (built_by_user.get(user_id) is None
                 or built_by_user[user_id] < newest)
        ]
        if not stale_ids:
            return {"status": "ok", "dispatched": 0}

        dispatched = 0
        for user in User.query.filter(User.id.in_(stale_ids)).all():
            if user_local_hour(user) != NIGHTLY_DIGEST_LOCAL_HOUR:
                continue
            rebuild_external_digest.apply_async(
                args=[user.id],
                countdown=dispatched * DIGEST_DISPATCH_STAGGER_SECONDS)
            dispatched += 1
        if dispatched:
            logger.info(
                "Nightly external-digest sweep dispatched for %d users",
                dispatched)
        return {"status": "ok", "dispatched": dispatched}


@celery.task(name='backend.tasks.external_digest.rebuild_external_digest',
             bind=True, max_retries=2, default_retry_delay=60)
def rebuild_external_digest(self, user_id, force=False):
    """Rebuild one user's digest. Dispatched by the nightly sweep; pass
    force=True to rebuild a digest the staleness check calls current."""
    with flask_app.app_context():
        user = User.query.get(user_id)
        if user is None:
            return {"status": "no_user"}
        if not force and not digest_is_stale(user_id):
            return {"status": "not_stale"}

        # Stamp the artifact with the corpus state it reflects, not with
        # the moment the (30s+) LLM call returned: an item saved DURING a
        # rebuild is not in this digest and must still read as newer than
        # it, or the staleness check would skip it forever.
        corpus_at = datetime.utcnow()
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
            if item.source != "web_clip":
                author = f"@{author}"
            if item.title:
                snippet = f"{item.title} — {snippet}"[:ITEM_SNIPPET_CHARS]
            line = f"- {author} ({stamp}): {snippet}"
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

        default_model = flask_app.config.get(
            "DEFAULT_LLM_MODEL", "claude-opus-5")
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
            created_at=corpus_at,
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
