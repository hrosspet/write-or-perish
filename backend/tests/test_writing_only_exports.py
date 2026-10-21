"""Profile builds, intentions and poll drafts read the user's writing only
(#550).

Their export used to start with a preamble holding every system prompt,
profile version and todo list the AI replies in the window were written
against, and each AI session's system node showed its prompt as a
reference line. A profile built from that restated earlier profiles as
facts of the new window. Now these jobs build their export with
``omit_artifacts=True``: no preamble, no reference line, no system-prompt
node (also not one whose prompt the user edited), and a thread that is only
a system prompt is left out. The replies and the user's entries render as
before.

The other callers keep their output: the account export (full preamble),
budgeted ``{user_export}`` (preamble), recent context and the raw window
(reference lines, no preamble).

In-memory SQLite, ENCRYPTION_DISABLED, celery mocked so the task modules
import; the export runs for real, the model calls are scripted.
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
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    Node, NodeContextArtifact, User, UserArtifact, UserProfile, UserPrompt,
    UserTodo,
)

MODEL = {"provider": "anthropic", "api_model": "claude-x",
         "context_window": 1_000_000, "tokenizer_family": "claude_new",
         "input_price_per_mtok": 5.0, "output_price_per_mtok": 25.0}

T0 = datetime(2026, 4, 10, 9, 0, 0)
CUTOFF = datetime(2026, 4, 1)

PROMPT_TEXT = ("SYSTEMPROMPTTEXT you are a reflective coach. "
               "{user_profile} {user_todo} {user_memory}")
PINNED_PROFILE = "PINNEDPROFILETEXT the user loves sailing"
TODO = "TODOLISTTEXT renew the passport"
MEMORY = "MEMORYTEXT likes tea"
EDITED_PROMPT = "EDITEDPROMPTTEXT be terse"
BASE_PROFILE = "BASEPROFILETEXT"

WRITING = ("ENTRYONE", "ENTRYTWO", "ENTRYTHREE", "PLAINENTRY",
           "AIREPLYONE", "AIANSWERONE")
ARTIFACT_TEXTS = (PROMPT_TEXT, PINNED_PROFILE, TODO, MEMORY, EDITED_PROMPT)
ARTIFACT_MARKS = ("Referenced", "[System Prompt", "[User Profile",
                  "[User TODO", "[Artifact '", "(ref #", "\n===\n")

UPDATE_TEMPLATE = ("U {existing_profile}\n{new_data}|{source_tokens_past}"
                   "|{source_tokens_new}|{ratio_percent}")


@pytest.fixture
def app(monkeypatch):
    # Warm celery_app first: it resolves backend.tasks.exports and
    # backend.tasks.profile_batch in the safe import order (see
    # test_profile_regen_resume).
    import backend.celery_app  # noqa: F401
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SUPPORTED_MODELS"] = {"m": dict(MODEL)}
    app.config["DEFAULT_LLM_MODEL"] = "m"
    _db.init_app(app)
    import backend.llm_providers as lp
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda model_id, messages, keys: None))
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def frozen_export_time(monkeypatch):
    """The export header carries its render time to the second; prompts
    compared byte for byte must see the same clock."""
    import backend.routes.export_data as export_data

    class _Frozen(datetime):
        @classmethod
        def utcnow(cls):
            return datetime(2026, 10, 20, 12, 0, 0)

    monkeypatch.setattr(export_data, "datetime", _Frozen)


def _node(author, owner, parent, text, when, tokens=50, llm=False):
    n = Node(user_id=author.id, human_owner_id=owner.id,
             parent_id=parent.id if parent else None,
             node_type="llm" if llm else "user",
             llm_model="gpt-5" if llm else None,
             privacy_level="private", ai_usage="chat",
             created_at=when, updated_at=when, token_count=tokens)
    n.set_content(text)
    _db.session.add(n)
    _db.session.flush()
    return n


def _corpus():
    """Alice's writing after CUTOFF, in four threads:

    1. an agentic session: its system node (the prompt, which pins a
       profile, the todo list and the memory), her entry, an AI reply
       that read the todo list (pinned on it), the answer, her next entry;
    2. a session whose system prompt she edited (the link is gone, the
       prompt_key stamp and the edited prompt text stay), then her entry;
    3. a session with nothing but its system node;
    4. a plain entry.

    Plus the profile the next update builds on (BASE_PROFILE)."""
    from backend.utils.context_artifacts import attach_context_artifacts
    alice = User(username="alice", plan="alpha", approved=True,
                 twitter_id=None, preferred_model="m")
    llm = User(username="gpt-5", plan="alpha", approved=True,
               twitter_id="llm-gpt-5")
    _db.session.add_all([alice, llm])
    _db.session.flush()

    pinned = UserProfile(user_id=alice.id, generated_by="m", ai_usage="chat",
                         created_at=datetime(2026, 3, 1))
    pinned.set_content(PINNED_PROFILE)
    todo = UserTodo(user_id=alice.id, generated_by="user", ai_usage="chat")
    todo.set_content(TODO)
    memory = UserArtifact(user_id=alice.id, kind="memory", title="Memory",
                          generated_by="user", ai_usage="chat")
    memory.set_content(MEMORY)
    prompt = UserPrompt(user_id=alice.id, prompt_key="textmode",
                        title="Textmode", generated_by="default")
    prompt.set_content(PROMPT_TEXT)
    _db.session.add_all([pinned, todo, memory, prompt])
    _db.session.flush()

    system = _node(alice, alice, None, "", T0, tokens=0)
    attach_context_artifacts(system.id, alice.id, prompt_record=prompt)
    entry1 = _node(alice, alice, system, "ENTRYONE my own words",
                   T0 + timedelta(minutes=1))
    reply = _node(llm, alice, entry1, "AIREPLYONE let me look at the list",
                  T0 + timedelta(minutes=2), llm=True)
    _db.session.add(NodeContextArtifact(
        node_id=reply.id, artifact_type="todo", artifact_id=todo.id))
    answer = _node(llm, alice, reply, "AIANSWERONE here is what I see",
                   T0 + timedelta(minutes=3), llm=True)
    _node(alice, alice, answer, "ENTRYTWO thanks", T0 + timedelta(minutes=4))

    edited = _node(alice, alice, None, EDITED_PROMPT, T0 + timedelta(hours=1))
    edited.prompt_key = "voice"
    _node(alice, alice, edited, "ENTRYTHREE spoken",
          T0 + timedelta(hours=1, minutes=1))

    lone = _node(alice, alice, None, "", T0 + timedelta(hours=2), tokens=0)
    attach_context_artifacts(lone.id, alice.id, prompt_record=prompt)

    _node(alice, alice, None, "PLAINENTRY a note", T0 + timedelta(hours=3))

    base = UserProfile(user_id=alice.id, generated_by="m", ai_usage="chat",
                       generation_type="update", source_tokens_used=100_000,
                       source_data_cutoff=CUTOFF,
                       created_at=datetime(2026, 4, 5))
    base.set_content(BASE_PROFILE)
    _db.session.add(base)
    _db.session.commit()
    return alice, base


def _assert_writing_only(text):
    for word in WRITING:
        assert word in text, word
    for artifact in ARTIFACT_TEXTS:
        assert artifact.split()[0] not in text, artifact
    for mark in ARTIFACT_MARKS:
        assert mark not in text, mark


# ── Profile builds: the sync loop and the batch builder ──────────────────

def _sync_prompt(user, base, monkeypatch):
    import backend.tasks.exports as exports
    prompts = []

    def llm(task, model_id, prompt, user_id, keys, **k):
        prompts.append(prompt)
        return {"content": "NEW PROFILE", "input_tokens": 1000,
                "output_tokens": 5, "total_tokens": 1005}

    monkeypatch.setattr(exports, "_call_llm_with_retries", llm)
    exports._chunked_profile_loop(
        MagicMock(), user, "m", update_template=UPDATE_TEMPLATE, api_keys={},
        initial_profile_content=base.get_content(),
        initial_profile_id=base.id,
        initial_source_tokens=base.source_tokens_used,
        initial_cutoff=base.source_data_cutoff, generation_type="update")
    assert len(prompts) == 1
    return prompts[0]


def _batch_prompt(user, monkeypatch):
    import backend.tasks.profile_batch as pb
    monkeypatch.setattr(pb, "get_api_keys_for_usage",
                        lambda *a, **k: {"anthropic": "k"})
    monkeypatch.setattr(pb._exports, "build_update_template",
                        lambda uid: UPDATE_TEMPLATE)
    req = pb._build_next_profile_request(user)
    assert req["meta"]["kind"] == "chunk"
    return req["request"]["messages"][0]["content"][0]["text"]


def test_a_sync_profile_update_reads_only_the_writing(app, monkeypatch):
    alice, base = _corpus()

    prompt = _sync_prompt(alice, base, monkeypatch)

    _assert_writing_only(prompt)
    assert prompt.startswith(f"U {BASE_PROFILE}\n")  # the base stays
    # Threads 1, 2 and 4; the lone system node is left out.
    assert "**Entry Points:** 3" in prompt
    assert "# Thread 4" not in prompt


def test_a_batch_profile_update_reads_only_the_writing(app, monkeypatch):
    alice, _base = _corpus()

    prompt = _batch_prompt(alice, monkeypatch)

    _assert_writing_only(prompt)
    assert prompt.startswith(f"U {BASE_PROFILE}\n")


def test_sync_and_batch_send_the_same_bytes(app, monkeypatch,
                                            frozen_export_time):
    alice, base = _corpus()

    batch = _batch_prompt(alice, monkeypatch)
    sync = _sync_prompt(alice, base, monkeypatch)

    assert sync == batch


def test_the_replies_keep_their_place_in_the_thread(app):
    """The system node renders nothing; its replies keep their index
    paths, so the thread reads as the session it was."""
    from backend.tasks.exports import render_profile_window
    alice, _base = _corpus()

    content = render_profile_window(alice, 100_000, CUTOFF)["content"]

    thread1 = content[content.index("# Thread 1"):content.index("# Thread 2")]
    assert "[1]" not in thread1
    assert thread1.index("[1.1] User (alice)") < thread1.index("ENTRYONE")
    assert "[1.1.1] AI (gpt-5)" in thread1
    assert thread1.index("ENTRYONE") < thread1.index("AIREPLYONE") \
        < thread1.index("AIANSWERONE") < thread1.index("ENTRYTWO")


def _old_profile_window(user, units, cutoff):
    """The profile window as it was built before #550."""
    from backend.routes.export_data import build_user_export_content
    return build_user_export_content(
        user, max_tokens=int(units), filter_ai_usage=True,
        created_after=cutoff, chronological_order=True,
        return_metadata=True, include_strategy="engaged_threads")


@pytest.mark.parametrize("units", [200, 300, 100_000])
def test_the_window_selects_the_same_rows(app, units):
    """The unit budget never counted the preamble's profiles and todo
    lists, and a system node counts its stored units either way: the
    same budget selects the same rows and the same resume cursor."""
    from backend.tasks.exports import render_profile_window
    alice, _base = _corpus()

    new = render_profile_window(alice, units, CUTOFF)
    old = _old_profile_window(alice, units, CUTOFF)

    for key in ("node_ids", "unit_count", "latest_node_created_at",
                "origin_stats"):
        assert new[key] == old[key], key
    assert len(new["content"]) < len(old["content"])


def test_every_selected_entry_is_rendered(app):
    """A 200-unit window (100 after the header allowance) selects the
    system node (0 units), ENTRYONE and AIREPLYONE (50 each) and moves the
    cursor past the reply. The old build also counted the system node's
    reference line, so the reply no longer fitted: it was left out of
    the text, and no later window read it."""
    from backend.tasks.exports import render_profile_window
    alice, _base = _corpus()

    new = render_profile_window(alice, 200, CUTOFF)
    old = _old_profile_window(alice, 200, CUTOFF)

    assert new["latest_node_created_at"] == T0 + timedelta(minutes=2)
    assert "ENTRYONE" in new["content"] and "AIREPLYONE" in new["content"]
    assert "AIREPLYONE" not in old["content"]


def test_a_window_of_system_prompts_only_renders_nothing(app):
    """A window holding nothing but a system node gives the model
    nothing to read: the builder reports no data, as for an empty
    window."""
    from backend.tasks.exports import render_profile_window
    alice, _base = _corpus()

    after_plain = T0 + timedelta(hours=3, minutes=1)
    lone = _node(alice, alice, None, "", after_plain + timedelta(hours=1),
                 tokens=0)
    _db.session.add(NodeContextArtifact(
        node_id=lone.id, artifact_type="prompt",
        artifact_id=UserPrompt.query.first().id))
    _db.session.commit()

    assert render_profile_window(alice, 100_000, after_plain) is None


@pytest.mark.parametrize("kwargs", [
    dict(),
    dict(max_tokens=100_000),
])
def test_the_authored_threads_path_omits_them_too(app, kwargs):
    from backend.routes.export_data import build_user_export_content
    alice, _base = _corpus()

    content = build_user_export_content(
        alice, filter_ai_usage=True, omit_artifacts=True, **kwargs)

    _assert_writing_only(content)


# ── Intentions and poll drafts ───────────────────────────────────────────

def test_intentions_read_only_the_writing(app):
    import backend.tasks.intentions as it
    alice, _base = _corpus()
    template, budget, chronological = it._template_and_params()

    messages, export, _tokens = it._build_messages(
        alice, template, budget or 100_000, chronological)

    _assert_writing_only(export)
    text = messages[0]["content"][0]["text"]
    assert export in text
    for artifact in ARTIFACT_TEXTS:
        assert artifact.split()[0] not in text


def test_a_poll_draft_reads_only_the_writing(app):
    import backend.tasks.poll_draft as pd
    alice, _base = _corpus()

    context = pd._recent_window_context(alice, dict(MODEL))

    assert context.startswith("## The user's recent writing")
    _assert_writing_only(context)


# ── The other callers are unchanged ──────────────────────────────────────

def test_the_account_export_keeps_the_prompts_and_artifacts(app):
    """The user's own download shows everything, as before."""
    from backend.routes.export_data import build_user_export_content
    alice, _base = _corpus()

    content = build_user_export_content(alice, filter_ai_usage=False)

    for text in ARTIFACT_TEXTS + WRITING:
        assert text.split()[0] in content, text
    assert "[System Prompt: Textmode v1 (ref #" in content
    assert "[User TODO v1 (ref #" in content
    assert "**Total Threads:** 4" in content


def test_a_budgeted_user_export_keeps_its_preamble(app):
    """{user_export} (no collapse, no omit) still gets the referenced
    prompt, profile and todo list in full."""
    from backend.routes.export_data import build_user_export_content
    alice, _base = _corpus()

    content = build_user_export_content(
        alice, max_tokens=100_000, filter_ai_usage=True,
        include_strategy="engaged_threads")

    assert "## System Prompts Referenced" in content
    assert "## User Profiles Referenced" in content
    assert "## User TODOs Referenced" in content
    for text in (PROMPT_TEXT, PINNED_PROFILE, TODO, EDITED_PROMPT):
        assert text in content
    assert "**Entry Points:** 4" in content


def test_recent_context_keeps_its_reference_lines(app):
    import backend.tasks.recent_context as rc
    alice, _base = _corpus()

    content = rc._build_recent_export(alice, CUTOFF)["content"]

    assert "[System Prompt: Textmode v1 (ref #" in content
    assert "[User Profile v1 (ref #" in content
    assert "[User TODO v1 (ref #" in content
    assert "[Artifact 'memory' v1 (ref #" in content
    assert "Referenced" not in content
    assert PINNED_PROFILE not in content and TODO not in content
    # Unchanged: the edited prompt is an ordinary node there, and the
    # lone system node is still a thread.
    assert EDITED_PROMPT in content
    assert "**Entry Points:** 4" in content
