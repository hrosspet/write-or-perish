"""backend/scripts/restamp_artifact_ai_usage.py (#326): artifact rows the
old writers stamped 'chat' take their owner's default — 'train' owners by
default, 'none' owners only when asked — and nothing else moves."""
import importlib.util
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

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db  # noqa: E402
from backend.models import User, UserArtifact  # noqa: E402


def _load_script():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "scripts", "restamp_artifact_ai_usage.py")
    spec = importlib.util.spec_from_file_location("_restamp_art", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _user(name, default):
    u = User(username=name, default_ai_usage=default)
    db.session.add(u)
    db.session.flush()
    return u


def _art(owner, kind, usage):
    a = UserArtifact(user_id=owner.id, kind=kind, title=kind,
                     generated_by="test", ai_usage=usage)
    a.set_content(f"{kind} of {owner.username}")
    db.session.add(a)
    db.session.flush()
    return a


def _world():
    trainer = _user("trainer", "train")
    chatter = _user("chatter", "chat")
    optout = _user("optout", "none")
    rows = {
        "trainer_old": _art(trainer, "memory", "chat"),
        "trainer_new": _art(trainer, "memory", "train"),
        "trainer_digest": _art(trainer, "external_digest", "chat"),
        "trainer_none": _art(trainer, "scratchpad", "none"),
        "chatter": _art(chatter, "memory", "chat"),
        "optout": _art(optout, "memory", "chat"),
    }
    db.session.commit()
    return {k: v.id for k, v in rows.items()}


def _usage(ids):
    db.session.expire_all()
    return {k: UserArtifact.query.get(v).ai_usage for k, v in ids.items()}


def test_dry_run_counts_and_writes_nothing(app):
    mod = _load_script()
    ids = _world()
    before = _usage(ids)

    assert mod.survey() == {"train": (2, 1), "chat": (1, 1), "none": (1, 1)}
    counts = mod.restamp(["train"], apply=False, batch_size=1)

    assert counts == {("chat", "train"): 2}
    assert _usage(ids) == before


def test_apply_restamps_only_the_chat_rows_of_train_owners(app):
    mod = _load_script()
    ids = _world()

    counts = mod.restamp(["train"], apply=True, batch_size=1)

    assert counts == {("chat", "train"): 2}
    assert _usage(ids) == {
        "trainer_old": "train",
        "trainer_new": "train",
        "trainer_digest": "train",
        "trainer_none": "none",       # a 'none' row is never widened
        "chatter": "chat",            # the default is 'chat': nothing to do
        "optout": "chat",             # 'none' owners only when asked
    }
    # Rerunnable: a second run finds nothing.
    assert mod.restamp(["train"], apply=True) == {}
    assert mod.survey() == {"chat": (1, 1), "none": (1, 1)}


def test_include_none_hides_the_opted_out_owners_rows(app):
    mod = _load_script()
    ids = _world()

    counts = mod.restamp(["train", "none"], apply=True)

    assert counts == {("chat", "train"): 2, ("chat", "none"): 1}
    assert _usage(ids)["optout"] == "none"
    assert _usage(ids)["chatter"] == "chat"


def test_main_dry_run_prints_the_plan(app, monkeypatch, capsys):
    mod = _load_script()
    _world()
    monkeypatch.setattr("backend.create_app", lambda: app, raising=False)

    assert mod.main([]) == 0

    out = capsys.readouterr().out
    assert "owner default 'train': 2 row(s), 1 owner(s)" in out
    assert "would restamp 'chat' -> 'train': 2 row(s)" in out
    assert "--include-none" in out
    assert "dry run" in out
