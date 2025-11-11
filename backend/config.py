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
    
    # LLM API keys
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

    # Default model (for backward compatibility and fallback)
    DEFAULT_LLM_MODEL = os.environ.get("LLM_NAME", "gpt-5")

    # Supported models configuration
    SUPPORTED_MODELS = {
        "gpt-5": {
            "provider": "openai",
            "api_model": "gpt-5",
            "display_name": "GPT-5"
        },
        "claude-sonnet-4.5": {
            "provider": "anthropic",
            "api_model": "claude-sonnet-4-5-20250929",
            "display_name": "Claude 4.5 Sonnet"
        },
        "claude-opus-4.1": {
            "provider": "anthropic",
            "api_model": "claude-opus-4-1-20250514",
            "display_name": "Claude 4.1 Opus"
        },
        "claude-opus-3": {
            "provider": "anthropic",
            "api_model": "claude-3-opus-20240229",
            "display_name": "Claude 3 Opus"
        }
    }

    FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")

    # Production Security Settings
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = True
