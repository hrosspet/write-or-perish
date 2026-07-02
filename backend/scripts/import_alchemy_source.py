"""Import an Alchemy source from a URL or file into the database.

Usage (from repo root, inside the app environment):
    python backend/scripts/import_alchemy_source.py \
        --slug meditationbook \
        --title "Meditationbook (Mark Lippmann)" \
        --url https://meditationbook.page/ \
        --description "Mark Lippmann's (meditationstuff) book on meditation." \
        --execute

Without --execute it's a dry run: fetches, chunks, prints stats, writes
nothing. Embedding requires OPENAI key config (chat key); cost is logged
to the admin user (--cost-user-id, default 1).

NOTE: run off the prod VM or in small batches per
project_no_heavy_scripts_on_prod_vm — though this one is light (one page,
one embedding sweep).
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))

from backend import create_app  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--url", help="Fetch source HTML from this URL")
    ap.add_argument("--file", help="Read source text/HTML from a file")
    ap.add_argument("--description", default=None)
    ap.add_argument("--cost-user-id", type=int, default=1)
    ap.add_argument("--execute", action="store_true",
                    help="Actually write to the DB (default: dry run)")
    ap.add_argument("--no-embed", action="store_true",
                    help="Store chunks without embeddings (search inert)")
    args = ap.parse_args()

    if not args.url and not args.file:
        ap.error("one of --url / --file is required")

    app = create_app()
    with app.app_context():
        from backend.utils.alchemy_sources import (
            html_to_text, chunk_text, import_source,
        )
        from backend.utils.api_keys import get_openai_chat_key

        if args.url:
            import requests
            print(f"Fetching {args.url} …")
            resp = requests.get(args.url, timeout=120, headers={
                "User-Agent": "Loore-alchemy-import/1.0"})
            resp.raise_for_status()
            raw = resp.text
        else:
            with open(args.file, "r", encoding="utf-8") as f:
                raw = f.read()

        text = html_to_text(raw) if "<" in raw[:1000] else raw
        chunks = chunk_text(text)
        total_chars = sum(len(c) for _h, c in chunks)
        headings = sum(1 for h, _c in chunks if h)
        print(f"Text: {len(text):,} chars → {len(chunks)} chunks "
              f"({total_chars:,} chars, {headings} with headings)")
        if chunks:
            h0, c0 = chunks[0]
            print(f"First chunk [{h0}]: {c0[:200]!r}")

        if not args.execute:
            print("DRY RUN — nothing written. Re-run with --execute.")
            return

        api_key = None
        if not args.no_embed:
            api_key = get_openai_chat_key(app.config)
            if not api_key:
                print("No OpenAI key configured — aborting (use "
                      "--no-embed to store without vectors).")
                sys.exit(1)

        source, count = import_source(
            args.slug, args.title, text,
            description=args.description, origin_url=args.url,
            api_key=api_key, embed_user_id=args.cost_user_id,
        )
        print(f"Imported source '{source.slug}' (id={source.id}) "
              f"with {count} chunks"
              + (" (no embeddings)" if args.no_embed else ""))


if __name__ == "__main__":
    main()
