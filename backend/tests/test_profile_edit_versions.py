"""#183: an edit of a generated profile is saved as a new version the user
wrote (generated_by "user"), not over the generated version.

Decision (voice review, Peter, 2026-10-02): a job that regenerates
something the user has edited keeps the user's edits and refreshes only its
own part. The profile jobs can only do that when the edit is recorded as
the user's text, and the generated version stays in the history.
"""
from datetime import datetime

from backend.tests.test_tts_invalidation import (  # noqa: F401
    app, alice, _login, _db,
)
from backend.models import UserProfile, TTSChunk


def _generated(user, content="### SURFACE MAP\nLikes long walks.\nAvoids conflict.",
               with_audio=False):
    profile = UserProfile(
        user_id=user.id, generated_by="gpt-5.5", tokens_used=1200,
        ai_usage="chat", generation_type="integration",
        source_tokens_used=180_000, source_data_cutoff=datetime(2026, 5, 1),
        source_origin_stats={"loore": {"nodes": 40, "tokens": 180_000}},
        source_rendered_at=datetime(2026, 5, 2),
        audio_tts_url="user/1/profile/tts.mp3" if with_audio else None,
        tts_task_status="completed" if with_audio else None)
    profile.set_content(content)
    _db.session.add(profile)
    _db.session.commit()
    if with_audio:
        _db.session.add(TTSChunk(
            profile_id=profile.id, chunk_index=0, section_index=0,
            section_title="SURFACE MAP", audio_url="user/1/profile/c0.mp3",
            duration=12.5, status="completed"))
        _db.session.commit()
    return profile


def _edit(app, user, profile, content, **extra):  # noqa: F811
    client = app.test_client()
    _login(client, user)
    resp = client.put(f"/profile/{profile.id}",
                      json={"content": content, **extra})
    assert resp.status_code == 200
    return resp.get_json()["profile"]["id"]


def test_edit_of_generated_version_is_a_new_user_version(app, alice):  # noqa: F811
    generated = _generated(alice)

    new_id = _edit(app, alice, generated,
                   "### SURFACE MAP\nLikes long walks.\nSays no when it matters.")

    assert new_id != generated.id
    old = UserProfile.query.get(generated.id)
    assert old.get_content().endswith("Avoids conflict.")    # history intact
    assert old.generated_by == "gpt-5.5"
    new = UserProfile.query.get(new_id)
    assert new.generated_by == "user"
    assert new.get_content().endswith("Says no when it matters.")
    assert new.parent_profile_id == generated.id
    assert new.ai_usage == "chat"
    # Covers the same writing as the version it was edited from.
    assert new.source_tokens_used == 180_000
    assert new.source_data_cutoff == datetime(2026, 5, 1)
    assert new.source_origin_stats == generated.source_origin_stats
    assert new.source_rendered_at == datetime(2026, 5, 2)
    assert UserProfile.query.count() == 2


def test_keeping_audio_carries_it_to_the_new_version(app, alice):  # noqa: F811
    """"Keep existing audio" (no regenerate flag): the new version plays
    the same files; the edited version keeps them too."""
    generated = _generated(alice, with_audio=True)

    new_id = _edit(app, alice, generated, "edited text")

    new = UserProfile.query.get(new_id)
    assert new.audio_tts_url == "user/1/profile/tts.mp3"
    assert new.tts_task_status == "completed"
    chunks = TTSChunk.query.filter_by(profile_id=new_id).all()
    assert [(c.audio_url, c.section_title) for c in chunks] == [
        ("user/1/profile/c0.mp3", "SURFACE MAP")]
    assert UserProfile.query.get(generated.id).audio_tts_url
    assert TTSChunk.query.filter_by(profile_id=generated.id).count() == 1


def test_regenerating_audio_leaves_the_new_version_without_it(app, alice):  # noqa: F811
    """"Regenerate": the new version starts without audio; the edited
    version's text did not change, so its audio stays."""
    generated = _generated(alice, with_audio=True)

    new_id = _edit(app, alice, generated, "edited text", regenerate_tts=True)

    new = UserProfile.query.get(new_id)
    assert new.audio_tts_url is None
    assert TTSChunk.query.filter_by(profile_id=new_id).count() == 0
    old = UserProfile.query.get(generated.id)
    assert old.audio_tts_url == "user/1/profile/tts.mp3"
    assert TTSChunk.query.filter_by(profile_id=generated.id).count() == 1


def test_privacy_only_change_stays_in_place(app, alice):  # noqa: F811
    generated = _generated(alice)

    same_id = _edit(app, alice, generated, generated.get_content(),
                    privacy_level="private")

    assert same_id == generated.id
    assert UserProfile.query.count() == 1


def test_editing_the_user_version_again_stays_in_place(app, alice):  # noqa: F811
    """The first edit makes the user's version; later edits of it change
    that version, so one editing session does not pile up versions."""
    generated = _generated(alice)
    first = _edit(app, alice, generated, "first edit")

    second = _edit(app, alice, UserProfile.query.get(first), "second edit")

    assert second == first
    assert UserProfile.query.get(first).get_content() == "second edit"
    assert UserProfile.query.count() == 2


# ── Review of #414 ──────────────────────────────────────────────────────

def _chain_tip_and_integration(user):
    """A finished chain as both pipelines save it: the last chunk carries
    the render time; the integration on top of it has none of its own."""
    tip = UserProfile(
        user_id=user.id, generated_by="gpt-5.5", tokens_used=900,
        ai_usage="chat", generation_type="iterative",
        source_tokens_used=180_000, source_data_cutoff=datetime(2026, 5, 1),
        source_origin_stats={"x": {"nodes": 900, "tokens": 180_000}},
        source_rendered_at=datetime(2026, 5, 2),
        created_at=datetime(2026, 5, 2))
    tip.set_content("chunk text")
    _db.session.add(tip)
    _db.session.flush()
    integration = UserProfile(
        user_id=user.id, generated_by="gpt-5.5", tokens_used=1200,
        ai_usage="chat", generation_type="integration",
        source_tokens_used=180_000, source_data_cutoff=datetime(2026, 5, 1),
        source_origin_stats={"x": {"nodes": 900, "tokens": 180_000}},
        parent_profile_id=tip.id, created_at=datetime(2026, 5, 3))
    integration.set_content("### SURFACE MAP\nLikes long walks.")
    _db.session.add(integration)
    _db.session.commit()
    return tip, integration


def test_whitespace_only_change_makes_no_new_version(app, alice):  # noqa: F811
    """Finding 2: blank lines, trailing spaces and a trailing newline are
    not an edit of the words. The generated version is saved in place and
    no user version is made, so the jobs never label the generated text as
    the user's own."""
    generated = _generated(alice)

    same_id = _edit(
        app, alice, generated,
        "### SURFACE MAP  \n\nLikes long walks.\n\n\nAvoids conflict.\n")

    assert same_id == generated.id
    assert UserProfile.query.count() == 1
    assert UserProfile.query.get(generated.id).generated_by == "gpt-5.5"


def test_edit_of_an_integration_carries_its_chain_tips_render_time(app, alice):  # noqa: F811
    """Finding 3: an integration has no render time of its own; the edit
    takes the one of the chain tip the integration merged, so the update
    gates decide as they did before the edit (test_profile_batch has the
    seeder side)."""
    tip, integration = _chain_tip_and_integration(alice)

    new = UserProfile.query.get(_edit(app, alice, integration, "my words"))

    assert new.parent_profile_id == integration.id
    assert new.source_rendered_at == tip.source_rendered_at
    assert new.source_data_cutoff == datetime(2026, 5, 1)
    assert new.source_tokens_used == 180_000


def test_revert_from_the_history_is_marked_as_the_users(app, alice):  # noqa: F811
    """Finding 1: a revert the user makes is typed USER_REVERT, so the
    jobs can tell it from the pipeline's re-tips (typed "revert")."""
    from backend.utils.profile_versions import USER_REVERT
    generated = _generated(alice)
    _edit(app, alice, generated, "### SURFACE MAP\nMY EDIT")
    client = app.test_client()
    _login(client, alice)

    resp = client.post(f"/profile/revert/{generated.id}")

    assert resp.status_code == 200
    revert = UserProfile.query.get(resp.get_json()["profile"]["id"])
    assert revert.generation_type == USER_REVERT
    assert revert.parent_profile_id == generated.id
    assert revert.generated_by == "gpt-5.5"
    assert revert.get_content() == generated.get_content()


def test_revert_to_an_integration_carries_its_coverage(app, alice):  # noqa: F811
    """A revert to an integration on a pre-filled (pinned) account carries
    the coverage an edit of it would, render time included, so the update
    gates decide as before the revert (same rule as finding 3)."""
    from backend.utils.profile_versions import coverage_of
    alice.profile_force_batch = True
    tip = _generated(alice)
    tip.generation_type = "iterative"
    tip.source_rendered_at = datetime(2026, 5, 3)
    integration = _generated(alice)
    integration.parent_profile_id = tip.id
    integration.source_rendered_at = None
    _db.session.commit()
    _edit(app, alice, integration, "### SURFACE MAP\nMY EDIT")
    client = app.test_client()
    _login(client, alice)

    resp = client.post(f"/profile/revert/{integration.id}")

    assert resp.status_code == 200
    revert = UserProfile.query.get(resp.get_json()["profile"]["id"])
    assert revert.source_rendered_at == datetime(2026, 5, 3)
    assert revert.source_origin_stats == integration.source_origin_stats
    assert revert.source_data_cutoff == integration.source_data_cutoff
    for key, value in coverage_of(alice, integration).items():
        assert getattr(revert, key) == value


def test_new_profile_follows_the_newest_version(app, alice):  # noqa: F811
    """Finding 7: POST /profile (the iPhone app's "Save as new" when a new
    version arrived during an edit) makes a version that follows the
    newest one: that version is its parent and its coverage is copied, so
    the next update does not read the whole corpus again, and the jobs see
    which lines the user changed. The text is the user's, as sent."""
    tip, integration = _chain_tip_and_integration(alice)
    client = app.test_client()
    _login(client, alice)

    resp = client.post("/profile/",
                       json={"content": "### SURFACE MAP\nMY WORDS"})

    assert resp.status_code == 201
    new = UserProfile.query.get(resp.get_json()["profile"]["id"])
    assert new.generated_by == "user"
    assert new.get_content() == "### SURFACE MAP\nMY WORDS"
    assert new.parent_profile_id == integration.id
    assert new.source_data_cutoff == datetime(2026, 5, 1)
    assert new.source_tokens_used == 180_000
    assert new.source_origin_stats == integration.source_origin_stats
    assert new.source_rendered_at == tip.source_rendered_at


def test_first_profile_written_from_scratch_has_no_parent(app, alice):  # noqa: F811
    client = app.test_client()
    _login(client, alice)

    resp = client.post("/profile/", json={"content": "about me"})

    new = UserProfile.query.get(resp.get_json()["profile"]["id"])
    assert new.parent_profile_id is None
    assert new.source_data_cutoff is None
    assert new.source_rendered_at is None
    assert not new.source_tokens_used
