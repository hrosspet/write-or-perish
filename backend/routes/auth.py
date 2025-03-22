from flask import Blueprint, redirect, url_for, flash, current_app
from flask_login import login_user, logout_user, login_required, current_user
from backend.models import User
from backend.extensions import db
from flask_dance.contrib.twitter import twitter
from flask import session

auth_bp = Blueprint("auth_bp", __name__)

@auth_bp.route("/login")
def login():
    if not twitter.authorized:
        # If not authorized, start the OAuth flow.
        return redirect(url_for("twitter.login"))
    # Fetch Twitter info
    resp = twitter.get("account/verify_credentials.json")
    if not resp.ok:
        flash("Failed to fetch user info from Twitter.", "error")
        return redirect(url_for("index"))
    tw_info = resp.json()
    twitter_id = str(tw_info["id"])
    username = tw_info["screen_name"]
    user = User.query.filter_by(twitter_id=twitter_id).first()
    if not user:
        # check whether user created just with handle (eg via whitelist)
        user = User.query.filter_by(username=username).first()
        if user:
            # set correct twitter id
            user.twitter_id = twitter_id
            db.session.commit()
        else:
            # create new user
            user = User(twitter_id=twitter_id, username=username)
            db.session.add(user)
            db.session.commit()

    login_user(user)
    flash("Logged in successfully!", "success")
    # Instead of redirecting using url_for() to the backend dashboard,
    # redirect using the FRONTEND_URL from your config.
    frontend_dashboard = f"{current_app.config.get('FRONTEND_URL')}/dashboard"
    return redirect(frontend_dashboard)

@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    session.clear()
    flash("Logged out successfully", "success")
    frontend_url = current_app.config.get("FRONTEND_URL")
    return redirect(frontend_url)
