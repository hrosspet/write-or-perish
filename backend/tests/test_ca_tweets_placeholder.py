"""{ca_tweets?days=N} — the Community Archive feed placeholder (PoC)."""
import json
import pathlib
import types
from datetime import datetime

import pytest

from backend.utils.placeholders import (
    CA_TWEETS_PATTERN, CaTweetsValidationError, UserExportValidationError,
    parse_ca_tweets_days, parse_placeholder_params,
    validate_ca_tweets_placeholders,
)


class TestPlaceholderParsing:
    def test_bare_placeholder_defaults_to_one_day(self):
        m = CA_TWEETS_PATTERN.search("read this {ca_tweets} please")
        assert m.group(0) == "{ca_tweets}"
        assert parse_ca_tweets_days(parse_placeholder_params(m.group(0))) == 1

    def test_days_param(self):
        m = CA_TWEETS_PATTERN.search("{ca_tweets?days=3}")
        assert parse_ca_tweets_days(parse_placeholder_params(m.group(0))) == 3

    @pytest.mark.parametrize("bad", ["{ca_tweets?days=0}", "{ca_tweets?days=x}",
                                     "{ca_tweets?days=-2}", "{ca_tweets?days=4}"])
    def test_bad_days_is_refused_not_defaulted(self, bad):
        with pytest.raises(CaTweetsValidationError):
            validate_ca_tweets_placeholders(bad)

    def test_scope(self):
        from backend.utils.placeholders import parse_ca_tweets_scope
        assert parse_ca_tweets_scope({}) == "all"
        assert parse_ca_tweets_scope({"scope": "Follows"}) == "follows"
        validate_ca_tweets_placeholders("{ca_tweets?days=2&scope=follows}")
        with pytest.raises(CaTweetsValidationError):
            validate_ca_tweets_placeholders("{ca_tweets?scope=friends}")

    def test_days_cap(self):
        from backend.utils.placeholders import CA_TWEETS_MAX_DAYS
        assert CA_TWEETS_MAX_DAYS == 3
        assert parse_ca_tweets_days({"days": "3"}) == 3

    def test_admin_gate(self):
        from backend.utils.placeholders import (
            ca_tweets_allowed, check_ca_tweets_access)
        admin = types.SimpleNamespace(id=1, is_admin=True)
        user = types.SimpleNamespace(id=2, is_admin=False)
        assert ca_tweets_allowed(admin) and not ca_tweets_allowed(user)
        assert not ca_tweets_allowed(None)
        check_ca_tweets_access("{ca_tweets}", admin)
        check_ca_tweets_access("no placeholder", user)
        with pytest.raises(CaTweetsValidationError, match="not available"):
            check_ca_tweets_access("see {ca_tweets?days=1}", user)

    def test_unknown_key_is_refused(self):
        with pytest.raises(CaTweetsValidationError) as exc:
            validate_ca_tweets_placeholders("{ca_tweets?day=1}")
        assert "day" in str(exc.value)

    def test_error_is_an_export_validation_error(self):
        # Every HTTP call site already maps UserExportValidationError → 400.
        assert issubclass(CaTweetsValidationError, UserExportValidationError)

    def test_no_placeholder_is_a_noop(self):
        validate_ca_tweets_placeholders("nothing here")
        validate_ca_tweets_placeholders("")
        validate_ca_tweets_placeholders("{user_export?days=3}")

    def test_user_export_pattern_does_not_match_ca(self):
        from backend.utils.placeholders import USER_EXPORT_PATTERN
        assert USER_EXPORT_PATTERN.search("{ca_tweets?days=1}") is None


@pytest.fixture
def snapshot(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    tweets = tmp_path / "tweets.parquet"
    profiles = tmp_path / "profiles.parquet"
    con.execute(
        "copy (select * from (values "
        "('t1', 'a1', timestamptz '2026-09-13 06:00:00+00', 'newest by alice'), "
        "('t2', 'a2', timestamptz '2026-09-13 05:00:00+00', 'bob says hi'), "
        "('t3', 'a1', timestamptz '2026-09-12 07:00:00+00', 'alice yesterday, in window'), "
        "('t4', 'a1', timestamptz '2026-09-12 05:00:00+00', 'alice too old'), "
        "('t5', 'a2', timestamptz '2026-09-13 05:30:00+00', 'RT @someone: retweet dropped'), "
        "('t6', 'zz', timestamptz '2026-09-13 04:00:00+00', 'no profile row')"
        ") v(tweet_id, account_id, created_at, full_text)) "
        f"to '{tweets}' (format parquet)")
    con.execute(
        "copy (select * from (values ('a1', 'alice'), ('a2', 'Bob')) "
        f"v(account_id, username)) to '{profiles}' (format parquet)")
    (tmp_path / "export_id").write_text("2026-09-13T07-08-27Z")
    return tmp_path


class TestRenderRecentTweets:
    def test_window_format_and_ordering(self, snapshot):
        from backend.utils import community_archive as ca
        text, stats, refs = ca.render_recent_tweets(snapshot, days=1)
        assert stats == {
            "export_id": "2026-09-13T07-08-27Z",
            "window_start": "2026-09-12 06:00", "window_end": "2026-09-13 06:00",
            "tweets": 4, "accounts": 3, "scope": "all",
        }
        assert text.startswith(
            "# Community Archive — tweets from 2026-09-12 06:00 to "
            "2026-09-13 06:00 UTC (last 1 day(s) of export 2026-09-13T07-08-27Z): "
            "4 tweets by 3 accounts, retweets omitted. Each tweet is "
            "numbered; cite a tweet by its number, e.g. #123.")
        # Sections sorted by username (case-insensitive), tweets by time.
        assert text.index("# Tweets by alice") < text.index("# Tweets by Bob") \
            < text.index("# Tweets by zz")
        # Sequential numbers, not tweet ids; refs map them back.
        assert "[#1] alice yesterday, in window" in text
        assert "[#2] newest by alice" in text
        assert "[#3] bob says hi" in text
        assert "#t1]" not in text
        assert "[2026-" not in text  # no per-tweet timestamps
        assert {n: (r["username"], r["tweet_id"]) for n, r in refs.items()} == {
            1: ("alice", "t3"), 2: ("alice", "t1"), 3: ("Bob", "t2"),
            4: ("zz", "t6")}
        assert refs[1]["text"] == "alice yesterday, in window"
        assert refs[1]["posted_at"] == datetime(2026, 9, 12, 7, 0)
        assert "(Community Archive) — 2 tweets" in text
        assert "too old" not in text
        assert "retweet dropped" not in text
        # Accounts missing from profiles.parquet fall back to the id.
        assert "# Tweets by zz (Community Archive) — 1 tweets" in text

    def test_longer_window(self, snapshot):
        from backend.utils import community_archive as ca
        text, stats, refs = ca.render_recent_tweets(snapshot, days=2)
        assert stats["tweets"] == 5 and len(refs) == 5
        assert "too old" in text

    def test_exclude_own_handle(self, snapshot):
        from backend.utils import community_archive as ca
        text, stats, refs = ca.render_recent_tweets(
            snapshot, days=1, exclude_usernames=["@Alice", None, ""])
        assert stats["tweets"] == 2 and stats["accounts"] == 2
        assert "alice" not in text.lower().split("retweets omitted")[1]
        assert [r["tweet_id"] for r in refs.values()] == ["t2", "t6"]

    def test_follows_scope(self, snapshot):
        from backend.utils import community_archive as ca
        text, stats, refs = ca.render_recent_tweets(
            snapshot, days=1, include_usernames=["@BOB", "nobody"])
        assert stats["tweets"] == 1 and stats["scope"] == "follows"
        assert "Only accounts the reader follows." in text
        assert [r["tweet_id"] for r in refs.values()] == ["t2"]

    def test_following_handles(self, snapshot):
        from backend.utils import community_archive as ca
        assert ca.following_handles(snapshot, "peter") is None
        d = snapshot / "following"; d.mkdir()
        (d / "peter.json").write_text(json.dumps({"usernames": ["a", "", "b"]}))
        assert ca.following_handles(snapshot, "peter") == ["a", "b"]

    def test_missing_snapshot_raises(self, tmp_path):
        from backend.utils import community_archive as ca
        with pytest.raises(ca.CommunityArchiveError):
            ca.render_recent_tweets(tmp_path / "nope", days=1)


class TestBatchCollectOne:
    def _client(self, status, entries=()):
        results = list(entries)

        class Batches:
            def retrieve(self, batch_id):
                return types.SimpleNamespace(
                    processing_status=status, request_counts={})

            def results(self, batch_id):
                return iter(results)

        class Client:
            def __init__(self, api_key=None):
                self.messages = types.SimpleNamespace(batches=Batches())
        return Client

    def test_pending(self, monkeypatch):
        import anthropic
        from backend.utils.llm_batch import anthropic_batch_collect_one
        monkeypatch.setattr(anthropic, "Anthropic", self._client("in_progress"))
        assert anthropic_batch_collect_one("k", "b1", "node-1") == (
            "in_progress", None)

    def test_succeeded_maps_to_provider_shape(self, monkeypatch):
        import anthropic
        from backend.utils.llm_batch import anthropic_batch_collect_one
        msg = types.SimpleNamespace(
            content=[types.SimpleNamespace(text="hello "),
                     types.SimpleNamespace(type="tool_use"),
                     types.SimpleNamespace(text="world")],
            usage=types.SimpleNamespace(input_tokens=250_000, output_tokens=900,
                                        cache_read_input_tokens=0,
                                        cache_creation_input_tokens=0),
            stop_reason="end_turn")
        entry = types.SimpleNamespace(
            custom_id="node-7",
            result=types.SimpleNamespace(type="succeeded", message=msg))
        monkeypatch.setattr(anthropic, "Anthropic", self._client("ended", [entry]))
        status, resp = anthropic_batch_collect_one("k", "b1", "node-7")
        assert status == "ended"
        assert resp["content"] == "hello world"
        assert resp["input_tokens"] == 250_000
        assert resp["total_tokens"] == 250_900
        assert resp["batch"] is True and resp["batch_id"] == "b1"
        assert resp["tool_calls"] == [] and resp["truncated"] is False

    def test_errored_item_raises(self, monkeypatch):
        import anthropic
        from backend.utils.llm_batch import anthropic_batch_collect_one
        entry = types.SimpleNamespace(
            custom_id="node-7",
            result=types.SimpleNamespace(type="errored", error="boom"))
        monkeypatch.setattr(anthropic, "Anthropic", self._client("ended", [entry]))
        with pytest.raises(RuntimeError, match="errored"):
            anthropic_batch_collect_one("k", "b1", "node-7")


class TestExpandCitations:
    refs = {1: {"username": "alice", "tweet_id": "t3"},
            12: {"username": "Bob", "tweet_id": "2098672387051175981"}}

    def test_links_known_numbers_only(self):
        from backend.utils.community_archive import expand_ca_citations
        out = expand_ca_citations(
            "★ @Bob #12 — worth it. Also #1, but #2026 and #AI are not "
            "tweets. #99 unknown.", self.refs)
        assert out == (
            "★ @Bob [#12](https://x.com/Bob/status/2098672387051175981) — "
            "worth it. Also [#1](https://x.com/alice/status/t3), but #2026 "
            "and #AI are not tweets. #99 unknown.")

    def test_already_linked_or_in_url_untouched(self):
        from backend.utils.community_archive import expand_ca_citations
        text = "[#1](https://x.com/alice/status/t3) and https://x.com/a/#1"
        assert expand_ca_citations(text, self.refs) == text

    def test_empty(self):
        from backend.utils.community_archive import expand_ca_citations
        assert expand_ca_citations("", self.refs) == ""
        assert expand_ca_citations("#1", {}) == "#1"


class TestOpenAIBatchOne:
    def _client(self, status, lines=(), output_file_id="f1"):
        class Files:
            def create(self, file, purpose):
                return types.SimpleNamespace(id="up1")

            def content(self, fid):
                return types.SimpleNamespace(
                    content="\n".join(json.dumps(x) for x in lines).encode())

        class Batches:
            created = {}

            def create(self, **kw):
                Batches.created.update(kw)
                return types.SimpleNamespace(id="batch_1")

            def retrieve(self, batch_id):
                return types.SimpleNamespace(
                    status=status, request_counts={},
                    output_file_id=output_file_id)

        class Client:
            def __init__(self, api_key=None):
                self.files = Files()
                self.batches = Batches()
        return Client, Batches

    def test_submit_uses_responses_with_strict_schema(self, monkeypatch):
        import openai
        from backend.utils.llm_batch import openai_batch_submit_one
        Client, Batches = self._client("validating")
        monkeypatch.setattr(openai, "OpenAI", Client)
        msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        bid = openai_batch_submit_one("k", "node-1", "gpt-6-astra", msgs, 5000,
                                      output_schema={"type": "object"})
        assert bid == "batch_1"
        assert Batches.created["endpoint"] == "/v1/responses"
        assert Batches.created["input_file_id"] == "up1"

    def test_pending_and_failed(self, monkeypatch):
        import openai
        from backend.utils.llm_batch import openai_batch_collect_one
        Client, _ = self._client("in_progress")
        monkeypatch.setattr(openai, "OpenAI", Client)
        assert openai_batch_collect_one("k", "b", "node-1") == ("in_progress", None)
        Client, _ = self._client("failed")
        monkeypatch.setattr(openai, "OpenAI", Client)
        with pytest.raises(RuntimeError, match="failed"):
            openai_batch_collect_one("k", "b", "node-1")

    def test_completed_maps_to_provider_shape(self, monkeypatch):
        import openai
        from backend.utils.llm_batch import openai_batch_collect_one
        line = {"custom_id": "node-7", "response": {"status_code": 200, "body": {
            "status": "completed",
            "output": [{"type": "reasoning"},
                       {"type": "message", "content": [
                           {"type": "output_text", "text": "{\"verdict\": \"x\", "},
                           {"type": "output_text", "text": "\"picks\": []}"}]}],
            "usage": {"input_tokens": 40000, "output_tokens": 300,
                      "input_tokens_details": {"cached_tokens": 1000,
                                               "cache_write_tokens": 700}}}}}
        Client, _ = self._client("completed", [line])
        monkeypatch.setattr(openai, "OpenAI", Client)
        status, resp = openai_batch_collect_one("k", "b", "node-7")
        assert status == "completed"
        assert json.loads(resp["content"]) == {"verdict": "x", "picks": []}
        assert (resp["input_tokens"], resp["output_tokens"], resp["cached_tokens"]) == (40000, 300, 1000)
        # #286: the write subset is read from the same details object.
        assert resp["cache_write_subset_tokens"] == 700
        assert resp["batch"] is True and resp["truncated"] is False


def test_pathlib_snapshot_dir_accepted(snapshot):
    from backend.utils import community_archive as ca
    assert ca.snapshot_export_id(pathlib.Path(snapshot)) == "2026-09-13T07-08-27Z"


class TestParseFeedReply:
    refs = {1: {"username": "alice", "tweet_id": "t3", "text": "a"},
            2: {"username": "Bob", "tweet_id": "t2", "text": "b"}}

    def test_normalizes(self):
        from backend.utils.ca_feed import parse_feed_reply
        reply = json.dumps({"verdict": " Nothing much. ", "picks": [
            {"n": 2, "why": "fits", "relevance": 140, "recommend": True},
            {"n": 2, "why": "dup", "relevance": 5, "recommend": False},
            {"n": 99, "why": "unknown", "relevance": 5, "recommend": False},
            {"n": "1", "why": "", "relevance": -3, "recommend": 0},
            "junk",
        ]})
        verdict, picks = parse_feed_reply(reply, self.refs)
        assert verdict == "Nothing much."
        assert [(p["n"], p["rank"], p["relevance"], p["recommend"]) for p in picks] == [
            (2, 1, 100, True), (1, 2, 0, False)]
        assert picks[0]["ref"] is self.refs[2]

    def test_empty_picks_is_valid(self):
        from backend.utils.ca_feed import parse_feed_reply, render_feed_reply
        verdict, picks = parse_feed_reply('{"verdict": "Nothing.", "picks": []}', self.refs)
        assert picks == []
        assert render_feed_reply(verdict, picks) == "Nothing."

    def test_render_counts(self):
        from backend.utils.ca_feed import parse_feed_reply, render_feed_reply
        verdict, picks = parse_feed_reply(json.dumps({"verdict": "One.", "picks": [
            {"n": 1, "why": "w", "relevance": 60, "recommend": True},
            {"n": 2, "why": "w", "relevance": 6, "recommend": False}]}), self.refs)
        assert render_feed_reply(verdict, picks) == "One.\n\n2 tweets below, 1 recommended."

    @pytest.mark.parametrize("bad", ["not json", "[]", '{"verdict": "x"}'])
    def test_bad_shape_raises(self, bad):
        from backend.utils.ca_feed import FeedReplyError, parse_feed_reply
        with pytest.raises(FeedReplyError):
            parse_feed_reply(bad, self.refs)

    def test_schema_is_strict(self):
        from backend.utils.ca_feed import FEED_SCHEMA
        assert FEED_SCHEMA["additionalProperties"] is False
        item = FEED_SCHEMA["properties"]["picks"]["items"]
        assert set(item["required"]) == {"n", "why", "relevance", "recommend"}
