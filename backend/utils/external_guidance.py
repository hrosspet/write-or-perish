"""Archive-search / saved-references guidance (#208, quote-as-response;
#329 split).

The default agentic prompt carries a bare {external_content_guidance}
placeholder. At LLM-call time it renders as up to two parts:

* the archive-search paragraph — on for EVERY user (the model can search
  the user's own past entries by meaning; #329), under the
  SEMANTIC_SEARCH_AGENTIC env killswitch (defaulting on);
* the saved-references paragraph — appended only when the node owner has
  the "External references" toggle on (Account page,
  User.external_content_enabled, default off), so the model never reasons
  about a corpus the search will not return.

Both prompt-render paths (render_system_message and the generation loop)
substitute it through render_external_guidance, and the node display
mirrors the same call so the system-prompt view shows exactly what the
model received. Lives in utils (not backend.tasks.llm_completion) so
display routes can import it without pulling in the Celery task module.
"""

EXTERNAL_GUIDANCE_PLACEHOLDER = "{external_content_guidance}"

ARCHIVE_GUIDANCE_TEXT = """## Archive search

You can search the user's own archive — their past entries and your past \
replies — by meaning, with the semantic_search tool. Use it when they \
explicitly ask ("have I written about this before?", "what did we say \
about this in that earlier thread?") — and sometimes proactively, when \
something they previously wrote would genuinely serve the present moment: \
they're circling a thought they have already articulated, or an earlier \
entry directly speaks to what they're working through. Proactive timing \
matters more than relevance: never interrupt emotional or focused sharing \
— while the user is mid-stream, your job is presence, not retrieval. Reach \
for the archive only once they've wrapped up a thread of sharing, and only \
when it seems beneficial to unblock them by shifting their attention, or \
to ground what they've been saying in something they wrote before. The \
preferred form is quote-as-response: quote the entry (by its search label) \
and say in your own words why it's relevant right now — the quote plus \
your reasoning is the response. Prefer quoting over paraphrasing whenever \
an entry already says what you would say: their own earlier words carry \
more weight than your restatement. Your commentary is what defeats \
recency bias: old content is subjectively discounted just for being old, \
but when your reasoning brings it into the present — how it speaks to \
exactly this moment — the combined force of the right quote at the right \
time, connected to now, is greater than the quote ever carried alone. The \
commentary is not decoration; it is the re-timing. Restraint is part of \
the craft: most turns need no quote, one is usually the maximum, and \
don't search for things already in your context."""

REFERENCES_GUIDANCE_TEXT = """The same search also covers the user's saved \
external references (imported tweets, bookmarks and clipped web pages), \
returned alongside their own entries. Use it when they ask about something \
they saved ("find my bookmark about...") — and proactively, under the same \
timing discipline as above, when they're circling a thought someone they \
saved has articulated, a reference directly speaks to what they're working \
through, or connecting their writing to something they chose to keep would \
add real depth. Quoting a reference is powerful because a quote borrows \
its author's validity: when you could make a point yourself or show the \
user someone they chose to save making it, the saved voice carries more \
weight than yours alone — they bookmarked that tweet, they deliberately \
imported that author. Prefer quoting over paraphrasing whenever a saved \
reference already says what you would say. The surfacing history shown \
with reference previews tells you what's already been quoted recently — \
weigh it. The 'Saved References Digest' artifact (in your artifacts index, \
when present) maps what their saved corpus contains; read it first when \
you're unsure whether a search is worth it."""


def archive_search_enabled(config):
    """The env killswitch (SEMANTIC_SEARCH_AGENTIC, default on). Constant
    per environment: when off, the search tools leave the tool list and
    the guidance renders empty."""
    return bool(config.get("SEMANTIC_SEARCH_AGENTIC", True))


def external_references_enabled_for_user(config, user_id):
    """Env killswitch AND the user's own "External references" opt-in.
    Constant per user per environment, so both prompt-render paths stay
    byte-identical between the pre-warm and generation."""
    from backend.models import User
    if not archive_search_enabled(config):
        return False
    owner = User.query.get(user_id)
    return bool(owner and owner.external_content_enabled)


def render_external_guidance(config, user_id):
    """The text that replaces {external_content_guidance} for this user:
    "" under the killswitch, the archive paragraph for everyone else, plus
    the references paragraph when their toggle is on. The single source
    for all three render paths (pre-warm render, generation loop, and the
    system-prompt display mirror)."""
    if not archive_search_enabled(config):
        return ""
    if external_references_enabled_for_user(config, user_id):
        return ARCHIVE_GUIDANCE_TEXT + "\n\n" + REFERENCES_GUIDANCE_TEXT
    return ARCHIVE_GUIDANCE_TEXT
