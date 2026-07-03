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
)
import backend.utils.changelog as changelog_mod  # noqa: E402
from backend.utils.changelog import (  # noqa: E402
    parse_changelog, unread_sections_for,
)
from backend.utils.notifications import (  # noqa: E402
    notify_user, notify_profile_ready,
)


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
    stub.draft_poll_response.delay.return_value = MagicMock(id="task-123")
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
        stub.draft_poll_response.delay.assert_called_once()
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
        stub.draft_poll_response.delay.assert_not_called()
        assert PollResponse.query.count() == 0

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


class TestAdminPolls:
    def test_admin_required(self, client):
        assert client.post("/api/admin/polls",
                           json={"question": "?"}).status_code == 403
        assert client.get("/api/admin/polls").status_code == 403

    def test_create_list_close(self, admin_client):
        res = admin_client.post("/api/admin/polls",
                                json={"question": "How is voice mode?"})
        assert res.status_code == 201
        poll_id = res.get_json()["id"]
        polls = admin_client.get("/api/admin/polls").get_json()["polls"]
        assert polls[0]["id"] == poll_id
        assert polls[0]["sent_count"] == 0
        res = admin_client.post(f"/api/admin/polls/{poll_id}/close")
        assert res.status_code == 200
        assert res.get_json()["closed_at"] is not None

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
