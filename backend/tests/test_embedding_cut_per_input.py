"""An embedding call shortens only the input that is over the token limit.

The sweep sends up to 32 entries of different accounts in one call. When
one of them is over the provider's 8,191-token limit, that one is cut
shorter and every other input of the call is embedded at the full cut.
The result keeps input order, one cost row covers all the calls a batch
made, and an input still over the limit at MIN_EMBED_CHARS raises as
before. The OpenAI client is a local stand-in that applies the token
limit (one token per four ASCII characters, two per other character).
"""
import sys
import types

import pytest

from backend.tests.test_semantic_search import (  # noqa: F401
    app, _mk_node, _sweep, _db,
)
from backend.models import APICostLog, NodeEmbedding, User
from backend.utils.embeddings import (
    MAX_EMBED_CHARS, MIN_EMBED_CHARS, embed_texts, unpack_vector,
)


def _tokens(text):
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return ascii_chars // 4 + 2 * (len(text) - ascii_chars)


class _Embeddings:
    def __init__(self, calls, fail_on=None, limit=8191):
        self.calls = calls
        self.fail_on = fail_on
        self.limit = limit

    def create(self, model, input):
        if self.fail_on is not None and self.fail_on(input):
            raise RuntimeError("Error code: 500 - server error")
        if any(_tokens(t) > self.limit for t in input):
            self.calls.append(("refused", [len(t) for t in input]))
            raise RuntimeError(
                "Error code: 400 - This model's maximum input length is "
                "8191 tokens")
        self.calls.append(("ok", [len(t) for t in input]))
        # The vector records how many characters of the input were sent.
        return types.SimpleNamespace(
            usage=types.SimpleNamespace(total_tokens=len(input)),
            data=[types.SimpleNamespace(embedding=[float(len(t)), 1.0])
                  for t in input])


def _fake_openai(monkeypatch, fail_on=None, limit=8191):
    calls = []
    fake = types.ModuleType("openai")
    fake.OpenAI = lambda api_key=None: types.SimpleNamespace(
        embeddings=_Embeddings(calls, fail_on, limit))
    monkeypatch.setitem(sys.modules, "openai", fake)
    return calls


ENGLISH = "plain english text " * 800       # 15,200 characters
DENSE = "漢" * 9000                          # over the limit until 4,000


def test_another_accounts_dense_entry_leaves_this_entry_whole(
        app, monkeypatch):  # noqa: F811
    with app.app_context():
        _fake_openai(monkeypatch)
        a = User.query.first()
        b = User(username="other-account")
        _db.session.add(b)
        _db.session.commit()
        english = _mk_node(a.id, ENGLISH)
        dense = _mk_node(b.id, DENSE)
        assert _sweep(50)["embedded"] == 2
        row = NodeEmbedding.query.filter_by(node_id=english.id).one()
        assert unpack_vector(row.vector)[0] == 15200.0
        dense_row = NodeEmbedding.query.filter_by(node_id=dense.id).one()
        assert unpack_vector(dense_row.vector)[0] == 4000.0
        # One cost row for the batch, with the tokens of every call.
        costs = APICostLog.query.filter_by(request_type="embedding").all()
        assert len(costs) == 1 and costs[0].input_tokens == 2


def test_only_the_inputs_over_the_limit_are_cut_and_order_is_kept(
        app, monkeypatch):  # noqa: F811
    with app.app_context():
        calls = _fake_openai(monkeypatch)
        texts = ([f"entry {i} " + "word " * 3000 for i in range(5)]
                 + [DENSE] + ["short"] * 10 + [DENSE] + ["x" * 20_000])
        vectors = embed_texts(texts, "k")
        sent = [v[0] for v in vectors]
        expected = [float(min(len(t), MAX_EMBED_CHARS)) for t in texts]
        expected[5] = expected[16] = 4000.0
        assert sent == expected
        # Bounded: about two calls per halving of the call's size for
        # each dense input, plus its own halvings.
        assert len(calls) <= 25


def test_a_single_input_still_over_the_limit_at_the_floor_raises(
        app, monkeypatch):  # noqa: F811
    with app.app_context():
        calls = _fake_openai(monkeypatch, limit=100)
        with pytest.raises(RuntimeError, match="maximum input length"):
            embed_texts(["a" * 3000], "k")
        assert calls[-1] == ("refused", [MIN_EMBED_CHARS])
        assert APICostLog.query.count() == 0


def test_a_failed_later_call_keeps_the_cost_of_the_calls_that_went_through(
        app, monkeypatch):  # noqa: F811
    with app.app_context():
        user = User.query.first()
        _fake_openai(monkeypatch, fail_on=lambda inputs: (
            len(inputs) <= 2 and inputs[-1:] == ["late"]))
        with pytest.raises(RuntimeError, match="server error"):
            embed_texts(["first", DENSE, "x" * 9000, "late"], "k",
                        user_id=user.id)
        costs = APICostLog.query.filter_by(user_id=user.id).all()
        assert len(costs) == 1 and costs[0].input_tokens > 0
