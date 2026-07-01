#!/usr/bin/env python3
"""
For every user with a recent_context history, show the boundary node's
token_count at their latest RC cutoff. Boundary nodes with
token_count >= 10k are the ones that would have tripped the pre-fix
regeneration loop.

Usage:
    python backend/scripts/check_boundary_node_risk.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.app import create_app
from backend.extensions import db
from sqlalchemy import text


THRESHOLD = 10000


def check():
    app = create_app()
    with app.app_context():
        rows = db.session.execute(text("""
            WITH latest_rc AS (
                SELECT DISTINCT ON (user_id)
                       user_id,
                       id          AS rc_id,
                       created_at  AS rc_created_at,
                       source_data_cutoff
                FROM user_recent_context
                ORDER BY user_id, created_at DESC
            )
            SELECT u.username,
                   u.plan,
                   r.rc_created_at,
                   r.source_data_cutoff,
                   (
                       SELECT COALESCE(SUM(n.token_count), 0)
                       FROM node n
                       WHERE (n.user_id = u.id OR n.human_owner_id = u.id)
                         AND n.ai_usage IN ('chat', 'train')
                         AND n.created_at = r.source_data_cutoff
                   ) AS boundary_token_count,
                   (
                       SELECT COUNT(*)
                       FROM api_cost_log l
                       WHERE l.user_id = u.id
                         AND l.request_type = 'recent_context'
                         AND l.created_at >= NOW() - INTERVAL '4 days'
                   ) AS rc_calls_4d
            FROM latest_rc r
            JOIN "user" u ON u.id = r.user_id
            ORDER BY boundary_token_count DESC
        """)).fetchall()

        if not rows:
            print("No users with recent_context history.")
            return

        print(f"{'username':<16} {'plan':<10} {'rc_created_at':<22} "
              f"{'boundary_tok':>13} {'calls_4d':>9} {'at_risk':>8}")
        print("-" * 85)
        for r in rows:
            at_risk = "YES" if (r.boundary_token_count or 0) >= THRESHOLD else ""
            print(f"{r.username:<16} {str(r.plan):<10} "
                  f"{str(r.rc_created_at):<22} "
                  f"{str(r.boundary_token_count):>13} "
                  f"{r.rc_calls_4d:>9} {at_risk:>8}")


if __name__ == "__main__":
    check()
