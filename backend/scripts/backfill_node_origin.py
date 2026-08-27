"""Backfill Node.origin for nodes imported before the column existed.

Derives the platform from the dedup source_key each importer already
stamps: ``twitter:<id>`` / ``chatgpt:<id>`` / ``claude:<uuid>`` prefixes,
and the 64-hex sha256 keys the markdown-zip importer (and the generic
fallback used by ChatGPT/Claude messages lacking a native id) produces.
The sha keys are ambiguous between markdown and generic-fallback chat
messages, so they are labelled "markdown" only when the node has no
llm_model and no llm sibling in its thread — see --dry-run output.

Loore-native nodes (source_key NULL) are left NULL: NULL means Loore.

Pure SQL, no decryption, no KMS calls — safe to run on prod:

    cd /path/to/write-or-perish && python backend/scripts/backfill_node_origin.py --dry-run
    cd /path/to/write-or-perish && python backend/scripts/backfill_node_origin.py
"""
import argparse
import os
import sys

sys.path.insert(0, os.getcwd())

from sqlalchemy import text  # noqa: E402

from backend import create_app, db  # noqa: E402

PREFIXED = {"twitter": "twitter:%", "chatgpt": "chatgpt:%", "claude": "claude:%"}

# sha256 keys: markdown files, or ChatGPT/Claude messages without a native
# id. A thread containing any llm_model-bearing node is a chat import.
SHA_SQL = """
UPDATE node SET origin = :origin
WHERE origin IS NULL
  AND source_key ~ '^[0-9a-f]{64}$'
  AND {cond}
"""
SHA_IS_CHAT = "llm_model IS NOT NULL"
SHA_IS_MD = (
    "llm_model IS NULL AND NOT EXISTS ("
    " SELECT 1 FROM node s WHERE s.user_id = node.user_id"
    "  AND s.llm_model IN ('chatgpt', 'claude-web')"
    "  AND s.created_at BETWEEN node.created_at - interval '1 day'"
    "                       AND node.created_at + interval '1 day')"
)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="count only, no writes")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        for origin, pattern in PREFIXED.items():
            n = db.session.execute(text(
                "SELECT count(*) FROM node WHERE origin IS NULL AND source_key LIKE :p"
            ), {"p": pattern}).scalar()
            print(f"{origin:8s} (prefixed): {n}")
            if not args.dry_run and n:
                db.session.execute(text(
                    "UPDATE node SET origin = :o WHERE origin IS NULL AND source_key LIKE :p"
                ), {"o": origin, "p": pattern})

        for origin, cond in (("chatgpt", SHA_IS_CHAT + " AND llm_model = 'chatgpt'"),
                             ("claude", SHA_IS_CHAT + " AND llm_model = 'claude-web'"),
                             ("markdown", SHA_IS_MD)):
            n = db.session.execute(text(
                f"SELECT count(*) FROM node WHERE origin IS NULL"
                f" AND source_key ~ '^[0-9a-f]{{64}}$' AND {cond}"
            )).scalar()
            print(f"{origin:8s} (sha key):  {n}")
            if not args.dry_run and n:
                db.session.execute(text(SHA_SQL.format(cond=cond)), {"origin": origin})

        left = db.session.execute(text(
            "SELECT count(*) FROM node WHERE origin IS NULL AND source_key IS NOT NULL"
        )).scalar()
        print(f"unresolved imported nodes (left NULL): {left}")

        if args.dry_run:
            db.session.rollback()
            print("dry run — nothing written")
        else:
            db.session.commit()
            print("committed")


if __name__ == "__main__":
    main()
