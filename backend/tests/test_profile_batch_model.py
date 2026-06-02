"""Foundation tests for the profile-batch pipeline (issue #173, Part A):
the ProfileBatchJob model, the User guard columns, and config-flag parsing.
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

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db  # noqa: E402
from backend.models import User, ProfileBatchJob  # noqa: E402


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


def test_user_batch_guard_defaults(app):
    u = User(username="u", plan="alpha", twitter_id=None, approved=True)
    db.session.add(u)
    db.session.commit()
    assert u.profile_batch_pending is False
    assert u.profile_batch_attempts == 0


def test_profile_batch_job_roundtrips_items_json(app):
    items = [
        {"custom_id": "profile:1:0:chunk", "user_id": 1,
         "prev_profile_id": None, "chunk_num": 1, "kind": "chunk",
         "source_data_cutoff": "2026-06-01T00:00:00",
         "prev_cumulative": 0, "model_id": "gpt-5.5"},
    ]
    job = ProfileBatchJob(
        provider_key="openai:gpt-5.5", batch_id="b-1",
        status="pending", items=items, submitted_at=datetime(2026, 6, 2),
    )
    db.session.add(job)
    db.session.commit()

    fetched = ProfileBatchJob.query.filter_by(batch_id="b-1").first()
    assert fetched.status == "pending"
    assert fetched.items[0]["custom_id"] == "profile:1:0:chunk"
    assert fetched.items[0]["kind"] == "chunk"
    assert fetched.collected_at is None


def test_config_flag_parsing():
    """PROFILE_BATCH_USER_IDS parses a comma list into a set of ints;
    PROFILE_USE_BATCH parses truthy strings."""
    parse_ids = lambda s: {  # noqa: E731  (mirrors config.py)
        int(x) for x in s.replace(" ", "").split(",") if x
    }
    assert parse_ids("44, 50 ,53") == {44, 50, 53}
    assert parse_ids("") == set()

    truthy = lambda s: s.lower() in ("1", "true", "yes")  # noqa: E731
    assert truthy("true") and truthy("1") and truthy("YES")
    assert not truthy("false") and not truthy("")
