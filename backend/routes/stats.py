from flask import Blueprint, jsonify
from flask_login import login_required, current_user
from backend.models import Node
from backend.extensions import db
from sqlalchemy import func

stats_bp = Blueprint("stats_bp", __name__)

# Helper functions to calculate summed tokens using the new distributed_tokens column.
def get_total_tokens(user):
    tokens = db.session.query(func.sum(Node.distributed_tokens)).filter(
        Node.user_id == user.id
    ).scalar()
    return tokens or 0

def get_global_tokens():
    tokens = db.session.query(func.sum(Node.distributed_tokens)).scalar()
    return tokens or 0

@stats_bp.route("/stats", methods=["GET"])
@login_required
def get_stats():
    # Personal day-to-day stats for the current user
    personal_stats = db.session.query(
        func.date(Node.created_at).label("date"),
        func.sum(Node.distributed_tokens).label("tokens")
    ).filter(
        Node.user_id == current_user.id
    ).group_by(func.date(Node.created_at)).order_by(func.date(Node.created_at)).all()

    # Global day-to-day stats over all users
    global_stats = db.session.query(
        func.date(Node.created_at).label("date"),
        func.sum(Node.distributed_tokens).label("tokens")
    ).group_by(func.date(Node.created_at)).order_by(func.date(Node.created_at)).all()

    result = {
       "personal": [{"date": str(date), "tokens": tokens} for date, tokens in personal_stats],
       "global": [{"date": str(date), "tokens": tokens} for date, tokens in global_stats],
       "personal_total": get_total_tokens(current_user),
       "global_total": get_global_tokens(),
    }
    return jsonify(result), 200