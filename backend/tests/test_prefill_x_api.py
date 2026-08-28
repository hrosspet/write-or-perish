"""Admin "Pre-fill from X": stdlib X API client (pagination, export
shaping, cap), the task impl (shares the CA pre-fill tail), the admin
check/start routes, and the spam flag/toggle."""
from unittest.mock import MagicMock

import pytest

from backend.tests.test_twitter_import import app, _make_user, _db  # noqa: F401
from backend.tests.test_admin_access import app as admin_app, _login  # noqa: F401
from backend.models import User, Node, APICostLog
from backend.utils import x_api


def _v2_tweet(i, text, reply_to_user=None, note=None, rt=False):
    t = {"id": str(i), "text": ("RT @x: " if rt else "") + text,
         "created_at": f"2026-08-{10 + (i % 10):02d}T10:00:00.000Z",
         "public_metrics": {"like_count": i, "retweet_count": 0}, "lang": "en"}
    if reply_to_user:
        t["in_reply_to_user_id"] = reply_to_user
        t["referenced_tweets"] = [{"type": "replied_to", "id": "900"}]
    if note:
        t["note_tweet"] = {"text": note}
    return t


CREDS = ("k", "s")


def test_fetchable_and_cost():
    assert x_api.fetchable(17104) == 3200
    assert x_api.fetchable(2460) == 2460
    assert x_api.fetchable(17104, 500) == 500
    assert x_api.fetchable(300, 500) == 300
    assert x_api.fetchable(300, 0) == 0
    assert x_api.estimate_cost(3200) == 16.01
    assert x_api.estimate_cost(0) == 0.01


def test_iter_user_tweets_pages_caps_and_shapes(monkeypatch):
    calls = []
    pages = [
        {"data": [_v2_tweet(3, "third", reply_to_user="77", note="the long version"), _v2_tweet(2, "second")],
         "includes": {"users": [{"id": "77", "username": "bob"}]},
         "meta": {"next_token": "T2"}},
        {"data": [_v2_tweet(1, "first")], "meta": {}},
    ]

    def fake_get(path, params, creds):
        calls.append((path, dict(params)))
        return pages[len(calls) - 1]

    monkeypatch.setattr(x_api, "_get", fake_get)
    rows = list(x_api.iter_user_tweets("U1", CREDS, max_tweets=3))
    assert [r["tweet"]["id_str"] for r in rows] == ["3", "2", "1"]
    assert rows[0]["tweet"]["full_text"] == "the long version"  # note_tweet wins
    assert rows[0]["tweet"]["in_reply_to_screen_name"] == "bob"
    assert rows[0]["tweet"]["in_reply_to_status_id_str"] == "900"
    assert rows[0]["tweet"]["created_at"] == "Thu Aug 13 10:00:00 +0000 2026"
    assert rows[0]["tweet"]["favorite_count"] == "3"
    assert calls[0][1]["exclude"] == "retweets" and "pagination_token" not in calls[0][1]
    assert calls[1][1]["pagination_token"] == "T2"
    # cap: stop after max_tweets even if the API has more
    calls.clear()
    pages[:] = [{"data": [_v2_tweet(9, "a"), _v2_tweet(8, "b")], "meta": {"next_token": "T"}}]
    rows = list(x_api.iter_user_tweets("U1", CREDS, max_tweets=1))
    assert len(rows) == 1 and len(calls) == 1
    assert calls[0][1]["max_results"] == 5  # API floor


def test_lookup_user_and_errors(monkeypatch):
    monkeypatch.setattr(x_api, "_get", lambda p, q, c: {"data": {
        "id": "1", "username": "kat_szpiech", "name": "Kat",
        "public_metrics": {"tweet_count": 17104}, "protected": False}})
    u = x_api.lookup_user("@kat_szpiech", CREDS)
    assert u["tweet_count"] == 17104 and u["protected"] is False
    monkeypatch.setattr(x_api, "_get", lambda p, q, c: {"errors": [{"title": "Not Found"}]})
    assert x_api.lookup_user("nobody", CREDS) is None
    assert x_api.lookup_user("", CREDS) is None
    with pytest.raises(x_api.XApiError):
        x_api._bearer(None, None)


def _fake_x(monkeypatch, tweet_count=5, protected=False, tweets=None):
    monkeypatch.setattr(x_api, "lookup_user", lambda h, c: {
        "id": "U1", "username": "Alice", "name": "A",
        "tweet_count": tweet_count, "protected": protected})
    seen = {}

    def fake_iter(user_id, creds, max_tweets=3200, on_page=None, on_raw=None):
        seen["max_tweets"] = max_tweets
        users = {"77": "bob"}
        raw = (tweets or [])[:max_tweets]
        for t in raw:
            if on_raw:
                on_raw(t, users)
        out = [x_api.to_export_entry(t, users) for t in raw]
        if on_page:
            on_page(len(out))
        return iter(out)

    monkeypatch.setattr(x_api, "iter_user_tweets", fake_iter)
    return seen


def test_prefill_x_impl_imports_and_pins_batch(app, monkeypatch):  # noqa: F811
    from backend.tasks import imports as imports_mod
    import backend.tasks.exports as ex
    u = _make_user("alice")
    _db.session.commit()
    seen = _fake_x(monkeypatch, tweet_count=4, tweets=[
        _v2_tweet(3, "third"), _v2_tweet(2, "reply", reply_to_user="77"),
        _v2_tweet(1, "first"), _v2_tweet(0, "a retweet", rt=True)])
    sync = MagicMock(return_value="sync-task")
    monkeypatch.setattr(ex, "maybe_trigger_profile_update", sync)
    states = []

    result = imports_mod.prefill_x_api_impl(
        u.id, "alice", {"max_tweets": 500, "include_replies": False},
        update_state=lambda **kw: states.append(kw["meta"]))

    assert seen["max_tweets"] == 4  # min(cap, tweet_count, requested)
    assert result["source"] == "x-api" and result["handle"] == "Alice"
    assert (result["fetched"], result["retweets_skipped"], result["created"]) == (4, 1, 2)
    assert result["est_cost_usd"] == x_api.estimate_cost(4)
    nodes = Node.query.filter_by(human_owner_id=u.id).order_by(Node.created_at).all()
    assert [n.get_content() for n in nodes] == ["first", "third"]  # reply excluded, sorted
    assert all(n.origin == "twitter" and n.privacy_level == "private" for n in nodes)
    fresh = User.query.get(u.id)
    assert fresh.profile_force_batch is True and fresh.prefilled_handle == "Alice"
    assert sync.call_count == 0
    assert states[0]["stage"] == "fetching" and states[-1]["stage"] == "importing"
    assert result["profile_batch_queued"] is False
    # Every paid-for post is kept as raw JSON, independent of the account.
    import json
    lines = [json.loads(ln) for ln in open(result["dump_path"], encoding="utf-8")]
    assert lines[0]["_meta"] == "loore x-api pre-fill" and lines[0]["account"]["username"] == "Alice"
    assert [ln["id"] for ln in lines[1:]] == ["3", "2", "1", "0"]  # retweet kept too
    assert lines[2]["_reply_to_username"] == "bob"
    assert "/x-api/Alice-" in result["dump_path"]
    # Cost ledger: 4 posts * $0.005 + 1 user read * $0.010
    log = APICostLog.query.filter_by(user_id=u.id, request_type="x_prefill").one()
    assert log.cost_microdollars == 4 * 5000 + 10000 and log.request_ref == "@Alice"
    assert log.model_id == "x-api/timeline"


def test_prefill_x_impl_refuses_protected_and_unknown(app, monkeypatch):  # noqa: F811
    from backend.tasks import imports as imports_mod
    u = _make_user("bob")
    _db.session.commit()
    _fake_x(monkeypatch, protected=True)
    with pytest.raises(x_api.XApiError, match="protected"):
        imports_mod.prefill_x_api_impl(u.id, "bob", {})
    monkeypatch.setattr(x_api, "lookup_user", lambda h, c: None)
    with pytest.raises(x_api.XApiError, match="no such"):
        imports_mod.prefill_x_api_impl(u.id, "bob", {})


def _admin(admin_app):  # noqa: F811
    admin = User(username="root", approved=True, is_admin=True, plan="alpha")
    _db.session.add(admin)
    _db.session.flush()
    client = admin_app.test_client()
    _login(client, admin.id)
    return client


def test_x_check_route(admin_app, monkeypatch):  # noqa: F811
    client = _admin(admin_app)
    target = _make_user("kat_szpiech")
    _db.session.commit()
    monkeypatch.setattr(x_api, "lookup_user", lambda h, c: {
        "id": "1", "username": "kat_szpiech", "name": "Kat", "tweet_count": 17104, "protected": False})
    r = client.get(f"/api/admin/prefill/x/check?handle=@kat_szpiech&user_id={target.id}")
    assert r.status_code == 200, r.json
    assert r.json["fetchable"] == 3200 and r.json["est_cost_usd"] == 16.01
    assert r.json["already_imported"] == 0 and r.json["timeline_cap"] == 3200
    log = APICostLog.query.filter_by(user_id=target.id, request_type="x_prefill_check").one()
    assert log.cost_microdollars == 10000 and log.model_id == "x-api/user-lookup"
    assert client.get("/api/admin/prefill/x/check?handle=kat_szpiech&max_tweets=500").json["fetchable"] == 500
    monkeypatch.setattr(x_api, "lookup_user", lambda h, c: {
        "id": "2", "username": "mikeytong", "name": "M", "tweet_count": 1995, "protected": True})
    r = client.get("/api/admin/prefill/x/check?handle=mikeytong")
    assert r.json["protected"] is True and r.json["fetchable"] == 0
    monkeypatch.setattr(x_api, "lookup_user", lambda h, c: None)
    assert client.get("/api/admin/prefill/x/check?handle=charllie").status_code == 404
    assert client.get("/api/admin/prefill/x/check").status_code == 400
    monkeypatch.setattr(x_api, "lookup_user", lambda h, c: (_ for _ in ()).throw(x_api.XApiError("rate")))
    assert client.get("/api/admin/prefill/x/check?handle=x").status_code == 502


def test_x_start_route_clamps_and_queues(admin_app, monkeypatch):  # noqa: F811
    from backend.tasks import imports as imports_mod
    client = _admin(admin_app)
    target = _make_user("aleifr")
    _db.session.commit()
    delay = MagicMock(return_value=MagicMock(id="task-1"))
    monkeypatch.setattr(imports_mod.prefill_x_api, "delay", delay)
    r = client.post(f"/api/admin/users/{target.id}/prefill-x", json={"max_tweets": 99999})
    assert r.status_code == 202 and r.json == {"task_id": "task-1", "handle": "aleifr", "max_tweets": 3200}
    delay.assert_called_once_with(target.id, "aleifr", {"max_tweets": 3200, "include_replies": True})
    r = client.post(f"/api/admin/users/{target.id}/prefill-x",
                    json={"handle": "@Other", "max_tweets": 500, "include_replies": False})
    assert r.json["max_tweets"] == 500 and r.json["handle"] == "Other"
    assert client.post(f"/api/admin/users/{target.id}/prefill-x", json={"max_tweets": 0}).status_code == 400
    assert client.post(f"/api/admin/users/{target.id}/prefill-x", json={"max_tweets": "x"}).status_code == 400


def test_spam_flag_toggle_and_listing(admin_app):  # noqa: F811
    client = _admin(admin_app)
    target = _make_user("silk_amnesia")
    _db.session.commit()
    assert User.query.get(target.id).spam is False
    r = client.post(f"/api/admin/users/{target.id}/toggle_spam")
    assert r.status_code == 200 and r.json["spam"] is True
    row = next(u for u in client.get("/api/admin/users").json["users"] if u["id"] == target.id)
    assert row["spam"] is True
    assert User.query.get(target.id).approved is True  # marking spam doesn't deactivate
    assert client.post(f"/api/admin/users/{target.id}/toggle_spam").json["spam"] is False
