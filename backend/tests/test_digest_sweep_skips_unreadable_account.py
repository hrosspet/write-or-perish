"""The nightly digest sweep goes on past one account it cannot read.

The sweep renders one request per account due in an hour and submits
them together. An account with a saved reference whose stored value does
not decrypt is left out of that run (logged by id only) and nothing is
written for it, so it is due again in its next night; every other account
due in that hour is submitted. Encryption on, key service replaced by a
local stand-in, batch submit stubbed.
"""
from datetime import datetime

import pytest
from google.api_core import exceptions as gexc

from backend.tests import test_external_digest_nightly as dn
from backend.extensions import db as _db
from backend.models import ExternalDigestBatchJob, ExternalItem, User
from backend.utils import encryption

_digest = dn._digest
app = dn.app  # noqa: F811 (fixture)

UNREADABLE = "ENC:v2:AAAA:BBBB"


def _kms_on(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"wrapped:" + dek)

    def unwrap(w):
        if not w.startswith(b"wrapped:"):
            raise ValueError("key service: unknown wrapped key")
        return w[len(b"wrapped:"):]
    monkeypatch.setattr(encryption, "_unwrap_dek", unwrap)


def _record_submits(monkeypatch):
    sent = []

    def fake_submit(requests_by_provider, api_keys, phase=None, tag=None):
        sent.append(requests_by_provider)
        return {"anthropic": f"batch-{len(sent)}"}
    monkeypatch.setattr(_digest, "batch_submit", fake_submit)
    monkeypatch.setattr(_digest, "_batch_keys", lambda: {"anthropic": "k"})
    return sent


def _users_sent(sent):
    return sorted(r["user_id"] for call in sent
                  for reqs in call.values() for r in reqs)


def _reference(user_id, url, text=None, raw=None):
    item = ExternalItem(user_id=user_id, source="web_clip", external_id=url,
                        url=url, fetched_at=datetime.utcnow())
    if raw is not None:
        item.content = raw
    else:
        item.set_content(text)
    _db.session.add(item)
    _db.session.commit()
    return item


def test_one_unreadable_account_leaves_the_others_due_that_hour_submitted(
        app, monkeypatch):  # noqa: F811
    _kms_on(monkeypatch)
    sent = _record_submits(monkeypatch)
    night = dn._tz_at_hour(_digest.NIGHTLY_DIGEST_LOCAL_HOUR)
    first = User.query.first()
    first.timezone = night
    unreadable = User(username="unreadable", timezone=night,
                      last_seen_at=datetime.utcnow())
    last = User(username="last", timezone=night,
                last_seen_at=datetime.utcnow())
    _db.session.add_all([unreadable, last])
    _db.session.commit()
    _reference(first.id, "https://example.com/a", "a page worth keeping")
    _reference(unreadable.id, "https://example.com/x", raw=UNREADABLE)
    _reference(last.id, "https://example.com/b", "another page")

    result = _digest.sweep_external_digests()
    assert result == {"status": "ok", "submitted": 2}
    assert _users_sent(sent) == sorted([first.id, last.id])
    jobs = ExternalDigestBatchJob.query.all()
    assert {i["user_id"] for j in jobs for i in j.items} == {
        first.id, last.id}
    # Nothing written for it: due again in its next night.
    assert unreadable.id in _digest._stale_user_ids()


def test_a_reference_saved_with_such_text_is_read_like_any_other(
        app, monkeypatch):  # noqa: F811
    _kms_on(monkeypatch)
    sent = _record_submits(monkeypatch)
    user = User.query.first()
    user.timezone = dn._tz_at_hour(_digest.NIGHTLY_DIGEST_LOCAL_HOUR)
    _db.session.commit()
    _reference(user.id, "https://example.com/x", UNREADABLE)

    assert _digest.sweep_external_digests()["submitted"] == 1
    prompt = sent[0]["anthropic"][0]["messages"][0]["content"]
    assert UNREADABLE in prompt


def test_a_key_service_outage_ends_the_run_at_the_first_account(
        app, monkeypatch):  # noqa: F811
    """Unlike one unreadable account, an outage of the key service ends
    the run as before: every account would wait out its own retries."""
    _kms_on(monkeypatch)
    sent = _record_submits(monkeypatch)
    night = dn._tz_at_hour(_digest.NIGHTLY_DIGEST_LOCAL_HOUR)
    users = [User.query.first()]
    users[0].timezone = night
    users += [User(username=f"reader{i}", timezone=night,
                   last_seen_at=datetime.utcnow()) for i in range(3)]
    _db.session.add_all(users[1:])
    _db.session.commit()
    for i, user in enumerate(users):
        _reference(user.id, f"https://example.com/{i}", f"a saved page {i}")
    attempts = []

    def down(wrapped):
        attempts.append(wrapped)
        raise gexc.ServiceUnavailable("the service is unavailable")
    monkeypatch.setattr(encryption, "_unwrap_dek", down)

    with pytest.raises(gexc.ServiceUnavailable):
        _digest.sweep_external_digests()
    assert len(attempts) == 1
    assert sent == []
