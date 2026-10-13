"""Glean again after the day's tweets refreshed (#435, 2026-10-13).

A user gleaned (six picks, external items 1126-1131), the Community
Archive export refreshed overnight, and Glean again under the gleaning
told the user that "the earlier picks numbered 1126 through 1131 don't
match the corpus, which runs only to #5619", with "#5619" linked to an
unrelated tweet. The picks had reached the model as
<quoted_reference id="1126" ...> and the marks note as "reference 1126",
row ids it compared with the new day's numbering; and every "#N" in the
reply that was a number of the render became a tweet link.

Now the earlier picks reach the model as what they are, tweets already
shown, by author and text with the user's marks, and no number; and
nothing the model writes outside a pick becomes a link. The model is
scripted: no real model call.
"""
import json
import re
from datetime import datetime

import pytest  # noqa: F401

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _llm_task_mod, generate_llm_response, _FakeSelf, _mk_user, _resp,
    _fresh,
)
from backend.tests.test_read_context import (
    _CapturingProvider, _prompt_node, _user_node, _stub_archive,
)
from backend.extensions import db as _db
from backend.models import Node, ExternalItem, FeedPick, FeedRender
from backend.utils.ca_feed import (
    CA_READ_AGAIN_TURN, READ_FURTHER_MARKER, pick_label,
    render_read_reply_for_model,
)

# Yesterday's list (export 1) and today's (export 2): different tweets,
# numbered afresh; yesterday's #2 is not in today's list at all.
DAY1 = {
    1: {"username": "alice_w", "tweet_id": "111", "text": "first tweet",
        "posted_at": datetime(2026, 10, 12, 8, 0)},
    2: {"username": "bob_b", "tweet_id": "222",
        "text": "slow mornings make the work better",
        "posted_at": datetime(2026, 10, 12, 9, 0)},
}
DAY2 = {
    1: {"username": "carol_c", "tweet_id": "333", "text": "a new idea today",
        "posted_at": datetime(2026, 10, 13, 8, 0)},
    2: {"username": "dave_d", "tweet_id": "444", "text": "unrelated news",
        "posted_at": datetime(2026, 10, 13, 9, 0)},
}


def _corpus(refs, export_id):
    lines = [f"# Community Archive — export {export_id}"]
    lines += [f"[#{n}] {r['text']}" for n, r in sorted(refs.items())]
    return "\n".join(lines) + "\n"


def _archive(monkeypatch, tmp_path):
    """The stub archive, on the day state["day"] says."""
    _stub_archive(monkeypatch, tmp_path)
    from backend.utils import community_archive as ca
    state = {"day": 1}

    def _render(*args, **kwargs):
        refs, export_id = ((DAY1, "exp-1") if state["day"] == 1
                           else (DAY2, "exp-2"))
        seen = set(kwargs.get("exclude_tweet_ids") or ())
        refs = {n: dict(r) for n, r in refs.items()
                if r["tweet_id"] not in seen}
        stats = {"export_id": export_id, "tweets": len(refs), "accounts": 2,
                 "excluded": 0, "scope": "all",
                 "window_start_at": datetime(2026, 10, 11 + state["day"], 7),
                 "window_end_at": datetime(2026, 10, 12 + state["day"], 7)}
        return _corpus(refs, export_id), stats, refs
    monkeypatch.setattr(ca, "render_recent_tweets", _render)
    return state


def _placeholder(llm_user, alice, parent_id, meta=None):
    n = Node(user_id=llm_user.id, human_owner_id=alice.id,
             parent_id=parent_id, node_type="llm", llm_model="gpt-5",
             llm_task_status="pending", privacy_level="private",
             ai_usage="chat",
             tool_calls_meta=json.dumps(meta) if meta else None)
    n.set_content("[LLM response generation pending...]")
    _db.session.add(n)
    _db.session.commit()
    return n


def _run(monkeypatch, alice, parent, llm_node, response_text):
    _CapturingProvider.kwargs = []
    _CapturingProvider.reset([_resp(response_text)])
    monkeypatch.setattr(_llm_task_mod, "LLMProvider", _CapturingProvider)
    generate_llm_response(_FakeSelf(), parent.id, llm_node.id, "gpt-5",
                          alice.id, source_mode=None, ca_live=True)
    return _CapturingProvider.calls[-1]


def _feed(verdict, picks):
    return json.dumps({"verdict": verdict, "picks": picks})


def _gleaned_yesterday(monkeypatch, tmp_path):
    """A reflection, the Glean prompt under it and yesterday's gleaning,
    whose pick is external item 1126 (as in the report)."""
    state = _archive(monkeypatch, tmp_path)
    alice = _mk_user("alice", approved=True, plan="alpha", is_admin=True)
    llm_user = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    # Row ids run high on a real server; the next item is 1126.
    filler = ExternalItem(id=1125, user_id=alice.id, source="web_clip",
                          external_id="filler")
    filler.set_content("x")
    _db.session.add(filler)
    note = _user_node(alice, None, "how do I pace the mornings?")
    read = _prompt_node(alice, "read_thread", parent_id=note.id)
    _db.session.commit()
    gleaning = _placeholder(llm_user, alice, read.id)
    _run(monkeypatch, alice, read, gleaning, _feed(
        "One tweet today.",
        [{"n": 2, "qt": "This is about your mornings.", "relevance": 50,
          "recommend": True}]))
    gleaning = _fresh(gleaning.id)
    assert gleaning.llm_task_status == "completed"
    pick = ExternalItem.query.filter_by(user_id=alice.id,
                                        external_id="222").one()
    assert pick.id == 1126
    assert gleaning.get_content().endswith("{quote_ext:1126}")
    return state, alice, llm_user, read, gleaning, pick


def _texts(call):
    return [m["text"] for m in call["messages"]]


def test_glean_again_on_a_new_day_sees_the_earlier_picks_as_tweets(app, monkeypatch, tmp_path):  # noqa: F811
    state, alice, llm_user, read, gleaning, pick = _gleaned_yesterday(
        monkeypatch, tmp_path)
    pick.feedback = "good"           # the user's mark, not read yet
    _db.session.commit()
    state["day"] = 2                 # the export refreshed overnight

    again = _placeholder(llm_user, alice, gleaning.id,
                         meta=[{"name": READ_FURTHER_MARKER}])
    call = _run(monkeypatch, alice, gleaning, again, _feed(
        "Yesterday's pick is not in today's list, which runs to #2; "
        "#1 is the one.",
        [{"n": 1, "qt": "A new idea for your mornings.", "relevance": 40,
          "recommend": True}]))
    texts = _texts(call)
    joined = "\n".join(texts)

    # Today's list is in the prompt, numbered afresh.
    assert "[#1] a new idea today" in joined
    # The earlier pick is a tweet with its author and text, no number.
    assistant = [m["text"] for m in call["messages"]
                 if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert ('<picked_tweet author="@bob_b" posted_at="2026-10-12">\n'
            "slow mornings make the work better\n</picked_tweet>"
            in assistant[0])
    # No row id anywhere the model reads, and no raw marker.
    assert "1126" not in joined
    assert "quoted_reference" not in joined
    assert "{quote_ext" not in joined
    # The user's mark on it, naming it the same way, and the request.
    last = texts[-1]
    assert call["messages"][-1]["role"] == "user"
    assert ('@bob_b\'s tweet "slow mornings make the work better" — rated '
            "a good quote") in last
    assert CA_READ_AGAIN_TURN in last
    assert "carry no numbers" in CA_READ_AGAIN_TURN

    # The new gleaning: its pick is today's #1; the verdict's "#2" and
    # "#1" stay the model's words, not links to tweets.
    again = _fresh(again.id)
    assert again.llm_task_status == "completed"
    content = again.get_content()
    assert content.startswith(
        "Yesterday's pick is not in today's list, which runs to #2; "
        "#1 is the one.")
    assert "x.com" not in content
    assert "](" not in content
    markers = re.findall(r"\{quote_ext:(\d+)\}", content)
    assert len(markers) == 1
    assert ExternalItem.query.get(int(markers[0])).external_id == "333"
    assert [p.item.external_id
            for p in FeedPick.query.filter_by(node_id=again.id)] == ["333"]
    assert FeedRender.query.filter_by(
        node_id=again.id).one().export_id == "exp-2"


def test_the_earlier_gleaning_reads_the_same_whatever_the_marks(app, monkeypatch, tmp_path):  # noqa: F811
    """Two runs of Glean again on the same day, the user rating the pick
    in between: everything before the closing note is byte-identical (the
    system block and the earlier gleaning); only the note changes."""
    state, alice, llm_user, read, gleaning, pick = _gleaned_yesterday(
        monkeypatch, tmp_path)
    state["day"] = 2

    first = _placeholder(llm_user, alice, gleaning.id,
                         meta=[{"name": READ_FURTHER_MARKER}])
    call1 = _run(monkeypatch, alice, gleaning, first, _feed("Nothing.", []))
    pick.feedback = "bad"            # a verdict alone; still unread
    _db.session.commit()
    second = _placeholder(llm_user, alice, gleaning.id,
                          meta=[{"name": READ_FURTHER_MARKER}])
    call2 = _run(monkeypatch, alice, gleaning, second, _feed("Nothing.", []))

    texts1, texts2 = _texts(call1), _texts(call2)
    assert len(texts1) == len(texts2)

    def _unstamped(texts):
        # The closing note carries "now"; the rest carries node stamps.
        return texts[:-1]
    assert _unstamped(texts1) == _unstamped(texts2)
    assert "rated a bad quote" not in texts1[-1]
    assert ('@bob_b\'s tweet "slow mornings make the work better" — rated '
            "a bad quote") in texts2[-1]


def test_an_older_verdicts_citation_link_reaches_the_model_without_its_number(app):  # noqa: F811
    """A gleaning from before 2026-10-13 stored "[#2](https://x.com/…)" in
    its verdict; its number was that day's. The model reads the author."""
    alice = _mk_user("alice", approved=True, plan="alpha")
    item = ExternalItem(id=1131, user_id=alice.id, source="read_pick",
                        external_id="222", author_handle="bob_b")
    item.set_content("slow mornings make the work better")
    _db.session.add(item)
    _db.session.commit()
    stored = ("Only [#2](https://x.com/bob_b/status/222) is worth it.\n\n"
              "Your mornings.\n\n{quote_ext:1131}\n\n{quote_ext:4242}")

    text, ids, labels = render_read_reply_for_model(stored, alice.id)

    assert text.startswith("Only @bob_b's tweet is worth it.")
    assert ('<picked_tweet author="@bob_b">\n'
            "slow mornings make the work better\n</picked_tweet>") in text
    # A pick whose row is gone (or not the user's) says so, by no number.
    assert "[A tweet picked here earlier is no longer available.]" in text
    assert "1131" not in text and "4242" not in text and "#2" not in text
    assert ids == [1131]
    assert labels == {
        1131: '@bob_b\'s tweet "slow mornings make the work better"'}


def test_a_pick_label_quotes_the_opening_of_a_long_tweet():
    long_text = ("Mornings are where the week is decided, and I keep "
                 "learning this the hard way every single quarter")
    label = pick_label("bob_b", long_text)
    assert label == ('@bob_b\'s tweet "Mornings are where the week is '
                     'decided, and I keep learning…"')
    assert pick_label(None, "short") == 'an unknown author\'s tweet "short"'
