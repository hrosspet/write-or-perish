"""Every refusal of a Voice turn is said aloud (#651), as the approval
announcement is (Peter, 2026-11-12: "the announcement should be TTSed as
a separate chunk and appended to the queue. So that when it's not
pending, it's not voiced out anymore").

One fixed sentence per refusal (utils/voice_refusals), its own cached clip
per voice (utils/announcement_audio), served by GET
/api/voice/refusal/<code>.mp3 only to accounts with Voice mode, the same
check the approval announcement now has. A recording saved without a
reply names its refusal (``warning_code``), so the clients know which
sentence to say.
"""
import hashlib
import re

import pytest

from backend.tests.test_voice_approval_routes import (  # noqa: F401
    app, clips, _voice_reply)
from backend.tests.test_tool_approval_routes import (  # noqa: F401
    _client, thread)
from backend.tests.test_no_replies_for_none import st  # noqa: F401
from backend.extensions import db as _db
from backend.models import Draft
from backend.utils.voice_refusals import (
    AI_USAGE_NONE_CODE, APPROVAL_PENDING_CODE, SPEND_CAP_CODE,
    SPOKEN_REFUSALS, SUBSCRIPTION_CODES, SUBSCRIPTION_SENTENCES,
    THREAD_TOO_LONG_CODE, skipped_reply_code,
)

CODES = [SPEND_CAP_CODE, AI_USAGE_NONE_CODE, APPROVAL_PENDING_CODE,
         THREAD_TOO_LONG_CODE]


def _refusal(client, code):
    return client.get(f"/api/voice/refusal/{code}.mp3")


# ── The sentences ────────────────────────────────────────────────────────

def test_one_sentence_per_refusal_in_plain_words():
    """No numbers, no prices, nothing about buying (the iPhone app may not
    point to purchasing). A recording kept after the account's access
    ended (#438) names the state in the words the screen shows
    (subscriptions.paused_message), never where or how to pay."""
    assert sorted(SPOKEN_REFUSALS) == sorted(
        CODES + list(SUBSCRIPTION_CODES.values()))
    for code, text in SPOKEN_REFUSALS.items():
        assert not re.search(r"[0-9$€£%]", text), text
        assert not re.search(r"\b(buy|price|upgrade|plan|subscribe)",
                             text, re.I), text
        if code not in SUBSCRIPTION_SENTENCES:
            assert not re.search(r"\b(pay|subscri)", text, re.I), text


def test_the_codes_are_the_ones_the_routes_answer_with():
    from backend.utils.llm_nodes import AI_USAGE_NONE_CODE as ai_code
    from backend.utils.spend import spend_cap_payload
    from backend.utils.thread_size import THREAD_TOO_LONG_CODE as length
    from backend.utils.tool_approvals import APPROVAL_PENDING_CODE as waits
    assert AI_USAGE_NONE_CODE == ai_code
    assert THREAD_TOO_LONG_CODE == length
    assert APPROVAL_PENDING_CODE == waits
    assert spend_cap_payload()["error"] == SPEND_CAP_CODE


def test_the_length_keeps_its_sentence():
    """#648's sentence, which the clients also say when no clip comes."""
    assert SPOKEN_REFUSALS[THREAD_TOO_LONG_CODE] == (
        "This thread is too long for another reply. Start a new thread "
        "to keep talking.")


# ── The code beside a recording's warning ───────────────────────────────

def test_each_skipped_reply_names_its_refusal(st):  # noqa: F811
    """The copies in utils/voice_refusals are the task's own texts."""
    from backend.utils.thread_size import (
        RECORDING_REPLY_SKIPPED_MESSAGE, RECORDING_REPLY_SKIPPED_SIZE_MESSAGE,
    )
    assert skipped_reply_code(
        st.VOICE_REPLY_SKIPPED_SPEND_CAP) == SPEND_CAP_CODE
    assert skipped_reply_code(
        st.VOICE_REPLY_SKIPPED_AI_USAGE) == AI_USAGE_NONE_CODE
    assert skipped_reply_code(
        st.VOICE_REPLY_SKIPPED_APPROVAL) == APPROVAL_PENDING_CODE
    assert skipped_reply_code(
        RECORDING_REPLY_SKIPPED_MESSAGE) == THREAD_TOO_LONG_CODE
    assert skipped_reply_code(
        RECORDING_REPLY_SKIPPED_SIZE_MESSAGE) == THREAD_TOO_LONG_CODE


def test_warnings_that_are_not_refusals_have_no_code(st):  # noqa: F811
    """Nothing heard, nothing transcribed, a placeholder error: a toast
    only, as before."""
    for warning in (st.VOICE_REPLY_SKIPPED_NO_WORDS,
                    st.VOICE_REPLY_SKIPPED_NOT_TRANSCRIBED,
                    st.VOICE_REPLY_SKIPPED_READ_ONLY.format(model="x"),
                    "Unknown key in {user_export}", None, ""):
        assert skipped_reply_code(warning) is None


@pytest.mark.parametrize("code", [SPEND_CAP_CODE, AI_USAGE_NONE_CODE,
                                  APPROVAL_PENDING_CODE])
def test_the_streaming_status_carries_the_code(app, thread, code):
    """The polling fallback (the SSE's all_complete carries the same)."""
    from backend.utils.voice_refusals import _SKIPPED_REPLY_CODES
    warning = next(text for text, c in _SKIPPED_REPLY_CODES.items()
                   if c == code)
    ben = thread[0]
    draft = Draft(user_id=ben.id, session_id=f"sess-{code}",
                  streaming_status="completed", label="Voice",
                  privacy_level="private", ai_usage="chat",
                  streaming_warning=warning)
    draft.set_content("my words")
    _db.session.add(draft)
    _db.session.commit()

    data = _client(app, ben).get(
        f"/api/drafts/streaming/sess-{code}/status").get_json()

    assert data["warning"] == warning
    assert data["warning_code"] == code


# ── The clip ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code", CODES)
def test_each_refusal_has_its_clip(app, thread, clips, code):
    ben = thread[0]
    client = _client(app, ben)

    audio = _refusal(client, code)

    assert audio.status_code == 200, audio.get_data(as_text=True)
    assert audio.mimetype == "audio/mpeg"
    assert audio.data == b"ID3" + SPOKEN_REFUSALS[code].encode()
    assert clips["calls"] == [SPOKEN_REFUSALS[code]]
    assert clips["costs"] == [(ben.id, 4.2)]


def test_the_clip_is_made_once_per_voice(app, thread, clips):
    """Cached on disk for every account; a new voice makes a new clip."""
    ben, anna = thread[0], thread[1]
    _refusal(_client(app, ben), SPEND_CAP_CODE)
    again = _refusal(_client(app, anna), SPEND_CAP_CODE)
    assert again.status_code == 200
    assert len(clips["calls"]) == 1 and clips["costs"] == [(ben.id, 4.2)]

    clips["voice"] = "nova"
    assert _refusal(_client(app, anna), SPEND_CAP_CODE).status_code == 200
    assert len(clips["calls"]) == 2


def test_a_revalidated_clip_not_cached_for_a_day(app, thread, clips):
    """The address names no voice: a browser asks again (a 304 when it is
    the same clip) instead of keeping an older voice's clip."""
    resp = _refusal(_client(app, thread[0]), AI_USAGE_NONE_CODE)
    assert "no-cache" in resp.headers["Cache-Control"]
    assert resp.headers.get("ETag")


def test_an_unknown_refusal_is_not_found(app, thread, clips):
    client = _client(app, thread[0])
    for name in ("loud", "one", "spend_cap", "..%2Fsecret"):
        assert _refusal(client, name).status_code == 404
    assert clips["calls"] == []


def test_no_clip_when_it_cannot_be_made(app, thread, clips):
    """No TTS key or a provider error: 503, nothing billed; the clients
    say the sentence with the device's speech."""
    clips["fail"] = True
    assert _refusal(_client(app, thread[0]),
                    THREAD_TOO_LONG_CODE).status_code == 503
    assert clips["costs"] == []


def test_the_announcement_clips_keep_their_files(app, thread, clips):
    """The approval announcement's key and file name are what they were
    before the refusals came, so the clips already made stay in use."""
    from backend.utils import announcement_audio as aa
    raw = "\x00".join(["approval-announcement", "gpt-4o-mini-tts", "alloy",
                       aa.ANNOUNCEMENT_TEXTS["one"]])
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    clip = aa.ensure_clip("one", thread[0].id)
    assert clip.key == key
    assert clip.path.name == f"one-{key}.mp3"
    assert clip.url == f"/api/voice/approval-announcement/one.mp3?v={key}"
    refusal = aa.ensure_refusal_clip(APPROVAL_PENDING_CODE, thread[0].id)
    assert refusal.path.name.startswith("refusal-approval_pending-")
    assert refusal.url.startswith(
        "/api/voice/refusal/approval_pending.mp3?v=")
    assert refusal.key != key


# ── Who gets a clip: an account that may use Voice ───────────────────────

def _no_voice_mode(user):
    user.plan = "free"
    _db.session.commit()


def test_an_account_without_voice_mode_gets_no_refusal_clip(app, thread,
                                                            clips):
    ben = thread[0]
    _no_voice_mode(ben)
    client = _client(app, ben)
    for code in CODES:
        resp = _refusal(client, code)
        assert resp.status_code == 403, resp.get_data(as_text=True)
        assert resp.get_json() == {
            "error": "Voice mode not enabled for this account"}
    assert clips["calls"] == [] and clips["costs"] == []


def test_an_account_without_voice_mode_gets_no_announcement(app, thread,
                                                            clips):
    """The approval announcement's routes check the same."""
    ben, anna, nodes, reply, pending_id = thread
    _voice_reply(reply)
    _no_voice_mode(ben)
    client = _client(app, ben)
    for resp in (
            client.get(f"/api/voice/approval-announcement?node={reply.id}"),
            client.get("/api/voice/approval-announcement/one.mp3"),
            client.get("/api/voice/approval-announcement/several.mp3")):
        assert resp.status_code == 403, resp.get_data(as_text=True)
    assert clips["calls"] == [] and clips["costs"] == []


def test_an_admin_and_an_alpha_account_get_it(app, thread, clips):
    ben, anna = thread[0], thread[1]
    _no_voice_mode(anna)
    anna.is_admin = True
    _db.session.commit()
    assert _refusal(_client(app, ben), SPEND_CAP_CODE).status_code == 200
    assert _refusal(_client(app, anna), SPEND_CAP_CODE).status_code == 200


def test_a_capped_account_gets_the_usage_limit_sentence(app, thread, clips):
    """The sentence is for the account that met the limit: no spend check
    on this route (the clip is made once per voice for everyone)."""
    from backend.utils.spend import current_month
    ben = thread[0]
    ben.spend_blocked_month = current_month()
    _db.session.commit()
    assert _refusal(_client(app, ben), SPEND_CAP_CODE).status_code == 200


# ── A recording kept after the access ended (#438) ───────────────────────

def test_a_lapsed_account_gets_the_sentences_of_a_kept_recording(
        app, thread, clips):
    """The account that loses its access while it records has neither
    Voice mode nor the paid set by then; it still gets its sentence (fixed
    text, made once per voice for every account). Its other refusals
    keep the Voice mode check."""
    ben = thread[0]
    app.config["PAYMENTS_ENABLED"] = True
    ben.plan = "free"
    ben.subscription_level = "standard"
    ben.subscription_status = "ended"
    ben.subscription_ever_paid = True
    _db.session.commit()
    client = _client(app, ben)
    for code in SUBSCRIPTION_CODES.values():
        resp = _refusal(client, code)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.data == b"ID3" + SPOKEN_REFUSALS[code].encode()
    assert clips["calls"] == list(SUBSCRIPTION_SENTENCES.values())
    # Made once: a second account plays the stored clip.
    anna = thread[1]
    assert _refusal(_client(app, anna),
                    SUBSCRIPTION_CODES["ended"]).status_code == 200
    assert len(clips["calls"]) == len(SUBSCRIPTION_CODES)
    client = _client(app, ben)
    for code in CODES:
        assert _refusal(client, code).status_code == 403
