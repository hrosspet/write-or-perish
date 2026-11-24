"""The recent-context check submits what it built when its time runs out.

The 10-minute check builds one request per due account and submits them
together at the end. When the task's soft time limit is reached while
one account is being built, the requests built before it are submitted
and the task then stops (the soft limit is raised again), so the accounts
built earlier keep their request and the accounts after it are left for
the next check. Any other failure in one account's build still leaves
that account out and the pass goes on. Harness:
tests/test_recent_context_batch.py (sqlite in memory, provider faked).
"""
import pytest

from backend.tests.test_recent_context_batch import (  # noqa: F401
    app, rc, world, lock_redis, _user, _check,
)
from backend.models import RecentContextBatchJob
from backend.utils.soft_limit import SoftTimeLimitExceeded


def _fail_build_for(module, monkeypatch, user_id, exc):
    real = module._build_batch_request
    built = []

    def build(user, profile, data_cutoff):
        if user.id == user_id:
            raise exc
        built.append(user.id)
        return real(user, profile, data_cutoff)
    monkeypatch.setattr(module, "_build_batch_request", build)
    return built


def _submitted_user_ids(state):
    return sorted(int(r["custom_id"].rsplit("_", 1)[1])
                  for call in state["submitted"]
                  for reqs in call.values() for r in reqs)


def test_the_time_limit_submits_the_requests_built_before_it(
        app, rc, world, monkeypatch):  # noqa: F811
    first = _user("first")
    slow = _user("slow")
    _user("after")
    built = _fail_build_for(rc, monkeypatch, slow.id,
                            SoftTimeLimitExceeded())

    with pytest.raises(SoftTimeLimitExceeded):
        _check(rc)
    assert built == [first.id]
    assert _submitted_user_ids(world) == [first.id]
    jobs = RecentContextBatchJob.query.all()
    assert [i["user_id"] for j in jobs for i in j.items] == [first.id]


def test_another_failure_leaves_one_account_out_and_goes_on(
        app, rc, world, monkeypatch):  # noqa: F811
    first = _user("first")
    broken = _user("broken")
    last = _user("last")
    _fail_build_for(rc, monkeypatch, broken.id, RuntimeError("build failed"))

    assert _check(rc) == {"status": "ok", "submitted": 2}
    assert _submitted_user_ids(world) == sorted([first.id, last.id])
