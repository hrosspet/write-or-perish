"""Listen does not read code blocks aloud (#646).

The batch TTS task speaks a fenced code block as one short phrase, for a
node, a profile and a saved reference alike, and a ``# comment`` inside
the block makes no chapter. Audio made before stays as it is: it is not
made again or billed again. A Voice reply spoken while it is written is
covered in test_live_streaming.py, and the text rules themselves in
test_tts_links.py.

Runs the real backend.tasks.tts on the test_tts_chunk_cost_resume
harness: no TTS call is made.
"""
from unittest.mock import MagicMock

from backend.extensions import db as _db
from backend.models import ExternalItem, UserProfile
from backend.tests.test_tts_chunk_cost_resume import (  # noqa: F401 (fixtures)
    FakeTTS, _costs, _node, _rows, _speak, app, tts, users)
from backend.utils.spoken_links import CODE_BLOCK_SPOKEN

CODE = ("```python\n# load the file\nimport json\n## parse it\n"
        "data = json.loads(raw)\n# done\n```")


def test_a_node_speaks_its_code_block_as_one_phrase(app, tts, users):  # noqa: F811
    alice, _ = users
    node = _node(alice, "## Setup\nRead the file:\n\n" + CODE
                 + "\n\nThen use `data` below.\n\n## Done\nThat is all.")
    fake = FakeTTS(tts)

    _speak(tts, app, node, fake)

    assert fake.calls == [
        f"Setup.\n\nRead the file:\n\n{CODE_BLOCK_SPOKEN}\n\n"
        "Then use `data` below.",
        "Done.\n\nThat is all."]
    # The chapters are the entry's own headings, none from the code.
    assert [(r.section_title, r.section_index)
            for r in _rows({"node_id": node.id})] == [
        ("Setup", 0), ("Done", 1)]


def test_audio_made_before_is_kept_and_not_billed_again(app, tts, users):  # noqa: F811
    """Control (passes before #646 too): a node whose audio exists keeps
    it, code read aloud and all, until its audio is made again."""
    alice, _ = users
    node = _node(alice, "Code:\n\n" + CODE)
    node.audio_tts_url = f"/media/user/{alice.id}/node/{node.id}/tts.mp3?v=1"
    _db.session.commit()
    fake = FakeTTS(tts)

    result = _speak(tts, app, node, fake)

    assert fake.calls == []
    assert _costs() == []
    assert result["tts_url"] == node.audio_tts_url


def test_a_profile_and_a_reference_leave_code_out_too(app, tts, users):  # noqa: F811
    alice, _ = users
    profile = UserProfile(user_id=alice.id, generated_by="user",
                          tokens_used=0, ai_usage="chat")
    profile.set_content("I write Python.\n\n" + CODE)
    item = ExternalItem(user_id=alice.id, source="web_clip",
                        external_id="clip-646", title="A tutorial",
                        url="https://example.org/t")
    item.set_content("Step one:\n\n" + CODE + "\n\nStep two.")
    _db.session.add_all([profile, item])
    _db.session.commit()
    fake = FakeTTS(tts)
    tts.synthesize_to_file = fake

    tts.generate_tts_audio_for_profile(
        MagicMock(), profile.id, str(app.audio_root),
        requesting_user_id=alice.id)
    tts.generate_tts_audio_for_item(
        MagicMock(), item.id, str(app.audio_root),
        requesting_user_id=alice.id)

    assert fake.calls == [
        f"I write Python.\n\n{CODE_BLOCK_SPOKEN}",
        f"A tutorial.\n\nStep one:\n\n{CODE_BLOCK_SPOKEN}\n\nStep two."]
