#!/usr/bin/env python3
"""
Show the last recent_context generation for a user and time since.

Usage:
    python backend/scripts/recent_context_heartbeat.py [USERNAME]
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.app import create_app
from backend.extensions import db
from sqlalchemy import text


def heartbeat(username):
    app = create_app()
    with app.app_context():
        row = db.session.execute(text("""
            SELECT l.created_at,
                   l.input_tokens,
                   l.output_tokens,
                   ROUND(l.cost_microdollars::numeric / 1e6, 4) AS cost_usd,
                   NOW() - l.created_at AS age,
                   NOW() AS now_utc
            FROM api_cost_log l
            JOIN "user" u ON u.id = l.user_id
            WHERE u.username = :u
              AND l.request_type = 'recent_context'
            ORDER BY l.created_at DESC
            LIMIT 5
        """), {"u": username}).fetchall()

        if not row:
            print(f"No recent_context calls for {username}")
            return

        print(f"server now (UTC): {row[0].now_utc}")
        print()
        print(f"{'created_at':<22} {'age':<18} {'in_tok':>8} {'cost':>8}")
        for r in row:
            print(f"{str(r.created_at):<22} {str(r.age):<18} "
                  f"{r.input_tokens:>8} {'$'+str(r.cost_usd):>8}")

        print()
        print("Beat fires check_pending_recent_context_updates every 600s.")
        print("If the fix worked, subsequent calls should stop appearing "
              "(until user writes ≥10k new tokens).")


if __name__ == "__main__":
    username = sys.argv[1] if len(sys.argv) > 1 else "hrosspet"
    heartbeat(username)
