"""Regression test for fix 2b: a from-scratch full profile regen clears
``profile_needs_full_regen`` after the FIRST committed chunk.

Why it matters: full regen rebuilds the profile in chronological ~90k-token
chunks, each committed independently. If the run later times out, the flag
must already be off so the next heartbeat resumes *incrementally* from the
last saved chunk instead of restarting the whole rebuild from zero (the
behavior that made user 44 burn cost forever without finishing).

Patterned after the other task tests: in-memory SQLite, ENCRYPTION_DISABLED,
celery mocked so the module imports. Only ``@celery.task`` entry points
become mocks — the plain helper ``_chunked_profile_loop`` under test stays
real, with its LLM / export calls monkeypatched.
"""
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

# ── Environment ──────────────────────────────────────────────────────────
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

from backend.extensions import db as _db          # noqa: E402
from backend.models import User, UserProfile      # noqa: E402


@pytest.fixture
def app():
    # Warm celery_app first (lazily, at run time) so it resolves
    # backend.tasks.exports then backend.tasks.profile_batch in the safe order.
    # Importing exports directly as the first module trips the
    # exports <-> profile_batch import cycle; doing this at module top instead
    # breaks full-suite collection. So warm it here, in the fixture.
    import backend.celery_app  # noqa: F401
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    # _save_profile -> calculate_llm_cost_microdollars reads this; empty
    # dict makes the unknown model cost 0 without a KeyError.
    app.config["SUPPORTED_MODELS"] = {}
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def test_full_regen_clears_flag_after_first_chunk(app, monkeypatch):
    # Importing here (inside the app context) keeps the celery-mock + create_app
    # side effects scoped, matching the other task tests.
    import backend.tasks.exports as exports

    user = User(username="deep_user", plan="alpha", twitter_id=None,
                approved=True, profile_needs_full_regen=True)
    _db.session.add(user)
    _db.session.commit()

    # One chunk of source data, then the export is exhausted. The user has
    # no nodes, so the loop's has_more check also stops it after chunk 1.
    chunk = {
        "content": "the user's oldest writing",
        "token_count": 90000,
        "unit_count": 90000,
        "latest_node_created_at": datetime(2025, 2, 11, 10, 26, 14),
    }
    monkeypatch.setattr(exports, "build_user_export_content",
                        MagicMock(side_effect=[chunk, None]))
    # The planner's remainder: more than one chunk's worth (so chunk 1 is
    # an iterative root, not a single-chunk "initial"), then nothing.
    monkeypatch.setattr(exports, "count_remaining_units",
                        MagicMock(side_effect=[200_000, 0]))
    import backend.llm_providers as lp
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda m, msgs, k: None))
    monkeypatch.setattr(exports, "_call_llm_with_retries",
                        MagicMock(return_value={
                            "content": "PROFILE v1",
                            "input_tokens": 1000,
                            "output_tokens": 500,
                            "total_tokens": 1500,
                        }))

    fake_task = MagicMock()  # supplies .update_state(...)

    profile_id, chunk_num, _ = exports._chunked_profile_loop(
        fake_task, user, "gpt-5.5", update_template="{new_data}",
        api_keys={},
        first_chunk_prompt_fn=lambda c: "GEN PROMPT",
        initial_profile_content=None,
        generation_type="iterative",
    )

    # The whole point of fix 2b: flag is off after the first committed chunk.
    assert user.profile_needs_full_regen is False
    # And that first chunk really was persisted (so a resume has an anchor).
    assert chunk_num == 1
    saved = UserProfile.query.get(profile_id)
    assert saved is not None
    assert saved.generation_type == "iterative"


def test_incremental_update_null_cutoff_uses_existing_base_with_note(app, monkeypatch):
    """A user-written (null-cutoff) profile must NOT crash and must NOT be
    discarded by full regen — it's used as the incremental base, annotated so
    the LLM knows it's the user's own words."""
    import backend.tasks.exports as exports
    from backend.models import Node

    user = User(username="nullcut", plan="alpha", twitter_id=None,
                approved=True)
    _db.session.add(user)
    _db.session.flush()
    prev = UserProfile(
        user_id=user.id, generated_by="user", tokens_used=0,
        generation_type="initial", source_tokens_used=0,
        source_data_cutoff=None,
    )
    prev.set_content("USER-WRITTEN PROFILE")
    _db.session.add(prev)
    node = Node(user_id=user.id, node_type="user", ai_usage="chat")
    node.set_content("some recent writing")
    _db.session.add(node)
    _db.session.commit()

    captured = {}

    def fake_loop(*a, **kw):
        captured["base"] = kw.get("initial_profile_content")
        return (prev.id, 1, 0)
    monkeypatch.setattr(exports, "_chunked_profile_loop", fake_loop)
    full = MagicMock()
    monkeypatch.setattr(exports, "_do_initial_generation", full)

    exports._do_incremental_update(
        MagicMock(), user, "gpt-5.5", prev.id,
        context_window=200000, max_output_tokens=10000, api_keys={})

    full.assert_not_called()                            # NOT full regen
    assert "USER-WRITTEN PROFILE" in captured["base"]   # existing kept as base
    assert "written by the user" in captured["base"]    # annotated as user-written


def test_integration_annotates_user_written_chain_root(app):
    """The integration chain root can be the user's hand-written profile — it
    must be flagged as user-written; generated versions must not be."""
    import backend.tasks.exports as exports
    from datetime import datetime

    user = User(username="iu", plan="alpha", twitter_id=None, approved=True)
    _db.session.add(user)
    _db.session.flush()
    p1 = UserProfile(user_id=user.id, generated_by="user", tokens_used=0,
                     generation_type="initial", source_tokens_used=0,
                     source_data_cutoff=None)
    p1.set_content("USER BASE PROFILE")
    _db.session.add(p1)
    _db.session.flush()
    p2 = UserProfile(user_id=user.id, generated_by="gpt-5.5", tokens_used=0,
                     generation_type="update", source_tokens_used=1000,
                     source_data_cutoff=datetime(2026, 6, 1),
                     parent_profile_id=p1.id)
    p2.set_content("GENERATED UPDATE PROFILE")
    _db.session.add(p2)
    _db.session.commit()

    messages, chain = exports.build_integration_messages(user.id, p2.id)
    assert messages is not None and len(chain) == 2
    texts = [m["content"][0]["text"] for m in messages]
    user_msg = next(t for t in texts if "USER BASE PROFILE" in t)
    gen_msg = next(t for t in texts if "GENERATED UPDATE PROFILE" in t)
    assert "written by the user" in user_msg        # root flagged
    assert "written by the user" not in gen_msg      # generated not flagged


def test_save_profile_inherits_user_default_ai_usage(app):
    """Generated profiles take ai_usage from the user's global default, not a
    hardcoded 'chat' (#191). Combined with the profile_eligible_query gate, a
    'train' user gets a 'train' profile (and opted-out users never generate)."""
    import backend.tasks.exports as exports

    user = User(username="trainer", plan="alpha", twitter_id=None,
                approved=True, default_ai_usage="train")
    _db.session.add(user)
    _db.session.commit()

    response = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    profile = exports._save_profile(
        user, "gpt-5.5", "GENERATED PROFILE", response,
        source_tokens_used=0, source_data_cutoff=None,
        generation_type="initial",
    )

    assert profile.ai_usage == "train"


def test_revert_profile_copies_reverted_to_ai_usage(app):
    """A revert reproduces a prior profile version, so it carries that
    version's ai_usage rather than a fresh default (#191)."""
    import backend.tasks.exports as exports

    user = User(username="reverter", plan="alpha", twitter_id=None,
                approved=True)
    _db.session.add(user)
    _db.session.flush()
    # Older valid profile (train), cutoff before the import boundary.
    valid = UserProfile(
        user_id=user.id, generated_by="gpt-5.5", tokens_used=0,
        generation_type="update", source_tokens_used=100,
        source_data_cutoff=datetime(2026, 1, 1), ai_usage="train",
    )
    valid.set_content("VALID TRAIN PROFILE")
    _db.session.add(valid)
    _db.session.flush()
    # Newer profile (chat) that included since-removed imported data.
    newer = UserProfile(
        user_id=user.id, generated_by="gpt-5.5", tokens_used=0,
        generation_type="update", source_tokens_used=200,
        source_data_cutoff=datetime(2026, 3, 1), ai_usage="chat",
        parent_profile_id=valid.id,
    )
    newer.set_content("NEWER PROFILE")
    _db.session.add(newer)
    _db.session.commit()

    # Import boundary between the two cutoffs -> revert target is `valid`.
    exports.revert_profile_for_import(user.id, datetime(2026, 2, 1))
    _db.session.commit()

    revert = UserProfile.query.filter_by(
        user_id=user.id, generation_type="revert").first()
    assert revert is not None
    assert revert.ai_usage == "train"   # copied from the reverted-to version


def _seed_null_cutoff_user(username, node_tokens):
    """A user whose only profile is hand-written (null cutoff), plus one old
    node carrying ``node_tokens``. Old timestamps so the inactivity (30m) and
    interval (1h) gates both pass, isolating the token-threshold decision."""
    from backend.models import Node
    user = User(username=username, plan="alpha", twitter_id=None, approved=True)
    _db.session.add(user)
    _db.session.flush()
    prof = UserProfile(
        user_id=user.id, generated_by="user", tokens_used=0,
        generation_type="initial", source_tokens_used=0,
        source_data_cutoff=None, created_at=datetime(2025, 1, 1),
    )
    prof.set_content("USER-WRITTEN")
    _db.session.add(prof)
    node = Node(user_id=user.id, node_type="user", ai_usage="chat",
                token_count=node_tokens, created_at=datetime(2025, 1, 2),
                updated_at=datetime(2025, 1, 2))
    node.set_content("writing")
    _db.session.add(node)
    _db.session.commit()
    return user


def test_null_cutoff_low_data_does_not_trigger(app, monkeypatch):
    """A hand-written (null-cutoff) profile with <80k tokens of data must NOT
    force-trigger an update — the heartbeat now measures the real data instead
    of sentinelling a null cutoff straight to the threshold."""
    import backend.tasks.exports as exports
    import backend.tasks.profile_batch as pb
    monkeypatch.setattr(pb, "use_batch_for_user", lambda *a, **k: False)

    user = _seed_null_cutoff_user("nulllow", node_tokens=2884)

    called = {}
    monkeypatch.setattr(exports, "maybe_trigger_profile_update",
                        lambda *a, **k: called.setdefault("yes", (a, k)))

    result = exports.maybe_trigger_incremental_profile_update(user)
    assert result is None
    assert "yes" not in called          # did NOT trigger despite the null cutoff


def test_null_cutoff_high_data_still_triggers(app, monkeypatch):
    """A hand-written (null-cutoff) profile WITH >=80k tokens still triggers, so
    the base eventually gets folded into a data-grounded profile."""
    import backend.tasks.exports as exports
    import backend.tasks.profile_batch as pb
    monkeypatch.setattr(pb, "use_batch_for_user", lambda *a, **k: False)

    user = _seed_null_cutoff_user("nullhigh", node_tokens=90000)

    called = {}
    monkeypatch.setattr(exports, "maybe_trigger_profile_update",
                        lambda *a, **k: called.setdefault("yes", (a, k)))

    exports.maybe_trigger_incremental_profile_update(user)
    assert "yes" in called              # crossed threshold -> triggered
    assert called["yes"][0][0] == user.id


# ── #183: a full rebuild keeps the user's own profile ───────────────────
# Decision (voice review, 2026-10-02): a job that regenerates the profile
# keeps the user's hand-written text and refreshes the rest. The rebuild
# reads the writing oldest first and folds the user's profile in at its own
# date, so older writing is not read through it.

JAN, JUN, DEC = datetime(2025, 1, 1), datetime(2025, 6, 1), datetime(2025, 12, 1)
MARKERS = ("OLD DATA", "NEW DATA", "MY OWN WORDS")


def _user_written(user, content="MY OWN WORDS", created_at=JUN,
                  ai_usage="chat", parent=None):
    profile = UserProfile(
        user_id=user.id, generated_by="user", tokens_used=0,
        generation_type="initial", source_tokens_used=0,
        source_data_cutoff=None, ai_usage=ai_usage, created_at=created_at,
        parent_profile_id=parent.id if parent else None)
    profile.set_content(content)
    _db.session.add(profile)
    _db.session.commit()
    return profile


def _generated(user, cutoff, parent=None, gen_type="iterative"):
    profile = UserProfile(
        user_id=user.id, generated_by="gpt-5.5", tokens_used=0,
        generation_type=gen_type, source_tokens_used=90_000,
        source_data_cutoff=cutoff,
        parent_profile_id=parent.id if parent else None)
    profile.set_content("GENERATED")
    _db.session.add(profile)
    _db.session.commit()
    return profile


def _new_user(name):
    user = User(username=name, plan="alpha", twitter_id=None, approved=True)
    _db.session.add(user)
    _db.session.commit()
    return user


def _window(content, latest):
    return {"content": content, "token_count": 90_000, "unit_count": 90_000,
            "latest_node_created_at": latest}


def _echo(prompt_text):
    """A stand-in model that keeps every marker it was shown — the way a
    real update keeps what still holds — so a test can see what reached
    the final profile."""
    return " ".join(m for m in MARKERS if m in prompt_text)


def _rebuild_two_windows(exports, monkeypatch, windows):
    """Wire the sync loop to two windows: OLD DATA up to January, NEW DATA
    up to December. ``windows`` maps the window's cutoff to its chunk."""
    remaining = {None: 180_000, JAN: 90_000, DEC: 0}
    monkeypatch.setattr(exports, "count_remaining_units",
                        lambda uid, cutoff=None: remaining.get(cutoff, 0))
    monkeypatch.setattr(
        exports, "build_user_export_content",
        lambda user, max_tokens=None, **kw: windows.get(kw.get("created_after")))
    monkeypatch.setattr(exports, "should_continue_chain", lambda u, p: True)
    import backend.llm_providers as lp
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda m, msgs, k: None))

    def no_provider(*a, **k):
        raise AssertionError("a test reached the real LLM provider")
    # Nothing may reach a provider; a test that needs a model patches in
    # its stand-in over this.
    monkeypatch.setattr(lp.LLMProvider, "get_completion",
                        staticmethod(no_provider))
    monkeypatch.setattr(exports, "get_api_keys_for_usage", lambda *a, **k: {})
    prompts = []

    def fake_call(self_, model_id, prompt, uid, keys, **kw):
        prompts.append(prompt)
        return {"content": _echo(prompt), "input_tokens": 10,
                "output_tokens": 5, "total_tokens": 15}
    monkeypatch.setattr(exports, "_call_llm_with_retries", fake_call)
    return prompts


def test_183_full_rebuild_keeps_user_profile_and_integrates_new_writing(
        app, monkeypatch):
    """A hand-written profile survives a full rebuild: it joins the window
    that reaches its date (not the older one), the integration sees it, and
    the rebuilt profile holds the user's words and the new writing."""
    import backend.tasks.exports as exports

    user = _new_user("rebuild183")
    _user_written(user)
    prompts = _rebuild_two_windows(exports, monkeypatch, {
        None: _window("OLD DATA", JAN), JAN: _window("NEW DATA", DEC)})
    monkeypatch.setattr(exports, "build_update_template",
                        lambda uid: "UPDATE {existing_profile} || {new_data}")
    monkeypatch.setattr(exports, "_load_prompt",
                        lambda name, user_id=None: "INTEGRATE {N_MONTHS}")
    integration = []

    def fake_completion(model_id, messages, keys, **kw):
        text = "\n".join(m["content"][0]["text"] for m in messages)
        integration.append(messages)
        return {"content": _echo(text), "input_tokens": 10,
                "output_tokens": 5, "total_tokens": 15}
    monkeypatch.setattr(exports.LLMProvider, "get_completion",
                        staticmethod(fake_completion))

    result = exports._iterative_generation(
        MagicMock(), user, "gpt-5.5", "GEN {user_export}", 10_000, {})

    assert len(prompts) == 2
    # January's window predates the profile: read without it.
    assert "MY OWN WORDS" not in prompts[0]
    # December's window reaches June: the profile joins it, dated.
    assert "MY OWN WORDS" in prompts[1]
    assert "written by the user themselves on 2025-06-01" in prompts[1]
    # The integration shows the profile itself, between the two versions.
    texts = [m["content"][0]["text"] for m in integration[0]]
    assert "MY OWN WORDS" in texts[1] and "2025-06-01" in texts[1]
    assert texts[0].startswith("Profile No. 1") and "MY OWN WORDS" not in texts[0]
    final = UserProfile.query.get(result["profile_id"])
    assert final.generation_type == "integration"
    for marker in MARKERS:
        assert marker in final.get_content()


def test_183_profile_newer_than_all_writing_goes_into_the_final_window(
        app, monkeypatch):
    """Every window predates the profile: it joins the final one, and that
    version's cutoff is the profile's date, so the next update does not
    fold it in again."""
    import backend.tasks.exports as exports

    user = _new_user("late183")
    written = _user_written(user, created_at=datetime(2026, 1, 1))
    prompts = _rebuild_two_windows(exports, monkeypatch, {
        None: _window("OLD DATA", JAN), JAN: _window("NEW DATA", DEC)})

    profile_id, chunk_num, _ = exports._chunked_profile_loop(
        MagicMock(), user, "gpt-5.5", "UPDATE {existing_profile} || {new_data}",
        {}, first_chunk_prompt_fn=lambda c: "GEN " + c["content"])

    assert chunk_num == 2
    assert "MY OWN WORDS" not in prompts[0]
    assert "MY OWN WORDS" in prompts[1]
    tip = UserProfile.query.get(profile_id)
    assert tip.source_data_cutoff == written.created_at
    # The next window, after newer writing, is read without it.
    later = _window("NEWER", datetime(2026, 3, 1))
    exports.place_user_written_profile(user.id, tip, later)
    assert "user_written_block" not in later


def test_183_placement_rules(app, monkeypatch):
    """The placement depends only on the base and the window, so a resumed
    run and every batch step decide the same way."""
    import backend.tasks.exports as exports

    user = _new_user("rules183")
    monkeypatch.setattr(exports, "count_remaining_units",
                        lambda uid, cutoff=None: 50_000)

    def place(base, latest):
        return exports.place_user_written_profile(
            user.id, base, _window("DATA", latest))

    # No hand-written profile: nothing changes.
    chunk = place(None, DEC)
    assert "user_written_block" not in chunk
    assert chunk["version_cutoff"] == DEC

    own = _user_written(user)
    # From scratch: the window that reaches the profile's date takes it ...
    assert "MY OWN WORDS" in place(None, DEC)["user_written_block"]
    # ... an earlier one does not while writing remains after it.
    assert "user_written_block" not in place(None, JAN)
    # A base that already covers the date has passed it.
    assert "user_written_block" not in place(_generated(user, JUN), DEC)
    # A base from before the date (a resumed rebuild) still takes it.
    assert "user_written_block" in place(_generated(user, JAN), DEC)
    # A chain rooted at the profile had it as its base: not again.
    rooted = _generated(user, JAN, parent=own, gen_type="update")
    assert "user_written_block" not in place(rooted, DEC)
    # The profile as the base itself (no cutoff): the note path covers it.
    assert "user_written_block" not in place(own, DEC)


def test_183_profile_marked_none_is_never_folded_in(app, monkeypatch):
    """A hand-written version marked 'none' is never sent; the newest
    readable one before it is used instead (#346)."""
    import backend.tasks.exports as exports

    user = _new_user("none183")
    monkeypatch.setattr(exports, "count_remaining_units",
                        lambda uid, cutoff=None: 50_000)
    _user_written(user, content="READABLE WORDS", created_at=JAN)
    _user_written(user, content="PRIVATE WORDS", created_at=JUN,
                  ai_usage="none")

    chunk = exports.place_user_written_profile(
        user.id, None, _window("DATA", DEC))
    assert "READABLE WORDS" in chunk["user_written_block"]
    assert "PRIVATE WORDS" not in exports.chunk_content_for_prompt(chunk)


def test_183_integration_shows_user_profile_once_for_a_rooted_chain(app):
    """A chain rooted at the user's profile already shows it as Profile
    No. 1; it is not added a second time."""
    import backend.tasks.exports as exports

    user = _new_user("root183")
    own = _user_written(user)
    first = _generated(user, DEC, parent=own, gen_type="update")
    second = _generated(user, datetime(2026, 2, 1), parent=first,
                        gen_type="update")

    messages, chain = exports.build_integration_messages(user.id, second.id)
    texts = [m["content"][0]["text"] for m in messages[:-1]]
    assert len(chain) == 3 and len(texts) == 3
    assert sum("MY OWN WORDS" in t for t in texts) == 1
    assert "MY OWN WORDS" in texts[0]


# ── #183: an edit of a generated version (the profile page's Edit) ──────
# The profile page saves such an edit as a new user-written version whose
# parent is the generated one. The jobs see the user's edits as such: the
# full text plus the lines the user removed and wrote.

GENERATED_TEXT = "### SURFACE MAP\nLikes long walks.\nWRONG GUESS"
EDITED_TEXT = "### SURFACE MAP\nLikes long walks.\nMY EDIT"


def _edited(user, generated, created_at=JUN, source_tokens=90_000):
    edit = UserProfile(
        user_id=user.id, generated_by="user", tokens_used=0,
        generation_type="initial", source_tokens_used=source_tokens,
        source_data_cutoff=generated.source_data_cutoff, ai_usage="chat",
        created_at=created_at, parent_profile_id=generated.id)
    edit.set_content(EDITED_TEXT)
    _db.session.add(edit)
    _db.session.commit()
    return edit


def _generated_with(user, text, cutoff=JAN, ai_usage="chat"):
    """A generated version saved right after its window (before the
    edit)."""
    profile = _generated(user, cutoff)
    profile.set_content(text)
    profile.ai_usage = ai_usage
    profile.created_at = cutoff
    _db.session.commit()
    return profile


def test_183_edited_version_is_shown_with_the_users_edits(app):
    """The model sees which lines are the user's: what they removed and
    what they wrote, under a note to keep those and refresh the rest."""
    import backend.tasks.exports as exports

    user = _new_user("edit183")
    edit = _edited(user, _generated_with(user, GENERATED_TEXT))

    text = exports.profile_text_for_prompt(edit)

    assert "generated, then edited by the user themselves on 2025-06-01" in text
    assert EDITED_TEXT in text
    assert "- WRONG GUESS" in text and "+ MY EDIT" in text
    assert "- Likes long walks." not in text     # unchanged lines are not edits


def test_183_edit_of_a_none_version_never_shows_its_text(app):
    """A generated version marked 'none' is never shown to a model, so its
    removed lines are not either; the edit is shown as the user's text."""
    import backend.tasks.exports as exports

    user = _new_user("editnone183")
    edit = _edited(user, _generated_with(user, GENERATED_TEXT,
                                         ai_usage="none"))

    text = exports.profile_text_for_prompt(edit)

    assert "WRONG GUESS" not in text
    assert "written by the user themselves on 2025-06-01" in text


def test_183_edited_profile_survives_a_full_rebuild(app, monkeypatch):
    """A profile edited on the profile page survives a from-scratch
    rebuild: the edit joins the window that reaches its date, with the
    user's lines marked, and the rebuilt profile holds the edit and the new
    writing."""
    import backend.tasks.exports as exports

    user = _new_user("editrebuild183")
    _edited(user, _generated_with(user, GENERATED_TEXT))
    prompts = _rebuild_two_windows(exports, monkeypatch, {
        None: _window("OLD DATA", JAN), JAN: _window("NEW DATA", DEC)})
    monkeypatch.setattr(exports, "build_update_template",
                        lambda uid: "UPDATE {existing_profile} || {new_data}")
    monkeypatch.setattr(exports, "_load_prompt",
                        lambda name, user_id=None: "INTEGRATE {N_MONTHS}")
    markers = ("OLD DATA", "NEW DATA", "MY EDIT")

    def keep_markers(text):
        return " ".join(m for m in markers if m in text)

    monkeypatch.setattr(exports, "_call_llm_with_retries",
                        lambda self_, m, prompt, uid, keys, **kw: (
                            prompts.append(prompt) or {
                                "content": keep_markers(prompt),
                                "input_tokens": 10, "output_tokens": 5,
                                "total_tokens": 15}))
    monkeypatch.setattr(exports.LLMProvider, "get_completion", staticmethod(
        lambda model_id, messages, keys, **kw: {
            "content": keep_markers(
                "\n".join(m["content"][0]["text"] for m in messages)),
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15}))

    result = exports._iterative_generation(
        MagicMock(), user, "gpt-5.5", "GEN {user_export}", 10_000, {})

    assert "MY EDIT" not in prompts[0]
    assert "+ MY EDIT" in prompts[1] and "- WRONG GUESS" in prompts[1]
    final = UserProfile.query.get(result["profile_id"])
    for marker in markers:
        assert marker in final.get_content()


def test_183_update_builds_on_the_edited_version_with_its_edits(app, monkeypatch):
    """An incremental update after an edit builds on the edited version
    (the newest one) and shows the user's edits, not the generated text
    alone."""
    import backend.tasks.exports as exports
    from backend.models import Node

    user = _new_user("editupdate183")
    edit = _edited(user, _generated_with(user, GENERATED_TEXT))
    node = Node(user_id=user.id, node_type="user", ai_usage="chat",
                created_at=datetime(2025, 7, 1))
    node.set_content("writing after the cutoff")
    _db.session.add(node)
    _db.session.commit()
    assert exports.profile_update_base(user.id).id == edit.id

    captured = {}
    monkeypatch.setattr(exports, "_chunked_profile_loop", lambda *a, **kw: (
        captured.update(kw) or (edit.id, 0, 0)))

    exports._do_incremental_update(
        MagicMock(), user, "gpt-5.5", edit.id,
        context_window=200000, max_output_tokens=10000, api_keys={})

    base = captured["initial_profile_content"]
    assert "+ MY EDIT" in base and "- WRONG GUESS" in base
    assert captured["initial_cutoff"] == JAN


def test_183_an_edit_keeps_the_provisional_ladder_going(app):
    """An edit carries the edited version's coverage: an edit of an early
    (provisional) profile is provisional too, so the ladder's next rebuild
    still comes — and keeps the edit. A profile written from scratch stays
    the base."""
    import backend.tasks.exports as exports

    user = _new_user("ladder183")
    generated = _generated_with(user, GENERATED_TEXT)
    assert exports.profile_is_provisional(
        _edited(user, generated, source_tokens=10_000)) is True
    assert exports.profile_is_provisional(
        _edited(user, generated, source_tokens=180_000)) is False
    assert exports.profile_is_provisional(_user_written(user)) is False
