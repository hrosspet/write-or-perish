from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user
from backend.models import Node, NodeVersion
from backend.extensions import db
from datetime import datetime
from openai import OpenAI
import os
# Additional imports for Voice‑Mode functionality
from functools import wraps
from werkzeug.utils import secure_filename
import pathlib
from pydub import AudioSegment
import tempfile
# Privacy utilities
from backend.utils.privacy import (
    validate_privacy_level,
    validate_ai_usage,
    get_default_privacy_settings,
    can_user_access_node,
    can_user_edit_node,
    find_human_owner,
    PrivacyLevel,
    AIUsage
)
from backend.utils.quotes import find_quote_ids, get_quote_data, has_quotes
from backend.utils.api_keys import get_openai_chat_key
from backend.utils.webm_utils import get_webm_duration
from backend.utils.encryption import encrypt_file, decrypt_file_to_temp

# ---------------------------------------------------------------------------
# Voice‑Mode helpers
# ---------------------------------------------------------------------------


def voice_mode_required(f):
    """Decorator that restricts an endpoint to users who are allowed to access
    Voice‑Mode (see User.has_voice_mode)."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "Authentication required"}), 401

        if not current_user.has_voice_mode:
            return jsonify({"error": "Voice mode not enabled for this account"}), 403

        return f(*args, **kwargs)

    return wrapper


# Root folder (can be overridden via env var)
AUDIO_STORAGE_ROOT = pathlib.Path(os.environ.get("AUDIO_STORAGE_PATH", "data/audio")).resolve()
AUDIO_STORAGE_ROOT.mkdir(parents=True, exist_ok=True)

# OpenAI API limits for audio transcription
OPENAI_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB
OPENAI_MAX_DURATION_SEC = 1400  # ~23 minutes (OpenAI's actual limit)
CHUNK_DURATION_SEC = 20 * 60  # 20 minutes per chunk (leaves buffer below limit)

# Allowed extensions and max size (in bytes) - 200 MB.
ALLOWED_EXTENSIONS = {"webm", "wav", "m4a", "mp3", "mp4", "mpeg", "mpga", "ogg", "oga", "flac", "aac"}
MAX_AUDIO_BYTES = 200 * 1024 * 1024  # 200 MB


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _save_audio_file(file_storage, user_id: int, node_id: int, variant: str) -> str:
    """Save an uploaded (or generated) audio file and return its relative URL
    (under /media) so that the front‑end can stream it.

    variant – either "original" or "tts".  The function ensures that the
    directory structure `user/{user_id}/node/{node_id}/` exists before saving.
    """
    filename = secure_filename(file_storage.filename or f"{variant}")
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "webm"
    target_dir = AUDIO_STORAGE_ROOT / f"user/{user_id}/node/{node_id}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{variant}.{ext}"
    file_storage.save(target_path)
    # Encrypt the audio file at rest (URL stored without .enc)
    encrypt_file(str(target_path))
    # Produce URL to be consumed externally, routed via /media endpoint.
    # We use the relative path from AUDIO_STORAGE_ROOT for portability.
    rel_path = target_path.relative_to(AUDIO_STORAGE_ROOT)
    return f"/media/{rel_path.as_posix()}"


def _compress_audio_if_needed(file_path: pathlib.Path) -> pathlib.Path:
    """
    Compress audio file to MP3 if it's in an uncompressed format (WAV, FLAC)
    or if it exceeds OpenAI's size limit.
    Returns the path to the compressed file, or the original file if no compression needed.
    """
    file_size = file_path.stat().st_size
    ext = file_path.suffix.lower()

    # Check if file is uncompressed or too large
    needs_compression = ext in {".wav", ".flac"} or file_size > OPENAI_MAX_AUDIO_BYTES

    if not needs_compression:
        return file_path

    try:
        current_app.logger.info(f"Compressing audio file {file_path.name} (size: {file_size / 1024 / 1024:.1f} MB)")

        # Load audio file
        audio = AudioSegment.from_file(str(file_path))

        # Create compressed version
        compressed_path = file_path.with_suffix('.mp3')

        # Export as MP3 with reasonable quality (128kbps is good for speech)
        audio.export(
            str(compressed_path),
            format="mp3",
            bitrate="128k",
            parameters=["-q:a", "2"]  # VBR quality setting
        )

        compressed_size = compressed_path.stat().st_size
        current_app.logger.info(
            f"Compressed {file_path.name}: {file_size / 1024 / 1024:.1f} MB -> "
            f"{compressed_size / 1024 / 1024:.1f} MB"
        )

        return compressed_path

    except Exception as e:
        current_app.logger.error(f"Audio compression failed: {e}")
        return file_path  # Return original if compression fails


def _get_audio_duration(file_path: pathlib.Path) -> float:
    """Get the duration of an audio file in seconds."""
    try:
        audio = AudioSegment.from_file(str(file_path))
        return len(audio) / 1000.0  # pydub returns milliseconds
    except Exception as e:
        current_app.logger.error(f"Failed to get audio duration: {e}")
        return 0.0


def _chunk_audio(file_path: pathlib.Path, chunk_duration_sec: int = CHUNK_DURATION_SEC) -> list:
    """
    Split audio file into chunks of specified duration.
    Returns list of temporary file paths for each chunk.
    """
    try:
        audio = AudioSegment.from_file(str(file_path))
        chunk_duration_ms = chunk_duration_sec * 1000
        chunks = []

        for i, start_ms in enumerate(range(0, len(audio), chunk_duration_ms)):
            chunk = audio[start_ms:start_ms + chunk_duration_ms]

            # Create temporary file for chunk
            temp_file = tempfile.NamedTemporaryFile(
                delete=False,
                suffix='.mp3',
                prefix=f'chunk_{i}_'
            )
            chunk.export(temp_file.name, format="mp3", bitrate="128k")
            chunks.append(temp_file.name)

            current_app.logger.info(
                f"Created chunk {i + 1}: {start_ms / 1000:.0f}s - {(start_ms + len(chunk)) / 1000:.0f}s"
            )

        return chunks

    except Exception as e:
        current_app.logger.error(f"Audio chunking failed: {e}")
        return []


def _transcribe_audio_file(client: OpenAI, file_path: pathlib.Path) -> str:
    """
    Transcribe an audio file using OpenAI API.
    Handles large files by compressing and/or chunking as needed.
    Returns the transcript text.
    """
    # Step 1: Compress if needed
    processed_path = _compress_audio_if_needed(file_path)

    # Step 2: Check file size and duration
    file_size = processed_path.stat().st_size
    duration_sec = _get_audio_duration(processed_path)

    current_app.logger.info(
        f"Transcribing audio: {file_size / 1024 / 1024:.1f} MB, {duration_sec:.0f} seconds"
    )

    # Step 3: Determine if chunking is needed
    needs_chunking = (
        file_size > OPENAI_MAX_AUDIO_BYTES or
        duration_sec > OPENAI_MAX_DURATION_SEC
    )

    try:
        if not needs_chunking:
            # Simple case: transcribe whole file
            with open(processed_path, "rb") as audio_file:
                resp = client.audio.transcriptions.create(
                    model="gpt-4o-transcribe",
                    file=audio_file,
                    response_format="text"
                )

                # Extract transcript text
                if hasattr(resp, "text"):
                    return resp.text
                elif isinstance(resp, dict):
                    return resp.get("text") or resp.get("transcript") or ""
                else:
                    return str(resp)

        else:
            # Complex case: chunk and transcribe
            current_app.logger.info(
                f"File exceeds OpenAI limits (size: {file_size / 1024 / 1024:.1f} MB, "
                f"duration: {duration_sec:.0f}s), using chunked transcription"
            )

            chunk_paths = _chunk_audio(processed_path)

            if not chunk_paths:
                raise Exception("Failed to create audio chunks")

            transcripts = []

            try:
                for i, chunk_path in enumerate(chunk_paths):
                    current_app.logger.info(f"Transcribing chunk {i + 1}/{len(chunk_paths)}")

                    with open(chunk_path, "rb") as audio_file:
                        resp = client.audio.transcriptions.create(
                            model="gpt-4o-transcribe",
                            file=audio_file,
                            response_format="text"
                        )

                        # Extract transcript text
                        if hasattr(resp, "text"):
                            chunk_text = resp.text
                        elif isinstance(resp, dict):
                            chunk_text = resp.get("text") or resp.get("transcript") or ""
                        else:
                            chunk_text = str(resp)

                        transcripts.append(chunk_text)

                # Combine transcripts
                full_transcript = "\n\n".join(transcripts)
                current_app.logger.info(
                    f"Chunked transcription complete: {len(chunk_paths)} chunks, "
                    f"{len(full_transcript)} characters"
                )

                return full_transcript

            finally:
                # Clean up temporary chunk files
                for chunk_path in chunk_paths:
                    try:
                        os.unlink(chunk_path)
                    except Exception as e:
                        current_app.logger.warning(f"Failed to delete chunk file {chunk_path}: {e}")

    finally:
        # Clean up compressed file if it's different from original
        if processed_path != file_path:
            try:
                os.unlink(processed_path)
            except Exception as e:
                current_app.logger.warning(f"Failed to delete compressed file {processed_path}: {e}")


nodes_bp = Blueprint("nodes_bp", __name__)


def make_preview(text, length=200):
    return text[:length] + ("..." if len(text) > length else "")


def compute_descendant_counts(node):
    """
    Recursively computes the total number of descendants (children,
    grandchildren, etc.) for 'node' and stores it in node._descendant_count.
    Returns the computed count.
    """
    total = 0
    if node.children:
        for child in node.children:
            # For each child, compute its descendant count first, then add 1 (for the child itself)
            child_descendants = compute_descendant_counts(child)
            total += 1 + child_descendants
    node._descendant_count = total  # cache the value on the instance
    return total


def serialize_node_recursive(n, user_id=None):
    """Recursively serialize a node and its accessible children.

    Args:
        n: Node to serialize
        user_id: User ID to check access for (defaults to current_user.id)

    Returns:
        dict: Serialized node with only accessible children
    """
    if user_id is None:
        user_id = current_user.id if current_user.is_authenticated else None

    # Filter children to only include those the user can access
    accessible_children = [child for child in n.children if can_user_access_node(child, user_id)]

    # Sort the accessible children using the cached _descendant_count
    sorted_children = sorted(accessible_children, key=lambda child: child._descendant_count, reverse=True)

    return {
        "id": n.id,
        "content": n.get_content(),
        "node_type": n.node_type,
        "child_count": len(accessible_children),  # Only count accessible children
        "created_at": n.created_at.isoformat(),
        "updated_at": n.updated_at.isoformat(),
        "username": n.user.username if n.user else "Unknown",
        # You might also want to pass the descendant count along for display.
        "descendant_count": n._descendant_count,
        "children": [serialize_node_recursive(child, user_id) for child in sorted_children]
    }


def approximate_token_count(text):
    return max(1, len(text.split()))



# ---------------------------------------------------------------------------
# Create a new node (supports both text & voice uploads)
# ---------------------------------------------------------------------------


@nodes_bp.route("/", methods=["POST"])
@login_required
def create_node():
    """Create a new node.

    Two usage modes:
    1. JSON body     – `{ content, parent_id?, node_type? }` (existing behaviour)
    2. multipart/form – Field `audio_file` containing a WebM / m4a / wav file.
       Optional fields: `parent_id`, `node_type`.

    When an audio file is provided the server stores it and returns the node
    immediately.  Transcription (to populate `content`) is handled
    asynchronously (not implemented here – out of scope for MVP & unit tests).
    """

    if request.content_type and request.content_type.startswith("multipart"):
        # ------------------------------------------------------------------
        # Voice‑Mode upload path
        # ------------------------------------------------------------------
        if "audio_file" not in request.files:
            return jsonify({"error": "Field 'audio_file' is required"}), 400

        file = request.files["audio_file"]

        if file.filename == "":
            return jsonify({"error": "No selected file"}), 400
        if not _allowed_file(file.filename):
            return jsonify({"error": "Unsupported file type"}), 415

        # File size validation – some WSGI servers expose content_length.
        content_length = request.content_length or 0
        if content_length > MAX_AUDIO_BYTES:
            return jsonify({"error": "File too large"}), 413

        parent_id = request.form.get("parent_id")
        node_type = request.form.get("node_type", "user")

        # Get privacy settings from form data (with defaults)
        privacy_level = request.form.get("privacy_level", PrivacyLevel.PRIVATE)
        ai_usage = request.form.get("ai_usage", AIUsage.NONE)

        # Validate privacy settings
        if not validate_privacy_level(privacy_level):
            return jsonify({"error": f"Invalid privacy_level: {privacy_level}"}), 400
        if not validate_ai_usage(ai_usage):
            return jsonify({"error": f"Invalid ai_usage: {ai_usage}"}), 400

        # Placeholder content until transcription is ready.
        placeholder_text = "[Voice note – transcription pending]"

        node = Node(
            user_id=current_user.id,
            parent_id=parent_id,
            node_type=node_type,
            transcription_status='pending',  # Set initial status
            privacy_level=privacy_level,
            ai_usage=ai_usage
        )
        node.set_content(placeholder_text)
        db.session.add(node)
        db.session.commit()  # Need node.id for the file path.

        # Save the audio file now that we know node.id
        url = _save_audio_file(file, current_user.id, node.id, "original")
        node.audio_original_url = url
        node.audio_mime_type = file.mimetype
        db.session.add(node)
        db.session.commit()

        # -- Enqueue async transcription task
        api_key = get_openai_chat_key(current_app.config)
        if api_key:
            from backend.tasks.transcription import transcribe_audio

            # Derive local file path from the stored URL
            rel_path = node.audio_original_url.replace("/media/", "")
            local_path = str(AUDIO_STORAGE_ROOT / rel_path)

            # Enqueue task
            task = transcribe_audio.delay(node.id, local_path, file.filename)

            # Store task ID
            node.transcription_task_id = task.id
            db.session.commit()

            current_app.logger.info(f"Enqueued transcription task {task.id} for node {node.id}")

        return jsonify({
            "id": node.id,
            "audio_original_url": node.audio_original_url,
            "content": node.content,
            "node_type": node.node_type,
            "created_at": node.created_at.isoformat(),
            "transcription_status": node.transcription_status,
            "transcription_task_id": node.transcription_task_id
        }), 201

    # ------------------------------------------------------------------
    # Text upload path (original behaviour)
    # ------------------------------------------------------------------
    data = request.get_json() or {}
    content = data.get("content")
    if not content:
        return jsonify({"error": "Content is required"}), 400
    parent_id = data.get("parent_id")  # May be None for a root node.
    node_type = data.get("node_type", "user")  # default is "user"
    linked_node_id = data.get("linked_node_id")  # For linked nodes

    # Get privacy settings from request data (with defaults)
    privacy_level = data.get("privacy_level", PrivacyLevel.PRIVATE)
    ai_usage = data.get("ai_usage", AIUsage.NONE)

    # Validate privacy settings
    if not validate_privacy_level(privacy_level):
        return jsonify({"error": f"Invalid privacy_level: {privacy_level}"}), 400
    if not validate_ai_usage(ai_usage):
        return jsonify({"error": f"Invalid ai_usage: {ai_usage}"}), 400

    node = Node(
        user_id=current_user.id,
        parent_id=parent_id,
        node_type=node_type,
        linked_node_id=linked_node_id,
        privacy_level=privacy_level,
        ai_usage=ai_usage
    )
    node.set_content(content)
    db.session.add(node)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "DB error creating node"}), 500
    return jsonify({
        "id": node.id,
        "content": node.get_content(),
        "node_type": node.node_type,
        "parent_id": node.parent_id,
        "linked_node_id": node.linked_node_id,
        "created_at": node.created_at.isoformat(),
        "username": current_user.username,
        "privacy_level": node.privacy_level,
        "ai_usage": node.ai_usage
    }), 201

# Update (edit) a node. (The node's prior content is saved in NodeVersion.)
@nodes_bp.route("/<int:node_id>", methods=["PUT"])
@login_required
def update_node(node_id):
    node = Node.query.get_or_404(node_id)

    # Check authorization: owner OR LLM requester (parent node owner)
    if not can_user_edit_node(node):
        return jsonify({"error": "Not authorized"}), 403
    data = request.get_json()
    new_content = data.get("content")
    if new_content is None:
        return jsonify({"error": "Content required for update"}), 400

    # Handle privacy settings updates (optional)
    if "privacy_level" in data:
        privacy_level = data["privacy_level"]
        if not validate_privacy_level(privacy_level):
            return jsonify({"error": f"Invalid privacy_level: {privacy_level}"}), 400
        node.privacy_level = privacy_level
        # Auto-unpin if node is made private
        if privacy_level == "private" and node.pinned_at is not None:
            node.pinned_at = None
            node.pinned_by = None

    if "ai_usage" in data:
        ai_usage = data["ai_usage"]
        if not validate_ai_usage(ai_usage):
            return jsonify({"error": f"Invalid ai_usage: {ai_usage}"}), 400
        node.ai_usage = ai_usage

    # Save the current version before update.
    version = NodeVersion(node_id=node.id)
    version.set_content(node.get_content())
    db.session.add(version)
    node.set_content(new_content)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "DB error updating node"}), 500

    node = get_node(node.id)[0].get_json()  # to find all ancestors and children... / [0] is the actual node (wrapped in Response), [1] is the response code
    return jsonify({"message": "Node updated", "node": node}), 200

# Retrieve a node with its full content (the highlighted node) plus previews of its children.
@nodes_bp.route("/<int:node_id>", methods=["GET"])
@login_required
def get_node(node_id):
    node = Node.query.get_or_404(node_id)

    # Check if user has permission to access this node
    if not can_user_access_node(node, current_user.id):
        return jsonify({"error": "Not authorized to access this node"}), 403

    # Compute descendant counts once for the entire subtree.
    compute_descendant_counts(node)

    # Build ancestors, filtering by privacy
    ancestors = []
    current = node.parent
    while current:
        # Only include ancestor if user has access
        if can_user_access_node(current, current_user.id):
            ancestors.insert(0, {
                "id": current.id,
                "username": current.user.username if current.user else "Unknown",
                "preview": make_preview(current.get_content()),
                "node_type": current.node_type,
                "child_count": len(current.children),
                "created_at": current.created_at.isoformat()
            })
        current = current.parent

    # Filter children by privacy and serialize
    accessible_children = [child for child in node.children if can_user_access_node(child, current_user.id)]
    sorted_children = sorted(accessible_children, key=lambda child: child._descendant_count, reverse=True)

    # Serialize the current node (its children are now sorted descending by descendant count).
    node_data = {
        "id": node.id,
        "content": node.get_content(),
        "node_type": node.node_type,
        "child_count": len(accessible_children),
        "ancestors": ancestors,
        "children": [serialize_node_recursive(child, current_user.id) for child in sorted_children],
        "created_at": node.created_at.isoformat(),
        "updated_at": node.updated_at.isoformat(),
        "user": {
            "id": node.user.id,
            "username": node.user.username,
        },
        # Include human owner ID for LLM nodes (so frontend can check edit/delete permission)
        # For chains like Human → LLM → LLM, this returns the human's user_id
        "parent_user_id": find_human_owner(node) if node.node_type == "llm" else (node.parent.user_id if node.parent else None),
        # Privacy settings
        "privacy_level": node.privacy_level,
        "ai_usage": node.ai_usage,
        # Pin-to-profile
        "pinned_at": node.pinned_at.isoformat() if node.pinned_at else None,
        "llm_model": node.llm_model,
        "has_original_audio": bool(node.audio_original_url or node.streaming_transcription),
    }
    return jsonify(node_data), 200

# Resolve {quote:ID} placeholders in a node's content for frontend rendering.
@nodes_bp.route("/<int:node_id>/resolve-quotes", methods=["GET"])
@login_required
def resolve_node_quotes(node_id):
    """
    Return quote metadata for frontend rendering of {quote:ID} placeholders.

    Returns:
        {
            "quotes": {
                "123": { "id": 123, "content": "...", "username": "...", ... },
                "456": null  // if not accessible
            },
            "has_quotes": true
        }
    """
    node = Node.query.get_or_404(node_id)

    # Check if user has permission to access this node
    if not can_user_access_node(node, current_user.id):
        return jsonify({"error": "Not authorized to access this node"}), 403

    # Find all quote IDs in the content
    quote_ids = find_quote_ids(node.get_content())

    if not quote_ids:
        return jsonify({
            "quotes": {},
            "has_quotes": False
        }), 200

    # Get quote data with access checks
    quote_data = get_quote_data(quote_ids, current_user.id)

    return jsonify({
        "quotes": quote_data,
        "has_quotes": True
    }), 200


# Retrieve children of a node (as previews).
@nodes_bp.route("/<int:node_id>/children", methods=["GET"])
@login_required
def get_children(node_id):
    node = Node.query.get_or_404(node_id)
    def make_preview(text, length=200):
        return text[:length] + ("..." if len(text) > length else "")
    children = Node.query.filter_by(parent_id=node_id).all()
    children_list = [{
        "id": child.id,
        "preview": make_preview(child.get_content()),
        "child_count": len(child.children),
        "node_type": child.node_type,
    } for child in children]
    return jsonify({"children": children_list}), 200

@nodes_bp.route("/models", methods=["GET"])
@login_required
def get_models():
    """Return the list of supported models for the frontend."""
    supported = current_app.config["SUPPORTED_MODELS"]
    models = [
        {"id": model_id, "name": cfg["display_name"], "provider": cfg["provider"]}
        for model_id, cfg in supported.items()
    ]
    return jsonify({"models": models}), 200


# Get the default model from server config
@nodes_bp.route("/default-model", methods=["GET"])
@login_required
def get_default_model():
    """Return the default LLM model from server config."""
    default_model = current_app.config.get("DEFAULT_LLM_MODEL", "claude-opus-4.5")
    return jsonify({
        "suggested_model": default_model,
        "source": "default"
    }), 200


# Get the suggested model for a new LLM response based on the thread's context
@nodes_bp.route("/<int:node_id>/suggested-model", methods=["GET"])
@login_required
def get_suggested_model(node_id):
    """
    Return the suggested model for a new LLM response based on the thread's context.

    Logic:
    1. Walk up the thread ancestry from the given node
    2. Find the most recent node with node_type='llm' AND llm_model IS NOT NULL
    3. If found AND the model is in the supported models list, return that model
    4. If the model is "gpt-4.5-preview" (legacy), return default instead
    5. If no predecessor found, return system default
    """
    node = Node.query.get_or_404(node_id)

    # Walk up the ancestry to find the most recent LLM node
    current = node
    while current:
        if current.node_type == "llm" and current.llm_model:
            # Check if the model is supported (not legacy)
            if current.llm_model in current_app.config["SUPPORTED_MODELS"]:
                return jsonify({
                    "suggested_model": current.llm_model,
                    "source": "predecessor"
                }), 200
            # If it's the legacy model, fall through to default
            elif current.llm_model == "gpt-4.5-preview":
                break
        current = current.parent

    # No predecessor found or legacy model - return default
    default_model = current_app.config.get("DEFAULT_LLM_MODEL", "claude-opus-4.5")
    return jsonify({
        "suggested_model": default_model,
        "source": "default"
    }), 200

# Request an LLM response based on the thread (the ancestors' texts are joined as a prompt).
@nodes_bp.route("/<int:node_id>/llm", methods=["POST"])
@login_required
def request_llm_response(node_id):
    from backend.llm_providers import LLMProvider
    from backend.models import User

    parent_node = Node.query.get_or_404(node_id)

    # Get and validate the model from request body
    data = request.get_json() or {}
    model_id = data.get("model")

    if not model_id:
        # Fall back to default model for backward compatibility
        model_id = current_app.config.get("DEFAULT_LLM_MODEL", "claude-opus-4.5")

    # Validate model is supported
    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return jsonify({
            "error": f"Unsupported model: {model_id}",
            "supported_models": list(current_app.config["SUPPORTED_MODELS"].keys())
        }), 400

    # Enqueue async LLM completion task
    from backend.tasks.llm_completion import generate_llm_response

    # Create a placeholder LLM node before starting the async task.
    # This allows us to return the new node's ID to the frontend immediately,
    # which can then poll the status of this new node.

    # 1. Get or create the user for the LLM
    llm_user = User.query.filter_by(username=model_id).first()
    if not llm_user:
        llm_user = User(twitter_id=f"llm-{model_id}", username=model_id)
        db.session.add(llm_user)
        db.session.commit()

    # 2. Create the placeholder node
    # AI nodes inherit privacy settings from their parent node
    llm_node = Node(
        user_id=llm_user.id,
        parent_id=parent_node.id,
        node_type="llm",
        llm_model=model_id,
        llm_task_status='pending',
        privacy_level=parent_node.privacy_level,
        ai_usage=parent_node.ai_usage
    )
    llm_node.set_content("[LLM response generation pending...]")
    db.session.add(llm_node)
    db.session.commit()

    # 3. Enqueue the task, passing both parent and new node IDs
    task = generate_llm_response.delay(parent_node.id, llm_node.id, model_id, current_user.id)

    # 4. Store task ID on the new node
    llm_node.llm_task_id = task.id
    db.session.commit()

    current_app.logger.info(f"Enqueued LLM completion task {task.id} for parent node {parent_node.id}, new node {llm_node.id}")

    return jsonify({
        "message": "LLM response generation started",
        "task_id": task.id,
        "status": "pending",
        "node_id": llm_node.id  # Return the ID of the new node
    }), 202


# Create a linked node – allowing the user to reference another node either as a link alone or with additional text.
@nodes_bp.route("/<int:node_id>/link", methods=["POST"])
@login_required
def add_linked_node(node_id):
    parent_node = Node.query.get_or_404(node_id)
    data = request.get_json()
    linked_node_id = data.get("linked_node_id")
    additional_text = data.get("content", "")  # Optional extra text.
    if not linked_node_id:
        return jsonify({"error": "linked_node_id is required"}), 400
    # Validate that the node to be linked exists.
    linked_node = Node.query.get(linked_node_id)
    if not linked_node:
        return jsonify({"error": "Linked node not found"}), 404
    new_node = Node(
        user_id=current_user.id,
        parent_id=parent_node.id,
        node_type="link",
        linked_node_id=linked_node_id
    )
    new_node.set_content(additional_text)
    db.session.add(new_node)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "DB error adding linked node"}), 500
    return jsonify({
        "message": "Linked node added",
        "node": {
            "id": new_node.id,
            "content": new_node.get_content(),
            "node_type": new_node.node_type,
            "linked_node_id": new_node.linked_node_id,
            "created_at": new_node.created_at.isoformat(),
            "username": current_user.username
        }
    }), 201

# ---------------------------------------------------------------------------
# Voice‑Mode endpoints
# ---------------------------------------------------------------------------


@nodes_bp.route("/<int:node_id>/audio", methods=["GET"])
@login_required
def get_audio_urls(node_id):
    """Return JSON with URLs for original or TTS audio associated with a node.

    Response: 200 OK – `{ original_url: str|null, tts_url: str|null }`
              202 Accepted – when TTS generation is in progress
              404     – when neither audio exists and no generation in progress.
    """
    node = Node.query.get_or_404(node_id)

    # Public nodes: any authenticated user can listen.
    # Non-public nodes: require voice-mode (admin or paid plan).
    if node.privacy_level != "public":
        if not current_user.has_voice_mode:
            return jsonify({"error": "Voice mode not enabled for this account"}), 403

    def _media_file_exists(url):
        """Check if the file (or its .enc version) exists on disk."""
        if not url:
            return False
        rel_path = url.replace("/media/", "", 1)
        file_path = AUDIO_STORAGE_ROOT / rel_path
        enc_path = AUDIO_STORAGE_ROOT / (rel_path + '.enc')
        return file_path.is_file() or enc_path.is_file()

    # Only return URLs for files that actually exist on disk
    original_url = node.audio_original_url if _media_file_exists(node.audio_original_url) else None
    tts_url = node.audio_tts_url if _media_file_exists(node.audio_tts_url) else None

    # Check if audio chunks exist (streaming transcription nodes)
    has_audio_chunks = False
    if node.streaming_transcription:
        chunk_dir = AUDIO_STORAGE_ROOT / f"nodes/{node.user_id}/{node_id}"
        if chunk_dir.exists():
            has_chunks = (
                list(chunk_dir.glob("chunk_*.webm")) or
                list(chunk_dir.glob("chunk_*.webm.enc"))
            )
            has_audio_chunks = bool(has_chunks)

    if original_url or tts_url or has_audio_chunks:
        return jsonify({
            "original_url": original_url,
            "tts_url": tts_url,
            "has_audio_chunks": has_audio_chunks,
        }), 200

    # Check if TTS generation is in progress
    if node.tts_task_status in ['pending', 'processing']:
        return jsonify({
            "status": "generating",
            "message": "TTS generation in progress",
            "progress": node.tts_task_progress or 0,
            "task_id": node.tts_task_id
        }), 202  # 202 Accepted - request accepted but not yet completed

    # No audio and no generation in progress
    return jsonify({"error": "No audio available for this node"}), 404


@nodes_bp.route("/<int:node_id>/audio-chunks", methods=["GET"])
@login_required
def get_audio_chunks(node_id):
    """Return list of audio chunk URLs for streaming transcription nodes.

    For nodes recorded with streaming transcription, the audio is stored as
    multiple chunk files (chunk_0000.webm, chunk_0001.webm, etc.).

    Response: 200 OK – `{ chunks: [{url: "...", duration: 300.0}, ...] }`
              404     – when no chunks exist

    Note: Duration is obtained via ffprobe which correctly reads EBML metadata.
    Browsers incorrectly calculate duration from timestamps for WebM files
    with non-zero start times (common with MediaRecorder timeslice recordings).
    """
    node = Node.query.get_or_404(node_id)

    # Public nodes: any authenticated user can listen.
    # Non-public nodes: require voice-mode (admin or paid plan).
    if node.privacy_level != "public":
        if not current_user.has_voice_mode:
            return jsonify({"error": "Voice mode not enabled for this account"}), 403

    # Check if this is a streaming transcription node with chunks
    if not node.streaming_transcription:
        return jsonify({"error": "Not a streaming transcription node"}), 404

    # Find chunk files on disk
    chunk_dir = AUDIO_STORAGE_ROOT / f"nodes/{node.user_id}/{node_id}"

    if not chunk_dir.exists():
        return jsonify({"error": "No audio chunks found"}), 404

    # Get all chunk files sorted by name (support both plain and encrypted)
    chunk_files = sorted(chunk_dir.glob("chunk_*.webm"))
    chunk_files_enc = sorted(chunk_dir.glob("chunk_*.webm.enc"))
    # Prefer encrypted files if they exist, fall back to plain
    if not chunk_files and chunk_files_enc:
        chunk_files = chunk_files_enc
    elif not chunk_files and not chunk_files_enc:
        return jsonify({"error": "No audio chunks found"}), 404

    if not chunk_files:
        return jsonify({"error": "No audio chunks found"}), 404

    # Build chunk info with URLs and durations
    # Duration from ffprobe is accurate; browser's audio.duration is not
    # for WebM files with continuous timestamps across chunks
    chunks = []
    for f in chunk_files:
        # Strip .enc from filename for URL (media route handles .enc fallback)
        fname = f.name
        if fname.endswith('.enc'):
            fname = fname[:-4]
        url = f"/media/nodes/{node.user_id}/{node_id}/{fname}"
        # For encrypted files, decrypt to temp file to get accurate duration
        if f.name.endswith('.enc'):
            temp_path = None
            try:
                temp_path = decrypt_file_to_temp(str(f))
                duration = get_webm_duration(temp_path)
                if duration is None:
                    duration = 300.0
            except Exception as e:
                current_app.logger.warning(f"Failed to get duration for encrypted chunk {f.name}: {e}")
                duration = 300.0
            finally:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
        else:
            duration = get_webm_duration(str(f))
            if duration is None:
                duration = 300.0
        chunks.append({"url": url, "duration": duration})

    return jsonify({"chunks": chunks}), 200


@nodes_bp.route("/<int:node_id>/tts", methods=["POST"])
@login_required
@voice_mode_required
def generate_tts(node_id):
    """Trigger (mock) TTS generation for the node.

    In a production setup this would queue a background task.  For the purpose
    of unit tests and the MVP we generate a dummy file synchronously and return
    `202 Accepted` (if generation was triggered) or `200 OK` (if it already
    exists).
    """
    node = Node.query.get_or_404(node_id)

    # If original recording exists we stream that – generating TTS is not
    # allowed.
    if node.audio_original_url:
        return jsonify({"error": "Original audio exists – TTS not required"}), 409

    # Already generated?
    if node.audio_tts_url:
        return jsonify({"message": "TTS already available", "tts_url": node.audio_tts_url}), 200

    # Check if OpenAI API key is configured
    api_key = get_openai_chat_key(current_app.config)
    if not api_key:
        return jsonify({"error": "TTS not configured (missing API key)"}), 500

    # Enqueue async TTS generation task
    from backend.tasks.tts import generate_tts_audio

    # Set initial task status
    node.tts_task_status = 'pending'
    node.tts_task_progress = 0
    db.session.commit()

    # Enqueue task
    task = generate_tts_audio.delay(node.id, str(AUDIO_STORAGE_ROOT))

    # Store task ID
    node.tts_task_id = task.id
    db.session.commit()

    current_app.logger.info(f"Enqueued TTS generation task {task.id} for node {node.id}")

    return jsonify({
        "message": "TTS generation started",
        "task_id": task.id,
        "status": "pending",
        "node_id": node.id
    }), 202


# ---------------------------------------------------------------------------
# Async task status endpoints
# ---------------------------------------------------------------------------


@nodes_bp.route("/<int:node_id>/transcription-status", methods=["GET"])
@login_required
def get_transcription_status(node_id):
    """Get the current transcription status for a node."""
    node = Node.query.get_or_404(node_id)

    # Check ownership
    if node.user_id != current_user.id and not getattr(current_user, "is_admin", False):
        return jsonify({"error": "Unauthorized"}), 403

    # Get task status from Celery if still processing
    task_info = None
    if node.transcription_task_id and node.transcription_status == 'processing':
        try:
            from backend.celery_app import celery
            task = celery.AsyncResult(node.transcription_task_id)

            if task.state == 'PROGRESS':
                task_info = task.info  # Contains progress and status message
            elif task.state == 'SUCCESS':
                # Task completed but DB not updated yet
                node.transcription_status = 'completed'
                db.session.commit()
        except Exception as e:
            # If Celery check fails, log and continue with DB status
            current_app.logger.warning(f"Failed to check Celery task status: {e}")
            # Don't fail the request - just return DB status without real-time info

    return jsonify({
        "node_id": node.id,
        "status": node.transcription_status,
        "progress": node.transcription_progress or 0,
        "error": node.transcription_error,
        "started_at": node.transcription_started_at.isoformat() if node.transcription_started_at else None,
        "completed_at": node.transcription_completed_at.isoformat() if node.transcription_completed_at else None,
        "content": node.get_content() if node.transcription_status == 'completed' else None,
        "task_info": task_info  # Real-time progress from Celery
    })


@nodes_bp.route("/<int:node_id>/llm-status", methods=["GET"])
@login_required
def get_llm_status(node_id):
    """Get the current LLM completion status for a node."""
    node = Node.query.get_or_404(node_id)

    # Check ownership
    if node.user_id != current_user.id and not getattr(current_user, "is_admin", False):
        return jsonify({"error": "Unauthorized"}), 403

    # Get task status from Celery if still processing
    task_info = None
    created_node = None
    if node.llm_task_id:
        try:
            from backend.celery_app import celery
            task = celery.AsyncResult(node.llm_task_id)

            if task.state == 'PROGRESS':
                task_info = task.info
            elif task.state == 'SUCCESS':
                node.llm_task_status = 'completed'
                db.session.commit()
                # Get the created node ID from task result
                if task.result and 'llm_node_id' in task.result:
                    llm_node = Node.query.get(task.result['llm_node_id'])
                    if llm_node:
                        created_node = {
                            "id": llm_node.id,
                            "content": llm_node.get_content(),
                            "node_type": llm_node.node_type,
                            "llm_model": llm_node.llm_model,
                            "created_at": llm_node.created_at.isoformat()
                        }
        except Exception as e:
            # If Celery check fails, log and continue with DB status
            current_app.logger.warning(f"Failed to check Celery task status: {e}")
            # Don't fail the request - just return DB status without real-time info

    response_data = {
        "node_id": node.id,
        "status": node.llm_task_status,
        "progress": node.llm_task_progress or 0,
        "error": node.llm_task_error,
        "task_info": task_info
    }

    if created_node:
        response_data["node"] = created_node

    return jsonify(response_data)


@nodes_bp.route("/<int:node_id>/tts-status", methods=["GET"])
@login_required
def get_tts_status(node_id):
    """Get the current TTS generation status for a node."""
    node = Node.query.get_or_404(node_id)

    # Check ownership
    if node.user_id != current_user.id and not getattr(current_user, "is_admin", False):
        return jsonify({"error": "Unauthorized"}), 403

    # Get task status from Celery if still processing
    task_info = None
    if node.tts_task_id:
        try:
            from backend.celery_app import celery
            task = celery.AsyncResult(node.tts_task_id)

            if task.state == 'PROGRESS':
                task_info = task.info
            elif task.state == 'SUCCESS':
                node.tts_task_status = 'completed'
                db.session.commit()
        except Exception as e:
            # If Celery check fails, log and continue with DB status
            current_app.logger.warning(f"Failed to check Celery task status: {e}")
            # Don't fail the request - just return DB status without real-time info

    response_data = {
        "node_id": node.id,
        "status": node.tts_task_status,
        "progress": node.tts_task_progress or 0,
        "task_info": task_info
    }

    # Include node data when completed
    if node.tts_task_status == 'completed':
        response_data["node"] = {
            "id": node.id,
            "audio_tts_url": node.audio_tts_url
        }

    return jsonify(response_data)


# ---------------------------------------------------------------------------
# Chunked upload endpoints
# ---------------------------------------------------------------------------


@nodes_bp.route("/upload/init", methods=["POST"])
@login_required
def init_chunked_upload():
    """Initialize a chunked upload session.

    Creates a placeholder node and prepares for chunk reception.

    Request body:
    {
        "filename": "recording.m4a",
        "filesize": 190000000,
        "total_chunks": 38,
        "upload_id": "unique-id",
        "parent_id": 123,  // optional
        "node_type": "user"  // optional
    }

    Returns: { "node_id": 456, "upload_id": "unique-id" }
    """
    data = request.get_json() or {}

    filename = data.get("filename")
    filesize = data.get("filesize")
    total_chunks = data.get("total_chunks")
    upload_id = data.get("upload_id")
    parent_id = data.get("parent_id")
    node_type = data.get("node_type", "user")

    # Get privacy settings (with defaults)
    privacy_level = data.get("privacy_level", PrivacyLevel.PRIVATE)
    ai_usage = data.get("ai_usage", AIUsage.NONE)

    # Validate required fields
    if not all([filename, filesize, total_chunks, upload_id]):
        return jsonify({"error": "Missing required fields"}), 400

    # Validate file type
    if not _allowed_file(filename):
        return jsonify({"error": "Unsupported file type"}), 415

    # Validate file size
    if filesize > MAX_AUDIO_BYTES:
        return jsonify({"error": "File too large"}), 413

    # Validate privacy settings
    if not validate_privacy_level(privacy_level):
        return jsonify({"error": f"Invalid privacy_level: {privacy_level}"}), 400
    if not validate_ai_usage(ai_usage):
        return jsonify({"error": f"Invalid ai_usage: {ai_usage}"}), 400

    # Create placeholder node
    placeholder_text = "[Voice note – upload in progress]"
    node = Node(
        user_id=current_user.id,
        parent_id=parent_id,
        node_type=node_type,
        transcription_status='pending',
        privacy_level=privacy_level,
        ai_usage=ai_usage
    )
    node.set_content(placeholder_text)
    db.session.add(node)
    db.session.commit()

    # Create directory for chunk storage
    chunk_dir = AUDIO_STORAGE_ROOT / f"chunks/{current_user.id}/{upload_id}"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    # Store upload metadata
    metadata = {
        "node_id": node.id,
        "filename": filename,
        "filesize": filesize,
        "total_chunks": total_chunks,
        "uploaded_chunks": []
    }

    import json
    metadata_path = chunk_dir / "metadata.json"
    with open(metadata_path, "w") as f:
        json.dump(metadata, f)

    current_app.logger.info(
        f"Initialized chunked upload {upload_id} for node {node.id}: "
        f"{filename} ({filesize / (1024 * 1024):.1f} MB, {total_chunks} chunks)"
    )

    return jsonify({
        "node_id": node.id,
        "upload_id": upload_id
    }), 201


@nodes_bp.route("/upload/chunk", methods=["POST"])
@login_required
def upload_chunk():
    """Receive and store a single chunk.

    Expects multipart/form-data with:
    - chunk: file blob
    - chunk_index: integer
    - upload_id: string
    - node_id: integer
    """
    if "chunk" not in request.files:
        return jsonify({"error": "Missing chunk file"}), 400

    chunk_file = request.files["chunk"]
    chunk_index = request.form.get("chunk_index")
    upload_id = request.form.get("upload_id")
    node_id = request.form.get("node_id")

    if not all([chunk_index is not None, upload_id, node_id]):
        return jsonify({"error": "Missing required fields"}), 400

    try:
        chunk_index = int(chunk_index)
        node_id = int(node_id)
    except ValueError:
        return jsonify({"error": "Invalid chunk_index or node_id"}), 400

    # Verify node ownership
    node = Node.query.get_or_404(node_id)
    if node.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    # Save chunk
    chunk_dir = AUDIO_STORAGE_ROOT / f"chunks/{current_user.id}/{upload_id}"
    if not chunk_dir.exists():
        return jsonify({"error": "Upload session not found"}), 404

    chunk_path = chunk_dir / f"chunk_{chunk_index:04d}"
    chunk_file.save(chunk_path)

    # Update metadata
    import json
    metadata_path = chunk_dir / "metadata.json"
    with open(metadata_path, "r") as f:
        metadata = json.load(f)

    if chunk_index not in metadata["uploaded_chunks"]:
        metadata["uploaded_chunks"].append(chunk_index)
        metadata["uploaded_chunks"].sort()

    with open(metadata_path, "w") as f:
        json.dump(metadata, f)

    current_app.logger.debug(
        f"Received chunk {chunk_index} for upload {upload_id} "
        f"({len(metadata['uploaded_chunks'])}/{metadata['total_chunks']})"
    )

    return jsonify({
        "message": "Chunk received",
        "chunk_index": chunk_index,
        "uploaded_chunks": len(metadata["uploaded_chunks"]),
        "total_chunks": metadata["total_chunks"]
    }), 200


@nodes_bp.route("/upload/finalize", methods=["POST"])
@login_required
def finalize_chunked_upload():
    """Finalize a chunked upload by reassembling chunks into final file.

    Request body:
    {
        "upload_id": "unique-id",
        "node_id": 456
    }

    Returns: node info (same as create_node endpoint)
    """
    data = request.get_json() or {}
    upload_id = data.get("upload_id")
    node_id = data.get("node_id")

    if not all([upload_id, node_id]):
        return jsonify({"error": "Missing required fields"}), 400

    try:
        node_id = int(node_id)
    except ValueError:
        return jsonify({"error": "Invalid node_id"}), 400

    # Verify node ownership
    node = Node.query.get_or_404(node_id)
    if node.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    # Load metadata
    chunk_dir = AUDIO_STORAGE_ROOT / f"chunks/{current_user.id}/{upload_id}"
    if not chunk_dir.exists():
        return jsonify({"error": "Upload session not found"}), 404

    import json
    metadata_path = chunk_dir / "metadata.json"
    with open(metadata_path, "r") as f:
        metadata = json.load(f)

    # Verify all chunks received
    expected_chunks = list(range(metadata["total_chunks"]))
    if metadata["uploaded_chunks"] != expected_chunks:
        missing = set(expected_chunks) - set(metadata["uploaded_chunks"])
        return jsonify({
            "error": "Incomplete upload",
            "missing_chunks": list(missing)
        }), 400

    # Reassemble chunks
    filename = metadata["filename"]
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "webm"

    target_dir = AUDIO_STORAGE_ROOT / f"user/{current_user.id}/node/{node.id}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"original.{ext}"

    current_app.logger.info(f"Reassembling {metadata['total_chunks']} chunks for upload {upload_id}")

    # Combine chunks into final file
    with open(target_path, "wb") as outfile:
        for chunk_index in range(metadata["total_chunks"]):
            chunk_path = chunk_dir / f"chunk_{chunk_index:04d}"
            with open(chunk_path, "rb") as infile:
                outfile.write(infile.read())

    # Verify file size
    final_size = target_path.stat().st_size
    if final_size != metadata["filesize"]:
        current_app.logger.error(
            f"File size mismatch for upload {upload_id}: "
            f"expected {metadata['filesize']}, got {final_size}"
        )
        # Don't fail - continue with transcription

    # Encrypt the assembled audio file at rest (URL stored without .enc)
    encrypt_file(str(target_path))

    # Update node with audio info
    rel_path = target_path.relative_to(AUDIO_STORAGE_ROOT)
    node.audio_original_url = f"/media/{rel_path.as_posix()}"
    node.audio_mime_type = f"audio/{ext}"
    node.set_content("[Voice note – transcription pending]")
    db.session.commit()

    # Clean up chunks
    import shutil
    try:
        shutil.rmtree(chunk_dir)
        current_app.logger.info(f"Cleaned up chunks for upload {upload_id}")
    except Exception as e:
        current_app.logger.warning(f"Failed to clean up chunks: {e}")

    # Enqueue transcription task
    api_key = get_openai_chat_key(current_app.config)
    if api_key:
        from backend.tasks.transcription import transcribe_audio

        task = transcribe_audio.delay(node.id, str(target_path), metadata["filename"])
        node.transcription_task_id = task.id
        db.session.commit()

        current_app.logger.info(f"Enqueued transcription task {task.id} for node {node.id}")

    return jsonify({
        "id": node.id,
        "audio_original_url": node.audio_original_url,
        "content": node.content,
        "node_type": node.node_type,
        "created_at": node.created_at.isoformat(),
        "transcription_status": node.transcription_status,
        "transcription_task_id": node.transcription_task_id
    }), 200


@nodes_bp.route("/upload/cleanup", methods=["POST"])
@login_required
def cleanup_chunked_upload():
    """Clean up a failed/cancelled chunked upload.

    Request body:
    {
        "upload_id": "unique-id"
    }
    """
    data = request.get_json() or {}
    upload_id = data.get("upload_id")

    if not upload_id:
        return jsonify({"error": "Missing upload_id"}), 400

    chunk_dir = AUDIO_STORAGE_ROOT / f"chunks/{current_user.id}/{upload_id}"

    if chunk_dir.exists():
        import shutil
        try:
            shutil.rmtree(chunk_dir)
            current_app.logger.info(f"Cleaned up failed upload {upload_id}")
            return jsonify({"message": "Cleanup successful"}), 200
        except Exception as e:
            current_app.logger.error(f"Cleanup failed for upload {upload_id}: {e}")
            return jsonify({"error": "Cleanup failed"}), 500

    return jsonify({"message": "Nothing to clean up"}), 200


# ---------------------------------------------------------------------------
# Media serving endpoint (simple/dev only – not for production)
# ---------------------------------------------------------------------------


@nodes_bp.route("/media/<path:filename>", methods=["GET"])
def serve_audio_file(filename):
    """Serve files from the AUDIO_STORAGE_ROOT with support for range requests.

    This is a **development‑only** helper to unblock tests.  In production the
    app would be served by the web server (e.g. nginx) or a cloud storage
    bucket.  Range requests are *not* implemented; whole file is returned.

    Note: The production media blueprint (media_bp at /media) handles
    encrypted .enc files. This endpoint is only used in tests.
    """
    file_path = AUDIO_STORAGE_ROOT / filename
    if not file_path.is_file():
        return jsonify({"error": "File not found"}), 404
    from flask import send_file

    return send_file(file_path)


# ---------------------------------------------------------------------------
# Streaming Transcription endpoints (real-time transcription during recording)
# ---------------------------------------------------------------------------


@nodes_bp.route("/streaming/init", methods=["POST"])
@login_required
def init_streaming_transcription():
    """Initialize a streaming transcription session.

    Creates a placeholder node ready to receive audio chunks that will be
    transcribed in real-time as they arrive.

    Request body:
    {
        "parent_id": 123,  // optional
        "node_type": "user",  // optional
        "privacy_level": "private",  // optional
        "ai_usage": "none",  // optional
        "chunk_interval_ms": 300000  // 5 minutes in milliseconds
    }

    Returns: { "node_id": 456, "session_id": "unique-session-id" }
    """
    data = request.get_json() or {}

    parent_id = data.get("parent_id")
    node_type = data.get("node_type", "user")
    privacy_level = data.get("privacy_level", PrivacyLevel.PRIVATE)
    ai_usage = data.get("ai_usage", AIUsage.NONE)
    chunk_interval_ms = data.get("chunk_interval_ms", 300000)  # Default 5 minutes

    # Validate privacy settings
    if not validate_privacy_level(privacy_level):
        return jsonify({"error": f"Invalid privacy_level: {privacy_level}"}), 400
    if not validate_ai_usage(ai_usage):
        return jsonify({"error": f"Invalid ai_usage: {ai_usage}"}), 400

    # Generate session ID
    import uuid
    session_id = str(uuid.uuid4())

    # Create placeholder node with streaming mode enabled
    placeholder_text = "[Voice note – streaming transcription in progress]"
    node = Node(
        user_id=current_user.id,
        parent_id=parent_id,
        node_type=node_type,
        transcription_status='processing',
        streaming_transcription=True,
        streaming_session_id=session_id,
        streaming_total_chunks=None,  # Unknown until recording stops
        streaming_completed_chunks=0,
        privacy_level=privacy_level,
        ai_usage=ai_usage
    )
    node.set_content(placeholder_text)
    db.session.add(node)
    db.session.commit()

    # Create directory for chunk storage
    chunk_dir = AUDIO_STORAGE_ROOT / f"streaming/{current_user.id}/{session_id}"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    current_app.logger.info(
        f"Initialized streaming transcription session {session_id} for node {node.id} "
        f"(chunk_interval: {chunk_interval_ms}ms)"
    )

    return jsonify({
        "node_id": node.id,
        "session_id": session_id,
        "sse_url": f"/api/sse/nodes/{node.id}/transcription-stream"
    }), 201


@nodes_bp.route("/<int:node_id>/audio-chunk", methods=["POST"])
@login_required
def upload_audio_chunk(node_id):
    """Upload an audio chunk for streaming transcription.

    Receives an audio chunk from the frontend during recording, stores it,
    and immediately queues it for transcription.

    Expects multipart/form-data with:
    - chunk: audio file blob
    - chunk_index: integer (0-based)
    - session_id: string (from init_streaming_transcription)

    Returns: { "chunk_index": 0, "task_id": "celery-task-id" }
    """
    node = Node.query.get_or_404(node_id)

    # Verify ownership
    if node.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    # Verify streaming mode
    if not node.streaming_transcription:
        return jsonify({"error": "Node is not in streaming transcription mode"}), 400

    # Get form data
    if "chunk" not in request.files:
        return jsonify({"error": "Missing chunk file"}), 400

    chunk_file = request.files["chunk"]
    chunk_index = request.form.get("chunk_index")
    session_id = request.form.get("session_id")

    if chunk_index is None:
        return jsonify({"error": "Missing chunk_index"}), 400
    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    try:
        chunk_index = int(chunk_index)
    except ValueError:
        return jsonify({"error": "Invalid chunk_index"}), 400

    # Verify session ID matches
    if node.streaming_session_id != session_id:
        return jsonify({"error": "Session ID mismatch"}), 400

    # Save chunk to disk
    chunk_dir = AUDIO_STORAGE_ROOT / f"streaming/{current_user.id}/{session_id}"
    if not chunk_dir.exists():
        return jsonify({"error": "Streaming session not found"}), 404

    chunk_filename = f"chunk_{chunk_index:04d}.webm"
    chunk_path = chunk_dir / chunk_filename
    chunk_file.save(chunk_path)

    # Encrypt the audio chunk at rest
    encrypted_path = encrypt_file(str(chunk_path))

    # Create transcript chunk record
    from backend.models import NodeTranscriptChunk

    # Check if chunk already exists (retry scenario)
    existing_chunk = NodeTranscriptChunk.query.filter_by(
        node_id=node_id,
        chunk_index=chunk_index
    ).first()

    if existing_chunk:
        # Update existing chunk if it failed before
        if existing_chunk.status == 'failed':
            existing_chunk.status = 'pending'
            existing_chunk.error = None
            db.session.commit()
            transcript_chunk = existing_chunk
        else:
            return jsonify({
                "message": "Chunk already uploaded",
                "chunk_index": chunk_index,
                "status": existing_chunk.status
            }), 200
    else:
        transcript_chunk = NodeTranscriptChunk(
            node_id=node_id,
            chunk_index=chunk_index,
            status='pending'
        )
        db.session.add(transcript_chunk)
        db.session.commit()

    # Queue transcription task
    from backend.tasks.streaming_transcription import transcribe_chunk

    task = transcribe_chunk.delay(
        node_id=node_id,
        chunk_index=chunk_index,
        chunk_path=encrypted_path
    )

    # Update chunk with task ID
    transcript_chunk.task_id = task.id
    transcript_chunk.status = 'processing'
    db.session.commit()

    current_app.logger.info(
        f"Received audio chunk {chunk_index} for node {node_id}, "
        f"enqueued transcription task {task.id}"
    )

    return jsonify({
        "chunk_index": chunk_index,
        "task_id": task.id,
        "status": "processing"
    }), 202


@nodes_bp.route("/<int:node_id>/finalize-streaming", methods=["POST"])
@login_required
def finalize_streaming_transcription(node_id):
    """Finalize streaming transcription.

    Called when the user stops recording. Assembles all transcribed chunks
    into the final node content.

    Request body:
    {
        "session_id": "unique-session-id",
        "total_chunks": 5  // Total number of chunks sent
    }

    Returns: Node info with assembled transcript
    """
    node = Node.query.get_or_404(node_id)

    # Verify ownership
    if node.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    # Verify streaming mode
    if not node.streaming_transcription:
        return jsonify({"error": "Node is not in streaming transcription mode"}), 400

    data = request.get_json() or {}
    session_id = data.get("session_id")
    total_chunks = data.get("total_chunks")

    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400
    if total_chunks is None:
        return jsonify({"error": "Missing total_chunks"}), 400

    # Verify session ID matches
    if node.streaming_session_id != session_id:
        return jsonify({"error": "Session ID mismatch"}), 400

    # Update total chunks now that we know the final count
    node.streaming_total_chunks = total_chunks
    db.session.commit()

    # Queue finalization task
    from backend.tasks.streaming_transcription import finalize_streaming

    task = finalize_streaming.delay(
        node_id=node_id,
        session_id=session_id,
        total_chunks=total_chunks
    )

    current_app.logger.info(
        f"Finalizing streaming transcription for node {node_id}, "
        f"total_chunks: {total_chunks}, task: {task.id}"
    )

    return jsonify({
        "message": "Finalization started",
        "task_id": task.id,
        "node_id": node_id,
        "total_chunks": total_chunks
    }), 202


@nodes_bp.route("/<int:node_id>/streaming-status", methods=["GET"])
@login_required
def get_streaming_status(node_id):
    """Get the current streaming transcription status for a node.

    Returns status of all chunks and overall transcription progress.
    """
    node = Node.query.get_or_404(node_id)

    # Check ownership
    if node.user_id != current_user.id and not getattr(current_user, "is_admin", False):
        return jsonify({"error": "Unauthorized"}), 403

    if not node.streaming_transcription:
        return jsonify({"error": "Node is not in streaming transcription mode"}), 400

    # Get all chunks
    from backend.models import NodeTranscriptChunk

    chunks = NodeTranscriptChunk.query.filter_by(node_id=node_id).order_by(
        NodeTranscriptChunk.chunk_index
    ).all()

    chunk_statuses = [{
        "chunk_index": c.chunk_index,
        "status": c.status,
        "text": c.text if c.status == 'completed' else None,
        "error": c.error if c.status == 'failed' else None
    } for c in chunks]

    completed_count = sum(1 for c in chunks if c.status == 'completed')
    failed_count = sum(1 for c in chunks if c.status == 'failed')

    return jsonify({
        "node_id": node_id,
        "session_id": node.streaming_session_id,
        "transcription_status": node.transcription_status,
        "streaming_transcription": node.streaming_transcription,
        "total_chunks": node.streaming_total_chunks,
        "completed_chunks": completed_count,
        "failed_chunks": failed_count,
        "chunks": chunk_statuses,
        "content": node.get_content() if node.transcription_status == 'completed' else None
    })


# ---------------------------------------------------------------------------
# Pin / Unpin endpoints
# ---------------------------------------------------------------------------


@nodes_bp.route("/<int:node_id>/pin", methods=["POST"])
@login_required
def pin_node(node_id):
    """Pin a node to the current user's profile (Dashboard + Feed)."""
    node = Node.query.get_or_404(node_id)

    # Determine human owner: for LLM nodes walk up the parent chain
    if node.node_type == "llm":
        human_owner_id = find_human_owner(node)
    else:
        human_owner_id = node.user_id

    if human_owner_id != current_user.id:
        return jsonify({"error": "Only the owner can pin this node"}), 403

    if node.privacy_level == "private":
        return jsonify({"error": "Cannot pin a private node"}), 400

    if node.pinned_at is not None:
        return jsonify({"error": "Node is already pinned"}), 400

    node.pinned_at = datetime.utcnow()
    node.pinned_by = current_user.id
    db.session.commit()

    return jsonify({
        "message": "Node pinned",
        "pinned_at": node.pinned_at.isoformat()
    }), 200


@nodes_bp.route("/<int:node_id>/pin", methods=["DELETE"])
@login_required
def unpin_node(node_id):
    """Unpin a node from the current user's profile."""
    node = Node.query.get_or_404(node_id)

    # Determine human owner
    if node.node_type == "llm":
        human_owner_id = find_human_owner(node)
    else:
        human_owner_id = node.user_id

    if human_owner_id != current_user.id:
        return jsonify({"error": "Only the owner can unpin this node"}), 403

    node.pinned_at = None
    node.pinned_by = None
    db.session.commit()

    return jsonify({"message": "Node unpinned"}), 200


@nodes_bp.route("/<int:node_id>", methods=["DELETE"])
@login_required
def delete_node(node_id):
    from backend.models import NodeVersion, NodeTranscriptChunk, TTSChunk, Draft

    node = Node.query.get_or_404(node_id)
    # Allow deletion if user is the owner or LLM requester (parent node owner)
    if not can_user_edit_node(node):
        return jsonify({"error": "Not authorized"}), 403

    # Update all children: set their parent_id to None
    # (This "orphans" the children so they become top‑level nodes.)
    try:
        Node.query.filter_by(parent_id=node.id).update({"parent_id": None})

        # Clean up related records that have foreign keys to this node
        NodeVersion.query.filter_by(node_id=node.id).delete()
        NodeTranscriptChunk.query.filter_by(node_id=node.id).delete()
        TTSChunk.query.filter_by(node_id=node.id).delete()

        # Update drafts that reference this node
        Draft.query.filter_by(node_id=node.id).update({"node_id": None})
        Draft.query.filter_by(parent_id=node.id).update({"parent_id": None})

        # Update linked_node_id references in other nodes
        Node.query.filter_by(linked_node_id=node.id).update({"linked_node_id": None})

        db.session.delete(node)
        db.session.commit()
        return jsonify({"message": "Node deleted successfully"}), 200
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting node {node_id}: {e}")
        return jsonify({"error": "Error deleting node", "details": str(e)}), 500
