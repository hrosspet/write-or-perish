import os
from datetime import timedelta

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
    OPENAI_API_KEY_BATCH = os.environ.get("OPENAI_API_KEY_BATCH")
    ANTHROPIC_API_KEY_CHAT = os.environ.get("ANTHROPIC_API_KEY_CHAT")
    ANTHROPIC_API_KEY_TRAIN = os.environ.get("ANTHROPIC_API_KEY_TRAIN")

    # Default model (for backward compatibility and fallback)
    DEFAULT_LLM_MODEL = os.environ.get("LLM_NAME", "claude-opus-4.6")

    # --- API spend monitoring (issue #85) ---
    # Monthly Anthropic spend cap in USD. 0 (default) disables the check.
    ANTHROPIC_SPEND_LIMIT_USD = float(
        os.environ.get("ANTHROPIC_SPEND_LIMIT_USD", "0") or "0"
    )
    # Comma-separated fractions of the limit at which to alert.
    SPEND_ALERT_THRESHOLDS = os.environ.get(
        "SPEND_ALERT_THRESHOLDS", "0.5,0.8,0.95"
    )
    # Recipient for spend alerts (admin inbox also used for signup notices).
    SPEND_ALERT_EMAIL = os.environ.get("SPEND_ALERT_EMAIL", "signup@loore.org")

    # --- Profile-generation batch processing (issue #173, Part A) ---
    # A user takes the Batch API path if the global switch is on OR their id
    # is in the canary allowlist. Both default off → batch ships dark.
    PROFILE_USE_BATCH = os.environ.get(
        "PROFILE_USE_BATCH", "false").lower() in ("1", "true", "yes")
    PROFILE_BATCH_USER_IDS = {
        int(x) for x in
        os.environ.get("PROFILE_BATCH_USER_IDS", "").replace(" ", "").split(",")
        if x
    }
    # Kill-switch: pause ALL background profile/recent-context generation — the
    # sync check (check_pending_profile_updates), the batch seeder
    # (seed_profile_batches), and the recent-context check — while leaving the
    # batch poller (poll_profile_batches) running. So an in-flight batch can
    # finish on its own, but no NEW generation starts for anyone. Lets you
    # hand-seed one user and canary them in isolation (issue #173).
    PROFILE_UPDATES_PAUSED = os.environ.get(
        "PROFILE_UPDATES_PAUSED", "false").lower() in ("1", "true", "yes")

    # Safety factor for prompt-too-long retries (0.99 = aim for 99% of the limit)
    RETRY_SAFETY_FACTOR = 0.99

    # Pricing version — bump and update date whenever prices change.
    # Sources:
    #   Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
    #   OpenAI:    https://developers.openai.com/api/docs/pricing
    PRICING_VERSION = "2"
    PRICING_UPDATED_AT = "2026-06-09"

    # Supported models configuration (single source of truth for all model metadata)
    SUPPORTED_MODELS = {
        "gpt-5.5": {
            "provider": "openai",
            "api_model": "gpt-5.5",
            "display_name": "GPT-5.5",
            "context_window": 1050000,
            "input_price_per_mtok": 5.00,
            "output_price_per_mtok": 30.00,
            "long_context_threshold": 272000,
            "long_context_input_multiplier": 2.0,
            "long_context_output_multiplier": 1.5,
        },
        "gpt-5.4": {
            "provider": "openai",
            "api_model": "gpt-5.4",
            "display_name": "GPT-5.4",
            "context_window": 1050000,
            "input_price_per_mtok": 2.50,
            "output_price_per_mtok": 15.00,
            "long_context_threshold": 272000,
            "long_context_input_multiplier": 2.0,
            "long_context_output_multiplier": 1.5,
        },
        "gpt-5": {
            "provider": "openai",
            "api_model": "gpt-5",
            "display_name": "GPT-5",
            "context_window": 128000,
            "input_price_per_mtok": 1.25,
            "output_price_per_mtok": 10.00,
            "deprecated": True,
        },
        "gpt-5.1": {
            "provider": "openai",
            "api_model": "gpt-5.1",
            "display_name": "GPT-5.1",
            "context_window": 272000,
            "input_price_per_mtok": 1.25,
            "output_price_per_mtok": 10.00,
            "deprecated": True,
        },
        "gpt-5.2": {
            "provider": "openai",
            "api_model": "gpt-5.2",
            "display_name": "GPT-5.2",
            "context_window": 272000,
            "input_price_per_mtok": 1.75,
            "output_price_per_mtok": 14.00,
            "deprecated": True,
        },
        "claude-sonnet-4.5": {
            "provider": "anthropic",
            "api_model": "claude-sonnet-4-5-20250929",
            "display_name": "Claude 4.5 Sonnet",
            "context_window": 200000,
            "input_price_per_mtok": 3.00,
            "output_price_per_mtok": 15.00,
            "deprecated": True,
        },
        "claude-sonnet-4.6": {
            "provider": "anthropic",
            "api_model": "claude-sonnet-4-6",
            "display_name": "Claude 4.6 Sonnet",
            "context_window": 1000000,
            "input_price_per_mtok": 3.00,
            "output_price_per_mtok": 15.00,
        },
        "claude-opus-4.5": {
            "provider": "anthropic",
            "api_model": "claude-opus-4-5-20251101",
            "display_name": "Claude 4.5 Opus",
            "context_window": 200000,
            "input_price_per_mtok": 5.00,
            "output_price_per_mtok": 25.00,
            "deprecated": True,
        },
        "claude-opus-4.6": {
            "provider": "anthropic",
            "api_model": "claude-opus-4-6",
            "display_name": "Claude 4.6 Opus",
            "context_window": 1000000,
            "input_price_per_mtok": 5.00,
            "output_price_per_mtok": 25.00,
        },
        "claude-opus-4.8": {
            "provider": "anthropic",
            "api_model": "claude-opus-4-8",
            "display_name": "Claude 4.8 Opus",
            "context_window": 1000000,
            "input_price_per_mtok": 5.00,
            "output_price_per_mtok": 25.00,
        },
        "claude-opus-4.7": {
            "provider": "anthropic",
            "api_model": "claude-opus-4-7",
            "display_name": "Claude 4.7 Opus",
            "context_window": 1000000,
            "input_price_per_mtok": 5.00,
            "output_price_per_mtok": 25.00,
        },
        "claude-fable-5": {
            "provider": "anthropic",
            "api_model": "claude-fable-5",
            "display_name": "Claude Fable 5",
            "context_window": 1000000,
            "input_price_per_mtok": 10.00,
            "output_price_per_mtok": 50.00,
        },
        "claude-opus-3": {
            "provider": "anthropic",
            "api_model": "claude-3-opus-20240229",
            "display_name": "Claude 3 Opus",
            "context_window": 200000,
            "max_output_tokens": 4096,
            "input_price_per_mtok": 15.00,
            "output_price_per_mtok": 75.00,
        },
    }

    # Audio model pricing (per-minute approximations; APIs don't return token counts)
    # Version and date shared with PRICING_VERSION / PRICING_UPDATED_AT above.
    AUDIO_PRICING = {
        "gpt-4o-mini-tts": {"price_per_minute_usd": 0.015},
        "gpt-4o-transcribe": {"price_per_minute_usd": 0.006},
    }

    # Magic link email authentication (SMTP)
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "localhost")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "true").lower() in ("true", "1", "yes")
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "login@loore.org")
    MAGIC_LINK_EXPIRY_SECONDS = int(os.environ.get("MAGIC_LINK_EXPIRY_SECONDS", "900"))

    FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")

    # Celery configuration for async task queue
    CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
    CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')

    # GitHub API for issue creation from Voice mode
    GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
    GITHUB_REPO = os.environ.get("GITHUB_REPO")

    # GCP Cloud KMS configuration for content encryption at rest
    # Format: projects/{project}/locations/{location}/keyRings/{keyring}/cryptoKeys/{key}
    GCP_KMS_KEY_NAME = os.environ.get("GCP_KMS_KEY_NAME")
    # Set to "true" to disable encryption (for local development without GCP)
    ENCRYPTION_DISABLED = os.environ.get("ENCRYPTION_DISABLED", "false").lower() in ("true", "1", "yes")

    # Production Security Settings
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"  # Critical for session cookies to work
    REMEMBER_COOKIE_SECURE = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
