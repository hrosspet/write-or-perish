"""#183: every edit of a profile that changes the words is saved as a new
version the user wrote (generated_by "user"), dated by the edit, with the
version it changed as its predecessor (parent_profile_id). The edited
version stays in the history unchanged.

Decision (voice review, Peter, 2026-10-20): "user edits simply creating a
new version of profile, which will have the previous version linked as a
predecessor. This way the next ai update will have the information at
hand". The pipeline side is in test_profile_edit_wiring.
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
        created_at=datetime(2026, 5, 3),
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


def test_an_edit_is_a_new_version_with_its_predecessor_and_date(app, alice):  # noqa: F811
    generated = _generated(alice)
    before = datetime.utcnow()

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
    assert before <= new.created_at <= datetime.utcnow()     # the edit's date
    assert new.ai_usage == "chat"
    # Covers the same writing as the version it was edited from.
    assert new.source_tokens_used == 180_000
    assert new.source_data_cutoff == datetime(2026, 5, 1)
    assert new.source_origin_stats == generated.source_origin_stats
    assert UserProfile.query.count() == 2


def test_two_edits_make_two_versions(app, alice):  # noqa: F811
    """Q3 (voice review, 2026-10-20): an edit of the user's own version is
    a new version too, dated by that edit; the first edit stays as it
    was. Both are editions in the history."""
    generated = _generated(alice)
    first = _edit(app, alice, generated, "first edit")

    second = _edit(app, alice, UserProfile.query.get(first), "second edit")

    assert second != first
    one, two = UserProfile.query.get(first), UserProfile.query.get(second)
    assert one.get_content() == "first edit"
    assert two.get_content() == "second edit"
    assert two.parent_profile_id == one.id
    assert one.parent_profile_id == generated.id
    assert two.created_at >= one.created_at
    assert two.generated_by == "user"
    assert two.source_data_cutoff == generated.source_data_cutoff
    client = app.test_client()
    _login(client, alice)
    history = client.get("/profile/versions").get_json()["versions"]
    assert [v["id"] for v in history] == [second, first, generated.id]


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


def test_whitespace_only_change_makes_no_new_version(app, alice):  # noqa: F811
    """Blank lines, trailing spaces and a trailing newline are not an edit
    of the words: saved in place, no new version."""
    generated = _generated(alice)

    same_id = _edit(
        app, alice, generated,
        "### SURFACE MAP  \n\nLikes long walks.\n\n\nAvoids conflict.\n")

    assert same_id == generated.id
    assert UserProfile.query.count() == 1
    assert UserProfile.query.get(generated.id).generated_by == "gpt-5.5"


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


def test_edit_of_an_integration_carries_its_chain_tips_render_time(app, alice):  # noqa: F811
    """An integration has no render time of its own; the edit takes the
    one of the chain tip the integration merged, so the update gates
    decide as they did before the edit (test_profile_batch has the seeder
    side)."""
    tip, integration = _chain_tip_and_integration(alice)

    new = UserProfile.query.get(_edit(app, alice, integration, "my words"))

    assert new.parent_profile_id == integration.id
    assert new.source_rendered_at == tip.source_rendered_at
    assert new.source_data_cutoff == datetime(2026, 5, 1)
    assert new.source_tokens_used == 180_000


def test_revert_to_the_generated_version_is_the_newest_version(app, alice):  # noqa: F811
    """A revert from the history after an edit is a new 'revert' row, as on
    main. It is the newest version, so the next update builds on it and
    the edit it went back past does not come back."""
    from backend.tasks.exports import profile_update_base
    tip, integration = _chain_tip_and_integration(alice)
    _edit(app, alice, integration, "### SURFACE MAP\nMY EDIT")
    client = app.test_client()
    _login(client, alice)

    resp = client.post(f"/profile/revert/{integration.id}")

    assert resp.status_code == 200
    revert = UserProfile.query.get(resp.get_json()["profile"]["id"])
    assert revert.generation_type == "revert"
    assert revert.parent_profile_id == integration.id
    assert revert.generated_by == "gpt-5.5"
    assert revert.get_content() == integration.get_content()
    assert profile_update_base(alice.id).id == revert.id


def test_revert_to_an_integration_carries_its_coverage(app, alice):  # noqa: F811
    """A revert to an integration on a pre-filled (pinned) account carries
    the coverage an edit of it would, render time included, so the update
    gates decide as before the revert."""
    from backend.utils.profile_versions import coverage_of
    alice.profile_force_batch = True
    _db.session.commit()
    tip, integration = _chain_tip_and_integration(alice)
    _edit(app, alice, integration, "### SURFACE MAP\nMY EDIT")
    client = app.test_client()
    _login(client, alice)

    resp = client.post(f"/profile/revert/{integration.id}")

    assert resp.status_code == 200
    revert = UserProfile.query.get(resp.get_json()["profile"]["id"])
    assert revert.source_rendered_at == tip.source_rendered_at
    assert revert.source_origin_stats == integration.source_origin_stats
    assert revert.source_data_cutoff == integration.source_data_cutoff
    for key, value in coverage_of(alice, integration).items():
        assert getattr(revert, key) == value


def test_new_profile_follows_the_newest_version(app, alice):  # noqa: F811
    """POST /profile (the iPhone app's "Save as new" when a new version
    arrived during an edit) makes a version that follows the newest one,
    like an edit of it: that version is its parent and its coverage is
    copied, so the next update builds on the user's text and does not read
    the whole corpus again. The text is the user's, as sent."""
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


def test_an_edit_keeps_the_recent_context_summary(app, alice):  # noqa: F811
    """Review finding 3: chat replies look the recent-context summary up by
    the newest profile's id. The edit copies the edited version's summary
    to the new version (same text, cutoff and save time, no model call), so
    replies keep it right after the edit."""
    from backend.models import UserRecentContext
    from backend.utils.context_artifacts import _resolve_latest_artifact
    generated = _generated(alice)
    summary = UserRecentContext(
        user_id=alice.id, generated_by="gpt-5.5", tokens_used=300,
        created_at=datetime(2026, 5, 10),
        source_data_cutoff=datetime(2026, 5, 9), source_tokens_covered=4000,
        profile_id=generated.id, ai_usage="chat")
    summary.set_content("WHAT THE USER WROTE THIS WEEK")
    _db.session.add(summary)
    _db.session.commit()
    assert _resolve_latest_artifact("recent_context", alice.id) == summary.id

    new_id = _edit(app, alice, generated, "### SURFACE MAP\nMY EDIT")

    copy = UserRecentContext.query.filter_by(profile_id=new_id).one()
    assert copy.get_content() == "WHAT THE USER WROTE THIS WEEK"
    assert copy.source_data_cutoff == summary.source_data_cutoff
    assert copy.created_at == summary.created_at
    assert copy.source_tokens_covered == 4000
    assert copy.tokens_used == 0
    assert _resolve_latest_artifact("recent_context", alice.id) == copy.id
    assert UserRecentContext.query.get(summary.id).profile_id == generated.id


def test_saving_a_version_ends_a_pending_rebuild_merge(app, alice):  # noqa: F811
    """A rebuild merges the user's version the old chain carried
    (#183, option 2). A version the user saves meanwhile wins: the pending
    merge of the older one is dropped."""
    generated = _generated(alice)
    alice.profile_pending_merge_id = generated.id
    _db.session.commit()

    _edit(app, alice, generated, "### SURFACE MAP\nMY NEWER EDIT")

    assert _db.session.get(type(alice), alice.id).profile_pending_merge_id is None


def test_a_new_profile_or_a_revert_also_ends_a_pending_merge(app, alice):  # noqa: F811
    generated = _generated(alice)
    alice.profile_pending_merge_id = generated.id
    _db.session.commit()
    client = app.test_client()
    _login(client, alice)
    assert client.post("/profile/", json={"content": "NEW"}).status_code == 201
    assert _db.session.get(type(alice), alice.id).profile_pending_merge_id is None

    user = _db.session.get(type(alice), alice.id)
    user.profile_pending_merge_id = generated.id
    _db.session.commit()
    assert client.post(f"/profile/revert/{generated.id}").status_code == 200
    assert _db.session.get(type(alice), alice.id).profile_pending_merge_id is None


def test_a_none_edit_keeps_a_pending_merge(app, alice):  # noqa: F811
    """Review finding 4: an edit saved while the account is 'none' is a
    'none' version, never a base, so the pending merge of the user's older
    version is kept and runs once the account allows AI again."""
    generated = _generated(alice)
    own = UserProfile(user_id=alice.id, generated_by="user", tokens_used=0,
                      ai_usage="chat", created_at=datetime(2026, 5, 4))
    own.set_content("MY OLDER EDIT")
    _db.session.add(own)
    _db.session.commit()
    alice.profile_pending_merge_id = own.id
    alice.default_ai_usage = "none"
    _db.session.commit()

    new_id = _edit(app, alice, generated, "### SURFACE MAP\nWRITTEN UNDER NONE")

    assert UserProfile.query.get(new_id).ai_usage == "none"
    assert _db.session.get(type(alice), alice.id).profile_pending_merge_id == own.id
