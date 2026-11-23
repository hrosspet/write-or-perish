"""Profile builds and the training key, with entries shared through Circles
in the account (Peter, 2026-11-23).

Peter's rule: "Profile generation can use Train only if all included notes
are train and my Account setting for ai_usage is train. Regardless of who
authored the note." And: the document a build saves keeps its AI usage
(the account's), "because that's already processed data".

How the code holds it: every build of the profile, the recent-writing
summary and the intentions calls its model on the chat key, named in the
code, whatever its input holds. The training key is never used for a
build, so it is never used for one whose input holds a note that is not
Train, or for an account that is not Train. Shared entries change
nothing there: each person's corpus holds only their own nodes of a
shared thread (Builder's choice 10), and the key does not depend on the
corpus at all.

- A guard over the build modules: each call that picks API keys names
  "chat".
- The recent-context build and the intentions build run for real, the
  author's and a reader's, in an account that is Train with every note
  at Train and a shared thread in the corpus: the call goes out on the
  chat key, nobody else's words are in it, and the summary is saved at
  the account's AI usage.

No model is called (the provider is stubbed), nothing is billed.
"""
import ast
import pathlib
from datetime import datetime

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, _mk_user,
)
from backend.tests.test_circles_task import (  # noqa: F401 (fixture)
    circles, _node, _share_with,
)
from backend.extensions import db as _db
from backend.models import UserProfile, UserRecentContext

CHAT_KEYS = {"openai": "sk-chat", "anthropic": "ant-chat"}

# Every module that builds a person's profile, recent-writing summary or
# intentions from their writing.
BUILD_MODULES = (
    "tasks/exports.py", "tasks/profile_batch.py", "tasks/profile_windows.py",
    "tasks/recent_context.py", "tasks/intentions.py",
)


def test_every_build_picks_the_chat_key_by_name():
    backend = pathlib.Path(__file__).resolve().parents[1]
    calls = []
    for rel in BUILD_MODULES:
        tree = ast.parse((backend / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", None)
                    == "get_api_keys_for_usage"):
                calls.append((rel, node.lineno, node.args[1]))
    assert {rel for rel, _, _ in calls} == set(BUILD_MODULES)
    for rel, line, key in calls:
        assert isinstance(key, ast.Constant) and key.value == "chat", (
            f"{rel}:{line} picks its key from something other than 'chat'")


def _world():
    """anna (Train) shares her entry (Train) with ben and chidi (Train).
    ben replies and has an AI reply of his own shared into the thread;
    anna answers ben; chidi replies. ben also has an entry of his own.
    Every note is Train."""
    anna = _mk_user("anna", approved=True, plan="alpha",
                    default_ai_usage="train")
    ben = _mk_user("ben", approved=True, plan="alpha",
                   default_ai_usage="train")
    chidi = _mk_user("chidi", approved=True, plan="alpha",
                     default_ai_usage="train")
    gpt = _mk_user("gpt-5", twitter_id="llm-gpt-5")
    entry = _node(anna, None, "ANNA SHARED ENTRY", ai_usage="train")
    _share_with(entry, ben, chidi)
    b = _node(ben, entry, "BEN TYPED REPLY", privacy="circles",
              ai_usage="train")
    _node(gpt, b, "BEN AI REPLY", privacy="circles", ai_usage="train",
          human_owner=ben, node_type="llm", llm_model="gpt-5")
    _node(anna, b, "ANNA ANSWERS BEN", privacy="circles", ai_usage="train")
    _node(chidi, entry, "CHIDI TYPED REPLY", privacy="circles",
          ai_usage="train")
    _node(ben, None, "BEN OWN ENTRY", ai_usage="train")
    _db.session.commit()
    return dict(anna=anna, ben=ben, chidi=chidi)


CORPORA = [
    ("anna", ["ANNA SHARED ENTRY", "ANNA ANSWERS BEN"],
     ["BEN TYPED REPLY", "BEN AI REPLY", "CHIDI TYPED REPLY",
      "BEN OWN ENTRY"]),
    ("ben", ["BEN TYPED REPLY", "BEN AI REPLY", "BEN OWN ENTRY"],
     ["ANNA SHARED ENTRY", "ANNA ANSWERS BEN", "CHIDI TYPED REPLY"]),
]


@pytest.mark.parametrize("who, own, others", CORPORA)
def test_the_recent_context_build_goes_out_on_the_chat_key(
        app, circles, monkeypatch, who, own, others):  # noqa: F811
    import backend.tasks.recent_context as rc
    import backend.utils.llm_nodes as llm_nodes
    w = _world()
    calls = []

    def completion(model_id, messages, api_keys, **kwargs):
        text = "".join(block["text"] for m in messages
                       for block in m["content"])
        calls.append((dict(api_keys), text))
        return {"content": "SUMMARY", "input_tokens": 10,
                "output_tokens": 5, "total_tokens": 15}

    monkeypatch.setattr(rc, "flask_app", app)
    monkeypatch.setattr(rc.LLMProvider, "get_completion",
                        staticmethod(completion))
    monkeypatch.setattr(llm_nodes, "default_model_for", lambda u: "gpt-5")
    monkeypatch.setattr(rc, "_load_prompt",
                        lambda name, user_id=None:
                        "{user_profile}{recent_data}")
    user = w[who]
    # A summary covers the writing since the profile's cutoff.
    profile = UserProfile(user_id=user.id, generated_by="gpt-5",
                          tokens_used=0, ai_usage="train",
                          source_data_cutoff=datetime(2000, 1, 1))
    profile.set_content(f"PROFILE OF {who.upper()}")
    _db.session.add(profile)
    _db.session.commit()
    rc._generate_recent_context_impl(
        user.id, profile_id=profile.id,
        data_cutoff_iso=datetime(2000, 1, 1).isoformat())

    assert len(calls) == 1
    keys, text = calls[0]
    assert keys == CHAT_KEYS
    for words in own:
        assert words in text, words
    for words in others:
        assert words not in text, words
    # The summary keeps the account's AI usage (Peter: "I'm fine with
    # setting it to train").
    saved = UserRecentContext.query.filter_by(user_id=user.id).one()
    assert saved.ai_usage == "train"


@pytest.mark.parametrize("who, own, others", CORPORA)
def test_the_intentions_build_goes_out_on_the_chat_key(
        app, circles, monkeypatch, who, own, others):  # noqa: F811
    import backend.llm_providers as lp
    import backend.tasks.intentions as it
    import backend.utils.llm_nodes as llm_nodes
    w = _world()
    seen = {}

    def fit(model_id, api_keys, limit, cap, build, **kwargs):
        seen["keys"] = dict(api_keys)
        built = build(cap or 100_000)
        seen["text"] = built[1]
        return built, cap, None

    monkeypatch.setattr(lp, "fit_by_count", fit)
    monkeypatch.setattr(llm_nodes, "default_model_for", lambda u: "gpt-5")
    prep = it._prepare(w[who].id)

    # The sync call and the batch both send prep["api_keys"].
    assert prep["api_keys"] == CHAT_KEYS
    assert seen["keys"] == CHAT_KEYS
    for words in own:
        assert words in seen["text"], words
    for words in others:
        assert words not in seen["text"], words
