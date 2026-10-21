"""The profile update and integration prompts (#544, #549, #545).

- Every update step embeds the generation prompt's section instructions
  (Surface, Shadow and Light Map, the generation rules), cut at the exact
  "## OUTPUT" heading, not at "## OUTPUT FORMAT" near the top (#544).
- The integration prompt embeds the same instructions, without the
  generation prompt's empty {user_export} input, and ends with its own
  output line (#549).
- {N_MONTHS} covers the whole history, from the first version's window
  start (the user's earliest AI-readable writing) to the newest cutoff,
  and says "1 month", not "1 months" (#549).
- Each version's label gives the writing it covers from the first
  window's start and, in a chain, the writing it added to the version
  before it; the merge call (cumulative) labels full profiles side by side
  (#545).
- The integration prompt asks for the newest value of a fact that changes.

In-memory SQLite, ENCRYPTION_DISABLED, celery mocked so the modules
import. Synthetic profiles and writing only; no model is called.
"""
import os
import re
import sys
from datetime import datetime
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

from backend.extensions import db as _db                 # noqa: E402
from backend.models import User, UserProfile, Node      # noqa: E402

PROMPTS = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "prompts")
PLACEHOLDER = re.compile(r"\{[A-Za-z_]+\}")


@pytest.fixture
def app():
    # Warm celery_app first so it resolves the exports <-> profile_batch
    # import cycle in the safe order (see test_profile_regen_resume).
    import backend.celery_app  # noqa: F401
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SUPPORTED_MODELS"] = {}
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def _read(name):
    with open(os.path.join(PROMPTS, name), encoding="utf-8") as f:
        return f.read()


def _user(name="writer"):
    user = User(username=name, plan="alpha", twitter_id=None, approved=True,
                default_ai_usage="chat")
    _db.session.add(user)
    _db.session.commit()
    return user


def _writing(user, at, ai_usage="chat", deleted=False):
    node = Node(user_id=user.id, node_type="user", ai_usage=ai_usage,
                token_count=1000, created_at=at, updated_at=at,
                deleted_at=at if deleted else None)
    node.set_content("synthetic writing")
    _db.session.add(node)
    _db.session.commit()
    return node


def _version(user, cutoff, parent=None, text="SYNTHETIC PROFILE",
             generated_by="gpt-5.5", generation_type="iterative",
             merged_profile_id=None):
    profile = UserProfile(
        user_id=user.id, generated_by=generated_by, tokens_used=0,
        ai_usage="chat", generation_type=generation_type,
        source_tokens_used=1000, source_data_cutoff=cutoff,
        parent_profile_id=parent.id if parent else None,
        merged_profile_id=merged_profile_id)
    profile.set_content(text)
    _db.session.add(profile)
    _db.session.commit()
    return profile


def _texts(messages):
    return [m["content"][0]["text"] for m in messages]


def _label(text):
    return text.split("\n\n", 1)[0]


# ── #544: the update template has the section instructions ──────────────

def test_update_template_embeds_the_whole_structure(app):
    """The update template holds everything of the generation prompt
    except its closing OUTPUT and INPUT sections: the Shadow and Light Map
    instructions with their sub-headings and the generation rules, as a
    from-scratch build sends them. The update template's own framing
    stays."""
    import backend.tasks.exports as exports
    user = _user()
    gen = _read("profile_generation.txt")
    template = exports.build_update_template(user.id)

    structure = gen.split("\n## OUTPUT\n", 1)[0].rstrip()
    assert structure in template
    for heading in ("### SECTION 1: SURFACE MAP", "### SECTION 2: SHADOW MAP",
                    "**Hidden Architecture**", "**Shadow Guess**",
                    "### SECTION 3: LIGHT MAP", "**Unowned Strengths**",
                    "**Light Guess**", "## GENERATION RULES"):
        assert heading in template, heading
    # The generation prompt's own output line and empty input are not.
    assert "{user_export}" not in template
    assert "<user_export>" not in template
    assert "Generate the profile now." not in template
    # The update template's own parts.
    assert "{existing_profile}" in template and "{new_data}" in template
    assert "Generate the updated profile now." in template
    assert "<profile_structure>\n" + structure + "\n</profile_structure>" \
        in template


def test_the_cut_is_an_exact_heading_line(app):
    import backend.tasks.exports as exports
    cut = exports.profile_structure_instructions
    gen = ("Intro.\n\n## OUTPUT FORMAT\n### SECTION 1\nA\n\n"
           "## GENERATION RULES\n1. B\n\n## OUTPUT\nGo.\n\n## INPUT\n"
           "<user_export>\n{user_export}\n</user_export>\n")
    assert cut(gen) == ("Intro.\n\n## OUTPUT FORMAT\n### SECTION 1\nA\n\n"
                        "## GENERATION RULES\n1. B")
    # A prompt with an INPUT section and no OUTPUT one: cut at INPUT.
    assert cut("Intro.\n## OUTPUT FORMAT\nA\n## INPUT\n{user_export}") == \
        "Intro.\n## OUTPUT FORMAT\nA"
    # A prompt of the user's own with neither heading is embedded whole.
    own = "Describe me in three sections: {user_export}"
    assert cut(own) == own


# ── #549: the integration prompt ─────────────────────────────────────────

def test_integration_prompt_has_no_unreplaced_placeholder(app):
    import backend.tasks.exports as exports
    user = _user()
    _writing(user, datetime(2025, 1, 10))
    first = _version(user, datetime(2025, 3, 1))
    second = _version(user, datetime(2025, 6, 1), parent=first)

    messages, chain = exports.build_integration_messages(user.id, second.id)
    assert [p.id for p in chain] == [first.id, second.id]
    prompt = _texts(messages)[-1]

    assert PLACEHOLDER.search(prompt) is None
    assert "<user_export>" not in prompt and "## INPUT" not in prompt
    gen = _read("profile_generation.txt")
    assert gen.split("\n## OUTPUT\n", 1)[0].rstrip() in prompt
    assert "### SECTION 2: SHADOW MAP" in prompt
    assert "### SECTION 3: LIGHT MAP" in prompt
    # The integration's own output line closes the prompt.
    assert prompt.endswith(
        '## OUTPUT\nGenerate the unified profile now. No preamble, no '
        'meta-commentary. Begin directly with "### SURFACE MAP"')


def test_integration_prompt_asks_for_the_newest_value(app):
    import backend.tasks.exports as exports
    user = _user()
    first = _version(user, datetime(2025, 3, 1))
    second = _version(user, datetime(2025, 6, 1), parent=first)
    prompt = _texts(exports.build_integration_messages(user.id, second.id)[0])[-1]
    assert ("When a fact changes over time (an age, where they live, a job, "
            "the state of a relationship or a dispute), state its newest "
            "value; the dates given with each profile tell which is newest."
            in prompt)
    assert ("Where the writing gives a birth year or a date, state that "
            "rather than an age." in prompt)


def test_n_months_spans_the_whole_history(app):
    """The first version's own window counts: from the earliest writing
    (2024-01-10) to the newest cutoff (2025-07-01) is 538 days, ~18
    months. The old count, cutoff to cutoff, said 17."""
    import backend.tasks.exports as exports
    user = _user()
    _writing(user, datetime(2024, 1, 10))
    _writing(user, datetime(2024, 1, 20))
    v1 = _version(user, datetime(2024, 2, 1))
    v2 = _version(user, datetime(2025, 1, 1), parent=v1)
    v3 = _version(user, datetime(2025, 7, 1), parent=v2)

    prompt = _texts(exports.build_integration_messages(user.id, v3.id)[0])[-1]
    assert "over the course of ~18 months." in prompt


def test_n_months_ignores_writing_ai_may_not_read(app):
    """Writing set to 'none' or deleted does not move the start."""
    import backend.tasks.exports as exports
    user = _user()
    _writing(user, datetime(2020, 1, 1), ai_usage="none")
    _writing(user, datetime(2021, 1, 1), deleted=True)
    _writing(user, datetime(2024, 1, 10))
    v1 = _version(user, datetime(2024, 2, 1))
    v2 = _version(user, datetime(2025, 7, 1), parent=v1)

    messages, _chain = exports.build_integration_messages(user.id, v2.id)
    assert "over the course of ~18 months." in _texts(messages)[-1]
    assert _label(_texts(messages)[0]).startswith(
        "Profile No. 1\n- Covers all the writing from 2024-01-10 to")


def test_one_month_is_singular(app):
    import backend.tasks.exports as exports
    user = _user()
    _writing(user, datetime(2025, 5, 1))
    v1 = _version(user, datetime(2025, 5, 10))
    v2 = _version(user, datetime(2025, 5, 20), parent=v1)

    prompt = _texts(exports.build_integration_messages(user.id, v2.id)[0])[-1]
    assert "over the course of ~1 month." in prompt
    assert "1 months" not in prompt


def test_months_fall_back_to_the_cutoffs_without_writing(app):
    """No AI-readable writing before the first cutoff (deleted since, or
    set to 'none'): the span runs from the oldest cutoff, as before."""
    import backend.tasks.exports as exports
    assert exports._calculate_months_span(None, datetime(2025, 1, 1)) == 1
    assert exports._calculate_months_span(
        datetime(2024, 1, 1), datetime(2025, 1, 1)) == 12
    user = _user()
    v1 = _version(user, datetime(2024, 1, 1))
    v2 = _version(user, datetime(2025, 1, 1), parent=v1)
    messages, _chain = exports.build_integration_messages(user.id, v2.id)
    assert "over the course of ~12 months." in _texts(messages)[-1]
    assert _label(_texts(messages)[0]) == (
        "Profile No. 1\n"
        "- Covers all the writing from the start to 2024-01-01\n"
        "- The first version")


# ── #545: the labels say what each version covers ────────────────────────

def test_chain_labels_give_the_added_window_and_the_whole_coverage(app):
    import backend.tasks.exports as exports
    user = _user()
    _writing(user, datetime(2024, 1, 10))
    v1 = _version(user, datetime(2024, 2, 1), text="FIRST")
    v2 = _version(user, datetime(2025, 1, 1), parent=v1, text="SECOND")
    v3 = _version(user, datetime(2025, 7, 1), parent=v2, text="THIRD")

    texts = _texts(exports.build_integration_messages(user.id, v3.id)[0])
    assert texts[:3] == [
        "Profile No. 1\n"
        "- Covers all the writing from 2024-01-10 to 2024-02-01\n"
        "- The first version\n\nFIRST",
        "Profile No. 2\n"
        "- Covers all the writing from 2024-01-10 to 2025-01-01\n"
        "- Updates Profile No. 1 with the writing from 2024-02-01 to "
        "2025-01-01\n\nSECOND",
        "Profile No. 3\n"
        "- Covers all the writing from 2024-01-10 to 2025-07-01\n"
        "- Updates Profile No. 2 with the writing from 2025-01-01 to "
        "2025-07-01\n\nTHIRD",
    ]


def test_a_merge_step_in_the_chain_adds_no_writing(app):
    """A rebuild's merge step (#183) has its base's cutoff and carries a
    version the user wrote: its label says so instead of an empty window."""
    import backend.tasks.exports as exports
    user = _user()
    _writing(user, datetime(2024, 1, 10))
    own = _version(user, datetime(2024, 6, 1), generated_by="user",
                   generation_type="update", text="IN MY WORDS")
    v1 = _version(user, datetime(2024, 3, 1))
    v2 = _version(user, datetime(2024, 6, 1), parent=v1)
    merge = _version(user, datetime(2024, 6, 1), parent=v2,
                     merged_profile_id=own.id, text="MERGED")
    v3 = _version(user, datetime(2024, 9, 1), parent=merge)

    texts = _texts(exports.build_integration_messages(user.id, v3.id)[0])
    assert _label(texts[2]) == (
        "Profile No. 3\n"
        "- Covers all the writing from 2024-01-10 to 2024-06-01\n"
        "- Merges Profile No. 2 with a version the user wrote; adds no new "
        "writing")
    assert _label(texts[3]) == (
        "Profile No. 4\n"
        "- Covers all the writing from 2024-01-10 to 2024-09-01\n"
        "- Updates Profile No. 3 with the writing from 2024-06-01 to "
        "2024-09-01")


def test_a_hand_written_first_version_with_no_coverage(app):
    """A profile written by hand before any build (no cutoff) starts the
    chain: the next version's window starts at the earliest writing."""
    import backend.tasks.exports as exports
    user = _user()
    _writing(user, datetime(2025, 1, 2))
    own = _version(user, None, generated_by="user",
                   generation_type="initial", text="ABOUT ME")
    v2 = _version(user, datetime(2025, 6, 1), parent=own,
                  generation_type="update")

    texts = _texts(exports.build_integration_messages(user.id, v2.id)[0])
    assert _label(texts[0]) == (
        "Profile No. 1\n"
        "- Covers all the writing from the start to unknown\n"
        "- The first version")
    assert texts[0].endswith(f"{exports.USER_WRITTEN_PROFILE_NOTE}\n\nABOUT ME")
    assert _label(texts[1]) == (
        "Profile No. 2\n"
        "- Covers all the writing from 2025-01-02 to 2025-06-01\n"
        "- Updates Profile No. 1 with the writing from 2025-01-02 to "
        "2025-06-01")
    assert "over the course of ~5 months." in texts[-1]


def test_the_merge_call_labels_two_full_profiles(app):
    """build_merge_messages (cumulative): the rebuilt version and the
    user's version both cover the writing from its start; neither label
    claims an update of the other, and {N_MONTHS} runs from the start to
    the newer cutoff (the old count went from the base's cutoff back to
    the user's version's and said 1)."""
    import backend.tasks.exports as exports
    user = _user()
    _writing(user, datetime(2024, 1, 10))
    base = _version(user, datetime(2025, 7, 1), text="REBUILT")
    own = _version(user, datetime(2025, 5, 1), generated_by="user",
                   generation_type="update", text="IN MY WORDS")

    texts = _texts(exports.build_merge_messages(user.id, base, own))
    assert len(texts) == 3
    assert texts[0] == ("Profile No. 1\n"
                        "- Covers all the writing from 2024-01-10 to "
                        "2025-07-01\n\nREBUILT")
    assert texts[1] == ("Profile No. 2\n"
                        "- Covers all the writing from 2024-01-10 to "
                        "2025-05-01\n\n"
                        f"{exports.USER_WRITTEN_PROFILE_NOTE}\n\nIN MY WORDS")
    assert "over the course of ~18 months." in texts[2]
    assert PLACEHOLDER.search(texts[2]) is None
