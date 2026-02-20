from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node, User
from backend.extensions import db
from datetime import datetime
import zipfile
import io
import json
import re
from werkzeug.utils import secure_filename

import_bp = Blueprint("import_bp", __name__)

def approximate_token_count(text):
    """
    Approximate token count for a text string.
    Uses a simple heuristic: ~4 characters per token.
    """
    return len(text) // 4


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
        thread_count = 0

        if import_type == 'single_thread':
            # Create a single thread with all files as sequential nodes
            parent_node = None

            for file_data in files_sorted:
                filename = file_data.get('filename_without_ext', 'Untitled')
                content = file_data.get('content', '')

                # Add filename as markdown headline only if content doesn't have H1
                stripped_content = content.lstrip()
                if not stripped_content.startswith('# '):
                    node_content = f"# {filename}\n\n{content}"
                else:
                    node_content = content

                # Create node
                node = Node(
                    user_id=current_user.id,
                    parent_id=parent_node.id if parent_node else None,
                    node_type="user",
                    content=node_content,
                    token_count=approximate_token_count(node_content),
                    privacy_level=privacy_level,
                    ai_usage=ai_usage
                )

                db.session.add(node)
                db.session.flush()  # Get the node ID

                parent_node = node
                nodes_created += 1

            thread_count = 1

        else:  # separate_nodes
            # Create separate top-level threads for each file
            for file_data in files_sorted:
                filename = file_data.get('filename_without_ext', 'Untitled')
                content = file_data.get('content', '')

                # Add filename as markdown headline only if content doesn't have H1
                stripped_content = content.lstrip()
                if not stripped_content.startswith('# '):
                    node_content = f"# {filename}\n\n{content}"
                else:
                    node_content = content

                # Create top-level node (parent_id=None)
                node = Node(
                    user_id=current_user.id,
                    parent_id=None,
                    node_type="user",
                    content=node_content,
                    token_count=approximate_token_count(node_content),
                    privacy_level=privacy_level,
                    ai_usage=ai_usage
                )

                db.session.add(node)
                nodes_created += 1

            thread_count = len(files_sorted)

        # Commit all nodes
        db.session.commit()

        # Auto-trigger profile update if enough tokens imported
        profile_update_task_id = None
        total_imported_tokens = sum(
            approximate_token_count(f.get('content', ''))
            for f in files_sorted
        )
        if (total_imported_tokens >= 10000
                and ai_usage in ('chat', 'train')):
            try:
                from backend.tasks.exports import (
                    maybe_trigger_profile_update
                )
                user_obj = User.query.get(current_user.id)
                if (user_obj and (user_obj.plan or "free")
                        in User.VOICE_MODE_PLANS):
                    profile_update_task_id = (
                        maybe_trigger_profile_update(current_user.id)
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

    Expects:
        - multipart/form-data with 'zip_file' field containing a Twitter data export

    Returns:
        {
            "tweets": [...],
            "total_tweets": N,
            "original_count": N,
            "reply_count": N,
            "skipped_retweets": N,
            "total_tokens": N,
            "total_size": N
        }
    """
    if 'zip_file' not in request.files:
        return jsonify({"error": "No zip_file provided"}), 400

    zip_file = request.files['zip_file']

    if zip_file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    if not zip_file.filename.lower().endswith('.zip'):
        return jsonify({"error": "File must be a .zip file"}), 400

    try:
        zip_bytes = io.BytesIO(zip_file.read())

        tweets_js_content = None

        with zipfile.ZipFile(zip_bytes, 'r') as zip_ref:
            # Find data/tweets.js — may be nested under a top-level folder
            for name in zip_ref.namelist():
                if name.endswith('data/tweets.js') or name == 'data/tweets.js':
                    tweets_js_content = zip_ref.read(name).decode('utf-8')
                    break

        if tweets_js_content is None:
            return jsonify({
                "error": "Could not find data/tweets.js in the zip archive. "
                         "Please upload the original Twitter/X data export."
            }), 400

        # Strip the JS variable assignment prefix to get valid JSON
        # Format: window.YTD.tweets.part0 = [...]
        json_match = re.search(r'=\s*(\[.*)', tweets_js_content, re.DOTALL)
        if not json_match:
            return jsonify({
                "error": "Could not parse tweets.js — unexpected format."
            }), 400

        raw_tweets = json.loads(json_match.group(1))

        tweets = []
        skipped_retweets = 0
        original_count = 0
        reply_count = 0
        total_tokens = 0
        total_size = 0

        for entry in raw_tweets:
            tweet = entry.get('tweet', entry)

            full_text = tweet.get('full_text', '')

            # Skip retweets
            if full_text.startswith('RT @'):
                skipped_retweets += 1
                continue

            id_str = tweet.get('id_str', '')
            created_at = tweet.get('created_at', '')
            favorite_count = int(tweet.get('favorite_count', 0))
            retweet_count = int(tweet.get('retweet_count', 0))
            in_reply_to_status_id_str = tweet.get(
                'in_reply_to_status_id_str', None
            )
            in_reply_to_screen_name = tweet.get(
                'in_reply_to_screen_name', None
            )

            is_reply = bool(in_reply_to_status_id_str)
            if is_reply:
                reply_count += 1
            else:
                original_count += 1

            token_count = approximate_token_count(full_text)
            total_tokens += token_count
            total_size += len(full_text.encode('utf-8'))

            tweets.append({
                "id_str": id_str,
                "full_text": full_text,
                "created_at": created_at,
                "is_reply": is_reply,
                "in_reply_to_screen_name": in_reply_to_screen_name,
                "favorite_count": favorite_count,
                "retweet_count": retweet_count,
                "token_count": token_count
            })

        return jsonify({
            "tweets": tweets,
            "total_tweets": len(tweets),
            "original_count": original_count,
            "reply_count": reply_count,
            "skipped_retweets": skipped_retweets,
            "total_tokens": total_tokens,
            "total_size": total_size
        }), 200

    except zipfile.BadZipFile:
        return jsonify({"error": "Invalid zip file"}), 400
    except json.JSONDecodeError as e:
        return jsonify({
            "error": "Failed to parse tweets JSON",
            "details": str(e)
        }), 400
    except Exception as e:
        current_app.logger.error(
            f"Error analyzing Twitter import: {str(e)}"
        )
        return jsonify({
            "error": "Failed to analyze Twitter export",
            "details": str(e)
        }), 500


@import_bp.route("/import/claude/analyze", methods=["POST"])
@login_required
def analyze_claude_import():
    """
    Analyze a Claude data export zip file.

    Expects:
        - multipart/form-data with 'zip_file' field containing a Claude
          data export (contains conversations.json)

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
                            "created_at": "..."
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
    if 'zip_file' not in request.files:
        return jsonify({"error": "No zip_file provided"}), 400

    zip_file = request.files['zip_file']

    if zip_file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    if not zip_file.filename.lower().endswith('.zip'):
        return jsonify({"error": "File must be a .zip file"}), 400

    try:
        zip_bytes = io.BytesIO(zip_file.read())

        conversations_json = None

        with zipfile.ZipFile(zip_bytes, 'r') as zip_ref:
            for name in zip_ref.namelist():
                if name.endswith('conversations.json'):
                    conversations_json = zip_ref.read(name).decode('utf-8')
                    break

        if conversations_json is None:
            return jsonify({
                "error": "Could not find conversations.json in the zip "
                         "archive. Please upload a Claude data export."
            }), 400

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
                    "token_count": token_count
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

    except zipfile.BadZipFile:
        return jsonify({"error": "Invalid zip file"}), 400
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

    try:
        # Get or create the synthetic user for claude-web nodes
        llm_user = User.query.filter_by(username="claude-web").first()
        if not llm_user:
            llm_user = User(
                twitter_id="llm-claude-web", username="claude-web"
            )
            db.session.add(llm_user)
            db.session.flush()

        nodes_created = 0
        thread_count = 0

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

            parent_node = None

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

                node = Node(
                    user_id=llm_user.id if is_assistant else current_user.id,
                    parent_id=parent_node.id if parent_node else None,
                    node_type="llm" if is_assistant else "user",
                    llm_model="claude-web" if is_assistant else None,
                    content=node_content,
                    token_count=approximate_token_count(node_content),
                    privacy_level=privacy_level,
                    ai_usage=ai_usage
                )

                if msg_created_at:
                    node.created_at = msg_created_at

                db.session.add(node)
                db.session.flush()

                parent_node = node
                nodes_created += 1

            thread_count += 1

        db.session.commit()

        # Auto-trigger profile update if enough tokens imported
        profile_update_task_id = None
        total_imported_tokens = sum(
            approximate_token_count(msg.get('text', ''))
            for conv in conversations_sorted
            for msg in conv.get('messages', [])
            if msg.get('text')
        )
        if (total_imported_tokens >= 10000
                and ai_usage in ('chat', 'train')):
            try:
                from backend.tasks.exports import (
                    maybe_trigger_profile_update
                )
                user_obj = User.query.get(current_user.id)
                if (user_obj and (user_obj.plan or "free")
                        in User.VOICE_MODE_PLANS):
                    profile_update_task_id = (
                        maybe_trigger_profile_update(current_user.id)
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


@import_bp.route("/import/twitter/confirm", methods=["POST"])
@login_required
def confirm_twitter_import():
    """
    Create nodes from analyzed Twitter data.

    Request body:
        {
            "tweets": [...],
            "import_type": "single_thread" | "separate_nodes",
            "include_replies": true | false,
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

    tweets = data.get('tweets', [])
    import_type = data.get('import_type', 'separate_nodes')
    include_replies = data.get('include_replies', False)
    privacy_level = data.get('privacy_level', 'private')
    ai_usage = data.get('ai_usage', 'none')

    if not tweets:
        return jsonify({"error": "No tweets provided"}), 400

    if import_type not in ['single_thread', 'separate_nodes']:
        return jsonify({
            "error": "Invalid import_type. "
                     "Must be 'single_thread' or 'separate_nodes'"
        }), 400

    try:
        # Filter out replies if not included
        if not include_replies:
            tweets = [t for t in tweets if not t.get('is_reply', False)]

        # Sort by created_at ascending
        tweets_sorted = sorted(
            tweets, key=lambda t: t.get('created_at', '')
        )

        nodes_created = 0
        thread_count = 0

        if import_type == 'single_thread':
            parent_node = None

            for tweet_data in tweets_sorted:
                content = tweet_data.get('full_text', '')
                token_count = tweet_data.get(
                    'token_count', approximate_token_count(content)
                )

                node = Node(
                    user_id=current_user.id,
                    parent_id=parent_node.id if parent_node else None,
                    node_type="user",
                    content=content,
                    token_count=token_count,
                    privacy_level=privacy_level,
                    ai_usage=ai_usage
                )

                db.session.add(node)
                db.session.flush()

                parent_node = node
                nodes_created += 1

            thread_count = 1

        else:  # separate_nodes
            for tweet_data in tweets_sorted:
                content = tweet_data.get('full_text', '')
                token_count = tweet_data.get(
                    'token_count', approximate_token_count(content)
                )

                node = Node(
                    user_id=current_user.id,
                    parent_id=None,
                    node_type="user",
                    content=content,
                    token_count=token_count,
                    privacy_level=privacy_level,
                    ai_usage=ai_usage
                )

                db.session.add(node)
                nodes_created += 1

            thread_count = len(tweets_sorted)

        db.session.commit()

        # Auto-trigger profile update if enough tokens imported
        profile_update_task_id = None
        total_imported_tokens = sum(
            t.get('token_count', approximate_token_count(
                t.get('full_text', '')
            ))
            for t in tweets_sorted
        )
        if (total_imported_tokens >= 10000
                and ai_usage in ('chat', 'train')):
            try:
                from backend.tasks.exports import (
                    maybe_trigger_profile_update
                )
                user_obj = User.query.get(current_user.id)
                if (user_obj and (user_obj.plan or "free")
                        in User.VOICE_MODE_PLANS):
                    profile_update_task_id = (
                        maybe_trigger_profile_update(current_user.id)
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
        }), 201

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f"Error confirming Twitter import: {str(e)}"
        )
        return jsonify({
            "error": "Failed to import tweets",
            "details": str(e)
        }), 500
