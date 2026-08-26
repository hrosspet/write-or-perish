from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node, User, UserProfile
from backend.extensions import db
from backend.utils.privacy import AI_ALLOWED
from datetime import datetime, timedelta
import hashlib
import zipfile
import io
import json

import_bp = Blueprint("import_bp", __name__)

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
                                msg_created_at, origin):
    """Create the node(s) for one imported message, splitting content
    above NODE_CHAR_CAP into a serial parent→child chain.

    source_key lands on the chain TIP (last part): both the dedup index
    and the next message's parent resolve via source_key, so keeping it
    on the tip means re-imports skip the whole message and follow-ups
    chain after the full content — identical to a fresh import.

    Returns (tip_node, nodes_created_count).
    """
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
        )
        # Never pass content= to Node(): that writes the raw column and
        # bypasses KMS envelope encryption (#256). set_content() is the
        # only sanctioned way to store text.
        n.set_content(seg)
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
                  token_count=None):
    """Un-delete a soft-deleted imported node in place.

    Refills content from the archive — this also recovers tombstones
    whose content was already wiped by the cleanup task — and keeps the
    node id, so existing child links stay intact. Privacy/AI-usage are
    set to this import's choices, like any other (re)imported node.
    """
    node = Node.query.get(node_id)
    node.set_privacy_level(privacy_level)  # before set_content: decides encryption
    node.set_content(content)
    node.token_count = (
        token_count if token_count is not None
        else approximate_token_count(content)
    )
    node.ai_usage = ai_usage
    node.deleted_at = None


def _apply_settings_to_skipped(node_ids, privacy_level, ai_usage):
    """Apply this import's privacy/ai_usage to already-imported nodes.

    Re-importing an archive is also how users change their mind about
    import settings, so dedup-skipped (alive) nodes adopt the values
    chosen for this import instead of being a pure no-op. Returns the
    number of nodes whose settings actually changed; callers report it
    as ``updated`` and keep it disjoint from ``skipped``.
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
            node.set_privacy_level(privacy_level)
            node.ai_usage = ai_usage
            updated += 1
        db.session.flush()
    return updated


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

    # Validate it's a zip file
    if not zip_file.filename.lower().endswith('.zip'):
        return jsonify({"error": "File must be a .zip file"}), 400

    try:
        # Read zip file into memory
        zip_bytes = io.BytesIO(zip_file.read())

        files_data = []
        total_tokens = 0
        total_size = 0

        with zipfile.ZipFile(zip_bytes, 'r') as zip_ref:
            # Get list of all .md files in the zip
            md_files = [f for f in zip_ref.namelist()
                       if f.lower().endswith('.md') and not f.startswith('__MACOSX/')]

            if not md_files:
                return jsonify({"error": "No .md files found in the zip archive"}), 400

            for file_path in md_files:
                # Get file info
                zip_info = zip_ref.getinfo(file_path)

                # Skip directories
                if zip_info.is_dir():
                    continue

                # Read file content
                try:
                    content = zip_ref.read(file_path).decode('utf-8')
                except UnicodeDecodeError:
                    # Skip files that can't be decoded as UTF-8
                    current_app.logger.warning(f"Skipping {file_path}: not valid UTF-8")
                    continue

                # Extract just the filename from the path
                filename = file_path.split('/')[-1]
                filename_without_ext = filename.rsplit('.', 1)[0] if '.' in filename else filename

                # Get timestamps from zip metadata
                # ZipInfo date_time is a tuple: (year, month, day, hour, minute, second)
                dt_tuple = zip_info.date_time
                file_datetime = datetime(*dt_tuple)

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
            return jsonify({"error": "No valid .md files could be read from the zip archive"}), 400

        return jsonify({
            "files": files_data,
            "total_files": len(files_data),
            "total_tokens": total_tokens,
            "total_size": total_size
        }), 200

    except zipfile.BadZipFile:
        return jsonify({"error": "Invalid zip file"}), 400
    except Exception as e:
        current_app.logger.error(f"Error analyzing import: {str(e)}")
        return jsonify({"error": "Failed to analyze zip file", "details": str(e)}), 500


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
            "date_ordering": "modified" | "created"
        }

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

        def _file_source_key(filename, content):
            """sha256 of (filename, content) for markdown-zip dedup."""
            raw = f"{filename}\x00{content}"
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()

        conflict = _deleted_match_response(
            (_file_source_key(f.get('filename_without_ext', 'Untitled'),
                              f.get('content', ''))
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
                stripped_content = content.lstrip()
                if not stripped_content.startswith('# '):
                    node_content = f"# {filename}\n\n{content}"
                else:
                    node_content = content

                source_key = _file_source_key(filename, content)
                if source_key in key_index:
                    # Chain the next new file onto the existing copy so
                    # partially-skipped imports don't orphan new nodes.
                    if source_key in deleted_keys and on_deleted == 'restore':
                        _restore_node(key_index[source_key], node_content,
                                      privacy_level, ai_usage)
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
                tip, created_count = _add_imported_message_nodes(
                    user_id=current_user.id,
                    human_owner_id=current_user.id,
                    parent_id=parent_id,
                    node_type="user",
                    llm_model=None,
                    node_content=node_content,
                    privacy_level=privacy_level,
                    ai_usage=ai_usage,
                    source_key=source_key,
                    msg_created_at=node_created_at,
                    origin="markdown",
                )

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
                stripped_content = content.lstrip()
                if not stripped_content.startswith('# '):
                    node_content = f"# {filename}\n\n{content}"
                else:
                    node_content = content

                source_key = _file_source_key(filename, content)
                if source_key in key_index:
                    if source_key in deleted_keys and on_deleted == 'restore':
                        _restore_node(key_index[source_key], node_content,
                                      privacy_level, ai_usage)
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
                _, created_count = _add_imported_message_nodes(
                    user_id=current_user.id,
                    human_owner_id=current_user.id,
                    parent_id=None,
                    node_type="user",
                    llm_model=None,
                    node_content=node_content,
                    privacy_level=privacy_level,
                    ai_usage=ai_usage,
                    source_key=source_key,
                    msg_created_at=node_created_at,
                    origin="markdown",
                )

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
                    latest_profile = UserProfile.query.filter_by(
                        user_id=current_user.id
                    ).order_by(UserProfile.created_at.desc()).first()
                    cutoff = (latest_profile.source_data_cutoff
                              if latest_profile else None)

                    needs_full_regen = False
                    earliest_ts = None
                    if cutoff:
                        for f in files_sorted:
                            raw_ts = f.get('modified_at', '')
                            if raw_ts:
                                try:
                                    ts = datetime.fromisoformat(raw_ts)
                                    if ts < cutoff:
                                        needs_full_regen = True
                                    if (earliest_ts is None
                                            or ts < earliest_ts):
                                        earliest_ts = ts
                                except (ValueError, TypeError):
                                    pass

                    if needs_full_regen:
                        from backend.tasks.exports import (
                            revert_profile_for_import
                        )
                        revert_profile_for_import(
                            user_obj.id, earliest_ts
                        )
                        db.session.commit()

                    total_imported_tokens = sum(
                        approximate_token_count(f.get('content', ''))
                        for f in files_sorted
                    )
                    if total_imported_tokens >= 10000:
                        from backend.tasks.exports import (
                            maybe_trigger_profile_update
                        )
                        profile_update_task_id = (
                            maybe_trigger_profile_update(
                                current_user.id,
                                force_full_regen=needs_full_regen,
                            )
                        )
            except Exception as e:
                current_app.logger.warning(
                    f"Auto-trigger profile update failed: {e}"
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
        current_app.logger.error(f"Error confirming import: {str(e)}")
        return jsonify({"error": "Failed to import files", "details": str(e)}), 500


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
        current_app.logger.error(f"Twitter import stash failed: {e}")
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
        current_app.logger.error(
            f"Error analyzing Twitter import: {str(e)}"
        )
        return jsonify({
            "error": "Failed to analyze Twitter export",
            "details": str(e)
        }), 500

    summary["import_token"] = token
    return jsonify(summary), 200


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

    Returns:
        {
            "conversations": [
                {
                    "name": "...",
                    "created_at": "...",
                    "messages": [
                        {
                            "text": "...",
                            "sender": "human" | "assistant",
                            "created_at": "...",
                            "uuid": "..."
                        }
                    ],
                    "message_count": N,
                    "token_count": N
                }
            ],
            "total_conversations": N,
            "total_messages": N,
            "total_tokens": N,
            "total_size": N
        }
    """
    if 'conversations_file' not in request.files:
        return jsonify({"error": "No conversations_file provided"}), 400

    conv_file = request.files['conversations_file']

    if conv_file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    try:
        conversations_bytes = conv_file.read()
        current_app.logger.info(
            "Claude import: size_bytes=%d user_id=%s",
            len(conversations_bytes),
            getattr(current_user, 'id', None),
        )
        conversations_json = conversations_bytes.decode('utf-8')

        raw_conversations = json.loads(conversations_json)

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

        if not conversations:
            return jsonify({
                "error": "No conversations with messages found in the "
                         "export."
            }), 400

        return jsonify({
            "conversations": conversations,
            "total_conversations": len(conversations),
            "total_messages": total_messages,
            "total_tokens": total_tokens,
            "total_size": total_size
        }), 200

    except UnicodeDecodeError as e:
        return jsonify({
            "error": "conversations.json is not valid UTF-8",
            "details": str(e)
        }), 400
    except json.JSONDecodeError as e:
        return jsonify({
            "error": "Failed to parse conversations JSON",
            "details": str(e)
        }), 400
    except Exception as e:
        current_app.logger.error(
            f"Error analyzing Claude import: {str(e)}"
        )
        return jsonify({
            "error": "Failed to analyze Claude export",
            "details": str(e)
        }), 500


@import_bp.route("/import/claude/confirm", methods=["POST"])
@login_required
def confirm_claude_import():
    """
    Create nodes from analyzed Claude conversation data.
    Each conversation becomes a separate thread with messages chained
    sequentially.

    Request body:
        {
            "conversations": [...],
            "privacy_level": "private",
            "ai_usage": "none"
        }

    Returns:
        {
            "message": "Import successful",
            "nodes_created": N,
            "thread_count": N
        }
    """
    data = request.get_json()

    if not data:
        return jsonify({"error": "No data provided"}), 400

    conversations = data.get('conversations', [])
    privacy_level = data.get('privacy_level', 'private')
    ai_usage = data.get('ai_usage', 'none')

    if not conversations:
        return jsonify({"error": "No conversations provided"}), 400

    on_deleted = data.get('on_deleted')

    try:
        key_index, deleted_keys = _load_source_key_index(current_user.id)
        conflict = _deleted_match_response(
            (_claude_msg_key(m)
             for conv in conversations
             for m in conv.get('messages', [])
             if m.get('text')),
            deleted_keys, on_deleted,
        )
        if conflict:
            return conflict

        # Get or create the synthetic user for claude-web nodes
        llm_user = User.query.filter_by(username="claude-web").first()
        if not llm_user:
            llm_user = User(
                twitter_id="llm-claude-web", username="claude-web"
            )
            db.session.add(llm_user)
            db.session.flush()

        nodes_created = 0
        nodes_skipped = 0
        nodes_restored = 0
        thread_count = 0
        skipped_alive_ids = []

        # Sort conversations by created_at ascending
        conversations_sorted = sorted(
            conversations,
            key=lambda c: c.get('created_at', '')
        )

        for conv in conversations_sorted:
            messages = conv.get('messages', [])
            conv_name = conv.get('name', '')

            if not messages:
                continue

            parent_id = None
            conv_started_new_thread = False

            for i, msg in enumerate(messages):
                text = msg.get('text', '')
                sender = msg.get('sender', 'human')

                if not text:
                    continue

                # Prepend conversation name as H1 to first message
                if i == 0 and conv_name:
                    node_content = f"# {conv_name}\n\n{text}"
                else:
                    node_content = text

                is_assistant = sender == 'assistant'

                source_key = _claude_msg_key(msg)
                if source_key in key_index:
                    # Chain the next new message onto the existing copy
                    # so overlap re-imports extend the original thread.
                    if (source_key in deleted_keys
                            and on_deleted == 'restore'):
                        _restore_node(key_index[source_key], node_content,
                                      privacy_level, ai_usage)
                        deleted_keys.discard(source_key)
                        nodes_restored += 1
                    else:
                        nodes_skipped += 1
                        if source_key not in deleted_keys:
                            skipped_alive_ids.append(key_index[source_key])
                    parent_id = key_index[source_key]
                    continue

                # Parse original timestamp from Claude export
                msg_created_at = None
                raw_ts = msg.get('created_at', '')
                if raw_ts:
                    try:
                        raw_ts = raw_ts.replace('Z', '+00:00')
                        msg_created_at = datetime.fromisoformat(
                            raw_ts
                        ).replace(tzinfo=None)
                    except (ValueError, TypeError):
                        pass

                # A node created without a parent starts a new thread;
                # nodes chained onto an existing copy extend an old one.
                if parent_id is None:
                    conv_started_new_thread = True

                tip, created_count = _add_imported_message_nodes(
                    user_id=(
                        llm_user.id if is_assistant else current_user.id
                    ),
                    human_owner_id=current_user.id,
                    parent_id=parent_id,
                    node_type="llm" if is_assistant else "user",
                    llm_model="claude-web" if is_assistant else None,
                    node_content=node_content,
                    privacy_level=privacy_level,
                    ai_usage=ai_usage,
                    source_key=source_key,
                    msg_created_at=msg_created_at,
                    origin="claude",
                )

                key_index[source_key] = tip.id
                parent_id = tip.id
                nodes_created += created_count

            if conv_started_new_thread:
                thread_count += 1

        nodes_updated = _apply_settings_to_skipped(
            skipped_alive_ids, privacy_level, ai_usage
        )
        nodes_skipped -= nodes_updated

        db.session.commit()

        # Determine if imported data predates the current profile cutoff
        profile_update_task_id = None
        if ai_usage in AI_ALLOWED:
            try:
                user_obj = User.query.get(current_user.id)
                if (user_obj and (user_obj.plan or "free")
                        in User.VOICE_MODE_PLANS):
                    latest_profile = UserProfile.query.filter_by(
                        user_id=current_user.id
                    ).order_by(UserProfile.created_at.desc()).first()
                    cutoff = (latest_profile.source_data_cutoff
                              if latest_profile else None)

                    needs_full_regen = False
                    earliest_ts = None
                    if cutoff:
                        for conv in conversations_sorted:
                            for msg in conv.get('messages', []):
                                raw_ts = msg.get('created_at', '')
                                if raw_ts:
                                    try:
                                        raw_ts = raw_ts.replace(
                                            'Z', '+00:00'
                                        )
                                        ts = datetime.fromisoformat(
                                            raw_ts
                                        ).replace(tzinfo=None)
                                        if ts < cutoff:
                                            needs_full_regen = True
                                        if (earliest_ts is None
                                                or ts < earliest_ts):
                                            earliest_ts = ts
                                    except (ValueError, TypeError):
                                        pass

                    if needs_full_regen:
                        from backend.tasks.exports import (
                            revert_profile_for_import
                        )
                        revert_profile_for_import(
                            user_obj.id, earliest_ts
                        )
                        db.session.commit()

                    total_imported_tokens = sum(
                        approximate_token_count(msg.get('text', ''))
                        for conv in conversations_sorted
                        for msg in conv.get('messages', [])
                        if msg.get('text')
                    )
                    if total_imported_tokens >= 10000:
                        from backend.tasks.exports import (
                            maybe_trigger_profile_update
                        )
                        profile_update_task_id = (
                            maybe_trigger_profile_update(
                                current_user.id,
                                force_full_regen=needs_full_regen,
                            )
                        )
            except Exception as e:
                current_app.logger.warning(
                    f"Auto-trigger profile update failed: {e}"
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
        current_app.logger.error(
            f"Error confirming Claude import: {str(e)}"
        )
        return jsonify({
            "error": "Failed to import Claude conversations",
            "details": str(e)
        }), 500


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
                         privacy_level, ai_usage, on_deleted,
                         batch_size=500, on_progress=None):
    """Create nodes for analyzed tweets. Runs inside the Celery import
    task (backend/tasks/imports.py); ``rows`` is any iterable of compact
    tweet rows sorted by created_at (the analyze step sorts the stash).

    Commits every ``batch_size`` created nodes so a 60k-tweet archive
    never holds one giant transaction, and reports ``done`` rows through
    ``on_progress`` after each batch. Dedup/restore/skip semantics are the
    same as the other importers (see _load_source_key_index).

    Returns the dict the frontend renders as the import result.
    """
    if import_type not in ('single_thread', 'separate_nodes'):
        raise ValueError("Invalid import_type")

    nodes_created = 0
    nodes_skipped = 0
    nodes_restored = 0
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
                    privacy_level, ai_usage, token_count
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
        "restored": nodes_restored,
        "updated": nodes_updated,
    }


def _maybe_update_profile_after_import(user_id, earliest_ts, imported_tokens):
    """Revert the profile if imported data predates its cutoff, then
    trigger an update when enough new tokens landed. Best-effort."""
    try:
        user_obj = User.query.get(user_id)
        if not user_obj or (user_obj.plan or "free") not in User.VOICE_MODE_PLANS:
            return None
        latest_profile = UserProfile.query.filter_by(
            user_id=user_id
        ).order_by(UserProfile.created_at.desc()).first()
        cutoff = latest_profile.source_data_cutoff if latest_profile else None
        needs_full_regen = bool(cutoff and earliest_ts and earliest_ts < cutoff)
        if needs_full_regen:
            from backend.tasks.exports import revert_profile_for_import
            revert_profile_for_import(user_id, earliest_ts)
            db.session.commit()
        if imported_tokens >= 10000:
            from backend.tasks.exports import maybe_trigger_profile_update
            return maybe_trigger_profile_update(
                user_id, force_full_regen=needs_full_regen,
            )
    except Exception as e:
        current_app.logger.warning(
            f"Auto-trigger profile update failed: {e}"
        )
    return None


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
    task = import_twitter_archive.delay(current_user.id, data['import_token'], {
        "import_type": import_type,
        "include_replies": include_replies,
        "privacy_level": privacy_level,
        "ai_usage": ai_usage,
        "on_deleted": on_deleted,
    })
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

    Task meta carries the owning user_id; anyone else's task id reads
    as "queued" forever rather than leaking counts.
    """
    from backend.celery_app import celery

    task = celery.AsyncResult(task_id)
    info = task.info if isinstance(task.info, dict) else {}
    owner = info.get("user_id")
    if task.state in ("PROGRESS", "SUCCESS") and owner != current_user.id:
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
        err = task.info
        return jsonify({"task_id": task_id, "status": "failed",
                        "done": 0, "total": None, "result": None,
                        "error": str(err) if err else "Import failed"}), 200
    return jsonify({"task_id": task_id, "status": "queued",
                    "done": 0, "total": None, "result": None,
                    "error": None}), 200


def _linearize_chatgpt_messages(mapping):
    """
    Walk the ChatGPT conversation graph from root to leaf
    following the first child at each step (main thread).
    Returns a list of message dicts with text, role, created_at, model.
    """
    # Find the root node (no parent)
    root_id = None
    for node_id, entry in mapping.items():
        if not entry.get('parent'):
            root_id = node_id
            break

    if not root_id:
        return []

    messages = []
    current = root_id
    visited = set()

    while current and current not in visited:
        visited.add(current)
        entry = mapping.get(current)
        if not entry:
            break

        msg = entry.get('message')
        if msg:
            role = msg.get('author', {}).get('role', '')
            if role in ('user', 'assistant'):
                parts = msg.get('content', {}).get('parts', [])
                text = '\n'.join(
                    str(p) for p in parts if isinstance(p, str)
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

        children = entry.get('children', [])
        current = children[0] if children else None

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
          the conversations.json extracted from a ChatGPT data export

    Returns:
        {
            "conversations": [...],
            "total_conversations": N,
            "total_messages": N,
            "total_tokens": N,
            "total_size": N
        }
    """
    if 'conversations_file' not in request.files:
        return jsonify({"error": "No conversations_file provided"}), 400

    conv_file = request.files['conversations_file']

    if conv_file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    try:
        conversations_bytes = conv_file.read()
        current_app.logger.info(
            "ChatGPT import: size_bytes=%d user_id=%s",
            len(conversations_bytes),
            getattr(current_user, 'id', None),
        )
        conversations_json = conversations_bytes.decode('utf-8')

        raw_conversations = json.loads(conversations_json)

        conversations = []
        total_messages = 0
        total_tokens = 0
        total_size = 0

        for conv in raw_conversations:
            mapping = conv.get('mapping', {})
            if not mapping:
                continue

            messages = _linearize_chatgpt_messages(mapping)

            if not messages:
                continue

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

        if not conversations:
            return jsonify({
                "error": "No conversations with messages found in the "
                         "export."
            }), 400

        return jsonify({
            "conversations": conversations,
            "total_conversations": len(conversations),
            "total_messages": total_messages,
            "total_tokens": total_tokens,
            "total_size": total_size,
        }), 200

    except UnicodeDecodeError as e:
        return jsonify({
            "error": "conversations.json is not valid UTF-8",
            "details": str(e)
        }), 400
    except json.JSONDecodeError as e:
        return jsonify({
            "error": "Failed to parse conversations JSON",
            "details": str(e)
        }), 400
    except Exception as e:
        current_app.logger.error(
            f"Error analyzing ChatGPT import: {str(e)}"
        )
        return jsonify({
            "error": "Failed to analyze ChatGPT export",
            "details": str(e)
        }), 500


@import_bp.route("/import/chatgpt/confirm", methods=["POST"])
@login_required
def confirm_chatgpt_import():
    """
    Create nodes from analyzed ChatGPT conversation data.
    Each conversation becomes a separate thread with messages chained
    sequentially.

    Request body:
        {
            "conversations": [...],
            "privacy_level": "private",
            "ai_usage": "none"
        }

    Returns:
        {
            "message": "Import successful",
            "nodes_created": N,
            "thread_count": N
        }
    """
    data = request.get_json()

    if not data:
        return jsonify({"error": "No data provided"}), 400

    conversations = data.get('conversations', [])
    privacy_level = data.get('privacy_level', 'private')
    ai_usage = data.get('ai_usage', 'none')

    if not conversations:
        return jsonify({"error": "No conversations provided"}), 400

    on_deleted = data.get('on_deleted')

    try:
        key_index, deleted_keys = _load_source_key_index(current_user.id)
        conflict = _deleted_match_response(
            (_chatgpt_msg_key(m)
             for conv in conversations
             for m in conv.get('messages', [])
             if m.get('text')),
            deleted_keys, on_deleted,
        )
        if conflict:
            return conflict

        # Get or create the synthetic user for chatgpt nodes
        llm_user = User.query.filter_by(username="chatgpt").first()
        if not llm_user:
            llm_user = User(
                twitter_id="llm-chatgpt", username="chatgpt"
            )
            db.session.add(llm_user)
            db.session.flush()

        nodes_created = 0
        nodes_skipped = 0
        nodes_restored = 0
        thread_count = 0
        skipped_alive_ids = []

        # Sort conversations by created_at ascending
        conversations_sorted = sorted(
            conversations,
            key=lambda c: c.get('created_at', '')
        )

        for conv in conversations_sorted:
            messages = conv.get('messages', [])
            conv_name = conv.get('name', '')

            if not messages:
                continue

            parent_id = None
            conv_started_new_thread = False

            for i, msg in enumerate(messages):
                text = msg.get('text', '')
                role = msg.get('role', 'user')

                if not text:
                    continue

                # Prepend conversation name as H1 to first message
                if i == 0 and conv_name:
                    node_content = f"# {conv_name}\n\n{text}"
                else:
                    node_content = text

                is_assistant = role == 'assistant'

                source_key = _chatgpt_msg_key(msg)
                if source_key in key_index:
                    # Chain the next new message onto the existing copy
                    # so overlap re-imports extend the original thread.
                    if (source_key in deleted_keys
                            and on_deleted == 'restore'):
                        _restore_node(key_index[source_key], node_content,
                                      privacy_level, ai_usage)
                        deleted_keys.discard(source_key)
                        nodes_restored += 1
                    else:
                        nodes_skipped += 1
                        if source_key not in deleted_keys:
                            skipped_alive_ids.append(key_index[source_key])
                    parent_id = key_index[source_key]
                    continue

                # Parse original timestamp
                msg_created_at = None
                raw_ts = msg.get('created_at', '')
                if raw_ts:
                    try:
                        msg_created_at = datetime.fromisoformat(
                            raw_ts
                        )
                    except (ValueError, TypeError):
                        pass

                # Get model slug for assistant messages
                model_slug = msg.get('model', '') if is_assistant else None

                # A node created without a parent starts a new thread;
                # nodes chained onto an existing copy extend an old one.
                if parent_id is None:
                    conv_started_new_thread = True

                tip, created_count = _add_imported_message_nodes(
                    user_id=(
                        llm_user.id if is_assistant else current_user.id
                    ),
                    human_owner_id=current_user.id,
                    parent_id=parent_id,
                    node_type="llm" if is_assistant else "user",
                    llm_model=model_slug or (
                        "chatgpt" if is_assistant else None
                    ),
                    node_content=node_content,
                    privacy_level=privacy_level,
                    ai_usage=ai_usage,
                    source_key=source_key,
                    msg_created_at=msg_created_at,
                    origin="chatgpt",
                )

                key_index[source_key] = tip.id
                parent_id = tip.id
                nodes_created += created_count

            if conv_started_new_thread:
                thread_count += 1

        nodes_updated = _apply_settings_to_skipped(
            skipped_alive_ids, privacy_level, ai_usage
        )
        nodes_skipped -= nodes_updated

        db.session.commit()

        # Determine if imported data predates the current profile cutoff
        profile_update_task_id = None
        if ai_usage in AI_ALLOWED:
            try:
                user_obj = User.query.get(current_user.id)
                if (user_obj and (user_obj.plan or "free")
                        in User.VOICE_MODE_PLANS):
                    latest_profile = UserProfile.query.filter_by(
                        user_id=current_user.id
                    ).order_by(UserProfile.created_at.desc()).first()
                    cutoff = (latest_profile.source_data_cutoff
                              if latest_profile else None)

                    needs_full_regen = False
                    earliest_ts = None
                    if cutoff:
                        for conv in conversations_sorted:
                            for msg in conv.get('messages', []):
                                raw_ts = msg.get('created_at', '')
                                if raw_ts:
                                    try:
                                        ts = datetime.fromisoformat(
                                            raw_ts
                                        )
                                        if ts < cutoff:
                                            needs_full_regen = True
                                        if (earliest_ts is None
                                                or ts < earliest_ts):
                                            earliest_ts = ts
                                    except (ValueError, TypeError):
                                        pass

                    if needs_full_regen:
                        from backend.tasks.exports import (
                            revert_profile_for_import
                        )
                        revert_profile_for_import(
                            user_obj.id, earliest_ts
                        )
                        db.session.commit()

                    total_imported_tokens = sum(
                        approximate_token_count(msg.get('text', ''))
                        for conv in conversations_sorted
                        for msg in conv.get('messages', [])
                        if msg.get('text')
                    )
                    if total_imported_tokens >= 10000:
                        from backend.tasks.exports import (
                            maybe_trigger_profile_update
                        )
                        profile_update_task_id = (
                            maybe_trigger_profile_update(
                                current_user.id,
                                force_full_regen=needs_full_regen,
                            )
                        )
            except Exception as e:
                current_app.logger.warning(
                    f"Auto-trigger profile update failed: {e}"
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
        current_app.logger.error(
            f"Error confirming ChatGPT import: {str(e)}"
        )
        return jsonify({
            "error": "Failed to import ChatGPT conversations",
            "details": str(e)
        }), 500
