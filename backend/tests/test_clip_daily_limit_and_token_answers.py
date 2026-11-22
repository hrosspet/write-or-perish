"""The Chrome clipper's route: new clips per day, and what a token gets back.

- An account saves at most CLIPS_PER_DAY new pages or posts a day; the next
  new one answers 429 with a sentence the extension shows. Saving a known
  page again answers 200 as before and is not counted. With the counter
  out of reach the clip is saved.
- A request made with a token gets the fields the extension reads
  (`created`, `updated`, `source`, `truncated`), without the reference's id
  or stored title; the connection check gives it the username, the token's
  name and the number of clipped pages only.
- The same requests with a session answer as before.
"""
import pytest

from backend.tests.test_external_content import (  # noqa: F401
    app, client, _mint, _no_session,
)
from backend.extensions import db
from backend.models import ExternalItem, User
from backend.routes import external
from backend.utils import run_locks

URL = "https://example.com/an-article"


class _Counters:
    def __init__(self):
        self.counts = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.counts:
            return None
        self.counts[key] = int(value)
        return True

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]


@pytest.fixture
def counters(monkeypatch):
    fake = _Counters()
    monkeypatch.setattr(run_locks, "_client", lambda: fake)
    return fake


def _token_headers(client):
    # The token path refuses an account that is not approved.
    User.query.first().approved = True
    db.session.commit()
    return {"Authorization": f"Bearer {_mint(client)['token']}"}


def _clip(client, n, **more):
    return client.post("/api/external/clip", json={
        "url": f"https://example.com/page-{n}", "content": "text", **more})


def test_new_clips_stop_at_the_daily_number(app, client, counters,
                                            monkeypatch):
    monkeypatch.setattr(external, "CLIPS_PER_DAY", 3)
    for n in range(3):
        assert _clip(client, n).status_code == 201
    over = _clip(client, 3)
    assert over.status_code == 429
    body = over.get_json()
    assert body["code"] == "too_many_clips"
    assert body["error"] == external.CLIPS_PER_DAY_MESSAGE
    assert ExternalItem.query.count() == 3

    # A page saved before answers 200 at the limit too, and a fuller copy
    # still replaces the stored text.
    again = _clip(client, 0)
    assert again.status_code == 200 and again.get_json()["created"] is False
    fuller = client.post("/api/external/clip", json={
        "url": "https://example.com/page-1",
        "content": "text, and the rest of the page"})
    assert fuller.status_code == 200 and fuller.get_json()["updated"] is True


def test_saving_a_page_again_is_not_counted(app, client, counters,
                                            monkeypatch):
    monkeypatch.setattr(external, "CLIPS_PER_DAY", 2)
    assert _clip(client, 0).status_code == 201
    for _ in range(5):
        assert _clip(client, 0).status_code == 200
    assert _clip(client, 1).status_code == 201
    assert _clip(client, 2).status_code == 429


def test_the_number_is_per_account_for_token_and_session(app, client,
                                                         counters,
                                                         monkeypatch):
    monkeypatch.setattr(external, "CLIPS_PER_DAY", 2)
    headers = _token_headers(client)
    assert _clip(client, 0).status_code == 201
    by_token = _no_session(app, "post", "/api/external/clip", headers=headers,
                           json={"url": URL, "content": "text"})
    assert by_token.status_code == 201
    over = _no_session(app, "post", "/api/external/clip", headers=headers,
                       json={"url": URL + "/2", "content": "text"})
    assert over.status_code == 429
    # What the released extension shows: "Loore answered 429: <error>."
    assert over.get_json()["error"] == external.CLIPS_PER_DAY_MESSAGE
    assert _clip(client, 1).status_code == 429


def test_the_shipped_number_is_far_above_a_day_of_key_presses():
    assert external.CLIPS_PER_DAY == 500


def test_clips_are_saved_when_the_counter_is_out_of_reach(app, client,
                                                          monkeypatch):
    def unreachable():
        raise ConnectionError("no redis")
    monkeypatch.setattr(run_locks, "_client", unreachable)
    monkeypatch.setattr(external, "CLIPS_PER_DAY", 1)
    assert _clip(client, 0).status_code == 201
    assert _clip(client, 1).status_code == 201


def test_a_token_gets_the_fields_the_extension_reads(app, client, counters):
    client.post("/api/external/clip", json={
        "url": URL, "title": "A stored title", "content": "the saved text"})
    headers = _token_headers(client)

    known = _no_session(app, "post", "/api/external/clip", headers=headers,
                        json={"url": URL, "content": "x"})
    assert known.status_code == 200
    assert known.get_json() == {
        "created": False, "updated": False, "source": "web_clip",
        "truncated": False}

    new = _no_session(app, "post", "/api/external/clip", headers=headers,
                      json={"url": URL + "/other", "title": "Sent title",
                            "content": "x"})
    assert new.status_code == 201
    assert new.get_json() == {
        "created": True, "source": "web_clip", "truncated": False}

    fuller = _no_session(app, "post", "/api/external/clip", headers=headers,
                         json={"url": URL,
                               "content": "the saved text, and more of it"})
    assert fuller.status_code == 200
    assert fuller.get_json() == {
        "created": False, "updated": True, "source": "web_clip",
        "truncated": False}


def test_a_token_connection_check_counts_clipped_pages_only(app, client,
                                                            counters):
    client.post("/api/external/clip", json={"url": URL, "content": "text"})
    client.post("/api/external/clip", json={
        "url": "https://x.com/alice/status/123", "content": "a post"})
    headers = _token_headers(client)
    res = _no_session(app, "get", "/api/external/clip/status", headers=headers)
    assert res.status_code == 200
    # options.js prints "Connected as {username} · {counts.web_clip}
    # clipped pages so far."
    assert res.get_json() == {
        "ok": True, "username": "tester", "token_name": "test clipper",
        "counts": {"web_clip": 1}}


def test_a_session_gets_the_answers_it_got_before(app, client, counters):
    made = client.post("/api/external/clip", json={
        "url": URL, "title": "A stored title", "content": "the saved text"})
    assert made.status_code == 201
    body = made.get_json()
    assert body["title"] == "A stored title" and body["id"]
    again = client.post("/api/external/clip",
                        json={"url": URL, "content": "x"}).get_json()
    assert again["id"] == body["id"] and again["title"] == "A stored title"
    client.post("/api/external/clip", json={
        "url": "https://x.com/alice/status/123", "content": "a post"})
    status = client.get("/api/external/clip/status").get_json()
    assert status["username"] == "tester"
    assert status["counts"] == {"web_clip": 1, "twitter_bookmark": 1}
    assert status["token_name"] is None
