"""A stand-in for the Anthropic SDK's Message Batches, for the tests of
#693 (Anthropic batches deleted once Loore no longer needs them).

``install(monkeypatch)`` puts a fake ``anthropic`` module in sys.modules
whose client keeps a dict of batches by id. A batch that is not in it
answers 404 (``NotFoundError``), as a deleted one does at Anthropic; a
delete of a batch still processing is refused, as the API refuses it.
Every call is recorded in ``calls``.
"""
import sys
import types
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock


class NotFoundError(Exception):
    """The fake SDK's 404."""


class BadRequestError(Exception):
    """The fake SDK's 400."""


class FakeBatches:
    def __init__(self):
        self.batches = {}   # batch_id -> processing_status
        self.results_by_batch = {}   # batch_id -> [result entries]
        self.calls = []
        self.delete_error = None     # an exception every delete raises
        self.on_delete = None        # called with the id before a delete

    # ── set-up ──
    def add(self, batch_id, status="ended", results=()):
        self.batches[batch_id] = status
        self.results_by_batch[batch_id] = list(results)

    def calls_of(self, kind):
        return [b for (k, b) in self.calls if k == kind]

    # ── the SDK surface ──
    def _batch(self, batch_id):
        created = datetime(2026, 11, 13, 10, 0, 0)
        return SimpleNamespace(
            id=batch_id, processing_status=self.batches[batch_id],
            created_at=created, ended_at=created + timedelta(minutes=3),
            request_counts=SimpleNamespace(processing=0, succeeded=1))

    def retrieve(self, batch_id):
        self.calls.append(("retrieve", batch_id))
        if batch_id not in self.batches:
            raise NotFoundError(batch_id)
        return self._batch(batch_id)

    def results(self, batch_id):
        self.calls.append(("results", batch_id))
        if batch_id not in self.batches:
            raise NotFoundError(batch_id)
        return iter(self.results_by_batch.get(batch_id, []))

    def cancel(self, batch_id):
        self.calls.append(("cancel", batch_id))
        if batch_id not in self.batches:
            raise NotFoundError(batch_id)
        if self.batches[batch_id] == "in_progress":
            self.batches[batch_id] = "canceling"
        return self._batch(batch_id)

    def delete(self, batch_id):
        self.calls.append(("delete", batch_id))
        if self.on_delete is not None:
            self.on_delete(batch_id)
        if self.delete_error is not None:
            raise self.delete_error
        if batch_id not in self.batches:
            raise NotFoundError(batch_id)
        if self.batches[batch_id] != "ended":
            raise BadRequestError("a batch still processing can't be "
                                  "deleted")
        del self.batches[batch_id]
        return SimpleNamespace(id=batch_id, type="message_batch_deleted")

    def list(self, limit=20, after_id=None):
        self.calls.append(("list", after_id))
        ids = list(self.batches)
        start = ids.index(after_id) + 1 if after_id in ids else 0
        page = ids[start:start + limit]
        return SimpleNamespace(
            data=[self._batch(b) for b in page],
            has_more=start + limit < len(ids),
            last_id=page[-1] if page else None)


def succeeded(custom_id, text="ok", input_tokens=10, output_tokens=5):
    """A succeeded result entry of a batch."""
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(
        type="succeeded", message=SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=input_tokens, output_tokens=output_tokens,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0))))


def install(monkeypatch):
    """Fake the anthropic module; returns its FakeBatches."""
    batches = FakeBatches()
    client = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    fake = types.ModuleType("anthropic")
    fake.Anthropic = MagicMock(return_value=client)
    fake.NotFoundError = NotFoundError
    fake.BadRequestError = BadRequestError
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return batches
