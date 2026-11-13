"""Anthropic Message Batches are deleted once Loore no longer needs them
(#693): the bookkeeping (backend/utils/anthropic_batch_deletion.py), the
hourly sweep, what the collect helper names, and the admin script for
the batches from before (backend/scripts/delete_anthropic_batches.py).

The Anthropic SDK is faked (tests/anthropic_batch_fake.py); sqlite in
memory; no provider is called. The collectors' own tests (that each one
deletes only after its commit) are in their files: test_profile_batch,
test_intentions_task, test_updates (poll drafts),
test_external_digest_nightly, test_recent_context_batch,
test_read_batch_poll, test_user_purge.
"""
import os
import sys
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")

sys.modules.setdefault("celery", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import backend.llm_providers  # noqa: E402,F401 - before the SDK is faked
from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    AnthropicBatchDeletion, ExternalDigestBatchJob, Node, PollDraftBatchJob,
    ProfileBatchJob, RecentContextBatchJob, User,
)
from backend.scripts import delete_anthropic_batches as script  # noqa: E402
from backend.tests import anthropic_batch_fake as fake  # noqa: E402
from backend.utils import anthropic_batch_deletion as abd  # noqa: E402
from backend.utils.llm_batch import (  # noqa: E402
    BatchItemFailed, BatchItemGone, CollectedResults,
    anthropic_batch_collect_one, batch_check_and_collect,
)

KEYS = {"anthropic": "k-ant", "openai": "k-oai"}


def _make_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["ANTHROPIC_API_KEY"] = "k-ant"
    _db.init_app(app)
    return app


_app = _make_app()


@pytest.fixture
def app():
    with _app.app_context():
        _db.create_all()
        yield _app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def anthropic(monkeypatch):
    return fake.install(monkeypatch)


def _job(model, batch_id, status="pending", provider_key="anthropic"):
    job = model(provider_key=provider_key, batch_id=batch_id,
                status=status, items=[])
    _db.session.add(job)
    _db.session.commit()
    return job


def _reply(status, entries=()):
    import json
    user = User.query.first()
    if user is None:
        user = User(username="alice")
        _db.session.add(user)
        _db.session.commit()
    node = Node(user_id=user.id, human_owner_id=user.id, node_type="llm",
                llm_model="claude-opus-5", llm_task_status=status,
                tool_calls_meta=json.dumps(list(entries)) if entries else None)
    node.set_content("")
    _db.session.add(node)
    _db.session.commit()
    return node


def _batch_entry(batch_id, status="submitted", provider="anthropic"):
    return {"name": "_batch", "batch_id": batch_id, "custom_id": "node-1",
            "provider": provider, "key_type": "chat", "status": status}


def _row(batch_id):
    _db.session.expire_all()
    return AnthropicBatchDeletion.query.filter_by(batch_id=batch_id).first()


# ── What the collect helper names (llm_batch) ───────────────────────────

class TestCollect:
    def test_an_ended_batch_is_named_and_nothing_is_deleted_yet(
            self, anthropic):
        anthropic.add("msgbatch_1", results=[fake.succeeded("a")])

        results, pending, _ = batch_check_and_collect(
            {"anthropic": "msgbatch_1"}, KEYS)

        assert pending == {}
        assert results["a"]["content"] == "ok"
        assert results.anthropic_batches == ["msgbatch_1"]
        assert anthropic.calls_of("delete") == []

    def test_a_running_batch_is_not_named(self, anthropic):
        anthropic.add("msgbatch_1", status="in_progress")

        results, pending, _ = batch_check_and_collect(
            {"anthropic": "msgbatch_1"}, KEYS)

        assert pending == {"anthropic": "msgbatch_1"}
        assert results.anthropic_batches == []

    def test_a_one_item_batch_already_deleted_is_a_verdict(self, anthropic):
        with pytest.raises(BatchItemGone) as caught:
            anthropic_batch_collect_one("k", "msgbatch_gone", "node-1")
        assert isinstance(caught.value, BatchItemFailed)


# ── Registration and "still needed" ─────────────────────────────────────

class TestRegister:
    def test_one_row_per_batch_and_only_for_anthropic(self, app):
        assert abd.register("anthropic", "msgbatch_1", "read", "train",
                            node_id=7) is not None
        _db.session.commit()
        abd.register("anthropic", "msgbatch_1", "profile_batch_job")
        assert abd.register("openai:gpt-x", "batch_2", "read") is None
        assert abd.register("anthropic", "unsubmitted",
                            "profile_batch_job") is None
        assert abd.register("anthropic", None, "read") is None
        _db.session.commit()

        row = AnthropicBatchDeletion.query.one()
        assert (row.batch_id, row.source, row.key_type, row.node_id) == (
            "msgbatch_1", "read", "train", 7)
        assert row.deleted_at is None

    @pytest.mark.parametrize("model", [
        ProfileBatchJob, PollDraftBatchJob, ExternalDigestBatchJob,
        RecentContextBatchJob])
    def test_a_job_needs_its_batch_while_it_is_pending(self, app, model):
        job = _job(model, "msgbatch_1")
        row = abd.register("anthropic", "msgbatch_1", model.__tablename__)
        _db.session.commit()
        assert abd.still_needed(row) is True

        job.status = "collected"
        _db.session.commit()
        assert abd.still_needed(row) is False

        _db.session.delete(job)   # a purge deletes a finished job
        _db.session.commit()
        assert abd.still_needed(row) is False

    def test_a_reply_needs_its_batch_while_it_is_at_work(self, app):
        node = _reply("processing", [_batch_entry("msgbatch_1")])
        row = abd.register("anthropic", "msgbatch_1", abd.READ_SOURCE,
                           node_id=node.id)
        _db.session.commit()
        assert abd.still_needed(row) is True

        for status in ("completed", "failed", "cancelled"):
            node.llm_task_status = status
            _db.session.commit()
            assert abd.still_needed(row) is False

        _db.session.delete(node)
        _db.session.commit()
        assert abd.still_needed(row) is False

    def test_a_purge_or_script_row_is_never_needed(self, app):
        for source in (abd.PURGE_SOURCE, abd.SCRIPT_SOURCE):
            row = abd.register("anthropic", f"msgbatch_{source}", source)
            assert abd.still_needed(row) is False


# ── Deleting right after the save ───────────────────────────────────────

class TestDeleteNow:
    def test_an_ended_batch_is_deleted_and_recorded(self, app, anthropic):
        anthropic.add("msgbatch_1")

        counts = abd.delete_now(["msgbatch_1"], "profile_batch_job")

        assert counts == {"deleted": 1}
        assert anthropic.calls_of("delete") == ["msgbatch_1"]
        assert anthropic.calls_of("retrieve") == []   # read as ended
        row = _row("msgbatch_1")
        assert row.deleted_at is not None and row.attempts == 0
        assert "msgbatch_1" not in anthropic.batches

    def test_a_batch_already_gone_counts_as_deleted(self, app, anthropic):
        counts = abd.delete_now(["msgbatch_gone"], "read")

        assert counts == {"gone": 1}
        assert _row("msgbatch_gone").deleted_at is not None

    def test_a_failed_delete_is_counted_and_left(self, app, anthropic):
        anthropic.add("msgbatch_1")
        anthropic.delete_error = RuntimeError("503")

        counts = abd.delete_now(["msgbatch_1"], "profile_batch_job")

        assert counts == {"failed": 1}
        row = _row("msgbatch_1")
        assert row.deleted_at is None
        assert (row.attempts, row.last_error) == (1, "RuntimeError")
        assert "msgbatch_1" in anthropic.batches

    def test_a_client_that_cannot_be_made_never_raises(
            self, app, monkeypatch):
        monkeypatch.setattr(abd, "_anthropic_client", MagicMock(
            side_effect=RuntimeError("boom")))
        assert abd.delete_now(["msgbatch_1"], "read") == {}

    def test_without_a_key_nothing_is_called(self, app, anthropic):
        app.config["ANTHROPIC_API_KEY"] = None
        try:
            counts = abd.delete_now(["msgbatch_1"], "read")
        finally:
            app.config["ANTHROPIC_API_KEY"] = "k-ant"
        assert counts == {"failed": 1}
        assert _row("msgbatch_1").last_error == "NoKey"
        assert anthropic.calls == []

    def test_collected_results_name_what_goes(self, app, anthropic):
        anthropic.add("msgbatch_1")
        results = CollectedResults()
        results.anthropic_batches.append("msgbatch_1")

        abd.delete_collected_batches(results, KEYS, "poll_draft_batch_job")
        # A stand-in's plain dict names nothing.
        abd.delete_collected_batches({"a": {}}, KEYS, "poll_draft_batch_job")

        assert anthropic.calls_of("delete") == ["msgbatch_1"]
        assert _row("msgbatch_1").source == "poll_draft_batch_job"


# ── The hourly sweep ────────────────────────────────────────────────────

class TestSweep:
    def test_a_failed_delete_is_retried_by_the_sweep(self, app, anthropic):
        _job(ProfileBatchJob, "msgbatch_1", status="collected")
        anthropic.add("msgbatch_1")
        anthropic.delete_error = RuntimeError("503")
        abd.delete_now(["msgbatch_1"], "profile_batch_job")
        anthropic.delete_error = None

        assert abd.sweep() == {"deleted": 1}
        assert _row("msgbatch_1").deleted_at is not None
        assert abd.sweep() == {}   # nothing left

    def test_a_crash_between_save_and_delete_is_caught(self, app, anthropic):
        """The row was written with the job at submit; the collector
        committed the job and died before its delete."""
        _job(RecentContextBatchJob, "msgbatch_1", status="collected")
        abd.register("anthropic", "msgbatch_1", "recent_context_batch_job")
        _db.session.commit()
        anthropic.add("msgbatch_1")

        assert abd.sweep() == {"deleted": 1}
        assert anthropic.calls_of("retrieve") == ["msgbatch_1"]

    def test_a_batch_still_needed_is_not_touched(self, app, anthropic):
        _job(ExternalDigestBatchJob, "msgbatch_1")   # pending
        node = _reply("processing", [_batch_entry("msgbatch_2")])
        abd.register("anthropic", "msgbatch_1", "external_digest_batch_job")
        abd.register("anthropic", "msgbatch_2", "read", node_id=node.id)
        _db.session.commit()
        anthropic.add("msgbatch_1", status="in_progress")
        anthropic.add("msgbatch_2")

        assert abd.sweep() == {"needed": 2}
        assert anthropic.calls == []
        assert _row("msgbatch_1").checked_at is not None

    def test_a_running_batch_is_cancelled_first_then_deleted(
            self, app, anthropic):
        """An abandoned or cancelled job's batch that still runs: never a
        delete while it processes (the API refuses it)."""
        _job(ExternalDigestBatchJob, "msgbatch_1", status="abandoned")
        abd.register("anthropic", "msgbatch_1", "external_digest_batch_job")
        _db.session.commit()
        anthropic.add("msgbatch_1", status="in_progress")

        assert abd.sweep() == {"cancelled": 1}
        assert anthropic.calls_of("cancel") == ["msgbatch_1"]
        assert anthropic.calls_of("delete") == []
        assert _row("msgbatch_1").cancel_requested_at is not None

        assert abd.sweep() == {"running": 1}   # canceling: wait
        assert anthropic.calls_of("cancel") == ["msgbatch_1"]
        assert anthropic.calls_of("delete") == []

        anthropic.batches["msgbatch_1"] = "ended"
        assert abd.sweep() == {"deleted": 1}
        assert anthropic.calls_of("delete") == ["msgbatch_1"]

    def test_a_batch_already_gone_counts_as_deleted(self, app, anthropic):
        node = _reply("completed", [_batch_entry("msgbatch_1", "ended")])
        abd.register("anthropic", "msgbatch_1", "read", node_id=node.id)
        _db.session.commit()

        assert abd.sweep() == {"gone": 1}
        assert _row("msgbatch_1").deleted_at is not None
        assert anthropic.calls_of("delete") == []

    def test_each_run_is_bounded_and_takes_the_oldest_looks_first(
            self, app, anthropic, monkeypatch):
        monkeypatch.setattr(abd, "SWEEP_LIMIT", 2)
        for n in range(3):
            _job(PollDraftBatchJob, f"msgbatch_{n}")   # pending: needed
            abd.register("anthropic", f"msgbatch_{n}",
                         "poll_draft_batch_job")
        _db.session.commit()

        assert abd.sweep(limit=abd.SWEEP_LIMIT) == {"needed": 2}
        assert _row("msgbatch_2").checked_at is None
        # The next run starts with the row not looked at yet.
        assert [r.batch_id for r in abd.due_rows(limit=1)] == ["msgbatch_2"]

    def test_the_sweep_task_runs_in_the_app(self, app, anthropic,
                                            monkeypatch):
        """backend.tasks.anthropic_batch_deletion: one sweep."""
        import importlib
        import backend.tasks as tasks_pkg
        name = "backend.tasks.anthropic_batch_deletion"
        stub = MagicMock()
        stub.celery.task = lambda *a, **k: (lambda fn: fn)
        stub.flask_app = app
        monkeypatch.setitem(sys.modules, "backend.celery_app", stub)
        # The real module and the package attribute come back afterwards.
        monkeypatch.delitem(sys.modules, name, raising=False)
        if hasattr(tasks_pkg, "anthropic_batch_deletion"):
            monkeypatch.setattr(tasks_pkg, "anthropic_batch_deletion",
                                tasks_pkg.anthropic_batch_deletion)
        task = importlib.import_module(name)
        monkeypatch.delitem(sys.modules, name)
        abd.register("anthropic", "msgbatch_1", abd.PURGE_SOURCE)
        _db.session.commit()
        anthropic.add("msgbatch_1")
        assert task.sweep_anthropic_batches() == {"deleted": 1}


# ── The admin script for the batches from before #693 ───────────────────

def _old_batches(anthropic):
    """Rows from before #693: nothing registered."""
    _job(ProfileBatchJob, "msgbatch_profile", status="collected")
    _job(ExternalDigestBatchJob, "msgbatch_digest", status="abandoned")
    _job(RecentContextBatchJob, "msgbatch_pending")                 # needed
    _job(ProfileBatchJob, "unsubmitted", status="failed")           # no batch
    _job(PollDraftBatchJob, "batch_oai", status="collected",
         provider_key="openai:gpt-x")                               # OpenAI
    _reply("completed", [_batch_entry("msgbatch_read_old", "cancelled"),
                         _batch_entry("msgbatch_read", "ended")])
    _reply("processing", [_batch_entry("msgbatch_read_live")])      # needed
    _reply("failed", [_batch_entry("batch_read_oai", provider="openai")])
    for batch_id in ("msgbatch_profile", "msgbatch_digest",
                     "msgbatch_pending", "msgbatch_read_old",
                     "msgbatch_read", "msgbatch_read_live"):
        anthropic.add(batch_id, status=(
            "in_progress" if batch_id in ("msgbatch_pending",
                                          "msgbatch_read_live")
            else "ended"))
    anthropic.add("msgbatch_other_env")   # not recorded here


class TestScript:
    def test_the_dry_run_counts_and_changes_nothing(self, app, anthropic):
        _old_batches(anthropic)
        lines = []

        assert script.run(out=lines.append) == {}

        text = "\n".join(lines)
        assert "not registered: 4 batch(es)" in text
        assert "external_digest_batch_job: 1" in text
        assert "profile_batch_job: 1" in text
        assert "read: 2" in text
        assert ("7 batch(es), 5 ended and 2 still processing. Of them: 4 "
                "found here and ended (to delete), 0 found here and still "
                "running (left to the hourly sweep), 2 still in use here, "
                "0 already registered, 1 not recorded here (never "
                "touched)") in text
        assert "Dry run" in text
        assert AnthropicBatchDeletion.query.count() == 0
        assert anthropic.calls_of("delete") == []
        assert anthropic.calls_of("retrieve") == []
        # Counts only: no batch id is printed.
        assert "msgbatch_" not in text

    def test_apply_deletes_what_loore_recorded_and_nothing_else(
            self, app, anthropic):
        _old_batches(anthropic)

        counts = script.run(apply=True, list_at_anthropic=False, pause=0,
                            out=lambda *_: None)

        assert counts == {"deleted": 4}
        assert sorted(anthropic.calls_of("delete")) == [
            "msgbatch_digest", "msgbatch_profile", "msgbatch_read",
            "msgbatch_read_old"]
        assert set(anthropic.batches) == {
            "msgbatch_pending", "msgbatch_read_live", "msgbatch_other_env"}
        assert anthropic.calls_of("cancel") == []

        # Idempotent: a second run finds nothing left.
        lines = []
        assert script.run(apply=True, list_at_anthropic=False, pause=0,
                          out=lines.append) == {}
        assert "not registered: 0 batch(es)" in lines[0]
        assert "Registered: 4 deleted, 0 waiting" in lines[1]

    def test_apply_leaves_a_running_batch_to_the_sweep(self, app, anthropic):
        _job(PollDraftBatchJob, "msgbatch_1", status="collected")
        anthropic.add("msgbatch_1", status="in_progress")

        counts = script.run(apply=True, list_at_anthropic=False, pause=0,
                            out=lambda *_: None)

        assert counts == {"running": 1}
        assert anthropic.calls_of("cancel") == []
        assert anthropic.calls_of("delete") == []
        assert _row("msgbatch_1").deleted_at is None
