"""The platform label on GitHub issues Loore files for a user.

Decision (Peter's voice review, 2026-10-02): one issue tracker for both
apps. An issue filed from the native iPhone app gets the ``ios`` label,
one filed from the web app (any browser, phone browsers and the PWA
included) gets ``web``; when the app can't be told, no platform label.

The app is read from the request (utils/client_platform): an explicit
``X-Loore-Client`` header first, else the User-Agent. An issue is filed
either by the "Create issue" request itself, or during a reply turn
(apply_github_issue), which uses the app of the request that started the
turn: create_llm_placeholder stamps it on the reply placeholder, and the
Voice finalize request hands it to its task, where the placeholder is
made. The turn's side is in test_issue_platform_task.py.

Routes run in the test_no_replies_for_none harness (in-memory sqlite, the
completion task stubbed); no real GitHub issue is filed.
"""
import json
import sys
import types
from unittest.mock import MagicMock

import pytest
from flask import Flask

from backend.tests.test_no_replies_for_none import (  # noqa: F401 (fixtures)
    app, task_mod, st, _user, _node, _reply, _client, _prompt_root,
    _voice_draft,
)
from backend.extensions import db as _db
from backend.models import Draft, Node
from backend.utils.client_platform import (
    CLIENT_MARKER, client_from_headers, request_client,
)

# What each app sends. The iPhone app's API requests go through URLSession
# with no User-Agent of its own: the system default, "<app>/<build>
# CFNetwork/<v> Darwin/<v>".
IOS_UA = "Loore/1 CFNetwork/1568.100.1 Darwin/24.0.0"
SAFARI_IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 "
    "Safari/604.1")
CHROME_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
CHROME_ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36")


def _meta(node):
    return json.loads(node.tool_calls_meta or "[]")


def _client_stamp(node):
    return [m for m in _meta(node) if m.get("name") == CLIENT_MARKER]


# ── Telling the apps apart ───────────────────────────────────────────────

@pytest.mark.parametrize("headers, expected", [
    ({"User-Agent": IOS_UA}, "ios"),
    ({"User-Agent": SAFARI_IPHONE_UA}, "web"),  # iPhone browser / PWA
    ({"User-Agent": CHROME_DESKTOP_UA}, "web"),
    ({"User-Agent": CHROME_ANDROID_UA}, "web"),
    ({"User-Agent": "curl/8.4.0"}, None),
    ({"User-Agent": "Werkzeug/3.0.1"}, None),
    ({"User-Agent": ""}, None),
    ({}, None),
    # An explicit header wins over the User-Agent, in any case...
    ({"X-Loore-Client": "ios", "User-Agent": CHROME_DESKTOP_UA}, "ios"),
    ({"X-Loore-Client": " WEB ", "User-Agent": IOS_UA}, "web"),
    ({"X-Loore-Client": "ios"}, "ios"),
    # ...and a value it doesn't know falls back to the User-Agent.
    ({"X-Loore-Client": "android", "User-Agent": IOS_UA}, "ios"),
    ({"X-Loore-Client": "android"}, None),
])
def test_client_from_headers(headers, expected):
    assert client_from_headers(headers) == expected


def test_request_client_reads_the_request():
    flask_app = Flask(__name__)
    assert request_client() is None  # no request at all
    with flask_app.test_request_context(headers={"User-Agent": IOS_UA}):
        assert request_client() == "ios"
    with flask_app.test_request_context(
            headers={"user-agent": SAFARI_IPHONE_UA}):
        assert request_client() == "web"
    with flask_app.test_request_context(
            headers={"x-loore-client": "ios",
                     "User-Agent": CHROME_DESKTOP_UA}):
        assert request_client() == "ios"
    with flask_app.test_request_context(headers={"User-Agent": "curl/8"}):
        assert request_client() is None


# ── The label ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("platform, extra", [
    ("ios", ["ios"]),
    ("web", ["web"]),
    (None, []),
    ("", []),
    ("android", []),  # unknown values are ignored
])
def test_create_github_issue_adds_the_platform_label(
        monkeypatch, platform, extra):
    import backend.utils.github as github
    flask_app = Flask(__name__)
    flask_app.config["GITHUB_TOKEN"] = "t"
    flask_app.config["GITHUB_REPO"] = "owner/repo"
    post = MagicMock()
    post.return_value.status_code = 201
    post.return_value.json.return_value = {
        "html_url": "https://github.com/owner/repo/issues/7", "number": 7}
    monkeypatch.setattr(github.requests, "post", post)

    with flask_app.app_context():
        result = github.create_github_issue(
            "Title", "Body", "bug", "alice", platform=platform)

    assert result == {
        "url": "https://github.com/owner/repo/issues/7", "number": 7}
    labels = post.call_args.kwargs["json"]["labels"]
    assert labels == ["loore", "bug", "loore:alice"] + extra


# ── The reply placeholder carries the turn's app ─────────────────────────

class TestPlaceholderStamp:
    def _make(self, **kwargs):
        from backend.utils.llm_nodes import create_llm_placeholder
        alice = _user()
        msg = _node(alice, _prompt_root(alice, "textmode"))
        _db.session.commit()
        node, _ = create_llm_placeholder(
            msg.id, "gpt-5", alice.id, enqueue=False, **kwargs)
        return node

    @pytest.mark.parametrize("ua, expected", [
        (IOS_UA, "ios"), (SAFARI_IPHONE_UA, "web")])
    def test_stamped_from_the_request(self, app, ua, expected):  # noqa: F811
        with app.test_request_context(headers={"User-Agent": ua}):
            node = self._make()
        assert _client_stamp(node) == [
            {"name": CLIENT_MARKER, "client": expected}]

    def test_unknown_app_is_not_stamped(self, app):  # noqa: F811
        with app.test_request_context(headers={"User-Agent": "curl/8"}):
            node = self._make()
        assert node.tool_calls_meta is None

    def test_no_request_no_stamp(self, app):  # noqa: F811
        assert self._make().tool_calls_meta is None

    def test_a_task_passes_the_app_its_request_recorded(self, app):  # noqa: F811
        assert _client_stamp(self._make(client="ios")) == [
            {"name": CLIENT_MARKER, "client": "ios"}]

    def test_other_markers_are_kept(self, app):  # noqa: F811
        node = self._make(meta=[{"name": "_read"}], client="web")
        assert _meta(node) == [
            {"name": "_read"}, {"name": CLIENT_MARKER, "client": "web"}]

    def test_a_reply_route_stamps_its_request(self, app, task_mod):  # noqa: F811
        alice = _user()
        leaf = _node(alice, _node(alice))
        _db.session.commit()
        resp = _client(app, alice).post(
            f"/api/nodes/{leaf.id}/llm", json={},
            headers={"User-Agent": IOS_UA})
        assert resp.status_code == 202, resp.get_json()
        reply = Node.query.filter_by(node_type="llm").one()
        assert _client_stamp(reply) == [
            {"name": CLIENT_MARKER, "client": "ios"}]


# ── Voice: the finalize request hands its app to the task ────────────────

def test_finalize_route_passes_the_app(app, monkeypatch):  # noqa: F811
    fake = types.ModuleType("backend.tasks.streaming_transcription")
    fake.finalize_draft_streaming = MagicMock()
    fake.finalize_draft_streaming.delay.return_value.id = "task-1"
    monkeypatch.setitem(
        sys.modules, "backend.tasks.streaming_transcription", fake)
    alice = _user()
    draft = Draft(user_id=alice.id, session_id="sess-ua",
                  streaming_status="recording")
    draft.set_content("")
    _db.session.add(draft)
    _db.session.commit()

    resp = _client(app, alice).post(
        "/api/drafts/streaming/sess-ua/finalize",
        json={"total_chunks": 1, "label": "Voice", "model": "gpt-5"},
        headers={"User-Agent": IOS_UA})

    assert resp.status_code == 202, resp.get_json()
    kwargs = fake.finalize_draft_streaming.delay.call_args.kwargs
    assert kwargs["client"] == "ios"


@pytest.mark.parametrize("client", ["ios", "web", None])
def test_finalize_task_stamps_the_reply(st, client):  # noqa: F811
    user = _user()
    _voice_draft(user, "sess-client", "chat", text="spoken words")

    st.finalize_draft_streaming(
        MagicMock(), "sess-client", 1, label="Voice", user_id=user.id,
        parent_id=None, model="gpt-5", client=client)

    reply = Node.query.filter_by(node_type="llm").one()
    expected = ([{"name": CLIENT_MARKER, "client": client}]
                if client else [])
    assert _client_stamp(reply) == expected
    st._fake_llm.generate_llm_response.si.assert_called_once()


# ── "Create issue": the app of that request ──────────────────────────────

@pytest.fixture
def gh_app(app, monkeypatch):  # noqa: F811
    """The harness app with the GitHub blueprint, and the GitHub call
    replaced by a recorder: no real issue is filed."""
    import backend.routes.github_issues as gh_routes
    app.register_blueprint(gh_routes.github_bp, url_prefix="/api/github")
    create = MagicMock(return_value={
        "url": "https://github.com/owner/repo/issues/9", "number": 9})
    monkeypatch.setattr(gh_routes, "create_github_issue", create)
    app.create_issue_mock = create
    return app


def _pending_issue(alice):
    root = _prompt_root(alice, "voice")
    msg = _node(alice, root, content="the record button does nothing")
    proposal = _reply(msg, alice, content=(
        "I'll draft that.\n\n"
        "### Issue Title\nRecord button does nothing\n"
        "### Description\nTapping record has no effect.\n"
        "### Category\nbug"))
    draft = Draft(user_id=alice.id, parent_id=proposal.id,
                  label="github_issue_pending")
    draft.set_content("")
    _db.session.add(draft)
    _db.session.commit()
    return proposal


@pytest.mark.parametrize("headers, platform", [
    ({"User-Agent": IOS_UA}, "ios"),
    ({"User-Agent": SAFARI_IPHONE_UA}, "web"),
    ({"User-Agent": CHROME_DESKTOP_UA}, "web"),
    ({"X-Loore-Client": "ios", "User-Agent": "curl/8"}, "ios"),
    ({"User-Agent": "curl/8"}, None),
])
def test_create_issue_route_labels_the_app(gh_app, headers, platform):
    alice = _user()
    proposal = _pending_issue(alice)

    resp = _client(gh_app, alice).post(
        "/api/github/create-issue", json={"llm_node_id": proposal.id},
        headers=headers)

    assert resp.status_code == 200, resp.get_json()
    gh_app.create_issue_mock.assert_called_once()
    kwargs = gh_app.create_issue_mock.call_args.kwargs
    assert kwargs["platform"] == platform
    assert kwargs["category"] == "bug"
    assert kwargs["title"] == "Record button does nothing"
