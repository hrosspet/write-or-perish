import hashlib
import os

PROMPT_DEFAULTS = {
    'reflect': {'title': 'Reflect', 'file': 'reflect.txt', 'hidden': True},
    'orient': {'title': 'Orient', 'file': 'orient.txt', 'hidden': True},
    'profile_generation': {
        'title': 'Profile Generation', 'file': 'profile_generation.txt',
    },
    'profile_update': {
        'title': 'Profile Update', 'file': 'profile_update.txt',
    },
    'profile_integration': {
        'title': 'Profile Integration', 'file': 'profile_integration.txt',
    },
    'narrative_detection': {
        'title': 'Narrative Detection', 'file': 'narrative_detection.txt',
    },
    'letter_from_the_future': {
        'title': 'Letter from the Future',
        'file': 'letter_from_the_future.txt',
    },
    'orient_apply_todo': {
        'title': 'Apply to Todo',
        'file': 'orient_apply_todo.txt',
    },
    'voice': {
        'title': 'Voice Mode',
        'file': 'agentic.txt',
    },
    'textmode': {
        'title': 'Text Mode',
        'file': 'agentic.txt',
    },
}

PROMPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts"
)


def load_default_prompt(prompt_key):
    """Load prompt content from the file system."""
    meta = PROMPT_DEFAULTS.get(prompt_key)
    if not meta:
        return None
    path = os.path.join(PROMPTS_DIR, meta['file'])
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def default_prompt_hash(prompt_key):
    """Return SHA-256 hex digest of the file default for *prompt_key*."""
    content = load_default_prompt(prompt_key)
    if not content:
        return None
    return hashlib.sha256(content.encode()).hexdigest()


def get_user_prompt_record(user_id, prompt_key):
    """Get the UserPrompt ORM row for a feature, creating from default if needed.

    Returns the exact version row (for FK assignment). If the latest DB row
    was created via "default" and the file default has since changed, creates
    a new row with the updated content.
    """
    from backend.models import UserPrompt
    from backend.extensions import db

    prompt = UserPrompt.query.filter_by(
        user_id=user_id, prompt_key=prompt_key
    ).order_by(UserPrompt.created_at.desc()).first()

    if prompt:
        if prompt.generated_by == "default":
            current_hash = default_prompt_hash(prompt_key)
            if current_hash and prompt.based_on_default_hash != current_hash:
                # File default changed — create a new row with updated content
                meta = PROMPT_DEFAULTS.get(prompt_key, {})
                new_content = load_default_prompt(prompt_key)
                new_prompt = UserPrompt(
                    user_id=user_id,
                    prompt_key=prompt_key,
                    title=meta.get('title', prompt_key),
                    generated_by="default",
                    based_on_default_hash=current_hash,
                )
                new_prompt.set_content(new_content)
                db.session.add(new_prompt)
                db.session.flush()
                return new_prompt
        return prompt

    # No DB row — create one from the file default
    content = load_default_prompt(prompt_key)
    if not content:
        return None
    meta = PROMPT_DEFAULTS.get(prompt_key, {})
    new_prompt = UserPrompt(
        user_id=user_id,
        prompt_key=prompt_key,
        title=meta.get('title', prompt_key),
        generated_by="default",
        based_on_default_hash=default_prompt_hash(prompt_key),
    )
    new_prompt.set_content(content)
    db.session.add(new_prompt)
    db.session.flush()
    return new_prompt


def get_user_prompt(user_id, prompt_key):
    """Get user's active prompt for a feature, or load the file default.

    If the latest DB row was created via "default" (i.e. the user accepted the
    default without customising) and the file default has since changed, return
    the updated file default instead of the stale DB content.
    """
    from backend.models import UserPrompt
    prompt = UserPrompt.query.filter_by(
        user_id=user_id, prompt_key=prompt_key
    ).order_by(UserPrompt.created_at.desc()).first()
    if prompt:
        if prompt.generated_by == "default":
            current_hash = default_prompt_hash(prompt_key)
            if current_hash and prompt.based_on_default_hash != current_hash:
                return load_default_prompt(prompt_key)
        return prompt.get_content()
    return load_default_prompt(prompt_key)
