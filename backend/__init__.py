from flask import Flask, request, jsonify, redirect, url_for, current_app
from dotenv import load_dotenv
import os

load_dotenv()

from backend.config import Config
from backend.extensions import db
from flask_migrate import Migrate
from flask_login import LoginManager, current_user
from backend.models import User
from backend.oauth import init_twitter_blueprint
from flask_cors import CORS

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

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
        return User.query.get(int(user_id))

    # --------------------------------------------------------------------
    # BLOCK UNAPPROVED USERS
    #
    # If the user is logged in but not approved:
    #   - Allow access to /auth, /static, and /favicon.ico.
    #   - Exempt GET requests to /api/dashboard (for retrieving current user info)
    #     and PUT requests to /api/dashboard/user (for updating user info, e.g. email).
    #   - For other API requests, return a 403 JSON error.
    #   - For other (HTML) requests, redirect to "/" with the query flag ?alpha=1.
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

        # Exempt endpoints that the frontend needs:
        # Allow GET requests to /api/dashboard to fetch current user info.
        # Allow PUT requests to /api/dashboard/user to update the profile (and email).
        if (request.method == "GET" and request.path.startswith("/api/dashboard")) or \
           (request.method == "PUT" and request.path.startswith("/api/dashboard/user")) or \
           request.path.startswith("/api/terms"):
            return

        # For API calls, check if the request expects JSON.
        accept_header = request.headers.get("Accept", "")
        if request.path.startswith("/api") or request.is_json or "application/json" in accept_header:
            return jsonify({"error": "Your account is not approved. Please wait for approval."}), 403

        # For HTML (or non-JSON) requests, redirect to the landing page with an alpha flag.
        return redirect("/?alpha=1")
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

    from backend.routes.export_data import export_bp
    app.register_blueprint(export_bp, url_prefix="/api")

    from backend.routes.feed import feed_bp
    app.register_blueprint(feed_bp, url_prefix="/api")

    from backend.routes.stats import stats_bp
    app.register_blueprint(stats_bp, url_prefix="/api")

    from backend.routes.terms import terms_bp
    app.register_blueprint(terms_bp, url_prefix="/api/terms")

    from backend.routes.admin import admin_bp
    app.register_blueprint(admin_bp, url_prefix="/api/admin")

    return app
