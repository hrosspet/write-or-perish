"""Archive-search / saved-references guidance (#208, quote-as-response).

The default agentic prompt carries a bare {external_content_guidance}
placeholder. At LLM-call time it renders as the section below when the
node owner has the external-content toggle on (the Account easter-egg
opt-in, User.external_content_enabled), or "" otherwise — so the model
never reasons about searching a corpus it has no tool for. Both
prompt-render paths (render_system_message and the generation loop)
substitute it identically, and the node display mirrors the same
substitution so the system-prompt view shows exactly what the model
received. SEMANTIC_SEARCH_AGENTIC (env) is the emergency killswitch,
defaulting on.

Lives in utils (not backend.tasks.llm_completion) so display routes can
import it without pulling in the Celery task module.
"""

EXTERNAL_GUIDANCE_PLACEHOLDER = "{external_content_guidance}"

EXTERNAL_GUIDANCE_TEXT = """## Archive search and saved references

You can search the user's own archive and their saved external references \
(imported tweets and bookmarks) by meaning, with the semantic_search tool. \
Use it when they explicitly ask ("find my bookmark about...", "have I \
written about this before?") — and sometimes proactively, when something \
they saved or previously wrote would genuinely serve the present moment: \
they're circling a thought someone they saved has articulated, a reference \
directly speaks to what they're working through, or connecting their \
writing to something they chose to keep would add real depth. Proactive \
timing matters more than relevance: never interrupt emotional or focused \
sharing — while the user is mid-stream, your job is presence, not \
references. Reach for external content only once they've wrapped up a \
thread of sharing, and only when it seems beneficial to unblock them by \
shifting their attention, or to ground what they've been saying in \
something they chose to save. The preferred form is quote-as-response: \
quote the entry or reference (by its search label) and say in your own \
words why it's relevant right now — the quote plus your reasoning is the \
response. This form is powerful for two reasons. First, a quote borrows \
its author's validity: when you could make a point yourself or show the \
user someone they chose to save making it, the saved voice carries more \
weight than yours alone — they bookmarked that tweet, they deliberately \
imported that author. Prefer quoting over paraphrasing whenever a saved reference \
already says what you would say. Second, your commentary is what defeats \
recency bias: old content is subjectively discounted just for being old, \
but when your reasoning brings it into the present — how it speaks to \
exactly this moment — the quote regains its full force. The commentary is \
not decoration; it is the re-timing. Restraint is part of the craft: most \
turns need no reference, \
one is usually the maximum, and the surfacing history shown with results \
tells you what's already been quoted recently — weigh it. The 'Saved \
References Digest' artifact (in your artifacts index, when present) maps \
what their saved corpus contains; read it first when you're unsure \
whether a search is worth it."""


def external_content_enabled_for_user(config, user_id):
    """Env killswitch AND the user's own opt-in (#208 easter-egg ship).
    Constant per user per environment, so both prompt-render paths stay
    byte-identical between the pre-warm and generation."""
    from backend.models import User
    if not config.get("SEMANTIC_SEARCH_AGENTIC", True):
        return False
    owner = User.query.get(user_id)
    return bool(owner and owner.external_content_enabled)
