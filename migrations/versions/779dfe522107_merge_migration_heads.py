"""Merge migration heads

Revision ID: 779dfe522107
Revises: add_async_task_columns, d585aa0b18c5
Create Date: 2025-11-18 15:16:32.007102

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '779dfe522107'
down_revision = ('add_async_task_columns', 'd585aa0b18c5')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
