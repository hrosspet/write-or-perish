"""Tests for the inbound GitHub webhook (issue-close -> targeted
notification through the dev-update channel).

Same minimal-app pattern as test_updates.py: sqlite in-memory, only the
blueprint under test.
"""
import hashlib
import hmac
import json
import os

import pytest
from flask import Flask

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_DISABLED", "true")

from backend.extensions import db as _db  # noqa: E402
from backend.models import User, UserNotification  # noqa: E402

SECRET = "test-webhook-secret"


def _make_app(secret=SECRET):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["GITHUB_WEBHOOK_SECRET"] = secret

    _db.init_app(app)

    from backend.routes.webhooks import webhooks_bp
    app.register_blueprint(webhooks_bp, url_prefix="/api/webhooks")
    return app


@pytest.fixture
def app():
    app = _make_app()
    with app.app_context():
        _db.create_all()
        _db.session.add(User(username="tester", approved=True))
        _db.session.commit()
        yield app
        _db.session.rollback()
        _db.drop_all()


def _signed_post(client, payload, event="issues", secret=SECRET,
                 signature=None):
    body = json.dumps(payload).encode("utf-8")
    if signature is None:
        digest = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        signature = f"sha256={digest}"
    return client.post(
        "/api/webhooks/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": signature,
        },
    )


def _issue_payload(action="closed", labels=("loore", "bug", "loore:tester"),
                   state_reason="completed", number=42,
                   title="Voice input drops words"):
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": title,
            "html_url": f"https://github.com/o/r/issues/{number}",
            "state_reason": state_reason,
            "labels": [{"name": name} for name in labels],
        },
    }


class TestAuth:
    def test_bad_signature_rejected(self, app):
        client = app.test_client()
        res = _signed_post(client, _issue_payload(),
                           signature="sha256=" + "0" * 64)
        assert res.status_code == 403
        assert UserNotification.query.count() == 0

    def test_missing_signature_rejected(self, app):
        client = app.test_client()
        res = client.post(
            "/api/webhooks/github",
            data=json.dumps(_issue_payload()),
            headers={"Content-Type": "application/json",
                     "X-GitHub-Event": "issues"},
        )
        assert res.status_code == 403

    def test_unconfigured_secret_disables_endpoint(self):
        app = _make_app(secret=None)
        with app.app_context():
            _db.create_all()
            res = _signed_post(app.test_client(), _issue_payload())
            assert res.status_code == 503
            _db.drop_all()

    def test_ping_event_pongs(self, app):
        res = _signed_post(app.test_client(), {"zen": "ok"}, event="ping")
        assert res.status_code == 200
        assert res.get_json()["status"] == "pong"


class TestIssueClosed:
    def test_completed_creates_fix_ready(self, app):
        res = _signed_post(app.test_client(), _issue_payload())
        assert res.get_json()["status"] == "notified"
        n = UserNotification.query.one()
        user = User.query.filter_by(username="tester").one()
        assert n.user_id == user.id
        assert n.type == "fix_ready"
        assert n.title.startswith("Fixed:")
        assert "Voice input drops words" in n.title
        assert "#42" in n.title
        assert n.body is None
        assert n.link == "https://github.com/o/r/issues/42"
        assert n.status == "unread"

    def test_not_planned_creates_declined(self, app):
        res = _signed_post(
            app.test_client(),
            _issue_payload(state_reason="not_planned"))
        assert res.get_json()["status"] == "notified"
        n = UserNotification.query.one()
        assert n.type == "issue_declined"
        assert "without a fix" in n.title
        assert n.body is None

    def test_long_issue_title_fits_column(self, app):
        res = _signed_post(
            app.test_client(),
            _issue_payload(title="x" * 300))
        assert res.get_json()["status"] == "notified"
        n = UserNotification.query.one()
        assert len(n.title) <= 200

    def test_two_closed_issues_do_not_collapse(self, app):
        client = app.test_client()
        _signed_post(client, _issue_payload(number=1, title="First"))
        _signed_post(client, _issue_payload(number=2, title="Second"))
        notifications = UserNotification.query.all()
        assert len(notifications) == 2
        assert {n.type for n in notifications} == {"fix_ready"}

    def test_non_loore_issue_ignored(self, app):
        res = _signed_post(
            app.test_client(),
            _issue_payload(labels=("bug",)))
        assert res.get_json()["status"] == "ignored"
        assert UserNotification.query.count() == 0

    def test_unknown_username_ignored(self, app):
        res = _signed_post(
            app.test_client(),
            _issue_payload(labels=("loore", "loore:ghost")))
        assert res.get_json()["status"] == "ignored"
        assert UserNotification.query.count() == 0

    def test_other_actions_ignored(self, app):
        res = _signed_post(app.test_client(),
                           _issue_payload(action="opened"))
        assert res.get_json()["status"] == "ignored"
        assert UserNotification.query.count() == 0

    def test_other_events_ignored(self, app):
        res = _signed_post(app.test_client(), _issue_payload(),
                           event="push")
        assert res.get_json()["status"] == "ignored"
        assert UserNotification.query.count() == 0
