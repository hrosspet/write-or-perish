import os

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "this-should-be-changed")
    # Make sure your DATABASE_URL looks something like
    # "postgresql://username:password@localhost/writeorperish"
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "postgresql://localhost/writeorperish")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Twitter OAuth configuration
    TWITTER_API_KEY = os.environ.get("TWITTER_API_KEY")
    TWITTER_API_SECRET = os.environ.get("TWITTER_API_SECRET")
    
    # LLM API keys - separated by usage type for privacy
    # CHAT keys: used when content has ai_usage='chat' (responses only, no training)
    # TRAIN keys: used when content has ai_usage='train' (can be used for training)
    OPENAI_API_KEY_CHAT = os.environ.get("OPENAI_API_KEY_CHAT")
    OPENAI_API_KEY_TRAIN = os.environ.get("OPENAI_API_KEY_TRAIN")
    ANTHROPIC_API_KEY_CHAT = os.environ.get("ANTHROPIC_API_KEY_CHAT")
    ANTHROPIC_API_KEY_TRAIN = os.environ.get("ANTHROPIC_API_KEY_TRAIN")

    # Default model (for backward compatibility and fallback)
    DEFAULT_LLM_MODEL = os.environ.get("LLM_NAME", "claude-opus-4.5")

    # Model context window limits (input tokens)
    MODEL_CONTEXT_WINDOWS = {
        "gpt-5": 128000,
        "gpt-5.1": 272000,
        "gpt-5.2": 272000,  # Temporarily 272k until OpenAI fixes their API limit (should be 400k)
        "claude-sonnet-4.5": 200000,  # Temporarily 200k due to API tier limit (should be 1M with higher tier)
        "claude-opus-4.5": 200000,
        "claude-opus-3": 200000,
    }

    # Buffer for token estimation error (percentage of context window)
    PROFILE_CONTEXT_BUFFER_PERCENT = 0.14  # 14%

    # Supported models configuration
    SUPPORTED_MODELS = {
        "gpt-5": {
            "provider": "openai",
            "api_model": "gpt-5",
            "display_name": "GPT-5"
        },
        "gpt-5.1": {
            "provider": "openai",
            "api_model": "gpt-5.1",
            "display_name": "GPT-5.1"
        },
        "gpt-5.2": {
            "provider": "openai",
            "api_model": "gpt-5.2",
            "display_name": "GPT-5.2"
        },
        "claude-sonnet-4.5": {
            "provider": "anthropic",
            "api_model": "claude-sonnet-4-5-20250929",
            "display_name": "Claude 4.5 Sonnet"
        },
        "claude-opus-4.5": {
            "provider": "anthropic",
            "api_model": "claude-opus-4-5-20251101",
            "display_name": "Claude 4.5 Opus"
        },
        "claude-opus-3": {
            "provider": "anthropic",
            "api_model": "claude-3-opus-20240229",
            "display_name": "Claude 3 Opus"
        }
    }

    FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")

    # Celery configuration for async task queue
    CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
    CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')

    # Production Security Settings
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"  # Critical for session cookies to work
    REMEMBER_COOKIE_SECURE = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
