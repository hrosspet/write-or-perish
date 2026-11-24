"""The profile batch poller goes on past one account it cannot handle.

Every pending job of every account is collected in one pass. An account
whose saved Profile Generation prompt cannot be read gets the outcome
"error" on its window items (a counted, bounded outcome) and every other
job of the pass is collected; a stale job is marked failed even then, so
it is not tried again every minute; and any other failure in one job's
handling leaves the later jobs collected and the chained steps of the
earlier ones submitted. Harness: tests/test_profile_windows.py (sqlite in
memory, a fake provider); encryption on with a local key-service stand-in.
"""
from datetime import datetime, timedelta

import pytest
from flask_login import LoginManager

from backend.tests import test_profile_windows as tw
from backend.tests.test_profile_windows import app, world  # noqa: F401
from backend.extensions import db
from backend.models import ProfileBatchJob, User, UserProfile, UserPrompt
from backend.utils import encryption

UNREADABLE = "ENC:v2:AAAA:BBBB"


def _kms_on(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"wrapped:" + dek)

    def unwrap(w):
        if not w.startswith(b"wrapped:"):
            raise ValueError("key service: unknown wrapped key")
        return w[len(b"wrapped:"):]
    monkeypatch.setattr(encryption, "_unwrap_dek", unwrap)


def _statuses():
    return [j.status for j in
            ProfileBatchJob.query.order_by(ProfileBatchJob.id).all()]


def _three_accounts_with_pending_jobs(app, world):  # noqa: F811
    app.config["PROFILE_USE_BATCH"] = False
    world.corpus = tw._windows_corpus(3)
    first = tw._user(profile_needs_full_regen=True)
    middle = tw._user(profile_needs_full_regen=True)
    last = tw._user(profile_needs_full_regen=True)
    tw._advance(first)
    tw._advance(middle)
    tw._advance(last)
    assert _statuses() == ["pending"] * 3
    return first.id, middle.id, last.id


def _store_unreadable_prompt(user_id):
    """A saved prompt whose stored value does not decrypt, written to the
    column directly (as such rows were stored before)."""
    row = UserPrompt(user_id=user_id, prompt_key="profile_generation",
                     title="Profile Generation", generated_by="user")
    row.content = UNREADABLE
    db.session.add(row)
    db.session.commit()


def test_an_unreadable_prompt_fails_only_that_accounts_items(
        app, world, monkeypatch):  # noqa: F811
    _kms_on(monkeypatch)
    first_id, middle_id, last_id = _three_accounts_with_pending_jobs(
        app, world)
    _store_unreadable_prompt(middle_id)

    tw._poll()
    assert _statuses() == ["collected"] * 3
    assert UserProfile.query.filter_by(user_id=first_id).count() > 0
    assert UserProfile.query.filter_by(user_id=last_id).count() > 0
    middle_job = ProfileBatchJob.query.order_by(ProfileBatchJob.id).all()[1]
    assert {i["outcome"] for i in middle_job.items} == {"error"}
    assert UserProfile.query.filter_by(user_id=middle_id).count() == 0


def test_a_stale_job_of_such_an_account_is_marked_failed(
        app, world, monkeypatch):  # noqa: F811
    _kms_on(monkeypatch)
    first_id, middle_id, last_id = _three_accounts_with_pending_jobs(
        app, world)
    _store_unreadable_prompt(middle_id)
    middle_job = ProfileBatchJob.query.order_by(ProfileBatchJob.id).all()[1]
    middle_job.submitted_at = datetime.utcnow() - timedelta(hours=26)
    db.session.commit()

    tw._poll()
    assert _statuses() == ["collected", "failed", "collected"]
    assert UserProfile.query.filter_by(user_id=last_id).count() > 0


def test_a_failure_in_one_jobs_handling_leaves_the_rest_of_the_pass(
        app, world, monkeypatch):  # noqa: F811
    first_id, middle_id, last_id = _three_accounts_with_pending_jobs(
        app, world)
    real_collect = tw.pw.collect

    def collect(job, items, results, queue_full=False):
        if any(i["user_id"] == middle_id for i in items):
            raise RuntimeError("one job's handling failed")
        return real_collect(job, items, results, queue_full=queue_full)
    monkeypatch.setattr(tw.pw, "collect", collect)
    submitted = []
    real_submit = tw.pb._submit_requests

    def submit(built, keys):
        submitted.append(len(built))
        return real_submit(built, keys)
    monkeypatch.setattr(tw.pb, "_submit_requests", submit)

    tw._poll()
    assert _statuses() == ["collected", "pending", "collected"]
    assert UserProfile.query.filter_by(user_id=last_id).count() > 0
    assert submitted == [0]


def test_a_prompt_saved_with_such_text_is_used_as_written(
        app, world, monkeypatch):  # noqa: F811
    _kms_on(monkeypatch)
    first_id, middle_id, last_id = _three_accounts_with_pending_jobs(
        app, world)
    from backend.routes.prompts import prompts_bp
    lm = LoginManager()
    lm.init_app(app)
    lm.request_loader(lambda req: db.session.get(User, middle_id))
    app.register_blueprint(prompts_bp, url_prefix="/api/prompts")
    resp = app.test_client().put("/api/prompts/profile_generation",
                                 json={"content": UNREADABLE})
    assert resp.status_code == 200
    row = UserPrompt.query.filter_by(
        user_id=middle_id, prompt_key="profile_generation").one()
    assert row.content != UNREADABLE
    assert row.get_content() == UNREADABLE
    db.session.remove()

    tw._poll()
    assert _statuses() == ["collected"] * 3
    assert UserProfile.query.filter_by(user_id=last_id).count() > 0


def test_a_key_service_outage_ends_the_pass_and_the_job_waits(
        app, world, monkeypatch):  # noqa: F811
    """Unlike one account's unreadable prompt, an outage of the key
    service ends the pass as before: the job stays pending with its
    results unused, and the chained steps built so far are submitted."""
    from google.api_core import exceptions as gexc
    _kms_on(monkeypatch)
    first_id, middle_id, last_id = _three_accounts_with_pending_jobs(
        app, world)
    for user_id in (middle_id, last_id):
        row = UserPrompt(user_id=user_id, prompt_key="profile_generation",
                         title="Profile Generation", generated_by="user")
        row.set_content("My own profile prompt.")
        db.session.add(row)
    db.session.commit()
    attempts = []

    def down(wrapped):
        attempts.append(wrapped)
        raise gexc.ServiceUnavailable("the service is unavailable")
    monkeypatch.setattr(encryption, "_unwrap_dek", down)
    submitted = []
    real_submit = tw.pb._submit_requests

    def submit(built, keys):
        submitted.append(len(built))
        return real_submit(built, keys)
    monkeypatch.setattr(tw.pb, "_submit_requests", submit)

    with pytest.raises(gexc.ServiceUnavailable):
        tw._poll()
    assert _statuses() == ["collected", "pending", "pending"]
    assert len(attempts) == 1
    assert submitted == [0]
    middle_job = ProfileBatchJob.query.order_by(ProfileBatchJob.id).all()[1]
    assert not any(i.get("outcome") for i in middle_job.items)
