"""Alchemy source material: chunking, embedding, and retrieval.

Sources are published texts (not user data), stored as plaintext chunks
with packed-float32 embeddings (same convention as NodeEmbedding). The
alchemy guide retrieves passages per turn via search_source_chunks.
"""
import re

from backend.extensions import db
from backend.models import AlchemySource, AlchemySourceChunk
from backend.utils.embeddings import (
    embed_texts, pack_vector, top_k_similar,
)

# Chunk size targets, in characters. Split on headings first; oversized
# sections split on paragraph boundaries.
CHUNK_TARGET_CHARS = 2400
CHUNK_MIN_CHARS = 200


def html_to_text(html):
    """Best-effort HTML → readable text without external deps.

    Good enough for long-form book pages (meditationbook.page); not a
    general-purpose parser. Keeps heading markers so chunking can split
    on section boundaries.
    """
    # Drop script/style/head wholesale.
    html = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", html)
    # Mark headings before stripping tags.
    html = re.sub(r"(?is)<h([1-6])[^>]*>(.*?)</h\1>",
                  lambda m: "\n\n## " + re.sub(
                      r"(?s)<[^>]+>", "", m.group(2)).strip() + "\n\n",
                  html)
    # Block-level closers become paragraph breaks.
    html = re.sub(r"(?i)</(p|div|li|blockquote|section|article|tr)>",
                  "\n\n", html)
    html = re.sub(r"(?i)<(br|hr)\s*/?>", "\n", html)
    # Strip remaining tags.
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    # Entities (minimal set).
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " "),
                    ("&mdash;", "—"), ("&ndash;", "–"), ("&hellip;", "…"),
                    ("&rsquo;", "'"), ("&lsquo;", "'"),
                    ("&rdquo;", '"'), ("&ldquo;", '"')):
        text = text.replace(ent, ch)
    # Collapse whitespace but keep paragraph breaks.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text):
    """Split source text into (heading, content) chunks.

    Sections start at '## ' markers (from html_to_text). Oversized
    sections split further on paragraph boundaries; tiny fragments merge
    into their predecessor.
    """
    sections = []
    current_heading = None
    current = []
    for line in text.split("\n"):
        if line.startswith("## "):
            if current:
                sections.append((current_heading, "\n".join(current).strip()))
            current_heading = line[3:].strip()[:255] or None
            current = []
        else:
            current.append(line)
    if current:
        sections.append((current_heading, "\n".join(current).strip()))

    chunks = []
    for heading, body in sections:
        if not body:
            continue
        if len(body) <= CHUNK_TARGET_CHARS:
            pieces = [body]
        else:
            pieces = []
            buf = ""
            for para in body.split("\n\n"):
                if buf and len(buf) + len(para) + 2 > CHUNK_TARGET_CHARS:
                    pieces.append(buf)
                    buf = para
                else:
                    buf = f"{buf}\n\n{para}" if buf else para
            if buf:
                pieces.append(buf)
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            if (len(piece) < CHUNK_MIN_CHARS and chunks
                    and chunks[-1][0] == heading):
                prev_h, prev_c = chunks[-1]
                chunks[-1] = (prev_h, f"{prev_c}\n\n{piece}")
            else:
                chunks.append((heading, piece))
    return chunks


def import_source(slug, title, text, description=None, origin_url=None,
                  api_key=None, embed_user_id=None, replace=True,
                  batch_size=64):
    """Create/replace an AlchemySource from raw text: chunk, embed, store.

    Returns (source, chunk_count). Embedding cost is logged against
    *embed_user_id* (the admin running the import) when given.
    """
    source = AlchemySource.query.filter_by(slug=slug).first()
    if source is None:
        source = AlchemySource(slug=slug, title=title,
                               description=description,
                               origin_url=origin_url)
        db.session.add(source)
        db.session.flush()
    else:
        source.title = title
        if description:
            source.description = description
        if origin_url:
            source.origin_url = origin_url
        if replace:
            AlchemySourceChunk.query.filter_by(
                source_id=source.id).delete()
            db.session.flush()

    chunks = chunk_text(text)
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start:start + batch_size]
        vectors = None
        if api_key:
            vectors = embed_texts(
                [c for _h, c in batch], api_key,
                user_id=embed_user_id,
                request_type="alchemy_source_embedding",
            )
        for offset, (heading, content) in enumerate(batch):
            db.session.add(AlchemySourceChunk(
                source_id=source.id,
                idx=start + offset,
                heading=heading,
                content=content,
                vector=pack_vector(vectors[offset]) if vectors else None,
            ))
        db.session.flush()
    db.session.commit()
    return source, len(chunks)


def search_source_chunks(source_id, query_text, api_key, user_id=None, k=3):
    """Top-k chunks of one source for *query_text*. Returns
    [(chunk_id, score)]."""
    rows = db.session.query(
        AlchemySourceChunk.id, AlchemySourceChunk.vector
    ).filter(
        AlchemySourceChunk.source_id == source_id,
        AlchemySourceChunk.vector.isnot(None),
    ).all()
    if not rows:
        return []
    query_vector = embed_texts(
        [query_text], api_key, user_id=user_id,
        request_type="embedding_query",
    )[0]
    return top_k_similar(query_vector, rows, k=k, min_score=0.0)
