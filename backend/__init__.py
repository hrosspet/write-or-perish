from flask import Flask
from dotenv import load_dotenv
import os

load_dotenv()

from backend.config import Config
from backend.extensions import db  # import from extensions.py
from flask_migrate import Migrate
from flask_login import LoginManager

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Initialize extensions with app
    db.init_app(app)
    migrate = Migrate(app, db)
    login_manager = LoginManager(app)
    login_manager.login_view = "auth_bp.login"

    # Import models after initializing db (if needed)
    from backend import models

    # Register blueprints
    from backend.routes.auth import auth_bp
    app.register_blueprint(auth_bp, url_prefix="/auth")
    from backend.routes.nodes import nodes_bp
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    from backend.routes.dashboard import dashboard_bp
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    from backend.routes.export_data import export_bp
    app.register_blueprint(export_bp, url_prefix="/api")

    return app
