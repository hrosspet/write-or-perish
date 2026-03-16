"""Create node_context_artifact table and backfill from user_prompt_id.

This is a MANUAL migration (not auto-generated) because it includes
data operations (backfill from user_prompt_id, plus profile/todo
snapshot for historical nodes).

Revision ID: a1c2e3f4g5h6
Revises: ed06445e342b
Create Date: 2026-03-16
"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime

# revision identifiers, used by Alembic.
revision = 'a1c2e3f4g5h6'
down_revision = 'ed06445e342b'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Create the new join table
    op.create_table(
        'node_context_artifact',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('node_id', sa.Integer(),
                  sa.ForeignKey('node.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('artifact_type', sa.String(32), nullable=False),
        sa.Column('artifact_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), default=datetime.utcnow),
        sa.UniqueConstraint('node_id', 'artifact_type',
                            name='uq_node_artifact_type'),
    )

    # 2. Backfill prompt artifacts from the existing user_prompt_id FK
    op.execute("""
        INSERT INTO node_context_artifact (node_id, artifact_type, artifact_id)
        SELECT id, 'prompt', user_prompt_id
        FROM node
        WHERE user_prompt_id IS NOT NULL
    """)

    # 3. Backfill profile artifacts: for each system-prompt node, find the
    #    most recent UserProfile that existed at node creation time and is
    #    visible to AI (ai_usage in 'chat','train').
    op.execute("""
        INSERT INTO node_context_artifact (node_id, artifact_type, artifact_id)
        SELECT n.id, 'profile', p.id
        FROM node n
        JOIN LATERAL (
            SELECT up.id
            FROM user_profile up
            WHERE up.user_id = n.user_id
              AND up.created_at <= n.created_at
              AND up.ai_usage IN ('chat', 'train')
            ORDER BY up.created_at DESC
            LIMIT 1
        ) p ON TRUE
        WHERE n.user_prompt_id IS NOT NULL
    """)

    # 4. Backfill todo artifacts (same logic)
    op.execute("""
        INSERT INTO node_context_artifact (node_id, artifact_type, artifact_id)
        SELECT n.id, 'todo', t.id
        FROM node n
        JOIN LATERAL (
            SELECT ut.id
            FROM user_todo ut
            WHERE ut.user_id = n.user_id
              AND ut.created_at <= n.created_at
              AND ut.ai_usage IN ('chat', 'train')
            ORDER BY ut.created_at DESC
            LIMIT 1
        ) t ON TRUE
        WHERE n.user_prompt_id IS NOT NULL
    """)

    # NOTE: user_prompt_id column is intentionally NOT dropped here.
    # It will be removed in a future cleanup migration after verifying
    # data integrity in production.

    # ---------- Verification ----------
    conn = op.get_bind()

    # Count of nodes with user_prompt_id should equal count of
    # artifact_type='prompt' rows.
    nodes_with_prompt = conn.execute(sa.text(
        "SELECT COUNT(*) FROM node WHERE user_prompt_id IS NOT NULL"
    )).scalar()
    prompt_artifacts = conn.execute(sa.text(
        "SELECT COUNT(*) FROM node_context_artifact "
        "WHERE artifact_type = 'prompt'"
    )).scalar()
    profile_artifacts = conn.execute(sa.text(
        "SELECT COUNT(*) FROM node_context_artifact "
        "WHERE artifact_type = 'profile'"
    )).scalar()
    todo_artifacts = conn.execute(sa.text(
        "SELECT COUNT(*) FROM node_context_artifact "
        "WHERE artifact_type = 'todo'"
    )).scalar()

    # Orphan check: prompt artifacts referencing non-existent UserPrompt rows
    orphan_prompts = conn.execute(sa.text(
        "SELECT COUNT(*) FROM node_context_artifact nca "
        "LEFT JOIN user_prompt up ON nca.artifact_id = up.id "
        "WHERE nca.artifact_type = 'prompt' AND up.id IS NULL"
    )).scalar()
    orphan_profiles = conn.execute(sa.text(
        "SELECT COUNT(*) FROM node_context_artifact nca "
        "LEFT JOIN user_profile up ON nca.artifact_id = up.id "
        "WHERE nca.artifact_type = 'profile' AND up.id IS NULL"
    )).scalar()
    orphan_todos = conn.execute(sa.text(
        "SELECT COUNT(*) FROM node_context_artifact nca "
        "LEFT JOIN user_todo ut ON nca.artifact_id = ut.id "
        "WHERE nca.artifact_type = 'todo' AND ut.id IS NULL"
    )).scalar()

    print(f"[context_artifacts migration] Verification:")
    print(f"  Nodes with user_prompt_id: {nodes_with_prompt}")
    print(f"  Prompt artifacts created:  {prompt_artifacts} "
          f"({'OK' if prompt_artifacts == nodes_with_prompt else 'MISMATCH'})")
    print(f"  Profile artifacts created: {profile_artifacts}")
    print(f"  Todo artifacts created:    {todo_artifacts}")
    print(f"  Orphan prompts:  {orphan_prompts} "
          f"({'OK' if orphan_prompts == 0 else 'WARNING'})")
    print(f"  Orphan profiles: {orphan_profiles} "
          f"({'OK' if orphan_profiles == 0 else 'WARNING'})")
    print(f"  Orphan todos:    {orphan_todos} "
          f"({'OK' if orphan_todos == 0 else 'WARNING'})")

    if prompt_artifacts != nodes_with_prompt:
        raise RuntimeError(
            f"Prompt backfill mismatch: {nodes_with_prompt} nodes with "
            f"user_prompt_id but {prompt_artifacts} prompt artifacts created. "
            f"Migration will be rolled back."
        )
    if orphan_prompts > 0 or orphan_profiles > 0 or orphan_todos > 0:
        raise RuntimeError(
            f"Orphaned artifacts detected (prompts={orphan_prompts}, "
            f"profiles={orphan_profiles}, todos={orphan_todos}). "
            f"Migration will be rolled back."
        )


def downgrade():
    op.drop_table('node_context_artifact')
