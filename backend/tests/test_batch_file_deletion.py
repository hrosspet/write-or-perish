"""OpenAI batch files are deleted after use (#667).

A batch's requests are uploaded as a file, and OpenAI adds an output and
an error file. Once a batch has ended and its results are saved, the
three files are deleted; a failed delete is logged and left, never
failing the batch. The OpenAI SDK is faked at its import, as in
test_llm_batch.py.
"""
import json
import logging
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("SECRET_KEY", "test-secret")
sys.modules.setdefault("celery", MagicMock())

import pytest  # noqa: E402

from backend.utils import llm_batch  # noqa: E402
from backend.utils.llm_batch import (  # noqa: E402
    BatchItemFailed, CollectedResults, batch_check_and_collect,
    delete_collected_batch_files, delete_openai_batch_files_by_id,
    openai_batch_cancel_one, openai_batch_collect_one,
)

KEYS = {"anthropic": "k-ant", "openai": "k-oai"}
KEY = "openai:gpt-x"


class NotFoundError(Exception):
    """The fake SDK's 404."""


def _fake_openai(monkeypatch, batch=None, files=None):
    """The OpenAI SDK faked at its import: ``batch`` is what retrieve and
    cancel return, ``files`` the JSONL entries of each file id."""
    files = files or {}
    client = MagicMock()
    if batch is not None:
        client.batches.retrieve.return_value = batch
        client.batches.cancel.return_value = batch
    client.files.content.side_effect = lambda fid: SimpleNamespace(
        content="".join(json.dumps(e) + "\n" for e in files[fid]).encode())
    fake = types.ModuleType("openai")
    fake.OpenAI = MagicMock(return_value=client)
    fake.NotFoundError = NotFoundError
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.setitem(sys.modules, "anthropic", types.ModuleType("anthropic"))
    sys.modules["anthropic"].Anthropic = MagicMock()
    return client


def _batch(status="completed", completed=1, **files):
    return SimpleNamespace(
        id="b-1", status=status, created_at=1000, completed_at=1060,
        request_counts=SimpleNamespace(total=completed, completed=completed,
                                       failed=0),
        input_file_id=files.get("input_file_id"),
        output_file_id=files.get("output_file_id"),
        error_file_id=files.get("error_file_id"),
        errors=None)


def _ok_line(custom_id):
    return {"custom_id": custom_id, "response": {"status_code": 200, "body": {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}}}


class TestCollect:
    def test_an_ended_batch_names_its_files_and_deletes_nothing_yet(
            self, monkeypatch):
        client = _fake_openai(
            monkeypatch,
            _batch(input_file_id="file-in", output_file_id="file-out",
                   error_file_id="file-err"),
            {"file-out": [_ok_line("a")], "file-err": []})

        results, pending, _ = batch_check_and_collect({KEY: "b-1"}, KEYS)

        assert pending == {}
        assert results["a"]["content"] == "ok"
        assert results.batch_files == ["file-in", "file-out", "file-err"]
        client.files.delete.assert_not_called()

    def test_a_pending_batch_names_no_files(self, monkeypatch):
        _fake_openai(monkeypatch, _batch(status="in_progress",
                                         input_file_id="file-in"))

        results, pending, _ = batch_check_and_collect({KEY: "b-1"}, KEYS)

        assert pending == {KEY: "b-1"}
        assert results.batch_files == []

    def test_a_batch_refused_on_a_full_queue_names_its_input(
            self, monkeypatch):
        batch = _batch(status="failed", completed=0, input_file_id="file-in")
        batch.errors = SimpleNamespace(data=[SimpleNamespace(
            code="token_limit_exceeded", message="Enqueued token limit")])
        _fake_openai(monkeypatch, batch)

        results, _, _ = batch_check_and_collect({KEY: "b-1"}, KEYS)

        assert KEY in results.queue_full
        assert results.batch_files == ["file-in"]


class TestDeleteCollected:
    def _results(self, *file_ids):
        results = CollectedResults()
        results.batch_files.extend(file_ids)
        return results

    def test_every_file_is_deleted(self, monkeypatch):
        client = _fake_openai(monkeypatch)

        gone = delete_collected_batch_files(
            self._results("file-in", "file-out", "file-err"), KEYS)

        assert gone == 3
        assert [c.args[0] for c in client.files.delete.call_args_list] == [
            "file-in", "file-out", "file-err"]

    def test_a_file_already_gone_counts_and_a_failure_is_left(
            self, monkeypatch, caplog):
        client = _fake_openai(monkeypatch)

        def delete(file_id):
            if file_id == "file-gone":
                raise NotFoundError()
            if file_id == "file-busy":
                raise RuntimeError("503 from the provider")
        client.files.delete.side_effect = delete

        with caplog.at_level(logging.WARNING, logger=llm_batch.__name__):
            gone = delete_collected_batch_files(
                self._results("file-gone", "file-busy", "file-ok"), KEYS)

        assert gone == 2
        assert client.files.delete.call_count == 3
        warning = [r.getMessage() for r in caplog.records
                   if r.levelno == logging.WARNING]
        assert warning == [
            "Deleting OpenAI batch file file-busy failed (RuntimeError); left"]

    def test_never_raises(self, monkeypatch):
        fake = _fake_openai(monkeypatch)
        sys.modules["openai"].OpenAI.side_effect = RuntimeError("no client")

        assert delete_collected_batch_files(self._results("file-in"),
                                            KEYS) == 0
        fake.files.delete.assert_not_called()

    def test_nothing_to_delete_makes_no_call(self, monkeypatch):
        _fake_openai(monkeypatch)
        # A stand-in collect's plain dict, or an Anthropic batch's results.
        assert delete_collected_batch_files({"a": {}}, KEYS) == 0
        assert delete_collected_batch_files(CollectedResults(), KEYS) == 0
        sys.modules["openai"].OpenAI.assert_not_called()


class TestOneItemBatches:
    def test_by_id_deletes_an_ended_batchs_files(self, monkeypatch):
        client = _fake_openai(monkeypatch, _batch(
            status="cancelled", input_file_id="file-in",
            error_file_id="file-err"))

        assert delete_openai_batch_files_by_id("k", "b-1") == 2
        assert [c.args[0] for c in client.files.delete.call_args_list] == [
            "file-in", "file-err"]

    def test_by_id_leaves_a_running_batch_alone(self, monkeypatch):
        client = _fake_openai(monkeypatch, _batch(
            status="in_progress", input_file_id="file-in"))

        assert delete_openai_batch_files_by_id("k", "b-1") == 0
        client.files.delete.assert_not_called()

    def test_a_second_collect_after_the_delete_is_a_verdict(
            self, monkeypatch):
        client = _fake_openai(monkeypatch, _batch(
            input_file_id="file-in", output_file_id="file-out"))
        client.files.content.side_effect = NotFoundError()

        with pytest.raises(BatchItemFailed):
            openai_batch_collect_one("k", "b-1", "node-1")

    def test_a_cancel_for_a_rerun_deletes_the_files(self, monkeypatch):
        client = _fake_openai(monkeypatch, _batch(
            status="cancelling", input_file_id="file-in"))

        openai_batch_cancel_one("k", "b-1", delete_files=True)

        client.files.delete.assert_called_once_with("file-in")

    def test_a_cancel_whose_result_is_collected_keeps_them(
            self, monkeypatch):
        client = _fake_openai(monkeypatch, _batch(
            status="cancelling", input_file_id="file-in"))

        openai_batch_cancel_one("k", "b-1")

        client.files.delete.assert_not_called()

    def test_a_refused_cancel_still_deletes_an_ended_batchs_files(
            self, monkeypatch):
        client = _fake_openai(monkeypatch, _batch(
            status="completed", input_file_id="file-in",
            output_file_id="file-out"))
        client.batches.cancel.side_effect = RuntimeError("already ended")

        with pytest.raises(RuntimeError):
            openai_batch_cancel_one("k", "b-1", delete_files=True)

        assert [c.args[0] for c in client.files.delete.call_args_list] == [
            "file-in", "file-out"]
