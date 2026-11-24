"""The Saved References Digest's model, cap and order (Peter, 2026-11-24).

- The digest runs on the middle model of the account's own provider:
  Sonnet 5.5 for Anthropic accounts, GPT-6.2 Sol for OpenAI accounts
  (DIGEST_MODEL_ANTHROPIC / DIGEST_MODEL_OPENAI). When that provider's
  setting names no model of that provider, the digest is not built and
  the account stays due: never another provider's model, never another
  model.
- One cap, DIGEST_MAX_UNITS (250,000 units of 4 characters of the
  rendered lines), overridable by the environment; no item cap.
- An item without a posting date is ordered by when it was saved
  (saved_at, else fetched_at), not placed last and cut first.
- The build is priced by the model the request went to, at its Batch
  price, with OpenAI's cache-write premium and price step.
- A change of the digest's model alone rebuilds no digest.

The app, fixtures and fakes of test_external_digest_nightly.py; no model
or provider is called.
"""
import importlib.util
import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend.tests import test_external_digest_nightly as dn
from backend.extensions import db as _db
from backend.models import (
    APICostLog, ExternalDigestBatchJob, ExternalItem, User, UserArtifact,
)

_digest = dn._digest
app = dn.app  # noqa: F811 (fixture)


def _night():
    return dn._tz_at_hour(_digest.NIGHTLY_DIGEST_LOCAL_HOUR)


def _account(name, preferred_model=None):
    user = User(username=name, timezone=_night(),
                preferred_model=preferred_model,
                last_seen_at=datetime.utcnow())
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


def _sent_prompt(submitted, provider="anthropic"):
    return submitted[-1][provider][0]["messages"][0]["content"]


def _corpus_lines(prompt):
    return [line for line in prompt.splitlines() if line.startswith("- @")]


# ── The model: the account's own provider, its middle model ──────────────

def test_each_provider_gets_its_middle_model_on_both_paths(app, monkeypatch):
    anthropic_user = User.query.first()
    openai_user = _account("openai-user", preferred_model="gpt-6-astra")
    for user in (anthropic_user, openai_user):
        _item(user.id, f"saved-{user.id}")
    _db.session.commit()

    calls = dn._stub_llm(monkeypatch)
    assert _digest.rebuild_external_digest(
        dn._FakeSelf(), openai_user.id)["status"] == "ok"
    assert [c["model_id"] for c in calls] == ["gpt-6.2-sol"]
    assert UserArtifact.latest_for(
        openai_user.id, _digest.DIGEST_KIND).generated_by == "gpt-6.2-sol"

    anthropic_user.timezone = _night()
    _db.session.commit()
    submitted = dn._stub_batch_submit(monkeypatch)
    assert _digest.sweep_external_digests()["submitted"] == 1
    assert [r["model_id"] for r in submitted[0]["anthropic"]] == [
        "claude-sonnet-5.5"]


@pytest.mark.parametrize("setting", [
    "claude-sonnet-5.5",   # another provider's model
    "gpt-7-sol",           # a model that is not configured
])
def test_no_digest_and_no_fallback_without_the_providers_digest_model(
        app, monkeypatch, setting):
    """The OpenAI account's digest is not built: not on the Anthropic
    model the setting names, not on its own chat model. It stays due for
    the next night, as an account whose model is not configured always
    did. The Anthropic account due in the same hour is submitted."""
    anthropic_user = User.query.first()
    anthropic_user.timezone = _night()
    openai_user = _account("openai-user", preferred_model="gpt-6-astra")
    for user in (anthropic_user, openai_user):
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
    # Its references were not even read.
    assert openai_user.id not in read
    assert _digest.digest_is_stale(openai_user.id) is True

    assert _digest.rebuild_external_digest(
        dn._FakeSelf(), openai_user.id, force=True) == {"status": "no_model"}
    assert calls == []
    assert APICostLog.query.count() == 0


# ── The cap: one setting, in units of the rendered lines ────────────────

def test_the_cap_counts_units_of_the_rendered_lines(app, monkeypatch):
    """Lines are listed newest first while their characters, each with its
    line break, fit DIGEST_MAX_UNITS x 4; the oldest beyond that are left
    out and counted in the header."""
    user = User.query.first()
    start = datetime(2026, 1, 1)
    for i in range(10):
        _item(user.id, f"t{i}", text=f"tweet number {i} " + "x" * 50,
              posted_at=start + timedelta(days=i))
    _db.session.commit()

    prompt, total = _digest._render_digest_prompt(user.id)
    lines = _corpus_lines(prompt)
    assert total == 10 and len(lines) == 10
    assert "ALL of them are listed below" in prompt
    assert "tweet number 9" in lines[0] and "tweet number 0" in lines[-1]

    # A cap that holds the newest six lines and part of the seventh.
    six = sum(len(line) + 1 for line in lines[:6])
    monkeypatch.setitem(_digest.flask_app.config, "DIGEST_MAX_UNITS",
                        math.ceil(six / 4) + 2)
    prompt, total = _digest._render_digest_prompt(user.id)
    assert _corpus_lines(prompt) == lines[:6]
    assert ("The corpus holds 10 saved items; the 6 most recent are listed "
            "below (4 older items not shown).") in prompt


def test_more_than_1500_items_are_listed_while_they_fit_the_cap(app):
    """The item cap of 1,500 is gone: a list of short items is cut by the
    unit cap alone."""
    user = User.query.first()
    start = datetime(2025, 1, 1)
    for i in range(1600):
        _item(user.id, f"t{i}", text=f"short {i}",
              posted_at=start + timedelta(hours=i))
    _db.session.commit()

    prompt, total = _digest._render_digest_prompt(user.id)
    assert total == 1600
    assert len(_corpus_lines(prompt)) == 1600
    assert "The corpus holds 1600 saved items; ALL of them" in prompt


def test_items_past_the_cap_are_not_decrypted(app, monkeypatch):
    """The list is read newest first and decrypted only up to the cap (one
    key-service call per item when encryption is on)."""
    user = User.query.first()
    start = datetime(2026, 1, 1)
    for i in range(40):
        _item(user.id, f"t{i}", text="y" * 100,
              posted_at=start + timedelta(days=i))
    _db.session.commit()
    read = []
    real = ExternalItem.get_content
    monkeypatch.setattr(ExternalItem, "get_content",
                        lambda self: read.append(self.id) or real(self))
    # 400 characters: three lines of about 125 characters.
    monkeypatch.setitem(_digest.flask_app.config, "DIGEST_MAX_UNITS", 100)

    prompt, _ = _digest._render_digest_prompt(user.id)
    listed = len(_corpus_lines(prompt))
    assert 0 < listed < 5
    assert len(read) == listed + 1   # the one that did not fit


def _config_class(monkeypatch, **env):
    """config.py loaded on its own with the given environment, under a
    name of its own, so the Config every other test reads is untouched."""
    monkeypatch.delenv("DIGEST_MAX_UNITS", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    path = Path(__file__).resolve().parents[1] / "config.py"
    spec = importlib.util.spec_from_file_location("_digest_config_probe",
                                                  path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Config


def test_the_cap_is_one_setting_overridable_by_the_environment(monkeypatch):
    assert _config_class(monkeypatch).DIGEST_MAX_UNITS == 250_000
    assert _config_class(
        monkeypatch, DIGEST_MAX_UNITS="200000").DIGEST_MAX_UNITS == 200_000


def test_the_shipped_digest_models_are_the_middle_models():
    """Peter, 2026-11-24: Sonnet 5.5 and GPT-6.2 Sol; Haiku 5.5 and Luna
    are not used for the digest."""
    from backend.config import Config
    assert Config.DIGEST_MODEL_ANTHROPIC == "claude-sonnet-5.5"
    assert Config.DIGEST_MODEL_OPENAI == "gpt-6.2-sol"


# ── The order: an undated item by when it was saved ─────────────────────

def test_an_undated_item_is_ordered_by_when_it_was_saved(app, monkeypatch):
    """A PDF or a page that states no date sits among the dated items by
    the day it was saved (saved_at, which a re-clip does not move), not
    after all of them, so the cap no longer cuts it first."""
    user = User.query.first()
    now = datetime.utcnow()
    _item(user.id, "dated-new", text="dated new",
          posted_at=datetime(2026, 1, 10))
    _item(user.id, "dated-old", text="dated old",
          posted_at=datetime(2025, 1, 1))
    _item(user.id, "undated-saved-feb", text="undated saved in February",
          saved_at=datetime(2026, 2, 1), fetched_at=datetime(2026, 2, 1))
    # Saved long ago and re-clipped today: fetched_at moved, saved_at not.
    _item(user.id, "undated-reclipped", text="undated re-clipped today",
          saved_at=datetime(2024, 6, 1), fetched_at=now)
    # From before saved_at existed: fetched_at stands in for it.
    _item(user.id, "undated-legacy", text="undated legacy row",
          fetched_at=datetime(2025, 6, 1))
    _db.session.commit()
    ExternalItem.query.filter_by(external_id="undated-legacy").update(
        {"saved_at": None})
    _db.session.commit()

    prompt, _ = _digest._render_digest_prompt(user.id)
    order = [line.split(": ", 1)[1] for line in _corpus_lines(prompt)]
    assert order == ["undated saved in February", "dated new",
                     "undated legacy row", "dated old",
                     "undated re-clipped today"]
    # The undated item keeps "?" for its posting date.
    assert "(?): undated saved in February" in prompt

    lines = _corpus_lines(prompt)
    two = sum(len(line) + 1 for line in lines[:2])
    monkeypatch.setitem(_digest.flask_app.config, "DIGEST_MAX_UNITS",
                        math.ceil(two / 4))
    prompt, _ = _digest._render_digest_prompt(user.id)
    assert [line.split(": ", 1)[1] for line in _corpus_lines(prompt)] == [
        "undated saved in February", "dated new"]


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
    assert job.provider_key == "openai:gpt-6.2-sol"

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
    assert (log.model_id, log.cost_microdollars) == ("gpt-6.2-sol", cost)
    assert UserArtifact.latest_for(
        user.id, _digest.DIGEST_KIND).generated_by == "gpt-6.2-sol"


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
    assert (log.model_id, log.cost_microdollars) == ("claude-sonnet-5.5",
                                                     1500)


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
    assert [r["model_id"] for r in submitted[0]["anthropic"]] == [
        "claude-sonnet-5.5"]
