from flask import Blueprint, jsonify
from flask_login import login_required, current_user
from backend.models import Node, User
from backend.extensions import db
from sqlalchemy import func, text

stats_bp = Blueprint("stats_bp", __name__)

def get_total_tokens(user):
    tokens = db.session.query(func.sum(Node.distributed_tokens)).filter(
        Node.user_id == user.id
    ).scalar()
    return tokens or 0

def get_global_tokens():
    tokens = db.session.query(func.sum(Node.distributed_tokens)).scalar()
    return tokens or 0

# Allow an optional username (if not provided, defaults to current_user)
@stats_bp.route("/stats", defaults={"username": None}, methods=["GET"])
@stats_bp.route("/stats/<string:username>", methods=["GET"])
@login_required
def get_stats(username):
    # If a username is provided, fetch that user; otherwise, use current_user.
    if username:
        user = db.session.query(User).filter_by(username=username).first_or_404()
    else:
        user = current_user

    # PERSONAL SERIES: get the first day the current user posted
    personal_first_date = db.session.query(func.min(func.date(Node.created_at))).filter(
        Node.user_id == user.id
    ).scalar()

    if personal_first_date is None:
        personal_series = []
    else:
        stmt_personal = text("""
            SELECT gs.day,
                   COALESCE(s.tokens, 0) as tokens
            FROM generate_series(CAST(:start_date AS date), CURRENT_DATE, '1 day') AS gs(day)
            LEFT JOIN (
                SELECT date(created_at) AS day, SUM(distributed_tokens) AS tokens
                FROM node
                WHERE user_id = :user_id
                GROUP BY day
            ) s ON gs.day = s.day
            ORDER BY gs.day
        """)
        personal_result = db.session.execute(stmt_personal, {
            "start_date": personal_first_date,
            "user_id": user.id
        }).fetchall()
        personal_series = [{"date": row.day.strftime("%Y-%m-%d"), "tokens": int(row.tokens)} for row in personal_result]

    # GLOBAL SERIES: get the very first post date among all users.
    global_first_date = db.session.query(func.min(func.date(Node.created_at))).scalar()

    if global_first_date is None:
        global_series = []
    else:
        stmt_global = text("""
            SELECT gs.day,
                   COALESCE(s.tokens, 0) as tokens
            FROM generate_series(CAST(:start_date AS date), CURRENT_DATE, '1 day') AS gs(day)
            LEFT JOIN (
                SELECT date(created_at) AS day, SUM(distributed_tokens) AS tokens
                FROM node
                GROUP BY day
            ) s ON gs.day = s.day
            ORDER BY gs.day
        """)
        global_result = db.session.execute(stmt_global, {
            "start_date": global_first_date
        }).fetchall()
        global_series = [{"date": row.day.strftime("%Y-%m-%d"), "tokens": int(row.tokens)} for row in global_result]

    result = {
       "personal": personal_series,
       "global": global_series,
       "personal_total": get_total_tokens(user),
       "global_total": get_global_tokens(),
    }
    return jsonify(result), 200
