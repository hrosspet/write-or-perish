"""Share-proposal guidance for Upload v1 (#226, SHARE_V1).

The default agentic prompt carries a bare {share_guidance} placeholder.
At LLM-call time it renders as the proposing-shares section below when the
node owner has sharing enabled, or "" otherwise — so the model never
proposes shares where the Save flow doesn't exist. Both prompt-render
paths (render_system_message and the generation loop) substitute it
identically, and the node display mirrors the same substitution so the
system-prompt view shows exactly what the model received.

Lives in utils (not backend.tasks.llm_completion) so display routes can
import it without pulling in the Celery task module.
"""

SHARE_GUIDANCE_PLACEHOLDER = "{share_guidance}"

SHARE_GUIDANCE_TEXT = """## Proposing shares

When the user expresses something worth giving outward — a need they want help with, an offering, an insight or learning, an open exploration, or an intention they want to be public about — and either asks you to make it shareable or agrees when you offer, propose it using these headings: ### Share (the shareable text, written to stand alone for readers who lack the conversation's context — faithful to the user's meaning and register, not corporate-polished), ### Share type (just the single type word: need, offering, insight, exploration, intention, or other — nothing else on that line or after it). The system auto-detects the headings and shows the user a "Save to shares" button. Confirming only saves a PRIVATE draft to their Share page — publication is a separate deliberate action there, so nothing becomes visible to anyone without the user explicitly publishing it. When the user confirms out loud (e.g. "yes save that"), call the apply_share tool. Put any lead-in or closing remark *before* the headings — text after them is treated as part of the proposal, not your message. Offer shares proactively whenever something in the user's writing strikes you as genuinely novel, or likely to be helpful to others. If unsure, briefly note that what they said might be worth sharing — with your reasons why — and ask whether they'd like Loore to draft a shareable piece from it."""


def share_enabled_for_user(config, user_id):
    """Env flag AND the user's own opt-in (#228 dark ship). Constant per
    user per environment, so both prompt-render paths stay byte-identical
    between the pre-warm and generation."""
    from backend.models import User
    if not config.get("SHARE_V1", False):
        return False
    owner = User.query.get(user_id)
    return bool(owner and owner.public_sharing_enabled)
