import os
from datetime import timedelta


def float_from_env(name, default):
    """A float setting that never stops the app at import: a missing,
    empty or non-numeric value falls back to *default* (with a warning)."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        import logging
        logging.getLogger(__name__).warning(
            "%s=%r is not a number; using %s", name, raw, default)
        return default


def stripe_key_from_env(name, sandbox_name, environ=None):
    """A Stripe key (#438): *name*, or in development and testing only
    (FLASK_ENV) the local sandbox key *sandbox_name*. Staging and
    production never fall back to it."""
    environ = os.environ if environ is None else environ
    deploy_env = (environ.get("FLASK_ENV") or "production").lower()
    value = environ.get(name)
    if not value and deploy_env in ("development", "testing"):
        value = environ.get(sandbox_name)
    return value or None


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "this-should-be-changed")
    # Make sure your DATABASE_URL looks something like
    # "postgresql://username:password@localhost/writeorperish"
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "postgresql://localhost/writeorperish")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # A database error's text leaves out the statement's parameters (what
    # was being saved), and so does SQLAlchemy's statement logging.
    # Postgres's own DETAIL line can still quote the row: see
    # backend/utils/error_privacy.py for how that is kept out of
    # responses, logs and Sentry.
    SQLALCHEMY_ENGINE_OPTIONS = {"hide_parameters": True}

    # Twitter OAuth configuration
    TWITTER_API_KEY = os.environ.get("TWITTER_API_KEY")
    TWITTER_API_SECRET = os.environ.get("TWITTER_API_SECRET")
    
    # LLM API keys - separated by usage type for privacy
    # CHAT keys: used when content has ai_usage='chat' (responses only, no training)
    # TRAIN keys: used when content has ai_usage='train' (can be used for training)
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
    OPENAI_API_KEY_CHAT = os.environ.get("OPENAI_API_KEY_CHAT")
    OPENAI_API_KEY_TRAIN = os.environ.get("OPENAI_API_KEY_TRAIN")
    OPENAI_API_KEY_BATCH = os.environ.get("OPENAI_API_KEY_BATCH")
    ANTHROPIC_API_KEY_CHAT = os.environ.get("ANTHROPIC_API_KEY_CHAT")
    ANTHROPIC_API_KEY_TRAIN = os.environ.get("ANTHROPIC_API_KEY_TRAIN")
    # Legacy single key — the documented fallback in
    # utils/api_keys.get_api_keys_for_usage was dead without it in Config
    # (local dev sets only the base key and hit "not configured").
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

    # Default model (for backward compatibility and fallback). Must be an
    # active (non-deprecated) SUPPORTED_MODELS key: the app refuses to boot
    # otherwise (validate_default_model). Prod sets LLM_NAME explicitly.
    DEFAULT_LLM_MODEL = os.environ.get("LLM_NAME") or "claude-opus-4.6"

    # Each provider's default chat model (#487). A saved model that is no
    # longer offered for chat (a preference, a thread's model, the model
    # an old page sends; deprecated or since removed) is replaced with a
    # model of its own provider, never another provider's: "A user's
    # model is theirs" (LOORE-ESSENCE). The user's own model of that
    # provider comes first; else the provider's default. The provider
    # of DEFAULT_LLM_MODEL has LLM_NAME as its default, so its setting
    # here applies only while LLM_NAME is another provider's model. Each
    # must be an active chat model of its own provider (checked at boot,
    # validate_default_model).
    CHAT_MODEL_ANTHROPIC = (os.environ.get("CHAT_MODEL_ANTHROPIC")
                            or "claude-opus-5.5")
    CHAT_MODEL_OPENAI = os.environ.get("CHAT_MODEL_OPENAI") or "gpt-6-astra"

    # The model a read (routes/read.py) runs on when neither the request
    # nor an earlier read reply in the thread names one. Reads run only on
    # models flagged "read" in SUPPORTED_MODELS (#355).
    READ_DEFAULT_MODEL = "gpt-6-luna"

    # --- Glean (#435): the Community Archive read, for every user ---
    # Rollout gate, the PROFILE_USE_BATCH / PROFILE_BATCH_USER_IDS
    # pattern: admins always; everyone when GLEAN_FOR_ALL is on; else the
    # user ids in GLEAN_USER_IDS (comma-separated). Both default off, so
    # Glean ships dark. On top of the gate every user has their own
    # switch in Account settings (User.glean_enabled, utils/glean.py).
    GLEAN_FOR_ALL = os.environ.get(
        "GLEAN_FOR_ALL", "false").lower() in ("1", "true", "yes")
    GLEAN_USER_IDS = {
        int(x) for x in
        os.environ.get("GLEAN_USER_IDS", "").replace(" ", "").split(",")
        if x
    }
    # The read model a glean runs on by default, one setting per provider
    # of the user's chat model (llm_nodes.glean_provider). During Glean's
    # testing both default to GPT-5.6 Luna, also for Anthropic users: it
    # is the best model for Glean so far (Peter, 2026-10-14; Haiku 5.5 is
    # among the models still to be tested). The per-provider settings
    # stay, so each provider can get its own default again without a code
    # change. The user may pick another read model, another provider's
    # too, in the Glean picker (Peter, 2026-10-09). Each must be an
    # active "read" model, of any provider (checked at boot,
    # validate_default_model).
    GLEAN_MODEL_ANTHROPIC = (os.environ.get("GLEAN_MODEL_ANTHROPIC")
                             or "gpt-5.6-luna")
    GLEAN_MODEL_OPENAI = (os.environ.get("GLEAN_MODEL_OPENAI")
                          or "gpt-5.6-luna")

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

    # Model calls that fail for an account reason (spend limit, billing,
    # API key, unknown model; #369, #360) email SPEND_ALERT_EMAIL once per
    # cause per this many seconds (backend/utils/provider_alerts.py).
    # Default 6 h: a cause that stays broken is re-sent at most 4x a day.
    PROVIDER_ACCOUNT_ALERT_THROTTLE_SECONDS = int(
        os.environ.get("PROVIDER_ACCOUNT_ALERT_THROTTLE_SECONDS", "21600")
        or "21600"
    )

    # Disk-full alert (backend/utils/disk_alert.py): an hourly beat task
    # emails SPEND_ALERT_EMAIL when the filesystem holding the audio
    # storage root is at DISK_ALERT_PERCENT or more used (at most once per
    # 24 h; again at 95 %). Ships dark: set DISK_ALERT_ENABLED=true in prod.
    DISK_ALERT_ENABLED = os.environ.get(
        "DISK_ALERT_ENABLED", "false").lower() in ("1", "true", "yes")
    DISK_ALERT_PERCENT = float_from_env("DISK_ALERT_PERCENT", 90.0)

    # Pre-filled accounts nobody claimed (#685, backend/utils/
    # unclaimed_accounts.py). Decided by Peter (2026-11-13: "90 days makes
    # sense to remove it from prod Loore"): an account Loore opened and
    # pre-filled from a person's public posts, that nobody has signed in
    # to, is deleted with everything in it this many days after it was
    # opened. The privacy page states the same number.
    UNCLAIMED_ACCOUNT_DAYS = int(float_from_env("UNCLAIMED_ACCOUNT_DAYS", 90))
    # Off (the default), the nightly job is a dry run: it only reports what
    # it would delete (the admin's Users tab, `flask unclaimed-accounts`,
    # the worker log). The owner turns deletion on after reading the
    # first dry run.
    UNCLAIMED_ACCOUNT_DELETION_ENABLED = os.environ.get(
        "UNCLAIMED_ACCOUNT_DELETION_ENABLED", "false").lower() in (
        "1", "true", "yes")

    # Per-user monthly spend hard cap in USD, summed across ALL providers
    # (issue #85 follow-up; free-alpha guardrail). 0 (default) disables the
    # cap. When a user's month-to-date spend reaches this, they are
    # hard-blocked from new cost-incurring actions until the month rolls
    # over, and an alert goes to SPEND_ALERT_EMAIL. Ships dark; set in prod.
    PER_USER_MONTHLY_LIMIT_USD = float(
        os.environ.get("PER_USER_MONTHLY_LIMIT_USD", "0") or "0"
    )

    # --- Payments (#438): Stripe subscriptions, Managed Payments ---
    # The one switch. Off (default): no billing route answers, no
    # subscription text or control shows, and AI replies are not gated by
    # plan, exactly as before. On: new accounts start on the "free" plan
    # and get AI replies, Voice mode and profile updates from a trialing
    # or active subscription (backend/utils/subscriptions.py). Accounts
    # whose plan was set by hand (alpha, pro) are not affected by it.
    PAYMENTS_ENABLED = os.environ.get(
        "PAYMENTS_ENABLED", "false").lower() in ("1", "true", "yes")
    # Where this runs: FLASK_ENV ("development", "testing", "staging"; any
    # other value or none at all counts as production).
    DEPLOY_ENV = (os.environ.get("FLASK_ENV") or "production").lower()
    # Stripe keys. The sandbox names are a fallback in development and
    # testing only; staging and production set the STRIPE_* names. In
    # production a test-mode key keeps payments off (create_app). The
    # publishable key is read for completeness: Stripe-hosted Checkout
    # needs no Stripe.js.
    STRIPE_SECRET_KEY = stripe_key_from_env(
        "STRIPE_SECRET_KEY", "STRIPE_SANDBOX_PRIVKEY")
    STRIPE_PUBLISHABLE_KEY = stripe_key_from_env(
        "STRIPE_PUBLISHABLE_KEY", "STRIPE_SANDBOX_PUBKEY")
    # Signing secret of the webhook endpoint (/api/webhooks/stripe). Unset:
    # the endpoint answers 404 while the switch is off, 503 while it is on.
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET") or None
    # The monthly prices, in cents of SUBSCRIPTION_CURRENCY (Peter,
    # 2026-11-12): Standard about 30 USD, "that most people will choose";
    # Supporter 99 USD, "mostly as an anchor", the same product for now;
    # a discounted 12 USD opened by a code, shown nowhere. `flask
    # stripe-sync-catalog` writes them to Stripe; a changed amount makes a
    # new price there, and subscriptions already running keep theirs.
    SUBSCRIPTION_PRICE_CENTS_STANDARD = int(
        os.environ.get("SUBSCRIPTION_PRICE_CENTS_STANDARD") or "3000")
    SUBSCRIPTION_PRICE_CENTS_SUPPORTER = int(
        os.environ.get("SUBSCRIPTION_PRICE_CENTS_SUPPORTER") or "9900")
    SUBSCRIPTION_PRICE_CENTS_DISCOUNTED = int(
        os.environ.get("SUBSCRIPTION_PRICE_CENTS_DISCOUNTED") or "1200")
    SUBSCRIPTION_CURRENCY = (
        os.environ.get("SUBSCRIPTION_CURRENCY") or "usd").lower()
    # "exclusive" (Peter, 2026-11-12: "tax should be on top of our
    # pricing"): the VAT or sales tax Stripe collects as merchant of record
    # is added at checkout. "inclusive" would put it inside the amounts.
    # Stripe cannot change a price's tax behaviour later; the sync makes a
    # new price instead.
    SUBSCRIPTION_TAX_BEHAVIOR = (
        os.environ.get("SUBSCRIPTION_TAX_BEHAVIOR") or "exclusive").lower()
    # Managed Payments sells only products with an eligible tax code:
    # AI as a Service, cloud based, personal use.
    STRIPE_PRODUCT_TAX_CODE = (
        os.environ.get("STRIPE_PRODUCT_TAX_CODE") or "txcd_10105001")
    # Free days before the first payment, with a card (Peter, 2026-11-12:
    # two weeks). Once per account: a second subscription has none.
    SUBSCRIPTION_TRIAL_DAYS = int(
        os.environ.get("SUBSCRIPTION_TRIAL_DAYS") or "14")
    # Monthly model-cost ceiling of a subscribed account, in USD (Peter,
    # 2026-11-12): the per-user spend cap for an account whose access
    # comes from its subscription. An admin's per-user limit still wins;
    # accounts set by hand keep PER_USER_MONTHLY_LIMIT_USD.
    SUBSCRIPTION_SPEND_LIMIT_USD_STANDARD = float_from_env(
        "SUBSCRIPTION_SPEND_LIMIT_USD_STANDARD", 50.0)
    SUBSCRIPTION_SPEND_LIMIT_USD_SUPPORTER = float_from_env(
        "SUBSCRIPTION_SPEND_LIMIT_USD_SUPPORTER", 100.0)
    SUBSCRIPTION_SPEND_LIMIT_USD_DISCOUNTED = float_from_env(
        "SUBSCRIPTION_SPEND_LIMIT_USD_DISCOUNTED", 50.0)
    # Model cost a trial may use in all, whatever the level: nothing has
    # been paid yet. Counted from the trial's start, not per month. From
    # the first successful payment the level's monthly ceiling applies, and
    # it counts the whole calendar month, trial usage in that month
    # included. 0: no separate trial ceiling. Peter, 2026-11-12: "let's go
    # with $30 for the free trial period, and if the user pays, extend
    # this to the subscription's cap" — a starting value, to be checked
    # with the prices before the switch is turned on.
    SUBSCRIPTION_TRIAL_SPEND_LIMIT_USD = float_from_env(
        "SUBSCRIPTION_TRIAL_SPEND_LIMIT_USD", 30.0)
    # Wrong discount codes one account may try per hour before the code
    # field answers "try again later". 10: a typo or two is never
    # refused, guessing an 8-character code is out of reach.
    DISCOUNT_CODE_ATTEMPTS_PER_HOUR = int(
        os.environ.get("DISCOUNT_CODE_ATTEMPTS_PER_HOUR") or "10")
    # Where the Subscribe view tells a user to ask for the lower price.
    SUBSCRIPTION_CONTACT_EMAIL = (
        os.environ.get("SUBSCRIPTION_CONTACT_EMAIL") or "info@loore.org")
    # The alpha free month (Peter, 2026-11-12: alpha testers are "free for
    # one month after the opening, then the discounted price"; "The number
    # of months may still change"). Days, counted from the start date given
    # to `flask alpha-free-month --apply --start` (the Beta release date).
    ALPHA_FREE_MONTH_DAYS = int(
        os.environ.get("ALPHA_FREE_MONTH_DAYS") or "30")
    # Referrals (#439): an invite link per account; on the friend's first
    # payment both get a free week as Stripe customer credit. On only
    # while PAYMENTS_ENABLED is on; this switch turns the link and new
    # referrals off by itself (a free week already earned is still given).
    REFERRALS_ENABLED = os.environ.get(
        "REFERRALS_ENABLED", "true").lower() in ("1", "true", "yes")
    # Days of the free week (Peter, 2026-10-09: "a free week for both"):
    # credit worth this many thirtieths of the next invoice.
    REFERRAL_REWARD_DAYS = int(os.environ.get("REFERRAL_REWARD_DAYS") or "7")

    # Long threads (#648; Peter, 2026-11-12): every AI reply sends the
    # whole thread above it. A reply whose prompt would be over
    # THREAD_PROMPT_TOKEN_LIMIT tokens is refused before any model call,
    # and from THREAD_PROMPT_NOTICE_FRACTION of it the reply form says the
    # thread is getting long (backend/utils/thread_size.py). 250k sits
    # below the 272k tier at which some models reprice the whole request
    # and above last month's 99th percentile (234k). 0 turns the limit
    # (and the notice) off.
    THREAD_PROMPT_TOKEN_LIMIT = int(
        float_from_env("THREAD_PROMPT_TOKEN_LIMIT", 250_000))
    THREAD_PROMPT_NOTICE_FRACTION = float_from_env(
        "THREAD_PROMPT_NOTICE_FRACTION", 0.9)

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

    # --- Independent profile versions for large builds (#545) ---
    # A build whose remainder plans into two or more chunks on the user's
    # chat model reads each window on its own (generation prompt plus the
    # events paragraph), all windows in one Batch submission, and a merge
    # model integrates the versions (backend/tasks/profile_windows.py,
    # docs/design/profile-batch-processing.md). Off: every build takes
    # the chained path, as before.
    PROFILE_INDEPENDENT_VERSIONS = (
        os.environ.get("PROFILE_INDEPENDENT_VERSIONS") or "true"
    ).lower() in ("1", "true", "yes")
    # The version and merge models, one pair per provider of the user's
    # chat model. Each must be an active model of THAT provider (checked
    # at boot, validate_default_model): a user's writing never reaches
    # another provider's model.
    PROFILE_VERSION_MODEL_ANTHROPIC = (
        os.environ.get("PROFILE_VERSION_MODEL_ANTHROPIC") or "claude-haiku-5.5")
    PROFILE_VERSION_MODEL_OPENAI = (
        os.environ.get("PROFILE_VERSION_MODEL_OPENAI") or "gpt-6-luna")
    PROFILE_MERGE_MODEL_ANTHROPIC = (
        os.environ.get("PROFILE_MERGE_MODEL_ANTHROPIC") or "claude-opus-5.5")
    PROFILE_MERGE_MODEL_OPENAI = (
        os.environ.get("PROFILE_MERGE_MODEL_OPENAI") or "gpt-6.1-sol")
    # The pair for a build whose writing may reach either provider: every
    # entry is public (a pre-fill from the Community Archive or the X API,
    # the lab), or the user agreed to both providers
    # (User.profile_both_providers). Peter, 2026-11-05: "luna for versions,
    # opus 5.5 for merges is preferred". Checked at boot as active models.
    PROFILE_VERSION_MODEL_CROSS_PROVIDER = (
        os.environ.get("PROFILE_VERSION_MODEL_CROSS_PROVIDER")
        or "gpt-6-luna")
    PROFILE_MERGE_MODEL_CROSS_PROVIDER = (
        os.environ.get("PROFILE_MERGE_MODEL_CROSS_PROVIDER")
        or "claude-opus-5.5")
    # A from-scratch build that fits ONE chunk on the user's chat model,
    # per provider of that chat model (Peter, 2026-11-03): on, it is read
    # as a window version with the events paragraph and then rewritten as
    # the profile with the single-version prompt
    # (profile_single_integration.txt), two Batch rounds; off, it is one
    # call on the chat model with the plain generation prompt, as before.
    # Both on (Peter, 2026-11-03: "yes, we should do the same for Ant").
    # The switch decides the path only; the models come from the rules of
    # 2026-11-05 (profile_windows.pass_models): the user's chat model for
    # both steps, or, when every entry is public or the user agreed to
    # both providers, the cross-provider pair above. Updates of a chained
    # profile are not affected.
    PROFILE_ONE_CHUNK_WINDOW_OPENAI = (
        os.environ.get("PROFILE_ONE_CHUNK_WINDOW_OPENAI") or "true"
    ).lower() in ("1", "true", "yes")
    PROFILE_ONE_CHUNK_WINDOW_ANTHROPIC = (
        os.environ.get("PROFILE_ONE_CHUNK_WINDOW_ANTHROPIC") or "true"
    ).lower() in ("1", "true", "yes")
    # Reasoning effort sent with an OpenAI merge request (a Responses
    # batch request's "reasoning.effort"). One of PROFILE_REASONING_EFFORTS
    # (checked at boot). "high" (2026-10-22): on identical input Sol kept
    # 27 of 41 core facts at high effort against 25 at its default.
    PROFILE_MERGE_REASONING_EFFORT_OPENAI = (
        os.environ.get("PROFILE_MERGE_REASONING_EFFORT_OPENAI")
        or "high").lower()
    # "flat" (default): one merge over every window while it fits the
    # merge model's input cap, else fixed groups of
    # PROFILE_MERGE_GROUP_SIZE, then the groups; "grouped": always the
    # groups above PROFILE_MERGE_GROUP_SIZE windows. Flat on 2026-10-22's
    # 149-version test: fewer errors, every template sub-heading kept.
    PROFILE_MERGE_STRUCTURE = (
        os.environ.get("PROFILE_MERGE_STRUCTURE") or "flat").lower()
    PROFILE_MERGE_GROUP_SIZE = int(
        os.environ.get("PROFILE_MERGE_GROUP_SIZE") or "30")
    # Submissions of one window before it is given up: a refused, cut-off
    # or invalid output is retried once unchanged, then skipped; a
    # provider failure (no result) is retried until the third, then the
    # window waits a day (refusal_backoff.UNBILLED_STOP_EXPIRY).
    PROFILE_VERSION_CONTENT_ATTEMPTS = int(
        os.environ.get("PROFILE_VERSION_CONTENT_ATTEMPTS") or "2")
    PROFILE_VERSION_PROVIDER_ATTEMPTS = int(
        os.environ.get("PROFILE_VERSION_PROVIDER_ATTEMPTS") or "3")
    # One submission's request bytes before it is split into another
    # batch (OpenAI's input file limit is 200 MB, Anthropic's 256 MB).
    # This many prompt bytes are held in the worker before each submit,
    # so it bounds the build's memory too: 15 MB (100 MB before
    # 2026-11-07, when a multi-million-unit build ran the server out of
    # memory). Same default as profile_windows.DEFAULT_BATCH_MAX_BYTES.
    PROFILE_BATCH_MAX_BYTES = int(
        os.environ.get("PROFILE_BATCH_MAX_BYTES") or "15000000")
    # Rendering time one advance task spends before it submits what it
    # has and hands the rest to a new task (under the 3300 s soft limit).
    # 600 s (2400 before 2026-11-07); profile_windows.DEFAULT_RENDER_SECONDS.
    PROFILE_WINDOW_RENDER_SECONDS = int(
        os.environ.get("PROFILE_WINDOW_RENDER_SECONDS") or "600")

    # --- Agentic semantic search (#155/#208/#329) ---
    # Manual Cmd+K semantic search is always on. Own-archive semantic_search
    # as a within-turn LLM tool (and the read_full / {quote:ID} pull it
    # drives) is on for EVERY user (#329); only the saved-references half
    # of that search is per-user (User.external_content_enabled — the
    # Account "External references" toggle, default off). This env var is
    # the emergency KILL SWITCH only: set SEMANTIC_SEARCH_AGENTIC=false and
    # restart (it is read at boot) to drop both search tools and the
    # archive-search guidance for everyone. The #192 render-cache key
    # carries it, so already-cached system renders do not keep the
    # guidance for the rest of their TTL.
    SEMANTIC_SEARCH_AGENTIC = os.environ.get(
        "SEMANTIC_SEARCH_AGENTIC", "true").lower() in ("1", "true", "yes")

    # --- The public side of Loore (Share/Commons/public pages, #228) ---
    # Deployed by default; the DARKNESS is per-user (User.
    # public_sharing_enabled, default off — the Account toggle is the
    # easter-egg opt-in until the #207 announcement). This env var is the
    # emergency KILL SWITCH only: set SHARE_V1=false to remove the entire
    # public surface (routes, funnel, prompt guidance, tools, the Account
    # toggle itself) without a deploy.
    SHARE_V1 = os.environ.get(
        "SHARE_V1", "true").lower() in ("1", "true", "yes")

    # --- Replies while they are written (#367) ---
    # STREAMING_REPLIES: an LLM reply's text shows on its node as it
    # arrives (Node.streaming_content + the llm-stream SSE). On by
    # default; the env var is the kill switch (read by the worker).
    STREAMING_REPLIES = os.environ.get(
        "STREAMING_REPLIES", "true").lower() in ("1", "true", "yes")
    # STREAMING_VOICE_TTS: voice mode speaks a reply while it is written
    # instead of after it is complete. Dark until a staging pass: off
    # unless set.
    STREAMING_VOICE_TTS = os.environ.get(
        "STREAMING_VOICE_TTS", "false").lower() in ("1", "true", "yes")

    # --- Voice reply waits for its cache pre-warm (#187) ---
    # A voice reply that had an Anthropic cache pre-warm sent for it waits
    # until the pre-warm reports that it finished, at most this many
    # seconds, before its first model call; past that it writes the cache
    # itself. Peter, 2026-10-09: 10 s, a bit over twice the longest
    # pre-warm call seen (4.4 s). 0 turns the wait off (read by the worker;
    # see utils/prompt_cache.wait_for_prewarm).
    PREWARM_WAIT_MAX_SECONDS = float(
        os.environ.get("PREWARM_WAIT_MAX_SECONDS") or "10")

    # --- Dev-update channel: changelog + notifications + polls (#207) ---
    # Intrinsically quiet (nothing shows unless something is unread), so it
    # deploys on. This env var is the emergency KILL SWITCH only: set
    # DEV_UPDATES_V1=false to make /api/updates serve nothing (the modal
    # never appears) without a deploy.
    DEV_UPDATES_V1 = os.environ.get(
        "DEV_UPDATES_V1", "true").lower() in ("1", "true", "yes")

    # Community Archive pre-fill (admin): accounts with at least this many
    # tweets are read from the nightly parquet snapshot (downloaded once per
    # export to COMMUNITY_ARCHIVE_SNAPSHOT_DIR, ~1 GB) instead of the REST
    # API. Env vars reach containers as empty strings when unset — guard.
    COMMUNITY_ARCHIVE_PARQUET_MIN_TWEETS = int(
        os.environ.get("COMMUNITY_ARCHIVE_PARQUET_MIN_TWEETS") or "5000")
    COMMUNITY_ARCHIVE_SNAPSHOT_DIR = (
        os.environ.get("COMMUNITY_ARCHIVE_SNAPSHOT_DIR") or None)
    # Personal key for the Community Archive API v1 (#688; created at
    # community-archive.org, settings, API keys). Set: the pre-fill and
    # archive lookups use API v1, and Glean reads the last day from it
    # while the bulk export is stale. Unset: the Supabase REST API, which
    # the archive retires on 2026-11-19. A secret: never logged.
    COMMUNITY_ARCHIVE_API_KEY = (
        os.environ.get("COMMUNITY_ARCHIVE_API_KEY") or "").strip() or None

    # Safety factor for prompt-too-long retries (0.99 = aim for 99% of the limit)
    RETRY_SAFETY_FACTOR = 0.99

    # Pricing version — bump and update date whenever prices change.
    # Sources:
    #   Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
    #   OpenAI:    https://developers.openai.com/api/docs/pricing
    PRICING_VERSION = "9"
    PRICING_UPDATED_AT = "2026-10-08"

    # Supported models configuration (single source of truth for all model metadata)
    #
    # Profile-chunk sizing keys (docs/design/chunk-planner.md):
    #   tokenizer_family  — which BPE the model bills with: "claude_old"
    #                       (Opus ≤ 4.6, Sonnet 4.6, Haiku 4.5), "claude_new"
    #                       (introduced with Opus 4.7: Opus 4.7/4.8/5/5.5, Sonnet
    #                       5/5.5, Haiku 5.5, Fable 5/5.1; 1.0–1.35× the old
    #                       family), "o200k"
    #                       (GPT-5.x; tiktoken o200k_base matches billing).
    #                       Chunk balance is in stored content units and does
    #                       not depend on the family; the family only selects
    #                       the prior for the real-token cap check and tags the
    #                       per-user measured ratio.
    #   max_input_tokens  — the provider's hard input ceiling when it sits
    #                       below the context window: OpenAI reserves its full
    #                       128k max output out of the window whatever is
    #                       requested, so a 1.05M window takes 922k of input.
    #   long_context_threshold — the pricing tier: a prompt (uncached input
    #                       + cache reads + cache writes) above it bills the
    #                       whole request at the long_context_input_ /
    #                       _output_multiplier (utils/cost.py). The profile
    #                       pipeline never plans a prompt across it (#259).
    #
    #   cache_diagnostics — OpenAI Prompt Cache Diagnostics (GPT-5.6 and later,
    #                       #348): conversation calls pass the previous call's
    #                       response id and record why the cache missed. Older
    #                       models must not get the option.
    #
    # Model picker keys (#355):
    #   deprecated — hidden from every picker and never inherited from a
    #                thread; old replies keep their label. A preference,
    #                a thread or a request still on it moves to a model
    #                of the same provider (#487): the user's own model of
    #                that provider, else CHAT_MODEL_<PROVIDER> /
    #                LLM_NAME. Deprecate a model rather than remove its
    #                entry: the entry is how its provider is known.
    #   featured   — in the picker's short list (with the selected model);
    #                the rest sit behind "More models…". Set by hand.
    #   read       — a read (Community Archive picks) may run on it; the
    #                Read button's picker lists only these.
    #   chat       — False keeps the model to reads: it is left out of
    #                the chat pickers (LLM Response, the Account default,
    #                admin polls) and refused as an account preference,
    #                as LLM_NAME and as a poll's model, and a reply that
    #                is not a read turn sent with it is refused with a 400
    #                (create_llm_placeholder, ReadOnlyModelRefused), never
    #                moved to another model. Absent means True.
    #                "Read only" = read: True + chat: False.
    # The picker's full list follows this dict's order: newest first
    # within each provider.
    SUPPORTED_MODELS = {
        "gpt-6.1-sol": {
            # ASSUMED o200k, as for gpt-6-sol.
            "tokenizer_family": "o200k",
            "provider": "openai",
            "api_model": "gpt-6.1-sol",
            "cache_diagnostics": True,
            "max_input_tokens": 922000,
            "display_name": "GPT-6.1 Sol",
            # Read only (Peter, 2026-10-02): "for Read (not for normal
            # chatting, though)".
            "read": True,
            "chat": False,
            "context_window": 1050000,
            # Verified 2026-10-02 on the OpenAI pricing + model pages:
            # $2.00 / $10.00, cached input $0.10 (0.05x, where gpt-6-sol
            # is 0.1x), cache writes $2.50 (the default 1.25x); >272k
            # input: 2x input and cache rates, 1.5x output for the whole
            # request; batch half.
            "input_price_per_mtok": 2.00,
            "output_price_per_mtok": 10.00,
            "cached_input_multiplier": 0.05,
            "long_context_threshold": 272000,
            "long_context_input_multiplier": 2.0,
            "long_context_output_multiplier": 1.5,
        },
        "gpt-6-astra": {
            "provider": "openai",
            "api_model": "gpt-6-astra",
            "cache_diagnostics": True,
            "max_input_tokens": 922000,
            "display_name": "GPT-6 Astra",
            "featured": True,
            "context_window": 1050000,
            # Verified 2026-09-03 on the OpenAI pricing page.
            "input_price_per_mtok": 10.00,
            "output_price_per_mtok": 50.00,
            # OpenAI auto-cache discount: GPT-6 Astra caches at 90% off
            "cached_input_multiplier": 0.10,
            "long_context_threshold": 272000,
            "long_context_input_multiplier": 2.0,
            "long_context_output_multiplier": 1.5,
            # ASSUMED same new-generation tokenizer as gpt-5.6-sol (~2
            # chars/token); not measured on GPT-6. The per-user observed
            # calibration (User.profile_token_ratio) corrects the residual
            # after the first chunk, so a wrong guess costs at most one
            # mis-sized chunk.
            "token_multiplier": 2.0,
        },
        "gpt-6-sol": {
            # ASSUMED o200k like GPT-5.x (also the provider default); not
            # measured on GPT-6. Only selects the cap-check prior until the
            # user's first chunk is measured.
            "tokenizer_family": "o200k",
            "provider": "openai",
            "api_model": "gpt-6-sol",
            "cache_diagnostics": True,
            "max_input_tokens": 922000,
            "display_name": "GPT-6 Sol",
            "read": True,
            "context_window": 1050000,
            # Verified 2026-09-23 on the OpenAI pricing + model pages:
            # $2.00 / $10.00, cached input $0.20 (0.1x), cache writes $2.50
            # (the default 1.25x); >272k input: $4.00 / $15.00, cached $0.40,
            # writes $5.00 (2x input and cache, 1.5x output); batch half.
            "input_price_per_mtok": 2.00,
            "output_price_per_mtok": 10.00,
            "cached_input_multiplier": 0.10,
            "long_context_threshold": 272000,
            "long_context_input_multiplier": 2.0,
            "long_context_output_multiplier": 1.5,
        },
        "gpt-6-luna": {
            # ASSUMED o200k, as for gpt-6-sol.
            "tokenizer_family": "o200k",
            "provider": "openai",
            "api_model": "gpt-6-luna",
            "cache_diagnostics": True,
            "max_input_tokens": 922000,
            "display_name": "GPT-6 Luna",
            "read": True,
            "context_window": 1050000,
            # Verified 2026-09-23 on the OpenAI pricing + model pages:
            # $0.10 / $0.50, cached input $0.01 (0.1x), cache writes $0.125
            # (the default 1.25x); >272k input: $0.20 / $0.75, cached $0.02,
            # writes $0.25 (2x input and cache, 1.5x output); batch half.
            "input_price_per_mtok": 0.10,
            "output_price_per_mtok": 0.50,
            "cached_input_multiplier": 0.10,
            "long_context_threshold": 272000,
            "long_context_input_multiplier": 2.0,
            "long_context_output_multiplier": 1.5,
        },
        "gpt-5.6-sol": {
            "tokenizer_family": "o200k",
            "provider": "openai",
            "api_model": "gpt-5.6-sol",
            "cache_diagnostics": True,
            "max_input_tokens": 922000,
            "display_name": "GPT-5.6 Sol",
            "read": True,
            "context_window": 1050000,
            # Lowered 2026-08-25 (was 5.00 / 30.00); promotional pricing
            # runs at least through 2026-11-21.
            "input_price_per_mtok": 4.00,
            "output_price_per_mtok": 20.00,
            # OpenAI auto-cache discount: GPT-5.6 Sol caches at 90% off
            "cached_input_multiplier": 0.10,
            "long_context_threshold": 272000,
            "long_context_input_multiplier": 2.0,
            "long_context_output_multiplier": 1.5,
        },
        "gpt-5.6-luna": {
            "tokenizer_family": "o200k",
            "provider": "openai",
            "api_model": "gpt-5.6-luna",
            "cache_diagnostics": True,
            "max_input_tokens": 922000,
            "display_name": "GPT-5.6 Luna",
            "read": True,
            "context_window": 1050000,
            # Verified 2026-09-13 on the OpenAI pricing page: $0.20 / $1.20,
            # cached input $0.02, long context (>272k) $0.40 / $1.80, batch
            # half of each. No promotional note.
            "input_price_per_mtok": 0.20,
            "output_price_per_mtok": 1.20,
            "cached_input_multiplier": 0.10,
            "long_context_threshold": 272000,
            "long_context_input_multiplier": 2.0,
            "long_context_output_multiplier": 1.5,
        },
        "gpt-5.5": {
            "tokenizer_family": "o200k",
            "provider": "openai",
            "api_model": "gpt-5.5",
            "max_input_tokens": 922000,
            "display_name": "GPT-5.5",
            "context_window": 1050000,
            "input_price_per_mtok": 5.00,
            "output_price_per_mtok": 30.00,
            # OpenAI auto-cache discount (#189): GPT-5.5 caches at 75% off
            "cached_input_multiplier": 0.25,
            "long_context_threshold": 272000,
            "long_context_input_multiplier": 2.0,
            "long_context_output_multiplier": 1.5,
            "deprecated": True,
        },
        "gpt-5.4": {
            "tokenizer_family": "o200k",
            "provider": "openai",
            "api_model": "gpt-5.4",
            "max_input_tokens": 922000,
            "display_name": "GPT-5.4",
            "context_window": 1050000,
            "input_price_per_mtok": 2.50,
            "output_price_per_mtok": 15.00,
            "long_context_threshold": 272000,
            "long_context_input_multiplier": 2.0,
            "long_context_output_multiplier": 1.5,
            "deprecated": True,
        },
        "gpt-5": {
            "tokenizer_family": "o200k",
            "provider": "openai",
            "api_model": "gpt-5",
            "display_name": "GPT-5",
            "context_window": 128000,
            "input_price_per_mtok": 1.25,
            "output_price_per_mtok": 10.00,
            "deprecated": True,
        },
        "gpt-5.1": {
            "tokenizer_family": "o200k",
            "provider": "openai",
            "api_model": "gpt-5.1",
            "display_name": "GPT-5.1",
            "context_window": 272000,
            "input_price_per_mtok": 1.25,
            "output_price_per_mtok": 10.00,
            "deprecated": True,
        },
        "gpt-5.2": {
            "tokenizer_family": "o200k",
            "provider": "openai",
            "api_model": "gpt-5.2",
            "display_name": "GPT-5.2",
            "context_window": 272000,
            "input_price_per_mtok": 1.75,
            "output_price_per_mtok": 14.00,
            "deprecated": True,
        },
        "claude-sonnet-5.5": {
            "tokenizer_family": "claude_new",
            "provider": "anthropic",
            "api_model": "claude-sonnet-5-5",
            "display_name": "Sonnet 5.5",
            # Read only (Peter, 2026-10-02): "for Read (not for normal
            # chatting, though)".
            "read": True,
            "chat": False,
            "context_window": 1000000,
            # Verified 2026-10-02 on the Anthropic pricing page: $2 / $10,
            # 5m cache writes $2.50 (the standard 1.25x), batch 50%, flat
            # pricing across the 1M window.
            "input_price_per_mtok": 2.00,
            "output_price_per_mtok": 10.00,
            # Cache hits bill at 0.05x base input ($0.10/MTok), as on Opus
            # 5.5 (pricing page, 2026-10-08; was recorded as 0.1x).
            "cache_read_multiplier": 0.05,
        },
        "claude-haiku-5.5": {
            # The Anthropic pricing page: Claude 4.7 and later models use
            # the newer tokenizer (~30% more tokens than Haiku 4.5).
            "tokenizer_family": "claude_new",
            "provider": "anthropic",
            "api_model": "claude-haiku-5-5",
            "display_name": "Haiku 5.5",
            # Read only (Peter, 2026-10-08): "yes pls, add it to the list
            # of models for Read. We will see how good it is - if it's
            # actually better than Luna, we can figure out how to get the
            # input below 100k toks."
            "read": True,
            "chat": False,
            # 1M context, 128K max output (above DEFAULT_MAX_OUTPUT_TOKENS,
            # so no max_output_tokens entry).
            "context_window": 1000000,
            # Verified 2026-10-08 on the Anthropic pricing page: prompts up
            # to 100,000 tokens $0.10 / $0.50, cache hits $0.01 (the
            # standard 0.1x), 5m writes $0.125 (1.25x); prompts over
            # 100,000 tokens $0.50 / $2.50, hits $0.05, writes $0.625 (5x
            # input, cache and output for the whole request); batch half
            # of each. A day of Community Archive tweets is ~270k tokens,
            # so every Read bills at the higher tier.
            "input_price_per_mtok": 0.10,
            "output_price_per_mtok": 0.50,
            "long_context_threshold": 100000,
            "long_context_input_multiplier": 5.0,
            "long_context_output_multiplier": 5.0,
        },
        "claude-sonnet-4.5": {
            "tokenizer_family": "claude_old",
            "provider": "anthropic",
            "api_model": "claude-sonnet-4-5-20250929",
            "display_name": "Sonnet 4.5",
            "context_window": 200000,
            "input_price_per_mtok": 3.00,
            "output_price_per_mtok": 15.00,
            "deprecated": True,
        },
        "claude-sonnet-4.6": {
            "tokenizer_family": "claude_old",
            "provider": "anthropic",
            "api_model": "claude-sonnet-4-6",
            "display_name": "Sonnet 4.6",
            "context_window": 1000000,
            "input_price_per_mtok": 3.00,
            "output_price_per_mtok": 15.00,
            "deprecated": True,
        },
        "claude-opus-5.5": {
            "tokenizer_family": "claude_new",
            "provider": "anthropic",
            "api_model": "claude-opus-5-5",
            "display_name": "Opus 5.5",
            "featured": True,
            "context_window": 1000000,
            # Verified 2026-09-22 on the Anthropic pricing page: $4/$20,
            # 5m cache writes $5 (the standard 1.25x), batch 50%, flat
            # pricing across the 1M window.
            "input_price_per_mtok": 4.00,
            "output_price_per_mtok": 20.00,
            # Cache hits on Opus 5.5 bill at 0.05x base input ($0.20/MTok),
            # not the standard 0.1x.
            "cache_read_multiplier": 0.05,
        },
        "claude-fable-5.1": {
            "tokenizer_family": "claude_new",
            "provider": "anthropic",
            "api_model": "claude-fable-5-1",
            "display_name": "Fable 5.1",
            "context_window": 1000000,
            # Verified 2026-09-02 on the Anthropic pricing page.
            "input_price_per_mtok": 10.00,
            "output_price_per_mtok": 50.00,
            # Cache hits on Fable 5.1 bill at 0.025x base input ($0.25/MTok),
            # not the 0.1x every other Anthropic model uses.
            "cache_read_multiplier": 0.025,
        },
        "claude-fable-5.2": {
            "tokenizer_family": "claude_new",
            "provider": "anthropic",
            "api_model": "claude-fable-5-2",
            "display_name": "Fable 5.2",
            "context_window": 1000000,
            # Verified 2026-11-10 on the Anthropic pricing page.
            "input_price_per_mtok": 8.00,
            "output_price_per_mtok": 40.00,
            # Cache hits on Fable 5.2 bill at 0.025x base input ($0.20/MTok),
            # not the 0.1x every other Anthropic model uses.
            "cache_read_multiplier": 0.025,
        },
        "claude-opus-5": {
            "tokenizer_family": "claude_new",
            "provider": "anthropic",
            "api_model": "claude-opus-5",
            "display_name": "Opus 5",
            "context_window": 1000000,
            "input_price_per_mtok": 5.00,
            "output_price_per_mtok": 25.00,
            "deprecated": True,
        },
        "claude-opus-4.5": {
            "tokenizer_family": "claude_old",
            "provider": "anthropic",
            "api_model": "claude-opus-4-5-20251101",
            "display_name": "Opus 4.5",
            "context_window": 200000,
            "input_price_per_mtok": 5.00,
            "output_price_per_mtok": 25.00,
            "deprecated": True,
        },
        "claude-opus-4.6": {
            "tokenizer_family": "claude_old",
            "provider": "anthropic",
            "api_model": "claude-opus-4-6",
            "display_name": "Opus 4.6",
            "featured": True,
            "context_window": 1000000,
            "input_price_per_mtok": 5.00,
            "output_price_per_mtok": 25.00,
        },
        "claude-opus-4.8": {
            "tokenizer_family": "claude_new",
            "provider": "anthropic",
            "api_model": "claude-opus-4-8",
            "display_name": "Opus 4.8",
            "context_window": 1000000,
            "input_price_per_mtok": 5.00,
            "output_price_per_mtok": 25.00,
            "deprecated": True,
        },
        "claude-opus-4.7": {
            "tokenizer_family": "claude_new",
            "provider": "anthropic",
            "api_model": "claude-opus-4-7",
            "display_name": "Opus 4.7",
            "context_window": 1000000,
            "input_price_per_mtok": 5.00,
            "output_price_per_mtok": 25.00,
            "deprecated": True,
        },
        "claude-fable-5": {
            "tokenizer_family": "claude_new",
            "provider": "anthropic",
            "api_model": "claude-fable-5",
            "display_name": "Fable 5",
            "context_window": 1000000,
            "input_price_per_mtok": 10.00,
            "output_price_per_mtok": 50.00,
            "deprecated": True,
        },
        "claude-opus-3": {
            "tokenizer_family": "claude_old",
            "provider": "anthropic",
            "api_model": "claude-3-opus-20240229",
            "display_name": "Opus 3",
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

    # --- X (Twitter) API for bookmark sync (#155 / Download) ---
    # Pay-per-use tier; OAuth2 PKCE app. Unset = feature env-gated off.
    X_CLIENT_ID = os.environ.get("X_CLIENT_ID")
    X_CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET")
    X_REDIRECT_URI = os.environ.get(
        "X_REDIRECT_URI",
        "https://loore.org/api/external/twitter/callback",
    )

    # Accounts nobody uses (#647; Peter, 2026-11-12: "I'd say a month?"):
    # after this many days without use, the nightly X bookmark sync and the
    # saved-references digest stop running for the account, and they catch
    # up once when its owner is back (backend/utils/activity.py,
    # in_use_user_ids). 0 turns the pause off.
    UNUSED_ACCOUNT_PAUSE_DAYS = float_from_env(
        "UNUSED_ACCOUNT_PAUSE_DAYS", 30.0)

    # Magic link email authentication (SMTP)
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "localhost")
    # `or "587"` (not just a get() default): docker compose injects UNSET
    # vars as empty strings, and int('') crashes the app on local dev.
    MAIL_PORT = int(os.environ.get("MAIL_PORT") or "587")
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "true").lower() in ("true", "1", "yes")
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "login@loore.org")
    MAGIC_LINK_EXPIRY_SECONDS = int(os.environ.get("MAGIC_LINK_EXPIRY_SECONDS") or "900")
    # How long the link confirming a new sign-in email stays valid (#260).
    # Longer than a sign-in link on purpose: a sign-in link is a credential
    # by itself, this one only works inside a session of the account that
    # asked for the change, and a waitlisted signup who opens the mail an
    # hour later should not find it dead.
    EMAIL_CHANGE_EXPIRY_SECONDS = int(os.environ.get("EMAIL_CHANGE_EXPIRY_SECONDS") or "86400")
    # Per socket operation, for every mail the app sends. smtplib's default
    # is no timeout: a stalled relay held the request that sent the mail.
    MAIL_TIMEOUT_SECONDS = float(os.environ.get("MAIL_TIMEOUT_SECONDS") or "10")
    # Requests one client (network address) may make to each route that
    # emails a typed-in address (sign-in, email change): per 15 minutes and
    # per day. Unset: the defaults in backend/utils/send_limits.py.
    SEND_LIMIT_CLIENT_PER_15_MIN = int(
        os.environ.get("SEND_LIMIT_CLIENT_PER_15_MIN") or "0") or None
    SEND_LIMIT_CLIENT_PER_DAY = int(
        os.environ.get("SEND_LIMIT_CLIENT_PER_DAY") or "0") or None

    FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")

    # --- Server-rendered public pages (SEO/no-JS pass, #228 family) ---
    # Where the built SPA shell lives; Flask injects per-route metadata
    # and public article content into build/index.html. Missing dir (dev
    # containers, tests) falls back to a minimal skeleton shell.
    # (`or` fallback, not a get() default: compose passes unset vars
    # through as empty strings.)
    FRONTEND_BUILD_DIR = os.environ.get("FRONTEND_BUILD_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "frontend", "build")
    # Absolute origin used in canonical URLs, OG tags, sitemap and feeds.
    # Falls back to FRONTEND_URL (correct on prod/staging).
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL") or None

    # Celery configuration for async task queue
    CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
    CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
    # The interactive/background queue split (CELERY_SPLIT_QUEUES, #379)
    # is read in backend/celery_queues.py, not here: Docker Compose reads
    # it there without loading the app.

    # GitHub API for issue creation from Voice mode
    GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
    GITHUB_REPO = os.environ.get("GITHUB_REPO")
    # Shared secret for the inbound GitHub webhook (issue-close
    # notifications). Unset = endpoint disabled (returns 503).
    GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET")

    # GCP Cloud KMS configuration for content encryption at rest
    # Format: projects/{project}/locations/{location}/keyRings/{keyring}/cryptoKeys/{key}
    GCP_KMS_KEY_NAME = os.environ.get("GCP_KMS_KEY_NAME")
    # Set to "true" to disable encryption (for local development without GCP)
    ENCRYPTION_DISABLED = os.environ.get("ENCRYPTION_DISABLED", "false").lower() in ("true", "1", "yes")

    # Production Security Settings
    # Secure defaults to true; local dev sets SESSION_COOKIE_SECURE=false
    # (docker-compose.override.yml) so LAN devices (http://<desktop-ip>:3001,
    # e.g. a phone on the same wifi) can hold a session — browsers reject
    # Secure cookies on insecure non-localhost origins. `or "true"`: compose
    # injects unset vars as empty strings.
    SESSION_COOKIE_SECURE = (
        os.environ.get("SESSION_COOKIE_SECURE") or "true"
    ).lower() in ("true", "1", "yes")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"  # Critical for session cookies to work
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
