#!/usr/bin/env python3
"""
Breakdown of API costs by user and request_type over the last N days.

Usage:
    python backend/scripts/cost_breakdown.py [DAYS]

DAYS defaults to 4.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.app import create_app
from backend.extensions import db
from sqlalchemy import text


def _print_rows(title, columns, rows, money_cols=("cost_usd",)):
    print()
    print(f"=== {title} ===")
    if not rows:
        print("  (no data)")
        return

    def fmt(col, val):
        if val is None:
            return "-"
        if col in money_cols:
            return f"${float(val):.4f}"
        return str(val)

    widths = [
        max(len(c), max(len(fmt(c, r[i])) for r in rows))
        for i, c in enumerate(columns)
    ]
    sep = "  "
    print(sep.join(c.ljust(w) for c, w in zip(columns, widths)))
    print(sep.join("-" * w for w in widths))
    for r in rows:
        print(sep.join(fmt(c, r[i]).ljust(w) for i, (c, w) in enumerate(zip(columns, widths))))


def cost_breakdown(days):
    app = create_app()
    with app.app_context():
        window = f"NOW() - INTERVAL '{int(days)} days'"

        by_user_type = db.session.execute(text(f"""
            SELECT u.username,
                   l.request_type,
                   COUNT(*) AS n_calls,
                   SUM(l.input_tokens) AS input_tokens,
                   SUM(l.output_tokens) AS output_tokens,
                   ROUND(SUM(l.audio_duration_seconds)::numeric, 1) AS audio_sec,
                   ROUND(SUM(l.cost_microdollars)::numeric / 1e6, 4) AS cost_usd
            FROM api_cost_log l
            JOIN "user" u ON u.id = l.user_id
            WHERE l.created_at >= {window}
            GROUP BY u.username, l.request_type
            ORDER BY cost_usd DESC
        """)).fetchall()

        by_user = db.session.execute(text(f"""
            SELECT u.username,
                   COUNT(*) AS n_calls,
                   ROUND(SUM(l.cost_microdollars)::numeric / 1e6, 4) AS cost_usd
            FROM api_cost_log l
            JOIN "user" u ON u.id = l.user_id
            WHERE l.created_at >= {window}
            GROUP BY u.username
            ORDER BY cost_usd DESC
        """)).fetchall()

        by_type = db.session.execute(text(f"""
            SELECT l.request_type,
                   COUNT(*) AS n_calls,
                   ROUND(SUM(l.cost_microdollars)::numeric / 1e6, 4) AS cost_usd
            FROM api_cost_log l
            WHERE l.created_at >= {window}
            GROUP BY l.request_type
            ORDER BY cost_usd DESC
        """)).fetchall()

        by_model = db.session.execute(text(f"""
            SELECT l.model_id,
                   COUNT(*) AS n_calls,
                   ROUND(SUM(l.cost_microdollars)::numeric / 1e6, 4) AS cost_usd
            FROM api_cost_log l
            WHERE l.created_at >= {window}
            GROUP BY l.model_id
            ORDER BY cost_usd DESC
        """)).fetchall()

        grand = db.session.execute(text(f"""
            SELECT COUNT(*) AS n_calls,
                   COUNT(DISTINCT user_id) AS n_users,
                   ROUND(SUM(cost_microdollars)::numeric / 1e6, 4) AS cost_usd
            FROM api_cost_log
            WHERE created_at >= {window}
        """)).fetchall()

        print(f"API cost breakdown — last {days} days")
        _print_rows(
            "By user × request_type",
            ["username", "request_type", "n_calls", "input_tokens",
             "output_tokens", "audio_sec", "cost_usd"],
            by_user_type,
        )
        _print_rows("By user", ["username", "n_calls", "cost_usd"], by_user)
        _print_rows("By request_type",
                    ["request_type", "n_calls", "cost_usd"], by_type)
        _print_rows("By model", ["model_id", "n_calls", "cost_usd"], by_model)
        _print_rows("Grand total",
                    ["n_calls", "n_users", "cost_usd"], grand)


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    cost_breakdown(days)
