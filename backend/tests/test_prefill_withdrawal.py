"""#662: the answer to "Start from your tweets?" can be changed in Settings,
both ways, and a no stops what the yes allowed.

- The switch: PUT /dashboard/user takes yes and no in either order, at any
  time; each change is stamped. "Not answered" cannot be chosen again.
- A no given after a yes refuses every pre-fill route at once (the admin
  pre-fills, Build profile, Infer intentions) and a yes lifts it.
- The tweets a pre-fill already brought in stay in the user's Loore, but
  every AI read of the user's writing as a whole leaves them out after a
  no: the profile windows (both reads of #612), the raw windows replies
  and poll drafts read, the AI's archive search (semantic_search), and
  the counts that decide when a profile build
  or a recent-context summary is due. Their own download keeps them. The
  user's own entries, and an archive they uploaded themselves, are read
  as before.
- The admin load script refuses an account that said no.

The pre-fill fetch that sees a no given during the pull is in
test_prefill_x_api.py (test_no_given_during_the_pull_imports_nothing).
Every text is synthetic.
"""
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from backend.tests.test_prefill_consent import app, users, _login, _db  # noqa: F401
from backend.models import Node, User

T0 = datetime(2026, 1, 1, 9, 0, 0)
TWEET_TEXT = "a synthetic public tweet"
OWN_TEXT = "a synthetic entry written in Loore"


def _node(user, text, minutes, provenance=None, parent=None, origin=None,
          tokens=100, ai_usage="chat"):
    n = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
             privacy_level="private", ai_usage=ai_usage,
             parent_id=parent.id if parent else None,
             origin=origin, provenance=provenance)
    n.set_content(text)
    n.token_count = tokens
    n.created_at = T0 + timedelta(minutes=minutes)
    _db.session.add(n)
    _db.session.flush()
    return n


@pytest.fixture
def writer(app, users):  # noqa: F811
    """birdperson: two pre-filled tweets (one a self-reply), an entry of
    their own under the first, an entry of their own on its own, and a
    tweet from an archive they uploaded themselves."""
    u = users["tw"]
    u.approved = True
    u.prefilled_handle = "birdperson"
    t1 = _node(u, TWEET_TEXT + " one", 0, "prefill_ca", origin="twitter")
    t2 = _node(u, TWEET_TEXT + " two", 1, "prefill_x", parent=t1,
               origin="twitter")
    own_reply = _node(u, OWN_TEXT + " under a tweet", 2, parent=t1)
    own = _node(u, OWN_TEXT, 3)
    upload = _node(u, "a tweet from my own archive upload", 4,
                   "archive_upload", origin="twitter")
    _db.session.commit()
    return {"user": u, "tweets": [t1, t2], "own": [own_reply, own],
            "upload": upload}


def _as(client, user_id):
    """Sign *client* in as *user_id*. The tests run inside one app context,
    where Flask-Login caches the signed-in user on ``g``: drop it, or the
    next client's request is served as the previous user."""
    from flask import g
    g.pop("_login_user", None)
    _login(client, user_id)


def _set_consent(user, value):
    user.prefill_consent = value
    _db.session.commit()


# ── The switch ────────────────────────────────────────────────────────────

class TestSwitch:
    def test_yes_no_yes_each_stamped(self, app, users):  # noqa: F811
        """Control (passes before #662): the route already took both
        answers; the Settings switch relies on it in both directions."""
        client = app.test_client()
        _login(client, users["tw"].id)
        stamps = []
        for value in ("yes", "no", "yes", "no"):
            resp = client.put("/api/dashboard/user",
                              json={"prefill_consent": value})
            assert resp.status_code == 200
            assert resp.get_json()["user"]["prefill_consent"] == value
            stamps.append(User.query.get(users["tw"].id).prefill_consent_at)
        assert all(stamps) and stamps == sorted(stamps)

    def test_not_answered_cannot_be_chosen_again(self, app, users):  # noqa: F811
        """Control: "Not answered yet" is a state, not a choice."""
        client = app.test_client()
        _login(client, users["tw"].id)
        client.put("/api/dashboard/user", json={"prefill_consent": "no"})
        resp = client.put("/api/dashboard/user", json={"prefill_consent": None})
        assert resp.status_code == 400
        assert User.query.get(users["tw"].id).prefill_consent == "no"


class TestNoFromSettingsRefusesPrefillWork:
    """Control (the gates are #346's): the switch's no reaches every route
    a yes allowed, and its yes lifts them again."""

    def test_routes_follow_the_switch(self, app, users, monkeypatch):  # noqa: F811
        import importlib
        tasks = {}
        for module, name in (("backend.tasks.imports", "prefill_community_archive"),
                             ("backend.tasks.imports", "prefill_x_api"),
                             ("backend.tasks.intentions", "infer_intentions"),
                             ("backend.tasks.profile_batch",
                              "seed_profile_batch_for_user")):
            task = MagicMock()
            task.delay.return_value.id = "task-1"
            monkeypatch.setattr(importlib.import_module(module), name, task)
            tasks[name] = task
        me = app.test_client()
        _as(me, users["tw"].id)
        assert me.put("/api/dashboard/user",
                      json={"prefill_consent": "yes"}).status_code == 200
        assert me.put("/api/dashboard/user",
                      json={"prefill_consent": "no"}).status_code == 200

        admin = app.test_client()
        _as(admin, users["admin"].id)
        uid = users["tw"].id
        urls = [f"/api/admin/users/{uid}/prefill",
                f"/api/admin/users/{uid}/prefill-x",
                f"/api/admin/users/{uid}/infer_intentions",
                f"/api/admin/users/{uid}/build_profile"]
        for url in urls:
            resp = admin.post(url, json={"handle": "bird"})
            assert resp.status_code == 400, url
            assert resp.get_json()["code"] == "prefill_declined"
        assert not any(t.delay.called for t in tasks.values())

        _as(me, uid)
        assert me.put("/api/dashboard/user",
                      json={"prefill_consent": "yes"}).status_code == 200
        _as(admin, users["admin"].id)
        for url in urls:
            assert admin.post(url, json={"handle": "bird"}).status_code == 202, url


# ── The tweets already brought in ─────────────────────────────────────────

def _texts(export):
    if isinstance(export, dict):
        export = export["content"]
    return export or ""


class TestWithheldFromAIReads:
    def test_profile_window_rows(self, writer):
        """Both reads of a profile pass (the database walk and the one
        read of #612) leave the pre-filled tweets out after a no, and
        read them again after a yes."""
        from backend.routes.export_data import (
            WindowScope, _select_incremental_rows, count_remaining_units)
        u = writer["user"]
        tweet_ids = {t.id for t in writer["tweets"]}
        everyone = tweet_ids | {n.id for n in writer["own"]} | {writer["upload"].id}

        def ids_now():
            walk = {r.id for r in _select_incremental_rows(
                u.id, filter_ai_usage=True, created_after=T0 - timedelta(days=1))}
            read = {r.id for r in WindowScope(u.id).rows(T0 - timedelta(days=1))}
            assert walk == read
            return walk

        assert ids_now() == everyone
        assert count_remaining_units(u.id, None) == 500
        _set_consent(u, "no")
        assert ids_now() == everyone - tweet_ids
        assert count_remaining_units(u.id, None) == 300
        _set_consent(u, "yes")
        assert ids_now() == everyone

    def test_a_no_during_a_pass_makes_its_read_stale(self, writer):
        """A pass that read the metadata before the no reads it again
        before its next window (_PassScope.units checks fresh())."""
        from backend.routes.export_data import WindowScope
        u = writer["user"]
        scope = WindowScope(u.id)
        assert scope.fresh()
        _set_consent(u, "no")
        assert not scope.fresh()
        scope.reload()
        assert {r.id for r in scope.rows()} == (
            {n.id for n in writer["own"]} | {writer["upload"].id})

    def test_rendered_profile_window(self, writer):
        """The text a profile build sends: the user's own entries, the one
        under a tweet included, and none of the tweets' text."""
        from backend.tasks.exports import render_profile_window
        u = writer["user"]
        _set_consent(u, "no")
        text = _texts(render_profile_window(u, 10_000, T0 - timedelta(days=1)))
        assert OWN_TEXT in text and OWN_TEXT + " under a tweet" in text
        assert "my own archive upload" in text
        assert TWEET_TEXT not in text

    @pytest.mark.parametrize("strategy", ["engaged_threads", "authored_threads"])
    def test_background_raw_windows(self, writer, strategy):
        """The writing a reply ({user_export}, {user_recent_raw}), an
        intentions run or a poll draft reads as background."""
        from backend.routes.export_data import build_user_export_content
        u = writer["user"]
        before = _texts(build_user_export_content(
            u, max_tokens=10_000, filter_ai_usage=True,
            include_strategy=strategy))
        assert TWEET_TEXT in before
        _set_consent(u, "no")
        after = _texts(build_user_export_content(
            u, max_tokens=10_000, filter_ai_usage=True,
            include_strategy=strategy))
        assert TWEET_TEXT not in after
        assert OWN_TEXT in after

    def test_the_users_own_download_keeps_them(self, writer):
        """Control: the user's own export reads everything they hold."""
        from backend.routes.export_data import build_user_export_content
        u = writer["user"]
        _set_consent(u, "no")
        assert TWEET_TEXT in _texts(
            build_user_export_content(u, filter_ai_usage=False))

    def test_due_counts(self, writer):
        """The counts that decide when a profile build or a summary is due
        count what those builds would read."""
        from backend.tasks import exports as ex
        from backend.tasks import profile_batch as pb
        from backend.tasks import recent_context as rc
        u = writer["user"]
        before = (ex._estimate_source_tokens(u), pb._new_token_count(u, None),
                  rc._count_total_eligible_tokens(u.id),
                  rc._count_new_tokens(u.id, T0 - timedelta(days=1)),
                  ex._first_writing_date(u.id, None))
        assert before[:4] == (500, 500, 500, 500)
        assert before[4] == T0
        _set_consent(u, "no")
        after = (ex._estimate_source_tokens(u), pb._new_token_count(u, None),
                 rc._count_total_eligible_tokens(u.id),
                 rc._count_new_tokens(u.id, T0 - timedelta(days=1)),
                 ex._first_writing_date(u.id, None))
        assert after[:4] == (300, 300, 300, 300)
        assert after[4] == T0 + timedelta(minutes=2)

    def test_content_class_follows(self, writer):
        """The planner's prior picks "tweets" for a corpus of mostly
        tweets; after a no the tweets it reads are the uploaded ones."""
        from backend.tasks.exports import content_class
        u = writer["user"]
        _node(u, "more pre-filled tweet", 5, "prefill_ca", origin="twitter",
              tokens=1000)
        _db.session.commit()
        assert content_class(u) == "tweets"
        _set_consent(u, "no")
        assert content_class(u) == "threads"

    def test_the_ais_archive_search(self, writer):
        """The semantic_search tool a reply may call ranks the archive's
        embeddings: after a no the pre-filled tweets are not among them
        (no model or embedding call here: the query vector is given)."""
        from backend.models import NodeEmbedding
        from backend.utils.embeddings import pack_vector, retrieve_relevant_snippets
        u = writer["user"]
        for n in writer["tweets"] + writer["own"]:
            _db.session.add(NodeEmbedding(
                node_id=n.id, user_id=u.id, model="test", content_hash="h",
                vector=pack_vector([1.0, 0.0])))
        _db.session.commit()

        def found():
            return {nid for nid, _at, _snip, _score in retrieve_relevant_snippets(
                u.id, "q", [], None, k=10, min_score=0.0,
                query_vector=[1.0, 0.0])}

        everything = {n.id for n in writer["tweets"] + writer["own"]}
        assert found() == everything
        _set_consent(u, "no")
        assert found() == {n.id for n in writer["own"]}

    def test_other_accounts_queries_do_not_change(self, writer, users):  # noqa: F811
        """Control: an unanswered or yes account adds no clause."""
        from backend.utils.privacy import withheld_prefill_clauses
        u = writer["user"]
        assert withheld_prefill_clauses(u.id) == []
        _set_consent(u, "yes")
        assert withheld_prefill_clauses(u.id) == []
        assert withheld_prefill_clauses(users["mail"].id) == []
        _set_consent(u, "no")
        assert len(withheld_prefill_clauses(u.id)) == 1


# ── The admin load script ─────────────────────────────────────────────────

def test_load_script_refuses_an_account_that_said_no(app, users, tmp_path, capsys):  # noqa: F811
    from backend.scripts import load_user
    dump = tmp_path / "bundle.jsonl"
    header = {"format": 2, "user": {"username": "birdperson"}, "profiles": []}
    node = {"id": 1, "content": TWEET_TEXT, "origin": "twitter",
            "source_key": "tw:1", "ai_usage": "chat",
            "created_at": T0.isoformat()}
    dump.write_text(json.dumps(header) + "\n" + json.dumps(node) + "\n")
    _set_consent(users["tw"], "no")
    with pytest.raises(SystemExit, match="said no"):
        load_user._run(str(dump), None, True, False)
    assert Node.query.count() == 0
