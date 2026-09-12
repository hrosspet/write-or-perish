"""Tests for the nightly external-references digest rebuild.

The digest is ONE LLM call over the user's whole saved corpus, so it must
fire once a night per user — not once per saved reference. Riding
individual saves cost $26 in a single day of clipping (30 rebuilds x
~77k input tokens on a flagship model), which is what this gate prevents.

Covers: the staleness definition, the not-stale skip, the corpus-state
timestamp, and the nightly fan-out (own-night gate, one dispatch however
many references arrived). LLM + celery glue stubbed.
"""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")

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
    APICostLog, ExternalItem, User, UserArtifact,
)


def _make_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["ANTHROPIC_API_KEY"] = "fake-key"
    app.config["DEFAULT_LLM_MODEL"] = "claude-opus-5"
    app.config["SUPPORTED_MODELS"] = {
        "claude-opus-5": {
            "provider": "anthropic",
            "input_price_per_mtok": 5.0,
            "output_price_per_mtok": 25.0,
        },
    }
    _db.init_app(app)
    return app


# Import the REAL digest module against an identity-decorator celery stub
# and our test flask_app (mirrors test_bookmark_nightly_sync.py).
_app = _make_app()
_celery_stub = MagicMock()
_celery_stub.celery.task = lambda *a, **k: (lambda fn: fn)
_celery_stub.flask_app = _app


def _import_real_digest_module():
    import importlib
    glue = ("backend.celery_app", "backend.tasks.external_digest")
    saved = {k: sys.modules.get(k) for k in glue}
    sys.modules["backend.celery_app"] = _celery_stub
    sys.modules.pop("backend.tasks.external_digest", None)
    try:
        mod = importlib.import_module("backend.tasks.external_digest")
        if isinstance(mod, MagicMock):
            sys.modules.pop("backend.tasks.external_digest", None)
            mod = importlib.import_module("backend.tasks.external_digest")
        return mod
    finally:
        for _k, _v in saved.items():
            if _v is None:
                sys.modules.pop(_k, None)
            else:
                sys.modules[_k] = _v


_digest = _import_real_digest_module()
assert not isinstance(_digest, MagicMock)


class _FakeSelf:
    def update_state(self, *args, **kwargs):
        pass

    def retry(self, exc=None, **kwargs):
        raise exc or AssertionError("retry")


@pytest.fixture
def app():
    saved_flask_app = _digest.flask_app
    _digest.flask_app = _app
    with _app.app_context():
        _db.create_all()
        user = User(username="tester")
        _db.session.add(user)
        _db.session.commit()
        yield _app
        _db.session.remove()
        _db.drop_all()
    _digest.flask_app = saved_flask_app


def _tz_at_hour(target_hour):
    """An IANA zone whose CURRENT local hour == target_hour, built from the
    fixed-offset Etc/GMT zones (POSIX-inverted signs: Etc/GMT+5 = UTC-5)."""
    x = (datetime.utcnow().hour - target_hour) % 24
    return f"Etc/GMT+{x}" if x <= 12 else f"Etc/GMT-{24 - x}"


def _mk_item(user_id, external_id="1", fetched_at=None):
    item = ExternalItem(
        user_id=user_id, source="web_clip", external_id=external_id,
        url=f"https://example.com/{external_id}", title="A page",
        fetched_at=fetched_at or datetime.utcnow(),
    )
    item.set_content("something worth returning to")
    _db.session.add(item)
    _db.session.commit()
    return item


def _mk_digest(user_id, created_at=None):
    artifact = UserArtifact(
        user_id=user_id, kind=_digest.DIGEST_KIND,
        title=_digest.DIGEST_TITLE, generated_by="claude-opus-5",
        created_at=created_at or datetime.utcnow(),
    )
    artifact.set_content("# Topics")
    _db.session.add(artifact)
    _db.session.commit()
    return artifact


def _stub_llm(monkeypatch, on_call=None):
    calls = []

    def fake_completion(model_id, messages, api_keys, **kwargs):
        calls.append({"model_id": model_id, "messages": messages})
        if on_call is not None:
            on_call()
        return {"content": "# Topics\n- one", "input_tokens": 100,
                "output_tokens": 20}
    monkeypatch.setattr(_digest.LLMProvider, "get_completion",
                        staticmethod(fake_completion))
    return calls


def test_digest_is_stale_only_while_the_corpus_is_ahead(app):
    uid = User.query.first().id
    assert _digest.digest_is_stale(uid) is False  # nothing saved yet

    _mk_item(uid, "a", fetched_at=datetime.utcnow() - timedelta(hours=2))
    assert _digest.digest_is_stale(uid) is True  # never digested

    _mk_digest(uid, created_at=datetime.utcnow() - timedelta(hours=1))
    assert _digest.digest_is_stale(uid) is False

    _mk_item(uid, "b")  # a fresh clip
    assert _digest.digest_is_stale(uid) is True


def test_rebuild_skips_a_current_digest_without_calling_the_model(
        app, monkeypatch):
    """The expensive call is the whole point of the gate: a re-dispatch
    for an unchanged corpus must cost nothing."""
    uid = User.query.first().id
    _mk_item(uid, "a", fetched_at=datetime.utcnow() - timedelta(hours=2))
    _mk_digest(uid, created_at=datetime.utcnow() - timedelta(hours=1))
    calls = _stub_llm(monkeypatch)

    assert _digest.rebuild_external_digest(
        _FakeSelf(), uid) == {"status": "not_stale"}
    assert calls == []
    assert APICostLog.query.count() == 0

    # force=True is the escape hatch for a manual rebuild.
    result = _digest.rebuild_external_digest(_FakeSelf(), uid, force=True)
    assert result["status"] == "ok"
    assert len(calls) == 1


def test_rebuild_uses_the_configured_default_model(app, monkeypatch):
    """Users without a preferred_model fall back to DEFAULT_LLM_MODEL —
    the config key. (LLM_NAME is only the env var it reads from, and
    asking config for it yielded None, i.e. no model at all.)"""
    uid = User.query.first().id
    _mk_item(uid, "a")
    calls = _stub_llm(monkeypatch)

    result = _digest.rebuild_external_digest(_FakeSelf(), uid)
    assert result["status"] == "ok"
    assert calls[0]["model_id"] == "claude-opus-5"
    artifact = UserArtifact.latest_for(uid, _digest.DIGEST_KIND)
    assert artifact.generated_by == "claude-opus-5"
    assert APICostLog.query.one().request_type == "external_digest"


def test_rebuild_stamps_the_corpus_state_not_the_finish_time(
        app, monkeypatch):
    """An item saved DURING a rebuild isn't in that digest, so it must
    still read as stale afterwards — otherwise it is never digested."""
    uid = User.query.first().id
    _mk_item(uid, "a")
    _stub_llm(monkeypatch, on_call=lambda: _mk_item(uid, "mid-flight"))

    assert _digest.rebuild_external_digest(_FakeSelf(), uid)["status"] == "ok"
    assert _digest.digest_is_stale(uid) is True


def test_nightly_sweep_dispatches_once_per_night_in_the_users_own_night(
        app, monkeypatch):
    """However many references arrived during the day, the user's corpus
    is digested once — and only while it is night where they are."""
    night_tz = _tz_at_hour(_digest.NIGHTLY_DIGEST_LOCAL_HOUR)
    day_tz = _tz_at_hour((_digest.NIGHTLY_DIGEST_LOCAL_HOUR + 12) % 24)

    night_user = User.query.first()
    night_user.timezone = night_tz
    day_user = User(username="daytime", timezone=day_tz)
    current_user_ = User(username="unchanged", timezone=night_tz)
    quiet_user = User(username="no-references", timezone=night_tz)
    _db.session.add_all([day_user, current_user_, quiet_user])
    _db.session.commit()

    for i in range(20):  # a day of clipping
        _mk_item(night_user.id, f"clip-{i}")
    _mk_item(day_user.id, "d1")
    _mk_item(current_user_.id, "c1",
             fetched_at=datetime.utcnow() - timedelta(hours=3))
    _mk_digest(current_user_.id,
               created_at=datetime.utcnow() - timedelta(hours=2))

    dispatched = []
    fake_task = MagicMock()
    fake_task.apply_async = lambda args, countdown: dispatched.append(args[0])
    monkeypatch.setattr(_digest, "rebuild_external_digest", fake_task)

    result = _digest.sweep_external_digests()
    assert result == {"status": "ok", "dispatched": 1}
    assert dispatched == [night_user.id]
