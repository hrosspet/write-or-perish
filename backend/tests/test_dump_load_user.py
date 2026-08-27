"""dump_user.py / load_user.py round-trip: id remapping (node parent +
continuation links, profile chain parent_profile_id, recent context →
profile), public-only default, merge dedup on source_key, refusal on a
non-empty user, and re-encryption under the #257 invariant.
"""
import json
import os
import sys
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
from flask import Flask  # noqa: E402

for _mod in ["backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db  # noqa: E402
from backend.models import User, Node, UserProfile, UserRecentContext  # noqa: E402
from backend.utils import encryption  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import dump_user  # noqa: E402
import load_user  # noqa: E402


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME", "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"w:" + dek)
    monkeypatch.setattr(encryption, "_unwrap_dek", lambda w: w[2:])
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _seed_source():
    """alice: a 3-node public thread (root→child, child continues to c2),
    one private node, an LLM reply, a 2-profile chain + recent context."""
    alice = User(username="alice", approved=True, plan="alpha", description="hi")
    llm = User(username="claude-opus-5")
    db.session.add_all([alice, llm])
    db.session.flush()

    def node(author, text, privacy="public", parent=None, key=None, origin=None):
        n = Node(user_id=author.id, human_owner_id=alice.id, node_type="user" if author is alice else "llm",
                 privacy_level=privacy, ai_usage="chat", parent_id=parent.id if parent else None,
                 source_key=key, origin=origin, token_count=len(text) // 4)
        n.set_content(text)
        db.session.add(n)
        db.session.flush()
        return n
    root = node(alice, "root tweet", key="twitter:1", origin="twitter")
    child = node(llm, "llm reply", parent=root)
    cont = node(alice, "continued", parent=root, key="twitter:2", origin="twitter")
    child.continuation_node_id = cont.id
    node(alice, "my secret", privacy="private", key="twitter:3")

    p1 = UserProfile(user_id=alice.id, generated_by="m", generation_type="initial",
                     source_tokens_used=10, source_origin_stats={"twitter": 10})
    p1.set_content("profile v1")
    db.session.add(p1)
    db.session.flush()
    p2 = UserProfile(user_id=alice.id, generated_by="m", generation_type="iterative",
                     parent_profile_id=p1.id, source_tokens_used=20)
    p2.set_content("profile v2")
    db.session.add(p2)
    db.session.flush()
    rc = UserRecentContext(user_id=alice.id, generated_by="m", profile_id=p2.id)
    rc.set_content("recent")
    db.session.add(rc)
    db.session.commit()
    return alice, root, child, cont, p1.id, p2.id


def _read(path):
    """Format 2: header line + one node per line → dict with 'nodes'."""
    with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    d = json.loads(lines[0])
    d["nodes"] = [json.loads(ln) for ln in lines[1:]]
    return d


def _raw(node_id):
    return db.session.execute(db.text("SELECT content FROM node WHERE id=:i"), {"i": node_id}).scalar()


def test_round_trip_remaps_links_and_respects_privacy(app, tmp_path):
    alice, root, child, cont, p1_id, p2_id = _seed_source()
    out = tmp_path / "alice.json"
    dump_user._run("alice", str(out))
    d = _read(out)
    assert d["format"] == 2
    assert [n["source_key"] for n in d["nodes"]] == ["twitter:1", None, "twitter:2"]  # private skipped
    assert d["nodes"][1]["author_username"] == "claude-opus-5"
    assert d["profiles"][1]["parent_profile_id"] == p1_id
    assert d["recent_contexts"][0]["profile_id"] == p2_id
    assert d["profiles"][0]["content"] == "profile v1"  # decrypted in the file

    # Load onto a fresh user in the same DB (ids will differ from the source's).
    load_user._run(str(out), "bob", merge=False, create_approved=True)
    bob = User.query.filter_by(username="bob").one()
    assert bob.approved is True and bob.description == "hi"
    nodes = Node.query.filter_by(human_owner_id=bob.id).order_by(Node.id).all()
    assert len(nodes) == 3
    r, c, k = nodes
    assert r.parent_id is None and c.parent_id == r.id and k.parent_id == r.id
    assert c.continuation_node_id == k.id
    assert c.user_id == User.query.filter_by(username="claude-opus-5").one().id
    assert r.origin == "twitter" and r.source_key == "twitter:1"
    assert _raw(r.id) == "root tweet"  # public → plaintext at rest (#257)
    profiles = UserProfile.query.filter_by(user_id=bob.id).order_by(UserProfile.id).all()
    assert [p.get_content() for p in profiles] == ["profile v1", "profile v2"]
    assert profiles[1].parent_profile_id == profiles[0].id
    assert profiles[0].source_origin_stats == {"twitter": 10}
    assert profiles[0].content.startswith(encryption.ENCRYPTED_PREFIX_V2)  # re-encrypted here
    rc = UserRecentContext.query.filter_by(user_id=bob.id).one()
    assert rc.profile_id == profiles[1].id and rc.get_content() == "recent"


def test_refuses_non_empty_user_without_merge_and_dedups_with_it(app, tmp_path):
    _seed_source()
    out = tmp_path / "alice.json"
    dump_user._run("alice", str(out))
    load_user._run(str(out), "bob", merge=False, create_approved=False)
    with pytest.raises(SystemExit):
        load_user._run(str(out), "bob", merge=False, create_approved=False)
    load_user._run(str(out), "bob", merge=True, create_approved=False)
    bob = User.query.filter_by(username="bob").one()
    # keyed nodes deduped; the unkeyed LLM reply is re-created (no key to match)
    assert Node.query.filter_by(human_owner_id=bob.id, source_key="twitter:1").count() == 1
    assert Node.query.filter_by(human_owner_id=bob.id).count() == 4
    assert UserProfile.query.filter_by(user_id=bob.id).count() == 4  # chain appended


def test_include_private_dumps_everything(app, tmp_path):
    _seed_source()
    out = tmp_path / "all.json"
    dump_user._run("alice", str(out), include_private=True)
    d = _read(out)
    assert len(d["nodes"]) == 4
    assert [n for n in d["nodes"] if n["privacy_level"] == "private"][0]["content"] == "my secret"


def test_format1_converts_and_loads(app, tmp_path):
    """A format-1 dump (single JSON document) converts to format 2 and loads."""
    import convert_user_dump
    _seed_source()
    out = tmp_path / "alice.jsonl"
    dump_user._run("alice", str(out))
    d = _read(out)
    nodes = d.pop("nodes")
    d["format"] = 1
    d["nodes"] = nodes
    legacy = tmp_path / "alice.json"
    legacy.write_text(json.dumps(d))
    converted = tmp_path / "alice2.jsonl"
    sys.argv = ["convert_user_dump.py", str(legacy), str(converted)]
    convert_user_dump.main()
    assert _read(converted)["nodes"] == nodes
    load_user._run(str(converted), "carol", merge=False, create_approved=False)
    assert Node.query.filter_by(human_owner_id=User.query.filter_by(username="carol").one().id).count() == 3


def test_twitter_id_is_set_and_guarded(app, tmp_path):
    _seed_source()
    out = tmp_path / "alice.jsonl"
    dump_user._run("alice", str(out))
    load_user._run(str(out), "bob", merge=False, create_approved=True, twitter_id=316970336)
    assert User.query.filter_by(username="bob").one().twitter_id == "316970336"
    # the same id can't be attached to a second account
    with pytest.raises(SystemExit):
        load_user._run(str(out), "dave", merge=False, create_approved=True, twitter_id="316970336")


def test_system_node_prompt_pin_round_trips(app, tmp_path):
    """An agentic thread root carries its pinned prompt as a reference:
    the dump stores the raw node text + the prompt by value, the loader
    recreates the prompt row for the target user and re-pins it, so the
    loaded root still resolves to the prompt and the Log skips it."""
    from backend.models import UserPrompt, NodeContextArtifact
    alice, root, child, cont, _, _ = _seed_source()
    prompt = UserPrompt(user_id=alice.id, prompt_key="textmode", title="Agentic",
                        generated_by="default")
    prompt.set_content("You are Loore. {user_profile}")
    db.session.add(prompt)
    db.session.flush()
    sysnode = Node(user_id=alice.id, human_owner_id=alice.id, node_type="user",
                   privacy_level="private", ai_usage="chat", token_count=1)
    sysnode.set_content("")  # system nodes hold no text of their own
    db.session.add(sysnode)
    db.session.flush()
    db.session.add(NodeContextArtifact(node_id=sysnode.id, artifact_type="prompt",
                                       artifact_id=prompt.id))
    reply = Node(user_id=alice.id, human_owner_id=alice.id, node_type="user",
                 parent_id=sysnode.id, privacy_level="private", ai_usage="chat", token_count=2)
    reply.set_content("the actual conversation")
    db.session.add(reply)
    db.session.commit()
    assert sysnode.is_system_prompt and sysnode.get_content().startswith("You are Loore")

    out = tmp_path / "alice.jsonl"
    dump_user._run("alice", str(out), include_private=True)
    d = _read(out)
    dumped = [n for n in d["nodes"] if n["prompt"]]
    assert len(dumped) == 1
    assert dumped[0]["content"] == ""                       # raw, not the resolved prompt
    assert dumped[0]["prompt"]["prompt_key"] == "textmode"
    assert dumped[0]["prompt"]["content"] == "You are Loore. {user_profile}"

    load_user._run(str(out), "bob", merge=False, create_approved=True)
    bob = User.query.filter_by(username="bob").one()
    loaded_root = Node.query.filter_by(human_owner_id=bob.id, parent_id=None).filter(
        Node.source_key.is_(None)).one()
    assert loaded_root.is_system_prompt
    assert loaded_root.get_content() == "You are Loore. {user_profile}"
    assert _raw(loaded_root.id) in ("", None)
    bob_prompt = UserPrompt.query.filter_by(user_id=bob.id, prompt_key="textmode").one()
    assert bob_prompt.get_content() == "You are Loore. {user_profile}"
    child_row = Node.query.filter_by(human_owner_id=bob.id, parent_id=loaded_root.id).one()
    assert child_row.get_content() == "the actual conversation"
    # second load onto bob reuses the prompt row rather than duplicating it
    load_user._run(str(out), "bob", merge=True, create_approved=True)
    assert UserPrompt.query.filter_by(user_id=bob.id, prompt_key="textmode").count() == 1
