"""Add llm_task_error column to Node table

Revision ID: 143c315bdad9
Revises: add_async_task_columns
Create Date: 2025-01-18

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '143c315bdad9'
down_revision = 'add_async_task_columns'
branch_labels = None
depends_on = None


def upgrade():
    # Add llm_task_error column for storing detailed error messages
    op.add_column('node', sa.Column('llm_task_error', sa.Text, nullable=True))


def downgrade():
    # Remove llm_task_error column
    op.drop_column('node', 'llm_task_error')
