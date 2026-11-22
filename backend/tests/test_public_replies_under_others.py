"""Public replies under other people's pieces: their links tell search
engines not to follow them, and one account adds at most
PUBLIC_REPLIES_PER_DAY of them in 24 hours. A piece's own author, private
writing and an account's own threads are not affected.

Real app (create_app(), in-memory sqlite), through the HTTP routes.
"""
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

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import User, Node  # noqa: E402
import backend.models as _real_backend_models  # noqa: E402

FRONTEND_URL = "https://loore.test"
REL = 'rel="nofollow ugc noopener"'


def _affected(k):
    return (k == "flask_login" or k.startswith("backend.routes")
            or k == "backend.models")


@pytest.fixture
def world(monkeypatch):
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]
    import backend.config as _config
    monkeypatch.setattr(_config.Config, "SQLALCHEMY_DATABASE_URI",
                        "sqlite:///:memory:")
    from backend import create_app
    app = create_app()
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"
    app.config["TESTING"] = True
    app.config["FRONTEND_URL"] = FRONTEND_URL
    app.config["SHARE_V1"] = True
    app.config["PAYMENTS_ENABLED"] = False

    import backend.utils.public_cache as public_cache
    monkeypatch.setattr(public_cache, "get", lambda path: None)
    monkeypatch.setattr(public_cache, "put", lambda *a, **k: None)
    monkeypatch.setattr(public_cache, "invalidate", lambda *a, **k: None)

    with app.app_context():
        _db.create_all()
        now = datetime.utcnow()
        _db.session.add_all([
            User(username=name, email=f"{name}@example.com", approved=True,
                 public_sharing_enabled=True, accepted_terms_at=now,
                 default_ai_usage="chat")
            for name in ("author", "guest")])
        _db.session.commit()
        root = _mk("author", "# Author piece\n\nSee http://own.example/a")
        from backend.utils.slugs import generate_unique_public_slug
        root.public_slug = generate_unique_public_slug(
            root.user_id, root.get_content())
        _db.session.commit()
        yield {"app": app, "root": root.id, "slug": root.public_slug}
        _db.session.remove()
        _db.drop_all()
    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    sys.modules.update(saved)


def _mk(username, content, parent_id=None, privacy="public", created=None):
    user = User.query.filter_by(username=username).first()
    node = Node(user_id=user.id, human_owner_id=user.id, parent_id=parent_id,
                node_type="user", privacy_level=privacy, ai_usage="chat")
    node.set_content(content)
    if created is not None:
        node.created_at = created
    _db.session.add(node)
    _db.session.commit()
    return node


def _client(app, name=None):
    from flask import g
    g.pop("_login_user", None)
    c = app.test_client()
    if name:
        _db.session.expire_all()
        uid = User.query.filter_by(username=name).first().id
        with c.session_transaction() as sess:
            sess["_user_id"] = str(uid)
            sess["_fresh"] = True
    return c


def _reply(client, parent_id, text="a reply", privacy="public"):
    return client.post("/api/nodes/", json={
        "content": text, "parent_id": parent_id, "privacy_level": privacy})


@pytest.fixture
def limit(monkeypatch):
    import backend.utils.public_reply_limits as limits
    monkeypatch.setattr(limits, "PUBLIC_REPLIES_PER_DAY", 3)
    return 3


def test_links_in_a_guests_reply_are_not_followed(world):
    app, root = world["app"], world["root"]
    r = _reply(_client(app, "guest"), root,
               "Look: http://shop.example/1 and [here](http://shop.example/2)")
    assert r.status_code == 201
    guest_reply = r.get_json()["id"]
    # The author answers in their own thread, under the guest's reply.
    r = _reply(_client(app, "author"), guest_reply,
               "Thanks, see http://own.example/b")
    assert r.status_code == 201

    for url in (f"/node/{root}", f"/@author/{world['slug']}"):
        page = _client(app).get(url).get_data(as_text=True)
        for n in (1, 2):
            assert f'<a {REL} href="http://shop.example/{n}">' in page
        # The author's own links, in the piece and in a reply, are as
        # they were.
        assert '<a href="http://own.example/a">' in page
        assert '<a href="http://own.example/b">' in page
        assert page.count(REL) == 2


def test_public_replies_under_others_stop_at_the_days_limit(world, limit):
    app, root = world["app"], world["root"]
    guest = _client(app, "guest")
    for i in range(limit):
        assert _reply(guest, root, f"reply {i}").status_code == 201
    before = Node.query.count()

    r = _reply(guest, root, "one more")
    assert r.status_code == 429
    body = r.get_json()
    assert body["code"] == "too_many_public_replies"
    assert "public replies" in body["error"]
    assert Node.query.count() == before

    # The account's own threads and its private writing go on.
    own = guest.post("/api/nodes/", json={
        "content": "# My piece", "privacy_level": "public"})
    assert own.status_code == 201
    assert _reply(guest, own.get_json()["id"], "under my own").status_code \
        == 201
    assert guest.post("/api/nodes/", json={
        "content": "a private note"}).status_code == 201
    # The piece's author is not counted in their own thread.
    author = _client(app, "author")
    for i in range(limit + 1):
        assert _reply(author, root, f"author {i}").status_code == 201


def test_replies_older_than_a_day_are_not_counted(world, limit):
    app, root = world["app"], world["root"]
    old = datetime.utcnow() - timedelta(hours=25)
    for i in range(limit):
        _mk("guest", f"earlier {i}", parent_id=root, created=old)
    assert _reply(_client(app, "guest"), root, "today").status_code == 201


def test_replies_below_ones_own_reply_in_anothers_thread_count(world, limit):
    app, root = world["app"], world["root"]
    guest = _client(app, "guest")
    r = _reply(guest, root, "first")
    assert r.status_code == 201
    tip = r.get_json()["id"]
    for i in range(limit - 1):
        r = _reply(guest, tip, f"follow-up {i}")
        assert r.status_code == 201
        tip = r.get_json()["id"]
    assert _reply(guest, tip, "one more").status_code == 429


def test_an_entry_made_public_in_anothers_thread_counts(world, limit):
    app, root = world["app"], world["root"]
    guest = _client(app, "guest")
    first = _reply(guest, root, "first").get_json()["id"]
    for i in range(limit - 1):
        assert _reply(guest, root, f"reply {i}").status_code == 201
    note = _mk("guest", "a note below my reply", parent_id=first,
               privacy="private")
    r = guest.put(f"/api/nodes/{note.id}", json={
        "content": "a note below my reply", "privacy_level": "public"})
    assert r.status_code == 429
    assert r.get_json()["code"] == "too_many_public_replies"
    _db.session.expire_all()
    assert _db.session.get(Node, note.id).privacy_level == "private"


def test_the_limit_is_far_above_a_days_real_use():
    from backend.utils.public_reply_limits import PUBLIC_REPLIES_PER_DAY
    assert PUBLIC_REPLIES_PER_DAY == 200
