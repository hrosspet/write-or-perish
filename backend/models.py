from datetime import datetime
from flask_login import UserMixin
from sqlalchemy import or_
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

    # Preferred AI model for craft-mode operations
    preferred_model = db.Column(db.String(64), nullable=True)

    # IANA timezone name (e.g. "Europe/Prague") captured from the browser via
    # Intl.DateTimeFormat().resolvedOptions().timeZone. Used to render absolute
    # local-time stamps in the LLM context (#130). Defaults to "UTC" when the
    # client hasn't reported one.
    timezone = db.Column(
        db.String(64), nullable=True, default="UTC", server_default="UTC"
    )

    # Subscription plan ("free", "alpha", "pro", etc.).
    plan = db.Column(db.String(16), nullable=False, default="alpha")

    # Per-user monthly spend hard cap (issue: free-alpha guardrail). Stores the
    # "YYYY-MM" (UTC) in which the user crossed PER_USER_MONTHLY_LIMIT_USD. While
    # this equals the current month the user is blocked from cost-incurring
    # actions; it auto-clears at month rollover (stored month no longer matches).
    spend_blocked_month = db.Column(db.String(7), nullable=True)

    # Per-user override for the monthly spend cap (USD). NULL means "use the
    # global PER_USER_MONTHLY_LIMIT_USD default"; a stored value (incl. 0 for
    # "uncapped") takes precedence. Admin-editable from the dashboard so a
    # single user can be granted more (or less) headroom without touching the
    # global env var. See backend/utils/spend.get_user_spend_limit_usd.
    monthly_spend_limit_usd = db.Column(db.Float, nullable=True)

    # Per-user defaults for new nodes (overridable per-node)
    default_privacy_level = db.Column(
        db.String(16), nullable=False, default="private",
        server_default="private"
    )
    default_ai_usage = db.Column(
        db.String(16), nullable=False, default="chat",
        server_default="chat"
    )

    # Concurrency guard: Celery task ID of in-flight profile generation
    profile_generation_task_id = db.Column(db.String(255), nullable=True)
    # When the profile generation task was dispatched (for staleness detection)
    profile_generation_task_dispatched_at = db.Column(db.DateTime, nullable=True)

    # Flag: next profile generation should be a full regen (not incremental)
    profile_needs_full_regen = db.Column(db.Boolean, nullable=False, default=False)

    # --- Profile-generation batch pipeline (issue #173, Part A) ---
    # True while a batch request for this user's profile is in flight
    # (one step at a time — chunks are sequential).
    profile_batch_pending = db.Column(
        db.Boolean, nullable=False, default=False, server_default="false")
    # Consecutive batch failures for the current step (bounds retries).
    profile_batch_attempts = db.Column(
        db.Integer, nullable=False, default=0, server_default="0")

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

    @classmethod
    def profile_eligible_query(cls):
        """Users eligible for automatic profile / recent-context updates:
        approved, active accounts on a Voice-Mode plan, minus the synthetic
        ``llm-<model>`` placeholder accounts.

        Three non-obvious guards:

        * ``approved`` gates all access (login + terms) and is flipped to
          ``False`` by admin deactivation. Excluding non-approved users
          stops us spending LLM budget building profiles nobody can open —
          and makes deactivation an effective "pause this user" switch.

        * ``default_ai_usage`` must be AI-readable ('chat'/'train'). A user
          who set their global AI-usage to 'none' has opted out, so we must
          not auto-generate (or refresh) an AI-readable profile / recent
          context from their writing. Node-level ``filter_ai_usage`` already
          keeps opted-out *content* out of the input, but this stops the
          generation from running at all for opted-out accounts.

        * The synthetic LLM placeholder accounts are excluded by their
          canonical signal — they author ``node_type == "llm"`` nodes —
          NOT by a ``twitter_id`` prefix. The prefix is unreliable: the
          convention has drifted across vintages (bare ``"llm"``,
          ``"llm-<model>"``), so ``~twitter_id.like('llm-%')`` silently let
          older accounts (e.g. ``twitter_id='llm'``) through and treated them
          as real users — eligible for profile gen, which mis-attributed cost
          to a placeholder account. Node authorship catches every vintage and
          still KEEPS email / magic-link humans (``twitter_id`` NULL), who
          never author LLM nodes.

        Shared helper (not an inline filter) so the profile and
        recent-context tasks can't drift apart again.
        """
        from backend.utils.privacy import AI_ALLOWED
        llm_authors = db.session.query(Node.user_id).filter(
            Node.node_type == "llm", Node.user_id.isnot(None)
        ).distinct()
        return cls.query.filter(
            cls.approved.is_(True),
            cls.plan.in_(list(cls.VOICE_MODE_PLANS)),
            cls.default_ai_usage.in_(list(AI_ALLOWED)),
            ~cls.id.in_(llm_authors),
        )


class ProfileBatchJob(db.Model):
    """A submitted provider batch covering one profile-generation step for one
    or more users (issue #173, Part A).

    A single batch is heterogeneous — it can carry "chunk" items for users
    mid-rebuild and "integration" items for users who just finished — so the
    per-item metadata the poller needs to save each result lives in `items`
    (keyed by custom_id), not on the job. The poller retrieves `batch_id`,
    collects results, and routes each one back to its user via `items`.
    """
    __tablename__ = "profile_batch_job"

    id = db.Column(db.Integer, primary_key=True)
    # "anthropic" | "openai:<api_model>"
    provider_key = db.Column(db.String(64), nullable=False)
    batch_id = db.Column(db.String(255), nullable=False, index=True)
    # "pending" | "collected" | "failed"
    status = db.Column(db.String(16), nullable=False, default="pending")
    # List of per-item dicts, each:
    #   {custom_id, user_id, prev_profile_id, chunk_num, kind,
    #    source_data_cutoff (iso str|None), cumulative_source_tokens, model_id}
    items = db.Column(db.JSON, nullable=False, default=list)
    submitted_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow)
    collected_at = db.Column(db.DateTime, nullable=True)


class Node(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=True)
    human_owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    linked_node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=True)
    node_type = db.Column(db.String(16), nullable=False, default="user")
    # Model used to generate this node (only populated for node_type='llm')
    llm_model = db.Column(db.String(64), nullable=True)
    content = db.Column(db.Text, nullable=True)
    token_count = db.Column(db.Integer, nullable=False, default=0)
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
    # JSON list of user-facing warnings emitted during the task
    # (e.g. unrecognized {user_export} param keys). Surfaced to the
    # frontend via /nodes/<id>/llm-status and rendered as toasts by
    # useLlmTaskWarnings. Generic — any future task can append via
    # backend.utils.task_warnings.record_task_warning.
    llm_task_warnings = db.Column(db.Text, nullable=True)

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

    # Tool call metadata for LLM nodes (JSON list of tool call logs)
    tool_calls_meta = db.Column(db.Text, nullable=True)

    # Within-turn retrieval chaining (#158): when a text-mode LLM node makes
    # a retrieval tool call, it is finalized as an interim node and a
    # continuation node is created to hold the answer. This self-FK links the
    # interim node to its continuation so callers can follow the chain to the
    # final answer within a single turn.
    continuation_node_id = db.Column(
        db.Integer, db.ForeignKey("node.id"), nullable=True
    )

    # Pin-to-profile: surfaces any node on Dashboard & Feed
    pinned_at = db.Column(db.DateTime, nullable=True)
    pinned_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    # Soft-delete: non-NULL means scheduled for cleanup after SOFT_DELETE_GRACE_DAYS.
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)

    # Stable per-message identity used to deduplicate imported data.
    # Scoped per importing human (see import_data.py). NULL for
    # natively-created nodes. Keys are short ("chatgpt:<uuid>") or a
    # sha256 hex digest, so they always fit String(255).
    source_key = db.Column(db.String(255), nullable=True, index=True)

    # DB-level backstop against concurrent imports racing past the
    # application dedup check. NULL source_key rows are exempt (both
    # PostgreSQL and SQLite treat NULLs as distinct in unique indexes).
    __table_args__ = (
        db.UniqueConstraint(
            "human_owner_id", "source_key",
            name="uq_node_human_owner_source_key",
        ),
    )

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Self–referential relationships…
    children = db.relationship("Node", backref=db.backref("parent", remote_side=[id]),
                               lazy=True, foreign_keys=[parent_id])
    linked_children = db.relationship("Node", backref=db.backref("linked_parent", remote_side=[id]),
                                      lazy=True, foreign_keys=[linked_node_id])
    # ----- Artifact helpers ------------------------------------------------

    @property
    def is_system_prompt(self):
        """True when this node carries a prompt artifact."""
        return self.has_artifact("prompt")

    def get_artifact_row(self, artifact_type):
        """Return the NodeContextArtifact row for *artifact_type*, or None."""
        for a in self.context_artifacts:
            if a.artifact_type == artifact_type:
                return a
        return None

    def get_artifact_id(self, artifact_type):
        """Return the artifact PK for *artifact_type*, or None."""
        row = self.get_artifact_row(artifact_type)
        return row.artifact_id if row else None

    def has_artifact(self, artifact_type):
        return self.get_artifact_row(artifact_type) is not None

    def get_user_artifacts(self):
        """Load pinned UserArtifact instances ({kind: artifact}).

        Multiple "user_artifact" rows may exist per node (one per kind,
        pinned at session start — #191).
        """
        result = {}
        for a in self.context_artifacts:
            if a.artifact_type != "user_artifact":
                continue
            row = UserArtifact.query.get(a.artifact_id)
            if row is not None:
                result[row.kind] = row
        return result

    def get_artifact(self, artifact_type):
        """Load and return the actual model instance for *artifact_type*."""
        row = self.get_artifact_row(artifact_type)
        if row is None:
            return None
        if artifact_type == "prompt":
            return UserPrompt.query.get(row.artifact_id)
        if artifact_type == "profile":
            return UserProfile.query.get(row.artifact_id)
        if artifact_type == "todo":
            return UserTodo.query.get(row.artifact_id)
        if artifact_type == "recent_context":
            return UserRecentContext.query.get(row.artifact_id)
        # ai_preferences is a UserArtifact now (#158 Slice 5), resolved via
        # get_user_artifacts() like other artifact kinds — not here.
        return None

    # ----- Content ---------------------------------------------------------

    def set_content(self, plaintext: str):
        """Set content with encryption."""
        self.content = encrypt_content(plaintext)

    def get_content(self) -> str:
        """Get decrypted content. Resolves from linked UserPrompt if set."""
        # Artifact-based prompt resolution
        prompt_artifact = self.get_artifact_row("prompt")
        if prompt_artifact is not None:
            prompt = UserPrompt.query.get(prompt_artifact.artifact_id)
            return prompt.get_content() if prompt else ""
        if self.content is None:
            return ""
        return decrypt_content(self.content)

class NodeContextArtifact(db.Model):
    """Generic join table linking nodes to versioned context artifacts.

    artifact_type is one of: "prompt", "profile", "todo"
    artifact_id references the PK in the corresponding table
    (UserPrompt, UserProfile, UserTodo).
    """
    __tablename__ = "node_context_artifact"
    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(
        db.Integer,
        db.ForeignKey("node.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    artifact_type = db.Column(db.String(32), nullable=False)
    artifact_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    node = db.relationship(
        "Node", backref="context_artifacts", cascade="all,delete-orphan",
        single_parent=True,
    )

    # artifact_id is part of the key because "user_artifact" is a multi-row
    # type (#158) — one pinned row per artifact kind on the same node. A
    # (node_id, artifact_type) unique constraint wrongly forbade that and
    # 500'd attach_context_artifacts whenever a user had 2+ artifact kinds.
    # Including artifact_id still prevents exact-duplicate pins while allowing
    # the per-kind rows (single-row types pin one artifact_id, so unaffected).
    __table_args__ = (
        db.UniqueConstraint(
            'node_id', 'artifact_type', 'artifact_id',
            name='uq_node_artifact_type_id',
        ),
    )


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
    # Family-only audio MIME ('audio/webm' or 'audio/mp4') chosen by the
    # frontend MediaRecorder negotiation. Set on chunk 0 after the init
    # segment is successfully parsed; chunks 1+ are rejected if their mime
    # family disagrees. Default 'audio/webm' keeps in-flight pre-deploy
    # sessions on their existing path.
    streaming_mime_type = db.Column(db.String(64), nullable=True, default='audio/webm')
    # Workflow label: 'Reflect', 'Orient', etc. Set on init for recovery UI.
    label = db.Column(db.String(20), nullable=True)
    # Privacy settings for when this draft becomes a node
    privacy_level = db.Column(db.String(20), nullable=True)
    ai_usage = db.Column(db.String(20), nullable=True)

    # Server-side LLM generation: stores the LLM node ID when the finalize
    # task kicks off LLM + TTS server-side (lock-screen workflow).
    llm_node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=True)

    # User-facing warning emitted during the streaming finalize task —
    # e.g. a misconfigured {user_export} placeholder rejected before LLM
    # dispatch. Surfaced via the SSE all_complete event and rendered as
    # a toast by the voice frontend. Generic; any abort-with-message
    # condition can use it.
    streaming_warning = db.Column(db.Text, nullable=True)

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
    # Default is 'chat' — profiles are AI-generated and need to be readable by AI
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


class UserRecentContext(db.Model):
    """Auto-generated summary of recent user activity since last profile update.

    Sits between the long-term UserProfile (~monthly) and raw data,
    providing ~500-800 token summaries regenerated every ~10k new tokens.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    generated_by = db.Column(db.String(64), nullable=False)
    tokens_used = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Timestamp of newest Node included in this summary
    source_data_cutoff = db.Column(db.DateTime, nullable=True)
    # Cumulative tokens since last profile update covered by this summary
    source_tokens_covered = db.Column(db.Integer, nullable=True, default=0)
    # Which profile this supplements (null if no profile exists yet)
    profile_id = db.Column(
        db.Integer, db.ForeignKey("user_profile.id"), nullable=True
    )
    # AI-generated context summary — needs to be readable by AI
    ai_usage = db.Column(db.String(16), nullable=False, default="chat")

    user = db.relationship("User", backref="recent_contexts")
    profile = db.relationship("UserProfile", backref="recent_contexts")

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
    # Todo is used as AI context in Voice sessions — must be AI-readable
    ai_usage = db.Column(db.String(16), nullable=False, default="chat")

    user = db.relationship("User", backref="todos")

    def set_content(self, plaintext):
        self.content = encrypt_content(plaintext)

    def get_content(self):
        return decrypt_content(self.content)


class UserAIPreferences(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    generated_by = db.Column(db.String(64), nullable=False)
    tokens_used = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    privacy_level = db.Column(db.String(16), nullable=False, default="private")
    # AI preferences are used as context in LLM calls — must be AI-readable
    ai_usage = db.Column(db.String(16), nullable=False, default="chat")

    user = db.relationship("User", backref="ai_preferences")

    def set_content(self, plaintext):
        self.content = encrypt_content(plaintext)

    def get_content(self):
        return decrypt_content(self.content)


class UserArtifact(db.Model):
    """Generic named user artifact (issue #158): "memory", "scratchpad",
    and user/LLM-created artifacts.

    Append-only versioning like UserAIPreferences — each update inserts a
    new row; the latest row per (user_id, kind) is current. Content is
    encrypted at rest and included in data exports as a default Loore
    artifact.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    # Slug identifying the artifact across versions, e.g. "memory".
    kind = db.Column(db.String(48), nullable=False, index=True)
    title = db.Column(db.String(128), nullable=False)
    # One-line summary of what this artifact is for / contains. Powers the
    # agentic artifacts index (so the AI can judge what to read) and the nav
    # dropdown subtitle. Carried forward across versions like title.
    description = db.Column(db.String(255), nullable=True)
    content = db.Column(db.Text, nullable=False)
    generated_by = db.Column(db.String(64), nullable=False)
    tokens_used = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    privacy_level = db.Column(db.String(16), nullable=False, default="private")
    # Artifacts are LLM working memory — must be AI-readable to function.
    ai_usage = db.Column(db.String(16), nullable=False, default="chat")

    user = db.relationship("User", backref="artifacts")

    # Default artifacts every user has (created lazily on first write).
    # "predictions" is surfaced in the UI as an empty editable artifact, but
    # has no dedicated agentic-prompt wiring yet (no {user_predictions}
    # placeholder) — it behaves like any custom artifact until written.
    DEFAULT_KINDS = {
        "memory": "Memory",
        "scratchpad": "Scratchpad",
        "predictions": "Predictions",
        # AI interaction preferences folded into the artifact model (#158
        # Slice 5). Always-inline (its own {user_ai_preferences} placeholder),
        # so it's excluded from the agentic index — see ALWAYS_INLINE_KINDS.
        "ai_preferences": "AI Interaction Preferences",
        # Long-running aspirations the AI helps clarify and track (#150).
        # Always-inline (its own {user_intentions} placeholder), so it's
        # excluded from the agentic index — see ALWAYS_INLINE_KINDS.
        "intentions": "Intentions",
    }

    # Pre-filled one-line descriptions for the built-in kinds. Custom kinds
    # get their description from whoever creates them (the AI sets it when it
    # creates a new kind; the user can set it in the create modal).
    # "{name}" is substituted with the user's username when served/rendered.
    DEFAULT_DESCRIPTIONS = {
        "memory": "Durable facts about {name}, remembered across sessions.",
        "scratchpad": "Working notes for ongoing threads — where we left off, open questions.",
        "predictions": "Predictions you want to record and revisit over time.",
        "ai_preferences": "How {name} wants the AI to interact with them — tone, style, boundaries.",
        "intentions": "Long-running aspirations {name} wants to hold — the AI helps clarify and track them.",
    }

    # Kinds rendered INLINE in the agentic system prompt — each has its own
    # {user_<kind>} placeholder, so it's injected directly (not via the
    # artifacts index) and the UI must resolve it for display. Single source
    # of truth: the LLM context builder (ALWAYS_INLINE_KINDS) and the node
    # display serializer both read this, so adding an inline kind can't
    # silently desync one side (which once left {user_intentions} unrendered).
    INLINE_KINDS = ("memory", "scratchpad", "ai_preferences", "intentions")

    def set_content(self, plaintext):
        self.content = encrypt_content(plaintext)

    def get_content(self):
        return decrypt_content(self.content)

    @classmethod
    def latest_for(cls, user_id, kind):
        return cls.query.filter_by(user_id=user_id, kind=kind).order_by(
            cls.created_at.desc(), cls.id.desc()).first()

    @classmethod
    def latest_per_kind(cls, user_id):
        """Latest version of every artifact kind the user has, as a dict."""
        rows = cls.query.filter_by(user_id=user_id).order_by(
            cls.created_at.asc(), cls.id.asc()).all()
        latest = {}
        for row in rows:
            latest[row.kind] = row  # later rows overwrite earlier
        return latest


class UserFeedback(db.Model):
    """User feedback proposed by the AI (under a ### Feedback heading) and
    sent only after the user confirms — via the Send button
    (/api/feedback/submit) or the apply_feedback tool (#158). For batch
    triage by the creators."""
    __tablename__ = "user_feedback"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(32), nullable=False, default="general")
    source = db.Column(db.String(16), nullable=False, default="llm")
    status = db.Column(db.String(16), nullable=False, default="new")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    user = db.relationship("User", backref="feedback_items")

    def set_content(self, plaintext):
        self.content = encrypt_content(plaintext)

    def get_content(self):
        return decrypt_content(self.content)


class ShareDraft(db.Model):
    """A shareable piece extracted from the user's writing (Feature 3 —
    Upload/Share, dark behind SHARE_V1).

    Two-stage consent, structurally enforced: the AI PROPOSES a share under
    ### Share headings in conversation; confirming there (Save button or the
    apply_share tool) only creates this row as a private draft. Publication
    is a separate, deliberate action on the Share page — nothing is ever
    visible to anyone else until status is "published", and publishing is
    revocable. Content is encrypted at rest like all user content.
    """
    __tablename__ = "share_draft"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    # What kind of internal state this shares (FOUR-FEATURE-ECOSYSTEM 3.2):
    # need / offering / insight / exploration / intention / other.
    share_type = db.Column(db.String(16), nullable=False, default="other")
    # draft -> published -> revoked; dismissed = user discarded the draft.
    status = db.Column(db.String(16), nullable=False, default="draft",
                       index=True)
    # The LLM node whose proposal this draft came from (null for drafts the
    # user creates by hand on the Share page).
    source_node_id = db.Column(db.Integer, db.ForeignKey("node.id"),
                               nullable=True)
    # The standalone public root node created when this share is published
    # (#228). Publishing = extraction into the public forum; revoking
    # soft-deletes that node. Null while draft/revoked.
    public_node_id = db.Column(db.Integer, db.ForeignKey("node.id"),
                               nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)
    published_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", backref="share_drafts")

    SHARE_TYPES = ("need", "offering", "insight", "exploration",
                   "intention", "other")

    def set_content(self, plaintext):
        self.content = encrypt_content(plaintext)

    def get_content(self):
        return decrypt_content(self.content)


class UserPrompt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    prompt_key = db.Column(db.String(64), nullable=False)
    title = db.Column(db.String(128), nullable=False)
    content = db.Column(db.Text, nullable=False)
    generated_by = db.Column(db.String(64), nullable=False, default="default")
    based_on_default_hash = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="prompts")

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
    # Prompt-cache breakdown (#187). The split of cached vs. fresh input that
    # input_tokens (the full prompt size) folds together — read = served from
    # cache (0.1x), write = written into cache this call (1.25x). 0 for
    # non-cached calls; lets us query cache hit-rate over time from the DB.
    cache_read_tokens = db.Column(db.Integer, nullable=True, default=0)
    cache_write_tokens = db.Column(db.Integer, nullable=True, default=0)
    audio_duration_seconds = db.Column(db.Float, nullable=True)
    cost_microdollars = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    user = db.relationship("User", backref="api_cost_logs")


class SpendAlert(db.Model):
    """Dedupe record for API spend-limit alerts (issue #85).

    One row per (provider, month, threshold) that has already been
    alerted, so the hourly check never re-sends the same alert within
    a calendar month.
    """
    __tablename__ = "spend_alert"
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(32), nullable=False)
    month = db.Column(db.String(7), nullable=False)  # "YYYY-MM" (UTC)
    threshold = db.Column(db.Float, nullable=False)  # fraction of limit, e.g. 0.8
    spend_usd = db.Column(db.Float, nullable=False)  # spend at alert time
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("provider", "month", "threshold",
                            name="uq_spend_alert_provider_month_threshold"),
    )


class NodeEmbedding(db.Model):
    """Vector embedding of a node's content for semantic search (#155).

    One row per node (latest content only — re-embedded when content_hash
    changes). Vectors are packed float32 (struct) — no pgvector dependency;
    similarity is brute-force cosine in Python, fine at alpha scale.
    Only nodes with ai_usage in AI_ALLOWED are embedded (embedding sends
    content to OpenAI, so 'none' nodes are never submitted).
    """
    __tablename__ = "node_embedding"
    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(
        db.Integer,
        db.ForeignKey("node.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"),
                        nullable=False, index=True)
    model = db.Column(db.String(64), nullable=False)
    content_hash = db.Column(db.String(64), nullable=False)
    vector = db.Column(db.LargeBinary, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    node = db.relationship("Node", backref=db.backref(
        "embedding", uselist=False, cascade="all, delete-orphan"))


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
    # Chapter metadata (#145): which markdown section this chunk belongs
    # to. section_title is None for content before the first heading.
    section_index = db.Column(db.Integer, nullable=True)
    section_title = db.Column(db.String(256), nullable=True)
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
