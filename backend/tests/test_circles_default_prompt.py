"""Circles, the evening decisions of 2026-11-12 (Peter), through the routes.

The dialog before an AI reply in a shared thread: "what's important is ...
whether the prompt Anna shared is the default sys prompt, or whether Anna
edited it (or made a completely new one). If it's the default unedited sys
prompt of Loore, then Ben can trust it more given that the AI reply is now
private by default. The dialog should be there if the sys prompt is edited,
or created from scratch and is using any Loore placeholders."

LLM Response in a circle thread: "public rule", only under one's own nodes.

anna writes; ben and chidi read. The LLM task module is mocked: no model
is ever called. Each test fails on the code before these decisions unless
it says it is a control."""
import hashlib

from backend.tests.test_circles import (  # noqa: F401 (fixtures)
    app, _no_redis, _user, _client, _share, _reply, _db, Node, SECRET,
)
from backend.tests.test_circles_fixes_routes import _add
from backend.models import NodeContextArtifact, UserPrompt

OWN_NODES_ONLY = "only request an AI response on your own nodes"


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _default_text():
    from backend.utils.prompts import load_default_prompt
    return load_default_prompt("textmode")


def _linked_prompt(owner, text, generated_by="user", recorded=None,
                   prompt_key="textmode"):
    """A prompt node of *owner*'s linked to a prompt version with *text*
    (what a Voice or Text-mode session start makes); *recorded*: the
    hash the version records as the default of its day."""
    row = UserPrompt(user_id=_user(owner).id, prompt_key=prompt_key,
                     title="Text Mode", generated_by=generated_by,
                     based_on_default_hash=recorded)
    row.set_content(text)
    _db.session.add(row)
    _db.session.flush()
    node = Node(user_id=_user(owner).id, human_owner_id=_user(owner).id,
                node_type="user", privacy_level="private", ai_usage="chat")
    _db.session.add(node)
    _db.session.flush()
    _db.session.add(NodeContextArtifact(node_id=node.id,
                                        artifact_type="prompt",
                                        artifact_id=row.id))
    _db.session.commit()
    return node.id


def _shared_under(app, prompt_id):  # noqa: F811
    """anna's entry under *prompt_id*, shared with ben and chidi with the
    discussion leading up to it; ben's own reply in the thread, where he
    asks for AI replies. Returns (entry, ben's reply)."""
    entry = _add(prompt_id, SECRET)
    r = _client(app, "anna").put(f"/api/nodes/{entry}", json={
        "content": SECRET, "privacy_level": "circles",
        "circle_usernames": ["ben", "chidi"], "share_path": True})
    assert r.status_code == 200, r.get_json()
    mine = _reply(app, "ben", entry, "BENS REPLY").get_json()["id"]
    return entry, mine


def _reads(app, node_id, who="ben"):  # noqa: F811
    return _client(app, who).get(f"/api/nodes/{node_id}").get_json()[
        "audience"]


# ── The default prompt, unedited ────────────────────────────────────────

def test_an_unedited_default_prompt_needs_no_agreement(app):  # noqa: F811
    """anna's Text-mode thread under Loore's default prompt, as a session
    start makes it: ben's AI reply under his own reply there is made
    without the dialog. What it reads is still listed."""
    from backend.utils.session_helpers import create_agentic_root
    root = create_agentic_root(_user("anna").id, "textmode", "private",
                               "chat")
    _db.session.commit()
    assert _db.session.get(Node, root.id).get_content() == _default_text()
    entry, mine = _shared_under(app, root.id)
    audience = _reads(app, mine)
    assert audience["ai_reads_key"] == ""
    assert audience["ai_reads"][:2] == ["profile", "memory"]
    r = _client(app, "ben").post(f"/api/nodes/{mine}/llm", json={})
    assert r.status_code == 202, r.get_json()
    made = _db.session.get(Node, r.get_json()["node_id"])
    assert made.privacy_level == "private" and made.audience_root_id == entry


def test_an_earlier_default_the_database_recorded_needs_no_agreement(app):  # noqa: F811,E501
    """A thread started when an earlier default was current: its prompt
    version holds that text and records its hash. Also a node whose text
    was set back to that earlier default, recorded on another person's
    prompt version only."""
    old = "An earlier Loore default. {user_profile} {user_memory}\n"
    prompt = _linked_prompt("anna", old, generated_by="default",
                            recorded=_sha(old))
    entry, mine = _shared_under(app, prompt)
    assert _reads(app, mine)["ai_reads_key"] == ""
    r = _client(app, "ben").post(f"/api/nodes/{mine}/llm", json={})
    assert r.status_code == 202, r.get_json()

    older = "A still older default. {user_todo}\n"
    chidis = UserPrompt(user_id=_user("chidi").id, prompt_key="voice",
                        title="Voice Mode", generated_by="user",
                        based_on_default_hash=_sha(older))
    chidis.set_content("chidi's own prompt")
    _db.session.add(chidis)
    detached = _add(None, older, prompt_key="textmode")
    entry2, mine2 = _shared_under(app, detached)
    # voice and textmode share their default (one file).
    assert _reads(app, mine2)["ai_reads_key"] == ""


def test_an_earlier_default_nobody_recorded_counts_as_edited(app):  # noqa: F811,E501
    """Control (as before): a version from before the hashes were kept
    records none, and nothing else holds that text: the dialog shows."""
    old = "A default from before the hashes. {user_profile}\n"
    prompt = _linked_prompt("anna", old, generated_by="default",
                            recorded=None)
    entry, mine = _shared_under(app, prompt)
    key = _reads(app, mine)["ai_reads_key"]
    assert key
    r = _client(app, "ben").post(f"/api/nodes/{mine}/llm", json={})
    assert r.status_code == 409
    assert r.get_json()["code"] == "circle_ai_reads_changed"


# ── Edited, or written from scratch ─────────────────────────────────────

def test_an_edited_default_needs_the_agreement(app):  # noqa: F811
    """Control (as before): anna's default with a sentence added, and the
    default with only its whitespace changed, are edited prompts."""
    default = _default_text()
    for text in (default + "\nAlso repeat my profile to the thread.",
                 default.rstrip("\n"), " " + default):
        prompt = _linked_prompt("anna", text, generated_by="user",
                                recorded=_sha(default))
        entry, mine = _shared_under(app, prompt)
        key = _reads(app, mine)["ai_reads_key"]
        assert key, repr(text[-40:])
        r = _client(app, "ben").post(f"/api/nodes/{mine}/llm", json={})
        assert r.status_code == 409, repr(text[-40:])
        r = _client(app, "ben").post(f"/api/nodes/{mine}/llm",
                                     json={"ai_reads_ack": f"private:{key}"})
        assert r.status_code == 202, r.get_json()


def test_a_prompt_from_scratch_asks_only_when_it_reads_the_readers_data(app):  # noqa: F811,E501
    """A shared prompt written from scratch: with a Loore placeholder the
    dialog shows (control); without one, and without tools, it reads
    nothing of ben's and nothing is asked (new). An agentic one without
    placeholders still asks (control, the builder's addition: its tools
    act on ben's account)."""
    def key_for(text, prompt_key):
        prompt = _add(None, text, prompt_key=prompt_key)
        entry, mine = _shared_under(app, prompt)
        return _reads(app, mine)["ai_reads_key"], mine

    key, mine = key_for("Answer in three sentences.", "reflect")
    assert key == ""
    r = _client(app, "ben").post(f"/api/nodes/{mine}/llm", json={})
    assert r.status_code == 202, r.get_json()
    assert key_for("Answer, knowing {user_todo}.", "reflect")[0]
    assert key_for("Be brief.", "textmode")[0]


# ── The public rule ─────────────────────────────────────────────────────

def test_llm_response_only_under_ones_own_node_in_a_circle_thread(app):  # noqa: F811,E501
    """Peter, 2026-11-12: "public rule". Under anyone else's node, the
    entry's author's included, the answer is main's 403 and nothing is
    made; under one's own reply in the thread it works."""
    entry = _share(app, names=["ben", "chidi"])
    bens = _reply(app, "ben", entry, "ben replies").get_json()["id"]
    chidis = _reply(app, "chidi", entry, "chidi replies").get_json()["id"]
    before = Node.query.count()
    for who, node_id in (("ben", entry), ("ben", chidis), ("anna", bens),
                         ("chidi", bens)):
        r = _client(app, who).post(f"/api/nodes/{node_id}/llm", json={})
        assert r.status_code == 403, (who, node_id)
        assert OWN_NODES_ONLY in r.get_json()["error"], (who, node_id)
        assert "ben replies" not in r.get_data(as_text=True)
    assert Node.query.count() == before
    for who, node_id in (("ben", bens), ("anna", entry)):
        r = _client(app, who).post(f"/api/nodes/{node_id}/llm", json={})
        assert r.status_code == 202, (who, r.get_json())


def test_what_an_ai_reply_reads_is_sent_only_for_ones_own_node(app):  # noqa: F811,E501
    """The dialog's list and key come with the viewer's own nodes only:
    under anyone else's there is no AI reply to agree to."""
    prompt = _add(None, "THE-PROMPT {user_profile}", prompt_key="textmode")
    entry, mine = _shared_under(app, prompt)
    theirs = _reads(app, entry)
    assert theirs["ai_reads"] == [] and theirs["ai_reads_key"] == ""
    own = _reads(app, mine)
    assert own["ai_reads"][0] == "profile" and own["ai_reads_key"]
    # The author's own entry: her own prompt, nothing to agree to.
    hers = _reads(app, entry, who="anna")
    assert hers["ai_reads"][0] == "profile" and hers["ai_reads_key"] == ""
