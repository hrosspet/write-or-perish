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

# ---------------------------------------------------------------------------
# Voice‑Mode helpers
# ---------------------------------------------------------------------------


def voice_mode_required(f):
    """Decorator that restricts an endpoint to users who are allowed to access
    Voice‑Mode (currently admins or paying users)."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "Authentication required"}), 401

        # Allow admins.
        if getattr(current_user, "is_admin", False):
            return f(*args, **kwargs)

        # Allow paying users (plan != 'free').
        if getattr(current_user, "plan", "free") != "free":
            return f(*args, **kwargs)

        return jsonify({"error": "Voice mode not enabled for this account"}), 403

    return wrapper


# Root folder (can be overridden via env var)
AUDIO_STORAGE_ROOT = pathlib.Path(os.environ.get("AUDIO_STORAGE_PATH", "data/audio")).resolve()
AUDIO_STORAGE_ROOT.mkdir(parents=True, exist_ok=True)

# OpenAI API limits for audio transcription
OPENAI_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB
OPENAI_MAX_DURATION_SEC = 1500  # 25 minutes
CHUNK_DURATION_SEC = 20 * 60  # 20 minutes per chunk (leaves buffer below 25 min limit)

# Allowed extensions and max size (in bytes) – 100 MB.
ALLOWED_EXTENSIONS = {"webm", "wav", "m4a", "mp3", "mp4", "mpeg", "mpga", "ogg", "oga", "flac", "aac"}
MAX_AUDIO_BYTES = 100 * 1024 * 1024  # 100 MB


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


def serialize_node_recursive(n):
    # Sort the children using the cached _descendant_count
    sorted_children = sorted(n.children, key=lambda child: child._descendant_count, reverse=True)
    return {
        "id": n.id,
        "content": n.content,
        "node_type": n.node_type,
        "child_count": len(n.children),
        "created_at": n.created_at.isoformat(),
        "updated_at": n.updated_at.isoformat(),
        "username": n.user.username if n.user else "Unknown",
        # You might also want to pass the descendant count along for display.
        "descendant_count": n._descendant_count,
        "children": [serialize_node_recursive(child) for child in sorted_children]
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

        # Placeholder content until transcription is ready.
        placeholder_text = "[Voice note – transcription pending]"

        node = Node(
            user_id=current_user.id,
            parent_id=parent_id,
            node_type=node_type,
            content=placeholder_text,
            transcription_status='pending'  # Set initial status
        )
        db.session.add(node)
        db.session.commit()  # Need node.id for the file path.

        # Save the audio file now that we know node.id
        url = _save_audio_file(file, current_user.id, node.id, "original")
        node.audio_original_url = url
        node.audio_mime_type = file.mimetype
        db.session.add(node)
        db.session.commit()

        # -- Enqueue async transcription task
        api_key = current_app.config.get("OPENAI_API_KEY")
        if api_key:
            from backend.tasks.transcription import transcribe_audio

            # Derive local file path from the stored URL
            rel_path = node.audio_original_url.replace("/media/", "")
            local_path = str(AUDIO_STORAGE_ROOT / rel_path)

            # Enqueue task
            task = transcribe_audio.delay(node.id, local_path)

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
    node = Node(
        user_id=current_user.id,
        parent_id=parent_id,
        node_type=node_type,
        content=content,
        linked_node_id=linked_node_id,
    )
    db.session.add(node)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "DB error creating node"}), 500
    return jsonify({
        "id": node.id,
        "content": node.content,
        "node_type": node.node_type,
        "parent_id": node.parent_id,
        "linked_node_id": node.linked_node_id,
        "created_at": node.created_at.isoformat(),
        "username": current_user.username,
    }), 201

# Update (edit) a node. (The node’s prior content is saved in NodeVersion.)
@nodes_bp.route("/<int:node_id>", methods=["PUT"])
@login_required
def update_node(node_id):
    node = Node.query.get_or_404(node_id)
    if node.user_id != current_user.id:
        return jsonify({"error": "Not authorized"}), 403
    data = request.get_json()
    new_content = data.get("content")
    if new_content is None:
        return jsonify({"error": "Content required for update"}), 400
    # Save the current version before update.
    version = NodeVersion(node_id=node.id, content=node.content)
    db.session.add(version)
    node.content = new_content
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

    # Compute descendant counts once for the entire subtree.
    compute_descendant_counts(node)

    # Build ancestors as before.
    ancestors = []
    current = node.parent
    while current:
        ancestors.insert(0, {
            "id": current.id,
            "username": current.user.username if current.user else "Unknown",
            "preview": make_preview(current.content),
            "node_type": current.node_type,
            "child_count": len(current.children),
            "created_at": current.created_at.isoformat()
        })
        current = current.parent

    # Serialize the current node (its children are now sorted descending by descendant count).
    node_data = {
        "id": node.id,
        "content": node.content,
        "node_type": node.node_type,
        "child_count": len(node.children),
        "ancestors": ancestors,
        "children": [serialize_node_recursive(child) for child in sorted(node.children,
                      key=lambda child: child._descendant_count, reverse=True)],
        "created_at": node.created_at.isoformat(),
        "updated_at": node.updated_at.isoformat(),
        "user": {
            "id": node.user.id,
            "username": node.user.username,
        },
    }
    return jsonify(node_data), 200

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
        "preview": make_preview(child.content),
        "child_count": len(child.children),
        "node_type": child.node_type,
    } for child in children]
    return jsonify({"children": children_list}), 200

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
    default_model = current_app.config.get("DEFAULT_LLM_MODEL", "gpt-5")
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
        model_id = current_app.config.get("DEFAULT_LLM_MODEL", "gpt-5")

    # Validate model is supported
    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return jsonify({
            "error": f"Unsupported model: {model_id}",
            "supported_models": list(current_app.config["SUPPORTED_MODELS"].keys())
        }), 400

    # Build the chain of nodes (from top‐level to current)
    node_chain = []
    current = parent_node
    while current:
        node_chain.insert(0, current)
        current = current.parent

    # Build messages array for LLM API
    messages = []
    for node in node_chain:
        author = node.user.username if node.user else "Unknown"
        # Determine if this node was created by an LLM (check if it matches any model ID or llm_model)
        is_llm_node = node.node_type == "llm" or (node.llm_model is not None)

        if is_llm_node:
            role = "assistant"
            message_text = node.content
        else:
            role = "user"
            message_text = f"author {author}: {node.content}"

        messages.append({
            "role": role,
            "content": [
                {
                    "type": "text",
                    "text": message_text
                }
            ]
        })

    # Prepare API keys
    api_keys = {
        "openai": current_app.config.get("OPENAI_API_KEY"),
        "anthropic": current_app.config.get("ANTHROPIC_API_KEY")
    }

    # Call the LLM provider abstraction
    try:
        response = LLMProvider.get_completion(model_id, messages, api_keys)
        llm_text = response["content"]
        total_tokens = response["total_tokens"]
    except ValueError as e:
        current_app.logger.error("LLM Provider error: %s", e)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        current_app.logger.error("LLM API error: %s", e)
        return jsonify({"error": "LLM API error", "details": str(e)}), 500

    # Look up (or create) the special LLM user with the model ID as username
    llm_user = User.query.filter_by(username=model_id).first()
    if not llm_user:
        llm_user = User(twitter_id=f"llm-{model_id}", username=model_id)
        db.session.add(llm_user)
        db.session.commit()

    # ---------- Redistribute tokens to the contributing (non-LLM) nodes ----------
    # Filter node_chain: only include nodes where node_type != 'llm'
    contributing_nodes = [n for n in node_chain if n.node_type != "llm"]
    if contributing_nodes and total_tokens:
        total_weight = sum(approximate_token_count(n.content) for n in contributing_nodes)
        for n in contributing_nodes:
            weight = approximate_token_count(n.content)
            # Calculate share proportionally (round if desired)
            share = int(round(total_tokens * (weight / total_weight))) if total_weight > 0 else 0
            # Add the share to distributed_tokens for that node.
            n.distributed_tokens += share
            db.session.add(n)
    # -----------------------------------------------------------------------------

    # Create the LLM node with token_count set to 0 and llm_model set
    llm_node = Node(
        user_id=llm_user.id,
        parent_id=parent_node.id,
        node_type="llm",
        llm_model=model_id,  # Store the model used
        content=llm_text,
        token_count=0  # No direct tokens assigned here.
    )
    db.session.add(llm_node)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.error("DB error saving LLM response: %s", e)
        return jsonify({"error": "DB error saving LLM response", "details": str(e)}), 500

    return jsonify({
        "message": "LLM response created",
        "node": {
            "id": llm_node.id,
            "content": llm_node.content,
            "token_count": llm_node.token_count,
            "created_at": llm_node.created_at.isoformat(),
            "username": model_id
        }
    }), 201


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
        content=additional_text,
        linked_node_id=linked_node_id
    )
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
            "content": new_node.content,
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
@voice_mode_required
def get_audio_urls(node_id):
    """Return JSON with URLs for original or TTS audio associated with a node.

    Response: 200 OK – `{ original_url: str|null, tts_url: str|null }`
              404     – when neither audio exists.
    """
    node = Node.query.get_or_404(node_id)
    if not node.audio_original_url and not node.audio_tts_url:
        return jsonify({"error": "No audio available for this node"}), 404
    return jsonify({
        "original_url": node.audio_original_url,
        "tts_url": node.audio_tts_url,
    }), 200


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

    # Simple safeguard: limit generation length.
    if len(node.content or "") > 10000:
        return jsonify({"error": "Content too long for TTS"}), 413

    # ------------------------------------------------------------------
    # Real TTS generation via OpenAI gpt-4o-mini-tts
    # ------------------------------------------------------------------
    api_key = current_app.config.get("OPENAI_API_KEY")
    if not api_key:
        return jsonify({"error": "TTS not configured (missing API key)"}), 500
    text = node.content or ""
    # Prevent overly long content
    if len(text) > 10000:
        return jsonify({"error": "Content too long for TTS"}), 413
    # Generate TTS via OpenAI Python SDK (streaming), chunking for text >4096 chars
    client = OpenAI(api_key=api_key)
    from pathlib import Path
    from pydub import AudioSegment

    # Prepare storage directory
    target_dir = AUDIO_STORAGE_ROOT / f"user/{node.user_id}/node/{node.id}"
    target_dir.mkdir(parents=True, exist_ok=True)
    final_path = Path(target_dir) / "tts.mp3"

    # Split text into word-boundary chunks <= 4096 chars
    def chunk_text(s, max_len=4096):
        parts = []
        while len(s) > max_len:
            idx = s.rfind(' ', 0, max_len)
            if idx <= 0:
                idx = max_len
            parts.append(s[:idx])
            s = s[idx:].lstrip()
        if s:
            parts.append(s)
        return parts

    chunks = chunk_text(text)
    try:
        if len(chunks) == 1:
            # Single chunk: direct streaming to MP3
            with client.audio.speech.with_streaming_response.create(
                model="gpt-4o-mini-tts",
                input=chunks[0],
                voice="alloy"
            ) as resp:
                resp.stream_to_file(final_path)
        else:
            # Multiple chunks: generate parts on disk and concatenate via pydub
            audio_parts = []
            for i, chunk in enumerate(chunks):
                part_path = Path(target_dir) / f"tts_part{i}.mp3"
                with client.audio.speech.with_streaming_response.create(
                    model="gpt-4o-mini-tts",
                    input=chunk,
                    voice="alloy"
                ) as resp:
                    resp.stream_to_file(part_path)
                # Load into AudioSegment from file path
                segment = AudioSegment.from_file(str(part_path), format="mp3")
                audio_parts.append(segment)
                # Cleanup part file
                part_path.unlink()
            # Concatenate all segments and export to final_path
            combined = sum(audio_parts)
            combined.export(final_path, format="mp3")
    except Exception as e:
        current_app.logger.error("TTS generation failed for node %s: %s", node_id, e)
        return jsonify({"error": "TTS generation failed", "details": str(e)}), 500

    # Update node record with new TTS URL
    rel_path = final_path.relative_to(AUDIO_STORAGE_ROOT)
    url = f"/media/{rel_path.as_posix()}"
    node.audio_tts_url = url
    # MP3 audio
    node.audio_mime_type = "audio/mpeg"
    db.session.add(node)
    db.session.commit()
    # Return the new TTS URL
    return jsonify({"message": "TTS generated", "tts_url": url}), 200


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
        from backend.celery_app import celery
        task = celery.AsyncResult(node.transcription_task_id)

        if task.state == 'PROGRESS':
            task_info = task.info  # Contains progress and status message
        elif task.state == 'SUCCESS':
            # Task completed but DB not updated yet
            node.transcription_status = 'completed'
            db.session.commit()

    return jsonify({
        "node_id": node.id,
        "status": node.transcription_status,
        "progress": node.transcription_progress or 0,
        "error": node.transcription_error,
        "started_at": node.transcription_started_at.isoformat() if node.transcription_started_at else None,
        "completed_at": node.transcription_completed_at.isoformat() if node.transcription_completed_at else None,
        "content": node.content if node.transcription_status == 'completed' else None,
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
    if node.llm_task_id and node.llm_task_status == 'processing':
        from backend.celery_app import celery
        task = celery.AsyncResult(node.llm_task_id)

        if task.state == 'PROGRESS':
            task_info = task.info
        elif task.state == 'SUCCESS':
            node.llm_task_status = 'completed'
            db.session.commit()

    return jsonify({
        "node_id": node.id,
        "status": node.llm_task_status,
        "progress": node.llm_task_progress or 0,
        "task_info": task_info
    })


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
    if node.tts_task_id and node.tts_task_status == 'processing':
        from backend.celery_app import celery
        task = celery.AsyncResult(node.tts_task_id)

        if task.state == 'PROGRESS':
            task_info = task.info
        elif task.state == 'SUCCESS':
            node.tts_task_status = 'completed'
            db.session.commit()

    return jsonify({
        "node_id": node.id,
        "status": node.tts_task_status,
        "progress": node.tts_task_progress or 0,
        "tts_url": node.audio_tts_url,
        "task_info": task_info
    })


# ---------------------------------------------------------------------------
# Media serving endpoint (simple/dev only – not for production)
# ---------------------------------------------------------------------------


@nodes_bp.route("/media/<path:filename>", methods=["GET"])
def serve_audio_file(filename):
    """Serve files from the AUDIO_STORAGE_ROOT with support for range requests.

    This is a **development‑only** helper to unblock tests.  In production the
    app would be served by the web server (e.g. nginx) or a cloud storage
    bucket.  Range requests are *not* implemented; whole file is returned.
    """
    file_path = AUDIO_STORAGE_ROOT / filename
    if not file_path.is_file():
        return jsonify({"error": "File not found"}), 404
    from flask import send_file

    return send_file(file_path)


@nodes_bp.route("/<int:node_id>", methods=["DELETE"])
@login_required
def delete_node(node_id):
    node = Node.query.get_or_404(node_id)
    # Only allow deletion if the current user is the creator 
    if node.user_id != current_user.id:
        return jsonify({"error": "Not authorized"}), 403

    # Update all children: set their parent_id to None
    # (This “orphans” the children so they become top‑level nodes.)
    try:
        Node.query.filter_by(parent_id=node.id).update({"parent_id": None})
        db.session.delete(node)
        db.session.commit()
        return jsonify({"message": "Node deleted successfully"}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Error deleting node", "details": str(e)}), 500
