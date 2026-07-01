#!/usr/bin/env python3
"""
Debug runaway recent_context costs for a specific user.

Usage:
    python backend/scripts/debug_recent_context.py [USERNAME] [DAYS]

USERNAME defaults to 'hrosspet'. DAYS defaults to 4.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.app import create_app
from backend.extensions import db
from sqlalchemy import text


def debug(username, days):
    app = create_app()
    with app.app_context():
        uid_row = db.session.execute(
            text('SELECT id FROM "user" WHERE username = :u'),
            {"u": username},
        ).fetchone()
        if not uid_row:
            print(f"No user named {username!r}")
            return
        uid = uid_row.id

        print(f"=== User {username} (id={uid}) — last {days} days ===\n")

        # 1. Each recent_context call: timing, input size, cost
        print("--- recent_context calls (most recent first) ---")
        rows = db.session.execute(text(f"""
            SELECT created_at,
                   input_tokens,
                   output_tokens,
                   ROUND(cost_microdollars::numeric / 1e6, 4) AS cost_usd
            FROM api_cost_log
            WHERE user_id = :uid
              AND request_type = 'recent_context'
              AND created_at >= NOW() - INTERVAL '{int(days)} days'
            ORDER BY created_at DESC
            LIMIT 40
        """), {"uid": uid}).fetchall()
        print(f"{'created_at':<22} {'in_tok':>9} {'out_tok':>8} {'cost_usd':>10}")
        for r in rows:
            print(f"{str(r.created_at):<22} {r.input_tokens:>9} "
                  f"{r.output_tokens:>8} {'$'+str(r.cost_usd):>10}")

        # 2. Inter-call deltas — are they bunched up?
        print("\n--- intervals between consecutive calls ---")
        interval_rows = db.session.execute(text(f"""
            SELECT created_at
                   - LAG(created_at) OVER (ORDER BY created_at) AS gap,
                   input_tokens,
                   ROUND(cost_microdollars::numeric / 1e6, 4) AS cost_usd
            FROM api_cost_log
            WHERE user_id = :uid
              AND request_type = 'recent_context'
              AND created_at >= NOW() - INTERVAL '{int(days)} days'
            ORDER BY created_at DESC
            LIMIT 20
        """), {"uid": uid}).fetchall()
        for r in interval_rows:
            print(f"gap={str(r.gap):<25} in_tok={r.input_tokens:>7}  "
                  f"cost=${r.cost_usd}")

        # 3. UserRecentContext records — are cutoffs advancing?
        print("\n--- UserRecentContext records (most recent first) ---")
        rc_rows = db.session.execute(text(f"""
            SELECT id, created_at, profile_id,
                   source_data_cutoff, source_tokens_covered, tokens_used
            FROM user_recent_context
            WHERE user_id = :uid
              AND created_at >= NOW() - INTERVAL '{int(days)} days'
            ORDER BY created_at DESC
            LIMIT 20
        """), {"uid": uid}).fetchall()
        print(f"{'id':>6} {'created_at':<22} {'profile':>8} "
              f"{'src_cutoff':<22} {'src_toks':>10} {'used_toks':>10}")
        for r in rc_rows:
            print(f"{r.id:>6} {str(r.created_at):<22} "
                  f"{str(r.profile_id):>8} "
                  f"{str(r.source_data_cutoff):<22} "
                  f"{str(r.source_tokens_covered):>10} "
                  f"{str(r.tokens_used):>10}")

        # 4. Is the cutoff advancing? diff per step
        print("\n--- cutoff advancement between consecutive RCs ---")
        adv_rows = db.session.execute(text(f"""
            SELECT created_at,
                   source_data_cutoff,
                   source_data_cutoff
                     - LAG(source_data_cutoff) OVER (ORDER BY created_at)
                     AS cutoff_delta,
                   source_tokens_covered
                     - LAG(source_tokens_covered) OVER (ORDER BY created_at)
                     AS tokens_delta
            FROM user_recent_context
            WHERE user_id = :uid
              AND created_at >= NOW() - INTERVAL '{int(days)} days'
            ORDER BY created_at DESC
            LIMIT 20
        """), {"uid": uid}).fetchall()
        for r in adv_rows:
            print(f"{str(r.created_at):<22} "
                  f"cutoff_Δ={str(r.cutoff_delta):<20} "
                  f"tokens_Δ={r.tokens_delta}")

        # 5. Latest profile & its cutoff — window size
        print("\n--- latest profile for this user ---")
        prof = db.session.execute(text("""
            SELECT id, created_at, source_data_cutoff, source_tokens_used
            FROM user_profile
            WHERE user_id = :uid
            ORDER BY created_at DESC
            LIMIT 3
        """), {"uid": uid}).fetchall()
        for p in prof:
            print(f"profile={p.id}  created={p.created_at}  "
                  f"cutoff={p.source_data_cutoff}  tokens={p.source_tokens_used}")

        # 6. New node activity since the most recent profile cutoff
        print("\n--- new eligible node tokens since latest profile cutoff ---")
        if prof:
            cutoff = prof[0].source_data_cutoff
            node_stats = db.session.execute(text("""
                SELECT COUNT(*) AS n_nodes,
                       COALESCE(SUM(token_count), 0) AS tokens,
                       MIN(created_at) AS earliest,
                       MAX(created_at) AS latest
                FROM node
                WHERE (user_id = :uid OR human_owner_id = :uid)
                  AND ai_usage IN ('chat', 'train')
                  AND created_at >= :cutoff
            """), {"uid": uid, "cutoff": cutoff}).fetchone()
            print(f"since profile cutoff {cutoff}:")
            print(f"  nodes={node_stats.n_nodes}  tokens={node_stats.tokens}")
            print(f"  earliest={node_stats.earliest}")
            print(f"  latest  ={node_stats.latest}")

        # 7. Node counts broken down over the last N days
        print("\n--- daily new eligible node tokens ---")
        daily = db.session.execute(text(f"""
            SELECT date_trunc('day', created_at) AS day,
                   COUNT(*) AS n_nodes,
                   SUM(token_count) AS tokens
            FROM node
            WHERE (user_id = :uid OR human_owner_id = :uid)
              AND ai_usage IN ('chat', 'train')
              AND created_at >= NOW() - INTERVAL '{int(days)} days'
            GROUP BY day
            ORDER BY day DESC
        """), {"uid": uid}).fetchall()
        for r in daily:
            print(f"  {r.day}  nodes={r.n_nodes:>4}  tokens={r.tokens}")


if __name__ == "__main__":
    username = sys.argv[1] if len(sys.argv) > 1 else "hrosspet"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    debug(username, days)
