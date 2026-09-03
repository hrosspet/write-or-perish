"""backend/scripts/replan_tail.py — where a pre-filled account's chain is
re-tipped so the planner folds its unread tail in with even weight."""
import os
import sys
from datetime import datetime, timedelta
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

from backend.extensions import db as _db              # noqa: E402
from backend.models import User, UserProfile, Node    # noqa: E402
from backend.scripts import replan_tail as rt         # noqa: E402

T = 90_000


@pytest.fixture
def app():
    import backend.celery_app  # noqa: F401
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SUPPORTED_MODELS"] = {}
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def test_choose_branch_takes_the_latest_full_chunk_version():
    # The exgenesis shape under the old sizing: 9 versions, a 44k tail.
    tip_first = [("v9", 44_336), ("v8", 115_602), ("v7", 187_130), ("v6", 258_658)]
    version, k, size = rt.choose_branch(tip_first, target=T)
    assert version == "v8"          # 44k is under 0.75 T; 115.6k plans into one full chunk
    assert (k, size) == (1, 115_602)
    # A tail that already plans into a full chunk keeps the tip.
    assert rt.choose_branch([("v9", 70_000), ("v8", 140_000)], target=T)[0] == "v9"
    # A remainder that plans into several chunks qualifies by the band floor.
    version, k, size = rt.choose_branch([("v9", 20_000), ("v8", 160_000)], target=T)
    assert (version, k) == ("v8", 2) and size == 80_000
    # Nothing qualifies: the whole corpus is below 0.75 T.
    assert rt.choose_branch([("v2", 10_000), ("v1", 50_000)], target=T) is None


def _chain(user, cutoffs):
    """Chronological chain of iterative/update versions ending in an
    integration, one day apart; returns the non-integration versions."""
    versions, parent = [], None
    for i, cutoff in enumerate(cutoffs):
        p = UserProfile(user_id=user.id, generated_by="m", tokens_used=0,
                        generation_type="iterative" if i == 0 else "update",
                        source_tokens_used=(i + 1) * 60_000,
                        source_data_cutoff=cutoff,
                        parent_profile_id=parent.id if parent else None,
                        created_at=datetime(2026, 8, 1 + i))
        p.set_content(f"V{i}")
        _db.session.add(p)
        _db.session.flush()
        versions.append(p)
        parent = p
    integ = UserProfile(user_id=user.id, generated_by="m", tokens_used=0,
                        generation_type="integration",
                        source_tokens_used=parent.source_tokens_used,
                        source_data_cutoff=parent.source_data_cutoff,
                        parent_profile_id=parent.id,
                        created_at=datetime(2026, 8, 1 + len(cutoffs)))
    integ.set_content("I")
    _db.session.add(integ)
    _db.session.commit()
    return versions


def _tweets(user, start, days, units_per_day):
    for d in range(days):
        n = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                 ai_usage="chat", origin="twitter", token_count=units_per_day,
                 created_at=start + timedelta(days=d))
        n.set_content("t")
        _db.session.add(n)
    _db.session.commit()


def test_plan_and_apply_re_tips_the_chain(app, monkeypatch):
    u = User(username="xiq", plan="alpha", twitter_id=None, approved=True,
             prefilled_handle="xiq", profile_force_batch=True)
    _db.session.add(u)
    _db.session.commit()
    # 340 days × 1,000 units; the chain covered the first 300 days in three
    # 100-day chunks (cutoffs at days 100/200/300), so 40 days — 40k units,
    # under 0.75 T — remain unread: the deferred tail.
    _tweets(u, datetime(2025, 1, 1), 340, 1000)
    v = _chain(u, [datetime(2025, 1, 1) + timedelta(days=d) for d in (99, 199, 299)])

    plan = rt.plan_repair(u)
    assert plan["tip"].id == v[2].id
    assert plan["tail"] == 40_000
    assert plan["status"] == "branch"
    version, k, size = plan["choice"]
    assert version.id == v[1].id                       # 140k after v1's cutoff → 2 × 70k, in the band
    assert (k, size) == (2, 70_000)
    assert [s.id for s in plan["superseded"]] == [v[2].id]

    copy = rt.apply_branch(u, version)
    assert copy.generation_type == "revert" and copy.parent_profile_id == v[1].id
    assert copy.source_data_cutoff == v[1].source_data_cutoff
    assert copy.get_content() == "V1"

    # The copy is now the chain tip: the continue rule fires (the tail is
    # older than the copy), the chain walks copy → v1 → v0, and a second
    # run of the script finds nothing left to write.
    from backend.tasks.profile_batch import _latest_non_integration_profile
    from backend.tasks.exports import should_continue_chain, _collect_iterative_chain
    assert _latest_non_integration_profile(u.id).id == copy.id
    assert should_continue_chain(u, copy) is True
    assert [p.id for p in _collect_iterative_chain(copy.id)] == [v[0].id, v[1].id, copy.id]
    again = rt.plan_repair(u)
    assert again["status"].startswith("tail already plans into full chunks")


def test_plan_repair_reports_complete_and_missing_chains(app):
    u = User(username="fresh", plan="alpha", twitter_id=None, approved=True)
    _db.session.add(u)
    _db.session.commit()
    assert rt.plan_repair(u) == {"status": "no profile"}
    _tweets(u, datetime(2025, 1, 1), 10, 1000)
    _chain(u, [datetime(2025, 1, 10)])
    assert rt.plan_repair(u)["status"].startswith("complete")
