"""Liveness and readiness endpoints (roadmap Phase 0).

/health — liveness: the process is up and serving requests. Always 200.
/ready  — readiness: critical dependencies reachable. 200 when DB and
          Redis both respond, 503 otherwise (load balancers / monitors
          should route away on 503).

Unauthenticated by design — they expose only component up/down booleans.
"""
import logging

from flask import Blueprint, current_app, jsonify
from sqlalchemy import text

from backend.extensions import db

logger = logging.getLogger(__name__)

health_bp = Blueprint("health_bp", __name__)


def _check_db():
    try:
        db.session.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("Readiness check: database unreachable")
        return False


def _check_redis():
    try:
        import redis
        url = current_app.config.get(
            "CELERY_BROKER_URL", "redis://localhost:6379/0")
        client = redis.Redis.from_url(
            url, socket_connect_timeout=2, socket_timeout=2)
        return bool(client.ping())
    except Exception:
        logger.exception("Readiness check: redis unreachable")
        return False


@health_bp.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@health_bp.route("/ready", methods=["GET"])
def ready():
    db_ok = _check_db()
    redis_ok = _check_redis()
    ready_ok = db_ok and redis_ok
    return jsonify({
        "status": "ready" if ready_ok else "not_ready",
        "database": db_ok,
        "redis": redis_ok,
    }), (200 if ready_ok else 503)
