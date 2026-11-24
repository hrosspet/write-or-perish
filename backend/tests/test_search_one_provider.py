"""Search data for an account that keeps to one provider.

An account that answered "Keep to Anthropic" (preferred model on
Anthropic, profile_both_providers False, answered) gets no embedding
call: the sweep embeds nothing new for it, search by meaning answers 403
`search_one_provider`, and the user JSON names the provider so the apps
can say so. Accounts that never answered, that said yes, or whose own
provider runs the embedding model are embedded and searched as before.
Vectors stored before the answer stay.

No outside call: embed_texts is replaced everywhere.
"""
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    ExternalItem, ExternalItemEmbedding, Node, NodeEmbedding, User,
)
from backend.utils.embeddings import pack_vector  # noqa: E402

_GLUE = ("backend.celery_app", "backend.tasks.embeddings")
_saved = {k: sys.modules.get(k) for k in _GLUE}
sys.modules["backend.celery_app"] = MagicMock()
sys.modules.pop("backend.tasks.embeddings", None)
import backend.tasks.embeddings as sweep_module  # noqa: E402
for _k, _v in _saved.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v

ANTHROPIC_MODEL = "claude-test"
OPENAI_MODEL = "gpt-test"
ENTRY = "A private entry of an account that keeps to one provider."


@pytest.fixture
def app():
    from flask_login import LoginManager
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SECRET_KEY="test-secret", TESTING=True,
        OPENAI_API_KEY_CHAT="fake-key",
        SUPPORTED_MODELS={
            ANTHROPIC_MODEL: {"provider": "anthropic"},
            OPENAI_MODEL: {"provider": "openai"},
        },
        DEFAULT_LLM_MODEL=ANTHROPIC_MODEL,
    )
    _db.init_app(app)
    lm = LoginManager(app)

    @lm.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.search import search_bp
    app.register_blueprint(search_bp, url_prefix="/api")
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.rollback()
        _db.drop_all()


def _user(name, model=ANTHROPIC_MODEL, answer=False):
    """answer: False ("one provider"), True ("both") or None (never
    answered)."""
    user = User(username=name, preferred_model=model,
                default_ai_usage="chat",
                profile_both_providers=bool(answer),
                profile_both_providers_answered_at=(
                    None if answer is None else datetime.utcnow()))
    _db.session.add(user)
    _db.session.commit()
    return user


def _entry(user, text=ENTRY):
    node = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                ai_usage="chat")
    node.set_content(text)
    _db.session.add(node)
    _db.session.commit()
    return node


def _reference(user, text="a saved page"):
    item = ExternalItem(user_id=user.id, source="web_clip",
                        external_id=f"clip-{user.id}")
    item.set_content(text)
    _db.session.add(item)
    _db.session.commit()
    return item


def _record_sweep_calls(monkeypatch):
    sent = []

    def fake_embed(texts, api_key, **kw):
        sent.append(list(texts))
        return [[0.1] * 4 for _ in texts]

    monkeypatch.setattr(sweep_module, "embed_texts", fake_embed)
    return sent


def _record_query_calls(monkeypatch):
    import backend.utils.embeddings as emb
    sent = []

    def fake_embed(texts, api_key, **kw):
        sent.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(emb, "embed_texts", fake_embed)
    return sent


def _client(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


# ── The sweep ────────────────────────────────────────────────────────────

def test_sweep_embeds_nothing_for_an_account_that_keeps_to_anthropic(
        app, monkeypatch):
    user = _user("keeps_to_anthropic")
    _entry(user)
    _reference(user)
    sent = _record_sweep_calls(monkeypatch)

    result = sweep_module.sweep(50)

    assert sent == []
    assert result["embedded"] == 0 and result["external_embedded"] == 0
    assert NodeEmbedding.query.count() == 0
    assert ExternalItemEmbedding.query.count() == 0


def test_sweep_embeds_an_account_on_the_default_model_that_keeps_to_it(
        app, monkeypatch):
    """No saved model: the default model's provider counts."""
    user = _user("no_saved_model", model=None)
    _entry(user)
    sent = _record_sweep_calls(monkeypatch)

    sweep_module.sweep(50)

    assert sent == []


@pytest.mark.parametrize("model,answer", [
    (ANTHROPIC_MODEL, None),    # never answered
    (ANTHROPIC_MODEL, True),    # both providers
    (OPENAI_MODEL, False),      # keeps to the embedding model's provider
    (OPENAI_MODEL, None),
])
def test_sweep_embeds_the_other_accounts_as_before(
        app, monkeypatch, model, answer):
    user = _user("other", model=model, answer=answer)
    _entry(user)
    _reference(user)
    sent = _record_sweep_calls(monkeypatch)

    result = sweep_module.sweep(50)

    assert [ENTRY] in sent
    assert result["embedded"] == 1 and result["external_embedded"] == 1


def test_sweep_embeds_the_others_next_to_an_account_that_keeps_to_anthropic(
        app, monkeypatch):
    kept = _user("kept")
    _entry(kept, "the kept account's entry")
    other = _user("unanswered", answer=None)
    _entry(other, "the other account's entry")
    nobody = _user("no_model_unanswered", model=None, answer=None)
    _entry(nobody, "an entry on the default model")
    sent = _record_sweep_calls(monkeypatch)

    sweep_module.sweep(50)

    texts = [t for batch in sent for t in batch]
    assert sorted(texts) == ["an entry on the default model",
                             "the other account's entry"]


def test_vectors_stored_before_the_answer_stay(app, monkeypatch):
    user = _user("kept")
    node = _entry(user)
    _db.session.add(NodeEmbedding(
        node_id=node.id, user_id=user.id, model="test",
        content_hash="h", vector=pack_vector([1.0, 0.0])))
    _db.session.commit()
    sent = _record_sweep_calls(monkeypatch)

    result = sweep_module.sweep(50)

    assert sent == []
    assert result["removed"] == 0
    assert NodeEmbedding.query.count() == 1


def test_sweep_embeds_again_after_the_answer_changes_to_both(
        app, monkeypatch):
    user = _user("kept")
    _entry(user)
    sent = _record_sweep_calls(monkeypatch)
    sweep_module.sweep(50)
    assert sent == []

    from backend.utils.profile_providers import set_both_providers
    set_both_providers(user, True)
    _db.session.commit()
    sweep_module.sweep(50)

    assert sent == [[ENTRY]]


# ── Search by meaning ────────────────────────────────────────────────────

def test_search_by_meaning_is_refused_without_an_embedding_call(
        app, monkeypatch):
    from backend.routes.search import (
        SEARCH_ONE_PROVIDER_CODE, SEARCH_ONE_PROVIDER_MESSAGE)
    user = _user("kept")
    sent = _record_query_calls(monkeypatch)

    resp = _client(app, user).get("/api/search/semantic?q=my+question")

    assert resp.status_code == 403
    assert resp.get_json() == {
        "error": SEARCH_ONE_PROVIDER_MESSAGE.format(provider="Anthropic"),
        "code": SEARCH_ONE_PROVIDER_CODE,
        "provider": "anthropic",
    }
    assert sent == []


@pytest.mark.parametrize("model,answer", [
    (ANTHROPIC_MODEL, None),
    (ANTHROPIC_MODEL, True),
    (OPENAI_MODEL, False),
])
def test_search_by_meaning_works_for_the_other_accounts(
        app, monkeypatch, model, answer):
    user = _user("other", model=model, answer=answer)
    sent = _record_query_calls(monkeypatch)

    resp = _client(app, user).get("/api/search/semantic?q=my+question")

    assert resp.status_code == 200
    assert sent == [["my question"]]


def test_an_account_set_to_none_gets_the_ai_usage_answer_first(
        app, monkeypatch):
    user = _user("kept_and_none")
    user.default_ai_usage = "none"
    _db.session.commit()
    sent = _record_query_calls(monkeypatch)

    resp = _client(app, user).get("/api/search/semantic?q=my+question")

    assert resp.status_code == 403
    assert resp.get_json()["code"] == "ai_usage_none"
    assert sent == []


# ── What the user JSON says ──────────────────────────────────────────────

def test_the_user_field_names_the_provider_only_when_search_is_off(app):
    from backend.utils.profile_providers import search_kept_to_provider
    assert search_kept_to_provider(_user("kept")) == "anthropic"
    assert search_kept_to_provider(
        _user("default_model", model=None)) == "anthropic"
    assert search_kept_to_provider(_user("unanswered", answer=None)) is None
    assert search_kept_to_provider(_user("both", answer=True)) is None
    assert search_kept_to_provider(
        _user("openai", model=OPENAI_MODEL)) is None
