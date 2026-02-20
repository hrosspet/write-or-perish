"""auto-generated migration from deployment

Revision ID: 7791feaf755a
Revises: 7e4d49d8dc7b
Create Date: 2026-02-20 17:10:17.872644

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7791feaf755a'
down_revision = '7e4d49d8dc7b'
branch_labels = None
depends_on = None


def upgrade():
    # New tables
    op.create_table('user_todo',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('generated_by', sa.String(length=64), nullable=False),
    sa.Column('tokens_used', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('privacy_level', sa.String(length=16), nullable=False),
    sa.Column('ai_usage', sa.String(length=16), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )

    op.create_table('user_prompt',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('prompt_key', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=128), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('generated_by', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )

    # User table: new columns (server_default for NOT NULL columns)
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('craft_mode', sa.Boolean(),
                                      nullable=False,
                                      server_default=sa.text('false')))
        batch_op.add_column(sa.Column('profile_generation_task_id',
                                      sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('profile_needs_full_regen',
                                      sa.Boolean(), nullable=False,
                                      server_default=sa.text('false')))

    # UserProfile table: new columns (all nullable)
    with op.batch_alter_table('user_profile', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_tokens_used', sa.Integer(),
                                      nullable=True))
        batch_op.add_column(sa.Column('source_data_cutoff', sa.DateTime(),
                                      nullable=True))
        batch_op.add_column(sa.Column('generation_type',
                                      sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('parent_profile_id', sa.Integer(),
                                      nullable=True))
        batch_op.create_foreign_key('fk_user_profile_parent',
                                    'user_profile',
                                    ['parent_profile_id'], ['id'])


def downgrade():
    with op.batch_alter_table('user_profile', schema=None) as batch_op:
        batch_op.drop_constraint('fk_user_profile_parent',
                                 type_='foreignkey')
        batch_op.drop_column('parent_profile_id')
        batch_op.drop_column('generation_type')
        batch_op.drop_column('source_data_cutoff')
        batch_op.drop_column('source_tokens_used')

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('profile_needs_full_regen')
        batch_op.drop_column('profile_generation_task_id')
        batch_op.drop_column('craft_mode')

    op.drop_table('user_prompt')
    op.drop_table('user_todo')
