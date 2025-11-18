# Async Voice Transcription Design Document

## Executive Summary

Transform the synchronous voice transcription system to an asynchronous architecture using Celery task queue. This will resolve memory issues, eliminate timeout workarounds, and provide a better user experience for large audio file uploads.

---

## Current Architecture & Problems

### Current Flow
```
User uploads file (28 MB)
    ↓
Flask creates placeholder node
    ↓
Flask loads entire file into memory (pydub)
    ↓ (can take 5-15 minutes)
Flask calls OpenAI API synchronously
    ↓
Flask updates node with transcript
    ↓
Response sent to user
```

### Problems with Current Approach

1. **Memory Issues**
   - pydub loads entire audio as uncompressed PCM (~10x size)
   - 28 MB file → 200-400 MB in RAM
   - Worker gets OOM killed: `Worker was sent SIGKILL! Perhaps out of memory?`

2. **Timeout Workarounds**
   - Gunicorn timeout: 900s (15 minutes) - abnormally high
   - OpenAI client timeout: 900s - blocks worker entire time
   - Single slow request blocks entire worker process

3. **Poor User Experience**
   - Browser sits on "uploading..." for 5-15 minutes
   - No progress indication
   - If connection drops, transcription fails
   - No way to check status after submission

4. **Scalability Issues**
   - Each transcription blocks a worker
   - Can't process multiple large files concurrently
   - Worker pool exhaustion under load

---

## Proposed Architecture with Celery

### New Flow
```
User uploads file (28 MB)
    ↓
Flask receives file (< 1 second)
    ↓
Flask creates placeholder node
    ↓
Flask enqueues Celery task
    ↓
Flask returns immediately with task_id
    ↓
User's browser polls for status
    ↓
Celery worker processes in background
    ↓
When complete, updates node in DB
    ↓
Frontend polls and shows transcript
```

### Architecture Diagram
```
┌──────────────────┐
│   Frontend       │
│  (React/JS)      │
└────────┬─────────┘
         │ 1. POST /api/nodes (with audio)
         ├─────────────────────────────────┐
         │                                  ↓
         │                    ┌─────────────────────────┐
         │                    │   Flask API Server      │
         │                    │  - Saves audio to disk  │
         │                    │  - Creates node in DB   │
         │                    │  - Enqueues task        │
         │                    └──────────┬──────────────┘
         │                               │
         │                               │ 2. Enqueue task
         │                               ↓
         │                    ┌──────────────────────────┐
         │                    │   Redis Message Broker   │
         │                    │   (Task Queue)           │
         │                    └──────────┬───────────────┘
         │                               │
         │                               │ 3. Worker picks up
         │                               ↓
         │                    ┌──────────────────────────┐
         │                    │   Celery Worker(s)       │
         │                    │  - Compress audio        │
         │                    │  - Chunk if needed       │
         │                    │  - Transcribe chunks     │
         │                    │  - Update node in DB     │
         │                    └──────────┬───────────────┘
         │                               │
         │ 4. GET /api/nodes/:id/status  │ 5. DB update
         │    (polling every 2-3s)       ↓
         │                    ┌──────────────────────────┐
         │                    │   PostgreSQL Database    │
         │                    │  - Node status           │
         │                    │  - Task progress         │
         │                    │  - Transcription result  │
         │◄───────────────────┴──────────────────────────┘
         │ 6. Response with status/transcript
```

---

## Technical Implementation

### 1. New Database Schema

#### Add columns to `Node` table
```python
# New columns for async transcription
transcription_status = db.Column(
    db.String(20),
    default='pending',  # pending, processing, completed, failed
    nullable=True
)
transcription_task_id = db.Column(db.String(255), nullable=True)
transcription_error = db.Column(db.Text, nullable=True)
transcription_progress = db.Column(db.Integer, default=0)  # 0-100%
transcription_started_at = db.Column(db.DateTime, nullable=True)
transcription_completed_at = db.Column(db.DateTime, nullable=True)
```

#### Migration
```python
# migrations/versions/xxxxx_add_transcription_status.py
def upgrade():
    op.add_column('node', sa.Column('transcription_status', sa.String(20), nullable=True))
    op.add_column('node', sa.Column('transcription_task_id', sa.String(255), nullable=True))
    op.add_column('node', sa.Column('transcription_error', sa.Text, nullable=True))
    op.add_column('node', sa.Column('transcription_progress', sa.Integer, default=0))
    op.add_column('node', sa.Column('transcription_started_at', sa.DateTime, nullable=True))
    op.add_column('node', sa.Column('transcription_completed_at', sa.DateTime, nullable=True))

    # Set existing nodes to 'completed' if they have transcriptions
    op.execute("""
        UPDATE node
        SET transcription_status = 'completed'
        WHERE audio_original_url IS NOT NULL
        AND content != '[Voice note – transcription pending]'
    """)

    # Set stuck nodes to 'failed'
    op.execute("""
        UPDATE node
        SET transcription_status = 'failed',
            transcription_error = 'Legacy transcription failed'
        WHERE audio_original_url IS NOT NULL
        AND content = '[Voice note – transcription pending]'
    """)

def downgrade():
    op.drop_column('node', 'transcription_completed_at')
    op.drop_column('node', 'transcription_started_at')
    op.drop_column('node', 'transcription_progress')
    op.drop_column('node', 'transcription_error')
    op.drop_column('node', 'transcription_task_id')
    op.drop_column('node', 'transcription_status')
```

---

### 2. Backend Changes

#### File Structure
```
backend/
├── celery_app.py          # NEW: Celery app initialization
├── tasks/                 # NEW: Celery tasks
│   ├── __init__.py
│   └── transcription.py   # NEW: Transcription task
├── routes/
│   └── nodes.py           # MODIFIED: Remove sync transcription
└── utils/                 # NEW: Shared utilities
    └── audio_processing.py # NEW: Audio helpers (from nodes.py)
```

#### 2.1 Celery Configuration (`backend/celery_app.py`)
```python
from celery import Celery
from backend import create_app

# Create Flask app for Celery context
flask_app = create_app()

# Initialize Celery
celery = Celery(
    'write_or_perish',
    broker=flask_app.config.get('CELERY_BROKER_URL', 'redis://localhost:6379/0'),
    backend=flask_app.config.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
)

# Celery configuration
celery.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,  # 1 hour hard limit
    task_soft_time_limit=3300,  # 55 minute soft limit
    worker_prefetch_multiplier=1,  # Process one task at a time
    worker_max_tasks_per_child=50,  # Restart worker after 50 tasks (prevent memory leaks)
)

# Auto-discover tasks
celery.autodiscover_tasks(['backend.tasks'])
```

#### 2.2 Audio Processing Utilities (`backend/utils/audio_processing.py`)
```python
"""
Audio processing utilities for voice transcription.
Extracted from routes/nodes.py to be shared between API and Celery tasks.
"""
import os
import pathlib
import tempfile
from pydub import AudioSegment
from flask import current_app

# OpenAI API limits
OPENAI_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB
OPENAI_MAX_DURATION_SEC = 1500  # 25 minutes
CHUNK_DURATION_SEC = 20 * 60  # 20 minutes per chunk


def compress_audio_if_needed(file_path: pathlib.Path, logger=None) -> pathlib.Path:
    """
    Compress audio file to MP3 if it's uncompressed or exceeds size limit.
    Returns path to compressed file, or original if no compression needed.
    """
    if logger is None:
        logger = current_app.logger

    file_size = file_path.stat().st_size
    ext = file_path.suffix.lower()

    needs_compression = ext in {".wav", ".flac"} or file_size > OPENAI_MAX_AUDIO_BYTES

    if not needs_compression:
        return file_path

    try:
        logger.info(f"Compressing audio file {file_path.name} (size: {file_size / 1024 / 1024:.1f} MB)")

        audio = AudioSegment.from_file(str(file_path))
        compressed_path = file_path.with_suffix('.mp3')

        audio.export(
            str(compressed_path),
            format="mp3",
            bitrate="128k",
            parameters=["-q:a", "2"]
        )

        compressed_size = compressed_path.stat().st_size
        logger.info(
            f"Compressed {file_path.name}: {file_size / 1024 / 1024:.1f} MB -> "
            f"{compressed_size / 1024 / 1024:.1f} MB"
        )

        return compressed_path

    except Exception as e:
        logger.error(f"Audio compression failed: {e}")
        return file_path


def get_audio_duration(file_path: pathlib.Path, logger=None) -> float:
    """Get audio duration in seconds."""
    if logger is None:
        logger = current_app.logger

    try:
        audio = AudioSegment.from_file(str(file_path))
        return len(audio) / 1000.0
    except Exception as e:
        logger.error(f"Failed to get audio duration: {e}")
        return 0.0


def chunk_audio(file_path: pathlib.Path, chunk_duration_sec: int = CHUNK_DURATION_SEC, logger=None) -> list:
    """
    Split audio file into chunks.
    Returns list of temporary file paths.
    """
    if logger is None:
        logger = current_app.logger

    try:
        audio = AudioSegment.from_file(str(file_path))
        chunk_duration_ms = chunk_duration_sec * 1000
        chunks = []

        for i, start_ms in enumerate(range(0, len(audio), chunk_duration_ms)):
            chunk = audio[start_ms:start_ms + chunk_duration_ms]

            temp_file = tempfile.NamedTemporaryFile(
                delete=False,
                suffix='.mp3',
                prefix=f'chunk_{i}_'
            )
            chunk.export(temp_file.name, format="mp3", bitrate="128k")
            chunks.append(temp_file.name)

            logger.info(
                f"Created chunk {i + 1}: {start_ms / 1000:.0f}s - {(start_ms + len(chunk)) / 1000:.0f}s"
            )

        return chunks

    except Exception as e:
        logger.error(f"Audio chunking failed: {e}")
        return []
```

#### 2.3 Transcription Task (`backend/tasks/transcription.py`)
```python
"""
Celery task for asynchronous audio transcription.
"""
from celery import Task
from celery.utils.log import get_task_logger
from openai import OpenAI
import pathlib
import os
from datetime import datetime

from backend.celery_app import celery, flask_app
from backend.models import Node
from backend.extensions import db
from backend.utils.audio_processing import (
    compress_audio_if_needed,
    get_audio_duration,
    chunk_audio,
    OPENAI_MAX_AUDIO_BYTES,
    OPENAI_MAX_DURATION_SEC
)

logger = get_task_logger(__name__)


class TranscriptionTask(Task):
    """Custom task class with error handling."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Called when task fails."""
        node_id = args[0] if args else None
        if node_id:
            with flask_app.app_context():
                node = Node.query.get(node_id)
                if node:
                    node.transcription_status = 'failed'
                    node.transcription_error = str(exc)[:500]
                    node.transcription_completed_at = datetime.utcnow()
                    db.session.commit()
                    logger.error(f"Transcription failed for node {node_id}: {exc}")


@celery.task(base=TranscriptionTask, bind=True)
def transcribe_audio(self, node_id: int, audio_file_path: str):
    """
    Asynchronously transcribe an audio file.

    Args:
        node_id: Database ID of the node
        audio_file_path: Absolute path to the audio file
    """
    logger.info(f"Starting transcription task for node {node_id}")

    with flask_app.app_context():
        # Get node from database
        node = Node.query.get(node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found")

        # Update status to processing
        node.transcription_status = 'processing'
        node.transcription_started_at = datetime.utcnow()
        node.transcription_progress = 0
        db.session.commit()

        try:
            # Get OpenAI API key
            api_key = flask_app.config.get("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OPENAI_API_KEY not configured")

            client = OpenAI(api_key=api_key, timeout=900.0)
            file_path = pathlib.Path(audio_file_path)

            if not file_path.exists():
                raise FileNotFoundError(f"Audio file not found: {audio_file_path}")

            # Step 1: Compress if needed (10% progress)
            self.update_state(state='PROGRESS', meta={'progress': 10, 'status': 'Compressing audio'})
            node.transcription_progress = 10
            db.session.commit()

            processed_path = compress_audio_if_needed(file_path, logger)

            # Step 2: Check size and duration (20% progress)
            self.update_state(state='PROGRESS', meta={'progress': 20, 'status': 'Analyzing audio'})
            node.transcription_progress = 20
            db.session.commit()

            file_size = processed_path.stat().st_size
            duration_sec = get_audio_duration(processed_path, logger)

            logger.info(f"Transcribing audio: {file_size / 1024 / 1024:.1f} MB, {duration_sec:.0f} seconds")

            # Step 3: Determine if chunking is needed
            needs_chunking = (
                file_size > OPENAI_MAX_AUDIO_BYTES or
                duration_sec > OPENAI_MAX_DURATION_SEC
            )

            transcript = None

            if not needs_chunking:
                # Simple case: transcribe whole file (30% -> 90% progress)
                self.update_state(state='PROGRESS', meta={'progress': 30, 'status': 'Transcribing'})
                node.transcription_progress = 30
                db.session.commit()

                with open(processed_path, "rb") as audio_file:
                    resp = client.audio.transcriptions.create(
                        model="gpt-4o-transcribe",
                        file=audio_file,
                        response_format="text"
                    )

                    if hasattr(resp, "text"):
                        transcript = resp.text
                    elif isinstance(resp, dict):
                        transcript = resp.get("text") or resp.get("transcript") or ""
                    else:
                        transcript = str(resp)

                self.update_state(state='PROGRESS', meta={'progress': 90, 'status': 'Finalizing'})
                node.transcription_progress = 90
                db.session.commit()

            else:
                # Complex case: chunk and transcribe
                logger.info(f"File exceeds limits, using chunked transcription")

                self.update_state(state='PROGRESS', meta={'progress': 30, 'status': 'Creating chunks'})
                node.transcription_progress = 30
                db.session.commit()

                chunk_paths = chunk_audio(processed_path, logger=logger)

                if not chunk_paths:
                    raise Exception("Failed to create audio chunks")

                transcripts = []
                chunk_progress_step = 60 / len(chunk_paths)  # 30% -> 90% split across chunks

                try:
                    for i, chunk_path in enumerate(chunk_paths):
                        progress = 30 + int((i + 1) * chunk_progress_step)
                        self.update_state(
                            state='PROGRESS',
                            meta={
                                'progress': progress,
                                'status': f'Transcribing chunk {i+1}/{len(chunk_paths)}'
                            }
                        )
                        node.transcription_progress = progress
                        db.session.commit()

                        logger.info(f"Transcribing chunk {i + 1}/{len(chunk_paths)}")

                        with open(chunk_path, "rb") as audio_file:
                            resp = client.audio.transcriptions.create(
                                model="gpt-4o-transcribe",
                                file=audio_file,
                                response_format="text"
                            )

                            if hasattr(resp, "text"):
                                chunk_text = resp.text
                            elif isinstance(resp, dict):
                                chunk_text = resp.get("text") or resp.get("transcript") or ""
                            else:
                                chunk_text = str(resp)

                            transcripts.append(chunk_text)

                    transcript = "\n\n".join(transcripts)
                    logger.info(f"Chunked transcription complete: {len(chunk_paths)} chunks")

                finally:
                    # Clean up chunk files
                    for chunk_path in chunk_paths:
                        try:
                            os.unlink(chunk_path)
                        except Exception as e:
                            logger.warning(f"Failed to delete chunk: {e}")

            # Clean up compressed file if different from original
            if processed_path != file_path:
                try:
                    os.unlink(processed_path)
                except Exception as e:
                    logger.warning(f"Failed to delete compressed file: {e}")

            # Step 4: Update node with transcript (100% progress)
            self.update_state(state='PROGRESS', meta={'progress': 100, 'status': 'Complete'})

            node.content = transcript or node.content
            node.transcription_status = 'completed'
            node.transcription_progress = 100
            node.transcription_completed_at = datetime.utcnow()
            node.transcription_error = None
            db.session.commit()

            logger.info(f"Transcription successful for node {node_id}: {len(transcript)} characters")

            return {
                'node_id': node_id,
                'status': 'completed',
                'transcript_length': len(transcript)
            }

        except Exception as e:
            logger.error(f"Transcription error for node {node_id}: {e}", exc_info=True)
            node.transcription_status = 'failed'
            node.transcription_error = str(e)[:500]
            node.transcription_completed_at = datetime.utcnow()
            db.session.commit()
            raise
```

#### 2.4 Updated Node Routes (`backend/routes/nodes.py`)
```python
# MODIFIED: In the create_node() function, replace synchronous transcription with task queue

@nodes_bp.route("/", methods=["POST"])
@login_required
def create_node():
    if request.content_type and request.content_type.startswith("multipart"):
        # Voice-Mode upload path
        if "audio_file" not in request.files:
            return jsonify({"error": "Field 'audio_file' is required"}), 400

        file = request.files["audio_file"]

        if file.filename == "":
            return jsonify({"error": "No selected file"}), 400
        if not _allowed_file(file.filename):
            return jsonify({"error": "Unsupported file type"}), 415

        content_length = request.content_length or 0
        if content_length > MAX_AUDIO_BYTES:
            return jsonify({"error": "File too large"}), 413

        parent_id = request.form.get("parent_id")
        node_type = request.form.get("node_type", "user")

        placeholder_text = "[Voice note – transcription pending]"

        node = Node(
            user_id=current_user.id,
            parent_id=parent_id,
            node_type=node_type,
            content=placeholder_text,
            transcription_status='pending'  # NEW
        )
        db.session.add(node)
        db.session.commit()

        # Save audio file
        url = _save_audio_file(file, current_user.id, node.id, "original")
        node.audio_original_url = url
        node.audio_mime_type = file.mimetype
        db.session.add(node)
        db.session.commit()

        # Enqueue transcription task (instead of synchronous processing)
        from backend.tasks.transcription import transcribe_audio
        from backend.routes.nodes import AUDIO_STORAGE_ROOT

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
            "transcription_status": node.transcription_status,  # NEW
            "transcription_task_id": node.transcription_task_id  # NEW
        }), 201

    # ... rest of text upload logic unchanged ...


# NEW: Status endpoint for polling
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
```

#### 2.5 Configuration (`backend/config.py`)
```python
# ADD to Config class
class Config:
    # ... existing config ...

    # Celery configuration
    CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
    CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
```

---

### 3. Frontend Changes

#### 3.1 Updated NodeForm Component (`frontend/src/components/NodeForm.js`)
```javascript
// MODIFIED: Add polling for transcription status

const NodeForm = ({ parentId, onSuccess, onCancel }) => {
  const [isUploading, setIsUploading] = useState(false);
  const [transcriptionStatus, setTranscriptionStatus] = useState(null);
  const [transcriptionProgress, setTranscriptionProgress] = useState(0);

  // Poll for transcription status
  const pollTranscriptionStatus = async (nodeId) => {
    try {
      const response = await fetch(`/api/nodes/${nodeId}/transcription-status`, {
        credentials: 'include'
      });

      if (!response.ok) {
        throw new Error('Failed to fetch transcription status');
      }

      const data = await response.json();

      setTranscriptionStatus(data.status);
      setTranscriptionProgress(data.progress || 0);

      // If completed or failed, stop polling and navigate
      if (data.status === 'completed' || data.status === 'failed') {
        if (data.status === 'completed') {
          onSuccess({ id: nodeId, ...data });
        } else {
          console.error('Transcription failed:', data.error);
          alert(`Transcription failed: ${data.error}`);
        }
        return false; // Stop polling
      }

      return true; // Continue polling
    } catch (error) {
      console.error('Error polling transcription status:', error);
      return true; // Continue polling on error
    }
  };

  const handleAudioSubmit = async (audioBlob, mimeType) => {
    setIsUploading(true);
    setTranscriptionStatus('uploading');

    try {
      const formData = new FormData();
      formData.append('audio_file', audioBlob, 'recording.webm');
      if (parentId) {
        formData.append('parent_id', parentId);
      }

      const response = await fetch('/api/nodes/', {
        method: 'POST',
        credentials: 'include',
        body: formData
      });

      if (!response.ok) {
        throw new Error('Failed to upload audio');
      }

      const data = await response.json();

      // Start polling for transcription status
      setTranscriptionStatus('pending');
      const nodeId = data.id;

      // Poll every 2 seconds
      const pollInterval = setInterval(async () => {
        const shouldContinue = await pollTranscriptionStatus(nodeId);
        if (!shouldContinue) {
          clearInterval(pollInterval);
          setIsUploading(false);
        }
      }, 2000);

      // Stop polling after 30 minutes (failsafe)
      setTimeout(() => {
        clearInterval(pollInterval);
        setIsUploading(false);
      }, 30 * 60 * 1000);

    } catch (error) {
      console.error('Error uploading audio:', error);
      alert('Failed to upload audio');
      setIsUploading(false);
    }
  };

  // Render progress indicator
  const renderTranscriptionStatus = () => {
    if (!transcriptionStatus || transcriptionStatus === 'uploading') {
      return null;
    }

    const statusMessages = {
      'pending': 'Waiting in queue...',
      'processing': 'Transcribing audio...',
      'completed': 'Complete!',
      'failed': 'Transcription failed'
    };

    return (
      <div className="transcription-status">
        <p>{statusMessages[transcriptionStatus]}</p>
        <div className="progress-bar">
          <div
            className="progress-fill"
            style={{ width: `${transcriptionProgress}%` }}
          />
        </div>
        <p>{transcriptionProgress}%</p>
      </div>
    );
  };

  return (
    <div>
      {/* ... existing form UI ... */}

      {isUploading && renderTranscriptionStatus()}

      {/* ... rest of component ... */}
    </div>
  );
};
```

#### 3.2 CSS for Progress Indicator (`frontend/src/components/NodeForm.css`)
```css
.transcription-status {
  margin: 20px 0;
  padding: 15px;
  background: #f5f5f5;
  border-radius: 8px;
  text-align: center;
}

.progress-bar {
  width: 100%;
  height: 24px;
  background: #e0e0e0;
  border-radius: 12px;
  overflow: hidden;
  margin: 10px 0;
}

.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, #4CAF50, #45a049);
  transition: width 0.3s ease;
}
```

---

### 4. Infrastructure & Deployment

#### 4.1 Redis Installation (Production VM)
```bash
# Install Redis
sudo apt-get update
sudo apt-get install -y redis-server

# Configure Redis
sudo vim /etc/redis/redis.conf
# Set: supervised systemd
# Set: bind 127.0.0.1 (only local connections)

# Start Redis
sudo systemctl enable redis-server
sudo systemctl start redis-server

# Verify
redis-cli ping  # Should return PONG
```

#### 4.2 Celery Worker Service (`write-or-perish-celery.service`)
```ini
[Unit]
Description=Write or Perish Celery Worker
After=network.target redis-server.service

[Service]
Type=forking
User=hrosspet
Group=hrosspet
WorkingDirectory=/home/hrosspet/write-or-perish
EnvironmentFile=/home/hrosspet/write-or-perish/.env.production

ExecStart=/bin/bash -c 'source ~/miniconda3/etc/profile.d/conda.sh && conda activate write-or-perish && celery -A backend.celery_app:celery worker --loglevel=info --logfile=/home/hrosspet/write-or-perish/logs/celery-worker.log --pidfile=/home/hrosspet/write-or-perish/celery-worker.pid --detach --concurrency=2'

ExecReload=/bin/kill -s HUP $MAINPID
ExecStop=/bin/kill -s TERM $MAINPID

Restart=on-failure
RestartSec=10s

[Install]
WantedBy=multi-user.target
```

#### 4.3 Update Requirements (`backend/requirements.txt`)
```
# ADD:
celery==5.3.4
redis==5.0.1
```

#### 4.4 Environment Variables (`.env.production`)
```bash
# ADD:
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0
```

#### 4.5 Deployment Script Updates (`deploy.sh`)
```bash
#!/bin/bash
# ADD after "source conda activate":

# Restart Celery worker
echo "Restarting Celery worker..."
sudo systemctl restart write-or-perish-celery

# Wait for Celery to start
sleep 3

# Check Celery status
if ! systemctl is-active --quiet write-or-perish-celery; then
    echo "ERROR: Celery worker failed to start"
    systemctl status write-or-perish-celery
    exit 1
fi

echo "Celery worker restarted successfully"
```

---

## Migration Strategy

### Phase 1: Add Infrastructure (No Code Changes)
1. Install Redis on production VM
2. Test Redis connectivity
3. Add Celery to requirements.txt
4. Install Celery in conda environment

### Phase 2: Add Database Columns (Non-Breaking)
1. Create and run migration to add transcription status columns
2. Backfill existing nodes:
   - Completed nodes → `transcription_status = 'completed'`
   - Stuck nodes → `transcription_status = 'failed'`
3. Deploy migration to production

### Phase 3: Deploy Async Code (Feature Flag)
1. Add feature flag: `ASYNC_TRANSCRIPTION_ENABLED = True/False`
2. Deploy code with async path but flag disabled
3. Test in staging/development
4. Enable flag in production

### Phase 4: Full Rollout
1. Enable async transcription for all users
2. Monitor logs and error rates
3. Remove old synchronous code after 1 week

---

## Monitoring & Operations

### Health Checks
```bash
# Check Redis
redis-cli ping

# Check Celery workers
celery -A backend.celery_app:celery inspect active

# Check task queue size
celery -A backend.celery_app:celery inspect stats

# Monitor worker logs
tail -f /home/hrosspet/write-or-perish/logs/celery-worker.log
```

### Key Metrics to Monitor
- Task queue length (should stay < 10)
- Task success/failure rate
- Average task duration
- Worker memory usage
- Redis memory usage
- Stuck tasks (pending > 30 minutes)

### Celery Flower (Optional Monitoring UI)
```bash
# Install
pip install flower

# Run
celery -A backend.celery_app:celery flower --port=5555

# Access at http://localhost:5555
```

---

## Testing Plan

### Unit Tests
```python
# test_transcription_task.py
def test_transcribe_small_audio():
    """Test transcription of small audio file."""
    # Create test node
    # Enqueue task
    # Assert task completes
    # Assert transcript is correct

def test_transcribe_large_audio():
    """Test chunking for large files."""
    # Create test node with >25MB file
    # Enqueue task
    # Assert task completes
    # Assert transcript has multiple paragraphs (from chunks)

def test_transcription_failure():
    """Test error handling."""
    # Create node with invalid audio
    # Enqueue task
    # Assert status becomes 'failed'
    # Assert error message is stored
```

### Integration Tests
1. Upload small MP3 → verify transcription
2. Upload large WAV → verify compression + transcription
3. Upload 28 MB file → verify chunking + transcription
4. Simulate OpenAI API failure → verify error handling
5. Test polling endpoint → verify status updates

### Load Tests
- Queue 10 files simultaneously
- Verify all complete successfully
- Check worker doesn't run out of memory

---

## Rollback Plan

If issues occur after deployment:

1. **Immediate**: Set feature flag `ASYNC_TRANSCRIPTION_ENABLED = False`
2. **Database**: Rollback migration if needed (columns are nullable)
3. **Services**: Stop Celery worker: `sudo systemctl stop write-or-perish-celery`
4. **Code**: Revert to previous commit and redeploy

---

## Benefits Summary

### Problems Solved
✅ **Memory issues**: Workers no longer OOM killed (processing in dedicated worker)
✅ **Timeout workarounds**: Can remove 900s gunicorn timeout
✅ **User experience**: Immediate response, progress indication
✅ **Scalability**: Multiple files can be processed concurrently
✅ **Monitoring**: Clear visibility into transcription status

### New Capabilities
✅ **Retry logic**: Failed tasks can be retried automatically
✅ **Priority queues**: Could prioritize paid users' transcriptions
✅ **Rate limiting**: Can throttle OpenAI API calls
✅ **Offline processing**: Can process during off-peak hours

---

## Timeline Estimate

- **Phase 1** (Infrastructure): 2-3 hours
- **Phase 2** (Database): 1-2 hours
- **Phase 3** (Code): 4-6 hours
- **Phase 4** (Testing & Rollout): 2-3 hours

**Total**: ~10-15 hours of development + testing

---

## Questions & Decisions Needed

1. **Redis hosting**: Use local Redis or external service (Redis Cloud)?
2. **Worker count**: Start with 2 workers, scale up if needed?
3. **Task retention**: How long to keep task results? (suggest 24 hours)
4. **Retry policy**: Auto-retry failed tasks? How many times?
5. **User notifications**: Email when transcription completes?

---

## Appendix: Alternative Approaches Considered

### A. Webhooks (Rejected)
- OpenAI doesn't support webhooks for transcription
- Would require polling OpenAI anyway

### B. Serverless (Rejected)
- Cold starts would be problematic for large files
- More expensive than dedicated worker
- Harder to debug

### C. Direct ffmpeg (Considered for Phase 2)
- Could reduce memory further
- More complex to implement
- Could be added later if needed

---

**Document Version**: 1.0
**Last Updated**: 2025-11-18
**Author**: Claude Code
