"""External-references digest (quote-as-response, #155/#208).

Rebuilds the ``external_digest`` UserArtifact — a compact topic map of the
user's imported external content (tweets, bookmarks). It appears in the
agentic artifacts index like any other artifact, so the model can pull it
with read_artifact to learn what MIGHT exist in the saved corpus before
deciding to search it — and it costs nothing in contexts that never look
(most Loore contexts don't need external content).

Rebuilt ONCE A NIGHT, in the user's own night, and only when their
corpus changed since the last rebuild. The digest is a single LLM call
over a capped, compact rendering of the WHOLE corpus, so its cost scales
with the corpus, not with what just arrived: riding individual saves
cost $26 in one day of clipping (30 rebuilds x ~77k input tokens of a
1223-item corpus on a flagship model).

Nothing waits on a digest, so the nightly sweep submits every stale
user's rebuild as ONE provider batch (~50% cheaper, <=24h SLA) and a
beat collector saves the results — see sweep_external_digests /
collect_external_digest_batches. The synchronous rebuild_external_digest
task stays as the direct path (a future "rebuild now" button).
"""
from datetime import datetime, timedelta

from celery.utils.log import get_task_logger
from sqlalchemy import func

from backend.celery_app import celery, flask_app
from backend.extensions import db
from backend.models import (
    APICostLog, ExternalDigestBatchJob, ExternalItem, User, UserArtifact,
)
from backend.llm_providers import LLMProvider
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.cost import calculate_llm_cost_microdollars
from backend.utils.llm_batch import (
    apply_batch_key_override, batch_check_and_collect, batch_submit,
)
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
# Output cap for a digest request. Real digests run ~2k tokens; this is
# headroom, matching the direct path's provider default.
DIGEST_MAX_TOKENS = 10000
# Backstop for a batch we can no longer READ (lost batch id, revoked
# key): once it is older than this, the job is marked abandoned so its
# users, still stale, are resubmitted by the next sweep. A slow batch
# never needs this — both providers end a batch themselves at 24h
# (OpenAI `expired`, Anthropic `ended` with expired items) and the
# collector treats that as ended. So: the 24h window plus polling slack.
BATCH_JOB_MAX_AGE = timedelta(hours=25)

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


def _digest_model_id(user):
    default_model = flask_app.config.get(
        "DEFAULT_LLM_MODEL", "claude-opus-5")
    model_id = user.preferred_model or default_model
    if model_id not in flask_app.config.get("SUPPORTED_MODELS", {}):
        model_id = default_model
    return model_id


def _render_digest_prompt(user_id):
    """The digest prompt over the user's current corpus, or None when
    they have no items. Returns (prompt_text, total_items). Decrypts up
    to MAX_DIGEST_ITEMS items — the one place that reads the corpus."""
    items = ExternalItem.query.filter_by(user_id=user_id).order_by(
        ExternalItem.posted_at.desc().nullslast(),
        ExternalItem.fetched_at.desc(),
    ).limit(MAX_DIGEST_ITEMS).all()
    if not items:
        return None

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
    return DIGEST_PROMPT.replace("{corpus}", corpus), total


def _save_digest(user, model_id, digest_text, input_tokens, output_tokens,
                 corpus_at, batch):
    """Log the cost and write the new digest version. ``corpus_at`` is
    the moment the prompt was rendered: the artifact is stamped with the
    corpus state it reflects, not with the moment the response arrived
    (seconds later on the direct path, hours later from a batch), so an
    item saved in between still reads as newer than the digest and the
    staleness check picks it up next time."""
    db.session.add(APICostLog(
        user_id=user.id,
        model_id=model_id,
        request_type="external_digest",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_microdollars=calculate_llm_cost_microdollars(
            model_id, input_tokens, output_tokens, batch=batch),
    ))
    previous = UserArtifact.latest_for(user.id, DIGEST_KIND)
    artifact = UserArtifact(
        user_id=user.id,
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
    return artifact


# ── Nightly path: one provider batch for every stale user ───────────────

def _batch_keys():
    return apply_batch_key_override(
        get_api_keys_for_usage(flask_app.config, "chat"),
        flask_app.config)


def _users_in_pending_batches():
    pending = ExternalDigestBatchJob.query.filter_by(status="pending").all()
    return {item["user_id"] for job in pending for item in job.items}


def _stale_user_ids():
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
    return [
        user_id for user_id, newest in newest_by_user.items()
        if newest is not None
        and (built_by_user.get(user_id) is None
             or built_by_user[user_id] < newest)
    ]


def _submit_digest_batch(users):
    """Render one request per user, submit per provider, persist the
    jobs. Returns the number of users submitted."""
    supported = flask_app.config.get("SUPPORTED_MODELS", {})
    requests_by_provider = {}
    meta_by_custom_id = {}
    for user in users:
        # Stamped BEFORE the corpus is read (see _save_digest).
        corpus_at = datetime.utcnow()
        rendered = _render_digest_prompt(user.id)
        if rendered is None:
            continue
        prompt_text, total = rendered
        model_id = _digest_model_id(user)
        model_cfg = supported.get(model_id)
        if model_cfg is None:
            logger.warning("Digest for user %s skipped: model %s not "
                           "configured", user.id, model_id)
            continue
        custom_id = f"external-digest-{user.id}"
        requests_by_provider.setdefault(model_cfg["provider"], []).append({
            "custom_id": custom_id,
            "model_id": model_id,
            "api_model": model_cfg["api_model"],
            "messages": [{"role": "user", "content": prompt_text}],
            "max_tokens": DIGEST_MAX_TOKENS,
        })
        meta_by_custom_id[custom_id] = {
            "custom_id": custom_id,
            "user_id": user.id,
            "model_id": model_id,
            "corpus_at": corpus_at.isoformat(),
            "total_items": total,
        }
    if not requests_by_provider:
        return 0

    batch_ids = batch_submit(requests_by_provider, _batch_keys(), "digest")
    submitted = 0
    for provider_key, batch_id in batch_ids.items():
        # batch_submit keys OpenAI jobs "openai:<api_model>"; route each
        # request's metadata to the job that carries it.
        provider, _, api_model = provider_key.partition(":")
        items = [
            meta_by_custom_id[req["custom_id"]]
            for req in requests_by_provider.get(provider, [])
            if not api_model or req["api_model"] == api_model
        ]
        db.session.add(ExternalDigestBatchJob(
            provider_key=provider_key, batch_id=batch_id, items=items))
        submitted += len(items)
    db.session.commit()
    # batch_submit logs (not raises) a provider's failure; those users
    # stay stale and the next sweep tries again.
    return submitted


@celery.task(name='backend.tasks.external_digest.sweep_external_digests')
def sweep_external_digests():
    """Hourly beat gate: submit a digest rebuild for each user whose
    references changed since their last digest — at most once a night,
    in their own night, and never while a batch of theirs is pending.

    This is the ONLY scheduled dispatcher. Saving a reference (clipper,
    import, bookmark sync) deliberately does not rebuild — a burst of
    clips would otherwise pay for one whole-corpus LLM call each.
    """
    with flask_app.app_context():
        stale_ids = set(_stale_user_ids()) - _users_in_pending_batches()
        if not stale_ids:
            return {"status": "ok", "submitted": 0}
        due = [
            user for user in User.query.filter(User.id.in_(stale_ids)).all()
            if user_local_hour(user) == NIGHTLY_DIGEST_LOCAL_HOUR
        ]
        if not due:
            return {"status": "ok", "submitted": 0}
        submitted = _submit_digest_batch(due)
        if submitted:
            logger.info(
                "Nightly external-digest batch submitted for %d users",
                submitted)
        return {"status": "ok", "submitted": submitted}


def _collect_digest_batches():
    """Core of the collector (plain function so tests can call it
    without the Celery machinery)."""
    jobs = ExternalDigestBatchJob.query.filter_by(status="pending").all()
    if not jobs:
        return {"collected": 0, "abandoned": 0}

    keys = _batch_keys()
    now = datetime.utcnow()
    collected = abandoned = 0
    for job in jobs:
        try:
            results, still_pending, _ = batch_check_and_collect(
                {job.provider_key: job.batch_id}, keys)
        except Exception:
            logger.exception("Collect failed for external-digest batch %s",
                             job.batch_id)
            # Unreadable, not merely unfinished — the case the age
            # backstop exists for, so it must apply here too.
            still_pending = {job.provider_key: job.batch_id}
            results = {}
        if still_pending:
            if now - job.submitted_at > BATCH_JOB_MAX_AGE:
                logger.error(
                    "External-digest batch %s not ended after %s; "
                    "abandoning (%d users stay stale for the next sweep)",
                    job.batch_id, BATCH_JOB_MAX_AGE, len(job.items))
                job.status = "abandoned"
                job.collected_at = now
                db.session.commit()
                abandoned += 1
            continue

        # Batch ended: every item either has a result or failed. A
        # failed item leaves its user stale, so the next sweep retries.
        for item in job.items:
            result = results.get(item["custom_id"])
            digest_text = ((result or {}).get("content") or "").strip()
            user = User.query.get(item["user_id"]) if digest_text else None
            if user is None:
                logger.warning(
                    "External-digest item %s of batch %s yielded no "
                    "digest", item["custom_id"], job.batch_id)
                continue
            _save_digest(
                user, item["model_id"], digest_text,
                result["input_tokens"], result["output_tokens"],
                datetime.fromisoformat(item["corpus_at"]), batch=True)
            logger.info(
                "External digest rebuilt for user %s from batch (%d "
                "items, model %s)", user.id, item["total_items"],
                item["model_id"])
        job.status = "collected"
        job.collected_at = now
        db.session.commit()
        collected += 1
    return {"collected": collected, "abandoned": abandoned}


@celery.task(
    name='backend.tasks.external_digest.collect_external_digest_batches')
def collect_external_digest_batches():
    """Beat task: retrieve finished digest batches and save the digests.
    No-op when nothing is pending."""
    with flask_app.app_context():
        return _collect_digest_batches()


# ── Direct path: one synchronous call, kept for a manual rebuild ────────

@celery.task(name='backend.tasks.external_digest.rebuild_external_digest',
             bind=True, max_retries=2, default_retry_delay=60)
def rebuild_external_digest(self, user_id, force=False):
    """Rebuild one user's digest with a direct (non-batch) call. The
    scheduled path is the batch sweep above; this stays for a manual
    "rebuild now" (pass force=True to rebuild a digest the staleness
    check calls current)."""
    with flask_app.app_context():
        user = User.query.get(user_id)
        if user is None:
            return {"status": "no_user"}
        if not force and not digest_is_stale(user_id):
            return {"status": "not_stale"}

        corpus_at = datetime.utcnow()
        rendered = _render_digest_prompt(user_id)
        if rendered is None:
            return {"status": "no_items"}
        prompt_text, total = rendered
        model_id = _digest_model_id(user)

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

        artifact = _save_digest(
            user, model_id, digest_text,
            response.get("input_tokens", 0), response.get("output_tokens", 0),
            corpus_at, batch=False)
        db.session.commit()
        logger.info(
            "External digest rebuilt for user %s (%d items, model %s)",
            user_id, total, model_id)
        return {"status": "ok", "artifact_id": artifact.id,
                "items": total}
