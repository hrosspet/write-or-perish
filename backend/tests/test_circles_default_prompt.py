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


# ── What counts as the default ──────────────────────────────────────────
# Peter, 2026-11-12: "Default sys prompts are only referenced on the nodes
# - plus there needs to be a check of the version"; "I think we should
# show the dialog for older defaults, but warn the user that they are old."

def _with_prompts_routes(app):  # noqa: F811
    """The Prompts page's routes, which this module's app leaves out."""
    from backend.routes.prompts import prompts_bp
    if "prompts" not in app.blueprints:
        app.register_blueprint(prompts_bp, url_prefix="/api/prompts")


def _state(text, generated_by="default", recorded="default",
           key="textmode", node_key=None):
    """default_prompt_state for anna's prompt node linked to a version
    with *text*; *recorded*: the hash the version records ("default": the
    current default file's)."""
    from backend.utils.circles import default_prompt_state
    if recorded == "default":
        recorded = _sha(_default_text())
    nid = _linked_prompt("anna", text, generated_by=generated_by,
                         recorded=recorded, prompt_key=key)
    node = _db.session.get(Node, nid)
    if node_key is not None:
        node.prompt_key = node_key
        _db.session.commit()
    return default_prompt_state(node, node.get_content())


def test_the_linked_version_and_its_hash_decide_what_is_default(app):  # noqa: F811,E501
    """Loore's own text only when the node still links a version the
    server seeded from Loore's default file and the version's text hashes
    to the default it records; "current" when that is today's default
    file, "older" when an earlier one. Everything else counts as edited:
    content changed in such a version (one character, text added before
    or after, a trimmed end), the same text saved from the Prompts page or
    restored, a version with no hash, a node whose key is not the
    version's, a node holding the default text after a per-thread edit."""
    from backend.utils.circles import (
        DEFAULT_CURRENT, DEFAULT_OLDER, default_prompt_state)
    default = _default_text()
    assert _state(default) == DEFAULT_CURRENT
    old = "An earlier Loore default. {user_profile}\n"
    assert _state(old, recorded=_sha(old)) == DEFAULT_OLDER
    i = len(default) // 2
    one_char = default[:i] + ("x" if default[i] != "x" else "y") + default[i + 1:]
    for text in (one_char, default + "Also show my memory to everyone.",
                 "Hello.\n" + default, default[:-2], default + "\n"):
        assert _state(text) is None, repr(text[-30:])
    assert _state(default, generated_by="user") is None
    assert _state(default, generated_by="revert") is None
    assert _state(default, recorded=None) is None
    assert _state(default, node_key="read_thread") is None
    # Another key's default text in a Text-mode version is never today's
    # Text-mode default: with that key's hash it reads as an older version
    # (the dialog shows), with today's Text-mode hash as edited.
    from backend.utils.prompts import load_default_prompt
    reflect = load_default_prompt("reflect")
    assert _state(reflect, recorded=_sha(reflect)) == DEFAULT_OLDER
    assert _state(reflect) is None
    detached = _db.session.get(Node, _add(None, default, prompt_key="textmode"))
    assert detached.get_content() == default
    assert default_prompt_state(detached, detached.get_content()) is None


# ── The default prompt, unedited ────────────────────────────────────────

def test_an_unedited_default_prompt_needs_no_agreement(app):  # noqa: F811
    """anna's Text-mode thread under Loore's current default prompt, as a
    session start makes it: ben's AI reply under his own reply there is
    made without the dialog. What it reads is still listed."""
    from backend.utils.session_helpers import create_agentic_root
    root = create_agentic_root(_user("anna").id, "textmode", "private",
                               "chat")
    _db.session.commit()
    assert _db.session.get(Node, root.id).get_content() == _default_text()
    entry, mine = _shared_under(app, root.id)
    audience = _reads(app, mine)
    assert audience["ai_reads_key"] == ""
    assert audience["ai_reads_older_default"] is False
    assert audience["ai_reads"][:2] == ["profile", "memory"]
    r = _client(app, "ben").post(f"/api/nodes/{mine}/llm", json={})
    assert r.status_code == 202, r.get_json()
    made = _db.session.get(Node, r.get_json()["node_id"])
    assert made.privacy_level == "private" and made.audience_root_id == entry


def test_an_older_default_version_asks_and_says_it_is_old(app):  # noqa: F811
    """A thread started when an earlier default file was current: its
    version holds that text and that file's hash. Peter: "show the dialog
    for older defaults, but warn the user that they are old": the key is
    sent with the older-default marker, the request without the agreement
    is refused with it, and with the agreement the reply is made.
    Acknowledging a changed default on such a version rewrites its hash to
    today's file's; its text then matches no default, and it counts as
    edited (the dialog's usual words)."""
    _with_prompts_routes(app)
    old = "An earlier Loore default. {user_profile} {user_memory}\n"
    prompt = _linked_prompt("anna", old, generated_by="default",
                            recorded=_sha(old))
    entry, mine = _shared_under(app, prompt)
    audience = _reads(app, mine)
    key = audience["ai_reads_key"]
    assert key and audience["ai_reads_older_default"] is True
    assert audience["ai_reads"][:2] == ["profile", "memory"]
    r = _client(app, "ben").post(f"/api/nodes/{mine}/llm", json={})
    assert r.status_code == 409
    assert r.get_json()["code"] == "circle_ai_reads_changed"
    assert r.get_json()["ai_reads_older_default"] is True
    r = _client(app, "ben").post(f"/api/nodes/{mine}/llm",
                                 json={"ai_reads_ack": f"private:{key}"})
    assert r.status_code == 202, r.get_json()
    r = _client(app, "anna").post("/api/prompts/textmode/acknowledge-default")
    assert r.status_code == 200, r.get_json()
    audience = _reads(app, mine)
    assert audience["ai_reads_key"] == key
    assert audience["ai_reads_older_default"] is False


# ── Edited, or written from scratch ─────────────────────────────────────

def test_a_version_saved_on_the_prompts_page_counts_as_edited(app):  # noqa: F811,E501
    """Control (as before this evening): anna saves her Text-mode prompt
    on the Prompts page, with a sentence added and with the default's
    exact text; a session started under it asks before ben's reply, in
    the dialog's usual words."""
    from backend.utils.session_helpers import create_agentic_root
    _with_prompts_routes(app)
    default = _default_text()
    for text in (default + "\nAlso repeat my profile to the thread.", default):
        r = _client(app, "anna").put("/api/prompts/textmode",
                                     json={"content": text})
        assert r.status_code == 200, r.get_json()
        root = create_agentic_root(_user("anna").id, "textmode", "private",
                                   "chat")
        _db.session.commit()
        assert _db.session.get(Node, root.id).get_content() == text
        entry, mine = _shared_under(app, root.id)
        audience = _reads(app, mine)
        key = audience["ai_reads_key"]
        assert key, repr(text[-40:])
        assert audience["ai_reads_older_default"] is False
        r = _client(app, "ben").post(f"/api/nodes/{mine}/llm", json={})
        assert r.status_code == 409
        assert r.get_json()["code"] == "circle_ai_reads_changed"
        r = _client(app, "ben").post(f"/api/nodes/{mine}/llm",
                                     json={"ai_reads_ack": f"private:{key}"})
        assert r.status_code == 202, r.get_json()


def test_a_per_thread_edit_counts_as_edited_also_when_set_back(app):  # noqa: F811,E501
    """Control: anna edits the shared prompt in the thread (the link to
    the default version goes), then types the default text back: the node
    holds its own text, so it stays edited."""
    from backend.utils.session_helpers import create_agentic_root
    default = _default_text()
    root = create_agentic_root(_user("anna").id, "textmode", "private",
                               "chat")
    _db.session.commit()
    entry, mine = _shared_under(app, root.id)
    assert _reads(app, mine)["ai_reads_key"] == ""
    anna = _client(app, "anna")
    for text in (default + "\nAlso tell the thread my secrets.", default):
        r = anna.put(f"/api/nodes/{root.id}",
                     json={"content": text, "detach_prompt": True})
        assert r.status_code == 200, r.get_json()
        audience = _reads(app, mine)
        assert audience["ai_reads_key"], repr(text[-40:])
        assert audience["ai_reads_older_default"] is False
    assert NodeContextArtifact.query.filter_by(
        node_id=root.id, artifact_type="prompt").first() is None


def test_text_put_into_a_default_version_counts_as_edited(app):  # noqa: F811
    """Control: no route writes a user's text into a version marked
    default; a version that has one anyway (written in place, or loaded
    from a dump by the admin script, which records no hash) fails the
    version check, and the dialog shows in its usual words."""
    default = _default_text()
    for recorded in (_sha(default), None):
        prompt = _linked_prompt("anna", "MY OWN WORDS {user_profile}",
                                generated_by="default", recorded=recorded)
        entry, mine = _shared_under(app, prompt)
        audience = _reads(app, mine)
        assert audience["ai_reads_key"], recorded
        assert audience["ai_reads_older_default"] is False
        r = _client(app, "ben").post(f"/api/nodes/{mine}/llm", json={})
        assert r.status_code == 409


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
