"""The Saved References Digest's model, cap, order and cut report (Peter,
2026-11-24).

- GPT-6.2 Sol for an account on an OpenAI model and for one that answered
  "both providers"; Sonnet 5.5 for an account on an Anthropic model that
  answered "one provider" or never answered (DIGEST_MODEL_OPENAI /
  DIGEST_MODEL_ANTHROPIC). When the setting names no model of that
  provider, the digest is not built and the account stays due: no
  fallback to the other provider or another model.
- One cap for both models, DIGEST_MAX_PROMPT_TOKENS (258,400 o200k tokens
  of the whole request, 5 % under GPT-6.2 Sol's 272,000-token price step),
  overridable by the environment; no item cap. The same list goes to
  either model.
- Newest-saved first: by the day saved (saved_at, else fetched_at), then
  the publishing date (undated last). The oldest-saved are cut.
- The first time an account's list is cut, one Sentry warning with a
  fixed fingerprint and numbers only; again only after a list that fit.
- The build is priced by the model the request went to, at its Batch
  price, with OpenAI's cache-write premium and price step.
- A change of the digest's model alone rebuilds no digest.

The app, fixtures and fakes of test_external_digest_nightly.py; no model
or provider is called. o200k counts come from conftest's stand-in (a
token per four characters) unless a test installs another.
"""
import importlib.util
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.tests import test_external_digest_nightly as dn
from backend.extensions import db as _db
from backend.models import (
    APICostLog, ExternalDigestBatchJob, ExternalItem, User, UserArtifact,
)
from backend.utils import tokens

_digest = dn._digest
app = dn.app  # noqa: F811 (fixture)

SONNET, SOL = "claude-sonnet-5.5", "gpt-6.2-sol"


def _night():
    return dn._tz_at_hour(_digest.NIGHTLY_DIGEST_LOCAL_HOUR)


def _account(name, preferred_model=None, both=None):
    """An account in its night. *both*: True / False is its answer to the
    both-providers question, None never answered."""
    user = User(username=name, timezone=_night(),
                preferred_model=preferred_model,
                last_seen_at=datetime.utcnow())
    if both is not None:
        user.profile_both_providers = both
        user.profile_both_providers_answered_at = datetime.utcnow()
    _db.session.add(user)
    _db.session.commit()
    return user


def _item(user_id, external_id, text="a saved page", posted_at=None,
          saved_at=None, fetched_at=None, handle="author"):
    item = ExternalItem(
        user_id=user_id, source="twitter_bookmark", external_id=external_id,
        author_handle=handle, posted_at=posted_at,
        saved_at=saved_at or datetime.utcnow(),
        fetched_at=fetched_at or datetime.utcnow())
    item.set_content(text)
    _db.session.add(item)
    return item


def _corpus_lines(prompt):
    return [line for line in prompt.splitlines() if line.startswith("- @")]


def _texts(prompt):
    return [line.split(": ", 1)[1] for line in _corpus_lines(prompt)]


def _cap(monkeypatch, value):
    monkeypatch.setitem(_digest.flask_app.config,
                        "DIGEST_MAX_PROMPT_TOKENS", value)


class _TokenPerCharacter:
    """Stand-in for o200k on text dense in tokens (dense Japanese and
    Chinese lists measured 0.90 to 1.14 a character): a token per
    character."""

    def encode(self, text, disallowed_special=()):
        return [0] * len(text or "")


# ── The model ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("own_model, both, expected", [
    (None, None, SONNET),            # Anthropic, never answered
    (None, False, SONNET),           # Anthropic, one provider
    (None, True, SOL),               # Anthropic, both providers
    ("gpt-6-astra", None, SOL),      # OpenAI, never answered
    ("gpt-6-astra", False, SOL),     # OpenAI, one provider
    ("gpt-6-astra", True, SOL),      # OpenAI, both providers
])
def test_who_gets_which_model(app, own_model, both, expected):
    """Peter, 2026-11-24: "pls make Sol 6.2 default for digests for users
    who either use OAI models or who allowed to use both providers. For
    anthropic only users use Sonnet 5.5"."""
    user = _account("someone", preferred_model=own_model, both=both)
    assert _digest._digest_model_id(user) == expected


def test_each_model_on_both_paths(app, monkeypatch):
    anthropic_user = User.query.first()
    anthropic_user.timezone = _night()
    openai_user = _account("openai-user", preferred_model="gpt-6-astra")
    both_user = _account("both-user", both=True)   # Anthropic model
    for user in (anthropic_user, openai_user, both_user):
        _item(user.id, f"saved-{user.id}")
    _db.session.commit()

    calls = dn._stub_llm(monkeypatch)
    assert _digest.rebuild_external_digest(
        dn._FakeSelf(), openai_user.id)["status"] == "ok"
    assert [c["model_id"] for c in calls] == [SOL]
    assert UserArtifact.latest_for(
        openai_user.id, _digest.DIGEST_KIND).generated_by == SOL

    submitted = dn._stub_batch_submit(monkeypatch)
    assert _digest.sweep_external_digests()["submitted"] == 2
    assert [r["user_id"] for r in submitted[0]["anthropic"]] == [
        anthropic_user.id]
    assert [r["model_id"] for r in submitted[0]["anthropic"]] == [SONNET]
    assert [(r["user_id"], r["api_model"])
            for r in submitted[0]["openai"]] == [(both_user.id, SOL)]


@pytest.mark.parametrize("setting", [
    SONNET,        # another provider's model
    "gpt-7-sol",   # a model that is not configured
])
def test_no_digest_and_no_fallback_without_the_providers_digest_model(
        app, monkeypatch, setting):
    """The OpenAI-model account and the account that answered "both
    providers" get no digest: not on the Anthropic model the setting
    names, not on Sonnet 5.5, not on their own chat model. They stay due
    for the next night, as an account whose model is not configured
    always did. The Anthropic-only account due in the same hour is
    submitted."""
    anthropic_user = User.query.first()
    anthropic_user.timezone = _night()
    openai_user = _account("openai-user", preferred_model="gpt-6-astra")
    both_user = _account("both-user", both=True)
    for user in (anthropic_user, openai_user, both_user):
        _item(user.id, f"saved-{user.id}")
    _db.session.commit()
    monkeypatch.setitem(_digest.flask_app.config, "DIGEST_MODEL_OPENAI",
                        setting)
    submitted = dn._stub_batch_submit(monkeypatch)
    calls = dn._stub_llm(monkeypatch)
    read = []
    real_render = _digest._render_digest_prompt
    monkeypatch.setattr(_digest, "_render_digest_prompt",
                        lambda uid: read.append(uid) or real_render(uid))

    assert _digest.sweep_external_digests()["submitted"] == 1
    assert list(submitted[0]) == ["anthropic"]
    assert [r["user_id"] for r in submitted[0]["anthropic"]] == [
        anthropic_user.id]
    # Their references were not even read.
    assert read == [anthropic_user.id]
    for user in (openai_user, both_user):
        assert _digest.digest_is_stale(user.id) is True
        assert _digest.rebuild_external_digest(
            dn._FakeSelf(), user.id, force=True) == {"status": "no_model"}
    assert calls == []
    assert APICostLog.query.count() == 0


def test_the_shipped_digest_models():
    """Peter, 2026-11-24: Sonnet 5.5 and GPT-6.2 Sol; Haiku 5.5 and Luna
    are not used for the digest."""
    from backend.config import Config
    assert Config.DIGEST_MODEL_ANTHROPIC == SONNET
    assert Config.DIGEST_MODEL_OPENAI == SOL


# ── The cap: o200k tokens of the whole request ──────────────────────────

def _dated_list(user, n, text="tweet number {i} " + "x" * 50):
    start = datetime(2026, 1, 1)
    for i in range(n):
        _item(user.id, f"t{i}", text=text.format(i=i),
              saved_at=start + timedelta(days=i))
    _db.session.commit()


def test_the_cap_counts_tokens_of_the_whole_request_and_line_breaks(
        app, monkeypatch):
    """Lines are listed newest-saved first while the instructions, the
    longest header, the framing and every listed line with one token for
    its line break stay within DIGEST_MAX_PROMPT_TOKENS."""
    user = User.query.first()
    _dated_list(user, 10)
    full = _digest._render_digest_prompt(user.id)
    lines = _corpus_lines(full.prompt)
    assert (full.total, full.listed, full.cut) == (10, 10, False)
    assert "ALL of them are listed below" in full.prompt
    assert "tweet number 9" in lines[0] and "tweet number 0" in lines[-1]

    count, how = _digest._token_counter()
    assert how == "o200k"
    six = (_digest._fixed_prompt_tokens(count, 10)
           + sum(count(line) + 1 for line in lines[:6]))
    _cap(monkeypatch, six)
    cut = _digest._render_digest_prompt(user.id)
    assert _corpus_lines(cut.prompt) == lines[:6]
    assert (cut.listed, cut.cut, cut.tokens) == (6, True, six)
    assert ("The corpus holds 10 saved items; the 6 most recently saved "
            "are listed below (4 saved earlier are not shown).") in cut.prompt
    # One token less: the sixth line's line break no longer fits.
    _cap(monkeypatch, six - 1)
    assert _digest._render_digest_prompt(user.id).listed == 5


def test_text_dense_in_tokens_is_cut_sooner(app, monkeypatch):
    """The cap is in tokens, not characters: the same list, four times as
    many tokens a character, keeps far fewer lines, and the request stays
    within the cap."""
    user = User.query.first()
    _dated_list(user, 200)
    count, _ = _digest._token_counter()
    lines = _corpus_lines(_digest._render_digest_prompt(user.id).prompt)
    cap = (_digest._fixed_prompt_tokens(count, 200)
           + sum(count(line) + 1 for line in lines[:100]))
    _cap(monkeypatch, cap)
    assert _digest._render_digest_prompt(user.id).listed == 100

    monkeypatch.setitem(tokens._O200K, "enc", _TokenPerCharacter())
    dense = _digest._render_digest_prompt(user.id)
    assert 0 < dense.listed < 25
    assert dense.cut and dense.tokens <= cap


def test_the_same_list_goes_to_sonnet_and_to_sol(app, monkeypatch):
    """Peter, 2026-11-24: "The cap should be based on OAI's price hike. Use
    the same for Sonnet 5.5, even though there is no hike"."""
    anthropic_user = User.query.first()
    anthropic_user.timezone = _night()
    openai_user = _account("openai-user", preferred_model="gpt-6-astra")
    for user in (anthropic_user, openai_user):
        _dated_list(user, 30)
    count, _ = _digest._token_counter()
    lines = _corpus_lines(_digest._render_digest_prompt(
        anthropic_user.id).prompt)
    _cap(monkeypatch, _digest._fixed_prompt_tokens(count, 30)
         + sum(count(line) + 1 for line in lines[:12]))
    submitted = dn._stub_batch_submit(monkeypatch)

    assert _digest.sweep_external_digests()["submitted"] == 2
    prompts = {provider: reqs[0]["messages"][0]["content"]
               for provider, reqs in submitted[0].items()}
    assert len(_corpus_lines(prompts["anthropic"])) == 12
    assert prompts["anthropic"] == prompts["openai"]


def test_without_the_encoding_the_list_is_fitted_by_the_estimate(
        app, monkeypatch):
    """tiktoken's o200k_base did not load in this process: each line is
    estimated at ESTIMATED_TOKENS_PER_CHAR (1.2, above the densest text
    measured), so the list errs short."""
    user = User.query.first()
    _dated_list(user, 10)
    monkeypatch.setattr(tokens, "_O200K", {"failed": True})
    rendered = _digest._render_digest_prompt(user.id)
    assert rendered.counted == "estimate"
    line = _corpus_lines(rendered.prompt)[0]
    count, how = _digest._token_counter()
    assert how == "estimate"
    assert count(line) == -(-len(line) * 12 // 10)


class _DensestMeasured:
    """Stand-in for o200k on the densest lists measured (Japanese,
    Chinese): 1.14 tokens a character."""

    def encode(self, text, disallowed_special=()):
        return [0] * -(-len(text or "") * 114 // 100)


def test_the_estimate_keeps_the_densest_text_measured_under_the_cap(
        app, monkeypatch):
    """Review of #802: at 0.8 tokens a character, a dense Japanese list
    filled to the cap by the estimate was about 9 % over it in real
    tokens. At 1.2 the same list stays under."""
    user = User.query.first()
    _dated_list(user, 200, text="日本語の文章 {i} " + "あ" * 200)
    monkeypatch.setattr(tokens, "_O200K", {"failed": True})
    _cap(monkeypatch, 20_000)
    rendered = _digest._render_digest_prompt(user.id)
    assert rendered.cut and rendered.counted == "estimate"
    real = (len(_DensestMeasured().encode(rendered.prompt))
            + _digest.MESSAGE_FRAMING_TOKENS)
    assert real <= 20_000


def test_more_than_1500_items_are_listed_while_they_fit_the_cap(app):
    """The item cap of 1,500 is gone: a list of short items is cut by the
    token cap alone."""
    user = User.query.first()
    start = datetime(2025, 1, 1)
    for i in range(1600):
        _item(user.id, f"t{i}", text=f"short {i}",
              saved_at=start + timedelta(hours=i))
    _db.session.commit()

    rendered = _digest._render_digest_prompt(user.id)
    assert (rendered.total, rendered.listed, rendered.cut) == (
        1600, 1600, False)
    assert "The corpus holds 1600 saved items; ALL of them" in rendered.prompt


def test_items_past_the_cap_are_not_decrypted(app, monkeypatch):
    """The list is decrypted row by row up to the cap: the lines listed
    and the one that did not fit (one key-service call per item when
    encryption is on)."""
    user = User.query.first()
    _dated_list(user, 40, text="y" * 100 + " {i}")
    read = []
    real = ExternalItem.get_content
    monkeypatch.setattr(ExternalItem, "get_content",
                        lambda self: read.append(self.id) or real(self))
    count, _ = _digest._token_counter()
    _cap(monkeypatch, _digest._fixed_prompt_tokens(count, 40) + 100)

    rendered = _digest._render_digest_prompt(user.id)
    assert 0 < rendered.listed < 5
    assert len(read) == rendered.listed + 1


def test_the_rows_are_streamed_not_loaded_whole(app, monkeypatch):
    """A long list whose cap is reached early loads one fetch of rows
    (RENDER_ROWS_PER_FETCH), not every saved reference."""
    from sqlalchemy import event
    user = User.query.first()
    _dated_list(user, 1200, text="z {i}")
    count, _ = _digest._token_counter()
    _cap(monkeypatch, _digest._fixed_prompt_tokens(count, 1200) + 50)
    uid = user.id
    _db.session.expunge_all()
    loaded = []

    def on_load(target, context):
        loaded.append(target.id)
    event.listen(ExternalItem, "load", on_load)
    try:
        rendered = _digest._render_digest_prompt(uid)
    finally:
        event.remove(ExternalItem, "load", on_load)
    assert rendered.cut and rendered.listed < 20
    assert 0 < len(loaded) <= _digest.RENDER_ROWS_PER_FETCH


def _config_class(monkeypatch, **env):
    """config.py loaded on its own with the given environment, under a
    name of its own, so the Config every other test reads is untouched."""
    monkeypatch.delenv("DIGEST_MAX_PROMPT_TOKENS", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    path = Path(__file__).resolve().parents[1] / "config.py"
    spec = importlib.util.spec_from_file_location("_digest_config_probe",
                                                  path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Config


def test_the_cap_is_one_setting_overridable_by_the_environment(monkeypatch):
    # 272,000 (GPT-6.2 Sol's price step) less 5 %.
    assert _config_class(monkeypatch).DIGEST_MAX_PROMPT_TOKENS == 258_400
    assert _config_class(
        monkeypatch, DIGEST_MAX_PROMPT_TOKENS="200000"
    ).DIGEST_MAX_PROMPT_TOKENS == 200_000


# ── The order: newest-saved first ───────────────────────────────────────

def test_the_list_is_ordered_by_when_it_was_saved(app, monkeypatch):
    """Peter, 2026-11-24: "Why don't we use the timestamp when it's
    saved?" By the day saved, newest first; on the same day (a bulk
    import) by the publishing date, undated last. The oldest-saved are
    cut."""
    user = User.query.first()
    now = datetime.utcnow()
    import_day = datetime(2026, 3, 1, 10, 0)
    # One bulk import: rows saved seconds apart, in file order.
    _item(user.id, "imp-old", text="import posted 2019",
          posted_at=datetime(2019, 5, 1), saved_at=import_day)
    _item(user.id, "imp-undated", text="import undated",
          saved_at=import_day + timedelta(seconds=1))
    _item(user.id, "imp-new", text="import posted 2025",
          posted_at=datetime(2025, 5, 1),
          saved_at=import_day + timedelta(seconds=2))
    # Clipped later, whatever its publishing date.
    _item(user.id, "clip", text="clipped in April posted 2001",
          posted_at=datetime(2001, 1, 1), saved_at=datetime(2026, 4, 1))
    # Saved long ago and re-clipped today: fetched_at moved, saved_at not.
    _item(user.id, "reclipped", text="saved 2024 re-clipped today",
          posted_at=datetime(2026, 1, 1), saved_at=datetime(2024, 6, 1),
          fetched_at=now)
    # From before saved_at existed: fetched_at stands in for it.
    _item(user.id, "legacy", text="legacy row fetched 2025",
          fetched_at=datetime(2025, 6, 1))
    _db.session.commit()
    ExternalItem.query.filter_by(external_id="legacy").update(
        {"saved_at": None})
    _db.session.commit()

    rendered = _digest._render_digest_prompt(user.id)
    assert _texts(rendered.prompt) == [
        "clipped in April posted 2001",
        "import posted 2025", "import posted 2019", "import undated",
        "legacy row fetched 2025", "saved 2024 re-clipped today"]
    # An undated item keeps "?" for its publishing date.
    assert "(?): import undated" in rendered.prompt

    count, _ = _digest._token_counter()
    lines = _corpus_lines(rendered.prompt)
    _cap(monkeypatch, _digest._fixed_prompt_tokens(count, 6)
         + sum(count(line) + 1 for line in lines[:3]))
    assert _texts(_digest._render_digest_prompt(user.id).prompt) == [
        "clipped in April posted 2001",
        "import posted 2025", "import posted 2019"]


# ── The report of a cut list ────────────────────────────────────────────

@pytest.fixture
def sentry(monkeypatch):
    """Sentry set up for the digest module only (its _sentry), its events
    recorded as (message, level); the last scope in state["scope"]."""
    events = []
    state = {"active": True}

    @contextmanager
    def new_scope():
        scope = SimpleNamespace(fingerprint=None, extras={})
        scope.set_extra = scope.extras.__setitem__
        yield scope
        state["scope"] = scope

    def capture_message(message, level=None):
        events.append((message, level))

    fake = SimpleNamespace(
        get_client=lambda: SimpleNamespace(
            is_active=lambda: state["active"]),
        new_scope=new_scope, capture_message=capture_message)
    monkeypatch.setattr(_digest, "_sentry", lambda: fake)

    class Recorder:
        def __init__(self):
            self.state = state

        @property
        def events(self):
            return events
    return Recorder()


def _rebuild(user):
    assert _digest.rebuild_external_digest(
        dn._FakeSelf(), user.id, force=True)["status"] == "ok"


def test_the_first_cut_is_reported_once_and_again_after_a_fit(
        app, monkeypatch, sentry):
    user = User.query.first()
    _dated_list(user, 30)
    dn._stub_llm(monkeypatch)
    count, _ = _digest._token_counter()
    lines = _corpus_lines(_digest._render_digest_prompt(user.id).prompt)
    small = (_digest._fixed_prompt_tokens(count, 30)
             + sum(count(line) + 1 for line in lines[:10]))

    _rebuild(user)                      # fits: nothing to say
    assert sentry.events == [] and user.digest_cut_reported_at is None

    _cap(monkeypatch, small)
    _rebuild(user)                      # first cut: one event
    assert sentry.events == [(_digest.CUT_EVENT_MESSAGE, "warning")]
    scope = sentry.state["scope"]
    assert scope.fingerprint == _digest.CUT_EVENT_FINGERPRINT
    # Numbers only: the account's id, the tokens, the cap, the counts.
    assert scope.extras == {
        "user_id": user.id, "prompt_tokens": small, "counted": "o200k",
        "cap_tokens": small, "items_kept": 10, "items_cut": 20}
    _db.session.expire_all()            # kept across restarts
    assert User.query.get(user.id).digest_cut_reported_at is not None

    _rebuild(user)                      # still cut: no second event
    assert len(sentry.events) == 1

    _cap(monkeypatch, 258_400)
    _rebuild(user)                      # fits again: the mark clears
    _db.session.expire_all()
    assert User.query.get(user.id).digest_cut_reported_at is None
    assert len(sentry.events) == 1

    _cap(monkeypatch, small)
    _rebuild(user)                      # crosses again: reported again
    assert len(sentry.events) == 2


def test_the_nightly_sweep_reports_a_cut(app, monkeypatch, sentry):
    user = User.query.first()
    user.timezone = _night()
    _dated_list(user, 30)
    dn._stub_batch_submit(monkeypatch)
    count, _ = _digest._token_counter()
    _cap(monkeypatch, _digest._fixed_prompt_tokens(count, 30) + 100)

    assert _digest.sweep_external_digests()["submitted"] == 1
    assert sentry.events == [(_digest.CUT_EVENT_MESSAGE, "warning")]
    assert sentry.state["scope"].extras["user_id"] == user.id
    _db.session.expire_all()
    assert User.query.get(user.id).digest_cut_reported_at is not None


def test_no_report_and_no_mark_without_sentry(app, monkeypatch, sentry):
    """Local instances and tests have no Sentry: nothing is sent, and the
    account is not marked, so a later setup with Sentry still reports."""
    sentry.state["active"] = False
    user = User.query.first()
    _dated_list(user, 30)
    dn._stub_llm(monkeypatch)
    count, _ = _digest._token_counter()
    _cap(monkeypatch, _digest._fixed_prompt_tokens(count, 30) + 100)

    _rebuild(user)
    assert sentry.events == []
    assert user.digest_cut_reported_at is None


# ── The price: the model used, Batch, cache writes and the price step ───

@pytest.mark.parametrize("usage, cost", [
    # 5,000 ordinary x $2 + 1,000 cached x $0.10 + 4,000 written x $2.50
    # + 1,000 out x $10 = 30,100 microdollars, half at Batch.
    ({"prompt_tokens": 10_000, "completion_tokens": 1_000,
      "prompt_tokens_details": {"cached_tokens": 1_000,
                                "cache_write_tokens": 4_000}}, 15_050),
    # Above 272,000 input tokens the whole request bills at $4 / $15:
    # 300,000 x $4 + 2,000 x $15 = 1,230,000, half at Batch.
    ({"prompt_tokens": 300_000, "completion_tokens": 2_000}, 615_000),
])
def test_an_openai_digest_is_priced_at_sols_batch_prices(
        app, monkeypatch, usage, cost):
    user = _account("openai-user", preferred_model="gpt-6-astra")
    _item(user.id, "saved")
    _db.session.commit()
    dn._stub_batch_submit(monkeypatch)
    assert _digest.sweep_external_digests()["submitted"] == 1
    job = ExternalDigestBatchJob.query.one()
    assert job.provider_key == f"openai:{SOL}"

    dn._fake_openai_batch(
        monkeypatch, {"file-out": [{
            "custom_id": f"external-digest-{user.id}", "response": {
                "status_code": 200, "body": {
                    "choices": [{"message": {"content": "# Topics\n- x"},
                                 "finish_reason": "stop"}],
                    "usage": usage}}}]},
        status="completed", completed_at=1060, output_file_id="file-out",
        request_counts=dn._counts(1, 0))
    assert _digest._collect_digest_batches() == {"collected": 1,
                                                 "abandoned": 0}
    log = APICostLog.query.one()
    assert (log.model_id, log.cost_microdollars) == (SOL, cost)
    assert UserArtifact.latest_for(
        user.id, _digest.DIGEST_KIND).generated_by == SOL


def test_an_anthropic_digest_is_priced_at_sonnets_batch_prices(
        app, monkeypatch):
    user = User.query.first()
    user.timezone = _night()
    _item(user.id, "saved")
    _db.session.commit()
    dn._stub_batch_submit(monkeypatch)
    assert _digest.sweep_external_digests()["submitted"] == 1
    monkeypatch.setattr(_digest, "batch_check_and_collect", lambda ids, k: (
        {f"external-digest-{user.id}": {
            "content": "# Topics\n- x", "input_tokens": 1000,
            "output_tokens": 100}}, {}, {}))
    _digest._collect_digest_batches()
    log = APICostLog.query.one()
    # 1000 in x $2 + 100 out x $10 = 3000 microdollars, half at Batch.
    assert (log.model_id, log.cost_microdollars) == (SONNET, 1500)


# ── What makes a digest due ─────────────────────────────────────────────

def test_a_change_of_the_digest_model_alone_rebuilds_no_digest(
        app, monkeypatch):
    """A digest built on the account's chat model before the change stays
    until the account saves something new; then it is rebuilt once, on
    the digest model."""
    user = User.query.first()
    user.timezone = _night()
    _item(user.id, "old", fetched_at=datetime.utcnow() - timedelta(hours=3))
    _db.session.commit()
    dn._mk_digest(user.id, created_at=datetime.utcnow() - timedelta(hours=2))
    assert UserArtifact.latest_for(
        user.id, _digest.DIGEST_KIND).generated_by == "claude-opus-5"
    submitted = dn._stub_batch_submit(monkeypatch)

    assert _digest.sweep_external_digests() == {"status": "ok",
                                                "submitted": 0}
    assert submitted == []

    _item(user.id, "new")
    _db.session.commit()
    assert _digest.sweep_external_digests()["submitted"] == 1
    assert [r["model_id"] for r in submitted[0]["anthropic"]] == [SONNET]
