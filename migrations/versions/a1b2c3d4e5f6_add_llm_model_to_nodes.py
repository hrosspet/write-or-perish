"""Add llm_model to nodes

Revision ID: a1b2c3d4e5f6
Revises: 04543ac80a83
Create Date: 2025-11-11 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '04543ac80a83'
branch_labels = None
depends_on = None


def upgrade():
    # Add llm_model column (nullable for backward compatibility)
    op.add_column('node', sa.Column('llm_model', sa.String(64), nullable=True))

    # Populate existing LLM nodes with "gpt-4.5-preview" (the legacy model)
    # User-created nodes (node_type != 'llm') will remain NULL
    op.execute("""
        UPDATE node
        SET llm_model = 'gpt-4.5-preview'
        WHERE node_type = 'llm' AND llm_model IS NULL
    """)


def downgrade():
    op.drop_column('node', 'llm_model')

