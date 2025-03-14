from flask import Blueprint, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from backend.models import User
from backend.extensions import db
from flask_dance.contrib.twitter import twitter

auth_bp = Blueprint("auth_bp", __name__)

@auth_bp.route("/login")
def login():
    # When a user hits the “login” endpoint, redirect for OAuth if needed.
    if not twitter.authorized:
        return redirect(url_for("twitter.login"))
    # Use Twitter’s API to fetch basic account info.
    resp = twitter.get("account/verify_credentials.json")
    if not resp.ok:
        flash("Failed to fetch user info from Twitter.", "error")
        return redirect(url_for("index"))
    tw_info = resp.json()
    twitter_id = str(tw_info["id"])
    username = tw_info["screen_name"]
    # Look up or create the user in our DB.
    user = User.query.filter_by(twitter_id=twitter_id).first()
    if not user:
        user = User(twitter_id=twitter_id, username=username)
        db.session.add(user)
        db.session.commit()
        # (You can add an onboarding step here if desired.)
    login_user(user)
    flash("Logged in successfully!", "success")
    return redirect(url_for("dashboard_bp.get_dashboard"))

@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out successfully", "success")
    return redirect(url_for("index"))
