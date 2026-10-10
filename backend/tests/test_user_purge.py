"""Tests for the user data purge (#268): backend/utils/user_purge.py, the
self-service and admin endpoints, and the job lifecycle (grace period,
cancel, claim, resume, failure).

A fixture user (alice) has a row in every table the purge touches, plus
the cross-user cases: another user's replies under her nodes, another
user's AI reply under her node, another user's links, drafts and shares
pointing at her nodes, shared batch jobs, and audio on disk. Bob's data is
the control: none of it may change, apart from the documented columns
that pointed at a deleted node of alice's.

sqlite in-memory with foreign keys ON, so a delete order Postgres would
refuse fails here too. Celery, provider batch calls and the batch lock
are stubbed; no model API is ever called.
"""
import os
import sys
from contextlib import contextmanager
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
from flask import Flask, g  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    APICostLog, ApiToken, ArtifactView, ChangelogReadState, Draft,
    ExternalAccount, ExternalDigestBatchJob, ExternalItem,
    ExternalItemEmbedding, FeedPick, FeedRender, Node, NodeContextArtifact,
    NodeEmbedding, NodeTranscriptChunk, NodeVersion, Poll,
    PollDraftBatchJob, PollResponse, ProfileBatchJob, RecentContextBatchJob,
    ReferenceAction, ShareDraft, TTSChunk, Thread, User, UserArtifact,
    UserDataPurge, UserFeedback, UserNotification, UserProfile, UserPrompt,
    UserRecentContext, UserTodo,
)
from backend.utils import user_purge as up  # noqa: E402
from backend.utils.system_accounts import (  # noqa: E402
    ERASED_SYSTEM_USERNAME, POLL_SYSTEM_USERNAME,
)

T0 = datetime(2026, 9, 1, 12, 0, 0)


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    _db.init_app(app)
    lm = LoginManager(app)

    @lm.user_loader
    def load_user(user_id):
        return _db.session.get(User, int(user_id))

    from backend.routes.account_data import account_data_bp
    from backend.routes.admin import admin_bp
    app.register_blueprint(account_data_bp, url_prefix="/api/account")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    return app


@pytest.fixture
def app():
    affected = lambda k: (  # noqa: E731
        k == "flask_login" or k.startswith("backend.routes")
        or k == "backend.models")
    saved = {k: sys.modules[k] for k in list(sys.modules) if affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[k]

    app = _make_app()
    with app.app_context():
        assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"
        with _db.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        _db.create_all()
        yield app
        _db.session.remove()
        with _db.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        _db.drop_all()

    for k in [k for k in list(sys.modules) if affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


class Stubs:
    def __init__(self):
        self.revoked = []
        self.cancelled = []
        self.running = []
        self.lock_ok = True
        self.rc_lock_ok = True
        self.dispatched = []


@pytest.fixture
def stubs(monkeypatch, tmp_path):
    s = Stubs()
    monkeypatch.setattr(up, "_revoke_tasks", lambda ids: s.revoked.extend(ids))
    monkeypatch.setattr(up, "_running_tasks",
                        lambda ids: [t for t in ids if t in s.running])

    def cancel(provider_key, batch_id, key_type="chat"):
        s.cancelled.append(batch_id)
        return None
    monkeypatch.setattr(up, "_cancel_provider_batch", cancel)

    @contextmanager
    def lock():
        yield s.lock_ok
    monkeypatch.setattr(up, "_profile_batch_lock", lock)

    @contextmanager
    def rc_lock():
        yield s.rc_lock_ok
    monkeypatch.setattr(up, "_recent_context_batch_lock", rc_lock)

    from backend.utils import audio_storage, twitter_archive
    monkeypatch.setattr(audio_storage, "AUDIO_STORAGE_ROOT", tmp_path / "audio")
    monkeypatch.setattr(twitter_archive, "STASH_ROOT", tmp_path / "data" / "imports")
    s.audio = tmp_path / "audio"
    s.data = tmp_path / "data"
    return s


# ── Fixture data ────────────────────────────────────────────────────────

def _add(obj):
    _db.session.add(obj)
    _db.session.flush()
    return obj


def _node(user, parent=None, *, owner=None, node_type="user", text="words",
          privacy="private", **kw):
    n = Node(user_id=user.id, human_owner_id=owner,
             parent_id=parent.id if parent else None, node_type=node_type,
             privacy_level=privacy, ai_usage="chat", token_count=3, **kw)
    n.set_content(text)
    return _add(n)


def _file(path, data=b"audio"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class World:
    pass


@pytest.fixture
def world(app, stubs):
    w = World()
    w.alice = _add(User(username="alice", email="alice@example.com",
                        twitter_id="1001", approved=True, plan="alpha",
                        description="I write about birds",
                        prefilled_handle="alicebirds",
                        profile_batch_pending=True, profile_force_batch=True,
                        profile_generation_task_id="t-profile",
                        default_privacy_level="public", craft_mode=True))
    w.bob = _add(User(username="bob", email="bob@example.com",
                      twitter_id="1002", approved=True, plan="alpha",
                      description="bob's bio", prefilled_handle="bobhandle"))
    w.admin = _add(User(username="admin", approved=True, is_admin=True))
    w.llm = _add(User(username="claude-opus", twitter_id="llm-claude-opus"))
    a, b, llm = w.alice, w.bob, w.llm

    # Alice's thread with bob's reply under the root: the root stays as a
    # tombstone, the rest goes.
    w.A1 = _node(a, owner=a.id, privacy="public", public_slug="birds")
    w.A2 = _node(a, w.A1, owner=a.id, audio_original_url="/media/x",
                 llm_task_id="t-a2", llm_task_status="processing")
    w.L1 = _node(llm, w.A2, owner=a.id, node_type="llm", llm_model="claude",
                 tts_task_id="t-l1", tts_task_status="pending")
    w.L2 = _node(llm, w.A2, owner=None, node_type="llm")   # legacy, alice's
    w.B1 = _node(b, w.A1, owner=b.id, privacy="public")
    w.LB = _node(llm, w.A1, owner=b.id, node_type="llm")   # bob's AI reply
    # A deep chain: bob's reply under alice's reply keeps both above it.
    w.A4 = _node(a, owner=a.id)
    w.A5 = _node(a, w.A4, owner=a.id, pinned_at=T0, pinned_by=a.id)
    w.B2 = _node(b, w.A5, owner=b.id)
    w.A6 = _node(a, w.B2, owner=a.id)
    # Bob's thread with alice's reply in it.
    w.B3 = _node(b, owner=b.id, privacy="public")
    w.A7 = _node(a, w.B3, owner=a.id, linked_node_id=w.A2.id)
    w.L3 = _node(llm, w.B3, owner=None, node_type="llm")   # legacy, bob's
    # Bob links to alice's node and continues from it.
    w.B4 = _node(b, owner=b.id, linked_node_id=w.A2.id,
                 continuation_node_id=w.A2.id, updated_at=T0)
    # An imported node of alice's, owned via human_owner_id only.
    w.A8 = _node(a, owner=a.id, source_key="chatgpt:1", origin="chatgpt")

    w.P1 = _add(UserProfile(user_id=a.id, content="p1", generated_by="m"))
    w.P2 = _add(UserProfile(user_id=a.id, content="p2", generated_by="m",
                            parent_profile_id=w.P1.id,
                            tts_task_id="t-p2", tts_task_status="processing"))
    w.PB = _add(UserProfile(user_id=b.id, content="bob p", generated_by="m"))
    w.I1 = _add(ExternalItem(user_id=a.id, source="web_clip", external_id="e1",
                             content="clip"))
    w.IB = _add(ExternalItem(user_id=b.id, source="web_clip", external_id="e1",
                             content="bob clip"))
    w.poll = _add(Poll(question="How is it?", created_by=w.admin.id))
    w.R1 = _add(PollResponse(poll_id=w.poll.id, user_id=a.id, status="drafting",
                             draft_task_id="t-poll"))
    w.RB = _add(PollResponse(poll_id=w.poll.id, user_id=b.id, status="sent"))

    _add(NodeVersion(node_id=w.A2.id, content="old"))
    _add(NodeVersion(node_id=w.A1.id, content="old root"))
    _add(NodeVersion(node_id=w.B1.id, content="bob old"))
    _add(Thread(root_node_id=w.A1.id, name="alice thread"))
    _add(Thread(root_node_id=w.B3.id, name="bob thread"))
    _add(NodeContextArtifact(node_id=w.A2.id, artifact_type="profile",
                             artifact_id=w.P1.id))
    _add(NodeEmbedding(node_id=w.A2.id, user_id=a.id, model="e",
                       content_hash="h", vector=b"v"))
    _add(NodeEmbedding(node_id=w.B1.id, user_id=b.id, model="e",
                       content_hash="h", vector=b"v"))
    _add(NodeTranscriptChunk(node_id=w.A2.id, chunk_index=0, text="t"))
    _add(NodeTranscriptChunk(session_id="sess-a", chunk_index=0, text="t",
                             status="processing", task_id="t-chunk"))
    _add(NodeTranscriptChunk(session_id="sess-b", chunk_index=0, text="t"))
    _add(TTSChunk(node_id=w.A2.id, chunk_index=0, audio_url="/media/a"))
    _add(TTSChunk(node_id=w.L1.id, chunk_index=0, audio_url="/media/b"))
    _add(TTSChunk(profile_id=w.P2.id, chunk_index=0))
    _add(TTSChunk(item_id=w.I1.id, chunk_index=0))
    _add(TTSChunk(node_id=w.LB.id, chunk_index=0))
    _add(TTSChunk(profile_id=w.PB.id, chunk_index=0))
    _add(FeedPick(user_id=a.id, node_id=w.L1.id, external_item_id=w.I1.id,
                  rank=1))
    _add(FeedPick(user_id=b.id, node_id=w.LB.id, external_item_id=w.IB.id,
                  rank=1))
    _add(FeedRender(node_id=w.L1.id))
    _add(ReferenceAction(user_id=a.id, item_id=w.I1.id, node_id=w.L1.id,
                         kind="open"))
    _add(ReferenceAction(user_id=b.id, item_id=w.IB.id, node_id=w.LB.id,
                         kind="open"))
    _add(ExternalItemEmbedding(item_id=w.I1.id, user_id=a.id, model="e",
                               content_hash="h", vector=b"v"))
    _add(ExternalItemEmbedding(item_id=w.IB.id, user_id=b.id, model="e",
                               content_hash="h", vector=b"v"))
    _add(ExternalAccount(user_id=a.id, provider="twitter", handle="alicebirds",
                         access_token="tok", refresh_token="ref"))
    _add(ExternalAccount(user_id=b.id, provider="twitter", handle="bobhandle"))
    _add(Draft(user_id=a.id, parent_id=w.A1.id, content="draft",
               session_id="sess-a", llm_node_id=w.L1.id))
    w.DB1 = _add(Draft(user_id=b.id, parent_id=w.A2.id, content="bob draft",
                       updated_at=T0))
    w.DB2 = _add(Draft(user_id=b.id, parent_id=w.A1.id, content="bob draft 2"))
    _add(ShareDraft(user_id=a.id, content="share", source_node_id=w.A2.id,
                    public_node_id=w.A1.id))
    w.SB = _add(ShareDraft(user_id=b.id, content="bob share",
                           source_node_id=w.A2.id, updated_at=T0))
    _add(UserRecentContext(user_id=a.id, content="rc", generated_by="m",
                           profile_id=w.P2.id))
    _add(UserRecentContext(user_id=b.id, content="rc", generated_by="m",
                           profile_id=w.PB.id))
    for model in (UserTodo, UserArtifact):
        kw = {"kind": "memory", "title": "Memory"} if model is UserArtifact else {}
        _add(model(user_id=a.id, content="x", generated_by="m", **kw))
        _add(model(user_id=b.id, content="x", generated_by="m", **kw))
    _add(ArtifactView(user_id=a.id, kind="memory"))
    _add(ArtifactView(user_id=b.id, kind="memory"))
    _add(UserPrompt(user_id=a.id, prompt_key="voice", title="v", content="p"))
    _add(UserPrompt(user_id=b.id, prompt_key="voice", title="v", content="p"))
    _add(UserFeedback(user_id=a.id, content="f"))
    _add(UserFeedback(user_id=b.id, content="f"))
    _add(UserNotification(user_id=a.id, type="profile_ready", title="t"))
    _add(UserNotification(user_id=b.id, type="profile_ready", title="t"))
    # Kept: credentials, identity and UI state (#269 removes them).
    _add(ApiToken(user_id=a.id, name="clipper", token_hash="h1", prefix="p"))
    _add(ChangelogReadState(user_id=a.id, section_id="s1"))

    for i in range(2):
        _add(APICostLog(user_id=a.id, model_id="m", request_type="conversation",
                        cost_microdollars=1000 + i, request_ref=f"node:{w.L1.id}",
                        provider_response_id=f"resp_{i}",
                        system_prefix_hash="abcd", input_tokens=10))
    _add(APICostLog(user_id=b.id, model_id="m", request_type="conversation",
                    cost_microdollars=7, request_ref=f"node:{w.LB.id}",
                    provider_response_id="resp_b"))

    w.J_only = _add(ProfileBatchJob(provider_key="anthropic", batch_id="b-only",
                                    status="pending",
                                    items=[{"custom_id": "c1", "user_id": a.id}]))
    w.J_mixed = _add(ProfileBatchJob(provider_key="anthropic", batch_id="b-mixed",
                                     status="pending",
                                     items=[{"custom_id": "c2", "user_id": a.id},
                                            {"custom_id": "c3", "user_id": b.id}]))
    w.J_done = _add(ProfileBatchJob(provider_key="anthropic", batch_id="b-done",
                                    status="collected",
                                    items=[{"custom_id": "c4", "user_id": a.id}]))
    w.J_bob = _add(ProfileBatchJob(provider_key="anthropic", batch_id="b-bob",
                                   status="pending",
                                   items=[{"custom_id": "c5", "user_id": b.id}]))
    w.PJ = _add(PollDraftBatchJob(provider_key="anthropic", batch_id="pd-a",
                                  status="pending",
                                  items=[{"custom_id": "p1", "response_id": w.R1.id}]))
    w.DJ = _add(ExternalDigestBatchJob(provider_key="anthropic", batch_id="dg",
                                       status="collected",
                                       items=[{"custom_id": "d1", "user_id": a.id},
                                              {"custom_id": "d2", "user_id": b.id}]))
    w.RJ = _add(RecentContextBatchJob(provider_key="anthropic", batch_id="rc",
                                      status="pending",
                                      items=[{"custom_id": "r1", "user_id": a.id},
                                             {"custom_id": "r2", "user_id": b.id}]))
    _db.session.commit()

    au, d = stubs.audio, stubs.data
    w.files_alice = [
        _file(au / f"user/{a.id}/node/{w.A2.id}/original.webm.enc"),
        _file(au / f"user/{a.id}/node/{w.A1.id}/tts.mp3"),
        _file(au / f"user/{llm.id}/node/{w.L1.id}/tts_chunk_0.mp3"),
        _file(au / f"nodes/{a.id}/{w.A2.id}/batch_0.webm"),
        _file(au / f"drafts/{a.id}/sess-a/chunk_0.webm"),
        _file(au / f"drafts/{a.id}/sess-old/chunk_0.webm"),
        _file(au / f"chunks/{a.id}/up-1/part0"),
        _file(au / f"streaming/{a.id}/s9/chunk_0.webm"),
        _file(au / f"user/{a.id}/profile/{w.P2.id}/tts.mp3"),
        _file(au / f"user/{a.id}/item/{w.I1.id}/tts.mp3"),
        _file(d / f"imports/{a.id}/stash.jsonl"),
        _file(d / "x-api/alicebirds-2026-08-27T10-00-00Z.jsonl"),
    ]
    w.files_bob = [
        _file(au / f"user/{b.id}/node/{w.B1.id}/original.webm"),
        _file(au / f"user/{llm.id}/node/{w.LB.id}/tts.mp3"),
        _file(au / f"nodes/{b.id}/{w.B3.id}/batch_0.webm"),
        _file(au / f"drafts/{b.id}/sess-b/chunk_0.webm"),
        _file(d / f"imports/{b.id}/stash.jsonl"),
        _file(d / "x-api/bobhandle-2026-08-27T10-00-00Z.jsonl"),
        _file(d / "x-api/alicebirdsx-2026-08-27T10-00-00Z.jsonl"),
    ]
    w.ids = {k: getattr(w, k).id for k in (
        "A1", "A2", "L1", "L2", "B1", "LB", "A4", "A5", "B2", "A6", "B3",
        "A7", "L3", "B4", "A8")}
    return w


def _bob_snapshot(w):
    """Every row of bob's (and the shared rows that are not alice's), with
    the columns a purge could touch."""
    b = w.bob.id

    def rows(model, *conds):
        pk = list(model.__table__.primary_key.columns)[0]
        q = model.query.filter(*conds).order_by(pk)
        out = []
        for r in q:
            d = {c.name: getattr(r, c.name) for c in model.__table__.columns}
            out.append(d)
        return out

    _db.session.expire_all()
    return {
        "nodes": rows(Node, Node.id.in_(
            [w.ids[k] for k in ("B1", "LB", "B2", "B3", "L3", "B4")])),
        "versions": rows(NodeVersion, NodeVersion.node_id == w.ids["B1"]),
        "thread": rows(Thread, Thread.root_node_id == w.ids["B3"]),
        "embeddings": rows(NodeEmbedding, NodeEmbedding.user_id == b),
        "chunks": rows(NodeTranscriptChunk, NodeTranscriptChunk.session_id == "sess-b"),
        "tts": rows(TTSChunk, TTSChunk.id.in_(
            [t.id for t in TTSChunk.query.filter(
                (TTSChunk.node_id == w.ids["LB"]) |
                (TTSChunk.profile_id == w.PB.id))])),
        "feed": rows(FeedPick, FeedPick.user_id == b),
        "refs": rows(ReferenceAction, ReferenceAction.user_id == b),
        "item_emb": rows(ExternalItemEmbedding, ExternalItemEmbedding.user_id == b),
        "account": rows(ExternalAccount, ExternalAccount.user_id == b),
        "drafts": rows(Draft, Draft.user_id == b),
        "shares": rows(ShareDraft, ShareDraft.user_id == b),
        "user_tables": {m.__name__: rows(m, m.user_id == b) for m in (
            UserProfile, UserRecentContext, UserTodo, UserArtifact,
            ArtifactView, UserPrompt, UserFeedback, UserNotification,
            PollResponse, ExternalItem, APICostLog)},
        "user": rows(User, User.id == b),
    }


def _run_job(source="admin"):
    """Schedule, claim and run a purge of alice the way the beat does."""
    alice = User.query.filter_by(username="alice").one()
    job, _ = up.schedule_purge(alice, requested_by_id=alice.id, source=source,
                               at=datetime.utcnow())
    tokens = []
    up.dispatch_due_jobs(lambda job_id, token: tokens.append(token))
    assert tokens, "the due job was not claimed"
    return job.id, up.run_purge_job(job.id, tokens[-1])


def test_the_test_database_enforces_foreign_keys(world):
    """The FK-order tests below only mean something if sqlite refuses
    what Postgres refuses."""
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        Node.query.filter_by(id=world.ids["A1"]).delete(synchronize_session=False)
        _db.session.flush()
    _db.session.rollback()


# ── Refusals ────────────────────────────────────────────────────────────

def test_ai_and_system_accounts_are_refused(world):
    from backend.utils.system_accounts import (
        get_erased_system_user, get_poll_system_user)
    polls, erased = get_poll_system_user(), get_erased_system_user()
    assert up.purge_refusal(world.llm) == "AI account"
    assert up.purge_refusal(polls) == "system account"
    assert up.purge_refusal(erased) == "system account"
    # An AI account is known by the nodes it authors, whatever its id.
    old_style = _add(User(username="gpt", twitter_id=None))
    _node(old_style, world.B3, owner=world.bob.id, node_type="llm")
    _db.session.commit()
    assert up.purge_refusal(old_style) == "AI account"
    assert up.purge_refusal(world.alice) is None

    for target in (world.llm, polls, erased, old_style):
        with pytest.raises(up.PurgeRefused):
            up.purge_user_content(target.id)
        with pytest.raises(up.PurgeRefused):
            up.purge_user_content(target.id, dry_run=True)
        with pytest.raises(up.PurgeRefused):
            up.schedule_purge(target, requested_by_id=1, source="admin")
    # Nothing of the AI account's was touched.
    assert Node.query.filter_by(user_id=world.llm.id).count() == 4


def test_job_for_an_ai_account_fails_without_deleting(world):
    job = _add(UserDataPurge(user_id=world.llm.id, source="admin",
                             status="scheduled", scheduled_for=datetime.utcnow()))
    _db.session.commit()
    tokens = []
    up.dispatch_due_jobs(lambda j, t: tokens.append(t))
    assert up.run_purge_job(job.id, tokens[0]) == "refused"
    assert _db.session.get(UserDataPurge, job.id).status == "failed"
    assert Node.query.filter_by(user_id=world.llm.id).count() == 4


# ── The purge itself ────────────────────────────────────────────────────

def test_dry_run_changes_nothing_and_matches_the_purge(world, stubs, monkeypatch):
    monkeypatch.setattr(up, "PURGE_CHUNK_SIZE", 2)   # many chunks, FK order
    before = {m.__name__: m.query.count() for m in (
        Node, NodeVersion, Draft, ShareDraft, UserProfile, ExternalItem,
        APICostLog, ProfileBatchJob, TTSChunk, FeedPick)}
    dry = up.purge_user_content(world.alice.id, dry_run=True)
    _db.session.expire_all()
    assert {m.__name__: m.query.count() for m in (
        Node, NodeVersion, Draft, ShareDraft, UserProfile, ExternalItem,
        APICostLog, ProfileBatchJob, TTSChunk, FeedPick)} == before
    assert all(f.exists() for f in world.files_alice)
    assert stubs.revoked == [] and stubs.cancelled == []

    # The expected shape of alice's data.
    assert dry["node"] == 6            # A2 L1 L2 A6 A7 A8
    assert dry["node_tombstoned"] == 3   # A1, A4, A5
    assert dry["others_replies_kept"] == 3   # B1, LB under A1; B2 under A5
    assert dry["files"] == len(world.files_alice)
    assert dry["api_cost_log"] == 2
    assert dry["celery_tasks_revoked"] == 6
    assert dry["provider_batches_cancelled"] == 2   # b-only, pd-a
    assert dry["profile_batch_job"] == 3
    assert dry["draft.parent_id"] == 1    # bob's draft under A2
    assert dry["node.linked_node_id"] == 1
    assert dry["node.continuation_node_id"] == 1
    assert dry["share_draft.source_node_id"] == 1

    job_id, outcome = _run_job()
    assert outcome == "done"
    actual = _db.session.get(UserDataPurge, job_id).counts
    assert actual == dry


def test_all_of_alices_data_is_gone(world, stubs):
    a = world.alice.id
    _, outcome = _run_job()
    assert outcome == "done"
    _db.session.expire_all()

    assert up.leftovers(up.count_user_data(a)) == {}
    # Only the tombstones are left of her nodes.
    left = {n.id for n in Node.query.filter(
        (Node.user_id == a) | (Node.human_owner_id == a))}
    assert left == {world.ids["A1"], world.ids["A4"], world.ids["A5"]}
    for k in ("A2", "L1", "L2", "A6", "A7", "A8"):
        assert _db.session.get(Node, world.ids[k]) is None, k
    for model in (UserProfile, UserRecentContext, UserTodo, UserArtifact,
                  ArtifactView, UserPrompt, UserFeedback, UserNotification,
                  PollResponse, ExternalItem, ExternalItemEmbedding,
                  ExternalAccount, Draft, ShareDraft, FeedPick,
                  ReferenceAction, NodeEmbedding, APICostLog):
        assert model.query.filter_by(user_id=a).count() == 0, model.__name__
    assert NodeTranscriptChunk.query.filter(
        NodeTranscriptChunk.session_id == "sess-a").count() == 0
    assert NodeVersion.query.filter(NodeVersion.node_id.in_(
        [world.ids["A1"], world.ids["A2"]])).count() == 0
    assert Thread.query.filter_by(root_node_id=world.ids["A1"]).count() == 0
    assert FeedRender.query.count() == 0
    assert TTSChunk.query.count() == 2   # bob's two
    assert NodeContextArtifact.query.count() == 0
    for f in world.files_alice:
        assert not f.exists(), f
    for folder in ("user", "nodes", "drafts", "chunks", "streaming"):
        assert not (stubs.audio / folder / str(a)).exists(), folder
    assert not (stubs.data / "imports" / str(a)).exists()

    # The account stays: login, settings, plan. What described the
    # writing does not.
    user = _db.session.get(User, a)
    assert (user.username, user.email, user.twitter_id, user.plan,
            user.default_privacy_level, user.craft_mode) == (
        "alice", "alice@example.com", "1001", "alpha", "public", True)
    assert user.description == ""
    assert user.prefilled_handle is None
    assert not user.profile_batch_pending and not user.profile_force_batch
    assert user.profile_generation_task_id is None
    # Credentials and UI state stay for account deletion (#269).
    assert ApiToken.query.filter_by(user_id=a).count() == 1
    assert ChangelogReadState.query.filter_by(user_id=a).count() == 1


def test_tombstones_keep_other_users_replies(world, stubs):
    _run_job()
    _db.session.expire_all()
    for k in ("A1", "A4", "A5"):
        n = _db.session.get(Node, world.ids[k])
        assert n.content is None and n.deleted_at is not None, k
        assert n.public_slug is None and n.pinned_at is None
        assert n.pinned_by is None and n.audio_original_url is None
    assert _db.session.get(Node, world.ids["B1"]).parent_id == world.ids["A1"]
    assert _db.session.get(Node, world.ids["LB"]).parent_id == world.ids["A1"]
    assert _db.session.get(Node, world.ids["B2"]).parent_id == world.ids["A5"]
    assert _db.session.get(Node, world.ids["A5"]).parent_id == world.ids["A4"]
    # Bob's draft under the tombstone keeps its parent.
    assert _db.session.get(Draft, world.DB2.id).parent_id == world.ids["A1"]


def test_other_users_data_is_untouched(world, stubs):
    before = _bob_snapshot(world)
    _run_job()
    after = _bob_snapshot(world)

    # The only changes: columns that pointed at a deleted node of alice's.
    for row in before["nodes"]:
        if row["id"] == world.ids["B4"]:
            row["linked_node_id"] = None
            row["continuation_node_id"] = None
    for row in before["drafts"]:
        if row["id"] == world.DB1.id:
            row["parent_id"] = None
    for row in before["shares"]:
        if row["id"] == world.SB.id:
            row["source_node_id"] = None
    assert after == before
    # updated_at was not bumped by the null-outs.
    assert _db.session.get(Node, world.ids["B4"]).updated_at == T0
    assert _db.session.get(Draft, world.DB1.id).updated_at == T0
    assert _db.session.get(ShareDraft, world.SB.id).updated_at == T0
    for f in world.files_bob:
        assert f.exists(), f
    # Bob's legacy AI reply in his own thread is his.
    assert _db.session.get(Node, world.ids["L3"]) is not None


def test_cost_rows_are_kept_without_the_person(world, stubs):
    total_before = sum(r.cost_microdollars for r in APICostLog.query)
    _run_job()
    _db.session.expire_all()
    erased = User.query.filter_by(username=ERASED_SYSTEM_USERNAME).one()
    assert not erased.approved and erased.plan == "free"
    rows = APICostLog.query.filter_by(user_id=erased.id).all()
    assert sorted(r.cost_microdollars for r in rows) == [1000, 1001]
    for r in rows:
        assert r.provider_response_id is None
        assert r.request_ref is None and r.system_prefix_hash is None
        assert r.model_id == "m" and r.input_tokens == 10
    assert sum(r.cost_microdollars for r in APICostLog.query) == total_before
    bob_row = APICostLog.query.filter_by(user_id=world.bob.id).one()
    assert bob_row.provider_response_id == "resp_b"


def test_in_flight_work_is_stopped(world, stubs):
    _run_job()
    _db.session.expire_all()
    # Alice's queued tasks are revoked; bob's are not among them.
    assert sorted(stubs.revoked) == sorted(
        ["t-a2", "t-l1", "t-p2", "t-poll", "t-profile", "t-chunk"])
    # Provider batches carrying only her requests are cancelled.
    assert sorted(stubs.cancelled) == ["b-only", "pd-a"]
    only = _db.session.get(ProfileBatchJob, world.J_only.id)
    assert only.status == "cancelled" and only.items == []
    mixed = _db.session.get(ProfileBatchJob, world.J_mixed.id)
    assert mixed.status == "pending"
    assert mixed.items == [{"custom_id": "c3", "user_id": world.bob.id}]
    assert _db.session.get(ProfileBatchJob, world.J_done.id) is None
    assert _db.session.get(ProfileBatchJob, world.J_bob.id).items == [
        {"custom_id": "c5", "user_id": world.bob.id}]
    assert _db.session.get(PollDraftBatchJob, world.PJ.id).status == "cancelled"
    assert _db.session.get(ExternalDigestBatchJob, world.DJ.id).items == [
        {"custom_id": "d2", "user_id": world.bob.id}]
    rj = _db.session.get(RecentContextBatchJob, world.RJ.id)
    assert rj.status == "pending"
    assert rj.items == [{"custom_id": "r2", "user_id": world.bob.id}]


def test_read_batch_of_a_node_is_cancelled(world, stubs):
    import json
    world.A2.tool_calls_meta = json.dumps([
        {"name": "_batch", "status": "submitted", "batch_id": "read-1",
         "provider": "openai", "key_type": "chat"}])
    _db.session.commit()
    _run_job()
    assert "read-1" in stubs.cancelled


def test_the_purge_never_decrypts(world, stubs, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the purge decrypted content")
    import backend.utils.encryption as enc
    monkeypatch.setattr(enc, "decrypt_content", boom)
    monkeypatch.setattr(_real_backend_models, "decrypt_content", boom)
    up.purge_user_content(world.alice.id, dry_run=True)
    _, outcome = _run_job()
    assert outcome == "done"


def test_rerun_is_safe_and_finds_nothing_more(world, stubs):
    _run_job()
    counts = up.purge_user_content(world.alice.id)
    assert up.leftovers(counts) == {}
    assert counts["node_tombstoned"] == 3   # wiped again, harmlessly
    assert Node.query.filter_by(user_id=world.bob.id).count() == 4


def test_rows_written_during_the_purge_are_caught(world, stubs, monkeypatch):
    """A node that appears while the purge runs is taken by the next
    round."""
    real_round = up._purge_round
    calls = []

    def round_and_write(user, plan, counts, heartbeat):
        real_round(user, plan, counts, heartbeat)
        if not calls:
            _node(_db.session.get(User, user.id), owner=user.id, text="late")
            _db.session.commit()
        calls.append(1)
    monkeypatch.setattr(up, "_purge_round", round_and_write)
    up.purge_user_content(world.alice.id)
    assert len(calls) == 2
    assert up.leftovers(up.count_user_data(world.alice.id)) == {}


def test_interrupted_purge_resumes(world, stubs, monkeypatch):
    """A crash after some chunks were committed: the job is retried by the
    beat and finishes from where the data stands."""
    monkeypatch.setattr(up, "PURGE_CHUNK_SIZE", 2)
    real = up._delete_node_rows_dependents
    calls = []

    def crash_on_second(ids, counts):
        calls.append(ids)
        if len(calls) == 2:
            raise RuntimeError("worker killed")
        return real(ids, counts)
    monkeypatch.setattr(up, "_delete_node_rows_dependents", crash_on_second)
    job_id, outcome = _run_job()
    assert outcome == "error"
    job = _db.session.get(UserDataPurge, job_id)
    assert job.status == "running" and job.heartbeat_at is None
    assert job.error.startswith("RuntimeError")
    # The first chunk (the deepest nodes) is gone, the second is not.
    assert _db.session.get(Node, world.ids["L1"]) is None
    assert _db.session.get(Node, world.ids["A2"]) is not None

    monkeypatch.setattr(up, "_delete_node_rows_dependents", real)
    tokens = []
    up.dispatch_due_jobs(lambda j, t: tokens.append(t))
    assert len(tokens) == 1
    assert up.run_purge_job(job_id, tokens[0]) == "done"
    job = _db.session.get(UserDataPurge, job_id)
    assert job.status == "done" and job.attempts == 2 and job.error is None
    assert up.leftovers(up.count_user_data(world.alice.id)) == {}


def test_a_job_that_keeps_failing_is_marked_failed(world, stubs, monkeypatch,
                                                   caplog):
    monkeypatch.setattr(up, "purge_user_content",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    alice = world.alice
    job, _ = up.schedule_purge(alice, requested_by_id=alice.id, source="admin",
                               at=datetime.utcnow())
    for _ in range(up.PURGE_MAX_ATTEMPTS):
        tokens = []
        up.dispatch_due_jobs(lambda j, t: tokens.append(t))
        assert up.run_purge_job(job.id, tokens[0]) == "error"
    caplog.clear()
    up.dispatch_due_jobs(lambda j, t: pytest.fail("dispatched a dead job"))
    job = _db.session.get(UserDataPurge, job.id)
    assert job.status == "failed" and "RuntimeError" in job.error
    assert job.attempts == up.PURGE_MAX_ATTEMPTS
    # Loud: an error-level log line, which Sentry reports.
    assert any(r.levelname == "ERROR" and f"job {job.id}" in r.getMessage()
               for r in caplog.records)


def _rerun_until_failed(job_id):
    """Let the beat retry the job until it has used up its attempts."""
    for _ in range(up.PURGE_MAX_ATTEMPTS):
        tokens = []
        up.dispatch_due_jobs(lambda j, t: tokens.append(t))
        if not tokens:
            break
        up.run_purge_job(job_id, tokens[0])
    up.dispatch_due_jobs(lambda j, t: None)
    return _db.session.get(UserDataPurge, job_id)


def test_rows_left_after_the_last_round_fail_the_run(
        world, stubs, monkeypatch, caplog):
    """A purge that still finds the user's rows after its last round has
    not deleted everything: the run fails (retried, then the job is
    marked failed and logged as an error), it is never reported done."""
    real_round = up._purge_round

    def round_and_write(user, plan, counts, heartbeat):
        real_round(user, plan, counts, heartbeat)
        _node(_db.session.get(User, user.id), owner=user.id, text="late")
        _db.session.commit()
    monkeypatch.setattr(up, "_purge_round", round_and_write)
    job_id, outcome = _run_job()
    assert outcome == "error"
    job = _db.session.get(UserDataPurge, job_id)
    assert job.status == "running" and job.heartbeat_at is None
    assert job.error.startswith("PurgeIncomplete") and "node" in job.error
    caplog.clear()
    job = _rerun_until_failed(job_id)
    assert job.status == "failed" and job.error.startswith("PurgeIncomplete")
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_a_file_that_cannot_be_deleted_fails_the_run(world, stubs, monkeypatch):
    real_unlink = up.os.unlink

    def refuse(path, *a, **k):
        if str(path).endswith("tts.mp3"):
            raise PermissionError("read-only")
        return real_unlink(path, *a, **k)
    monkeypatch.setattr(up.os, "unlink", refuse)
    job_id, outcome = _run_job()
    assert outcome == "error"
    job = _db.session.get(UserDataPurge, job_id)
    assert job.status != "done" and "files" in job.error
    monkeypatch.setattr(up.os, "unlink", real_unlink)
    tokens = []
    up.dispatch_due_jobs(lambda j, t: tokens.append(t))
    assert up.run_purge_job(job_id, tokens[0]) == "done"


def test_batch_items_left_behind_a_busy_lock_fail_the_run(world, stubs):
    """Past the longest wait the purge deletes what it can, but while her
    item is still in a batch job its collector can save a profile from
    the purged writing: the run is not done."""
    stubs.lock_ok = False
    job_id, outcome = _run_job()
    assert outcome == "wait"
    job = _db.session.get(UserDataPurge, job_id)
    job.waiting_since = datetime.utcnow() - up.PURGE_MAX_WAIT - timedelta(minutes=1)
    _db.session.commit()
    assert up.run_purge_job(job_id, job.task_id) == "error"
    job = _db.session.get(UserDataPurge, job_id)
    assert job.status != "done" and "profile_batch_job" in job.error
    assert _db.session.get(Node, world.ids["A2"]) is None   # the rest went
    stubs.lock_ok = True
    tokens = []
    up.dispatch_due_jobs(lambda j, t: tokens.append(t))
    assert up.run_purge_job(job_id, tokens[0]) == "done"
    assert _db.session.get(ProfileBatchJob, world.J_mixed.id).items == [
        {"custom_id": "c3", "user_id": world.bob.id}]


def test_an_unreadable_task_state_counts_as_running(world, stubs, monkeypatch):
    def down(ids):
        raise ConnectionError("result backend down")
    monkeypatch.setattr(up, "_running_tasks", down)
    job_id, outcome = _run_job()
    assert outcome == "wait"
    assert _db.session.get(Node, world.ids["A2"]) is not None


def test_a_failed_revoke_waits_and_revokes_again(world, stubs, monkeypatch):
    def down(ids):
        raise ConnectionError("broker down")
    monkeypatch.setattr(up, "_revoke_tasks", down)
    job_id, outcome = _run_job()
    assert outcome == "wait"
    assert _db.session.get(Node, world.ids["A2"]) is not None
    monkeypatch.setattr(up, "_revoke_tasks",
                        lambda ids: stubs.revoked.extend(ids))
    job = _db.session.get(UserDataPurge, job_id)
    assert up.run_purge_job(job_id, job.task_id) == "done"
    assert "t-a2" in stubs.revoked


def test_a_backed_up_queue_does_not_use_up_attempts(world, stubs, caplog):
    """The beat claims a due job every time its claim goes stale, but a
    claim whose runner is still waiting in the Celery queue is not an
    attempt: hours of backlog must not fail a purge with nothing done."""
    alice = world.alice
    job, _ = up.schedule_purge(alice, requested_by_id=alice.id, source="self",
                               at=datetime.utcnow())
    t0 = datetime.utcnow()
    tokens = []
    for minutes in (0, 11, 22, 33, 44, 55):
        up.dispatch_due_jobs(lambda j, t: tokens.append(t),
                             now=t0 + timedelta(minutes=minutes))
    job = _db.session.get(UserDataPurge, job.id)
    assert len(tokens) == 6
    assert job.status == "running" and job.attempts == 0
    assert job.runner_started_at is None
    assert Node.query.filter_by(user_id=alice.id).count() == 7
    # Overdue by more than an hour with no runner started: an error line.
    caplog.clear()
    up.dispatch_due_jobs(lambda j, t: tokens.append(t),
                         now=t0 + timedelta(minutes=66, seconds=1))
    assert any(r.levelname == "ERROR" and "no runner started" in r.getMessage()
               for r in caplog.records)
    # The queue drains: the old runners stand down, the latest one purges.
    for old in tokens[:-1]:
        assert up.run_purge_job(job.id, old) == "superseded"
    assert up.run_purge_job(job.id, tokens[-1]) == "done"
    job = _db.session.get(UserDataPurge, job.id)
    assert job.status == "done" and job.attempts == 1
    assert job.started_at is not None
    assert Node.query.filter_by(user_id=alice.id).count() == 3   # tombstones


def test_a_superseded_runner_stops(world, stubs):
    alice = world.alice
    job, _ = up.schedule_purge(alice, requested_by_id=alice.id, source="admin",
                               at=datetime.utcnow())
    assert up.claim_job(job.id, "old")
    # The old runner went quiet; the beat claims the job again.
    UserDataPurge.query.filter_by(id=job.id).update(
        {UserDataPurge.heartbeat_at: datetime.utcnow() - timedelta(hours=1)})
    _db.session.commit()
    assert up.claim_job(job.id, "new")
    assert up.run_purge_job(job.id, "old") == "superseded"
    assert Node.query.filter_by(user_id=alice.id).count() == 7
    assert up.run_purge_job(job.id, "new") == "done"


def test_a_running_job_with_a_fresh_heartbeat_is_not_claimed(world, stubs):
    alice = world.alice
    job, _ = up.schedule_purge(alice, requested_by_id=alice.id, source="admin",
                               at=datetime.utcnow())
    assert up.claim_job(job.id, "first")
    assert not up.claim_job(job.id, "second")
    assert up.dispatch_due_jobs(lambda j, t: pytest.fail("double claim")) == []


def test_the_purge_waits_for_running_tasks(world, stubs):
    stubs.running = ["t-a2"]
    job_id, outcome = _run_job()
    assert outcome == "wait"
    job = _db.session.get(UserDataPurge, job_id)
    assert job.status == "running" and job.waiting_since is not None
    assert job.heartbeat_at > datetime.utcnow()   # not taken for stale
    assert _db.session.get(Node, world.ids["A2"]) is not None
    assert "t-a2" in stubs.revoked
    # Still running past the longest a task can run: purge anyway.
    job.waiting_since = datetime.utcnow() - up.PURGE_MAX_WAIT - timedelta(minutes=1)
    _db.session.commit()
    assert up.run_purge_job(job_id, job.task_id) == "done"
    assert _db.session.get(Node, world.ids["A2"]) is None
    # Waiting under one claim is one attempt, however many looks it took.
    assert _db.session.get(UserDataPurge, job_id).attempts == 1


def test_a_running_profile_task_is_still_seen_on_the_next_look(world, stubs):
    """The profile task's guard is the only record of its id: the first
    look must not clear it, or the second look would stop waiting."""
    stubs.running = ["t-profile"]
    job_id, outcome = _run_job()
    assert outcome == "wait"
    assert _db.session.get(User, world.alice.id).profile_generation_task_id == "t-profile"
    job = _db.session.get(UserDataPurge, job_id)
    assert up.run_purge_job(job_id, job.task_id) == "wait"
    stubs.running = []
    assert up.run_purge_job(job_id, job.task_id) == "done"
    assert _db.session.get(User, world.alice.id).profile_generation_task_id is None


def test_cached_public_pages_are_dropped(world, stubs, monkeypatch):
    import backend.utils.public_cache as public_cache
    seen = []

    def record(user, former_handle=None):
        slug = _db.session.query(Node.public_slug).filter(
            Node.id == world.ids["A1"]).scalar()
        seen.append((user.id, slug))
    monkeypatch.setattr(public_cache, "invalidate_for_user", record)
    _run_job()
    # Before the purge, while the permalink's slug still names its page,
    # and after it.
    assert seen == [(world.alice.id, "birds"), (world.alice.id, None)]


def test_the_purge_waits_for_the_profile_batch_lock(world, stubs):
    stubs.lock_ok = False
    job_id, outcome = _run_job()
    assert outcome == "wait"
    assert _db.session.get(ProfileBatchJob, world.J_mixed.id).items[0][
        "user_id"] == world.alice.id
    stubs.lock_ok = True
    job = _db.session.get(UserDataPurge, job_id)
    assert up.run_purge_job(job_id, job.task_id) == "done"


def test_the_purge_waits_for_the_recent_context_batch_lock(world, stubs):
    """The recent-context collector saves from a job it claimed under
    its lock; the purge takes her item out under the same lock."""
    stubs.rc_lock_ok = False
    job_id, outcome = _run_job()
    assert outcome == "wait"
    assert [i["user_id"] for i in _db.session.get(
        RecentContextBatchJob, world.RJ.id).items] == [
        world.alice.id, world.bob.id]
    stubs.rc_lock_ok = True
    job = _db.session.get(UserDataPurge, job_id)
    assert up.run_purge_job(job_id, job.task_id) == "done"
    assert _db.session.get(RecentContextBatchJob, world.RJ.id).items == [
        {"custom_id": "r2", "user_id": world.bob.id}]


def test_profile_pipeline_skips_a_user_being_purged(world, stubs):
    """Waiting (the writing is hidden) or running: no profile or recent
    context is built (Peter, 2026-10-09); eligible again once done."""
    eligible = {u.id for u in User.profile_eligible_query()}
    assert world.alice.id in eligible
    job, _ = up.schedule_purge(world.alice, requested_by_id=world.alice.id,
                               source="admin", at=datetime.utcnow())
    assert world.alice.id not in {u.id for u in User.profile_eligible_query()}
    up.claim_job(job.id, "tok")
    assert world.alice.id not in {u.id for u in User.profile_eligible_query()}
    assert world.bob.id in {u.id for u in User.profile_eligible_query()}
    assert up.run_purge_job(job.id, "tok") == "done"
    assert world.alice.id in {u.id for u in User.profile_eligible_query()}


# ── Grace period, cancel, endpoints ─────────────────────────────────────

def _client(app, user):
    g.pop("_login_user", None)
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


def test_user_request_waits_out_the_grace_period(app, world, stubs):
    c = _client(app, world.alice)
    r = c.delete("/api/account/data", json={"confirm": "bob"})
    assert r.status_code == 400 and r.get_json()["code"] == "confirm_mismatch"
    assert UserDataPurge.query.count() == 0
    assert c.delete("/api/account/data").status_code == 400

    r = c.delete("/api/account/data", json={"confirm": "Alice"})
    assert r.status_code == 202
    body = r.get_json()
    assert body["status"] == "scheduled" and body["grace_days"] == 30
    job = UserDataPurge.query.one()
    assert job.source == "self" and job.requested_by_id == world.alice.id
    assert timedelta(days=29, hours=23) < job.scheduled_for - job.requested_at \
        <= timedelta(days=30)
    # Asking again does not add a second job.
    assert c.delete("/api/account/data",
                    json={"confirm": "alice"}).status_code == 200
    assert UserDataPurge.query.count() == 1

    # Nothing happens during the grace period...
    later = datetime.utcnow() + timedelta(days=29)
    assert up.dispatch_due_jobs(lambda j, t: pytest.fail("early"), now=later) == []
    assert Node.query.filter_by(user_id=world.alice.id).count() == 7
    # ...and the purge starts once it is over.
    tokens = []
    after = datetime.utcnow() + timedelta(days=30, minutes=1)
    assert up.dispatch_due_jobs(lambda j, t: tokens.append(t), now=after) == [job.id]
    assert up.run_purge_job(job.id, tokens[0]) == "done"
    assert c.get("/api/account/data").get_json()["status"] == "done"


def test_user_can_cancel_during_the_grace_period(app, world, stubs):
    c = _client(app, world.alice)
    assert c.post("/api/account/data/cancel").status_code == 404
    c.delete("/api/account/data", json={"confirm": "alice"})
    r = c.post("/api/account/data/cancel")
    assert r.status_code == 200 and r.get_json()["status"] is None
    job = UserDataPurge.query.one()
    assert job.status == "cancelled" and job.cancelled_by_id == world.alice.id
    after = datetime.utcnow() + timedelta(days=31)
    assert up.dispatch_due_jobs(lambda j, t: pytest.fail("cancelled"), now=after) == []
    assert Node.query.filter_by(user_id=world.alice.id).count() == 7
    assert APICostLog.query.filter_by(user_id=world.alice.id).count() == 2
    # A new request starts a new grace period.
    assert c.delete("/api/account/data",
                    json={"confirm": "alice"}).status_code == 202


def test_a_started_purge_cannot_be_cancelled(app, world, stubs):
    c = _client(app, world.alice)
    c.delete("/api/account/data", json={"confirm": "alice"})
    job = UserDataPurge.query.one()
    up.claim_job(job.id, "tok", now=job.scheduled_for)
    r = c.post("/api/account/data/cancel")
    assert r.status_code == 409 and r.get_json()["code"] == "already_running"
    assert _db.session.get(UserDataPurge, job.id).status == "running"


def test_cancel_and_claim_race_has_one_winner(world, stubs):
    job, _ = up.schedule_purge(world.alice, requested_by_id=1, source="self",
                               at=datetime.utcnow())
    assert up.claim_job(job.id, "tok")
    assert not up.cancel_purge(world.alice.id, world.alice.id)


def test_deletion_status_reports_a_removed_x_connection(world, stubs):
    _run_job()
    status = up.deletion_status(world.alice.id)
    assert status["status"] == "done" and status["x_connection_removed"]
    assert ExternalAccount.query.filter_by(user_id=world.alice.id).count() == 0
    assert ExternalAccount.query.filter_by(user_id=world.bob.id).count() == 1


# ── Revoking Loore's access at X (Peter, 2026-10-09) ────────────────────

ACCESS, REFRESH = "x-access-7f3a", "x-refresh-9c2e"


class _XRevoke:
    """A fake X revoke endpoint behind requests.post. Nothing leaves the
    test: a call aimed anywhere else fails it."""

    def __init__(self, monkeypatch, app, alice_id, outcome):
        import requests
        from backend.utils import external_content as ext
        app.config["X_CLIENT_ID"] = "cid"
        app.config["X_CLIENT_SECRET"] = "secret"
        self.calls = []

        def post(url, **kw):
            assert url == ext.X_REVOKE_URL, url
            # Revoked while the stored connection still exists.
            rows = ExternalAccount.query.filter_by(user_id=alice_id).count()
            self.calls.append((kw["data"]["token"], kw["auth"],
                               kw["timeout"], rows))
            if outcome == "timeout":
                raise requests.Timeout("read timed out")
            r = MagicMock()
            if outcome == "error":
                r.raise_for_status.side_effect = requests.HTTPError(
                    "503 Server Error", response=MagicMock(status_code=503))
            r.json.return_value = {"revoked": True}
            return r
        monkeypatch.setattr(ext.requests, "post", post)

    @property
    def tokens(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def x_revoke(world, app, monkeypatch):
    """Alice's X connection with recognisable tokens, and X configured."""
    acc = ExternalAccount.query.filter_by(user_id=world.alice.id).one()
    acc.access_token, acc.refresh_token = ACCESS, REFRESH
    _db.session.commit()
    return lambda outcome="ok": _XRevoke(monkeypatch, app, world.alice.id,
                                         outcome)


def _purge_log(caplog):
    return [(r.levelname, r.getMessage()) for r in caplog.records
            if r.name == up.logger.name]


def test_the_purge_revokes_the_stored_x_tokens_at_x(world, stubs, x_revoke,
                                                    caplog):
    x = x_revoke()
    caplog.set_level("INFO", logger=up.logger.name)
    _, outcome = _run_job()
    assert outcome == "done"
    assert x.tokens == [ACCESS, REFRESH]       # alice's only, never bob's
    for _, auth, timeout, rows_then in x.calls:
        assert auth == ("cid", "secret")
        assert timeout == up.X_REVOKE_TIMEOUT_SECONDS
        assert rows_then == 1
    assert ExternalAccount.query.filter_by(user_id=world.alice.id).count() == 0
    assert ExternalAccount.query.filter_by(user_id=world.bob.id).count() == 1
    log = _purge_log(caplog)
    assert any("2 stored X token(s) revoked" in m for _, m in log)
    assert not any(lvl == "ERROR" for lvl, _ in log)
    assert not any(ACCESS in m or REFRESH in m for _, m in log)


@pytest.mark.parametrize("outcome", ["error", "timeout"])
def test_a_failed_x_revoke_never_stops_the_purge(world, stubs, x_revoke,
                                                 caplog, outcome):
    x = x_revoke(outcome)
    _, result = _run_job()
    assert result == "done"
    assert x.tokens == [ACCESS, REFRESH]       # both tried
    assert ExternalAccount.query.filter_by(user_id=world.alice.id).count() == 0
    assert up.leftovers(up.count_user_data(world.alice.id)) == {}
    errors = [m for lvl, m in _purge_log(caplog) if lvl == "ERROR"]
    assert len(errors) == 2 and all("X did not revoke" in m for m in errors)
    assert not any(ACCESS in m or REFRESH in m for m in errors)
    if outcome == "error":
        assert all("HTTP 503" in m for m in errors)
    else:
        assert all("Timeout" in m for m in errors)


def test_a_refusal_is_a_warning_for_a_connection_x_had_refused(
        world, stubs, x_revoke, caplog):
    x_revoke("error")
    acc = ExternalAccount.query.filter_by(user_id=world.alice.id).one()
    acc.revoked_at = datetime.utcnow()
    _db.session.commit()
    _, outcome = _run_job()
    assert outcome == "done"
    assert [lvl for lvl, m in _purge_log(caplog)
            if "X did not revoke" in m] == ["WARNING", "WARNING"]


def test_no_stored_x_token_means_no_call(world, stubs, x_revoke):
    x = x_revoke()
    acc = ExternalAccount.query.filter_by(user_id=world.alice.id).one()
    acc.access_token = acc.refresh_token = None
    _db.session.commit()
    _, outcome = _run_job()
    assert outcome == "done" and x.calls == []
    assert ExternalAccount.query.filter_by(user_id=world.alice.id).count() == 0
    # No X connection at all: no call either.
    up.purge_user_content(world.alice.id)
    assert x.calls == []


def test_an_expired_access_token_is_not_sent(world, stubs, x_revoke):
    x = x_revoke()
    acc = ExternalAccount.query.filter_by(user_id=world.alice.id).one()
    acc.token_expires_at = datetime.utcnow() - timedelta(minutes=1)
    _db.session.commit()
    _, outcome = _run_job()
    assert outcome == "done" and x.tokens == [REFRESH]


def test_without_x_configured_the_connection_is_deleted_without_a_call(
        world, stubs, app, monkeypatch):
    from backend.utils import external_content as ext

    def post(url, **kw):
        raise AssertionError("called X")
    monkeypatch.setattr(ext.requests, "post", post)
    assert not app.config.get("X_CLIENT_ID")
    _, outcome = _run_job()
    assert outcome == "done"
    assert ExternalAccount.query.filter_by(user_id=world.alice.id).count() == 0


def test_the_x_tokens_are_the_only_values_the_purge_decrypts(
        world, stubs, x_revoke, monkeypatch):
    """Decrypting the stored X tokens to revoke them is allowed (they are
    credentials Loore holds, not writing); content is never decrypted."""
    x = x_revoke()
    decrypted = []

    def only_tokens(value, *a, **k):
        if value in (ACCESS, REFRESH):
            decrypted.append(value)
            return value
        raise AssertionError("the purge decrypted content")
    import backend.utils.encryption as enc
    monkeypatch.setattr(enc, "decrypt_content", only_tokens)
    monkeypatch.setattr(_real_backend_models, "decrypt_content", only_tokens)
    _, outcome = _run_job()
    assert outcome == "done"
    assert decrypted == [ACCESS, REFRESH] and x.tokens == [ACCESS, REFRESH]


def _stub_dispatch(monkeypatch, stubs):
    mod = MagicMock()
    mod.dispatch = lambda job_id, token: stubs.dispatched.append((job_id, token))
    monkeypatch.setitem(sys.modules, "backend.tasks.user_purge", mod)


def test_admin_dry_run_and_purge(app, world, stubs, monkeypatch):
    _stub_dispatch(monkeypatch, stubs)
    assert _client(app, world.bob).post(
        f"/api/admin/users/{world.alice.id}/purge_data?dry_run=1").status_code == 403

    c = _client(app, world.admin)
    r = c.post(f"/api/admin/users/{world.alice.id}/purge_data?dry_run=1")
    assert r.status_code == 200
    counts = r.get_json()["counts"]
    assert counts["node"] == 6 and counts["node_tombstoned"] == 3
    assert UserDataPurge.query.count() == 0
    assert Node.query.filter_by(user_id=world.alice.id).count() == 7

    r = c.post(f"/api/admin/users/{world.alice.id}/purge_data",
               json={"confirm_username": "bob"})
    assert r.status_code == 400
    r = c.post(f"/api/admin/users/{world.alice.id}/purge_data",
               json={"confirm_username": "alice"})
    assert r.status_code == 202
    job = UserDataPurge.query.one()
    assert job.source == "admin" and job.requested_by_id == world.admin.id
    assert job.status == "running" and len(stubs.dispatched) == 1
    assert up.run_purge_job(job.id, stubs.dispatched[0][1]) == "done"
    r = c.get(f"/api/admin/users/{world.alice.id}/purge_data")
    assert r.get_json()["job"]["status"] == "done"
    assert r.get_json()["job"]["counts"]["node"] == 6
    # The Users table row shows it.
    from backend.routes.admin import _latest_purge_jobs
    assert _latest_purge_jobs()[world.alice.id].status == "done"


def test_admin_purge_brings_a_scheduled_request_forward(app, world, stubs,
                                                        monkeypatch):
    _stub_dispatch(monkeypatch, stubs)
    up.schedule_purge(world.alice, requested_by_id=world.alice.id, source="self")
    c = _client(app, world.admin)
    r = c.post(f"/api/admin/users/{world.alice.id}/purge_data",
               json={"confirm_username": "alice"})
    assert r.status_code == 202
    job = UserDataPurge.query.one()
    assert job.status == "running" and len(stubs.dispatched) == 1
    r = c.post(f"/api/admin/users/{world.alice.id}/purge_data",
               json={"confirm_username": "alice"})
    assert r.status_code == 409


def test_admin_cannot_purge_an_ai_or_system_account(
        app, world, stubs, monkeypatch):
    _stub_dispatch(monkeypatch, stubs)
    from backend.utils.system_accounts import get_poll_system_user
    polls = get_poll_system_user()
    c = _client(app, world.admin)
    for target in (world.llm, polls):
        for url in (f"/api/admin/users/{target.id}/purge_data?dry_run=1",
                    f"/api/admin/users/{target.id}/purge_data"):
            r = c.post(url, json={"confirm_username": target.username})
            assert r.status_code == 409, url
            assert r.get_json()["code"] == "purge_refused"
    assert UserDataPurge.query.count() == 0
    assert stubs.dispatched == []
    assert POLL_SYSTEM_USERNAME == polls.username


def test_old_delete_my_data_route_is_gone(app):
    import backend.routes.export_data as export_data
    assert not hasattr(export_data, "delete_my_data")


def test_a_job_whose_account_is_gone_finishes(world, stubs):
    job = _add(UserDataPurge(user_id=99999, source="admin", status="scheduled",
                             scheduled_for=datetime.utcnow()))
    _db.session.commit()
    tokens = []
    up.dispatch_due_jobs(lambda j, t: tokens.append(t))
    assert up.run_purge_job(job.id, tokens[0]) == "done"
    assert _db.session.get(UserDataPurge, job.id).counts == {}


def test_purging_a_user_with_no_data(world, stubs):
    empty = _add(User(username="empty", approved=True))
    _db.session.commit()
    assert up.leftovers(up.purge_user_content(empty.id, dry_run=True)) == {}
    counts = up.purge_user_content(empty.id)
    assert up.leftovers(counts) == {}
    assert Node.query.filter_by(user_id=world.alice.id).count() == 7


def test_a_runner_superseded_mid_purge_stops(world, stubs, monkeypatch):
    monkeypatch.setattr(up, "PURGE_CHUNK_SIZE", 2)
    real = up._delete_node_rows_dependents

    def steal_after_first(ids, counts):
        real(ids, counts)
        UserDataPurge.query.update({UserDataPurge.task_id: "another-runner"})
    monkeypatch.setattr(up, "_delete_node_rows_dependents", steal_after_first)
    job_id, outcome = _run_job()
    assert outcome == "superseded"
    # It stopped after its first chunk.
    assert _db.session.get(Node, world.ids["L1"]) is None
    assert _db.session.get(Node, world.ids["A7"]) is not None
