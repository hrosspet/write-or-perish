"""#183: the profile jobs and the user's edits (voice review, Peter,
2026-10-20).

An edit is a new version the user wrote, with the version it changed as
its predecessor (routes: test_profile_edit_versions). Here, the jobs:

- the next update builds on the newest version, the user's included, as
  its previous profile, with the prompts as on main (the batch path now
  sends the sync path's author note too: test_profile_batch);
- a version saved while a run is working is not buried under the run's
  output: the step saves nothing and the run ends;
- a rebuilt chain (a from-scratch build, an import's re-tip) merges the
  user's version when it reaches the version's date, with one integration
  call, and goes on from the merge (Peter, 2026-10-20: option 2);
- a first profile written by hand follows the provisional ladder (S1),
  and the ladder's build merges it.

In-memory SQLite, ENCRYPTION_DISABLED, celery mocked so the modules
import. No test calls a provider: the model call is replaced by a stand-in
that records the prompt.
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

from backend.extensions import db as _db                     # noqa: E402
from backend.models import User, UserProfile, Node, APICostLog  # noqa: E402

JAN, MAY, JUN = datetime(2026, 1, 1), datetime(2026, 5, 1), datetime(2026, 6, 1)

# main's note, word for word (tasks/exports.USER_WRITTEN_PROFILE_NOTE):
# the prompts are not changed by #183.
MAIN_NOTE = (
    "[NOTE: The profile below was written by the user themselves, not "
    "AI-generated. Treat it as their own self-description - important, but "
    "also just another data point. Don't overindex on it.]"
)


@pytest.fixture
def app():
    # Warm celery_app first so it resolves the exports <-> profile_batch
    # import cycle in the safe order (see test_profile_regen_resume).
    import backend.celery_app  # noqa: F401
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    # Unknown model -> cost 0 without a KeyError.
    app.config["SUPPORTED_MODELS"] = {}
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def _user(name, **kw):
    user = User(username=name, plan="alpha", twitter_id=None, approved=True,
                default_ai_usage="chat", **kw)
    _db.session.add(user)
    _db.session.commit()
    return user


def _finished_chain(user, units=180_000):
    """A chain tip and its integration, as both pipelines save them."""
    tip = UserProfile(
        user_id=user.id, generated_by="gpt-5.5", tokens_used=900,
        ai_usage="chat", generation_type="iterative",
        source_tokens_used=units, source_data_cutoff=MAY,
        source_rendered_at=MAY + timedelta(hours=1),
        created_at=MAY + timedelta(hours=2))
    tip.set_content("CHAIN TIP TEXT")
    _db.session.add(tip)
    _db.session.flush()
    integration = UserProfile(
        user_id=user.id, generated_by="gpt-5.5", tokens_used=1200,
        ai_usage="chat", generation_type="integration",
        source_tokens_used=units, source_data_cutoff=MAY,
        parent_profile_id=tip.id, created_at=MAY + timedelta(hours=3))
    integration.set_content("### SURFACE MAP\nLikes long walks.\nWRONG GUESS")
    _db.session.add(integration)
    _db.session.commit()
    return tip, integration


def _edit(user, version, content, created_at=None):
    """The row PUT /profile/<id> saves (routes/profile.py
    _save_edit_as_new_version): generated_by "user", the edited version as
    its parent, that version's coverage, dated by the edit."""
    from backend.utils.profile_versions import coverage_of
    edit = UserProfile(user_id=user.id, generated_by="user", tokens_used=0,
                       ai_usage="chat", parent_profile_id=version.id,
                       created_at=created_at or datetime.utcnow(),
                       **coverage_of(user, version))
    edit.set_content(content)
    _db.session.add(edit)
    _db.session.commit()
    return edit


def _window(content="NEW WRITING", latest=JUN, units=90_000):
    return {"content": content, "token_count": units, "unit_count": units,
            "latest_node_created_at": latest}


def _new_writing(user):
    node = Node(user_id=user.id, node_type="user", ai_usage="chat",
                token_count=90_000, created_at=JUN)
    node.set_content("new writing")
    _db.session.add(node)
    _db.session.commit()


def _is_merge(messages):
    """A rebuild's merge call: two versions whose spans both start at
    "start" (exports.build_merge_messages), then the prompt."""
    texts = [m["content"][0]["text"] for m in messages]
    return len(texts) == 3 and texts[1].startswith("Profile No. 2\n- start")


def _stand_in(monkeypatch, exports, windows, remaining, chunk_outputs,
              integration_output="INTEGRATED", merge_output="MERGED"):
    """Replace the export, the planner's remainder and both model calls.
    Returns the list of (kind, prompt or messages) the model received:
    "chunk", "merge" or "integration"."""
    import backend.llm_providers as lp
    seen = []
    monkeypatch.setattr(exports, "build_user_export_content",
                        MagicMock(side_effect=list(windows)))
    monkeypatch.setattr(exports, "count_remaining_units",
                        MagicMock(side_effect=list(remaining)))
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda m, msgs, k: None))
    outputs = list(chunk_outputs)

    def call(self, model_id, prompt, user_id, api_keys, **kw):
        seen.append(("chunk", prompt))
        out = outputs.pop(0)
        if callable(out):
            out = out()
        return {"content": out, "input_tokens": 1000, "output_tokens": 500,
                "total_tokens": 1500}
    monkeypatch.setattr(exports, "_call_llm_with_retries", call)

    def complete(model_id, messages, api_keys, **kw):
        merge = _is_merge(messages)
        seen.append(("merge" if merge else "integration", messages))
        out = merge_output if merge else integration_output
        out = out() if callable(out) else out
        return {"content": out, "input_tokens": 2000, "output_tokens": 500,
                "total_tokens": 2500}
    monkeypatch.setattr(lp.LLMProvider, "get_completion",
                        staticmethod(complete))
    return seen


def _existing_profile(prompt):
    """The {existing_profile} slot of the real update template."""
    return prompt.split("<existing_profile>\n", 1)[1].split(
        "\n</existing_profile>", 1)[0]


# ── the next update builds on the user's version ─────────────────────────

def test_the_next_update_builds_on_the_users_version(app, monkeypatch):
    """Edit the newest version (an integration): the edit is the update
    base, the update's previous profile is the edit's text with main's
    note, the saved update descends from the edit, and the integration
    merges the edit and the update."""
    import backend.tasks.exports as exports
    user = _user("editor")
    tip, integration = _finished_chain(user)
    edit = _edit(user, integration,
                 "### SURFACE MAP\nLikes long walks.\nMY CORRECTION")
    _new_writing(user)

    base = exports.profile_update_base(user.id)
    assert base.id == edit.id

    seen = _stand_in(monkeypatch, exports, [_window()], [90_000, 0],
                     ["UPDATED, KEEPS MY CORRECTION"])
    result = exports._do_incremental_update(
        MagicMock(), user, "gpt-5.5", base.id, 200_000, 32_000, {})

    (kind, prompt), (ikind, messages) = seen
    assert kind == "chunk" and ikind == "integration"
    # main's prompt for a user-written base, byte for byte.
    assert _existing_profile(prompt) == f"{MAIN_NOTE}\n\n{edit.get_content()}"
    assert "WRONG GUESS" not in prompt
    update = UserProfile.query.filter_by(user_id=user.id,
                                         generation_type="update").one()
    assert update.parent_profile_id == edit.id
    texts = [m["content"][0]["text"] for m in messages[:-1]]
    assert len(texts) == 2
    assert texts[0] == ("Profile No. 1\n- start to 2026-05-01\n\n"
                        f"{MAIN_NOTE}\n\n{edit.get_content()}")
    assert texts[1].startswith("Profile No. 2\n- 2026-05-01 to 2026-06-01\n\n")
    assert "UPDATED, KEEPS MY CORRECTION" in texts[1]
    assert MAIN_NOTE not in texts[1]
    newest = UserProfile.query.get(result["profile_id"])
    assert newest.generation_type == "integration"
    assert newest.parent_profile_id == update.id


def test_an_edit_of_a_chunk_replaces_it_in_the_integration(app, monkeypatch):
    """The version shown is a chunk (no integration on top of it), and the
    user edits it. The edit replaces the chunk: after the next update, the
    integration's inputs are the edit and the update, not the chunk the
    user replaced, and the edit spans from "start" to the cutoff it covers
    (the real-model check of #414 found [chunk, edit, update] with a
    zero-length span for the edit)."""
    import backend.tasks.exports as exports
    user = _user("chunkedit")
    chunk = UserProfile(
        user_id=user.id, generated_by="gpt-5.5", tokens_used=900,
        ai_usage="chat", generation_type="iterative",
        source_tokens_used=180_000, source_data_cutoff=MAY,
        source_rendered_at=MAY + timedelta(hours=1),
        created_at=MAY + timedelta(hours=2))
    chunk.set_content("### SURFACE MAP\nTHE TEXT THE USER REPLACED")
    _db.session.add(chunk)
    _db.session.commit()
    edit = _edit(user, chunk, "### SURFACE MAP\nMY CORRECTION")
    _new_writing(user)
    seen = _stand_in(monkeypatch, exports, [_window()], [90_000, 0],
                     ["UPDATED"])

    exports._do_incremental_update(
        MagicMock(), user, "gpt-5.5", edit.id, 200_000, 32_000, {})

    (_kind, _prompt), (ikind, messages) = seen
    assert ikind == "integration"
    texts = _texts(messages)[:-1]
    assert len(texts) == 2
    assert texts[0] == ("Profile No. 1\n- start to 2026-05-01\n\n"
                        f"{MAIN_NOTE}\n\n### SURFACE MAP\nMY CORRECTION")
    assert texts[1].startswith("Profile No. 2\n- 2026-05-01 to 2026-06-01\n\n")
    assert texts[1].endswith("UPDATED")
    assert all("THE TEXT THE USER REPLACED" not in t for t in _texts(messages))
    update = UserProfile.query.filter_by(generation_type="update").one()
    assert [p.id for p in exports._collect_iterative_chain(update.id)] == [
        edit.id, update.id]


def test_two_edits_the_update_builds_on_the_second(app):
    import backend.tasks.exports as exports
    user = _user("twice")
    _tip, integration = _finished_chain(user)
    first = _edit(user, integration, "FIRST EDIT",
                  created_at=datetime.utcnow() - timedelta(minutes=5))
    second = _edit(user, first, "SECOND EDIT")

    assert exports.profile_update_base(user.id).id == second.id
    assert second.parent_profile_id == first.id
    # An update built on it walks back through both edits.
    update = UserProfile(user_id=user.id, generated_by="gpt-5.5",
                         tokens_used=0, generation_type="update",
                         source_tokens_used=270_000, source_data_cutoff=JUN,
                         parent_profile_id=second.id)
    update.set_content("UPDATE")
    _db.session.add(update)
    _db.session.commit()
    # The second edit replaced the first: the chain starts at it.
    chain = exports._collect_iterative_chain(update.id)
    assert [p.id for p in chain] == [second.id, update.id]


def test_an_edit_keeps_the_gates_of_the_version_it_edited(app, monkeypatch):
    """The edit carries the edited version's coverage: the next update
    reads only writing after its cutoff, and the 80k gate counts from
    there, as it did for the integration."""
    import backend.tasks.exports as exports
    import backend.tasks.profile_batch as pb
    monkeypatch.setattr(pb, "use_batch_for_user", lambda *a, **k: False)
    user = _user("gates")
    _tip, integration = _finished_chain(user)
    edit = _edit(user, integration, "MY CORRECTION",
                 created_at=datetime.utcnow() - timedelta(hours=2))
    node = Node(user_id=user.id, node_type="user", ai_usage="chat",
                token_count=20_000, created_at=JUN, updated_at=JUN)
    node.set_content("a little new writing")
    _db.session.add(node)
    _db.session.commit()
    triggered = []
    monkeypatch.setattr(exports, "maybe_trigger_profile_update",
                        lambda *a, **k: triggered.append((a, k)))

    assert exports.profile_is_provisional(edit) is False
    assert exports.maybe_trigger_incremental_profile_update(user) is None
    assert triggered == []          # 20k < 80k since the cutoff

    node.token_count = 85_000
    _db.session.commit()
    exports.maybe_trigger_incremental_profile_update(user)
    assert triggered == [((user.id,), {"force_full_regen": False})]


# ── a version saved while a run is working ───────────────────────────────

def test_an_edit_saved_while_a_chunk_runs_is_not_buried(app, monkeypatch):
    """The user edits while the update's chunk is being generated. The
    chunk saves nothing (its cost is logged), the run ends, and the edit
    stays the newest version and the next update's base."""
    import backend.tasks.exports as exports
    user = _user("racer")
    tip, integration = _finished_chain(user)
    _new_writing(user)
    edits = []

    def edit_during_the_call():
        edits.append(_edit(user, integration, "MY CORRECTION"))
        return "UPDATE THAT NEVER SAW THE EDIT"
    _stand_in(monkeypatch, exports, [_window()], [90_000, 0],
              [edit_during_the_call])

    with pytest.raises(exports.ProfileBaseMoved):
        exports._do_incremental_update(
            MagicMock(), user, "gpt-5.5", tip.id, 200_000, 32_000, {})

    (edit,) = edits
    assert UserProfile.query.filter_by(generation_type="update").count() == 0
    assert exports.profile_update_base(user.id).id == edit.id
    newest = UserProfile.query.order_by(UserProfile.created_at.desc()).first()
    assert newest.id == edit.id
    assert APICostLog.query.filter_by(user_id=user.id).count() == 1


def test_an_edit_saved_before_the_next_chunk_ends_the_run(app, monkeypatch):
    """A multi-chunk run: chunk 1 is saved, then the user edits it. The run
    stops before it calls the model again; chunk 1 stays."""
    import backend.tasks.exports as exports
    user = _user("between")
    tip, _integration = _finished_chain(user)
    _new_writing(user)
    monkeypatch.setattr(exports, "should_continue_chain",
                        lambda user, profile: True)
    seen = _stand_in(monkeypatch, exports, [_window(units=90_000)],
                     [180_000, 90_000], ["CHUNK 1"])
    real_save = exports._save_profile

    def save_then_edit(*a, **kw):
        saved = real_save(*a, **kw)
        _edit(user, saved, "MY EDIT OF CHUNK 1")
        return saved
    monkeypatch.setattr(exports, "_save_profile", save_then_edit)

    with pytest.raises(exports.ProfileBaseMoved):
        exports._do_incremental_update(
            MagicMock(), user, "gpt-5.5", tip.id, 200_000, 32_000, {})

    assert [kind for kind, _ in seen] == ["chunk"]
    assert UserProfile.query.filter_by(generation_type="update").count() == 1
    assert exports.profile_update_base(user.id).get_content() == \
        "MY EDIT OF CHUNK 1"


def test_an_edit_saved_while_the_integration_runs_is_not_buried(app, monkeypatch):
    """The integration would become the version the profile page shows;
    with an edit saved during its call it saves nothing."""
    import backend.tasks.exports as exports
    user = _user("integrating")
    _tip, integration = _finished_chain(user)
    _new_writing(user)
    edits = []

    def edit_during_integration():
        newest = UserProfile.query.order_by(
            UserProfile.created_at.desc()).first()
        edits.append(_edit(user, newest, "MY CORRECTION"))
        return "INTEGRATION THAT NEVER SAW THE EDIT"
    edit = _edit(user, integration, "FIRST EDIT",
                 created_at=datetime.utcnow() - timedelta(hours=2))
    _stand_in(monkeypatch, exports, [_window()], [90_000, 0], ["UPDATED"],
              integration_output=edit_during_integration)

    with pytest.raises(exports.ProfileBaseMoved):
        exports._do_incremental_update(
            MagicMock(), user, "gpt-5.5", edit.id, 200_000, 32_000, {})

    assert UserProfile.query.filter_by(
        user_id=user.id, generation_type="integration").count() == 1  # the old one
    assert exports.profile_update_base(user.id).id == edits[0].id
    # The chunk's cost and the discarded integration's cost.
    assert APICostLog.query.filter_by(user_id=user.id).count() == 2


def test_an_edit_saved_during_a_rebuilds_first_chunk_is_merged(app, monkeypatch):
    """A from-scratch first chunk reads no earlier version, so an edit saved
    while it runs does not stop it (base_moved has no base to compare).
    The save drops the pending merge of the older edit (the route), and
    the chunk plans the merge again before it is saved: the newer edit is
    merged at its date (review of #414, 2026-10-20, finding 1)."""
    import backend.tasks.exports as exports
    user = _user("scratchrace", profile_needs_full_regen=True)
    _tip, integration = _finished_chain(user)
    first = _edit(user, integration, "FIRST EDIT",
                  created_at=datetime.utcnow() - timedelta(hours=2))
    _writing(user)
    newer = []

    def edit_during_chunk_1():
        newer.append(_edit(user, first, "SECOND EDIT"))
        user.profile_pending_merge_id = None     # what the route does
        _db.session.commit()
        return "REBUILT 1"
    monkeypatch.setattr(exports, "should_continue_chain",
                        lambda user, profile: True)
    seen = _stand_in(
        monkeypatch, exports,
        [_window("OLD", latest=datetime(2026, 3, 1)),
         _window("NEW", latest=JUN)],
        [180_000, 90_000, 0], [edit_during_chunk_1, "REBUILT 2"])

    exports._do_initial_generation(MagicMock(), user, "gpt-5.5", 32_000, {})

    assert [kind for kind, _ in seen] == [
        "chunk", "chunk", "merge", "integration"]
    assert _texts(seen[2][1])[1].endswith(f"{MAIN_NOTE}\n\nSECOND EDIT")
    merge = UserProfile.query.filter(
        UserProfile.merged_profile_id.isnot(None)).one()
    assert merge.merged_profile_id == newer[0].id
    assert User.query.get(user.id).profile_pending_merge_id is None


# ── rebuilds merge the user's version (Peter, 2026-10-20: option 2) ──────

def _texts(messages):
    return [m["content"][0]["text"] for m in messages]


def _writing(user, units=90_000, at=JUN):
    """Enough stored units for a from-scratch build to start."""
    node = Node(user_id=user.id, node_type="user", ai_usage="chat",
                token_count=units, created_at=at, updated_at=at)
    node.set_content("writing")
    _db.session.add(node)
    _db.session.commit()
    return node


def test_a_full_rebuild_merges_the_users_version_at_its_date(app, monkeypatch):
    """The user's edit covers the writing up to May; an import of a 2019
    archive rebuilds the chain from scratch. The 2019 window is read
    without the edit. The window that reaches May is followed by one merge
    call: the rebuilt version, then the user's version with main's note.
    The next window builds on the merge, and the final integration merges
    the chain with the merge in it."""
    import backend.tasks.exports as exports
    user = _user("archive", profile_needs_full_regen=True)
    _tip, integration = _finished_chain(user)
    edit = _edit(user, integration, "### SURFACE MAP\nMY CORRECTION")
    _writing(user)
    monkeypatch.setattr(exports, "should_continue_chain",
                        lambda user, profile: True)
    seen = _stand_in(
        monkeypatch, exports,
        [_window("2019 ARCHIVE", latest=datetime(2019, 12, 1)),
         _window("SPRING WRITING", latest=datetime(2026, 5, 15)),
         _window("JUNE WRITING", latest=JUN)],
        [270_000, 180_000, 90_000, 0],
        ["REBUILT 2019", "REBUILT SPRING", "REBUILT JUNE"],
        merge_output="MERGED, KEEPS MY CORRECTION")

    exports._do_initial_generation(MagicMock(), user, "gpt-5.5", 32_000, {})

    kinds = [kind for kind, _ in seen]
    assert kinds == ["chunk", "chunk", "merge", "chunk", "integration"]
    assert "MY CORRECTION" not in seen[0][1] and "MY CORRECTION" not in seen[1][1]
    merge_texts = _texts(seen[2][1])
    assert merge_texts[0].startswith("Profile No. 1\n- start to 2026-05-15\n\n")
    assert "REBUILT SPRING" in merge_texts[0]
    assert merge_texts[1] == ("Profile No. 2\n- start to 2026-05-01\n\n"
                              f"{MAIN_NOTE}\n\n{edit.get_content()}")
    # The integration prompt, unchanged.
    assert merge_texts[2].startswith("The above is a series of profile updates")
    # The window after the merge builds on it.
    assert _existing_profile(seen[3][1]) == "MERGED, KEEPS MY CORRECTION"
    merge = UserProfile.query.filter_by(merged_profile_id=edit.id).one()
    spring = UserProfile.query.get(merge.parent_profile_id)
    assert "REBUILT SPRING" in spring.get_content()
    assert merge.generation_type == "iterative"
    assert merge.source_data_cutoff == datetime(2026, 5, 15)
    assert merge.source_tokens_used == spring.source_tokens_used
    june = UserProfile.query.filter_by(parent_profile_id=merge.id).one()
    assert "REBUILT JUNE" in june.get_content()
    integrated = [t for t in _texts(seen[4][1])[:-1]]
    assert len(integrated) == 4                       # 3 chunks + the merge
    assert "MERGED, KEEPS MY CORRECTION" in integrated[2]
    assert User.query.get(user.id).profile_pending_merge_id is None


def test_a_main_era_hand_written_profile_is_merged_not_replaced(app, monkeypatch):
    """Review finding 2: a profile written with POST /profile on main (no
    parent, no cutoff, 0 units) on an account with 5k to 80k units takes
    the ladder after deploy (S1). The ladder's from-scratch build merges it
    as the run's last step (it was written after all of the writing), so
    the account's newest version holds the user's words."""
    import backend.tasks.exports as exports
    import backend.tasks.profile_batch as pb
    monkeypatch.setattr(pb, "use_batch_for_user", lambda *a, **k: False)
    user = _user("mainera")
    own = UserProfile(user_id=user.id, generated_by="user", tokens_used=0,
                      generation_type="initial", source_tokens_used=0,
                      source_data_cutoff=None,
                      created_at=datetime(2025, 3, 1))
    own.set_content("ABOUT ME, IN MY WORDS")
    _db.session.add(own)
    _db.session.commit()
    _writing(user, units=6_000, at=datetime(2025, 1, 2))
    triggered = []
    monkeypatch.setattr(exports, "maybe_trigger_profile_update",
                        lambda *a, **k: triggered.append((a, k)))
    exports.maybe_trigger_incremental_profile_update(user)
    assert triggered == [((user.id,), {"force_full_regen": True})]

    seen = _stand_in(
        monkeypatch, exports,
        [_window("EARLY WRITING", latest=datetime(2025, 1, 2), units=6_000)],
        [6_000, 0], ["BUILT FROM THE WRITING"],
        merge_output="BUILT, WITH MY WORDS")
    result = exports._do_initial_generation(
        MagicMock(), user, "gpt-5.5", 32_000, {})

    assert [kind for kind, _ in seen] == ["chunk", "merge"]
    assert "ABOUT ME" not in seen[0][1]
    assert _texts(seen[1][1])[1] == (
        "Profile No. 2\n- start to unknown\n\n"
        f"{MAIN_NOTE}\n\nABOUT ME, IN MY WORDS")
    newest = UserProfile.query.order_by(UserProfile.created_at.desc()).first()
    assert newest.id == result["profile_id"]
    assert newest.merged_profile_id == own.id
    assert newest.generation_type == "initial"        # the run's final version
    assert "BUILT, WITH MY WORDS" in newest.get_content()
    # The next ladder step carries the same version again.
    assert exports.carried_user_version(newest).id == own.id


def test_a_re_tip_merges_an_edit_made_after_the_re_tip_point(app, monkeypatch):
    """An import older than part of the chain re-tips it at the last version
    before the import. The edit made after that point is merged when the
    regenerated chain reaches its date."""
    import backend.tasks.exports as exports
    user = _user("retip")
    c1 = UserProfile(user_id=user.id, generated_by="gpt-5.5", tokens_used=0,
                     generation_type="iterative", source_tokens_used=90_000,
                     source_data_cutoff=JAN, source_rendered_at=JAN,
                     created_at=JAN)
    c1.set_content("JANUARY")
    _db.session.add(c1)
    _db.session.flush()
    c2 = UserProfile(user_id=user.id, generated_by="gpt-5.5", tokens_used=0,
                     generation_type="update", source_tokens_used=180_000,
                     source_data_cutoff=MAY, source_rendered_at=MAY,
                     parent_profile_id=c1.id, created_at=MAY)
    c2.set_content("MAY")
    _db.session.add(c2)
    _db.session.commit()
    edit = _edit(user, c2, "MAY, CORRECTED BY ME")

    action, copy = exports.revert_profile_for_import(user.id, datetime(2026, 3, 1))
    _db.session.commit()

    assert action == "revert" and copy.parent_profile_id == c1.id
    assert User.query.get(user.id).profile_pending_merge_id == edit.id
    _writing(user, at=datetime(2026, 3, 2))
    seen = _stand_in(
        monkeypatch, exports,
        [_window("IMPORTED SPRING", latest=MAY)], [90_000, 0],
        ["REGENERATED MAY"])
    exports._do_incremental_update(
        MagicMock(), user, "gpt-5.5", copy.id, 200_000, 32_000, {})

    assert [kind for kind, _ in seen] == ["chunk", "merge", "integration"]
    assert _texts(seen[1][1])[1].endswith(
        f"{MAIN_NOTE}\n\nMAY, CORRECTED BY ME")
    merge = UserProfile.query.filter_by(merged_profile_id=edit.id).one()
    assert merge.generation_type == "update"
    assert User.query.get(user.id).profile_pending_merge_id is None


def test_a_re_tip_at_the_edit_merges_nothing(app):
    """An import newer than the edit's coverage re-tips at the edit or
    after it: the regenerated chain already carries the edit."""
    import backend.tasks.exports as exports
    user = _user("retipkeeps")
    _tip, integration = _finished_chain(user)
    edit = _edit(user, integration, "MY CORRECTION",
                 created_at=datetime.utcnow() - timedelta(days=1))
    update = UserProfile(user_id=user.id, generated_by="gpt-5.5",
                         tokens_used=0, generation_type="update",
                         source_tokens_used=270_000, source_data_cutoff=JUN,
                         source_rendered_at=JUN, parent_profile_id=edit.id)
    update.set_content("UPDATE ON MY CORRECTION")
    _db.session.add(update)
    _db.session.commit()

    action, copy = exports.revert_profile_for_import(user.id, MAY + timedelta(days=5))
    _db.session.commit()

    assert action == "revert" and copy.parent_profile_id == edit.id
    assert User.query.get(user.id).profile_pending_merge_id is None


def test_a_revert_past_the_edit_is_not_undone_by_a_rebuild(app):
    """The user reverted from their edit to the generated version: the
    version they chose carries no edit, so a rebuild merges nothing."""
    import backend.tasks.exports as exports
    user = _user("reverted")
    _tip, integration = _finished_chain(user)
    _edit(user, integration, "MY CORRECTION",
          created_at=datetime.utcnow() - timedelta(minutes=5))
    revert = UserProfile(user_id=user.id, generated_by="gpt-5.5",
                         tokens_used=0, generation_type="revert",
                         source_tokens_used=180_000, source_data_cutoff=MAY,
                         parent_profile_id=integration.id)
    revert.set_content(integration.get_content())
    _db.session.add(revert)
    _db.session.commit()

    assert exports.profile_update_base(user.id).id == revert.id
    assert exports.plan_user_version_merge(
        user, exports.profile_update_base(user.id)) is None
    assert user.profile_pending_merge_id is None


def test_a_version_saved_during_the_merge_is_not_buried(app, monkeypatch):
    """base_moved holds for the merge step too: an edit saved while the
    merge runs stays the newest version."""
    import backend.tasks.exports as exports
    user = _user("mergerace", profile_needs_full_regen=True)
    _tip, integration = _finished_chain(user)
    _edit(user, integration, "MY CORRECTION",
          created_at=datetime.utcnow() - timedelta(minutes=5))
    _writing(user)
    edits = []

    def edit_during_the_merge():
        newest = UserProfile.query.order_by(
            UserProfile.created_at.desc()).first()
        edits.append(_edit(user, newest, "A NEWER EDIT"))
        return "MERGE THAT NEVER SAW IT"
    _stand_in(monkeypatch, exports, [_window(latest=JUN)], [90_000, 0],
              ["REBUILT"], merge_output=edit_during_the_merge)

    with pytest.raises(exports.ProfileBaseMoved):
        exports._do_initial_generation(MagicMock(), user, "gpt-5.5", 32_000, {})

    assert UserProfile.query.filter(
        UserProfile.merged_profile_id.isnot(None)).count() == 0
    assert exports.profile_update_base(user.id).id == edits[0].id


def test_a_pending_merge_runs_at_the_next_check(app, monkeypatch):
    """A rebuild whose merge did not run (it failed, or the run stopped)
    merges at the next hourly check, whatever the update gates say."""
    import backend.tasks.exports as exports
    import backend.tasks.profile_batch as pb
    monkeypatch.setattr(pb, "use_batch_for_user", lambda *a, **k: False)
    user = _user("pendingmerge")
    _tip, integration = _finished_chain(user)
    edit = _edit(user, integration, "MY CORRECTION",
                 created_at=datetime.utcnow() - timedelta(minutes=5))
    rebuilt = UserProfile(user_id=user.id, generated_by="gpt-5.5",
                          tokens_used=0, generation_type="initial",
                          source_tokens_used=180_000, source_data_cutoff=MAY,
                          source_rendered_at=MAY)
    rebuilt.set_content("REBUILT")
    _db.session.add(rebuilt)
    user.profile_pending_merge_id = edit.id
    _db.session.commit()
    triggered = []
    monkeypatch.setattr(exports, "maybe_trigger_profile_update",
                        lambda *a, **k: triggered.append((a, k)))

    exports.maybe_trigger_incremental_profile_update(user)

    assert triggered == [((user.id,), {"force_full_regen": False})]


# ── S1: a first profile written by hand follows the ladder ──────────────

def _hand_written_first_profile(name, units):
    user = _user(name)
    profile = UserProfile(user_id=user.id, generated_by="user",
                          tokens_used=0, generation_type="initial",
                          source_tokens_used=0, source_data_cutoff=None,
                          created_at=datetime(2025, 1, 1))
    profile.set_content("ABOUT ME")
    node = Node(user_id=user.id, node_type="user", ai_usage="chat",
                token_count=units, created_at=datetime(2025, 1, 2),
                updated_at=datetime(2025, 1, 2))
    node.set_content("writing")
    _db.session.add_all([profile, node])
    _db.session.commit()
    return user, profile


@pytest.mark.parametrize("units, due", [(4_000, False), (5_000, True)])
def test_a_first_hand_written_profile_takes_the_ladder(app, monkeypatch,
                                                       units, due):
    """S1 (voice review, 2026-10-20): the first build at 5k units, as for
    an account with no profile or a provisional one, instead of waiting
    for 80k. The build is from scratch."""
    import backend.tasks.exports as exports
    import backend.tasks.profile_batch as pb
    monkeypatch.setattr(pb, "use_batch_for_user", lambda *a, **k: False)
    user, profile = _hand_written_first_profile(f"hand{units}", units)
    triggered = []
    monkeypatch.setattr(exports, "maybe_trigger_profile_update",
                        lambda *a, **k: triggered.append((a, k)))

    assert exports.profile_is_provisional(profile) is True
    exports.maybe_trigger_incremental_profile_update(user)

    assert triggered == ([((user.id,), {"force_full_regen": True})]
                         if due else [])


def test_a_ladder_build_that_stops_before_its_first_chunk_is_retried_from_scratch(
        app, monkeypatch):
    """Review finding 2: the ladder build records the merge of the user's
    hand-written profile, then fails before chunk 1 is saved. The sync
    ladder sets the regen flag, as the batch seeder does, so the next check
    dispatches a from-scratch build again, not an update that merged the
    profile with itself."""
    import backend.tasks.exports as exports
    import backend.tasks.profile_batch as pb
    import backend.llm_providers as lp
    monkeypatch.setattr(pb, "use_batch_for_user", lambda *a, **k: False)
    user = _user("ladderfail")
    own = UserProfile(user_id=user.id, generated_by="user", tokens_used=0,
                      generation_type="initial", source_tokens_used=0,
                      source_data_cutoff=None,
                      created_at=datetime.utcnow() - timedelta(days=30))
    own.set_content("ABOUT ME, IN MY WORDS")
    _db.session.add(own)
    _db.session.commit()
    _writing(user, units=6_000, at=datetime.utcnow() - timedelta(days=40))
    triggered = []
    monkeypatch.setattr(exports, "maybe_trigger_profile_update",
                        lambda *a, **k: triggered.append((a, k)))
    exports.maybe_trigger_incremental_profile_update(user)
    assert triggered == [((user.id,), {"force_full_regen": True})]
    assert User.query.get(user.id).profile_needs_full_regen is True

    def provider_down(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(exports, "_call_llm_with_retries", provider_down)
    monkeypatch.setattr(exports, "build_user_export_content",
                        MagicMock(return_value=_window(units=6_000)))
    monkeypatch.setattr(exports, "count_remaining_units",
                        MagicMock(return_value=6_000))
    monkeypatch.setattr(lp.LLMProvider, "count_tokens",
                        staticmethod(lambda m, msgs, k: None))
    with pytest.raises(RuntimeError):
        exports._do_initial_generation(MagicMock(), user, "gpt-5.5",
                                       32_000, {})
    user = User.query.get(user.id)
    assert user.profile_pending_merge_id == own.id
    assert user.profile_needs_full_regen is True

    triggered.clear()
    exports.maybe_trigger_incremental_profile_update(user)
    assert triggered == [((user.id,), {"force_full_regen": True})]


def test_a_base_that_holds_the_pending_version_merges_nothing(app, monkeypatch):
    """The safety net on both paths: a pending merge whose version the base
    already descends from is dropped without a call, so the user's version
    is never merged with itself."""
    import backend.tasks.exports as exports
    user = _user("selfmerge")
    own = UserProfile(user_id=user.id, generated_by="user", tokens_used=0,
                      generation_type="initial", source_tokens_used=0,
                      source_data_cutoff=None,
                      created_at=datetime.utcnow() - timedelta(days=30))
    own.set_content("ABOUT ME, IN MY WORDS")
    _db.session.add(own)
    user.profile_pending_merge_id = own.id
    _db.session.commit()
    _writing(user, units=6_000, at=datetime.utcnow() - timedelta(days=40))
    seen = _stand_in(monkeypatch, exports, [], [0], [])

    exports._do_incremental_update(
        MagicMock(), user, "gpt-5.5", own.id, 200_000, 32_000, {})

    assert seen == []
    assert User.query.get(user.id).profile_pending_merge_id is None
    assert APICostLog.query.filter_by(user_id=user.id).count() == 0


def test_an_import_inside_the_chain_re_tips_under_an_edited_integration(app):
    """Review finding 3: after an edit of the integration, an import dated
    inside the chain re-tips it at the chunk before the import, as without
    the edit, instead of a full rebuild; the edit is merged at its date."""
    import backend.tasks.exports as exports
    user = _user("retipedit")
    chunks, parent = [], None
    for i, (cutoff, units) in enumerate([(JAN, 90_000),
                                         (datetime(2026, 3, 1), 180_000),
                                         (MAY, 270_000)]):
        chunk = UserProfile(
            user_id=user.id, generated_by="gpt-5.5", tokens_used=0,
            generation_type="iterative", source_tokens_used=units,
            source_data_cutoff=cutoff, source_rendered_at=cutoff,
            parent_profile_id=parent.id if parent else None,
            created_at=datetime.utcnow() - timedelta(days=10 - i))
        chunk.set_content(f"C{i}")
        _db.session.add(chunk)
        _db.session.flush()
        chunks.append(chunk)
        parent = chunk
    integration = UserProfile(
        user_id=user.id, generated_by="gpt-5.5", tokens_used=0,
        generation_type="integration", source_tokens_used=270_000,
        source_data_cutoff=MAY, parent_profile_id=parent.id,
        created_at=datetime.utcnow() - timedelta(days=5))
    integration.set_content("INTEGRATED")
    _db.session.add(integration)
    _db.session.commit()
    edit = _edit(user, integration, "MY CORRECTION",
                 created_at=datetime.utcnow() - timedelta(days=1))

    action, copy = exports.revert_profile_for_import(user.id, datetime(2026, 4, 1))
    _db.session.commit()

    assert action == "revert"
    assert copy.parent_profile_id == chunks[1].id
    user = User.query.get(user.id)
    assert user.profile_needs_full_regen is False
    assert user.profile_pending_merge_id == edit.id


def test_a_merge_saved_as_none_keeps_the_pending_merge(app, monkeypatch):
    """Review finding 4: the account is set to 'none' while the merge call
    runs. The merge is saved 'none' (never a base), the pending merge is
    kept and the run ends; once the account allows AI again, the next run
    merges the user's version."""
    import backend.tasks.exports as exports
    user = _user("noneswitch")
    _tip, integration = _finished_chain(user)
    edit = _edit(user, integration, "MY CORRECTION",
                 created_at=datetime.utcnow() - timedelta(minutes=5))
    rebuilt = UserProfile(user_id=user.id, generated_by="gpt-5.5",
                          tokens_used=0, generation_type="iterative",
                          source_tokens_used=180_000, source_data_cutoff=MAY,
                          source_rendered_at=MAY)
    rebuilt.set_content("REBUILT")
    _db.session.add(rebuilt)
    user.profile_pending_merge_id = edit.id
    _db.session.commit()

    def switch_to_none():
        user.default_ai_usage = "none"
        _db.session.commit()
        return "MERGED UNDER NONE"
    _stand_in(monkeypatch, exports, [], [0], [], merge_output=switch_to_none)
    with pytest.raises(exports.ProfileBaseMoved):
        exports._do_incremental_update(
            MagicMock(), user, "gpt-5.5", rebuilt.id, 200_000, 32_000, {})
    hidden = UserProfile.query.filter_by(merged_profile_id=edit.id).one()
    assert hidden.ai_usage == "none"
    assert User.query.get(user.id).profile_pending_merge_id == edit.id

    user.default_ai_usage = "chat"
    _db.session.commit()
    assert exports.profile_update_base(user.id).id == rebuilt.id
    seen = _stand_in(monkeypatch, exports, [], [0], [], merge_output="MERGED")
    exports._do_incremental_update(
        MagicMock(), user, "gpt-5.5", rebuilt.id, 200_000, 32_000, {})
    assert [kind for kind, _ in seen] == ["merge"]
    assert User.query.get(user.id).profile_pending_merge_id is None
    assert UserProfile.query.filter_by(merged_profile_id=edit.id,
                                       ai_usage="chat").count() == 1


def test_a_pending_merge_with_no_new_writing_still_runs(app, monkeypatch):
    """The run the pending merge dispatches has no new writing to read: the
    merge is its only step, on the rebuilt chain's newest version."""
    import backend.tasks.exports as exports
    user = _user("mergeonly")
    _tip, integration = _finished_chain(user)
    edit = _edit(user, integration, "MY CORRECTION",
                 created_at=datetime.utcnow() - timedelta(minutes=5))
    rebuilt = UserProfile(user_id=user.id, generated_by="gpt-5.5",
                          tokens_used=0, generation_type="initial",
                          source_tokens_used=180_000, source_data_cutoff=MAY,
                          source_rendered_at=MAY)
    rebuilt.set_content("REBUILT")
    _db.session.add(rebuilt)
    user.profile_pending_merge_id = edit.id
    _db.session.commit()
    seen = _stand_in(monkeypatch, exports, [], [0], [])

    result = exports._do_incremental_update(
        MagicMock(), user, "gpt-5.5", rebuilt.id, 200_000, 32_000, {})

    assert [kind for kind, _ in seen] == ["merge"]
    merge = UserProfile.query.get(result["profile_id"])
    assert merge.merged_profile_id == edit.id
    assert merge.parent_profile_id == rebuilt.id
    assert User.query.get(user.id).profile_pending_merge_id is None
