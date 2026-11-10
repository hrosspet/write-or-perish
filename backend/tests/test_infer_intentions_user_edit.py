"""#412: the admin "Infer intentions" route refuses to replace intentions
the user wrote, restored or had updated in a conversation, unless the
admin confirmed replacing that exact version as it was when asked; the
refusal names the version, who made it and when it last changed, never
its text. The Users-tab column shows a run whose result was dropped for
the user's version as "kept"."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from backend.tests.test_prefill_consent import app, users, _login, _db  # noqa: F401
from backend.models import APICostLog, ProfileBatchJob, UserArtifact

SECRET = "my private intention text"


def _version(user, generated_by, tokens_used=0, created_at=None):
    a = UserArtifact(user_id=user.id, kind="intentions", title="Intentions",
                     generated_by=generated_by, tokens_used=tokens_used)
    if created_at:
        a.created_at = created_at
    a.set_content(SECRET)
    _db.session.add(a)
    _db.session.commit()
    return a


@pytest.fixture
def task(monkeypatch):
    import backend.tasks.intentions as it
    t = MagicMock()
    t.delay.return_value.id = "task-1"
    monkeypatch.setattr(it, "infer_intentions", t)
    cancels = []
    monkeypatch.setattr(it, "cancel_pending_intentions", lambda uid: (
        cancels.append(uid) or {"cancelled": 1, "errors": []}))
    t.cancels = cancels
    return t


def _post(app, users, body):  # noqa: F811
    client = app.test_client()
    _login(client, users["admin"].id)
    return client.post(f"/api/admin/users/{users['tw'].id}/infer_intentions",
                       json=body)


@pytest.mark.parametrize("author,by", [
    ("user", "user"), ("revert", "revert"), ("claude-opus-5.5", "conversation")])
def test_a_user_version_is_refused_with_its_metadata_only(app, users, task, author, by):  # noqa: F811
    mine = _version(users["tw"], author)
    resp = _post(app, users, {"mode": "sync", "cancel_pending": True})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["code"] == "intentions_edited"
    assert body["version_id"] == mine.id and body["changed_by"] == by
    assert body["changed_at"].startswith(f"{mine.created_at:%Y-%m-%d}")
    assert SECRET not in resp.get_data(as_text=True)    # never the text
    assert not task.delay.called
    assert task.cancels == []                           # a refused click cancels nothing


def _confirm(asked):
    """The body the dialog's "Replace their version" sends for a 409."""
    return {"replace_version_id": asked["version_id"],
            "replace_changed_at": asked["changed_at"]}


def test_confirming_that_version_runs_and_passes_it_on(app, users, task):  # noqa: F811
    from backend.utils.intentions_versions import confirmation
    mine = _version(users["tw"], "user")
    asked = _post(app, users, {}).get_json()
    resp = _post(app, users, {"mode": "batch", **_confirm(asked)})
    assert resp.status_code == 202
    task.delay.assert_called_once_with(users["tw"].id, "batch", confirmation(mine))


def test_a_stale_confirmation_asks_again_for_the_newer_version(app, users, task):  # noqa: F811
    old = _version(users["tw"], "user", created_at=datetime.utcnow() - timedelta(days=2))
    stale = _post(app, users, {}).get_json()
    newer = _version(users["tw"], "user")
    fresh = _post(app, users, {}).get_json()
    for body in (_confirm(stale),
                 {**_confirm(fresh), "replace_version_id": True},
                 {**_confirm(fresh), "replace_version_id": str(newer.id)},
                 {**_confirm(fresh), "replace_changed_at": stale["changed_at"]},
                 {"replace_version_id": newer.id},
                 {}):
        resp = _post(app, users, body)
        assert resp.status_code == 409, body
        assert resp.get_json()["version_id"] == newer.id
    assert old.id != newer.id and not task.delay.called


def test_an_in_place_edit_after_the_confirmation_is_kept(app, users, task, monkeypatch):  # noqa: F811
    """The review's sequence: the user's version X is current; the admin
    confirms replacing it; while the batch is out, the user ticks a box,
    which edits X in place (same id); the result arrives and must not
    replace X. The next 409 shows the tick as the last change."""
    import backend.tasks.intentions as it
    from backend.routes.artifacts import artifacts_bp
    from backend.utils.intentions_versions import KEPT_USER_EDIT_REF
    app.register_blueprint(artifacts_bp, url_prefix="/api/artifacts")
    app.config["SUPPORTED_MODELS"] = {"claude-opus-4.8": {
        "provider": "anthropic", "api_model": "claude-opus-4-8",
        "input_price_per_mtok": 5.0, "output_price_per_mtok": 25.0}}
    owner = users["tw"]
    x = _version(owner, "user", created_at=datetime.utcnow() - timedelta(days=1))
    asked = _post(app, users, {}).get_json()
    assert _post(app, users, {"mode": "batch", **_confirm(asked)}).status_code == 202
    confirmed = task.delay.call_args.args[2]
    item = {"custom_id": f"int-u{owner.id}", "user_id": owner.id,
            "kind": "intentions", "budget": 1, "resubmitted": False,
            "confirmed": confirmed}

    from flask import g
    g.pop("_login_user", None)     # Flask-Login caches the admin on g
    client = app.test_client()
    _login(client, owner.id)
    resp = client.patch("/api/artifacts/intentions",
                        json={"content": "- [x] my private intention"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["artifact"]["id"] == x.id
    g.pop("_login_user", None)

    saved = it.apply_intentions_item(owner, item, {
        "content": "# Endorsed\ninferred", "input_tokens": 10, "output_tokens": 1})
    assert saved["version"] is None and saved["kept_user_edit"] is True
    current = UserArtifact.latest_for(owner.id, "intentions")
    assert current.id == x.id
    assert current.get_content() == "- [x] my private intention"
    assert APICostLog.query.filter_by(user_id=owner.id).one().request_ref == KEPT_USER_EDIT_REF

    again = _post(app, users, _confirm(asked))      # the old confirmation
    assert again.status_code == 409
    assert again.get_json()["changed_at"] > asked["changed_at"]
    assert again.get_json()["changed_at"].startswith(f"{datetime.utcnow():%Y-%m-%d}")


@pytest.mark.parametrize("inferred", [False, True])
def test_no_dialog_without_a_user_version(app, users, task, inferred):  # noqa: F811
    """No intentions yet, or the current version is an earlier inference
    run's: the run starts without asking."""
    if inferred:
        _version(users["tw"], "user", created_at=datetime.utcnow() - timedelta(days=2))
        _version(users["tw"], "claude-opus-4.8", tokens_used=120_000)
    resp = _post(app, users, {"mode": "batch"})
    assert resp.status_code == 202
    task.delay.assert_called_once_with(users["tw"].id, "batch", None)


def test_the_column_shows_a_run_dropped_for_the_users_version(app, users):  # noqa: F811
    """Sync or batch with a result: a cost row marked kept. A failed batch
    not resubmitted: the item is marked. A newer version ends the state."""
    from backend.models import User
    from backend.utils.intentions_versions import KEPT_USER_EDIT_REF
    now = datetime.utcnow()
    kept, kept_item = users["tw"], users["mail"]
    for u in (kept, kept_item):
        _version(u, "user", created_at=now - timedelta(hours=2))
    _db.session.add(APICostLog(
        user_id=kept.id, model_id="m", request_type="intentions_infer",
        request_ref=KEPT_USER_EDIT_REF, input_tokens=1, output_tokens=1,
        cost_microdollars=1, created_at=now - timedelta(minutes=5)))
    _db.session.add(ProfileBatchJob(
        provider_key="anthropic", batch_id="b-k", status="collected",
        items=[{"custom_id": f"int-u{kept_item.id}", "user_id": kept_item.id,
                "kind": "intentions", "kept_user_edit": True}],
        submitted_at=now - timedelta(hours=3), collected_at=now - timedelta(minutes=5)))
    _db.session.commit()
    client = app.test_client()
    _login(client, users["admin"].id)

    def states():
        rows = client.get("/api/admin/users").get_json()["users"]
        return {r["username"]: (r["intentions"] or {}).get("state") for r in rows}
    assert states()[kept.username] == "kept"
    assert states()[kept_item.username] == "kept"
    _version(User.query.get(kept.id), "user")
    assert states()[kept.username] == "complete"
