from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.utils.error_privacy import describe_exception
from backend.models import Node, User, UserProfile
from backend.extensions import db
from backend.utils.privacy import AI_ALLOWED
from backend.utils import docx_import, pdf_import
from backend.utils.pdf_extract_worker import MAX_CHAPTERS as MAX_PDF_CHAPTERS
from datetime import datetime, timedelta
import hashlib
import os
import re
import hmac
import sys
import zipfile
import zlib
import io
import shutil
import tempfile
import json

import_bp = Blueprint("import_bp", __name__)

DOCX_UNEXPECTED_ERROR = (
    "Something went wrong while reading this file. You can try again, or "
    "save the document again as .docx and upload that copy.")

# Analyzed formats that become one entry per chapter (see _file_parts).
CHAPTER_FORMATS = ("docx", "pdf")
# The confirm step's answer to "Whose writing is this?" for a PDF.
WHOSE_WRITING = ("mine", "someone_else")

# Node.provenance values — HOW an imported node reached Loore (the column
# comment in models.py says what each one vouches for). `origin` names the
# platform; this names the path, which is what decides whether the text
# is known to be public (#295).
PROVENANCE_ARCHIVE_UPLOAD = "archive_upload"  # the user's own export
PROVENANCE_PREFILL_CA = "prefill_ca"          # Loore fetched, Community Archive
PROVENANCE_PREFILL_X = "prefill_x"            # Loore fetched, X API
PROVENANCES = (PROVENANCE_ARCHIVE_UPLOAD, PROVENANCE_PREFILL_CA,
               PROVENANCE_PREFILL_X)


def approximate_token_count(text):
    """
    Approximate token count for a text string.
    Uses a simple heuristic: ~4 characters per token.
    """
    return len(text) // 4


def _generic_source_key(author, timestamp, content):
    """Stable fallback dedup key when no source-native id is available.

    NUL separators prevent boundary ambiguity between the fields.
    """
    raw = f"{author}\x00{timestamp}\x00{content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_source_key_index(user_id):
    """
    Map source_key -> node id for this user's previously imported nodes,
    plus the set of keys whose node is currently soft-deleted.

    Loaded once per confirm request so dedup checks are dict lookups
    instead of one query per message. The node ids let partially-skipped
    threads chain new messages onto the existing copy of the previous
    message rather than orphaning them as new roots.

    Scoping is on ``human_owner_id`` (the importing human), NOT ``user_id``:
    imported assistant/LLM messages are stored under a synthetic LLM user
    (e.g. ``chatgpt`` / ``claude-web``) via ``user_id`` while the real
    importer is recorded in ``human_owner_id``. Scoping on the human owner
    therefore dedups both human and assistant turns per importing user, so
    two different users importing the same archive each keep their own copy.

    Returns:
        (key_index, deleted_keys) where key_index maps source_key ->
        node id and deleted_keys contains the keys of soft-deleted nodes.
    """
    rows = db.session.query(
        Node.source_key, Node.id, Node.deleted_at
    ).filter(
        Node.human_owner_id == user_id,
        Node.source_key.isnot(None),
    )
    key_index = {}
    deleted_keys = set()
    for source_key, node_id, deleted_at in rows:
        key_index[source_key] = node_id
        if deleted_at is not None:
            deleted_keys.add(source_key)
    return key_index, deleted_keys


def _claude_msg_key(msg):
    """Dedup key for one Claude confirm-payload message.

    Prefers the export's per-message uuid (rename-proof, bounded
    length); falls back to a content hash when it is missing.
    """
    msg_uuid = msg.get('uuid')
    if msg_uuid:
        return f"claude:{msg_uuid}"
    return _generic_source_key(
        msg.get('sender', 'human'), msg.get('created_at', ''),
        msg.get('text', ''),
    )


def _chatgpt_msg_key(msg):
    """Dedup key for one ChatGPT confirm-payload message.

    The mapping id alone identifies the message globally (rename-proof,
    bounded length); falls back to a content hash when it is missing.
    """
    mapping_id = msg.get('mapping_id')
    if mapping_id:
        return f"chatgpt:{mapping_id}"
    return _generic_source_key(
        msg.get('role', 'user'), msg.get('created_at', ''),
        msg.get('text', ''),
    )


def _add_imported_message_nodes(user_id, human_owner_id, parent_id,
                                node_type, llm_model, node_content,
                                privacy_level, ai_usage, source_key,
                                msg_created_at, origin,
                                provenance=PROVENANCE_ARCHIVE_UPLOAD,
                                dek=None):
    """Create the node(s) for one imported message, splitting content
    above NODE_CHAR_CAP into a serial parent→child chain.

    source_key lands on the chain TIP (last part): both the dedup index
    and the next message's parent resolve via source_key, so keeping it
    on the tip means re-imports skip the whole message and follow-ups
    chain after the full content — identical to a fresh import.

    Every caller today reads an export the user uploaded, hence the
    default provenance; a future Loore-fetched path must say what it is.

    *dek*: the import's shared data key (see Node.set_content), or None
    for one KMS call per node.

    Returns (tip_node, nodes_created_count).
    """
    if provenance not in PROVENANCES:
        raise ValueError(f"Unknown provenance {provenance!r}")
    from backend.utils.node_split import split_text_at_cap
    segments = split_text_at_cap(node_content)
    tip = None
    for j, seg in enumerate(segments):
        n = Node(
            user_id=user_id,
            human_owner_id=human_owner_id,
            parent_id=parent_id if tip is None else tip.id,
            node_type=node_type,
            llm_model=llm_model,
            token_count=approximate_token_count(seg),
            privacy_level=privacy_level,
            ai_usage=ai_usage,
            source_key=source_key if j == len(segments) - 1 else None,
            origin=origin,
            provenance=provenance,
        )
        # Never pass content= to Node(): that writes the raw column and
        # bypasses KMS envelope encryption (#256). set_content() is the
        # only sanctioned way to store text.
        n.set_content(seg, dek=dek)
        if msg_created_at:
            n.created_at = msg_created_at + timedelta(milliseconds=j)
        db.session.add(n)
        db.session.flush()
        tip = n
    return tip, len(segments)


def _deleted_match_response(keys, deleted_keys, on_deleted):
    """409 response when the import collides with soft-deleted nodes and
    the client hasn't said what to do about them.

    The frontend surfaces this as a restore-or-skip dialog and retries
    the confirm with ``on_deleted`` set: ``"skip"`` leaves the deleted
    nodes alone (they stay deleted and are not re-imported), ``"restore"``
    un-deletes them in place. Returns None when no dialog is needed.
    """
    if on_deleted in ('restore', 'skip'):
        return None
    matches = sum(1 for k in keys if k in deleted_keys)
    if not matches:
        return None
    return jsonify({
        "error": "deleted_content_matches",
        "deleted_matches": matches,
    }), 409


def _restore_node(node_id, content, privacy_level, ai_usage,
                  token_count=None, *, provenance, dek=None):
    """Un-delete a soft-deleted imported node in place.

    Refills content from the archive — this also recovers tombstones
    whose content was already wiped by the cleanup task — and keeps the
    node id, so existing child links stay intact. Privacy/AI-usage are
    set to this import's choices, like any other (re)imported node, and
    so is the provenance: the restored text is this import's copy (a
    re-upload after a CA pre-fill drops the public vouch, which errs on
    the private side; the reverse regains it). Required, as on
    create_twitter_nodes, so no importer can bring back a row nobody
    can classify.
    """
    if provenance not in PROVENANCES:
        raise ValueError(f"Unknown provenance {provenance!r}")
    node = Node.query.get(node_id)
    # before set_content: decides encryption
    node.set_privacy_level(privacy_level, dek=dek)
    node.set_content(content, dek=dek)
    node.token_count = (
        token_count if token_count is not None
        else approximate_token_count(content)
    )
    node.ai_usage = ai_usage
    node.provenance = provenance
    node.deleted_at = None


def _apply_settings_to_skipped(node_ids, privacy_level, ai_usage, dek=None):
    """Apply this import's privacy/ai_usage to already-imported nodes.

    Re-importing an archive is also how users change their mind about
    import settings, so dedup-skipped (alive) nodes adopt the values
    chosen for this import instead of being a pure no-op. Returns the
    number of nodes whose settings actually changed; callers report it
    as ``updated`` and keep it disjoint from ``skipped``. Provenance is
    NOT touched: how a node first arrived is a fact, not a setting.
    """
    from sqlalchemy import or_
    ids = [i for i in node_ids if i is not None]
    updated = 0
    # Chunked so a huge archive doesn't produce an unbounded IN clause.
    # Goes through the ORM (not a bulk UPDATE) because a privacy change
    # moves content across the encryption boundary (#257) — only the
    # rows whose settings actually differ are loaded.
    for start in range(0, len(ids), 1000):
        chunk = ids[start:start + 1000]
        for node in Node.query.filter(
            Node.id.in_(chunk),
            or_(
                Node.privacy_level != privacy_level,
                Node.ai_usage != ai_usage,
            ),
        ).all():
            node.set_privacy_level(privacy_level, dek=dek)
            node.ai_usage = ai_usage
            updated += 1
        db.session.flush()
    return updated


def _release_deleted_docx_key(file_data, source_key, key_index,
                              deleted_keys, on_deleted):
    """Re-importing a deleted Word document or PDF with "restore" is a new
    import: the dedup key sits on the last node of its chapter chain, so
    refilling that node would bring back only the last chapter. The
    deleted tip gives up the key and the document is imported again in
    full."""
    if (file_data.get('source_format') in CHAPTER_FORMATS
            and on_deleted == 'restore' and source_key in deleted_keys):
        old = Node.query.get(key_index.pop(source_key))
        if old is not None:
            old.source_key = None
            db.session.flush()
        deleted_keys.discard(source_key)


def _errors_summary(errors, max_names=3):
    """The skipped files of a zip as one clause per distinct message, so a
    zip of thirty PDFs says "upload each PDF on its own" once, not thirty
    times: "a.pdf, b.pdf, c.pdf and 27 more: PDFs inside a zip ..."."""
    by_message = {}
    for e in errors:
        by_message.setdefault(e['error'], []).append(e['name'])
    parts = []
    for message, names in by_message.items():
        shown = ", ".join(names[:max_names])
        if len(names) > max_names:
            shown += f" and {len(names) - max_names} more"
        parts.append(f"{shown}: {message}")
    return " ".join(parts)


def _docx_file_entry(filename, content, modified):
    """Analyze-step entry for a converted Word document. source_format
    tells the confirm step to split it into chapter nodes."""
    return {
        "name": filename,
        "filename_without_ext": filename.rsplit('.', 1)[0],
        "content": content,
        "size": len(content.encode('utf-8')),
        "modified_at": modified.isoformat(),
        "token_count": approximate_token_count(content),
        "source_format": "docx",
    }


def _pdf_file_entry(filename, pdf):
    """Analyze-step entry for an extracted PDF. Own entries are dated by
    the PDF's modification date, else its creation date, else now; a
    Reference keeps the creation date (when the document was written),
    else the modification date. `reference` is what the confirm step
    saves when the user says it is someone else's writing."""
    from backend.utils import document_reference
    from backend.utils.node_split import NODE_CHAR_CAP
    content = pdf["markdown"]
    stem = filename.rsplit('.', 1)[0]
    own_date = pdf["modified"] or pdf["created"] or datetime.utcnow()
    doc_date = pdf["created"] or pdf["modified"]
    ref_title = pdf_import.reference_title(pdf["title"], stem)
    # The same room the save leaves for the text under title and author
    room = document_reference.body_room(ref_title, pdf["author"],
                                        NODE_CHAR_CAP)
    return {
        "name": filename,
        "filename_without_ext": stem,
        "content": content,
        "size": len(content.encode('utf-8')),
        "modified_at": own_date.isoformat(),
        "token_count": approximate_token_count(content),
        "source_format": "pdf",
        "pages": pdf["pages"],
        "chapters": pdf["chapters"],
        "reference": {
            "title": ref_title,
            "author": pdf["author"],
            "date": doc_date.isoformat() if doc_date else None,
            # A Reference holds one entry's worth of text, as a clip does
            "truncated": len(content.strip()) > room,
            "char_cap": room,
        },
    }


def _analyze_pdf(upload):
    """/import/analyze for a single .pdf upload."""
    try:
        pdf = pdf_import.extract_pdf(upload.stream)
    except pdf_import.PdfImportError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        # Event reaches Sentry; only the exception type is logged
        current_app.logger.exception(
            "Unexpected error reading PDF: %s", describe_exception(sys.exc_info()[1]))
        return jsonify({"error": pdf_import.PDF_UNEXPECTED_ERROR}), 500
    entry = _pdf_file_entry(upload.filename, pdf)
    return jsonify({
        "files": [entry], "total_files": 1,
        "total_tokens": entry["token_count"],
        "total_size": entry["size"], "errors": [],
    }), 200


def _keyed_digest(purpose, *parts):
    """HMAC-SHA256 hex (64 chars) of `parts` for the current user, keyed
    with the app's SECRET_KEY: a stable dedup key for an uploaded
    document's text that does not stand for the text by itself."""
    secret = current_app.config.get("SECRET_KEY")
    if not secret:
        raise RuntimeError("SECRET_KEY is not set")
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    msg = "\x00".join([purpose, str(current_user.id), *map(str, parts)])
    return hmac.new(secret, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _save_pdf_reference(file_data):
    """Answer "someone else's": the PDF's text is saved as one Reference
    (an ExternalItem, like a page saved with the Chrome clipper), not as
    the user's entries, so it never feeds their profile. Mirrors clip():
    the text is capped at one entry's worth, it is encrypted at rest, a
    second save of the same text is a no-op, and a copy hidden by a
    waiting "Delete all my writing" becomes the user's again. Unlike a
    clip, the title and author go inside the encrypted text
    (utils/document_reference), not into the plaintext columns. The
    periodic sweeps pick it up like any new reference (embedding,
    digest)."""
    from sqlalchemy.exc import IntegrityError
    from backend.models import ExternalItem, SOURCE_DOCUMENT
    from backend.utils import document_reference
    from backend.utils.hidden_rows import reclaim_external_items
    from backend.utils.node_split import NODE_CHAR_CAP

    content = file_data.get('content')
    if not isinstance(content, str) or not content.strip():
        return jsonify({"error": "This PDF has no text to save."}), 400
    content = content.strip()
    # Keyed by the whole extracted text, so the same PDF is one Reference
    external_id = _keyed_digest("pdf-reference", content)
    ref = file_data.get('reference')
    ref = ref if isinstance(ref, dict) else {}
    title = (str(ref.get('title') or '').strip()
             or str(file_data.get('filename_without_ext') or '').strip())
    author = ref.get('author')
    # The header and the text together hold one entry's worth
    room = document_reference.body_room(title, author, NODE_CHAR_CAP)
    truncated = len(content) > room
    if truncated:
        content = content[:room].rstrip()
    content = document_reference.compose(title, author, content)
    posted_at = pdf_import.parse_date(ref.get('date'))
    user_id = current_user.id

    reclaim_external_items(user_id, [external_id], sources=[SOURCE_DOCUMENT])

    def lookup():
        return ExternalItem.query.filter_by(
            user_id=user_id, source=SOURCE_DOCUMENT,
            external_id=external_id).first()

    def already_saved(existing):
        db.session.commit()  # keeps a reclaim from above
        return jsonify({
            "message": "Already saved as a reference",
            "saved_as": "reference", "reference_id": existing.id,
            "nodes_created": 0, "thread_count": 0,
            "profile_update_task_id": None,
            "created": 0, "skipped": 1, "restored": 0, "updated": 0,
            "truncated": False,
        }), 200

    existing = lookup()
    if existing is not None:
        return already_saved(existing)
    item = ExternalItem(
        user_id=user_id, source=SOURCE_DOCUMENT, external_id=external_id,
        # Title and author are in the encrypted text; the date is kept
        # as the reference's date, as for every other source
        author_handle=None, title=None, url=None, posted_at=posted_at,
        # An uploaded file may be private: assessed, not vouched public
        public_source=False,
    )
    item.set_content(content)
    db.session.add(item)
    try:
        db.session.commit()
    except IntegrityError:
        # A double press of Confirm saved it first
        db.session.rollback()
        existing = lookup()
        if existing is None:
            raise
        return already_saved(existing)
    return jsonify({
        "message": "Saved as a reference",
        "saved_as": "reference", "reference_id": item.id,
        "nodes_created": 0, "thread_count": 0,
        "profile_update_task_id": None,
        "created": 1, "skipped": 0, "restored": 0, "updated": 0,
        "truncated": truncated,
    }), 201


def _file_parts(file_data, filename, content):
    """Node texts for one analyzed file. Markdown: one text. Word
    documents whose headings repeat, and PDFs with a usable outline: one
    text per chapter. The filename heading goes on the first text unless
    it starts with an H1. A PDF never gets more entries per chapter than
    analyze allows (MAX_CHAPTERS); above that it is split by size."""
    if file_data.get('source_format') in CHAPTER_FORMATS:
        parts = docx_import.split_chapters(content)
        if (file_data.get('source_format') == 'pdf'
                and len(parts) > MAX_PDF_CHAPTERS + 1):
            parts = [content]
    else:
        parts = [content]
    if not parts[0].lstrip().startswith('# '):
        parts[0] = f"# {filename}\n\n{parts[0]}"
    return parts


def _import_file_nodes(file_data, parts, parent_id, source_key, created_at,
                       privacy_level, ai_usage):
    """Create the node(s) for one analyzed file: a chain with one entry
    per part (each part itself splits above the per-node cap). The
    source_key lands on the last node. Returns (tip_node, nodes_created)."""
    source_format = file_data.get('source_format')
    origin = source_format if source_format in CHAPTER_FORMATS else "markdown"
    tip = None
    created = 0
    for k, part in enumerate(parts):
        tip, n = _add_imported_message_nodes(
            user_id=current_user.id,
            human_owner_id=current_user.id,
            parent_id=parent_id if tip is None else tip.id,
            node_type="user",
            llm_model=None,
            node_content=part,
            privacy_level=privacy_level,
            ai_usage=ai_usage,
            source_key=source_key if k == len(parts) - 1 else None,
            # one second per chapter keeps the chain in order
            msg_created_at=(created_at + timedelta(seconds=k)
                            if created_at else None),
            origin=origin,
        )
        created += n
    return tip, created


@import_bp.route("/import/analyze", methods=["POST"])
@login_required
def analyze_import():
    """
    Analyze a zip file containing .md documents for import.

    Expects:
        - multipart/form-data with 'zip_file' field

    Returns:
        {
            "files": [
                {
                    "name": "document.md",
                    "filename_without_ext": "document",
                    "content": "file content",
                    "size": 1234,
                    "created_at": "2024-01-01T12:00:00",
                    "modified_at": "2024-01-02T12:00:00",
                    "token_count": 300
                },
                ...
            ],
            "total_files": 5,
            "total_tokens": 1500,
            "total_size": 6789
        }
    """
    # Check if file is present
    if 'zip_file' not in request.files:
        return jsonify({"error": "No zip_file provided"}), 400

    zip_file = request.files['zip_file']

    if zip_file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    filename_lower = zip_file.filename.lower()
    if filename_lower.endswith('.docm'):
        return jsonify({"error": f"{zip_file.filename}: macro-enabled Word "
                        "files (.docm) are not accepted; save it as .docx "
                        "and try again"}), 400
    if filename_lower.endswith('.doc'):
        return jsonify({"error": pdf_import.MSG_OLD_DOC}), 400

    # A PDF is extracted in a child process (see utils/pdf_import.py)
    if filename_lower.endswith('.pdf'):
        return _analyze_pdf(zip_file)

    # A single Word document is analyzed from the upload stream directly
    if filename_lower.endswith('.docx'):
        try:
            content, modified = docx_import.convert_docx(zip_file.stream)
        except docx_import.DocxImportError as e:
            return jsonify({"error": f"{zip_file.filename}: {e}"}), 400
        except Exception:
            # Event reaches Sentry; only the exception type is logged
            current_app.logger.exception(
                "Unexpected error converting .docx: %s",
                describe_exception(sys.exc_info()[1]))
            return jsonify({"error": DOCX_UNEXPECTED_ERROR}), 500
        if not content.strip():
            return jsonify({"error": f"{zip_file.filename}: the document has no text"}), 400
        entry = _docx_file_entry(
            zip_file.filename, content, modified or datetime.utcnow())
        return jsonify({
            "files": [entry], "total_files": 1,
            "total_tokens": entry["token_count"],
            "total_size": entry["size"], "errors": [],
        }), 200

    # Validate it's a zip file
    if not filename_lower.endswith('.zip'):
        return jsonify({"error": "File must be a .zip, .docx or .pdf file"}), 400

    try:
        # Read zip file into memory
        zip_bytes = io.BytesIO(zip_file.read())

        files_data = []
        errors = []
        parse_budget = docx_import.ParseBudget()  # shared by all docx here
        total_tokens = 0
        total_size = 0

        with zipfile.ZipFile(zip_bytes, 'r') as zip_ref:
            # Get list of all .md / .docx files in the zip (.docm, .doc
            # and .pdf are listed so that they are reported, not silently
            # ignored)
            md_files = [f for f in zip_ref.namelist()
                        if f.lower().endswith(('.md', '.docx', '.docm',
                                               '.doc', '.pdf'))
                        and not f.startswith('__MACOSX/')
                        and not f.split('/')[-1].startswith('~$')]

            if not md_files:
                return jsonify({"error": "No .md or .docx files found in the zip archive"}), 400

            for file_path in md_files:
                # Get file info
                zip_info = zip_ref.getinfo(file_path)

                # Skip directories
                if zip_info.is_dir():
                    continue

                # Extract just the filename from the path
                filename = file_path.split('/')[-1]
                filename_without_ext = filename.rsplit('.', 1)[0] if '.' in filename else filename

                # Get timestamps from zip metadata
                # ZipInfo date_time is a tuple: (year, month, day, hour, minute, second)
                dt_tuple = zip_info.date_time
                file_datetime = datetime(*dt_tuple)

                if filename.lower().endswith('.doc'):
                    errors.append({"name": filename,
                                   "error": pdf_import.MSG_OLD_DOC})
                    continue
                if filename.lower().endswith('.pdf'):
                    errors.append({"name": filename,
                                   "error": pdf_import.MSG_PDF_IN_ZIP})
                    continue

                if not filename.lower().endswith('.md'):
                    try:
                        docx_import.check_extension(filename)
                        with tempfile.SpooledTemporaryFile(
                                max_size=docx_import.SPOOL_MAX_MEMORY_BYTES) as tmp:
                            try:
                                with zip_ref.open(zip_info) as src:
                                    shutil.copyfileobj(src, tmp)
                            except (RuntimeError, zipfile.BadZipFile,
                                    zlib.error, NotImplementedError):
                                # encrypted, CRC/data error, unsupported
                                # compression; zipfile's messages name the
                                # member, so nothing is logged here
                                raise docx_import.DocxImportError(
                                    "the file is damaged or "
                                    "password-protected")
                            tmp.seek(0)
                            content, modified = docx_import.convert_docx(
                                tmp, parse_budget)
                    except docx_import.DocxImportError as e:
                        errors.append({"name": filename, "error": str(e)})
                        continue
                    except Exception:
                        # type only: exception messages can contain names
                        current_app.logger.exception(
                            "Unexpected error converting .docx in zip: %s",
                            describe_exception(sys.exc_info()[1]))
                        errors.append({"name": filename,
                                       "error": DOCX_UNEXPECTED_ERROR})
                        continue
                    if not content.strip():
                        errors.append({"name": filename,
                                       "error": "the document has no text"})
                        continue
                    entry = _docx_file_entry(
                        filename, content, modified or file_datetime)
                    files_data.append(entry)
                    total_tokens += entry["token_count"]
                    total_size += entry["size"]
                    continue

                # Read file content
                try:
                    content = zip_ref.read(file_path).decode('utf-8')
                except UnicodeDecodeError:
                    # Skip files that can't be decoded as UTF-8
                    current_app.logger.warning("Skipping a zip entry: not valid UTF-8")
                    continue

                # Calculate tokens
                token_count = approximate_token_count(content)

                file_data = {
                    "name": filename,
                    "filename_without_ext": filename_without_ext,
                    "content": content,
                    "size": zip_info.file_size,
                    "modified_at": file_datetime.isoformat(),
                    "token_count": token_count
                }

                files_data.append(file_data)
                total_tokens += token_count
                total_size += zip_info.file_size

        if not files_data:
            if errors:
                return jsonify({"error": "No files could be imported. "
                                         + _errors_summary(errors)}), 400
            return jsonify({"error": "No valid .md or .docx files could be read from the zip archive"}), 400

        return jsonify({
            "files": files_data,
            "total_files": len(files_data),
            "total_tokens": total_tokens,
            "total_size": total_size,
            "errors": errors,
        }), 200

    except zipfile.BadZipFile:
        return jsonify({"error": "Invalid zip file"}), 400
    except Exception as e:
        # Event reaches Sentry; only the exception type is logged
        current_app.logger.exception(
            "Error analyzing import: %s", describe_exception(e))
        return jsonify({"error": "Failed to analyze zip file"}), 500


@import_bp.route("/import/confirm", methods=["POST"])
@login_required
def confirm_import():
    """
    Create nodes from analyzed import data.

    Request body:
        {
            "files": [
                {
                    "filename_without_ext": "document",
                    "content": "file content",
                    "modified_at": "2024-01-01T12:00:00"
                },
                ...
            ],
            "import_type": "single_thread" | "separate_nodes",
            "date_ordering": "modified" | "created",
            "whose_writing": "mine" | "someone_else"   (required for a PDF)
        }

    A PDF that is someone else's writing is saved as a Reference instead
    of entries (see _save_pdf_reference).

    Returns:
        {
            "message": "Import successful",
            "nodes_created": 5,
            "thread_count": 1 or 5
        }
    """
    data = request.get_json()

    if not data:
        return jsonify({"error": "No data provided"}), 400

    files = data.get('files', [])
    import_type = data.get('import_type', 'separate_nodes')
    date_ordering = data.get('date_ordering', 'modified')
    privacy_level = data.get('privacy_level', 'private')
    ai_usage = data.get('ai_usage', 'none')
    on_deleted = data.get('on_deleted')

    if not files:
        return jsonify({"error": "No files provided"}), 400

    if import_type not in ['single_thread', 'separate_nodes']:
        return jsonify({"error": "Invalid import_type. Must be 'single_thread' or 'separate_nodes'"}), 400

    if date_ordering not in ['modified', 'created']:
        return jsonify({"error": "Invalid date_ordering. Must be 'modified' or 'created'"}), 400

    # A PDF may be someone else's writing; the user must say (no default:
    # a wrong guess would feed another author's text into their profile).
    if any(isinstance(f, dict) and f.get('source_format') == 'pdf'
           for f in files):
        whose_writing = data.get('whose_writing')
        if whose_writing not in WHOSE_WRITING:
            return jsonify({"error": "Choose whose writing this is: yours, "
                            "or someone else's."}), 400
        if whose_writing == 'someone_else':
            if len(files) != 1:
                return jsonify({"error": "Save one PDF at a time as a "
                                "reference."}), 400
            return _save_pdf_reference(files[0])

    try:
        # Sort files by the selected date ordering
        # For now, we only have modified_at from zip metadata, so we'll use that
        files_sorted = sorted(files, key=lambda f: f.get('modified_at', ''))

        nodes_created = 0
        nodes_skipped = 0
        nodes_restored = 0
        thread_count = 0
        skipped_alive_ids = []
        key_index, deleted_keys = _load_source_key_index(current_user.id)

        def _file_source_key(filename, content, source_format=None):
            """sha256 of (filename, content) for markdown-zip dedup; a
            PDF's key is keyed with the server secret and the user."""
            if source_format == 'pdf':
                return _keyed_digest("pdf-entry", filename, content)
            raw = f"{filename}\x00{content}"
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()

        conflict = _deleted_match_response(
            (_file_source_key(f.get('filename_without_ext', 'Untitled'),
                              f.get('content', ''), f.get('source_format'))
             for f in files_sorted),
            deleted_keys, on_deleted,
        )
        if conflict:
            return conflict

        if import_type == 'single_thread':
            # Create a single thread with all files as sequential nodes
            parent_id = None

            for file_data in files_sorted:
                filename = file_data.get('filename_without_ext', 'Untitled')
                content = file_data.get('content', '')

                # Add filename as markdown headline only if content doesn't have H1
                parts = _file_parts(file_data, filename, content)
                node_content = parts[-1]  # the tip: what a restore refills

                source_key = _file_source_key(
                    filename, content, file_data.get('source_format'))
                _release_deleted_docx_key(file_data, source_key, key_index,
                                          deleted_keys, on_deleted)
                if source_key in key_index:
                    # Chain the next new file onto the existing copy so
                    # partially-skipped imports don't orphan new nodes.
                    if source_key in deleted_keys and on_deleted == 'restore':
                        _restore_node(key_index[source_key], node_content,
                                      privacy_level, ai_usage,
                                      provenance=PROVENANCE_ARCHIVE_UPLOAD)
                        deleted_keys.discard(source_key)
                        nodes_restored += 1
                    else:
                        nodes_skipped += 1
                        if source_key not in deleted_keys:
                            skipped_alive_ids.append(key_index[source_key])
                    parent_id = key_index[source_key]
                    continue

                # Parse original timestamp from zip metadata
                node_created_at = None
                raw_ts = file_data.get('modified_at', '')
                if raw_ts:
                    try:
                        node_created_at = datetime.fromisoformat(raw_ts)
                    except (ValueError, TypeError):
                        pass

                # Create node(s) — files above the per-node cap split
                # into a serial chain
                tip, created_count = _import_file_nodes(
                    file_data, parts, parent_id, source_key,
                    node_created_at, privacy_level, ai_usage)

                key_index[source_key] = tip.id
                parent_id = tip.id
                nodes_created += created_count

            thread_count = 1

        else:  # separate_nodes
            # Create separate top-level threads for each file
            threads_created = 0
            for file_data in files_sorted:
                filename = file_data.get('filename_without_ext', 'Untitled')
                content = file_data.get('content', '')

                # Add filename as markdown headline only if content doesn't have H1
                parts = _file_parts(file_data, filename, content)
                node_content = parts[-1]  # the tip: what a restore refills

                source_key = _file_source_key(
                    filename, content, file_data.get('source_format'))
                _release_deleted_docx_key(file_data, source_key, key_index,
                                          deleted_keys, on_deleted)
                if source_key in key_index:
                    if source_key in deleted_keys and on_deleted == 'restore':
                        _restore_node(key_index[source_key], node_content,
                                      privacy_level, ai_usage,
                                      provenance=PROVENANCE_ARCHIVE_UPLOAD)
                        deleted_keys.discard(source_key)
                        nodes_restored += 1
                    else:
                        nodes_skipped += 1
                        if source_key not in deleted_keys:
                            skipped_alive_ids.append(key_index[source_key])
                    continue
                key_index[source_key] = None  # id not needed: no chaining

                # Parse original timestamp from zip metadata
                node_created_at = None
                raw_ts = file_data.get('modified_at', '')
                if raw_ts:
                    try:
                        node_created_at = datetime.fromisoformat(raw_ts)
                    except (ValueError, TypeError):
                        pass

                # Create top-level node (parent_id=None); files above the
                # per-node cap split into a serial chain under the root
                _, created_count = _import_file_nodes(
                    file_data, parts, None, source_key,
                    node_created_at, privacy_level, ai_usage)

                nodes_created += created_count
                threads_created += 1

            thread_count = threads_created

        nodes_updated = _apply_settings_to_skipped(
            skipped_alive_ids, privacy_level, ai_usage
        )
        nodes_skipped -= nodes_updated

        # Commit all nodes
        db.session.commit()

        # Determine if imported data predates the current profile cutoff
        profile_update_task_id = None
        if ai_usage in AI_ALLOWED:
            try:
                user_obj = User.query.get(current_user.id)
                if (user_obj and (user_obj.plan or "free")
                        in User.VOICE_MODE_PLANS):
                    earliest_ts = None
                    for f in files_sorted:
                        raw_ts = f.get('modified_at', '')
                        if raw_ts:
                            try:
                                ts = datetime.fromisoformat(raw_ts)
                                if earliest_ts is None or ts < earliest_ts:
                                    earliest_ts = ts
                            except (ValueError, TypeError):
                                pass

                    total_imported_tokens = sum(
                        approximate_token_count(f.get('content', ''))
                        for f in files_sorted
                    )
                    profile_update_task_id = (
                        _hand_off_profile_update_after_import(
                            user_obj, earliest_ts, total_imported_tokens))
            except Exception as e:
                current_app.logger.warning(
                    "Auto-trigger profile update failed: %s", describe_exception(e)
                )

        return jsonify({
            "message": "Import successful",
            "nodes_created": nodes_created,
            "thread_count": thread_count,
            "profile_update_task_id": profile_update_task_id,
            "created": nodes_created,
            "skipped": nodes_skipped,
            "restored": nodes_restored,
            "updated": nodes_updated,
        }), 201

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(
            "Error confirming import: %s", describe_exception(e))
        return jsonify({"error": "Failed to import files"}), 500


@import_bp.route("/import/twitter/analyze", methods=["POST"])
@login_required
def analyze_twitter_import():
    """
    Analyze a Twitter/X data export zip file.

    Expects multipart/form-data with a 'zip_file' field.

    The tweets themselves stay on the server: they are parsed once,
    streamed to a per-user stash file (backend/utils/twitter_archive.py)
    and referenced by ``import_token`` in the confirm step. Only counts
    go back to the browser, so the response size no longer scales with
    the archive.

    Returns:
        {
            "import_token": "...",
            "total_tweets": N, "original_count": N, "reply_count": N,
            "skipped_retweets": N,
            "total_tokens": N, "original_tokens": N, "total_size": N
        }
    """
    from backend.utils import twitter_archive as ta

    if 'zip_file' not in request.files:
        return jsonify({"error": "No zip_file provided"}), 400

    zip_file = request.files['zip_file']

    if zip_file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    if not zip_file.filename.lower().endswith('.zip'):
        return jsonify({"error": "File must be a .zip file"}), 400

    try:
        token, path = ta.stash_new(current_user.id)
    except OSError as e:
        current_app.logger.exception(
            "Twitter import stash failed: %s", describe_exception(e))
        return jsonify({"error": "Could not store the archive for import"}), 500

    try:
        # The zip itself is small (a few MB); BytesIO gives ZipFile the
        # seekable handle werkzeug's spooled upload doesn't.
        summary = ta.analyze_archive_to_stash(
            io.BytesIO(zip_file.read()), path
        )
    except ta.TwitterArchiveError as e:
        ta.stash_delete(path)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        ta.stash_delete(path)
        current_app.logger.exception(
            "Error analyzing Twitter import: %s", describe_exception(e))
        return jsonify({
            "error": "Failed to analyze Twitter export"
        }), 500

    summary["import_token"] = token
    return jsonify(summary), 200


def _load_conversation_files(conv_files, source_label):
    """
    Parse the uploaded conversation file(s) into one list, in upload
    order. A sharded export arrives as one part per shard, each holding
    a JSON array.

    Returns (conversations, size_bytes); the analyze step logs the size
    with its counts. Raises UnicodeDecodeError / json.JSONDecodeError for
    the caller to report.
    """
    raw_conversations = []
    size_bytes = 0
    for conv_file in conv_files:
        conversations_bytes = conv_file.read()
        size_bytes += len(conversations_bytes)
        raw_conversations.extend(
            json.loads(conversations_bytes.decode('utf-8')))
    return raw_conversations, size_bytes


def _role_counts(source, conversations):
    """Messages and tokens (approximate_token_count, the unit of
    total_tokens) by who wrote them: the user, or the assistant (an
    `llm` node on import). Counts only, for the logs and the stash
    header (Peter, 2026-10-20: learn the real split from real imports)."""
    role_field = _CONVERSATION_SOURCES[source]["role_field"]
    counts = {"user_msgs": 0, "assistant_msgs": 0,
              "user_tokens": 0, "assistant_tokens": 0}
    for conv in conversations:
        for msg in conv.get('messages', []):
            who = ("assistant" if msg.get(role_field) == 'assistant'
                   else "user")
            counts[f"{who}_msgs"] += 1
            counts[f"{who}_tokens"] += msg.get(
                'token_count', approximate_token_count(msg.get('text', '')))
    return counts


def _log_analyzed_import(label, size_bytes, n_files, conversations, roles):
    """The analyze step's one log line: sizes and counts, never content
    or titles."""
    current_app.logger.info(
        "%s import: size_bytes=%d files=%d conversations=%d user_msgs=%d "
        "assistant_msgs=%d user_tokens=%d assistant_tokens=%d user_id=%s",
        label, size_bytes, n_files, len(conversations), roles["user_msgs"],
        roles["assistant_msgs"], roles["user_tokens"],
        roles["assistant_tokens"], getattr(current_user, 'id', None))


def _plural(n, one, many):
    return one if n == 1 else many


# Key names that may appear in a Sentry event. Anything else could be user
# content (a format keyed by title or id), so it is only counted.
_CHATGPT_KNOWN_KEYS = frozenset({
    'id', 'conversation_id', 'title', 'create_time', 'update_time',
    'current_node', 'mapping', 'conversation_template_id',
    'default_model_slug', 'is_archived', 'is_starred', 'is_do_not_remember',
    'is_read_only', 'is_study_mode', 'memory_scope', 'pinned_time',
    'plugin_ids', 'voice', 'gizmo_id', 'gizmo_type', 'moderation_results',
    'safe_urls', 'async_status', 'disabled_tool_ids', 'sugar_item_id',
    'chats', 'messages', 'members', 'name', 'items', 'conversations',
    'sections',
})


def _chatgpt_file_label(name):
    """Fixed label for an uploaded file name (client-controlled, so the
    raw name never goes to Sentry or the logs)."""
    if name == 'conversations.json':
        return 'conversations'
    if re.fullmatch(r'conversations-\d+\.json', name):
        return 'conversations-shard'
    if name == 'sectioned_conversations.json':
        return 'sectioned_conversations'
    if name == 'group_chats.json':
        return 'group_chats'
    return 'other'


def _chatgpt_key_summary(obj):
    """For a dict: its allowlisted key names plus a count of the others
    (never their text). For anything else: None."""
    if not isinstance(obj, dict):
        return None
    known = sorted(k for k in obj if k in _CHATGPT_KNOWN_KEYS)
    return {"keys": known, "unknown_key_count": len(obj) - len(known)}


def _report_unreadable_chatgpt_items(problems):
    """One Sentry warning per request for unreadable ChatGPT export
    items. Counts and key names only: never a value, title or id from
    the export. A fixed message and fingerprint group the events into
    one issue; a no-op when Sentry is not initialised."""
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.fingerprint = ["chatgpt-import-unreadable-items"]
            scope.set_context("chatgpt_import", {"files": problems})
            sentry_sdk.capture_message(
                "ChatGPT import: unreadable export items",
                level="warning")
    except Exception:  # reporting must not break the import
        current_app.logger.exception(
            "Sentry report of unreadable ChatGPT items failed")


def _load_chatgpt_parts(conv_files):
    """
    Parse the uploaded ChatGPT parts (told apart by basename) into
    ([(conversation, messages), ...], notices). A conversation whose
    linearization raises is counted as unreadable.

    - group_chats.json is not imported; its chats are counted for a notice.
    - Every other part: each dict item with a dict "mapping" is a
      conversation (so a conversation-shaped item in
      sectioned_conversations.json imports like the rest); any other item,
      or a top level that is not a list, is counted as unreadable.
    - Conversations repeated across parts are kept once (by "id", else
      "conversation_id"; first one wins).

    Also returns the uploaded size (the analyze step logs it). Logs
    counts only, never content. Raises UnicodeDecodeError /
    json.JSONDecodeError for the caller to report.
    """
    conversations = []  # (conversation, linearized messages) pairs
    seen_ids = set()
    unreadable = 0
    group_chats = 0
    size_bytes = 0
    problems = []
    for conv_file in conv_files:
        name = os.path.basename(conv_file.filename or '')
        label = _chatgpt_file_label(name)
        data = conv_file.read()
        size_bytes += len(data)
        parsed = json.loads(data.decode('utf-8'))

        if name == 'group_chats.json':
            chats = parsed.get('chats') if isinstance(parsed, dict) else None
            n = len(chats) if isinstance(chats, list) else 0
            group_chats += n
            if n or not isinstance(chats, list):
                current_app.logger.warning(
                    "ChatGPT import: file=%s group_chats=%d top_level=%s",
                    label, n, type(parsed).__name__)
            continue

        if not isinstance(parsed, list):
            unreadable += 1
            current_app.logger.warning(
                "ChatGPT import: file=%s top_level=%s unreadable=1",
                label, type(parsed).__name__)
            problems.append({
                "file": label, "top_level": type(parsed).__name__,
                "items": None, "unreadable": 1,
                "unreadable_types": {type(parsed).__name__: 1},
                "first_unreadable": _chatgpt_key_summary(parsed),
            })
            continue

        part_unreadable = 0
        first_unreadable = None
        unreadable_types = {}
        for item in parsed:
            if not (isinstance(item, dict)
                    and isinstance(item.get('mapping'), dict)):
                if part_unreadable == 0:
                    first_unreadable = _chatgpt_key_summary(item)
                tname = type(item).__name__
                unreadable_types[tname] = unreadable_types.get(tname, 0) + 1
                part_unreadable += 1
                continue
            conv_id = item.get('id') or item.get('conversation_id')
            if isinstance(conv_id, str):  # other types count as absent
                if conv_id in seen_ids:
                    continue
                seen_ids.add(conv_id)
            if not item['mapping']:
                continue
            try:
                messages = _linearize_chatgpt_messages(
                    item['mapping'], item.get('current_node'))
            except (TypeError, AttributeError, KeyError, ValueError) as e:
                # One malformed conversation must not fail the upload.
                # Type names only, never values.
                if part_unreadable == 0:
                    first_unreadable = {"error": type(e).__name__}
                tname = "conversation:" + type(e).__name__
                unreadable_types[tname] = unreadable_types.get(tname, 0) + 1
                part_unreadable += 1
                continue
            if messages:
                conversations.append((item, messages))
        unreadable += part_unreadable
        if part_unreadable:
            current_app.logger.warning(
                "ChatGPT import: file=%s items=%d unreadable=%d "
                "top_level=list", label, len(parsed), part_unreadable)
            problems.append({
                "file": label, "top_level": "list", "items": len(parsed),
                "unreadable": part_unreadable,
                "unreadable_types": unreadable_types,
                "first_unreadable": first_unreadable,
            })

    if problems:
        _report_unreadable_chatgpt_items(problems)

    notices = []
    if unreadable:
        notices.append(
            f"{unreadable} {_plural(unreadable, 'item', 'items')} in your "
            f"export {_plural(unreadable, 'is', 'are')} in a format Loore "
            f"can't read yet, so {_plural(unreadable, 'it', 'they')} "
            f"won't be imported.")
    if group_chats:
        notices.append(
            f"Group chats aren't imported yet ({group_chats} in your "
            f"export).")
    return conversations, notices, size_bytes


@import_bp.route("/import/claude/analyze", methods=["POST"])
@login_required
def analyze_claude_import():
    """
    Analyze a Claude conversations.json file.

    The client extracts conversations.json from the Claude export zip
    in the browser and uploads only that file, so the full (potentially
    large) export never has to traverse the network.

    Expects:
        - multipart/form-data with 'conversations_file' field containing
          the conversations.json extracted from a Claude data export

    The analyzed conversations stay on the server, encrypted
    (utils/conversation_stash.py); the confirm step names them by
    ``import_token``. Each stashed conversation is
    {"name", "created_at", "messages": [{"text", "sender",
    "created_at", "uuid", "token_count"}], "message_count",
    "token_count"}.

    Returns:
        {
            "import_token": "...",
            "total_conversations": N,
            "total_messages": N,
            "total_tokens": N,
            "total_size": N
        }
    """
    if 'conversations_file' not in request.files:
        return jsonify({"error": "No conversations_file provided"}), 400

    conv_files = request.files.getlist('conversations_file')

    if any(f.filename == '' for f in conv_files):
        return jsonify({"error": "No file selected"}), 400

    try:
        raw_conversations, size_bytes = _load_conversation_files(
            conv_files, "Claude")

        conversations = []
        total_messages = 0
        total_tokens = 0
        total_size = 0

        for conv in raw_conversations:
            chat_messages = conv.get('chat_messages', [])
            messages = []

            for msg in chat_messages:
                text = (msg.get('text') or '').strip()
                if not text:
                    continue

                sender = msg.get('sender', 'human')
                created_at = msg.get('created_at', '')
                token_count = approximate_token_count(text)

                messages.append({
                    "text": text,
                    "sender": sender,
                    "created_at": created_at,
                    "token_count": token_count,
                    # Stable per-message id from the Claude export.
                    # Survives analyze->confirm so re-imports dedup on
                    # the original message identity (rename-proof).
                    "uuid": msg.get('uuid', ''),
                })

                total_tokens += token_count
                total_size += len(text.encode('utf-8'))

            if not messages:
                continue

            conv_token_count = sum(m['token_count'] for m in messages)

            conversations.append({
                "name": conv.get('name', ''),
                "created_at": conv.get('created_at', ''),
                "messages": messages,
                "message_count": len(messages),
                "token_count": conv_token_count
            })

            total_messages += len(messages)

        roles = _role_counts("claude", conversations)
        _log_analyzed_import("Claude", size_bytes, len(conv_files),
                             conversations, roles)
        if not conversations:
            return jsonify({
                "error": "No conversations with messages found in the "
                         "export."
            }), 400

        del raw_conversations  # the raw export; only the analysis is kept
        token = _stash_conversations(
            current_user.id, "claude", conversations, total_messages,
            roles=roles)

        return jsonify({
            "import_token": token,
            "total_conversations": len(conversations),
            "total_messages": total_messages,
            "total_tokens": total_tokens,
            "total_size": total_size
        }), 200

    except UnicodeDecodeError:
        return jsonify({
            "error": "conversations.json is not valid UTF-8 text"
        }), 400
    except json.JSONDecodeError:
        return jsonify({
            "error": "conversations.json is not valid JSON"
        }), 400
    except Exception as e:
        current_app.logger.exception(
            "Error analyzing Claude import: %s", describe_exception(e))
        return jsonify({
            "error": "Failed to analyze Claude export"
        }), 500


@import_bp.route("/import/claude/confirm", methods=["POST"])
@login_required
def confirm_claude_import():
    """Start importing an analyzed Claude export in the background.

    Request body:
        {
            "import_token": "...",            # from /import/claude/analyze
            "privacy_level": "private",
            "ai_usage": "none",
            "on_deleted": "restore" | "skip"  # after a 409
        }

    Returns 202 {"task_id": "...", "status": "queued", "total": N}; poll
    GET /import/status/<task_id>. See _confirm_conversation_import.
    """
    return _confirm_conversation_import("claude")


def _parse_tweet_ts(raw_ts):
    """Export timestamp -> naive UTC datetime, or None."""
    if not raw_ts:
        return None
    try:
        return datetime.strptime(
            raw_ts, "%a %b %d %H:%M:%S %z %Y"
        ).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def create_twitter_nodes(user_id, rows, total, import_type, include_replies,
                         privacy_level, ai_usage, on_deleted, *, provenance,
                         batch_size=500, on_progress=None):
    """Create nodes for analyzed tweets. Runs inside the Celery import
    task (backend/tasks/imports.py); ``rows`` is any iterable of compact
    tweet rows sorted by created_at (the analyze step sorts the stash).

    ``provenance`` (one of PROVENANCES) says how these tweets reached
    Loore — the user's own upload, or a Loore-side fetch from the
    Community Archive or the X API. Required, not defaulted: a caller
    that forgets it would leave rows nobody can later classify.

    Commits every ``batch_size`` created nodes so a 60k-tweet archive
    never holds one giant transaction, and reports ``done`` rows through
    ``on_progress`` after each batch. Dedup/restore/skip semantics are the
    same as the other importers (see _load_source_key_index).

    Returns the dict the frontend renders as the import result.
    """
    if import_type not in ('single_thread', 'separate_nodes'):
        raise ValueError("Invalid import_type")
    if provenance not in PROVENANCES:
        raise ValueError(f"Unknown provenance {provenance!r}")

    nodes_created = 0
    nodes_skipped = 0
    nodes_restored = 0
    nodes_empty = 0
    skipped_alive_ids = []
    key_index, deleted_keys = _load_source_key_index(user_id)
    parent_id = None
    earliest_ts = None
    total_imported_tokens = 0
    since_commit = 0
    done = 0

    for tweet_data in rows:
        done += 1
        if not include_replies and tweet_data.get('is_reply', False):
            continue

        content = tweet_data.get('full_text', '')
        if not content.strip():
            # A tweet with no text (a media-only post, whose text in the
            # export is empty) would become a blank node: nothing for the
            # profile to read, a blank card in the Log, and a row no
            # encryption pass can ever touch — set_content("") stores the
            # empty string as-is. 39 of these reached prod before this
            # guard, across 8 accounts (#317).
            nodes_empty += 1
            continue

        token_count = tweet_data.get(
            'token_count', approximate_token_count(content)
        )
        total_imported_tokens += token_count
        tweet_created_at = _parse_tweet_ts(tweet_data.get('created_at', ''))
        if tweet_created_at and (earliest_ts is None
                                 or tweet_created_at < earliest_ts):
            earliest_ts = tweet_created_at

        source_key = _tweet_source_key(tweet_data)
        if source_key in key_index:
            if source_key in deleted_keys and on_deleted == 'restore':
                _restore_node(
                    key_index[source_key], content,
                    privacy_level, ai_usage, token_count,
                    provenance=provenance,
                )
                deleted_keys.discard(source_key)
                nodes_restored += 1
            else:
                nodes_skipped += 1
                if source_key not in deleted_keys:
                    skipped_alive_ids.append(key_index[source_key])
            if import_type == 'single_thread':
                # Chain the next new tweet onto the existing copy so
                # partially-skipped imports don't orphan new nodes.
                parent_id = key_index[source_key]
            continue

        node = Node(
            user_id=user_id,
            human_owner_id=user_id,
            parent_id=parent_id if import_type == 'single_thread' else None,
            node_type="user",
            token_count=token_count,
            privacy_level=privacy_level,
            ai_usage=ai_usage,
            source_key=source_key,
            origin="twitter",
            provenance=provenance,
        )
        node.set_content(content)
        if tweet_created_at:
            node.created_at = tweet_created_at
        db.session.add(node)

        if import_type == 'single_thread':
            db.session.flush()
            key_index[source_key] = node.id
            parent_id = node.id
        else:
            key_index[source_key] = None  # id not needed: no chaining

        nodes_created += 1
        since_commit += 1
        if since_commit >= batch_size:
            db.session.commit()
            since_commit = 0
            if on_progress:
                on_progress(done)

    nodes_updated = _apply_settings_to_skipped(
        skipped_alive_ids, privacy_level, ai_usage
    )
    nodes_skipped -= nodes_updated
    db.session.commit()
    if on_progress:
        on_progress(total)

    thread_count = 1 if import_type == 'single_thread' else nodes_created

    profile_update_task_id = None
    if ai_usage in AI_ALLOWED and nodes_created + nodes_restored > 0:
        profile_update_task_id = _maybe_update_profile_after_import(
            user_id, earliest_ts, total_imported_tokens
        )

    return {
        "message": "Import successful",
        "nodes_created": nodes_created,
        "thread_count": thread_count,
        "profile_update_task_id": profile_update_task_id,
        "created": nodes_created,
        "skipped": nodes_skipped,
        "empty": nodes_empty,
        "restored": nodes_restored,
        "updated": nodes_updated,
    }


def _maybe_update_profile_after_import(user_id, earliest_ts, imported_tokens):
    """Twitter path: same hand-off as every other importer. Best-effort."""
    try:
        user_obj = User.query.get(user_id)
        if not user_obj or (user_obj.plan or "free") not in User.VOICE_MODE_PLANS:
            return None
        return _hand_off_profile_update_after_import(
            user_obj, earliest_ts, imported_tokens)
    except Exception as e:
        current_app.logger.warning(
            "Auto-trigger profile update failed: %s", describe_exception(e)
        )
    return None


def _hand_off_profile_update_after_import(user_obj, earliest_ts, imported_tokens):
    """After an import, invalidate only the profile versions the imported
    data touches and regenerate the chain from there to the end, on the
    pipeline the account is on. Shared by every importer (Twitter,
    ChatGPT, Claude, markdown).

    - Data older than a version's cutoff invalidates that version
      (``revert_profile_for_import``): the latest still-valid version
      becomes the tip again and the chain continues from it now. Older
      than the whole chain: a from-scratch build now.
    - Otherwise (data newer than the tip, or no profile yet) nothing is
      invalidated; the import is new data like any other, and the
      account's seeding gate is evaluated right away instead of at the
      next hourly beat — the provisional ladder for a first or
      provisional profile, the 80k incremental gate for a full one — so
      an import that tips the account over starts its build immediately
      and one that does not starts nothing.

    Batch users (all of prod with ``PROFILE_USE_BATCH``) are seeded, and
    never sent to the synchronous full-price task; sync users dispatch it
    directly. Returns a task id or None.

    The refusal backoff (utils/refusal_backoff.py) does not apply here: a
    build the import triggers runs even if the profile job is waiting or
    stopped after cut-off outputs (#368). An import that triggers no build
    leaves the backoff as it is."""
    from backend.tasks.exports import (
        revert_profile_for_import, maybe_trigger_profile_update,
        maybe_trigger_incremental_profile_update)
    from backend.tasks.profile_batch import (
        use_batch_for_user, seed_profile_batch_for_user)

    tip = UserProfile.query.filter(
        UserProfile.user_id == user_obj.id,
        UserProfile.generation_type != 'integration'
    ).order_by(UserProfile.created_at.desc()).first()
    action = "none"
    if tip is not None:
        action, _version = revert_profile_for_import(user_obj.id, earliest_ts)
        db.session.commit()

    if use_batch_for_user(user_obj, current_app.config):
        current_app.logger.info(
            f"User {user_obj.id}: import ({imported_tokens} tokens) handed "
            f"to the batch profile pipeline ({action}); seeding now")
        # An import skips the refusal backoff (wait and stop): it is new
        # input, so a build it triggers runs now (voice review,
        # 2026-10-01). The seed's own gates still decide whether it does.
        seed_profile_batch_for_user.delay(user_obj.id, ignore_backoff=True)
        return None
    if action == "none":
        return maybe_trigger_incremental_profile_update(
            user_obj, ignore_backoff=True)
    return maybe_trigger_profile_update(
        user_obj.id, force_full_regen=(action == "full"))


def _tweet_source_key(tweet_data):
    """twitter:<id_str>; content hash when id_str is absent."""
    id_str = tweet_data.get('id_str', '')
    if id_str:
        return f"twitter:{id_str}"
    return _generic_source_key(
        "twitter",
        tweet_data.get('created_at', ''),
        tweet_data.get('full_text', ''),
    )


@import_bp.route("/import/twitter/confirm", methods=["POST"])
@login_required
def confirm_twitter_import():
    """
    Start creating nodes from an analyzed Twitter archive.

    Request body:
        {
            "import_token": "...",       # from /import/twitter/analyze
            "import_type": "single_thread" | "separate_nodes",
            "include_replies": true | false,
            "privacy_level": "private",
            "ai_usage": "none",
            "on_deleted": "restore" | "skip"   # after a 409
        }

    Node creation runs in a Celery task; poll
    GET /import/status/<task_id> for progress and the final result.

    Returns 202 {"task_id": "...", "total": N}.
    """
    from backend.utils import twitter_archive as ta

    data = request.get_json()

    if not data:
        return jsonify({"error": "No data provided"}), 400

    import_type = data.get('import_type', 'separate_nodes')
    include_replies = bool(data.get('include_replies', False))
    privacy_level = data.get('privacy_level', 'private')
    ai_usage = data.get('ai_usage', 'none')
    on_deleted = data.get('on_deleted')

    path = ta.stash_path(current_user.id, data.get('import_token'))
    if path is None or not path.exists():
        return jsonify({
            "error": "Import data expired — please upload the archive again."
        }), 400

    if import_type not in ['single_thread', 'separate_nodes']:
        return jsonify({
            "error": "Invalid import_type. "
                     "Must be 'single_thread' or 'separate_nodes'"
        }), 400

    _, deleted_keys = _load_source_key_index(current_user.id)
    conflict = _deleted_match_response(
        (_tweet_source_key(t) for t in ta.stash_iter(path)
         if include_replies or not t.get('is_reply', False)),
        deleted_keys, on_deleted,
    )
    if conflict:
        return conflict

    from backend.tasks.imports import import_twitter_archive
    from backend.utils.task_owner import record_task_owner
    task = import_twitter_archive.delay(current_user.id, data['import_token'], {
        "import_type": import_type,
        "include_replies": include_replies,
        "privacy_level": privacy_level,
        "ai_usage": ai_usage,
        "on_deleted": on_deleted,
    })
    # /import/status answers for this task id to this user only.
    record_task_owner(task.id, current_user.id, current_app.config)
    return jsonify({
        "task_id": task.id,
        "status": "queued",
        "total": ta.stash_count(path),
    }), 202


@import_bp.route("/import/status/<task_id>", methods=["GET"])
@login_required
def import_status(task_id):
    """Progress of a background import task.

    {"status": "queued" | "running" | "completed" | "failed",
     "done": N, "total": N, "result": {...} | null, "error": str | null}

    Answers for the caller's own import only: the task's meta carries
    the owning user_id while it runs and once it succeeds, and the
    confirm request recorded the owner (utils/task_owner), which also
    covers a failure. Anyone else's task id reads as "queued" forever.
    A failure says only text written for the user, else "Import failed".
    """
    from backend.celery_app import celery
    from backend.utils.task_owner import failure_text, task_owned_by

    task = celery.AsyncResult(task_id)
    info = task.info if isinstance(task.info, dict) else {}
    mine = (info.get("user_id") == current_user.id
            or task_owned_by(task_id, current_user.id, current_app.config))
    if not mine:
        return jsonify({"task_id": task_id, "status": "queued",
                        "done": 0, "total": None, "result": None,
                        "error": None}), 200

    if task.state == "PROGRESS":
        return jsonify({"task_id": task_id, "status": "running",
                        "done": info.get("done", 0),
                        "total": info.get("total"),
                        "result": None, "error": None}), 200
    if task.state == "SUCCESS":
        return jsonify({"task_id": task_id, "status": "completed",
                        "done": info.get("total"), "total": info.get("total"),
                        "result": {k: v for k, v in info.items() if k != "user_id"},
                        "error": None}), 200
    if task.state == "FAILURE":
        return jsonify({"task_id": task_id, "status": "failed",
                        "done": 0, "total": None, "result": None,
                        "error": failure_text(task.info, "Import failed")}), 200
    return jsonify({"task_id": task_id, "status": "queued",
                    "done": 0, "total": None, "result": None,
                    "error": None}), 200


def _str_parent(entry):
    parent = entry.get('parent')
    return parent if isinstance(parent, str) and parent else None


def _chatgpt_thread_ids(mapping, current_node=None):
    """
    Node ids of a ChatGPT conversation's main thread, root first.

    The main thread is the path from the root to ``current_node``: the
    branch the user last saw in ChatGPT, which after an edited message
    or a regenerated reply is not the first child. Without a usable
    current_node, walk from the root following the first child.
    """
    # A non-string current_node or parent is treated as absent.
    if not isinstance(current_node, str):
        current_node = None
    if current_node in mapping:
        path = []
        visited = set()
        node_id = current_node
        while node_id in mapping and node_id not in visited:
            visited.add(node_id)
            path.append(node_id)
            parent = mapping[node_id].get('parent')
            node_id = parent if isinstance(parent, str) else None
        return path[::-1]

    # Find the root node (no parent)
    root_id = None
    for node_id, entry in mapping.items():
        if not _str_parent(entry):
            root_id = node_id
            break

    if not root_id:
        return []

    # Exports since Oct 2026 drop each node's 'children' list; derive
    # it from the parent pointers, in mapping order.
    derived_children = {}
    for node_id, entry in mapping.items():
        if _str_parent(entry):
            derived_children.setdefault(entry['parent'], []).append(node_id)

    path = []
    current = root_id
    visited = set()

    while current and current not in visited:
        visited.add(current)
        entry = mapping.get(current)
        if not entry:
            break
        path.append(current)
        children = entry.get('children')
        if children is None:
            children = derived_children.get(current, [])
        current = children[0] if children else None

    return path


def _chatgpt_part_text(part):
    """Text of one message part: string parts, and the transcript of a
    voice-mode turn (an "audio_transcription" dict part), as plain text.
    Other dict parts (images, audio pointers) give ''. To mark spoken
    turns, change the transcription return value here."""
    if isinstance(part, str):
        return part
    if (isinstance(part, dict)
            and part.get('content_type') == 'audio_transcription'
            and isinstance(part.get('text'), str)):
        return part['text']
    return ''


def _linearize_chatgpt_messages(mapping, current_node=None):
    """
    Walk the ChatGPT conversation graph along its main thread (see
    _chatgpt_thread_ids).
    Returns a list of message dicts with text, role, created_at, model.
    """
    messages = []

    for current in _chatgpt_thread_ids(mapping, current_node):
        entry = mapping[current]

        msg = entry.get('message')
        if msg:
            role = msg.get('author', {}).get('role', '')
            if role in ('user', 'assistant'):
                parts = msg.get('content', {}).get('parts', [])
                text = '\n'.join(
                    _chatgpt_part_text(p) for p in parts
                    if _chatgpt_part_text(p)
                ).strip()
                if text:
                    create_time = msg.get('create_time')
                    model = (
                        msg.get('metadata', {}).get(
                            'model_slug', ''
                        )
                    )

                    msg_created_at = None
                    if create_time:
                        try:
                            msg_created_at = datetime.utcfromtimestamp(
                                float(create_time)
                            ).isoformat()
                        except (ValueError, TypeError, OSError):
                            pass

                    messages.append({
                        "text": text,
                        "role": role,
                        "created_at": msg_created_at or '',
                        "model": model,
                        # Stable per-message id from the export graph.
                        # Survives analyze->confirm so re-imports dedup
                        # on the original ChatGPT message identity.
                        "mapping_id": current,
                    })

    return messages


@import_bp.route("/import/chatgpt/analyze", methods=["POST"])
@login_required
def analyze_chatgpt_import():
    """
    Analyze a ChatGPT conversations.json file.

    The client extracts conversations.json from the ChatGPT export zip
    in the browser and uploads only that file, so the full (multi-GB)
    export with images/audio never has to traverse the network.

    Expects:
        - multipart/form-data with 'conversations_file' field containing
          the conversations.json extracted from a ChatGPT data export;
          exports since Oct 2026 split it into conversations-000.json,
          conversations-001.json, ..., sent as one part each

    The analyzed conversations stay on the server, encrypted
    (utils/conversation_stash.py); the confirm step names them by
    ``import_token``. Each stashed conversation is {"name",
    "created_at", "default_model", "messages": [{"text", "role",
    "created_at", "model", "mapping_id", "token_count"}],
    "message_count", "token_count"}.

    Returns:
        {
            "import_token": "...",
            "total_conversations": N,
            "total_messages": N,
            "total_tokens": N,
            "total_size": N,
            "notices": [...]
        }
    """
    if 'conversations_file' not in request.files:
        return jsonify({"error": "No conversations_file provided"}), 400

    conv_files = request.files.getlist('conversations_file')

    if any(f.filename == '' for f in conv_files):
        return jsonify({"error": "No file selected"}), 400

    try:
        raw_conversations, notices, size_bytes = _load_chatgpt_parts(
            conv_files)

        conversations = []
        total_messages = 0
        total_tokens = 0
        total_size = 0

        for conv, messages in raw_conversations:
            conv_token_count = 0
            for msg in messages:
                token_count = approximate_token_count(msg['text'])
                msg['token_count'] = token_count
                conv_token_count += token_count
                total_size += len(msg['text'].encode('utf-8'))

            conv_created_at = ''
            create_time = conv.get('create_time')
            if create_time:
                try:
                    conv_created_at = datetime.utcfromtimestamp(
                        float(create_time)
                    ).isoformat()
                except (ValueError, TypeError, OSError):
                    pass

            default_model = conv.get('default_model_slug', '')

            conversations.append({
                "name": conv.get('title', ''),
                "created_at": conv_created_at,
                "default_model": default_model,
                "messages": messages,
                "message_count": len(messages),
                "token_count": conv_token_count,
            })

            total_messages += len(messages)
            total_tokens += conv_token_count

        roles = _role_counts("chatgpt", conversations)
        _log_analyzed_import("ChatGPT", size_bytes, len(conv_files),
                             conversations, roles)
        if not conversations:
            error = "No conversations with messages found in the export."
            if notices:
                error = " ".join([error] + notices)
            return jsonify({"error": error, "notices": notices}), 400

        del raw_conversations  # the raw export; only the analysis is kept
        token = _stash_conversations(
            current_user.id, "chatgpt", conversations, total_messages,
            roles=roles)

        return jsonify({
            "import_token": token,
            "total_conversations": len(conversations),
            "total_messages": total_messages,
            "total_tokens": total_tokens,
            "total_size": total_size,
            "notices": notices,
        }), 200

    except UnicodeDecodeError:
        return jsonify({
            "error": "conversations.json is not valid UTF-8 text"
        }), 400
    except json.JSONDecodeError:
        return jsonify({
            "error": "conversations.json is not valid JSON"
        }), 400
    except Exception as e:
        current_app.logger.exception(
            "Error analyzing ChatGPT import: %s", describe_exception(e))
        return jsonify({
            "error": "Failed to analyze ChatGPT export"
        }), 500


@import_bp.route("/import/chatgpt/confirm", methods=["POST"])
@login_required
def confirm_chatgpt_import():
    """Start importing an analyzed ChatGPT export in the background.

    Request body:
        {
            "import_token": "...",            # from /import/chatgpt/analyze
            "privacy_level": "private",
            "ai_usage": "none",
            "on_deleted": "restore" | "skip"  # after a 409
        }

    Returns 202 {"task_id": "...", "status": "queued", "total": N}; poll
    GET /import/status/<task_id>. See _confirm_conversation_import.
    """
    return _confirm_conversation_import("chatgpt")


# ── ChatGPT / Claude: background import from the server-side stash ────────
#
# Analyze stashes the analyzed conversations (utils/conversation_stash.py)
# and returns a token; confirm checks the token and starts the Celery task
# backend.tasks.chat_imports.import_conversations, which runs
# run_conversation_import. The client polls /import/status/<task_id>, as
# for a Twitter import.

# HEURISTIC: messages per commit (same size as the Twitter import's
# chunks). Every message with text counts: a new node (each part of a
# split one), a skip, a restore. A commit happens at the first
# conversation boundary after this many, so a resumed run starts at a
# whole conversation, and a re-import that only skips or changes
# settings is checkpointed too. Each committed chunk gets a fresh data
# key: one KMS call per chunk.
CONVERSATION_BATCH_SIZE = 500
# HEURISTIC: the job reports progress at most this often (seconds).
CONVERSATION_PROGRESS_INTERVAL = 1.0

IMPORT_EXPIRED_MESSAGE = (
    "Import data expired — please upload the export again.")
IMPORT_OUTDATED_CLIENT_MESSAGE = (
    "This import needs the latest version of Loore. Reload the page (or "
    "update the app) and upload the export again.")
IMPORT_RUNNING_MESSAGE = (
    "Another import is still running. Wait until it finishes, then try "
    "again.")
IMPORT_SUPERSEDED_MESSAGE = (
    "This import was started again and continues there. Check your Log in "
    "a few minutes.")


def _parse_chatgpt_ts(raw_ts):
    return datetime.fromisoformat(raw_ts)


def _parse_claude_ts(raw_ts):
    return datetime.fromisoformat(
        raw_ts.replace('Z', '+00:00')).replace(tzinfo=None)


# What differs between the two chat importers; everything else is shared.
_CONVERSATION_SOURCES = {
    "chatgpt": {
        "llm_username": "chatgpt",
        "llm_twitter_id": "llm-chatgpt",
        "role_field": "role",
        "default_role": "user",
        "msg_key": _chatgpt_msg_key,
        "parse_ts": _parse_chatgpt_ts,
        "origin": "chatgpt",
        # The model slug of the reply, else the platform name.
        "llm_model": lambda msg: msg.get('model', '') or "chatgpt",
    },
    "claude": {
        "llm_username": "claude-web",
        "llm_twitter_id": "llm-claude-web",
        "role_field": "sender",
        "default_role": "human",
        "msg_key": _claude_msg_key,
        "parse_ts": _parse_claude_ts,
        "origin": "claude",
        "llm_model": lambda msg: "claude-web",
    },
}


def _stash_conversations(user_id, source, conversations, total_messages,
                         roles=None):
    """Sort analyzed conversations into import order (oldest first, as
    the confirm step always did) and stash them, with the per-role
    counts (_role_counts) in the header; returns the token."""
    from backend.utils import conversation_stash as cs
    conversations = sorted(conversations,
                           key=lambda c: c.get('created_at', ''))
    msg_key = _CONVERSATION_SOURCES[source]["msg_key"]
    keys = [msg_key(m) for conv in conversations
            for m in conv.get('messages', []) if m.get('text')]
    if roles is None:
        roles = _role_counts(source, conversations)
    return cs.write_stash(user_id, source, conversations, keys,
                          total_messages, roles=roles)


def _load_deleted_source_keys(user_id):
    """The source keys of this user's soft-deleted imported nodes (the
    deleted half of _load_source_key_index, for the confirm check)."""
    rows = db.session.query(Node.source_key).filter(
        Node.human_owner_id == user_id,
        Node.source_key.isnot(None),
        Node.deleted_at.isnot(None),
    )
    return {key for (key,) in rows}


def _dispatch_conversation_import(user_id, token, task_id, options):
    """Queue the import task under *task_id* (tests run it inline)."""
    from backend.tasks.chat_imports import import_conversations
    import_conversations.apply_async(
        args=(user_id, token, options), task_id=task_id)


def _running_import_response(lock, token, total):
    """A confirm while this user's conversation import runs: the same
    import answers with the running job's id (a second press, a retry
    after a lost connection); a different one is refused."""
    if lock.get("token") == token:
        return jsonify({
            "task_id": lock["task_id"], "status": "running",
            "total": total, "already_running": True,
        }), 202
    return jsonify({
        "error": IMPORT_RUNNING_MESSAGE, "code": "import_running",
        "task_id": lock["task_id"],
    }), 409


def _confirm_conversation_import(source):
    """Confirm for the ChatGPT and Claude importers.

    Checks the stash token, allows one running conversation import per
    user (a second confirm of the same import gets the running job's
    id), asks about previously deleted content (409, as before), then
    queues the job. Returns 202 {"task_id", "status", "total"}, where
    total counts messages.
    """
    import uuid
    from backend.utils import conversation_stash as cs
    from backend.utils.task_owner import record_task_owner

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400

    token = data.get('import_token')
    if not token:
        # A page or app from before the background import posts the
        # conversations themselves.
        return jsonify({"error": IMPORT_OUTDATED_CLIENT_MESSAGE}), 400

    privacy_level = data.get('privacy_level', 'private')
    ai_usage = data.get('ai_usage', 'none')
    on_deleted = data.get('on_deleted')

    path = cs.stash_path(current_user.id, token)
    try:
        header = cs.read_header(path) if path and cs.is_fresh(path) else None
    except (OSError, cs.StashError):
        header = None
    if header is None or header.get("source") != source:
        return jsonify({"error": IMPORT_EXPIRED_MESSAGE}), 400
    total = header.get("messages", 0)

    running = cs.running_job(current_user.id)
    if running:
        return _running_import_response(running, token, total)

    try:
        conflict = _deleted_match_response(
            cs.read_keys(path), _load_deleted_source_keys(current_user.id),
            on_deleted,
        )
    except (OSError, cs.StashError):
        return jsonify({"error": IMPORT_EXPIRED_MESSAGE}), 400
    if conflict:
        return conflict

    task_id = str(uuid.uuid4())
    acquired, holder = cs.acquire_lock(current_user.id, task_id, token, source)
    if not acquired:
        if holder:  # another confirm got there first
            return _running_import_response(holder, token, total)
        return jsonify({"error": IMPORT_RUNNING_MESSAGE}), 409
    options = {
        "privacy_level": privacy_level,
        "ai_usage": ai_usage,
        "on_deleted": on_deleted,
    }
    try:
        _dispatch_conversation_import(current_user.id, token, task_id, options)
    except Exception as e:
        cs.release_lock(current_user.id, task_id)
        current_app.logger.exception(
            "Could not queue the %s import: %s", source, describe_exception(e))
        return jsonify({
            "error": "Could not start the import. Please try again."
        }), 500
    # /import/status answers for this task id to this user only.
    record_task_owner(task_id, current_user.id, current_app.config)
    return jsonify({"task_id": task_id, "status": "queued",
                    "total": total}), 202


class _ChunkDataKey:
    """One data key (encryption.new_dek) per committed chunk of nodes:
    one KMS call per chunk instead of one per node. Made on first use,
    so a public import, or a chunk of only skipped messages, makes
    none."""
    _UNSET = object()

    def __init__(self, privacy_level):
        self._public = privacy_level == "public"
        self._dek = self._UNSET

    def get(self):
        if self._public:
            return None
        if self._dek is self._UNSET:
            from backend.utils.encryption import new_dek
            self._dek = new_dek()  # None while encryption is off
        return self._dek

    def rotate(self):
        self._dek = self._UNSET


def _new_conversation_state(options):
    return {
        "options": options,
        "next_conv": 0,   # stash index of the next conversation to import
        "done": 0,        # messages processed (the progress count)
        "created": 0,
        "skipped": 0,     # includes the updated ones until the result
        "restored": 0,
        "updated": 0,
        "thread_count": 0,
        # For the profile hand-off, over every message of the import.
        "earliest_ts": None,
        "imported_tokens": 0,
    }


def create_conversation_nodes(user_id, source, conversations, privacy_level,
                              ai_usage, on_deleted, *, state=None,
                              batch_size=CONVERSATION_BATCH_SIZE,
                              on_commit=None, on_progress=None,
                              progress_interval=CONVERSATION_PROGRESS_INTERVAL):
    """Create the nodes of analyzed ChatGPT or Claude conversations: each
    conversation one thread, its messages chained in order. Runs in the
    background job (run_conversation_import); ``conversations`` yields
    (stash index, conversation) in import order.

    The rules are the confirm step's from before the background import:
    dedup on the per-message source key (a re-import skips what exists
    and chains new messages onto the existing copy), restore or skip
    soft-deleted matches per ``on_deleted``, and give already-imported
    nodes this import's privacy/AI-usage settings (``updated``).

    Commits at the first conversation boundary after ``batch_size``
    processed messages (new nodes, skips, restores) and then calls
    ``on_commit(state)``: ``state`` holds the next
    conversation and the counts so far, so a run that stops later can
    be continued from it. Calls ``on_progress(messages_done)`` at most
    every ``progress_interval`` seconds. Returns the final state.
    """
    import time
    spec = _CONVERSATION_SOURCES[source]
    state = dict(state) if state else _new_conversation_state(None)
    earliest_ts = (datetime.fromisoformat(state["earliest_ts"])
                   if state.get("earliest_ts") else None)

    key_index, deleted_keys = _load_source_key_index(user_id)

    # Get or create the synthetic user for the assistant's nodes
    llm_user = User.query.filter_by(username=spec["llm_username"]).first()
    if not llm_user:
        llm_user = User(
            twitter_id=spec["llm_twitter_id"], username=spec["llm_username"]
        )
        db.session.add(llm_user)
        db.session.flush()

    data_key = _ChunkDataKey(privacy_level)
    skipped_alive_ids = []
    since_commit = 0
    last_progress = time.monotonic()

    def commit():
        nonlocal skipped_alive_ids
        state["updated"] += _apply_settings_to_skipped(
            skipped_alive_ids, privacy_level, ai_usage,
            dek=data_key.get() if skipped_alive_ids else None)
        skipped_alive_ids = []
        state["earliest_ts"] = (earliest_ts.isoformat()
                                if earliest_ts else None)
        db.session.commit()
        data_key.rotate()
        if on_commit:
            on_commit(state)

    for index, conv in conversations:
        messages = conv.get('messages', [])
        conv_name = conv.get('name', '')

        # The profile hand-off reads every message of the import, the
        # skipped ones too (as the confirm step did).
        for msg in messages:
            raw_ts = msg.get('created_at', '')
            if raw_ts:
                try:
                    ts = spec["parse_ts"](raw_ts)
                    if earliest_ts is None or ts < earliest_ts:
                        earliest_ts = ts
                except (ValueError, TypeError, AttributeError):
                    pass
            if msg.get('text'):
                state["imported_tokens"] += approximate_token_count(
                    msg.get('text', ''))

        parent_id = None
        conv_started_new_thread = False

        for i, msg in enumerate(messages):
            text = msg.get('text', '')
            role = msg.get(spec["role_field"], spec["default_role"])

            if not text:
                continue

            # Prepend conversation name as H1 to first message
            if i == 0 and conv_name:
                node_content = f"# {conv_name}\n\n{text}"
            else:
                node_content = text

            is_assistant = role == 'assistant'

            source_key = spec["msg_key"](msg)
            if source_key in key_index:
                # Chain the next new message onto the existing copy
                # so overlap re-imports extend the original thread.
                if (source_key in deleted_keys
                        and on_deleted == 'restore'):
                    _restore_node(key_index[source_key], node_content,
                                  privacy_level, ai_usage,
                                  provenance=PROVENANCE_ARCHIVE_UPLOAD,
                                  dek=data_key.get())
                    deleted_keys.discard(source_key)
                    state["restored"] += 1
                else:
                    state["skipped"] += 1
                    if source_key not in deleted_keys:
                        skipped_alive_ids.append(key_index[source_key])
                parent_id = key_index[source_key]
                since_commit += 1  # a skip or restore is work to checkpoint
                continue

            # Parse original timestamp from the export
            msg_created_at = None
            raw_ts = msg.get('created_at', '')
            if raw_ts:
                try:
                    msg_created_at = spec["parse_ts"](raw_ts)
                except (ValueError, TypeError, AttributeError):
                    pass

            # A node created without a parent starts a new thread;
            # nodes chained onto an existing copy extend an old one.
            if parent_id is None:
                conv_started_new_thread = True

            tip, created_count = _add_imported_message_nodes(
                user_id=llm_user.id if is_assistant else user_id,
                human_owner_id=user_id,
                parent_id=parent_id,
                node_type="llm" if is_assistant else "user",
                llm_model=spec["llm_model"](msg) if is_assistant else None,
                node_content=node_content,
                privacy_level=privacy_level,
                ai_usage=ai_usage,
                source_key=source_key,
                msg_created_at=msg_created_at,
                origin=spec["origin"],
                dek=data_key.get(),
            )

            key_index[source_key] = tip.id
            parent_id = tip.id
            state["created"] += created_count
            since_commit += created_count

        if conv_started_new_thread:
            state["thread_count"] += 1
        state["done"] += len(messages)
        state["next_conv"] = index + 1

        if since_commit >= batch_size:
            commit()
            since_commit = 0
        if on_progress and (time.monotonic() - last_progress
                            >= progress_interval):
            last_progress = time.monotonic()
            on_progress(state["done"])

    commit()
    if on_progress:
        on_progress(state["done"])
    return state


def run_conversation_import(user_id, token, task_id, options, *,
                            on_progress=None, resumable=(),
                            batch_size=CONVERSATION_BATCH_SIZE):
    """The background ChatGPT/Claude import job: import the stash named
    by *token* for *user_id* with the confirm's *options*
    (privacy_level, ai_usage, on_deleted). Celery runs it
    (backend/tasks/chat_imports.py); tests call it directly.

    Resumable: the job state (utils/conversation_stash) records the next
    conversation and the counts after every commit, so a later run of
    the same import (the task run again after a deploy or a time limit,
    or the user confirming again after a failure) continues where the
    last commit left off; dedup keys skip anything a run created after
    its last saved state. A run with other settings than the saved
    state starts from the first conversation: dedup skips what exists
    and gives it the new settings, as any re-import does.

    *on_progress(done, total)* reports messages. Exceptions in
    *resumable* leave the lock in place (the task runs again); any
    other releases it and keeps the stash for a resume. On success the
    profile hand-off runs once (same rules as the confirm step had),
    the stash is deleted, and the result is returned (and kept in the
    state, so a repeated run of a finished job returns it again).

    The whole run holds the import's run guard: a second run of the
    same import at the same time raises conversation_stash.RunInProgress
    before it reads or changes anything (the task turns it into Ignore).
    """
    from backend.utils import conversation_stash as cs

    with cs.run_guard(user_id, token):
        return _run_conversation_import(
            user_id, token, task_id, options, on_progress=on_progress,
            resumable=resumable, batch_size=batch_size)


def _run_conversation_import(user_id, token, task_id, options, *,
                             on_progress, resumable, batch_size):
    """run_conversation_import, inside the run guard."""
    from backend.utils import conversation_stash as cs
    from backend.utils.task_owner import UserFacingTaskError

    state = cs.read_state(user_id, token)
    if state and state.get("finished"):
        return dict(state["result"])

    path = cs.stash_path(user_id, token)
    if path is None or not path.exists():
        cs.release_lock(user_id, task_id)
        raise UserFacingTaskError(IMPORT_EXPIRED_MESSAGE)
    header = cs.read_header(path)
    source = header["source"]
    total = header.get("messages", 0)

    if not cs.claim_lock(user_id, task_id, token, source):
        # This run waited in the queue until its lock went stale, and a
        # new confirm started the import again: that run does the work.
        raise UserFacingTaskError(IMPORT_SUPERSEDED_MESSAGE)
    cs.touch(path)

    if not state or state.get("options") != options:
        state = _new_conversation_state(options)

    def save(st):
        cs.write_state(user_id, token, dict(st, task_id=task_id))
        cs.touch(path)
        cs.heartbeat(user_id, task_id)

    def progress(done):
        cs.heartbeat(user_id, task_id)
        if on_progress:
            on_progress(done, total)

    progress(state["done"])
    try:
        state = create_conversation_nodes(
            user_id, source,
            cs.iter_conversations(path, start=state["next_conv"]),
            options.get("privacy_level", "private"),
            options.get("ai_usage", "none"),
            options.get("on_deleted"),
            state=state, batch_size=batch_size,
            on_commit=save, on_progress=progress,
        )
    except resumable:
        db.session.rollback()
        raise
    except Exception:
        db.session.rollback()
        cs.release_lock(user_id, task_id)
        raise

    profile_update_task_id = None
    if options.get("ai_usage", "none") in AI_ALLOWED:
        earliest_ts = (datetime.fromisoformat(state["earliest_ts"])
                       if state.get("earliest_ts") else None)
        profile_update_task_id = _maybe_update_profile_after_import(
            user_id, earliest_ts, state["imported_tokens"])

    result = {
        "message": "Import successful",
        "nodes_created": state["created"],
        "thread_count": state["thread_count"],
        "profile_update_task_id": profile_update_task_id,
        "created": state["created"],
        "skipped": state["skipped"] - state["updated"],
        "restored": state["restored"],
        "updated": state["updated"],
        "total": total,
    }
    roles = header.get("roles") or {}
    current_app.logger.info(
        "%s import job done: user_msgs=%s assistant_msgs=%s user_tokens=%s "
        "assistant_tokens=%s created=%d skipped=%d restored=%d updated=%d "
        "user_id=%s", "ChatGPT" if source == "chatgpt" else "Claude",
        roles.get("user_msgs"), roles.get("assistant_msgs"),
        roles.get("user_tokens"), roles.get("assistant_tokens"),
        result["created"], result["skipped"], result["restored"],
        result["updated"], user_id)
    cs.write_state(user_id, token,
                   {"finished": True, "result": result, "task_id": task_id})
    cs.delete_stash(user_id, token)
    cs.release_lock(user_id, task_id)
    return dict(result)
