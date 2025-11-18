from datetime import datetime
from flask_login import UserMixin
from backend.extensions import db

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    twitter_id = db.Column(db.String(64), unique=True, nullable=False)
    username = db.Column(db.String(64), unique=True, nullable=False)
    # A short description that the user may set (max 128 characters)
    description = db.Column(db.String(128), nullable=True, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    accepted_terms_at = db.Column(db.DateTime, nullable=True)
    # NEW: Approval status for our alpha (whitelisting) and optional email.
    approved = db.Column(db.Boolean, default=False, nullable=False)
    email = db.Column(db.String(128), nullable=True)
    
    # Relationship to text nodes
    nodes = db.relationship("Node", backref="user", lazy=True)

    # --- Voice‑Mode fields ---
    # Whether the user is an administrator.  Voice‑mode features are currently limited
    # to admins only.  (The column is kept optional for backward‑compatibility with
    # databases created before this change.)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)

    # Subscription plan ("free", "pro", etc.).  Used for future gating of Voice‑mode.
    plan = db.Column(db.String(16), nullable=False, default="free")

    def get_id(self):
        return str(self.id)

class Node(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=True)
    linked_node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=True)
    node_type = db.Column(db.String(16), nullable=False, default="user")
    # Model used to generate this node (only populated for node_type='llm')
    llm_model = db.Column(db.String(64), nullable=True)
    content = db.Column(db.Text, nullable=False)
    token_count = db.Column(db.Integer, nullable=True)
    # NEW: distributed_tokens will hold the portion of an LLM response allocated to this node’s author.
    distributed_tokens = db.Column(db.Integer, nullable=False, default=0)

    # -------------------------- Voice‑Mode columns ---------------------------
    # If the user recorded audio while creating this node, the file is stored
    # locally (or on S3) and the URL/path is saved here.  Null when no original
    # recording exists.
    audio_original_url = db.Column(db.String, nullable=True)

    # When a TTS version of the node is generated (only if the user did not
    # record original audio), the resulting file URL/path is stored here.
    audio_tts_url = db.Column(db.String, nullable=True)

    # Duration (in seconds) of the original recording or generated TTS.  Filled
    # asynchronously; may be null initially.
    audio_duration_sec = db.Column(db.Float, nullable=True)

    # MIME type of the stored audio (e.g. "audio/webm;codecs=opus").
    audio_mime_type = db.Column(db.String, nullable=True)

    # -------------------------- Async Task Tracking columns ---------------------------
    # Transcription task tracking
    transcription_status = db.Column(db.String(20), nullable=True)  # pending, processing, completed, failed
    transcription_task_id = db.Column(db.String(255), nullable=True)
    transcription_error = db.Column(db.Text, nullable=True)
    transcription_progress = db.Column(db.Integer, default=0)  # 0-100%
    transcription_started_at = db.Column(db.DateTime, nullable=True)
    transcription_completed_at = db.Column(db.DateTime, nullable=True)

    # LLM completion task tracking
    llm_task_id = db.Column(db.String(255), nullable=True)
    llm_task_status = db.Column(db.String(20), nullable=True)  # pending, processing, completed, failed
    llm_task_progress = db.Column(db.Integer, default=0)
    llm_task_error = db.Column(db.Text, nullable=True)

    # TTS generation task tracking
    tts_task_id = db.Column(db.String(255), nullable=True)
    tts_task_status = db.Column(db.String(20), nullable=True)  # pending, processing, completed, failed
    tts_task_progress = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Self–referential relationships…
    children = db.relationship("Node", backref=db.backref("parent", remote_side=[id]),
                               lazy=True, foreign_keys=[parent_id])
    linked_children = db.relationship("Node", backref=db.backref("linked_parent", remote_side=[id]),
                                      lazy=True, foreign_keys=[linked_node_id])

class NodeVersion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # Which node this version belongs to
    node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=False)
    # The prior content of the node
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    # Optional backreference, if you need to access versions from a Node instance:
    node = db.relationship("Node", backref="versions")

class UserProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # Which user this profile belongs to
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    # The profile content (generated by LLM or written by user)
    content = db.Column(db.Text, nullable=False)
    # Who generated/wrote this profile: "user" or model ID like "gpt-5", "claude-sonnet-4.5"
    generated_by = db.Column(db.String(64), nullable=False)
    # Number of tokens used to generate this profile (0 if user-written)
    tokens_used = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship back to user
    user = db.relationship("User", backref="profiles")
