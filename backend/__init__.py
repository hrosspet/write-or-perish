from flask import Flask
from dotenv import load_dotenv
import os

load_dotenv()

from backend.config import Config
from backend.extensions import db  # import from extensions.py
from flask_migrate import Migrate
from flask_login import LoginManager
from backend.models import User  # Import your User model
from backend.oauth import init_twitter_blueprint
from flask_cors import CORS

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Configure CORS to allow credentials from your frontend
    CORS(app, supports_credentials=True, origins=[app.config.get("FRONTEND_URL")])

    # Initialize your extensions:
    db.init_app(app)
    migrate = Migrate(app, db)
    login_manager = LoginManager(app)
    login_manager.login_view = "auth_bp.login"

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # Initialize Twitter blueprint
    init_twitter_blueprint(app)

    # Then register other blueprints
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
    
    return app
