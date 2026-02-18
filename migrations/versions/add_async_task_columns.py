"""Add async task columns to Node table

Revision ID: add_async_task_columns
Revises:
Create Date: 2025-11-18

"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime


# revision identifiers, used by Alembic.
revision = 'add_async_task_columns'
down_revision = None  # Set this to the latest migration ID in your project
branch_labels = None
depends_on = None


def upgrade():
    # Add columns for async transcription tracking
    op.add_column('node', sa.Column('transcription_status', sa.String(20), nullable=True))
    op.add_column('node', sa.Column('transcription_task_id', sa.String(255), nullable=True))
    op.add_column('node', sa.Column('transcription_error', sa.Text, nullable=True))
    op.add_column('node', sa.Column('transcription_progress', sa.Integer, default=0))
    op.add_column('node', sa.Column('transcription_started_at', sa.DateTime, nullable=True))
    op.add_column('node', sa.Column('transcription_completed_at', sa.DateTime, nullable=True))

    # Add columns for async LLM completion tracking
    op.add_column('node', sa.Column('llm_task_id', sa.String(255), nullable=True))
    op.add_column('node', sa.Column('llm_task_status', sa.String(20), nullable=True))
    op.add_column('node', sa.Column('llm_task_progress', sa.Integer, default=0))

    # Add columns for async TTS tracking
    op.add_column('node', sa.Column('tts_task_id', sa.String(255), nullable=True))
    op.add_column('node', sa.Column('tts_task_status', sa.String(20), nullable=True))
    op.add_column('node', sa.Column('tts_task_progress', sa.Integer, default=0))

    # Backfill existing nodes (only if audio_original_url column exists,
    # which may not be the case on a fresh DB where this migration runs first)
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'node' AND column_name = 'audio_original_url'"
    ))
    if result.fetchone():
        op.execute("""
            UPDATE node
            SET transcription_status = 'completed'
            WHERE audio_original_url IS NOT NULL
            AND content != '[Voice note – transcription pending]'
            AND content IS NOT NULL
        """)

        op.execute("""
            UPDATE node
            SET transcription_status = 'failed',
                transcription_error = 'Legacy transcription failed or timeout'
            WHERE audio_original_url IS NOT NULL
            AND (content = '[Voice note – transcription pending]' OR content IS NULL)
        """)


def downgrade():
    # Remove async task columns
    op.drop_column('node', 'tts_task_progress')
    op.drop_column('node', 'tts_task_status')
    op.drop_column('node', 'tts_task_id')
    op.drop_column('node', 'llm_task_progress')
    op.drop_column('node', 'llm_task_status')
    op.drop_column('node', 'llm_task_id')
    op.drop_column('node', 'transcription_completed_at')
    op.drop_column('node', 'transcription_started_at')
    op.drop_column('node', 'transcription_progress')
    op.drop_column('node', 'transcription_error')
    op.drop_column('node', 'transcription_task_id')
    op.drop_column('node', 'transcription_status')
