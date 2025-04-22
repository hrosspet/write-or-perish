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


# Allowed extensions and max size (in bytes) – 100 MB.
ALLOWED_EXTENSIONS = {"webm", "wav", "m4a"}
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
        )
        db.session.add(node)
        db.session.commit()  # Need node.id for the file path.

        # Save the audio file now that we know node.id
        url = _save_audio_file(file, current_user.id, node.id, "original")
        node.audio_original_url = url
        node.audio_mime_type = file.mimetype
        db.session.add(node)
        db.session.commit()

        # -- Speech-to-text transcription via OpenAI
        api_key = current_app.config.get("OPENAI_API_KEY")
        if api_key:
            client = OpenAI(api_key=api_key)
            # Derive local file path from the stored URL
            rel_path = node.audio_original_url.replace("/media/", "")
            local_path = AUDIO_STORAGE_ROOT / rel_path
            try:
                with open(local_path, "rb") as audio_file:
                    # Transcribe audio to text
                    resp = client.audio.transcriptions.create(
                        model="gpt-4o-transcribe",
                        file=audio_file,
                        response_format="text"
                    )
                # Extract transcript text
                transcript = None
                if hasattr(resp, "text"):
                    transcript = resp.text
                elif isinstance(resp, dict):
                    transcript = resp.get("text") or resp.get("transcript")
                else:
                    transcript = str(resp)
                # Update node content with transcript
                node.content = transcript or node.content
                db.session.add(node)
                db.session.commit()
            except Exception as e:
                current_app.logger.error("Transcription error for node %s: %s", node.id, e)

        return jsonify({
            "id": node.id,
            "audio_original_url": node.audio_original_url,
            "content": node.content,
            "node_type": node.node_type,
            "created_at": node.created_at.isoformat(),
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

# Request an LLM response based on the thread (the ancestors’ texts are joined as a prompt).
@nodes_bp.route("/<int:node_id>/llm", methods=["POST"])
@login_required
def request_llm_response(node_id):
    parent_node = Node.query.get_or_404(node_id)

    # Build the chain of nodes (from top‐level to current)
    node_chain = []
    current = parent_node
    while current:
        node_chain.insert(0, current)
        current = current.parent

    model_name = os.environ.get("LLM_NAME")
    messages = []
    for node in node_chain:
        author = node.user.username if node.user else "Unknown"
        # For the LLM user, use the "assistant" role; otherwise, "user."
        if author == model_name:
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

    api_key = current_app.config.get("OPENAI_API_KEY")
    # (Initialize your OpenAI client as before)
    client = OpenAI(api_key=api_key)
    
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            response_format={"type": "text"},
            temperature=1,
            max_completion_tokens=10000,
            top_p=1,
            frequency_penalty=0,
            presence_penalty=0,
        )
    except Exception as e:
        current_app.logger.error("OpenAI API error: %s", e)
        return jsonify({"error": "OpenAI API error", "details": str(e)}), 500

    try:
        llm_text = response.choices[0].message.content
    except Exception as e:
        current_app.logger.error("Error extracting LLM text: %s", e)
        return jsonify({"error": "Error parsing LLM response"}), 500

    # Extract the total token count from the response.
    total_tokens = response.usage.total_tokens if response.usage else 0

    # Look up (or create) the special LLM user.
    from backend.models import User
    llm_user = User.query.filter_by(username=model_name).first()
    if not llm_user:
        llm_user = User(twitter_id="llm", username=model_name)
        db.session.add(llm_user)
        db.session.commit()

    # ---------- Redistribute tokens to the contributing (non-LLM) nodes ----------
    # Filter node_chain: only include nodes where the user is NOT the LLM user.
    contributing_nodes = [n for n in node_chain if n.user.username != model_name]
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
    
    # Create the LLM node with token_count set to 0 so no tokens are credited to the LLM user.
    llm_node = Node(
        user_id=llm_user.id,
        parent_id=parent_node.id,
        node_type="llm",
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
            "username": model_name
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
    # Generate TTS via OpenAI Python SDK (streaming)
    client = OpenAI(api_key=api_key)
    from pathlib import Path
    target_dir = AUDIO_STORAGE_ROOT / f"user/{node.user_id}/node/{node.id}"
    target_dir.mkdir(parents=True, exist_ok=True)
    # Save TTS output as MP3
    target_path = Path(target_dir) / "tts.mp3"
    try:
        with client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            input=text,
            voice="alloy"
        ) as response:
            response.stream_to_file(target_path)
    except Exception as e:
        current_app.logger.error("TTS generation failed for node %s: %s", node_id, e)
        return jsonify({"error": "TTS generation failed", "details": str(e)}), 500

    # Update node record with new TTS URL
    rel_path = target_path.relative_to(AUDIO_STORAGE_ROOT)
    url = f"/media/{rel_path.as_posix()}"
    node.audio_tts_url = url
    # MP3 audio
    node.audio_mime_type = "audio/mpeg"
    db.session.add(node)
    db.session.commit()
    # Return the new TTS URL
    return jsonify({"message": "TTS generated", "tts_url": url}), 200


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
