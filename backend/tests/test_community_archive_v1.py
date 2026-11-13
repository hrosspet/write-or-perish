"""Community Archive API v1 and the stale bulk export (#688).

No test here reaches the network: every v1 request goes to FakeV1, which
answers with bodies shaped like the OpenAPI document's schemas
(TweetPage, Account, Error; https://analytics.community-archive.org/v1/
openapi.json, read 2026-11-13) and fails the test on any other host. The
REST path is faked at ``community_archive._get`` / ``requests.get``.
"""
import io
import json
import logging
import pathlib
import types
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from backend.utils import ca_feed
from backend.utils import community_archive as ca

KEY = "cak_testkey_not-a-real-secret"
V1_HOST = "analytics.community-archive.org"


# ── recorded-shape fakes ─────────────────────────────────────────────────

def v1_tweet(tweet_id, username, text, created_at, account_id=None, **extra):
    """A Tweet as the OpenAPI schema lists it (every required field)."""
    row = {
        "tweet_id": str(tweet_id),
        "account_id": str(account_id or 1000 + len(username)),
        "username": username, "account_display_name": username.title(),
        "avatar_media_url": None, "created_at": created_at,
        "full_text": text, "favorite_count": 2, "retweet_count": 1,
        "reply_to_tweet_id": None, "reply_to_user_id": None,
        "reply_to_username": None, "quoted_tweet_id": None,
        "retweeted_tweet_id": None, "conversation_id": str(tweet_id),
        "media": [], "urls": [], "mentioned_account_ids": [],
    }
    row.update(extra)
    return row


def v1_account(account_id, username, has_archive=True, num_tweets=10):
    """An Account as the OpenAPI schema lists it."""
    return {
        "account_id": str(account_id), "username": username,
        "account_display_name": username.title(),
        "account_created_at": "2010-01-01T00:00:00.000Z", "bio": None,
        "website": None, "location": None, "avatar_media_url": None,
        "header_media_url": None, "num_tweets": num_tweets,
        "num_followers": 1, "num_following": 1, "num_likes": 1,
        "is_member": has_archive, "has_archive": has_archive,
        "is_opted_in": False, "latest_observed_at": None,
    }


def page(data, next_cursor=None):
    return {"data": data, "next_cursor": next_cursor, "request_id": "req"}


class V1Error:
    def __init__(self, status, code, headers=None):
        self.status, self.code, self.headers = status, code, headers or {}


class _Resp(io.BytesIO):
    def __init__(self, body, headers):
        super().__init__(json.dumps(body).encode())
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeV1:
    """urllib.request.urlopen for the v1 host. ``routes[path]`` is a list
    of answers served in order (the last one repeats), each a body dict, a
    V1Error, or a callable(params) returning one of those."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        self.sleeps = []
        self.headers = {}

    def __call__(self, req, timeout=None):
        url = urllib.parse.urlparse(req.full_url)
        if url.netloc != V1_HOST:
            raise AssertionError(f"unexpected network call to {url.netloc}")
        params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        path = url.path[len("/v1"):]
        self.requests.append({"path": path, "params": params,
                              "headers": dict(req.header_items()),
                              "timeout": timeout})
        answers = self.routes[path]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if callable(answer):
            answer = answer(params)
        if isinstance(answer, V1Error):
            body = {"error": {"code": answer.code, "message": "no"},
                    "request_id": "req"}
            raise urllib.error.HTTPError(
                req.full_url, answer.status, answer.code, answer.headers,
                io.BytesIO(json.dumps(body).encode()))
        return _Resp(answer, dict(self.headers))


@pytest.fixture
def v1(monkeypatch):
    """API v1 with a key (no app context: the key comes from the env)."""
    fake = FakeV1()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(ca, "_sleep", fake.sleeps.append)
    monkeypatch.setattr(ca, "_v1_not_before", 0.0)
    monkeypatch.setenv("COMMUNITY_ARCHIVE_API_KEY", KEY)
    return fake


@pytest.fixture
def keyless(monkeypatch):
    """No key: any v1 request fails the test."""
    monkeypatch.delenv("COMMUNITY_ARCHIVE_API_KEY", raising=False)

    def no_network(req, timeout=None):
        raise AssertionError(f"unexpected network call: {req.full_url}")
    monkeypatch.setattr(urllib.request, "urlopen", no_network)


@pytest.fixture(autouse=True)
def _fresh_alert_claims():
    for kind in (ca_feed.ALERT_EXPORT, ca_feed.ALERT_API_V1_DAY):
        ca_feed._archive_problem_cleared(kind)
    ca_feed._newest_memo.clear()
    yield
    for kind in (ca_feed.ALERT_EXPORT, ca_feed.ALERT_API_V1_DAY):
        ca_feed._archive_problem_cleared(kind)


def make_export(d, newest, export_id="2026-11-03T07-01-38Z"):
    """A bulk export (the archive's column names) whose newest tweet is at
    *newest* (naive UTC)."""
    duckdb = pytest.importorskip("duckdb")
    d = pathlib.Path(d)
    d.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    stamp = newest.strftime("%Y-%m-%d %H:%M:%S")
    older = (newest - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute(
        "copy (select * from (values "
        f"('e1', 'a1', timestamptz '{stamp}+00', 'export tweet by alice'), "
        f"('e2', 'a2', timestamptz '{older}+00', 'export tweet by bob')"
        ") v(tweet_id, account_id, created_at, full_text)) "
        f"to '{d / 'tweets.parquet'}' (format parquet)")
    con.execute(
        "copy (select * from (values ('a1', 'alice', 'Alice'), "
        "('a2', 'bob', 'Bob')) v(account_id, username, display_name)) "
        f"to '{d / 'profiles.parquet'}' (format parquet)")
    (d / "export_id").write_text(export_id)
    return d


# ── the key ──────────────────────────────────────────────────────────────

def test_key_comes_from_config_in_the_app_and_from_the_env_outside(monkeypatch):
    from flask import Flask
    monkeypatch.setenv("COMMUNITY_ARCHIVE_API_KEY", " env-key ")
    assert ca.api_key() == "env-key" and ca.v1_enabled()
    app = Flask(__name__)
    app.config["COMMUNITY_ARCHIVE_API_KEY"] = None
    with app.app_context():
        assert ca.api_key() is None and not ca.v1_enabled()
        app.config["COMMUNITY_ARCHIVE_API_KEY"] = "cfg-key"
        assert ca.api_key() == "cfg-key"
    monkeypatch.setenv("COMMUNITY_ARCHIVE_API_KEY", "")
    assert ca.api_key() is None


# ── account lookup ───────────────────────────────────────────────────────

def test_fetch_account_v1_matches_exactly_and_refuses_several(v1):
    v1.routes["/accounts"] = [lambda p: page([
        v1_account(2, "jane1doe"), v1_account(1, "Jane_Doe", has_archive=False,
                                              num_tweets=13000)])]
    account = ca.fetch_account("@jane_doe")
    req = v1.requests[-1]
    assert req["params"] == {"usernames": "jane_doe"}
    assert req["headers"]["Authorization"] == f"Bearer {KEY}"
    # The same shape the REST row had; created_via from has_archive.
    assert account == {"account_id": "1", "username": "Jane_Doe",
                       "account_display_name": "Jane_Doe".title(),
                       "num_tweets": 13000, "created_via": "twitter_import",
                       "has_archive": False}
    v1.routes["/accounts"] = [page([v1_account(5, "bob")])]
    assert ca.fetch_account("bob")["created_via"] is None  # an uploader
    v1.routes["/accounts"] = [page([v1_account(1, "janedoe"),
                                    v1_account(2, "JaneDoe")])]
    with pytest.raises(ca.CommunityArchiveError,
                       match="2 Community Archive accounts"):
        ca.fetch_account("janedoe")
    v1.routes["/accounts"] = [page([])]
    assert ca.fetch_account("nobody") is None
    n = len(v1.requests)
    assert ca.fetch_account("ja*ne") is None and len(v1.requests) == n


def test_fetch_account_keyless_still_uses_rest(keyless, monkeypatch):
    """Control: without a key the REST call is made exactly as before."""
    calls = []
    monkeypatch.setattr(ca, "_get", lambda table, params, **kw: calls.append(
        (table, params)) or [{"account_id": "1", "username": "bob"}])
    assert ca.fetch_account("bob")["account_id"] == "1"
    assert calls[0][0] == "all_account"


# ── tweets, paging, limits ───────────────────────────────────────────────

def test_iter_tweets_v1_follows_the_cursor_past_short_pages(v1):
    v1.routes["/accounts/42/tweets"] = [
        page([v1_tweet(1, "al", "a", "2026-01-01T00:00:00.000Z"),
              v1_tweet(2, "al", "b", "2026-01-02T00:00:00.000Z")], "c1"),
        # Withdrawn tweets are left out: a short page is not the last one.
        page([v1_tweet(3, "al", "c", "2026-01-03T00:00:00.000Z")], "c2"),
        page([], None),
    ]
    progress = []
    rows = list(ca.iter_tweets("42", on_page=progress.append))
    assert [r["tweet_id"] for r in rows] == ["1", "2", "3"]
    assert progress == [2, 3, 3]
    first, second, third = (r["params"] for r in v1.requests)
    assert first == {"order": "asc", "limit": "200"}  # 1000 capped to 200
    assert second["cursor"] == "c1" and third["cursor"] == "c2"
    # The rows feed the importer as the REST rows did.
    entry = ca.to_export_entry(rows[0])["tweet"]
    assert entry["id_str"] == "1" and entry["full_text"] == "a"
    assert entry["created_at"] == "Thu Jan 01 00:00:00 +0000 2026"


def test_iter_tweets_v1_unknown_account_yields_nothing(v1):
    v1.routes["/accounts/42/tweets"] = [V1Error(404, "not_found")]
    progress = []
    assert list(ca.iter_tweets("42", on_page=progress.append)) == []
    assert progress == [0]
    with pytest.raises(ca.CommunityArchiveError):
        list(ca.iter_tweets("someone_s_handle"))  # never a username path


def test_v1_retries_429_and_503_after_retry_after(v1):
    v1.routes["/accounts"] = [
        V1Error(429, "rate_limited", {"Retry-After": "7"}),
        V1Error(503, "busy", {"Retry-After": "600"}),  # capped at 60 s
        V1Error(429, "too_many_concurrent_requests"),  # no header: 5 s
        page([v1_account(1, "bob")])]
    assert ca.fetch_account("bob")["account_id"] == "1"
    assert v1.sleeps == [7.0, 60.0, 5.0]
    v1.routes["/accounts"] = [V1Error(429, "rate_limited", {"Retry-After": "1"})]
    with pytest.raises(urllib.error.HTTPError) as err:
        ca.fetch_account("bob")
    assert err.value.code == 429
    assert len(v1.requests) == 4 + ca.V1_MAX_ATTEMPTS


def test_v1_waits_for_the_reset_when_the_minute_is_used_up(v1):
    v1.headers = {"RateLimit-Remaining": "0", "RateLimit-Reset": "12"}
    v1.routes["/accounts"] = [page([v1_account(1, "bob")])]
    ca.fetch_account("bob")
    assert v1.sleeps == []
    v1.headers = {"RateLimit-Remaining": "59"}
    ca.fetch_account("bob")
    assert len(v1.sleeps) == 1 and 11 < v1.sleeps[0] <= 12


def test_v1_time_budget_is_not_slept_through(v1):
    """The whitelist gives the lookup a budget: a Retry-After beyond it
    fails the lookup instead of holding the request."""
    v1.routes["/accounts"] = [V1Error(429, "rate_limited", {"Retry-After": "30"})]
    with pytest.raises(urllib.error.HTTPError):
        ca.fetch_account("bob", timeout=5)
    assert v1.sleeps == [] and len(v1.requests) == 1
    assert v1.requests[0]["timeout"] <= 5


def test_the_key_is_never_logged_nor_in_an_error(v1, caplog):
    v1.routes["/accounts"] = [V1Error(401, "unauthorized")]
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(urllib.error.HTTPError) as err:
            ca.fetch_account("bob")
    assert KEY not in str(err.value)
    assert KEY not in caplog.text and "unauthorized" in caplog.text


def test_count_is_unknown_on_v1_and_the_check_scan_is_capped(v1, monkeypatch):
    assert ca.count_archived("42") is None and v1.requests == []
    v1.routes["/accounts"] = [page([v1_account(42, "big")])]
    full = [v1_tweet(i, "big", "t", "2026-01-01T00:00:00.000Z", account_id=42)
            for i in range(200)]
    v1.routes["/accounts/42/tweets"] = [lambda p: page(full, "more")]
    monkeypatch.setattr(ca, "V1_CHECK_SCAN_LIMIT", 400)
    out = ca.coverage_summary("big")
    assert out["detail_source"] == "too_many_to_scan" and out["archived"] is None
    assert len([r for r in v1.requests if r["path"] != "/accounts"]) == 3
    v1.routes["/accounts/42/tweets"] = [page([
        v1_tweet(1, "big", "RT @x: y", "2026-01-01T00:00:00.000Z", account_id=42),
        v1_tweet(2, "big", "hello there", "2026-01-01T00:00:00.000Z",
                 account_id=42, reply_to_tweet_id="9"),
        v1_tweet(3, "big", "mine", "2026-01-01T00:00:00.000Z", account_id=42)])]
    out = ca.coverage_summary("big")
    assert (out["archived"], out["retweets"], out["replies"], out["originals"],
            out["detail_source"]) == (3, 1, 1, 1, "api-v1")


# ── the REST retirement ──────────────────────────────────────────────────

def test_rest_after_the_retirement_logs_one_error(keyless, monkeypatch, caplog):
    monkeypatch.setattr(ca, "_rest_retired_logged", False)
    with caplog.at_level(logging.ERROR, logger=ca.log.name):
        ca._warn_if_rest_retired(now=datetime(2026, 11, 18, tzinfo=timezone.utc))
        assert caplog.records == []
        ca._warn_if_rest_retired(now=datetime(2026, 11, 19, tzinfo=timezone.utc))
        ca._warn_if_rest_retired(now=datetime(2026, 11, 20, tzinfo=timezone.utc))
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "COMMUNITY_ARCHIVE_API_KEY" in errors[0].getMessage()


# ── the "fetch donated archive" import ───────────────────────────────────

def test_external_content_uses_v1_with_a_key(v1):
    from backend.utils import external_content as content
    v1.routes["/accounts"] = [page([v1_account(7, "donor")])]
    assert content.ca_lookup_account("donor") == "7"
    v1.routes["/accounts/7/tweets"] = [
        page([v1_tweet(i, "donor", f"t{i}", "2026-01-01T00:00:00.000Z",
                       account_id=7) for i in (5, 4)], "c1"),
        page([v1_tweet(i, "donor", f"t{i}", "2026-01-01T00:00:00.000Z",
                       account_id=7) for i in (3, 2)], "c2")]
    items = list(content.ca_fetch_tweets("7", "donor", max_items=3))
    assert [i["external_id"] for i in items] == ["5", "4", "3"]
    tweet_calls = [r["params"] for r in v1.requests
                   if r["path"] == "/accounts/7/tweets"]
    assert tweet_calls[0] == {"order": "desc", "limit": "3"}
    assert tweet_calls[1] == {"order": "desc", "limit": "1", "cursor": "c1"}


# ── Glean's day from v1 ──────────────────────────────────────────────────

def test_day_window_ends_at_the_last_export_hour():
    since, until = ca.v1_day_window(datetime(2026, 11, 13, 6, 59, tzinfo=timezone.utc))
    assert until == datetime(2026, 11, 12, 7, tzinfo=timezone.utc)
    assert since == until - timedelta(days=1)
    _, until = ca.v1_day_window(datetime(2026, 11, 13, 7, 0, tzinfo=timezone.utc))
    assert ca.v1_window_id(until) == "api-v1-2026-11-13T07-00Z"


def _day_route(v1):
    tweets = [
        v1_tweet(11, "alice", "alice in the morning", "2026-11-12T08:00:00.000Z", 1),
        v1_tweet(12, "bob", "RT @alice: alice in the morning",
                 "2026-11-12T09:00:00.000Z", 2, retweeted_tweet_id="11"),
        v1_tweet(13, "carol", "carol late", "2026-11-13T06:30:00.000Z", 3),
    ]
    v1.routes["/tweets"] = [page(tweets[:2], "c1"), page(tweets[1:], None)]


def test_a_day_from_v1_renders_like_an_export(v1, tmp_path):
    _day_route(v1)
    now = datetime(2026, 11, 13, 9, tzinfo=timezone.utc)
    assert ca.ensure_v1_day(tmp_path, now=now) == ("api-v1-2026-11-13T07-00Z", True)
    first = v1.requests[0]["params"]
    assert first == {"since": "2026-11-12T07:00:00Z", "until": "2026-11-13T07:00:00Z",
                     "order": "asc", "limit": "200"}
    assert v1.requests[1]["params"]["cursor"] == "c1"
    # Cached per window: no request the second time.
    assert ca.ensure_v1_day(tmp_path, now=now) == ("api-v1-2026-11-13T07-00Z", False)
    assert len(v1.requests) == 2
    day = ca.latest_v1_day(tmp_path)
    text, stats, refs = ca.render_recent_tweets(
        day, days=1, exclude_usernames=["Carol"])
    assert stats["export_id"] == "api-v1-2026-11-13T07-00Z"
    assert stats["window_end"] == "2026-11-13 06:30"
    assert "[#1] alice in the morning" in text
    assert "RT @alice" not in text  # retweets dropped as on the export
    assert "carol late" not in text  # the reader's own handle
    assert refs[1]["display_name"] == "Alice" and refs[1]["tweet_id"] == "11"
    assert ca.fetch_tweets_by_id(day, ["13"])["13"]["username"] == "carol"


def test_a_day_halves_its_window_on_422_and_keeps_three_days(v1, tmp_path):
    def window(params):
        since = datetime.fromisoformat(params["since"].replace("Z", "+00:00"))
        until = datetime.fromisoformat(params["until"].replace("Z", "+00:00"))
        if until - since > timedelta(hours=12):
            return V1Error(422, "query_budget_exceeded")
        return page([v1_tweet(int(since.timestamp()), "dave", "half",
                              params["since"].replace("Z", ".000Z"), 4)])
    v1.routes["/tweets"] = [window]
    for day in (10, 11, 12, 13):
        ca.ensure_v1_day(tmp_path, now=datetime(2026, 11, day, 8, tzinfo=timezone.utc))
    kept = sorted(p.name for p in (tmp_path / "api-v1").iterdir() if p.is_dir())
    assert kept == ["api-v1-2026-11-11T07-00Z", "api-v1-2026-11-12T07-00Z",
                    "api-v1-2026-11-13T07-00Z"]
    _, stats, _ = ca.render_recent_tweets(ca.latest_v1_day(tmp_path), days=1)
    assert stats["tweets"] == 2  # one per half


def test_an_empty_day_is_not_cached(v1, tmp_path):
    v1.routes["/tweets"] = [page([])]
    with pytest.raises(ca.CommunityArchiveError):
        ca.ensure_v1_day(tmp_path, now=datetime(2026, 11, 13, 8, tzinfo=timezone.utc))
    assert ca.latest_v1_day(tmp_path) is None


def test_glean_reads_the_v1_day_only_when_the_export_is_stale(v1, tmp_path):
    now = datetime(2026, 11, 13, 9)
    make_export(tmp_path, newest=datetime(2026, 11, 3, 6, 50))
    # Stale export, no day cached yet: the export (and its notice).
    assert ca_feed.glean_source_dir(tmp_path, 1, now=now) == tmp_path
    _day_route(v1)
    assert ca_feed.refresh_v1_day_for_read(tmp_path, now=now) == "api-v1-2026-11-13T07-00Z"
    day = ca.latest_v1_day(tmp_path)
    assert ca_feed.glean_source_dir(tmp_path, 1, now=now) == day
    # Only the default one-day read; a longer admin window stays on the export.
    assert ca_feed.glean_source_dir(tmp_path, 2, now=now) == tmp_path
    # A fresh export wins, and the beat sends no request for a day.
    fresh = make_export(tmp_path / "fresh", newest=datetime(2026, 11, 13, 6, 50))
    n = len(v1.requests)
    assert ca_feed.glean_source_dir(fresh, 1, now=now) == fresh
    assert ca_feed.refresh_v1_day_for_read(fresh, now=now) is None
    assert len(v1.requests) == n


def test_glean_without_a_key_never_leaves_the_export(keyless, tmp_path):
    """Control: no key, today's behaviour, however stale the export."""
    make_export(tmp_path, newest=datetime(2026, 11, 3, 6, 50))
    now = datetime(2026, 11, 13, 9)
    assert ca_feed.glean_source_dir(tmp_path, 1, now=now) == tmp_path
    assert ca_feed.refresh_v1_day_for_read(tmp_path, now=now) is None


def test_a_failed_day_fetch_reports_once(v1, tmp_path, caplog):
    make_export(tmp_path, newest=datetime(2026, 11, 3, 6, 50))
    v1.routes["/tweets"] = [V1Error(401, "unauthorized")]
    now = datetime(2026, 11, 13, 9)
    with caplog.at_level(logging.WARNING):
        assert ca_feed.refresh_v1_day_for_read(tmp_path, now=now) is None
        assert ca_feed.refresh_v1_day_for_read(tmp_path, now=now) is None
    levels = [r.levelno for r in caplog.records
              if "fetching the day for Glean failed" in r.getMessage()]
    assert levels == [logging.ERROR, logging.WARNING]
    assert "HTTPError 401" in caplog.text


def test_picks_of_a_v1_day_resolve_from_that_day(v1, tmp_path):
    _day_route(v1)
    ca.ensure_v1_day(tmp_path, now=datetime(2026, 11, 13, 9, tzinfo=timezone.utc))
    row = types.SimpleNamespace(
        export_id="api-v1-2026-11-13T07-00Z", dropped_picks=None,
        tweet_id_for=lambda n: {1: "11", 2: "13"}.get(n))
    reply = json.dumps({"verdict": "v", "picks": [
        {"n": 1, "qt": "a", "relevance": 50, "recommend": True},
        {"n": 2, "qt": "b", "relevance": 50, "recommend": True}]})
    refs = ca_feed.refs_from_render(row, reply, tmp_path)
    assert {n: r["tweet_id"] for n, r in refs.items()} == {1: "11", 2: "13"}
    assert row.dropped_picks == 0


# ── the stale export shows ───────────────────────────────────────────────

def _cached_export(tmp_path, newest):
    make_export(tmp_path, newest=newest)
    return tmp_path


def test_a_failed_refresh_reports_at_error_once_per_streak(
        keyless, tmp_path, monkeypatch, caplog):
    _cached_export(tmp_path, datetime(2026, 11, 3, 6, 50))
    monkeypatch.setattr(ca_feed, "legacy_batches_live", lambda: False)

    def boom(_d):
        raise urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
    monkeypatch.setattr(ca, "refresh_snapshot", boom)
    with caplog.at_level(logging.WARNING):
        assert ca_feed.refresh_snapshot_for_read(tmp_path) is None
        assert ca_feed.refresh_snapshot_for_read(tmp_path) is None
    records = [r for r in caplog.records if "refresh failed" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.ERROR, logging.WARNING]
    assert "HTTPError 400" in records[0].getMessage()
    assert "2026-11-03 06:50" in records[0].getMessage()
    # A healthy run ends the streak; the next failure reports at once.
    make_export(tmp_path / "x", newest=datetime.utcnow())
    monkeypatch.setattr(ca, "refresh_snapshot", lambda d: ("new", False))
    assert ca_feed.refresh_snapshot_for_read(tmp_path / "x") == "new"
    caplog.clear()
    monkeypatch.setattr(ca, "refresh_snapshot", boom)
    with caplog.at_level(logging.WARNING):
        ca_feed.refresh_snapshot_for_read(tmp_path)
    assert [r.levelno for r in caplog.records
            if "refresh failed" in r.getMessage()] == [logging.ERROR]


def test_an_export_left_stale_without_errors_reports_too(keyless, tmp_path,
                                                         monkeypatch, caplog):
    _cached_export(tmp_path, datetime(2026, 11, 3, 6, 50))
    monkeypatch.setattr(ca_feed, "legacy_batches_live", lambda: False)
    monkeypatch.setattr(ca, "refresh_snapshot",
                        lambda d: ("2026-11-03T07-01-38Z", False))
    with caplog.at_level(logging.WARNING):  # 36 h 10 min, 37 h 10 min
        ca_feed.refresh_snapshot_for_read(tmp_path, now=datetime(2026, 11, 4, 19, 0))
        ca_feed.refresh_snapshot_for_read(tmp_path, now=datetime(2026, 11, 4, 20, 0))
    assert [r.levelno for r in caplog.records
            if "is stale" in r.getMessage()] == [logging.ERROR, logging.WARNING]
    caplog.clear()
    with caplog.at_level(logging.WARNING):  # 35 h 10 min: not stale yet
        ca_feed.refresh_snapshot_for_read(tmp_path, now=datetime(2026, 11, 4, 18, 0))
    assert caplog.records == []


def test_the_window_line_is_told_when_the_read_was_stale():
    def row(created, window_end):
        return types.SimpleNamespace(
            export_id="e", days=1, scope="all", window_start=window_end - timedelta(days=1),
            window_end=window_end, tweet_count=3, account_count=2,
            excluded_count=0, created_at=created)
    end = datetime(2026, 11, 3, 6, 50)
    assert ca_feed.read_window_fields(row(end + timedelta(hours=36), end))["export_stale"] is False
    assert ca_feed.read_window_fields(
        row(end + timedelta(hours=36, minutes=1), end))["export_stale"] is True
    assert ca_feed.read_window_fields(row(None, end))["export_stale"] is False
