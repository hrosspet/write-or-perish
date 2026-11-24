"""A reply's run starts by setting the account's spend block from the
spend recorded so far (utils/spend.refresh_user_cap).

Replies of one account queued together reach the worker one after
another; the block the deferred check sets (the cost row's listener)
waits in the same queue behind them. So the run itself sums what the
earlier replies recorded: once they reach the limit, the replies still
queued stop before any model call.

Runs the real task body through the test_retrieval_loop harness (identity
@celery.task, scripted provider, test flask_app); nothing reaches a real
model, broker or mail server.
"""
import sys
import types
from datetime import datetime

import pytest

from backend.tests.test_retrieval_loop import (  # noqa: F401 (fixture)
    app, generate_llm_response, _FakeSelf, _ScriptedProvider, _build_chain,
    _resp, _fresh,
)
from backend.extensions import db as _db
from backend.models import APICostLog, User
# Registers the cost row's listener, as create_app() does in the app.
import backend.utils.spend  # noqa: F401,E402

LIMIT_USD = 50.0


@pytest.fixture
def limit(app, monkeypatch):  # noqa: F811
    """A $50 monthly limit per account; the deferred check and the admin
    mail recorded instead of sent."""
    deferred, mailed = [], []
    monkeypatch.setitem(app.config, "PER_USER_MONTHLY_LIMIT_USD", LIMIT_USD)
    monkeypatch.setitem(
        sys.modules, "backend.tasks.spend_monitor",
        types.SimpleNamespace(enforce_user_cap=types.SimpleNamespace(
            apply_async=lambda **k: deferred.append(k))))
    import backend.utils.email as email_mod
    monkeypatch.setattr(email_mod, "send_user_spend_block_email",
                        lambda **k: mailed.append(k))
    return types.SimpleNamespace(deferred=deferred, mailed=mailed)


def _spent(user_id, usd):
    """Cost rows of replies that already ran, written the way the task
    writes them (the listener then queues the deferred check)."""
    _db.session.add(APICostLog(
        user_id=user_id, model_id="gpt-5", request_type="conversation",
        input_tokens=1, output_tokens=1,
        cost_microdollars=int(usd * 1_000_000)))
    _db.session.commit()


def _blocked_month(user_id):
    _db.session.expire_all()
    return _db.session.get(User, user_id).spend_blocked_month


def test_a_queued_reply_stops_once_earlier_replies_reach_the_limit(
        app, limit):  # noqa: F811
    alice, system, user_node, llm_node = _build_chain("textmode")
    _spent(alice.id, 60)
    # The deferred check is queued, not run: the account is not blocked.
    assert len(limit.deferred) == 1
    assert _blocked_month(alice.id) is None
    _ScriptedProvider.reset([_resp("must not be sent")])

    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert _ScriptedProvider.calls == []
    assert _fresh(llm_node.id).llm_task_status == "failed"
    assert _blocked_month(alice.id) == datetime.utcnow().strftime("%Y-%m")
    assert len(limit.mailed) == 1


def test_a_queued_reply_under_the_limit_runs(app, limit):  # noqa: F811
    alice, system, user_node, llm_node = _build_chain("textmode")
    _spent(alice.id, 10)
    _ScriptedProvider.reset([_resp("An answer.")])

    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert len(_ScriptedProvider.calls) == 1
    node = _fresh(llm_node.id)
    assert node.llm_task_status == "completed"
    assert node.get_content() == "An answer."
    assert _blocked_month(alice.id) is None
    assert limit.mailed == []


def test_without_a_per_user_limit_the_run_sums_nothing(app, monkeypatch):  # noqa: F811
    from backend.utils import spend
    alice, system, user_node, llm_node = _build_chain("textmode")
    summed = []
    monkeypatch.setattr(spend, "enforce_user_spend_cap",
                        lambda *a, **k: summed.append(a))
    _ScriptedProvider.reset([_resp("An answer.")])

    generate_llm_response(_FakeSelf(), user_node.id, llm_node.id, "gpt-5",
                          alice.id, source_mode="textmode")

    assert summed == []
    assert len(_ScriptedProvider.calls) == 1
