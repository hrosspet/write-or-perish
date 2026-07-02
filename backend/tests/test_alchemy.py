"""Tests for Alchemical Mode (ALCHEMY_V1 dark flag).

Covers: the double gate (readiness pre-filter + explicit opt-in), the
hidden alchemy prompt (off the Prompts page, blocked from read/edit),
source chunking, the read_source retrieval tool + injection, the
readiness verdict parser (fails closed), and flag-off 404s.

Mirrors test_textmode.py's app/mocking pattern: celery + the LLM task
module are mocked so create_llm_placeholder never touches a real queue.
"""
import os
import sys
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
sys.modules.setdefault("ffmpeg", MagicMock())

_mock_llm_task_module = MagicMock()
_mock_task_result = MagicMock()
_mock_task_result.id = "fake-task-id"
_mock_llm_task_module.generate_llm_response.delay.return_value = (
    _mock_task_result
)
sys.modules["backend.tasks.llm_completion"] = _mock_llm_task_module

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import (                     # noqa: E402
    User, Node, NodeContextArtifact,
    AlchemySource, AlchemySourceChunk, AlchemyState,
)
import backend.models as _real_backend_models    # noqa: E402
from backend.utils.alchemy_sources import (      # noqa: E402
    html_to_text, chunk_text,
)
from backend.utils.embeddings import pack_vector  # noqa: E402


def _make_app(alchemy_v1=True):
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["ALCHEMY_V1"] = alchemy_v1
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5"},
    }

    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.alchemy import alchemy_bp
    from backend.routes.prompts import prompts_bp
    app.register_blueprint(alchemy_bp, url_prefix="/api/alchemy")
    app.register_blueprint(prompts_bp, url_prefix="/api/prompts")
    return app


@pytest.fixture
def app():
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}

    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]

    app = _make_app()
    with app.app_context():
        _db.create_all()
        user = User(username="tester")
        _db.session.add(user)
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        del sys.modules[k]
    sys.modules.update(saved)


@pytest.fixture
def client(app):
    client = app.test_client()
    user = User.query.first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


def _mk_source(slug="meditationbook", with_chunks=True, vectors=True):
    source = AlchemySource(slug=slug, title="Meditationbook",
                           description="test source")
    _db.session.add(source)
    _db.session.flush()
    if with_chunks:
        for i, (heading, content, vec) in enumerate([
            ("On sitting", "The practice begins with sitting down.",
             [1.0, 0.0]),
            ("On noting", "Noting is the act of gently labeling.",
             [0.0, 1.0]),
        ]):
            _db.session.add(AlchemySourceChunk(
                source_id=source.id, idx=i, heading=heading,
                content=content,
                vector=pack_vector(vec) if vectors else None,
            ))
    _db.session.commit()
    return source


def _mk_state(uid, readiness="ready", opted_in=False, source_slug=None):
    from datetime import datetime
    state = AlchemyState(user_id=uid, readiness_status=readiness)
    if opted_in:
        state.opted_in_at = datetime.utcnow()
        state.source_slug = source_slug
    _db.session.add(state)
    _db.session.commit()
    return state


# ── Model ────────────────────────────────────────────────────────────────

def test_status_for_user_transitions(app):
    uid = User.query.first().id
    state = _mk_state(uid, readiness="not_checked")
    assert state.status_for_user is None
    state.readiness_status = "not_ready"
    assert state.status_for_user is None
    state.readiness_status = "ready"
    assert state.status_for_user == "offered"
    from datetime import datetime
    state.opted_in_at = datetime.utcnow()
    assert state.status_for_user == "active"


# ── Chunking ─────────────────────────────────────────────────────────────

def test_html_to_text_keeps_headings():
    html = ("<html><head><style>x{}</style></head><body>"
            "<h2>First Section</h2><p>Alpha beta.</p>"
            "<h2>Second</h2><p>Gamma &amp; delta.</p></body></html>")
    text = html_to_text(html)
    assert "## First Section" in text
    assert "Gamma & delta." in text
    assert "style" not in text


def test_chunk_text_splits_on_headings_and_size():
    big_para = "Long paragraph sentence. " * 30  # ~750 chars
    text = ("## One\n" + "\n\n".join([big_para] * 5)
            + "\n## Two\nShort body of the second section that is long "
              "enough to stand alone as a chunk on its own merits, with "
              "plenty of extra words to clear the merge threshold easily "
              "and then some more padding to be safe about the minimum "
              "chunk length constant used by the splitter.")
    chunks = chunk_text(text)
    assert all(len(c) <= 3200 for _h, c in chunks)
    assert any(h == "One" for h, _c in chunks)
    assert any(h == "Two" for h, _c in chunks)
    # Section One was oversized → multiple chunks under the same heading.
    assert sum(1 for h, _c in chunks if h == "One") >= 2


# ── Routes: the double gate ──────────────────────────────────────────────

def test_status_and_sources(app, client):
    uid = User.query.first().id
    _mk_source()
    r = client.get("/api/alchemy/status")
    assert r.status_code == 200
    assert r.get_json()["status"] is None  # no state row yet

    _mk_state(uid, readiness="ready")
    assert client.get(
        "/api/alchemy/status").get_json()["status"] == "offered"

    srcs = client.get("/api/alchemy/sources").get_json()["sources"]
    assert srcs[0]["slug"] == "meditationbook"
    assert srcs[0]["available"] is True


def test_opt_in_requires_readiness_gate(app, client):
    _mk_source()
    # No state row at all → 403.
    r = client.post("/api/alchemy/opt-in",
                    json={"source_slug": "meditationbook",
                          "accept_risks": True})
    assert r.status_code == 403

    uid = User.query.first().id
    _mk_state(uid, readiness="not_ready")
    r = client.post("/api/alchemy/opt-in",
                    json={"source_slug": "meditationbook",
                          "accept_risks": True})
    assert r.status_code == 403


def test_opt_in_requires_explicit_risk_acknowledgment(app, client):
    uid = User.query.first().id
    _mk_source()
    _mk_state(uid, readiness="ready")
    r = client.post("/api/alchemy/opt-in",
                    json={"source_slug": "meditationbook"})
    assert r.status_code == 400
    r = client.post("/api/alchemy/opt-in",
                    json={"source_slug": "meditationbook",
                          "accept_risks": "yes"})  # truthy is NOT enough
    assert r.status_code == 400


def test_opt_in_happy_path_then_conflict_then_opt_out(app, client):
    uid = User.query.first().id
    _mk_source()
    _mk_state(uid, readiness="ready")
    r = client.post("/api/alchemy/opt-in",
                    json={"source_slug": "meditationbook",
                          "accept_risks": True})
    assert r.status_code == 200
    assert r.get_json()["status"] == "active"

    # Double opt-in conflicts.
    r = client.post("/api/alchemy/opt-in",
                    json={"source_slug": "meditationbook",
                          "accept_risks": True})
    assert r.status_code == 409

    # Stopping is one call, no ceremony.
    r = client.post("/api/alchemy/opt-out")
    assert r.status_code == 200
    state = AlchemyState.query.filter_by(user_id=uid).first()
    assert state.opted_in_at is None
    assert state.status_for_user == "offered"


def test_opt_in_rejects_unavailable_source(app, client):
    uid = User.query.first().id
    _mk_source(slug="empty-source", with_chunks=False)
    _mk_state(uid, readiness="ready")
    r = client.post("/api/alchemy/opt-in",
                    json={"source_slug": "empty-source",
                          "accept_risks": True})
    assert r.status_code == 400


def test_start_requires_active_state(app, client):
    uid = User.query.first().id
    _mk_source()
    _mk_state(uid, readiness="ready")  # offered, NOT opted in
    r = client.post("/api/alchemy/start", json={"content": "hello"})
    assert r.status_code == 403


def test_start_creates_thread_with_hidden_prompt(app, client):
    uid = User.query.first().id
    _mk_source()
    _mk_state(uid, readiness="ready", opted_in=True,
              source_slug="meditationbook")
    r = client.post("/api/alchemy/start",
                    json={"content": "What's alive: restlessness."})
    assert r.status_code == 202
    body = r.get_json()
    system = Node.query.get(body["conversation_id"])
    assert system.privacy_level == "private"
    pin = NodeContextArtifact.query.filter_by(
        node_id=system.id, artifact_type="prompt").first()
    assert pin is not None
    from backend.models import UserPrompt
    prompt_row = UserPrompt.query.get(pin.artifact_id)
    assert prompt_row.prompt_key == "alchemy"
    assert "ALCHEMY session" in prompt_row.get_content()


def test_routes_404_when_flag_off():
    _affected = lambda k: (  # noqa: E731
        k == "flask_login" or k.startswith("backend.routes")
        or k == "backend.models")
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules)
               if k.startswith("backend.routes")]:
        del sys.modules[_k]
    app = _make_app(alchemy_v1=False)
    with app.app_context():
        _db.create_all()
        user = User(username="tester")
        _db.session.add(user)
        _db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        assert client.get("/api/alchemy/status").status_code == 404
        assert client.get("/api/alchemy/sources").status_code == 404
        assert client.post("/api/alchemy/start",
                           json={"content": "x"}).status_code == 404
        _db.session.remove()
        _db.drop_all()
    for k in [k for k in list(sys.modules) if _affected(k)]:
        del sys.modules[k]
    sys.modules.update(saved)


# ── Hidden prompt ────────────────────────────────────────────────────────

def test_alchemy_prompt_hidden_from_prompts_api(app, client):
    listed = client.get("/api/prompts/").get_json()["prompts"]
    assert all(p["prompt_key"] != "alchemy" for p in listed)
    assert client.get("/api/prompts/alchemy").status_code == 404
    assert client.put("/api/prompts/alchemy",
                      json={"content": "edited gate"}).status_code == 404


# ── Readiness verdict parsing (fails closed) ─────────────────────────────

def test_parse_verdict_variants():
    # Import against stub glue (the module imports backend.celery_app).
    glue = ("backend.celery_app", "backend.llm_providers",
            "backend.tasks.alchemy_readiness")
    saved = {k: sys.modules.get(k) for k in glue}
    sys.modules["backend.celery_app"] = MagicMock()
    sys.modules["backend.llm_providers"] = MagicMock()
    sys.modules.pop("backend.tasks.alchemy_readiness", None)
    from backend.tasks.alchemy_readiness import _parse_verdict
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v

    ok = _parse_verdict(
        '{"ready": true, "rationale": "Stable.", "flags": []}')
    assert ok["ready"] is True

    fenced = _parse_verdict(
        '```json\n{"ready": false, "rationale": "Thin.", '
        '"flags": ["insufficient-data"]}\n```')
    assert fenced["ready"] is False
    assert "insufficient-data" in fenced["flags"]

    garbage = _parse_verdict("I think the user seems fine, probably?")
    assert garbage["ready"] is False  # the gate fails closed
    assert "unparseable-verdict" in garbage["flags"]


# ── read_source retrieval ────────────────────────────────────────────────

def test_search_source_chunks_ranks(app, monkeypatch):
    _mk_source()
    from backend.utils import alchemy_sources as als
    monkeypatch.setattr(als, "embed_texts",
                        lambda texts, key, **kw: [[1.0, 0.0]])
    source = AlchemySource.query.first()
    matches = als.search_source_chunks(source.id, "sitting", "fake-key")
    assert len(matches) == 2
    top_chunk = AlchemySourceChunk.query.get(matches[0][0])
    assert top_chunk.heading == "On sitting"
