"""#183: the profile jobs and the user's edits (voice review, Peter,
2026-10-20).

An edit is a new version the user wrote, with the version it changed as
its predecessor (routes: test_profile_edit_versions). Here, the jobs:

- the next update builds on the newest version, the user's included, as
  its previous profile, with the prompts as on main;
- a version saved while a run is working is not buried under the run's
  output: the step saves nothing and the run ends;
- a full rebuild reads the writing only, no earlier version (main's
  behaviour, unchanged here: how a rebuild should treat an edit is open
  for Peter);
- a first profile written by hand follows the provisional ladder (S1).

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


def _stand_in(monkeypatch, exports, windows, remaining, chunk_outputs,
              integration_output="INTEGRATED"):
    """Replace the export, the planner's remainder and both model calls.
    Returns the list of (kind, prompt or messages) the model received."""
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
        seen.append(("integration", messages))
        out = integration_output() if callable(integration_output) \
            else integration_output
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
    assert texts[0].startswith("Profile No. 1\n")
    assert f"{MAIN_NOTE}\n\n{edit.get_content()}" in texts[0]
    assert "UPDATED, KEEPS MY CORRECTION" in texts[1]
    assert MAIN_NOTE not in texts[1]
    newest = UserProfile.query.get(result["profile_id"])
    assert newest.generation_type == "integration"
    assert newest.parent_profile_id == update.id


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
    chain = exports._collect_iterative_chain(update.id)
    assert [p.id for p in chain] == [first.id, second.id, update.id]


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


def test_a_from_scratch_first_chunk_is_not_stopped(app, monkeypatch):
    """A rebuild's first chunk reads no earlier version, so an edit saved
    meanwhile does not stop it (base_moved has no base to compare)."""
    import backend.tasks.exports as exports
    assert exports.base_moved(1, None) is False


# ── full rebuilds (main's behaviour; open for Peter) ─────────────────────

def test_a_rebuild_reads_no_earlier_version(app, monkeypatch):
    """A from-scratch build (an import older than the chain, a ladder step,
    admin Build profile) starts from the generation prompt over the oldest
    window; no profile version, the user's edit included, is in any of its
    prompts. Unchanged from main: how a rebuild should carry the user's
    edit forward is open for Peter (#414, Needs Peter)."""
    import backend.tasks.exports as exports
    user = _user("rebuilt", profile_needs_full_regen=True)
    _tip, integration = _finished_chain(user)
    _edit(user, integration, "MY CORRECTION")
    monkeypatch.setattr(exports, "should_continue_chain",
                        lambda user, profile: True)
    seen = _stand_in(
        monkeypatch, exports,
        [_window("2019 ARCHIVE", latest=datetime(2019, 12, 1)),
         _window("RECENT WRITING", latest=JUN)],
        [180_000, 90_000, 0], ["REBUILT 1", "REBUILT 2"])

    exports._iterative_generation(
        MagicMock(), user, "gpt-5.5",
        exports._load_prompt("profile_generation.txt", user_id=user.id),
        32_000, {})

    prompts = [p if kind == "chunk" else "\n".join(
        m["content"][0]["text"] for m in p) for kind, p in seen]
    assert [kind for kind, _ in seen] == ["chunk", "chunk", "integration"]
    assert "2019 ARCHIVE" in prompts[0]
    assert all("MY CORRECTION" not in p for p in prompts)
    assert all(MAIN_NOTE not in p for p in prompts)


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
