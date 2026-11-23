"""#712: with an API key, the Community Archive pre-fill reads the latest
daily export first, then only the newer tweets through API v1, newest
first, stopping at the first page on which every tweet is already known.

No test reaches the network: the export is a small parquet written here
(the archive's column names), its manifest is faked, and every API v1
request goes to FakeV1 (test_community_archive_v1). Every text is
synthetic.
"""
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest
from sqlalchemy import update

from backend.tests.test_twitter_import import app, _make_user, _db  # noqa: F401
from backend.tests.test_community_archive_v1 import (  # noqa: F401
    FakeV1, page, v1_account, v1_tweet)
from backend.models import Node, User
from backend.utils import community_archive as ca

EXPORT_ID = "2026-11-03T07-00-00Z"
ACCOUNT = 77
TIMELINE = f"/accounts/{ACCOUNT}/tweets"


def _ts(i):
    """The posting time of tweet *i*: later ids are newer, as on X."""
    return f"2026-11-01 {i % 24:02d}:{i // 24 % 60:02d}:00+00"


def _write_export(d, tweets, export_id=EXPORT_ID):
    """A daily export in *d*: tweets.parquet from (tweet_id, account_id,
    text) and profiles.parquet, with the columns the archive's export has.
    Another account's tweet is always in it, as in the real export."""
    import duckdb
    d.mkdir(parents=True, exist_ok=True)
    rows = list(tweets) + [(999, 88, "a tweet of another account")]
    con = duckdb.connect()
    con.execute(f"""
        copy (select * from (values
            ('{ACCOUNT}', 'vee', 'Vee', 900::UBIGINT),
            ('88', 'other', 'Other', 1::UBIGINT))
            t(account_id, username, display_name, num_tweets))
        to '{d / "profiles.parquet"}' (format parquet)""")
    values = ", ".join(
        f"('{i}', '{a}', timestamp with time zone '{_ts(i)}', '{text}', "
        "0::UBIGINT, 0::UBIGINT, NULL::VARCHAR, NULL::VARCHAR)"
        for i, a, text in rows)
    con.execute(f"""
        copy (select * from (values {values})
            t(tweet_id, account_id, created_at, full_text, favorite_count,
              retweet_count, reply_to_tweet_id, reply_to_account_id))
        to '{d / "tweets.parquet"}' (format parquet)""")
    (d / "export_id").write_text(export_id)


def _tweet(i, text=None):
    return v1_tweet(i, "vee", text or f"thought {i}",
                    f"2026-11-{1 + i % 28:02d}T10:00:00.000Z", ACCOUNT)


def _timeline(*pages):
    """The account's timeline as API v1 serves it newest first: page n
    holds the given tweets; its cursor leads to page n + 1."""
    def answer(params):
        n = int((params.get("cursor") or "p0")[1:])
        return page([_tweet(i) if isinstance(i, int) else i for i in pages[n]],
                    f"p{n + 1}" if n + 1 < len(pages) else None)
    return answer


class _Log:
    """The task logger's lines, formatted (celery's logger may be a mock in
    this test process, so the lines are recorded here)."""

    def __init__(self):
        self.lines = []

    def info(self, msg, *args):
        self.lines.append(msg % args)

    warning = exception = info


@pytest.fixture
def v1(app, monkeypatch, tmp_path):  # noqa: F811
    fake = FakeV1()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(ca, "_sleep", fake.sleeps.append)
    monkeypatch.setattr(ca, "_v1_not_before", 0.0)
    app.config["COMMUNITY_ARCHIVE_API_KEY"] = "cak_test_only"
    app.config["COMMUNITY_ARCHIVE_SNAPSHOT_DIR"] = str(tmp_path / "snap")
    fake.routes["/accounts"] = [page([v1_account(ACCOUNT, "vee",
                                                 num_tweets=900)])]
    # The archive names the export the server has cached as its latest.
    monkeypatch.setattr(ca, "fetch_latest_manifest",
                        lambda: {"export_id": EXPORT_ID, "package_paths": []})
    import backend.tasks.exports as ex
    monkeypatch.setattr(ex, "maybe_trigger_profile_update", MagicMock())
    from backend.tasks import imports
    log = _Log()
    monkeypatch.setattr(imports, "logger", log)
    fake.log = log
    fake.snap = tmp_path / "snap"
    fake.stash = tmp_path / "ca-prefill"
    return fake


@pytest.fixture
def imports_mod(v1):
    """backend.tasks.imports, imported inside the test as the other
    pre-fill tests do: imported at collection time, it builds the Celery
    app before the test app is in place."""
    from backend.tasks import imports
    return imports


def _timeline_requests(fake):
    return [r for r in fake.requests if r["path"] == TIMELINE]


def _contents(user):
    return sorted(n.get_content() for n in
                  Node.query.filter_by(human_owner_id=user.id).all())


def _user():
    u = _make_user("vee")
    _db.session.commit()
    return u


# ── the issue's "How to check" ───────────────────────────────────────────

def test_an_account_fully_in_the_export_costs_one_request(v1, imports_mod):
    """No newer tweets: the first page of the timeline is all known, one
    request. A retweet in the export counts as known though it never
    becomes an entry."""
    _write_export(v1.snap, [(101, ACCOUNT, "first"), (102, ACCOUNT, "second"),
                            (103, ACCOUNT, "third"),
                            (104, ACCOUNT, "RT @x: their words")])
    v1.routes[TIMELINE] = [_timeline([104, 103], [102, 101])]
    u = _user()

    result = imports_mod.prefill_community_archive_impl(u.id, "vee", {})

    assert len(_timeline_requests(v1)) == 1
    assert (result["api_requests"], result["api_stop"]) == (1, ca.STOP_KNOWN_PAGE)
    assert (result["source"], result["export_id"]) == ("export+api-v1", EXPORT_ID)
    assert (result["from_export"], result["from_api"]) == (4, 0)
    assert result["created"] == 3 and result["retweets_skipped"] == 1
    assert _contents(u) == ["first", "second", "third"]
    # The count of requests is in the pre-fill's log line.
    line = [ln for ln in v1.log.lines if "Community Archive pre-fill for user" in ln]
    assert len(line) == 1
    assert "4 tweets from export 2026-11-03T07-00-00Z" in line[0]
    assert "0 newer through API v1 in 1 timeline requests" in line[0]
    assert "stopped at a page of known tweets" in line[0]


def test_newer_tweets_are_read_until_the_first_page_of_known_ones(v1, imports_mod):
    """Page 1 is all newer than the export, page 2 mixes newer and known,
    page 3 is all known: the read stops there; page 4 is never asked for."""
    _write_export(v1.snap, [(i, ACCOUNT, f"exported {i}") for i in (101, 102, 103, 104)])
    v1.routes[TIMELINE] = [_timeline([108, 107], [106, 104], [103, 102], [101])]
    u = _user()

    result = imports_mod.prefill_community_archive_impl(u.id, "vee", {})

    assert [r["params"].get("cursor") for r in _timeline_requests(v1)] == [
        None, "p1", "p2"]
    assert all(r["params"]["order"] == "desc" for r in _timeline_requests(v1))
    assert (result["api_requests"], result["api_stop"]) == (3, ca.STOP_KNOWN_PAGE)
    assert (result["from_export"], result["from_api"], result["api_fetched"]) == (4, 3, 6)
    assert result["created"] == 7
    assert _contents(u) == sorted([f"exported {i}" for i in (101, 102, 103, 104)]
                                  + [f"thought {i}" for i in (106, 107, 108)])
    assert any("3 newer through API v1 in 3 timeline requests" in ln
               for ln in v1.log.lines)
    assert not (v1.stash / f"{u.id}-{ACCOUNT}").exists()  # stash gone


def test_an_account_absent_from_the_export_is_read_whole(v1, imports_mod):
    """The archive got the account after the export was made: the export
    holds none of it, and the timeline is read to its end."""
    _write_export(v1.snap, [])  # only another account's tweet
    v1.routes[TIMELINE] = [_timeline([3, 2], [1])]
    u = _user()

    result = imports_mod.prefill_community_archive_impl(u.id, "vee", {})

    assert len(_timeline_requests(v1)) == 2
    assert (result["api_requests"], result["api_stop"]) == (2, ca.STOP_END)
    assert (result["from_export"], result["from_api"], result["created"]) == (0, 3, 3)
    assert any("read to the end of the timeline" in ln for ln in v1.log.lines)


# ── the issue's cases ────────────────────────────────────────────────────

def test_no_export_to_be_had_reads_the_account_through_the_api(v1, monkeypatch, imports_mod):
    """The archive cannot be asked for its export and none is cached: the
    pre-fill still runs, through the API alone, and says why."""
    monkeypatch.setattr(ca, "fetch_latest_manifest", MagicMock(
        side_effect=urllib.error.URLError("unreachable")))
    v1.routes[TIMELINE] = [_timeline([3, 2], [1])]
    u = _user()

    result = imports_mod.prefill_community_archive_impl(u.id, "vee", {})

    assert result["export_id"] is None and result["created"] == 3
    assert any("the latest export could not be fetched (URLError)" in ln
               and "through API v1 only" in ln for ln in v1.log.lines)
    # Never the exception's text in the log (#540).
    assert not any("unreachable" in ln for ln in v1.log.lines)


def test_an_unreachable_archive_reads_the_cached_export(v1, monkeypatch, imports_mod):
    _write_export(v1.snap, [(101, ACCOUNT, "first")])
    monkeypatch.setattr(ca, "fetch_latest_manifest", MagicMock(
        side_effect=urllib.error.URLError("unreachable")))
    v1.routes[TIMELINE] = [_timeline([101])]
    u = _user()

    result = imports_mod.prefill_community_archive_impl(u.id, "vee", {})

    assert result["export_id"] == EXPORT_ID and result["api_requests"] == 1
    assert _contents(u) == ["first"]


def test_a_rerun_reads_one_page_and_adds_nothing(v1, imports_mod):
    """Run again after a complete pre-fill: the newest page is already in
    the account (as entries; the export never had them), so one request,
    and every tweet is deduplicated by its id."""
    _write_export(v1.snap, [(i, ACCOUNT, f"exported {i}") for i in (101, 102)])
    v1.routes[TIMELINE] = [_timeline([104, 103], [102, 101])]
    u = _user()
    first = imports_mod.prefill_community_archive_impl(u.id, "vee", {})
    assert first["created"] == 4 and first["api_requests"] == 2
    v1.requests.clear()

    again = imports_mod.prefill_community_archive_impl(u.id, "vee", {})

    assert len(_timeline_requests(v1)) == 1
    assert again["created"] == 0 and again["skipped"] == 4
    assert Node.query.filter_by(human_owner_id=u.id).count() == 4


def test_a_deleted_entry_counts_as_known(v1, imports_mod):
    """An entry the person deleted is still the account's (the import
    skips it too): its page does not make the read go on."""
    _write_export(v1.snap, [])
    v1.routes[TIMELINE] = [_timeline([2, 1], [0])]
    u = _user()
    first = imports_mod.prefill_community_archive_impl(u.id, "vee", {})
    assert first["created"] == 3
    from datetime import datetime
    for n in Node.query.filter_by(human_owner_id=u.id).all():
        n.deleted_at = datetime.utcnow()
    _db.session.commit()
    v1.requests.clear()

    again = imports_mod.prefill_community_archive_impl(u.id, "vee", {})

    assert len(_timeline_requests(v1)) == 1
    assert again["created"] == 0


def _seed_entries(imports_mod, user, ids, provenance):
    """Entries of tweets *ids* already in the account, as an earlier import
    with *provenance* made them (prefill_x: the X API pull)."""
    from backend.utils import twitter_archive as ta
    rows = [ta.compact_row(ca.to_export_entry(_tweet(i))["tweet"]) for i in ids]
    imports_mod._import_prefill_rows(
        user.id, "vee", rows, {}, lambda *a: None, False,
        provenance=provenance)


def test_entries_from_the_x_api_prefill_do_not_stop_the_read(v1, imports_mod):
    """Review of #742: the X API pull holds only the newest ~3,200 posts, so
    a page of its entries says nothing about what is older. The read goes
    on to the end; tweets 3 and 4 stay one entry each and keep the X API's
    marks (provenance prefill_x, Chat at most, the X API input mark), also
    when this import asks for Train."""
    from backend.utils.privacy import unlicensed_input
    _write_export(v1.snap, [])  # the export does not hold the account
    u = _user()
    _seed_entries(imports_mod, u, [4, 3], "prefill_x")
    v1.routes[TIMELINE] = [_timeline([6, 5], [4, 3], [2, 1])]

    result = imports_mod.prefill_community_archive_impl(
        u.id, "vee", {"ai_usage": "train"})

    assert len(_timeline_requests(v1)) == 3
    assert result["api_stop"] == ca.STOP_END
    assert _contents(u) == [f"thought {i}" for i in range(1, 7)]
    assert Node.query.filter_by(human_owner_id=u.id).count() == 6
    for i in (3, 4):
        [n] = Node.query.filter_by(human_owner_id=u.id,
                                   source_key=f"twitter:{i}").all()
        assert (n.provenance, n.ai_usage) == ("prefill_x", "chat")
        assert unlicensed_input(n) == "x_api"
    others = Node.query.filter(Node.human_owner_id == u.id,
                               Node.provenance != "prefill_x").all()
    assert {(n.provenance, n.ai_usage) for n in others} == {("prefill_ca", "train")}
    assert len(others) == 4


def test_entries_from_the_persons_own_archive_stop_the_read(v1, imports_mod):
    """The person's own archive upload holds everything older: a page of
    its tweets is known."""
    _write_export(v1.snap, [])
    u = _user()
    _seed_entries(imports_mod, u, [4, 3, 2, 1], "archive_upload")
    v1.routes[TIMELINE] = [_timeline([6, 5], [4, 3], [2, 1])]

    result = imports_mod.prefill_community_archive_impl(u.id, "vee", {})

    assert len(_timeline_requests(v1)) == 2
    assert (result["api_stop"], result["created"]) == (ca.STOP_KNOWN_PAGE, 2)


def test_ids_as_json_numbers_match_the_exports_ids(v1, imports_mod):
    _write_export(v1.snap, [(i, ACCOUNT, f"exported {i}") for i in (101, 102)])
    u = _user()

    def answer(params):
        n = int((params.get("cursor") or "p0")[1:])
        rows = []
        for i in [[103, 102], [101]][n]:
            t = _tweet(i)
            t["tweet_id"] = i  # a JSON number
            rows.append(t)
        return page(rows, "p1" if n == 0 else None)
    v1.routes[TIMELINE] = [answer]

    result = imports_mod.prefill_community_archive_impl(u.id, "vee", {})

    assert (result["api_requests"], result["api_stop"]) == (2, ca.STOP_KNOWN_PAGE)
    assert Node.query.filter_by(human_owner_id=u.id).count() == 3


def test_export_only_for_an_account_the_export_does_not_hold(v1, imports_mod):
    _write_export(v1.snap, [])
    v1.routes[TIMELINE] = [AssertionError("no timeline request")]
    u = _user()

    with pytest.raises(ca.CommunityArchiveError,
                       match="export 2026-11-03T07-00-00Z does not hold this account"):
        imports_mod.prefill_community_archive_impl(
            u.id, "vee", {"force_parquet": True})

    assert _timeline_requests(v1) == []


def test_export_only_sends_no_timeline_request(v1, imports_mod):
    """The admin's "force the export" option is now "export only"."""
    _write_export(v1.snap, [(101, ACCOUNT, "first"), (102, ACCOUNT, "second")])
    v1.routes[TIMELINE] = [AssertionError("no timeline request")]
    u = _user()

    result = imports_mod.prefill_community_archive_impl(
        u.id, "vee", {"force_parquet": True})

    assert _timeline_requests(v1) == []
    assert (result["source"], result["api_requests"], result["created"]) == (
        "export", 0, 2)
    assert any("export only" in ln for ln in v1.log.lines)


def test_tweets_from_the_export_and_the_api_carry_the_archive_marks(v1, imports_mod):
    """Both halves are the Community Archive, not the X API: provenance
    prefill_ca, Chat, no X API mark (#689), private, as before #712."""
    from backend.utils.privacy import unlicensed_input
    _write_export(v1.snap, [(101, ACCOUNT, "from the export")])
    v1.routes[TIMELINE] = [_timeline([102, 101])]
    u = _user()

    imports_mod.prefill_community_archive_impl(u.id, "vee", {})

    nodes = Node.query.filter_by(human_owner_id=u.id).all()
    assert sorted(n.get_content() for n in nodes) == ["from the export", "thought 102"]
    assert {(n.provenance, n.ai_usage, n.privacy_level, n.origin) for n in nodes} == {
        ("prefill_ca", "chat", "private", "twitter")}
    assert {unlicensed_input(n) for n in nodes} == {None}


# ── a no to the tweet seed stops everything ──────────────────────────────

def _say_no(user):
    """The Settings switch, written by another request meanwhile."""
    _db.session.execute(update(User).where(User.id == user.id)
                        .values(prefill_consent="no"))


def test_a_no_while_the_export_is_read_sends_no_request(v1, monkeypatch, imports_mod):
    _write_export(v1.snap, [(101, ACCOUNT, "first")])
    v1.routes[TIMELINE] = [AssertionError("no timeline request")]
    u = _user()
    real = ca.iter_tweets_parquet

    def then_no(*a, **k):
        yield from real(*a, **k)
        _say_no(u)
    monkeypatch.setattr(ca, "iter_tweets_parquet", then_no)

    with pytest.raises(RuntimeError, match="declined"):
        imports_mod.prefill_community_archive_impl(u.id, "vee", {})

    assert _timeline_requests(v1) == []
    assert Node.query.filter_by(human_owner_id=u.id).count() == 0


def test_a_no_during_the_read_stops_it_and_deletes_the_stash(v1, imports_mod):
    _write_export(v1.snap, [])
    u = _user()

    def first_page_then_no(params):
        _say_no(u)
        return _timeline([5, 4], [3, 2], [1])(params)
    v1.routes[TIMELINE] = [first_page_then_no]

    with pytest.raises(imports_mod.PrefillRefused, match="declined"):
        imports_mod.prefill_community_archive_impl(u.id, "vee", {})

    assert len(_timeline_requests(v1)) == 1  # no page after the no
    assert Node.query.filter_by(human_owner_id=u.id).count() == 0
    assert User.query.get(u.id).prefilled_handle is None
    assert not (v1.stash / f"{u.id}-{ACCOUNT}").exists()


def test_a_no_between_two_runs_deletes_the_stash(v1, monkeypatch, imports_mod):
    """Review of #742: the no comes while the follow-up run waits; that run
    stops at its first check and the first run's staged tweets go too."""
    _write_export(v1.snap, [])
    v1.routes[TIMELINE] = [_timeline([6, 5], [4, 3], [2, 1])]
    monkeypatch.setattr(imports_mod, "PREFILL_V1_RUN_SECONDS", 0)
    u = _user()
    with pytest.raises(imports_mod.PrefillContinues):
        imports_mod.prefill_community_archive_impl(u.id, "vee", {})
    stash = v1.stash / f"{u.id}-{ACCOUNT}"
    assert stash.exists()
    other = v1.stash / f"{u.id + 100}-{ACCOUNT}"  # another user's: kept
    other.mkdir()
    _say_no(u)
    _db.session.commit()

    with pytest.raises(imports_mod.PrefillRefused):
        imports_mod.prefill_community_archive_impl(u.id, "vee", {})

    assert not stash.exists() and other.exists()
    assert len(_timeline_requests(v1)) == 1


# ── a resumed pre-fill ───────────────────────────────────────────────────

def test_a_resumed_prefill_keeps_its_cursor_and_its_request_count(v1, monkeypatch, imports_mod):
    """A run that used its time continues in the next run from the stored
    cursor; the stop rule and the request count carry over."""
    _write_export(v1.snap, [(i, ACCOUNT, f"exported {i}") for i in (101, 102)])
    v1.routes[TIMELINE] = [_timeline([105, 104], [102, 101], [100])]
    monkeypatch.setattr(imports_mod, "PREFILL_V1_RUN_SECONDS", 0)  # a page a run
    u = _user()

    with pytest.raises(imports_mod.PrefillContinues) as stop:
        imports_mod.prefill_community_archive_impl(u.id, "vee", {})
    assert (stop.value.fetched, stop.value.requests) == (2, 1)
    assert Node.query.filter_by(human_owner_id=u.id).count() == 0
    result = imports_mod.prefill_community_archive_impl(u.id, "vee", {})

    assert [r["params"].get("cursor") for r in _timeline_requests(v1)] == [None, "p1"]
    assert (result["api_requests"], result["api_stop"]) == (2, ca.STOP_KNOWN_PAGE)
    assert result["created"] == 4


def test_an_unreadable_export_is_read_as_no_export(v1, imports_mod):
    """A cached export that cannot be read: nothing from it, the account
    whole through the API (an export kept partly would hold only its oldest
    tweets)."""
    v1.snap.mkdir(parents=True)
    for name in ("tweets.parquet", "profiles.parquet"):
        (v1.snap / name).write_bytes(b"not a parquet file")
    (v1.snap / "export_id").write_text(EXPORT_ID)
    v1.routes[TIMELINE] = [_timeline([2], [1])]
    u = _user()

    result = imports_mod.prefill_community_archive_impl(u.id, "vee", {})

    assert result["export_id"] is None and result["from_export"] == 0
    assert (result["api_stop"], result["created"]) == (ca.STOP_END, 2)
