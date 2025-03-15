from flask import Blueprint, jsonify
from flask_login import login_required, current_user
from backend.models import Node
from backend.extensions import db
from sqlalchemy import func

stats_bp = Blueprint("stats_bp", __name__)

@stats_bp.route("/stats", methods=["GET"])
@login_required
def get_stats():
    # Personal day-to-day stats (LLM nodes for the current user)
    personal_stats = db.session.query(
        func.date(Node.created_at).label("date"),
        func.sum(Node.token_count).label("tokens")
    ).filter(
        Node.user_id == current_user.id,
        Node.node_type == 'llm'
    ).group_by(func.date(Node.created_at)).order_by(func.date(Node.created_at)).all()

    # Global day-to-day stats (all LLM nodes)
    global_stats = db.session.query(
        func.date(Node.created_at).label("date"),
        func.sum(Node.token_count).label("tokens")
    ).filter(
        Node.node_type == 'llm'
    ).group_by(func.date(Node.created_at)).order_by(func.date(Node.created_at)).all()

    result = {
       "personal": [{"date": str(date), "tokens": tokens} for date, tokens in personal_stats],
       "global": [{"date": str(date), "tokens": tokens} for date, tokens in global_stats],
       # Optionally, add a totals summary (if you want them separate)…
       "personal_total": db.session.query(func.sum(Node.token_count)).filter(
            Node.user_id == current_user.id, Node.node_type == 'llm'
       ).scalar() or 0,
       "global_total": db.session.query(func.sum(Node.token_count)).filter(
            Node.node_type == 'llm'
       ).scalar() or 0,
    }
    return jsonify(result), 200
