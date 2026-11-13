"""The refusals of a Voice turn, said aloud (#651).

Someone talking in Voice mode hands-free, or with the phone locked, does
not see the message on the screen. So every refusal of a Voice turn is
also said in one fixed sentence: in a cached clip in the replies' voice
(utils/announcement_audio, GET /api/voice/refusal/<code>.mp3), the way
the approval announcement is (Peter, 2026-11-12: "the announcement should
be TTSed as a separate chunk and appended to the queue. So that when it's
not pending, it's not voiced out anymore"). A sentence is never part of a
reply's audio: the clients play it when the refusal comes, as its own
sound, and stop it when a new recording starts. Where the clip can't be
had (offline, no TTS key, a browser that plays nothing outside a tap) the
clients say the same sentence with the device's own speech.

The refusals, by the code the clients get (the HTTP answer's ``code`` or
``error``, or the ``warning_code`` of a recording saved without a reply):

- the monthly usage limit (402 ``monthly_spend_limit_reached``),
- AI usage None for the account or the thread (403 ``ai_usage_none``),
- a change the AI proposed waiting for the user's approval (409
  ``approval_pending``),
- a thread too long for another reply (409 ``thread_too_long``, #648),
- payments on and the account's access ended while the turn was recorded
  (#438; Peter, 2026-11-13): the recording is kept and transcribed, only
  the reply is skipped. One code per reason (``subscription_required_``
  + subscriptions.PAUSED_REASONS), whose sentence is the text the entry
  and the screen show (subscriptions.paused_message, "recording"): it
  names the state, as that text does, never where or how to pay. A Voice
  turn refused at its start for the subscription (402
  ``subscription_required``, nothing recorded) has no code and says
  nothing, as before.

Plain words: no numbers, no prices, nothing about buying. The web and the
iPhone app keep the same sentences for their fallback.
"""
from backend.utils.subscriptions import PAUSED_REASONS, paused_message

SPEND_CAP_CODE = "monthly_spend_limit_reached"   # utils/spend
AI_USAGE_NONE_CODE = "ai_usage_none"              # utils/llm_nodes
APPROVAL_PENDING_CODE = "approval_pending"        # utils/tool_approvals
THREAD_TOO_LONG_CODE = "thread_too_long"          # utils/thread_size

SPOKEN_REFUSALS = {
    SPEND_CAP_CODE: ("You've reached your monthly usage limit, so Loore "
                     "can't reply until it resets."),
    AI_USAGE_NONE_CODE: "AI usage is set to None, so Loore can't reply here.",
    APPROVAL_PENDING_CODE: ("A change to your data is waiting for your "
                            "approval. Approve or reject it on the screen "
                            "to continue."),
    # #648's sentence, unchanged.
    THREAD_TOO_LONG_CODE: ("This thread is too long for another reply. "
                           "Start a new thread to keep talking."),
}

# A recording kept without its reply because the account's access ended
# meanwhile (#438), by subscriptions.PAUSED_REASONS. The sentence is the
# entry's own text, so it is said in the words the screen shows.
SUBSCRIPTION_CODE_PREFIX = "subscription_required_"
SUBSCRIPTION_CODES = {reason: SUBSCRIPTION_CODE_PREFIX + reason
                      for reason in PAUSED_REASONS}
SUBSCRIPTION_SENTENCES = {code: paused_message(reason, "recording")
                          for reason, code in SUBSCRIPTION_CODES.items()}
SPOKEN_REFUSALS.update(SUBSCRIPTION_SENTENCES)


def recording_kept_code(reason):
    """The code of a recording kept without its reply for *reason*
    (subscriptions.PAUSED_REASONS; SubscriptionRequired.reason)."""
    return SUBSCRIPTION_CODES.get(reason, SUBSCRIPTION_CODES["none"])


# The warnings the finalize task leaves on a Voice recording whose reply it
# skipped (tasks/streaming_transcription VOICE_REPLY_SKIPPED_*; copied here
# so the routes need not import the task module, and
# test_voice_refusal_speech checks that the copies match), with the code
# the clients get beside each. The thread's length has its own texts
# (thread_size.skipped_reply_code).
_SKIPPED_REPLY_CODES = {
    ("Your recording is saved. Loore didn't reply because you've reached "
     "your monthly usage limit, which resets at the start of next month."):
        SPEND_CAP_CODE,
    ("Your recording is saved. Loore didn't reply because AI usage is set "
     "to None."): AI_USAGE_NONE_CODE,
    ("Your recording is saved. Loore didn't reply because a change to your "
     "data waits for your approval. Approve or reject it, then continue."):
        APPROVAL_PENDING_CODE,
}
# The access-ended texts are the sentences themselves.
_SKIPPED_REPLY_CODES.update(
    {text: code for code, text in SUBSCRIPTION_SENTENCES.items()})


def skipped_reply_code(warning):
    """The code a Voice recording's warning carries for the clients
    (``warning_code`` beside ``warning`` on the SSE ``all_complete`` and
    the streaming status): the refusal they say aloud, or None for a
    warning that is not one (nothing transcribed, a placeholder error)."""
    from backend.utils.thread_size import (
        skipped_reply_code as thread_length_code,
    )
    return thread_length_code(warning) or _SKIPPED_REPLY_CODES.get(warning)
