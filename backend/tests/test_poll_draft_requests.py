"""AI drafts of a poll answer (#207): one at a time, a bounded number per
answer, and a cost row for every collected result.

- The move into 'drafting' is a conditional UPDATE, so of two draft
  requests at once one queues a draft; the other is answered with the
  draft in progress.
- The answer is not changed while its draft is being written: PUT
  /response answers 409, as /send does.
- An answer asks for at most MAX_DRAFTS_PER_ANSWER drafts (then 429
  ``too_many_drafts``); a draft that did not work can be asked for again
  within it, and writing the answer by hand stays possible.
- Every result the collector receives writes its cost row on the polls
  system account, also when the answer is no longer waiting for it.

The real routes and the real task bodies (_submit_poll_draft,
_collect_poll_draft_batches); only the provider calls are replaced. The
same on PostgreSQL with parallel requests is in
test_poll_draft_requests_postgres.py.
"""
import pytest

from backend.tests.test_updates import (  # noqa: F401
    app, client, changelog_file, pipeline, _write_entry, _login, _db, User,
    Poll, PollResponse, PollDraftBatchJob, APICostLog, UserProfile)
import backend.routes.updates as updates_routes

MAX = updates_routes.MAX_DRAFTS_PER_ANSWER


@pytest.fixture
def setup(app, pipeline, monkeypatch):  # noqa: F811
    """A user with an entry and a profile, an open poll, and the task
    glue: a queued draft is submitted at once; results are collected on
    demand."""
    user = User.query.filter_by(username="tester").first()
    _write_entry(user)
    profile = UserProfile(user_id=user.id, generated_by="user",
                          tokens_used=0)
    profile.set_content("PROFILE: daily journaler.")
    _db.session.add(profile)
    poll = Poll(question="What's missing?", model_id="test-model")
    _db.session.add(poll)
    _db.session.commit()

    submitted = []

    def fake_submit(requests_by_provider, keys, phase=None):
        submitted.append(requests_by_provider)
        return {"anthropic": f"batch-{len(submitted)}"}
    monkeypatch.setattr(pipeline, "batch_submit", fake_submit)

    class _Task:
        @staticmethod
        def delay(response_id):
            pipeline._submit_poll_draft(response_id, task_id="t")
    monkeypatch.setattr(pipeline, "submit_poll_draft", _Task)
    monkeypatch.setattr(
        pipeline.anthropic_batch_deletion, "delete_collected_batches",
        lambda *a, **k: None)
    monkeypatch.setattr(
        pipeline, "llm_cost_log_fields",
        lambda model_id, response, batch=None: {
            "input_tokens": 1000, "output_tokens": 50,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "cost_microdollars": 7})
    results = {}
    monkeypatch.setattr(
        pipeline, "batch_check_and_collect",
        lambda batch_ids, keys: (dict(results), {}, None))
    return user, poll, submitted, results


def _collect(pipeline, setup, content="A draft."):  # noqa: F811
    user, poll, _submitted, results = setup
    resp = PollResponse.query.filter_by(poll_id=poll.id,
                                        user_id=user.id).one()
    results.clear()
    if content:
        results[f"poll-draft-{resp.id}"] = {
            "content": content, "input_tokens": 1000, "output_tokens": 50}
    pipeline._collect_poll_draft_batches()
    _db.session.expire_all()


def _draft(client, poll):  # noqa: F811
    return client.post(f"/api/updates/polls/{poll.id}/draft")


def _put(client, poll, text="My own words."):  # noqa: F811
    return client.put(f"/api/updates/polls/{poll.id}/response",
                      json={"content": text})


def test_the_answer_is_not_changed_while_its_draft_is_written(
        setup, client, pipeline):  # noqa: F811
    user, poll, submitted, _results = setup
    assert _draft(client, poll).status_code == 202

    r = _put(client, poll)
    assert r.status_code == 409
    assert r.get_json()["error"] == updates_routes.DRAFT_IN_PROGRESS_TEXT
    # Asking again while it is written queues nothing more.
    r = _draft(client, poll)
    assert r.status_code == 200
    assert r.get_json()["response"]["status"] == "drafting"
    assert len(submitted) == 1
    assert PollDraftBatchJob.query.count() == 1

    _collect(pipeline, setup)
    resp = PollResponse.query.filter_by(poll_id=poll.id).one()
    assert (resp.status, resp.get_content()) == ("draft", "A draft.")
    # Once shown, the draft is saved and sent as before.
    assert _put(client, poll, "A draft.").status_code == 200
    assert client.post(
        f"/api/updates/polls/{poll.id}/send").status_code == 200
    resp = PollResponse.query.filter_by(poll_id=poll.id).one()
    assert (resp.status, resp.generated_by) == ("sent", "test-model")


def test_repeating_draft_and_save_stops_at_the_ceiling(
        setup, client, pipeline):  # noqa: F811
    user, poll, submitted, _results = setup
    codes = []
    for _ in range(MAX + 5):
        codes.append(_draft(client, poll).status_code)
        _put(client, poll)
        _collect(pipeline, setup)
        _put(client, poll)

    assert codes == [202] * MAX + [429] * 5
    assert len(submitted) == MAX
    r = _draft(client, poll)
    assert r.get_json()["code"] == "too_many_drafts"
    resp = PollResponse.query.filter_by(poll_id=poll.id).one()
    assert resp.draft_count == MAX
    # Every billed result has its cost row.
    assert APICostLog.query.filter_by(
        request_type="poll_draft").count() == MAX
    # Writing by hand and sending stay possible.
    assert _put(client, poll, "Written by hand.").status_code == 200
    assert client.post(
        f"/api/updates/polls/{poll.id}/send").status_code == 200


def test_a_draft_that_did_not_work_can_be_asked_for_again(
        setup, client, pipeline):  # noqa: F811
    user, poll, submitted, _results = setup
    assert _draft(client, poll).status_code == 202
    _collect(pipeline, setup, content=None)     # missing: failed
    resp = PollResponse.query.filter_by(poll_id=poll.id).one()
    assert resp.status == "draft_failed"

    assert _draft(client, poll).status_code == 202
    _collect(pipeline, setup)
    resp = PollResponse.query.filter_by(poll_id=poll.id).one()
    assert (resp.status, resp.draft_count) == ("draft", 2)
    assert len(submitted) == 2


def test_a_result_for_an_answer_no_longer_waiting_is_still_logged(
        setup, client, pipeline):  # noqa: F811
    user, poll, submitted, _results = setup
    assert _draft(client, poll).status_code == 202
    assert client.post(
        f"/api/updates/polls/{poll.id}/decline").status_code == 200

    _collect(pipeline, setup)

    resp = PollResponse.query.filter_by(poll_id=poll.id).one()
    assert (resp.status, resp.content) == ("declined", None)
    rows = APICostLog.query.filter_by(request_type="poll_draft").all()
    assert [(r.request_ref, r.cost_microdollars) for r in rows] == [
        (f"poll:{poll.id}", 7)]
    assert rows[0].user_id != user.id


def test_two_draft_requests_at_once_queue_one_draft(
        app, setup, client, monkeypatch):  # noqa: F811
    user, poll, submitted, _results = setup
    # The second request arrives between the first one's read of the
    # answer and its move into 'drafting'.
    real = updates_routes._get_or_create_response
    answers = []

    def get_or_create(p):
        resp = real(p)
        if not answers:
            answers.append(None)
            _db.session.commit()
            other = _login(app, "tester")
            answers[0] = _draft(other, poll).status_code
        return resp
    monkeypatch.setattr(updates_routes, "_get_or_create_response",
                        get_or_create)

    first = _draft(client, poll)

    assert sorted([first.status_code, answers[0]]) == [200, 202]
    assert first.get_json()["response"]["status"] == "drafting"
    assert len(submitted) == 1
    resp = PollResponse.query.filter_by(poll_id=poll.id).one()
    assert resp.draft_count == 1


def test_a_draft_request_that_cannot_be_queued_leaves_the_answer_open(
        setup, client, pipeline, monkeypatch):  # noqa: F811
    user, poll, submitted, _results = setup
    queued = pipeline.submit_poll_draft

    class _NoBroker:
        @staticmethod
        def delay(response_id):
            raise RuntimeError("queue unreachable")
    monkeypatch.setattr(pipeline, "submit_poll_draft", _NoBroker)

    with pytest.raises(RuntimeError):
        _draft(client, poll)

    _db.session.expire_all()
    resp = PollResponse.query.filter_by(poll_id=poll.id).one()
    assert (resp.status, resp.draft_count) == ("draft_failed", 0)
    assert submitted == []
    # The person can write the answer, or ask for a draft again.
    assert _put(client, poll, "Written by hand.").status_code == 200
    monkeypatch.setattr(pipeline, "submit_poll_draft", queued)
    assert _draft(client, poll).status_code == 202
    assert len(submitted) == 1
