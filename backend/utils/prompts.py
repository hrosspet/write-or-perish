import os

PROMPT_DEFAULTS = {
    'converse': {'title': 'Converse', 'file': 'converse.txt'},
    'reflect': {'title': 'Reflect', 'file': 'reflect.txt'},
    'orient': {'title': 'Orient', 'file': 'orient.txt'},
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


def get_user_prompt(user_id, prompt_key):
    """Get user's active prompt for a feature, or load the file default."""
    from backend.models import UserPrompt
    prompt = UserPrompt.query.filter_by(
        user_id=user_id, prompt_key=prompt_key
    ).order_by(UserPrompt.created_at.desc()).first()
    if prompt:
        return prompt.get_content()
    return load_default_prompt(prompt_key)
