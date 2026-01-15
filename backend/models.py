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
    # NEW: distributed_tokens will hold the portion of an LLM response allocated to this node's author.
    distributed_tokens = db.Column(db.Integer, nullable=False, default=0)

    # Privacy level: controls who can access this node
    # - private: Only the owner can read (default for new nodes)
    # - circles: Shared with specific user-defined groups (future feature)
    # - public: Visible to all users
    privacy_level = db.Column(db.String(16), nullable=False, default="private")

    # AI usage permission: controls how AI can use this node's content
    # - none: No AI usage allowed
    # - chat: AI can use for generating responses (not for training)
    # - train: AI can use for training data
    ai_usage = db.Column(db.String(16), nullable=False, default="none")

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

    # Streaming transcription fields
    # Indicates this node is using streaming transcription mode
    streaming_transcription = db.Column(db.Boolean, default=False)
    # Total expected chunks (set when recording starts with known interval)
    streaming_total_chunks = db.Column(db.Integer, nullable=True)
    # Number of chunks that have completed transcription
    streaming_completed_chunks = db.Column(db.Integer, default=0)
    # Session ID for streaming upload (groups chunks together)
    streaming_session_id = db.Column(db.String(64), nullable=True)

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

class Draft(db.Model):
    """
    Temporary storage for auto-saved drafts.
    Drafts are private - only visible to the user who created them.
    Deleted when the user saves or discards their work.
    """
    id = db.Column(db.Integer, primary_key=True)
    # The user who owns this draft
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    # If editing an existing node, store its ID; null for new node drafts
    node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=True)
    # Parent node ID for new node creation drafts
    parent_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=True)
    # The draft content
    content = db.Column(db.Text, nullable=False, default="")
    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship("User", backref="drafts")
    node = db.relationship("Node", foreign_keys=[node_id])
    parent = db.relationship("Node", foreign_keys=[parent_id])


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

    # Privacy level: controls who can access this profile
    # Default for profiles is 'private' (only owner can see)
    privacy_level = db.Column(db.String(16), nullable=False, default="private")

    # AI usage permission: controls how AI can use this profile's content
    # Default for profiles is 'chat' (AI can use for responses)
    ai_usage = db.Column(db.String(16), nullable=False, default="chat")

    # --- Voice‑Mode fields ---
    audio_tts_url = db.Column(db.String, nullable=True)
    tts_task_id = db.Column(db.String(255), nullable=True)
    tts_task_status = db.Column(db.String(20), nullable=True)
    tts_task_progress = db.Column(db.Integer, default=0)

    # Relationship back to user
    user = db.relationship("User", backref="profiles")


class NodeTranscriptChunk(db.Model):
    """
    Stores individual transcript chunks for streaming transcription.
    During streaming recording, each 5-minute audio chunk is transcribed
    independently and stored here. When recording is finalized, these
    chunks are assembled into the final node content.
    """
    id = db.Column(db.Integer, primary_key=True)
    # Which node this chunk belongs to
    node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=False)
    # Zero-based index of the chunk
    chunk_index = db.Column(db.Integer, nullable=False)
    # Transcribed text for this chunk
    text = db.Column(db.Text, nullable=True)
    # Status of this chunk's transcription
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending, processing, completed, failed
    # Error message if transcription failed
    error = db.Column(db.Text, nullable=True)
    # Celery task ID for tracking
    task_id = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

    # Relationship back to node
    node = db.relationship("Node", backref="transcript_chunks")

    # Unique constraint: one chunk per index per node
    __table_args__ = (
        db.UniqueConstraint('node_id', 'chunk_index', name='uq_node_chunk_index'),
    )


class TTSChunk(db.Model):
    """
    Stores individual TTS audio chunk URLs for streaming TTS playback.
    When TTS is generated, each text chunk produces an audio file that
    can be played immediately while subsequent chunks are still generating.
    """
    id = db.Column(db.Integer, primary_key=True)
    # Which node this TTS chunk belongs to
    node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=False)
    # Zero-based index of the chunk
    chunk_index = db.Column(db.Integer, nullable=False)
    # URL to the audio chunk file
    audio_url = db.Column(db.String, nullable=True)
    # Status of this chunk's generation
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending, processing, completed, failed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

    # Relationship back to node
    node = db.relationship("Node", backref="tts_chunks")

    # Unique constraint: one chunk per index per node
    __table_args__ = (
        db.UniqueConstraint('node_id', 'chunk_index', name='uq_node_tts_chunk_index'),
    )
