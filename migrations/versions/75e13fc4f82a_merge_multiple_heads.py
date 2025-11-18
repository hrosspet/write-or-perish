"""Merge multiple heads

Revision ID: 75e13fc4f82a
Revises: 052779f2c04d, d121c82077bb
Create Date: 2025-04-22 20:11:45.949642

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '75e13fc4f82a'
down_revision = ('052779f2c04d', 'd121c82077bb')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
