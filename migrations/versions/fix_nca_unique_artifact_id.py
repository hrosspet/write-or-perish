"""Fix node_context_artifact unique constraint to include artifact_id.

MANUAL migration: Alembic autogenerate does not reliably detect unique
constraint changes. The original constraint uq_node_artifact_type
(node_id, artifact_type) wrongly forbade the multi-row "user_artifact"
pin type (#158 — one pinned row per artifact kind on the same node), so
attach_context_artifacts 500'd for any user with 2+ artifact kinds.
Replace it with uq_node_artifact_type_id (node_id, artifact_type,
artifact_id), which still prevents exact-duplicate pins.

Revision ID: b7c8d9e0f1a2
Revises: f02da2d8a127
Create Date: 2026-06-16
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b7c8d9e0f1a2'
down_revision = 'f02da2d8a127'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        'uq_node_artifact_type', 'node_context_artifact', type_='unique')
    op.create_unique_constraint(
        'uq_node_artifact_type_id', 'node_context_artifact',
        ['node_id', 'artifact_type', 'artifact_id'])


def downgrade():
    # Note: reverting is only safe if no node has 2+ user_artifact rows.
    op.drop_constraint(
        'uq_node_artifact_type_id', 'node_context_artifact', type_='unique')
    op.create_unique_constraint(
        'uq_node_artifact_type', 'node_context_artifact',
        ['node_id', 'artifact_type'])
