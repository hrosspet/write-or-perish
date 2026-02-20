from datetime import datetime
from flask_login import UserMixin
from backend.extensions import db
from backend.utils.encryption import encrypt_content, decrypt_content

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    twitter_id = db.Column(db.String(64), unique=True, nullable=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    # A short description that the user may set (max 128 characters)
    description = db.Column(db.String(128), nullable=True, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    accepted_terms_at = db.Column(db.DateTime, nullable=True)
    accepted_terms_version = db.Column(db.String(16), nullable=True)
    # NEW: Approval status for our alpha (whitelisting) and optional email.
    approved = db.Column(db.Boolean, default=False, nullable=False)
    email = db.Column(db.String(128), nullable=True, unique=True)
    magic_link_token_hash = db.Column(db.String(128), nullable=True)
    magic_link_expires_at = db.Column(db.DateTime, nullable=True)
    deactivated_at = db.Column(db.DateTime, nullable=True)
    
    # Relationship to text nodes (explicit foreign_keys needed because Node has
    # multiple FKs pointing to User: user_id and pinned_by)
    nodes = db.relationship("Node", backref="user", lazy=True, foreign_keys="Node.user_id")

    # --- Voice‑Mode fields ---
    # Whether the user is an administrator.  Voice‑mode features are currently limited
    # to admins only.  (The column is kept optional for backward‑compatibility with
    # databases created before this change.)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)

    # Craft mode toggle — shows power-user features in the nav overflow menu
    craft_mode = db.Column(db.Boolean, default=False, nullable=False)

    # Subscription plan ("free", "alpha", "pro", etc.).
    plan = db.Column(db.String(16), nullable=False, default="alpha")

    # Concurrency guard: Celery task ID of in-flight profile generation
    profile_generation_task_id = db.Column(db.String(255), nullable=True)

    # All valid subscription plans (single source of truth).
    ALLOWED_PLANS = {"free", "alpha", "pro"}

    # Plans that grant Voice-Mode access (any non-free plan).
    VOICE_MODE_PLANS = {"alpha", "pro"}

    @property
    def has_voice_mode(self):
        """Whether this user can access Voice-Mode features."""
        if self.is_admin:
            return True
        return (self.plan or "free") in self.VOICE_MODE_PLANS

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

    # Pin-to-profile: surfaces any node on Dashboard & Feed
    pinned_at = db.Column(db.DateTime, nullable=True)
    pinned_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Self–referential relationships…
    children = db.relationship("Node", backref=db.backref("parent", remote_side=[id]),
                               lazy=True, foreign_keys=[parent_id])
    linked_children = db.relationship("Node", backref=db.backref("linked_parent", remote_side=[id]),
                                      lazy=True, foreign_keys=[linked_node_id])

    def set_content(self, plaintext: str):
        """Set content with encryption."""
        self.content = encrypt_content(plaintext)

    def get_content(self) -> str:
        """Get decrypted content."""
        return decrypt_content(self.content)

class NodeVersion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # Which node this version belongs to
    node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=False)
    # The prior content of the node
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    # Optional backreference, if you need to access versions from a Node instance:
    node = db.relationship("Node", backref="versions")

    def set_content(self, plaintext: str):
        """Set content with encryption."""
        self.content = encrypt_content(plaintext)

    def get_content(self) -> str:
        """Get decrypted content."""
        return decrypt_content(self.content)

class Draft(db.Model):
    """
    Temporary storage for auto-saved drafts.
    Drafts are private - only visible to the user who created them.
    Deleted when the user saves or discards their work.

    Also used for streaming transcription sessions - audio chunks are
    uploaded and transcribed in real-time, with transcript text appended
    to the draft content as each chunk completes.
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

    # Streaming transcription fields
    session_id = db.Column(db.String(36), nullable=True, unique=True, index=True)  # UUID for streaming session
    streaming_status = db.Column(db.String(20), nullable=True)  # recording, finalizing, completed, failed
    streaming_total_chunks = db.Column(db.Integer, nullable=True)  # Total chunks expected (set on finalize)
    streaming_completed_chunks = db.Column(db.Integer, default=0)  # Chunks transcribed so far
    # Privacy settings for when this draft becomes a node
    privacy_level = db.Column(db.String(20), nullable=True)
    ai_usage = db.Column(db.String(20), nullable=True)

    # Relationships
    user = db.relationship("User", backref="drafts")
    node = db.relationship("Node", foreign_keys=[node_id])
    parent = db.relationship("Node", foreign_keys=[parent_id])

    def set_content(self, plaintext: str):
        """Set content with encryption."""
        self.content = encrypt_content(plaintext)

    def get_content(self) -> str:
        """Get decrypted content."""
        return decrypt_content(self.content)


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

    # Cumulative source data tokens the profile is based on
    source_tokens_used = db.Column(db.Integer, nullable=True, default=0)
    # Timestamp cursor: created_at of last included Node
    source_data_cutoff = db.Column(db.DateTime, nullable=True)
    # Distinguishes initial, update, iterative, and user edits
    generation_type = db.Column(db.String(16), nullable=True, default="initial")
    # Links to previous profile version used as input (chain tracing)
    parent_profile_id = db.Column(
        db.Integer, db.ForeignKey("user_profile.id"), nullable=True
    )

    # --- Voice‑Mode fields ---
    audio_tts_url = db.Column(db.String, nullable=True)
    tts_task_id = db.Column(db.String(255), nullable=True)
    tts_task_status = db.Column(db.String(20), nullable=True)
    tts_task_progress = db.Column(db.Integer, default=0)

    # Relationship back to user
    user = db.relationship("User", backref="profiles")
    parent_profile = db.relationship(
        "UserProfile", remote_side="UserProfile.id", uselist=False
    )

    def set_content(self, plaintext: str):
        """Set content with encryption."""
        self.content = encrypt_content(plaintext)

    def get_content(self) -> str:
        """Get decrypted content."""
        return decrypt_content(self.content)


class UserTodo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    generated_by = db.Column(db.String(64), nullable=False)
    tokens_used = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    privacy_level = db.Column(db.String(16), nullable=False, default="private")
    ai_usage = db.Column(db.String(16), nullable=False, default="chat")

    user = db.relationship("User", backref="todos")

    def set_content(self, plaintext):
        self.content = encrypt_content(plaintext)

    def get_content(self):
        return decrypt_content(self.content)


class NodeTranscriptChunk(db.Model):
    """
    Stores individual transcript chunks for streaming transcription.
    During streaming recording, each 5-minute audio chunk is transcribed
    independently and stored here. When recording is finalized, these
    chunks are assembled into the final node/draft content.

    Can be associated with either:
    - A session_id (for draft-based streaming, before node is created)
    - A node_id (legacy, or after draft is saved as node)
    """
    id = db.Column(db.Integer, primary_key=True)
    # Which node this chunk belongs to (nullable for draft-based streaming)
    node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=True)
    # Session ID for draft-based streaming (links to Draft.session_id)
    session_id = db.Column(db.String(36), nullable=True, index=True)
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

    # Unique constraint: one chunk per index per session OR per node
    __table_args__ = (
        db.UniqueConstraint('session_id', 'chunk_index', name='uq_session_chunk_index'),
        db.UniqueConstraint('node_id', 'chunk_index', name='uq_node_chunk_index'),
    )

    def set_text(self, plaintext: str):
        """Set text with encryption."""
        self.text = encrypt_content(plaintext)

    def get_text(self) -> str:
        """Get decrypted text."""
        return decrypt_content(self.text)


class APICostLog(db.Model):
    __tablename__ = "api_cost_log"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    model_id = db.Column(db.String(64), nullable=False)
    request_type = db.Column(db.String(32), nullable=False)
    input_tokens = db.Column(db.Integer, nullable=True)
    output_tokens = db.Column(db.Integer, nullable=True)
    audio_duration_seconds = db.Column(db.Float, nullable=True)
    cost_microdollars = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    user = db.relationship("User", backref="api_cost_logs")


class TTSChunk(db.Model):
    """
    Stores individual TTS audio chunk URLs for streaming TTS playback.
    When TTS is generated, each text chunk produces an audio file that
    can be played immediately while subsequent chunks are still generating.
    """
    id = db.Column(db.Integer, primary_key=True)
    # Which node or profile this TTS chunk belongs to (one must be set)
    node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("user_profile.id"), nullable=True)
    # Zero-based index of the chunk
    chunk_index = db.Column(db.Integer, nullable=False)
    # URL to the audio chunk file
    audio_url = db.Column(db.String, nullable=True)
    # Duration of the audio chunk in seconds (from pydub/ffprobe)
    duration = db.Column(db.Float, nullable=True)
    # Status of this chunk's generation
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending, processing, completed, failed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

    # Relationships
    node = db.relationship("Node", backref="tts_chunks")
    profile = db.relationship("UserProfile", backref="tts_chunks")

    # Unique constraints: one chunk per index per node/profile
    __table_args__ = (
        db.UniqueConstraint('node_id', 'chunk_index', name='uq_node_tts_chunk_index'),
        db.UniqueConstraint('profile_id', 'chunk_index', name='uq_profile_tts_chunk_index'),
    )
