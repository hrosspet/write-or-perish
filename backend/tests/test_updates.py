"""Tests for the dev-update channel (#207): changelog parsing + per-user
read state, targeted notifications, and the two-phase-consent poll flow.

Same minimal-app pattern as test_share.py: sqlite in-memory, only the
blueprints under test, session login via _user_id.
"""
import os
import sys
import textwrap
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from flask import Flask

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_DISABLED", "true")

from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    User, UserNotification, Poll, PollResponse, ChangelogReadState,
    PollDraftBatchJob, APICostLog, UserProfile,
)
import backend.utils.changelog as changelog_mod  # noqa: E402
from backend.utils.changelog import (  # noqa: E402
    parse_changelog, unread_sections_for,
)
from backend.utils.notifications import (  # noqa: E402
    notify_user, notify_profile_ready,
)
from backend.utils.system_accounts import (  # noqa: E402
    get_poll_system_user, POLL_SYSTEM_USERNAME,
)

# ── Import the real poll_draft module against stub glue (same pattern as
# test_share.py): backend.celery_app would boot the full app. ──
_GLUE = ("backend.celery_app",)
_saved_glue = {k: sys.modules.get(k) for k in _GLUE}
sys.modules["backend.celery_app"] = MagicMock()
sys.modules.pop("backend.tasks.poll_draft", None)
import backend.tasks.poll_draft as poll_draft_mod  # noqa: E402
for _k, _v in _saved_glue.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v
# Route tests stub sys.modules["backend.tasks.poll_draft"]; keep the real
# module reachable for the pipeline tests regardless of ordering.
sys.modules["backend.tasks.poll_draft"] = poll_draft_mod


SAMPLE = textwrap.dedent("""\
    # Loore — what's new

    <!--
    Authoring notes that must never leak into a section body.
    -->

    <!-- id: second-feature -->
    ## 2026-07-02 — The second feature

    Newest section body.

    ## 2026-06-01 — First feature

    Older body with **markdown**.

    ## Undated announcement

    No date on this one.
    """)


@pytest.fixture
def changelog_file(tmp_path):
    path = tmp_path / "user_changelog.md"
    path.write_text(SAMPLE, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class TestParser:
    def test_sections_ids_dates_order(self, changelog_file):
        sections = parse_changelog(changelog_file)
        assert [s["id"] for s in sections] == [
            "second-feature", "first-feature", "undated-announcement"]
        assert sections[0]["date"].isoformat() == "2026-07-02"
        assert sections[2]["date"] is None
        assert sections[0]["title"] == "The second feature"

    def test_bodies_and_comment_stripping(self, changelog_file):
        sections = parse_changelog(changelog_file)
        assert sections[0]["body"] == "Newest section body."
        assert "Authoring notes" not in sections[0]["body"]
        for s in sections:
            assert "<!--" not in s["body"]

    def test_missing_file_is_empty(self, tmp_path):
        assert parse_changelog(str(tmp_path / "nope.md")) == []

    def test_duplicate_ids_keep_first(self, tmp_path):
        path = tmp_path / "dup.md"
        path.write_text(
            "<!-- id: x -->\n## 2026-01-01 — A\n\nfirst\n\n"
            "<!-- id: x -->\n## 2026-01-02 — B\n\nsecond\n",
            encoding="utf-8")
        sections = parse_changelog(str(path))
        assert len(sections) == 1
        assert sections[0]["title"] == "A"

    def test_cache_invalidated_on_change(self, tmp_path):
        path = tmp_path / "c.md"
        path.write_text("## 2026-01-01 — One\n\nbody\n", encoding="utf-8")
        assert len(parse_changelog(str(path))) == 1
        path.write_text(
            "## 2026-01-01 — One\n\nbody\n\n"
            "## 2026-01-02 — Two\n\nmore body\n", encoding="utf-8")
        assert len(parse_changelog(str(path))) == 2


class TestUnread:
    def _user(self, created_at):
        user = MagicMock()
        user.created_at = created_at
        return user

    def test_all_unread_without_state(self, changelog_file):
        user = self._user(datetime(2026, 1, 1))
        assert len(unread_sections_for(user, [], changelog_file)) == 3

    def test_read_hides_skip_keeps(self, changelog_file):
        user = self._user(datetime(2026, 1, 1))
        read = MagicMock(section_id="second-feature", status="read")
        skipped = MagicMock(section_id="first-feature", status="skipped")
        ids = [s["id"] for s in
               unread_sections_for(user, [read, skipped], changelog_file)]
        assert "second-feature" not in ids
        assert "first-feature" in ids

    def test_no_backlog_for_new_users(self, changelog_file):
        # Signed up 2026-06-15: the 06-01 section is pre-signup (hidden),
        # the 07-02 one and the undated one still show.
        user = self._user(datetime(2026, 6, 15))
        ids = [s["id"] for s in unread_sections_for(user, [], changelog_file)]
        assert ids == ["second-feature", "undated-announcement"]

    def test_same_day_signup_still_shows(self, changelog_file):
        user = self._user(datetime(2026, 7, 2, 23, 59))
        ids = [s["id"] for s in unread_sections_for(user, [], changelog_file)]
        assert "second-feature" in ids


# ---------------------------------------------------------------------------
# App / API
# ---------------------------------------------------------------------------

def _make_app(dev_updates=True):
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["DEV_UPDATES_V1"] = dev_updates
    app.config["DEFAULT_LLM_MODEL"] = "test-model"
    app.config["SUPPORTED_MODELS"] = {
        "test-model": {
            "provider": "anthropic", "api_model": "test-model-api",
            "display_name": "Test Model", "context_window": 200000,
        },
    }

    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.updates import updates_bp
    app.register_blueprint(updates_bp, url_prefix="/api/updates")
    from backend.routes.admin import admin_bp
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    return app


@pytest.fixture
def app(changelog_file, monkeypatch):
    monkeypatch.setattr(changelog_mod, "CHANGELOG_PATH", changelog_file)
    app = _make_app()
    with app.app_context():
        _db.create_all()
        user = User(username="tester", approved=True,
                    created_at=datetime(2026, 1, 1),
                    default_ai_usage="chat")
        admin = User(username="boss", approved=True, is_admin=True,
                     created_at=datetime(2026, 1, 1))
        _db.session.add_all([user, admin])
        _db.session.commit()
        yield app
        _db.session.rollback()
        _db.drop_all()


def _login(app, username):
    client = app.test_client()
    user = User.query.filter_by(username=username).first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


@pytest.fixture
def client(app):
    return _login(app, "tester")


@pytest.fixture
def admin_client(app):
    return _login(app, "boss")


class TestUpdatesAPI:
    def test_requires_login(self, app):
        res = app.test_client().get("/api/updates")
        assert res.status_code in (302, 401)

    def test_get_updates_full_payload(self, client):
        res = client.get("/api/updates")
        assert res.status_code == 200
        data = res.get_json()
        assert [s["id"] for s in data["changelog"]] == [
            "second-feature", "first-feature", "undated-announcement"]
        assert data["notifications"] == []
        assert data["polls"] == []

    def test_read_and_skip_semantics(self, client):
        assert client.post(
            "/api/updates/changelog/second-feature/read").status_code == 200
        assert client.post(
            "/api/updates/changelog/first-feature/skip").status_code == 200
        ids = [s["id"] for s in client.get(
            "/api/updates").get_json()["changelog"]]
        assert "second-feature" not in ids     # read → gone
        assert "first-feature" in ids          # skipped → back next open

    def test_unknown_section_404(self, client):
        res = client.post("/api/updates/changelog/nope/read")
        assert res.status_code == 404
        assert ChangelogReadState.query.count() == 0

    def test_killswitch_serves_nothing(self, changelog_file, monkeypatch):
        monkeypatch.setattr(changelog_mod, "CHANGELOG_PATH", changelog_file)
        app = _make_app(dev_updates=False)
        with app.app_context():
            _db.create_all()
            user = User(username="u", approved=True)
            _db.session.add(user)
            _db.session.commit()
            client = _login(app, "u")
            data = client.get("/api/updates").get_json()
            assert data == {
                "changelog": [], "notifications": [], "polls": []}
            _db.drop_all()


class TestNotifications:
    def test_notify_dedupes_unread(self, app):
        user = User.query.filter_by(username="tester").first()
        notify_profile_ready(user.id)
        notify_profile_ready(user.id)
        assert UserNotification.query.filter_by(
            user_id=user.id, type="profile_ready").count() == 1

    def test_read_then_new_one_stacks(self, app, client):
        user = User.query.filter_by(username="tester").first()
        first = notify_profile_ready(user.id)
        res = client.post(f"/api/updates/notifications/{first.id}/read")
        assert res.status_code == 200
        notify_profile_ready(user.id)
        assert UserNotification.query.filter_by(
            user_id=user.id, type="profile_ready").count() == 2
        unread = client.get("/api/updates").get_json()["notifications"]
        assert len(unread) == 1

    def test_skip_keeps_unread(self, app, client):
        user = User.query.filter_by(username="tester").first()
        n = notify_user(user.id, "fix_ready", "A fix you reported is live")
        client.post(f"/api/updates/notifications/{n.id}/skip")
        unread = client.get("/api/updates").get_json()["notifications"]
        assert [x["id"] for x in unread] == [n.id]

    def test_cannot_touch_others_notification(self, app, admin_client):
        user = User.query.filter_by(username="tester").first()
        n = notify_profile_ready(user.id)
        res = admin_client.post(f"/api/updates/notifications/{n.id}/read")
        assert res.status_code == 404


def _stub_poll_draft_task(monkeypatch):
    """The route imports the celery task lazily; give it a stub module so
    tests never touch backend.celery_app."""
    stub = MagicMock()
    stub.submit_poll_draft.delay.return_value = MagicMock(id="task-123")
    monkeypatch.setitem(sys.modules, "backend.tasks.poll_draft", stub)
    return stub


class TestPolls:
    def _poll(self):
        poll = Poll(question="What's missing in Loore?")
        _db.session.add(poll)
        _db.session.commit()
        return poll

    def test_pending_poll_appears_in_updates(self, app, client):
        poll = self._poll()
        polls = client.get("/api/updates").get_json()["polls"]
        assert [p["id"] for p in polls] == [poll.id]
        assert polls[0]["response"] is None

    def test_optin1_dispatches_draft(self, app, client, monkeypatch):
        stub = _stub_poll_draft_task(monkeypatch)
        poll = self._poll()
        res = client.post(f"/api/updates/polls/{poll.id}/draft")
        assert res.status_code == 202
        assert res.get_json()["response"]["status"] == "drafting"
        stub.submit_poll_draft.delay.assert_called_once()
        resp = PollResponse.query.filter_by(poll_id=poll.id).first()
        assert resp.draft_requested_at is not None

    def test_optin1_refused_when_ai_opted_out(self, app, client,
                                              monkeypatch):
        stub = _stub_poll_draft_task(monkeypatch)
        user = User.query.filter_by(username="tester").first()
        user.default_ai_usage = "none"
        _db.session.commit()
        poll = self._poll()
        res = client.post(f"/api/updates/polls/{poll.id}/draft")
        assert res.status_code == 403
        stub.submit_poll_draft.delay.assert_not_called()
        assert PollResponse.query.count() == 0

    def test_draft_terms_shown_before_optin(self, app, client):
        poll = Poll(question="?", model_id="test-model",
                    data_source="recent_window")
        _db.session.add(poll)
        _db.session.commit()
        polls = client.get("/api/updates").get_json()["polls"]
        assert polls[0]["draft_terms"] == {
            "model": "Test Model", "data_source": "recent_window"}

    def test_send_requires_content(self, app, client):
        poll = self._poll()
        res = client.post(f"/api/updates/polls/{poll.id}/send")
        assert res.status_code == 400

    def test_manual_answer_then_send(self, app, client):
        poll = self._poll()
        res = client.put(f"/api/updates/polls/{poll.id}/response",
                         json={"content": "More silence between prompts."})
        assert res.status_code == 200
        res = client.post(f"/api/updates/polls/{poll.id}/send")
        assert res.status_code == 200
        resp = PollResponse.query.filter_by(poll_id=poll.id).first()
        assert resp.status == "sent"
        assert resp.sent_at is not None
        # resolved → gone from the surface
        assert client.get("/api/updates").get_json()["polls"] == []

    def test_decline_hides_poll(self, app, client):
        poll = self._poll()
        client.post(f"/api/updates/polls/{poll.id}/decline")
        assert client.get("/api/updates").get_json()["polls"] == []

    def test_closed_poll_rejects_answers(self, app, client):
        poll = self._poll()
        poll.closed_at = datetime.utcnow()
        _db.session.commit()
        res = client.put(f"/api/updates/polls/{poll.id}/response",
                         json={"content": "too late"})
        assert res.status_code == 409
        assert client.get("/api/updates").get_json()["polls"] == []

    def test_edit_clears_generated_by(self, app, client):
        poll = self._poll()
        resp = PollResponse(poll_id=poll.id, status="draft",
                            generated_by="claude-opus-4.6",
                            user_id=User.query.filter_by(
                                username="tester").first().id)
        resp.set_content("AI draft")
        _db.session.add(resp)
        _db.session.commit()
        client.put(f"/api/updates/polls/{poll.id}/response",
                   json={"content": "my own words"})
        _db.session.refresh(resp)
        assert resp.generated_by is None

    def test_sending_unedited_draft_keeps_provenance(self, app, client):
        # The send flow PUTs the current text before /send; a verbatim
        # AI draft must keep its (AI-drafted) marker for the admin.
        poll = self._poll()
        resp = PollResponse(poll_id=poll.id, status="draft",
                            generated_by="claude-opus-4.6",
                            user_id=User.query.filter_by(
                                username="tester").first().id)
        resp.set_content("AI draft")
        _db.session.add(resp)
        _db.session.commit()
        client.put(f"/api/updates/polls/{poll.id}/response",
                   json={"content": "AI draft"})
        client.post(f"/api/updates/polls/{poll.id}/send")
        _db.session.refresh(resp)
        assert resp.status == "sent"
        assert resp.generated_by == "claude-opus-4.6"


class TestAdminPolls:
    def test_admin_required(self, client):
        assert client.post("/api/admin/polls",
                           json={"question": "?"}).status_code == 403
        assert client.get("/api/admin/polls").status_code == 403

    def test_create_list_close(self, admin_client):
        res = admin_client.post("/api/admin/polls",
                                json={"question": "How is voice mode?"})
        assert res.status_code == 201
        created = res.get_json()
        # Defaults: server default model, derived context
        assert created["model_id"] == "test-model"
        assert created["data_source"] == "derived"
        poll_id = created["id"]
        polls = admin_client.get("/api/admin/polls").get_json()["polls"]
        assert polls[0]["id"] == poll_id
        assert polls[0]["sent_count"] == 0
        assert polls[0]["model_id"] == "test-model"
        res = admin_client.post(f"/api/admin/polls/{poll_id}/close")
        assert res.status_code == 200
        assert res.get_json()["closed_at"] is not None

    def test_create_validates_model_and_source(self, admin_client):
        assert admin_client.post("/api/admin/polls", json={
            "question": "?", "model_id": "nope"}).status_code == 400
        assert admin_client.post("/api/admin/polls", json={
            "question": "?", "data_source": "everything"
        }).status_code == 400
        res = admin_client.post("/api/admin/polls", json={
            "question": "?", "model_id": "test-model",
            "data_source": "recent_window"})
        assert res.status_code == 201
        assert res.get_json()["data_source"] == "recent_window"

    def test_admin_sees_only_sent_responses(self, app, admin_client):
        user = User.query.filter_by(username="tester").first()
        poll = Poll(question="?")
        _db.session.add(poll)
        _db.session.commit()

        private_draft = PollResponse(
            poll_id=poll.id, user_id=user.id, status="draft")
        private_draft.set_content("PRIVATE draft — never visible")
        sent = PollResponse(
            poll_id=poll.id, status="sent", sent_at=datetime.utcnow(),
            user_id=User.query.filter_by(username="boss").first().id)
        sent.set_content("shared on purpose")
        _db.session.add_all([private_draft, sent])
        _db.session.commit()

        data = admin_client.get(
            f"/api/admin/polls/{poll.id}/responses").get_json()
        assert len(data["responses"]) == 1
        assert data["responses"][0]["content"] == "shared on purpose"
        counts = admin_client.get("/api/admin/polls").get_json()["polls"][0]
        assert counts["sent_count"] == 1


# ---------------------------------------------------------------------------
# Batch draft pipeline (submit → collect), cost attribution
# ---------------------------------------------------------------------------

TEST_MODELS_CONFIG = {
    "SUPPORTED_MODELS": {
        "test-model": {
            "provider": "anthropic", "api_model": "test-model-api",
            "display_name": "Test Model", "context_window": 200000,
        },
    },
    "DEFAULT_LLM_MODEL": "test-model",
}


@pytest.fixture
def pipeline(app, monkeypatch):
    """Wire poll_draft's glue (stub flask_app config, no-op app_context,
    captured batch calls) inside the real test app context."""
    prev_config = poll_draft_mod.flask_app.config
    poll_draft_mod.flask_app.config = dict(TEST_MODELS_CONFIG)
    monkeypatch.setattr(poll_draft_mod, "get_api_keys_for_usage",
                        lambda *a, **k: {"anthropic": "k", "openai": "k"})
    monkeypatch.setattr(poll_draft_mod, "apply_batch_key_override",
                        lambda keys, cfg: keys)
    yield poll_draft_mod
    poll_draft_mod.flask_app.config = prev_config


def _make_drafting_response(model_id="test-model", data_source="derived",
                            with_profile=True):
    user = User.query.filter_by(username="tester").first()
    if with_profile:
        profile = UserProfile(user_id=user.id, generated_by="user",
                              tokens_used=0)
        profile.set_content("PROFILE: daily journaler, voice mode.")
        _db.session.add(profile)
    poll = Poll(question="What's missing?", model_id=model_id,
                data_source=data_source)
    _db.session.add(poll)
    _db.session.commit()
    resp = PollResponse(poll_id=poll.id, user_id=user.id,
                        status="drafting",
                        draft_requested_at=datetime.utcnow())
    _db.session.add(resp)
    _db.session.commit()
    return poll, resp


class TestDraftBatchPipeline:
    def test_submit_creates_job(self, pipeline, monkeypatch):
        captured = {}

        def fake_submit(requests_by_provider, keys, phase=None):
            captured.update(requests_by_provider)
            return {"anthropic": "batch-abc"}

        monkeypatch.setattr(pipeline, "batch_submit", fake_submit)
        poll, resp = _make_drafting_response()
        pipeline._submit_poll_draft(resp.id, task_id="t-1")

        job = PollDraftBatchJob.query.one()
        assert job.provider_key == "anthropic"
        assert job.batch_id == "batch-abc"
        assert job.status == "pending"
        assert job.items == [{
            "custom_id": f"poll-draft-{resp.id}",
            "response_id": resp.id, "poll_id": poll.id,
            "model_id": "test-model",
        }]
        request = captured["anthropic"][0]
        body = request["messages"][1]["content"]
        assert "PROFILE: daily journaler" in body
        assert "What's missing?" in body
        assert request["max_tokens"] == pipeline.MAX_DRAFT_TOKENS
        assert resp.status == "drafting"

    def test_submit_recent_window_uses_export(self, pipeline, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            pipeline, "batch_submit",
            lambda reqs, keys, phase=None: (
                captured.update(reqs) or {"anthropic": "batch-w"}))
        exports_stub = MagicMock()
        exports_stub.build_user_export_content.return_value = (
            "RECENT RAW WRITING")
        monkeypatch.setitem(sys.modules, "backend.tasks.exports",
                            exports_stub)

        poll, resp = _make_drafting_response(
            data_source="recent_window", with_profile=False)
        pipeline._submit_poll_draft(resp.id)

        kwargs = exports_stub.build_user_export_content.call_args.kwargs
        assert kwargs["max_tokens"] == (
            200000 - pipeline.MAX_DRAFT_TOKENS
            - pipeline.WINDOW_OVERHEAD_TOKENS)
        assert kwargs["filter_ai_usage"] is True
        assert "RECENT RAW WRITING" in (
            captured["anthropic"][0]["messages"][1]["content"])

    def test_submit_without_context_fails_soft(self, pipeline,
                                               monkeypatch):
        submit = MagicMock()
        monkeypatch.setattr(pipeline, "batch_submit", submit)
        poll, resp = _make_drafting_response(with_profile=False)
        pipeline._submit_poll_draft(resp.id)
        assert resp.status == "draft_failed"
        submit.assert_not_called()
        assert PollDraftBatchJob.query.count() == 0

    def _pending_job(self, poll, resp):
        job = PollDraftBatchJob(
            provider_key="anthropic", batch_id="batch-abc",
            items=[{"custom_id": f"poll-draft-{resp.id}",
                    "response_id": resp.id, "poll_id": poll.id,
                    "model_id": "test-model"}])
        _db.session.add(job)
        _db.session.commit()
        return job

    def test_collect_saves_draft_and_attributes_cost(self, pipeline,
                                                     monkeypatch):
        poll, resp = _make_drafting_response()
        job = self._pending_job(poll, resp)
        monkeypatch.setattr(
            pipeline, "batch_check_and_collect",
            lambda ids, keys: ({f"poll-draft-{resp.id}": {
                "content": " A drafted answer. ",
                "input_tokens": 100, "output_tokens": 50,
            }}, {}, {}))
        cost_calls = {}

        def fake_cost(model_id, inp, out, batch=False):
            cost_calls.update(model=model_id, batch=batch)
            return 4242

        monkeypatch.setattr(
            pipeline, "calculate_llm_cost_microdollars", fake_cost)

        pipeline._collect_poll_draft_batches()

        assert resp.status == "draft"
        assert resp.get_content() == "A drafted answer."
        assert resp.generated_by == "test-model"
        assert job.status == "collected"

        log = APICostLog.query.one()
        system_user = User.query.filter_by(
            username=POLL_SYSTEM_USERNAME).one()
        assert log.user_id == system_user.id          # NOT the tester
        assert log.request_type == "poll_draft"
        assert log.request_ref == f"poll:{poll.id}"   # traceable to poll
        assert log.cost_microdollars == 4242
        assert cost_calls == {"model": "test-model", "batch": True}
        # System account can't log in / never gets profile-generated
        assert system_user.approved is False
        assert system_user.plan == "free"

    def test_collect_still_pending_leaves_job(self, pipeline, monkeypatch):
        poll, resp = _make_drafting_response()
        job = self._pending_job(poll, resp)
        monkeypatch.setattr(
            pipeline, "batch_check_and_collect",
            lambda ids, keys: ({}, {"anthropic": "batch-abc"}, {}))
        pipeline._collect_poll_draft_batches()
        assert job.status == "pending"
        assert resp.status == "drafting"

    def test_collect_missing_item_fails_response(self, pipeline,
                                                 monkeypatch):
        poll, resp = _make_drafting_response()
        job = self._pending_job(poll, resp)
        monkeypatch.setattr(
            pipeline, "batch_check_and_collect",
            lambda ids, keys: ({}, {}, {}))  # ended, no result
        pipeline._collect_poll_draft_batches()
        assert job.status == "collected"
        assert resp.status == "draft_failed"

    def test_system_account_is_idempotent(self, app):
        first = get_poll_system_user()
        second = get_poll_system_user()
        assert first.id == second.id
        assert first.username == POLL_SYSTEM_USERNAME
