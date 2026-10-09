from flask import Flask, request, jsonify, redirect, url_for, current_app
from dotenv import load_dotenv
import os

# Load environment variables - check for .env.production first, then fall back to .env
# Use absolute path based on the project root (parent of backend/)
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_production_path = os.path.join(project_root, '.env.production')
env_path = os.path.join(project_root, '.env')

if os.path.exists(env_production_path):
    load_dotenv(env_production_path)
elif os.path.exists(env_path):
    load_dotenv(env_path)
else:
    # Fall back to default behavior (looks in current directory and parents)
    load_dotenv()

from backend.config import Config
from backend.extensions import db
from flask_migrate import Migrate
from flask_login import LoginManager, current_user
from backend.models import User  # noqa: F401 - registers every model (migrations)
from backend.oauth import init_twitter_blueprint
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix


def validate_default_model(config):
    """Refuse to boot on an ``LLM_NAME`` that is not an active (non-
    deprecated) chat (not ``"chat": False``) SUPPORTED_MODELS key, or on
    a READ_DEFAULT_MODEL that is not an active read model.

    The keys are the dotted display ids (``claude-opus-4.6``); the API ids
    use dashes (``claude-opus-4-6``). Setting the API id in the env file
    does not fail anywhere visible: every reply route 400s for users with
    no preferred model, and every periodic task (profile seeder, recent
    context, digest) logs a per-user warning and skips — prod ran that way
    for five days on 2026-09-10..15 and once before in May. A boot failure
    turns the typo into a red deploy instead."""
    model_id = config.get("DEFAULT_LLM_MODEL")
    supported = config.get("SUPPORTED_MODELS") or {}
    if model_id in supported:
        # A deprecated default is in no picker and never inherited, so
        # every user without a preference would start on a model the UI
        # cannot show, and the periodic tasks would keep running on it
        # (#355). A read-only model is in no chat picker either, and the
        # default runs every reply and background job (2026-10-02).
        chat_models = ', '.join(
            k for k, v in supported.items()
            if 'provider' in v and not v.get('deprecated')
            and v.get('chat', True))
        if supported[model_id].get("deprecated"):
            raise RuntimeError(
                f"LLM_NAME={model_id!r} is deprecated. Active chat "
                f"models: {chat_models}")
        if not supported[model_id].get("chat", True):
            raise RuntimeError(
                f"LLM_NAME={model_id!r} is read only (\"chat\": False). "
                f"Active chat models: {chat_models}")
        read_default = config.get("READ_DEFAULT_MODEL")
        read_cfg = supported.get(read_default) or {}
        if read_default and (not read_cfg.get("read")
                             or read_cfg.get("deprecated")):
            raise RuntimeError(
                f"READ_DEFAULT_MODEL={read_default!r} is not an active "
                f"model flagged 'read'.")
        return
    hint = ""
    dotted = (model_id or "").replace("-", ".")
    for key in supported:
        if key.replace("-", ".") == dotted:
            hint = f" (did you mean {key!r}?)"
            break
    raise RuntimeError(
        f"LLM_NAME={model_id!r} is not a SUPPORTED_MODELS key{hint}. "
        f"Known keys: {', '.join(sorted(supported))}")


# What Sentry may receive. send_default_pii=False only drops cookies, IPs
# and user ids: the SDK still attaches each stack frame's local variables
# and request bodies up to 10 KB, and both can hold decrypted user content
# (an entry being saved, a todo list being merged). Nobody outside the
# user's own session reads their content, so neither is sent.
SENTRY_PRIVACY_OPTIONS = {
    "send_default_pii": False,
    "include_local_variables": False,
    "max_request_body_size": "never",
    "traces_sample_rate": 0.0,   # errors only, no perf tracing
}


def _without_query(url):
    return url.split("?", 1)[0].split("#", 1)[0]


def _drop_query_strings(event):
    """Query strings can carry user input (the search box sends its words
    as ?q=), and send_default_pii=False does not remove them: the SDK
    sends the request's query string, the Referer header carries the
    query of the page that made the call, and outgoing-HTTP breadcrumbs
    keep each call's query in `http.query`. All are removed; the paths
    stay, so the route is still identifiable. (Access-log lines, which
    hold whole request lines, are kept out by ignore_logger in
    create_app.)"""
    request_info = event.get("request")
    if isinstance(request_info, dict):
        request_info.pop("query_string", None)
        if isinstance(request_info.get("url"), str):
            request_info["url"] = _without_query(request_info["url"])
        headers = request_info.get("headers")
        if isinstance(headers, dict):
            for name, value in list(headers.items()):
                if name.lower() == "referer" and isinstance(value, str):
                    headers[name] = _without_query(value)
    breadcrumbs = event.get("breadcrumbs")
    if isinstance(breadcrumbs, dict):
        for crumb in breadcrumbs.get("values") or ():
            data = crumb.get("data") if isinstance(crumb, dict) else None
            if isinstance(data, dict):
                data.pop("http.query", None)
                data.pop("http.fragment", None)
                if isinstance(data.get("url"), str):
                    data["url"] = _without_query(data["url"])
    return event


def create_app():
    # Error monitoring (roadmap Phase 0). No-op unless SENTRY_DSN is set.
    sentry_dsn = os.environ.get("SENTRY_DSN")
    if sentry_dsn:
        import sentry_sdk
        from sentry_sdk.integrations.logging import ignore_logger

        # Access-log lines hold each request's full request line and
        # Referer, query strings included. Logged at INFO they become
        # breadcrumbs, and an event captured outside the request scope
        # (gunicorn's own "Error handling request" when a streamed
        # response fails) carries them. Sentry ignores these loggers.
        ignore_logger("gunicorn.access")
        ignore_logger("werkzeug")

        def _before_send(event, hint):
            _drop_query_strings(event)
            # Drop known, expected noise. Match against BOTH the exception text
            # and the log-record message so it's caught regardless of which
            # Sentry path captured it (OpenAI integration, logging, or the
            # Celery task-failure hook).
            exc = (hint.get("exc_info") or (None, None, None))[1]
            rec = hint.get("log_record")
            text = " ".join(filter(None, [
                str(exc) if exc else "",
                rec.getMessage() if rec is not None else "",
            ]))
            # 1) embed_texts intentionally trips the 8191-token limit on
            #    token-dense nodes, catches the 400, and retries at a smaller
            #    char cap — a handled, transient condition.
            if "maximum input length" in text:
                return None
            # A WorkerLostError("…signal 15 (SIGTERM)…") used to follow every
            # deploy and was dropped here as noise. Since #312 a restart lets
            # running tasks finish; a pool process gets SIGTERM only when a
            # task outlives the 90 s drain (scripts/celery-graceful-stop.sh),
            # and that task is lost, so it reports.
            # 2) A call refused for an account reason (#369): every event
            #    about it — task failures, logged exceptions, log lines
            #    tagged with provider_alerts.log_extra — groups into one
            #    issue per cause, whatever the call site.
            try:
                from backend.utils.provider_alerts import apply_fingerprint
                apply_fingerprint(event, exc, rec)
            except Exception:  # never lose an event over grouping
                pass
            return event

        sentry_sdk.init(
            dsn=sentry_dsn,
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
            before_send=_before_send,
            **SENTRY_PRIVACY_OPTIONS,
        )

    app = Flask(__name__)
    app.config.from_object(Config)
    validate_default_model(app.config)

    # Fix for running behind nginx reverse proxy - handles X-Forwarded-* headers
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # Configure CORS to allow credentials from your frontend
    CORS(app, supports_credentials=True, origins=[app.config.get("FRONTEND_URL")])

    # Initialize extensions:
    db.init_app(app)
    migrate = Migrate(app, db)
    login_manager = LoginManager(app)
    login_manager.login_view = "auth_bp.login"

    @login_manager.unauthorized_handler
    def unauthorized():
        if request.path.startswith("/api"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect(url_for("auth_bp.login"))

    @login_manager.user_loader
    def load_user(user_id):
        # A deleted account in its grace period (#269) loads as nobody:
        # every session and remember cookie stops working at once.
        from backend.utils.account_deletion import session_user
        return session_user(user_id)

    # --------------------------------------------------------------------
    # BLOCK UNAPPROVED USERS
    #
    # If the user is logged in but not approved:
    #   - Allow access to /auth, /static, and /favicon.ico.
    #   - Exempt GET requests to /api/dashboard (for retrieving current user info),
    #     PUT requests to /api/dashboard/user (for updating user info) and the
    #     email flow under /api/dashboard/email (a waitlisted signup leaves
    #     their address before approval).
    #   - For other API requests, return a 403 JSON error.
    #   - For other (HTML) requests, redirect to "/" with the query flag ?alpha=1.
    @app.before_request
    def touch_last_seen_hook():
        # Admin Activity tab: who came back, and where they last were.
        if (current_user.is_authenticated and request.path.startswith("/api/")
                and not request.path.startswith("/api/admin")):
            from backend.utils.activity import touch_last_seen
            touch_last_seen(current_user, request.path)

    @app.before_request
    def block_unapproved_users():
        if not current_user.is_authenticated:
            return  # nothing to check

        if current_user.approved:
            return  # approved users proceed

        # Allow access to specific URL prefixes regardless.
        allowed_prefixes = ["/auth", "/static", "/favicon.ico"]
        if any(request.path.startswith(prefix) for prefix in allowed_prefixes):
            return

        # Server-rendered public pages are world-readable by definition —
        # approval is irrelevant there (and /landing must stay reachable
        # or the redirect below would loop).
        if request.endpoint and request.endpoint.startswith("public_pages."):
            return

        # Exempt endpoints that the frontend needs:
        # Allow GET requests to /api/dashboard to fetch current user info.
        # Allow PUT requests to /api/dashboard/user to update the profile.
        # Allow the email flow (#260): request, confirm, cancel, remove. A
        # waitlisted signup leaves their address on the thank-you page, and
        # Activate & Welcome refuses an account without one. PUT
        # /api/dashboard/user no longer takes an email.
        email_path = request.path.rstrip("/")
        if (request.method == "GET" and request.path.startswith("/api/dashboard")) or \
           (request.method == "PUT" and request.path.startswith("/api/dashboard/user")) or \
           (request.method in ("POST", "DELETE")
                and (email_path == "/api/dashboard/email"
                     or email_path.startswith("/api/dashboard/email/"))) or \
           request.path.startswith("/api/terms"):
            return

        # For API calls, check if the request expects JSON.
        accept_header = request.headers.get("Accept", "")
        if request.path.startswith("/api") or request.is_json or "application/json" in accept_header:
            return jsonify({"error": "Your account is not approved. Please wait for approval."}), 403

        # For HTML (or non-JSON) requests, redirect to the landing page with an alpha flag.
        return redirect("/landing?alpha=1")
    # --------------------------------------------------------------------

    # Initialize Twitter blueprint.
    init_twitter_blueprint(app)

    # Register blueprints.
    from backend.routes.auth import auth_bp
    app.register_blueprint(auth_bp, url_prefix="/auth")

    from backend.routes.nodes import nodes_bp
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")

    from backend.routes.dashboard import dashboard_bp
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")

    # "Delete all my writing" (#268).
    from backend.routes.account_data import account_data_bp
    app.register_blueprint(account_data_bp, url_prefix="/api/account")

    # Account deletion and restore (#269).
    from backend.routes.account_deletion import account_deletion_bp
    app.register_blueprint(account_deletion_bp, url_prefix="/api/account")

    from backend.routes.export_data import export_bp
    app.register_blueprint(export_bp, url_prefix="/api")

    from backend.routes.import_data import import_bp
    app.register_blueprint(import_bp, url_prefix="/api")

    from backend.routes.log import log_bp
    app.register_blueprint(log_bp, url_prefix="/api")

    from backend.routes.search import search_bp
    app.register_blueprint(search_bp, url_prefix="/api")

    from backend.routes.terms import terms_bp
    app.register_blueprint(terms_bp, url_prefix="/api/terms")

    from backend.routes.admin import admin_bp
    app.register_blueprint(admin_bp, url_prefix="/api/admin")

    from backend.routes.profile import profile_bp
    app.register_blueprint(profile_bp, url_prefix="/api/profile")

    from backend.routes.drafts import drafts_bp
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")

    from backend.routes.todo import todo_bp
    app.register_blueprint(todo_bp, url_prefix="/api/todo")

    from backend.routes.textmode import textmode_bp
    app.register_blueprint(textmode_bp, url_prefix="/api/textmode")

    from backend.routes.voice import voice_bp
    app.register_blueprint(voice_bp, url_prefix="/api/voice")

    from backend.routes.read import read_bp
    app.register_blueprint(read_bp, url_prefix="/api/read")

    from backend.routes.github_issues import github_bp
    app.register_blueprint(github_bp, url_prefix="/api/github")

    from backend.routes.feedback import feedback_bp
    app.register_blueprint(feedback_bp, url_prefix="/api/feedback")

    # Upload v1 / Share (dark behind SHARE_V1; routes 404 while off).
    from backend.routes.share import share_bp
    app.register_blueprint(share_bp, url_prefix="/api/share")

    # The Commons — public forum (#228, same dark flag as the Share family).
    from backend.routes.commons import commons_bp
    app.register_blueprint(commons_bp, url_prefix="/api/commons")

    # Server-rendered public pages: articles, profiles, sitemap, feeds —
    # real HTML for crawlers and no-JS clients. nginx routes /@…, /node/…,
    # /sitemap.xml and the marketing paths here; everything else still
    # goes straight to the static SPA shell.
    from backend.routes.public_pages import public_pages_bp
    app.register_blueprint(public_pages_bp)

    # Dev-update channel: changelog + notifications + polls (#207).
    from backend.routes.updates import updates_bp
    app.register_blueprint(updates_bp, url_prefix="/api/updates")

    # Inbound webhooks (GitHub issue-close → fix_ready notification).
    from backend.routes.webhooks import webhooks_bp
    app.register_blueprint(webhooks_bp, url_prefix="/api/webhooks")

    # AI preferences folded into the artifact model (#158 Slice 5): managed via
    # the generic /api/artifacts CRUD (kind="ai_preferences"); the dedicated
    # /api/ai-preferences blueprint was removed.

    from backend.routes.artifacts import artifacts_bp
    app.register_blueprint(artifacts_bp, url_prefix="/api/artifacts")

    from backend.routes.external import external_bp
    app.register_blueprint(external_bp, url_prefix="/api/external")

    from backend.routes.prompts import prompts_bp
    app.register_blueprint(prompts_bp, url_prefix="/api/prompts")

    # --------------------------------------------------------------------
    # Voice‑mode media blueprint – serves audio files in dev & tests.
    # --------------------------------------------------------------------
    from backend.routes.media import media_bp
    app.register_blueprint(media_bp, url_prefix="/media")

    # --------------------------------------------------------------------
    # SSE (Server-Sent Events) blueprint – real-time streaming updates.
    # --------------------------------------------------------------------
    from backend.routes.sse import sse_bp
    app.register_blueprint(sse_bp, url_prefix="/api/sse")

    # Per-user spend cap: any HTTP path that tries to create an LLM
    # placeholder for a blocked user raises SpendCapExceeded; surface it as
    # the standard 402 payload so the frontend shows the spend-cap banner.
    from backend.utils.spend import SpendCapExceeded

    @app.errorhandler(SpendCapExceeded)
    def _handle_spend_cap(_exc):
        from flask import jsonify
        return jsonify({
            "error": "monthly_spend_limit_reached",
            "message": (
                "You've reached your monthly usage limit for the free "
                "alpha. It resets at the start of next month."
            ),
        }), 402

    # A reply (or a Voice turn) where AI may not read: create_llm_placeholder
    # raises AIUsageRefused before any write. Routes answer it themselves
    # where they keep the user's entry; any that lets it escape gets the
    # same 403 {"error", "code": "ai_usage_none", "scope"}.
    from backend.utils.llm_nodes import AIUsageRefused

    @app.errorhandler(AIUsageRefused)
    def _handle_ai_usage_refused(exc):
        from backend.utils.llm_nodes import ai_usage_refused_response
        return ai_usage_refused_response(exc)

    # A reply that is not a read, asked for on a read-only model:
    # create_llm_placeholder raises ReadOnlyModelRefused before any write
    # (the reply is never moved to another model, 2026-10-02). Routes that
    # let it escape get 400 {"error", "code": "model_read_only", "model"},
    # and whatever they flushed before the call is not kept.
    from backend.utils.llm_nodes import ReadOnlyModelRefused

    @app.errorhandler(ReadOnlyModelRefused)
    def _handle_read_only_model(exc):
        from backend.extensions import db
        from backend.utils.llm_nodes import read_only_model_response
        db.session.rollback()
        return read_only_model_response(exc)

    # --------------------------------------------------------------------
    # Health checks – liveness/readiness for monitoring (no auth).
    # --------------------------------------------------------------------
    from backend.routes.health import health_bp
    app.register_blueprint(health_bp)
    # Also under /api so the checks are reachable through the public
    # nginx proxy (which only forwards /api, /auth, /media to Flask).
    app.register_blueprint(health_bp, url_prefix="/api", name="health_api_bp")

    # Register CLI commands
    from backend.init_db import (
        init_db_command, backfill_human_owner_command,
    )
    app.cli.add_command(init_db_command)
    app.cli.add_command(backfill_human_owner_command)

    try:
        from experiments.prompt_rct.run_rct import rct_cli
        app.cli.add_command(rct_cli)
    except ImportError:
        pass  # experiments not available (e.g. Docker/staging)

    return app
